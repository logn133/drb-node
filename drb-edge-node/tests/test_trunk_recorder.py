"""
Trunk Recorder backend (node-26#45): config generation from the C2 system
config, the TR call JSON -> MQTT + /upload mapping, the inbox hand-off, the
live-audio follow policy and the health checklist.

No radio here: nothing in this file has run against a real Trunk Recorder.
The sample call JSON below is built from `create_call_json` in
trunk-recorder/call_concluder/call_concluder.cc at tag v5.2.1 (key names and
order as written there); the values are invented.
"""
import json
from pathlib import Path

import pytest

from app.config import settings
from app.internal import health, radio_backend, tr_config, upload_spool
from app.internal import trunk_recorder as trmod
from app.internal.conventional import channel_tgid
from app.internal.tr_calls import parse_call
from app.internal.upload_spool import UploadMeta

# Shape: call_concluder.cc v5.2.1 create_call_json. Values invented.
SAMPLE_CALL = {
    "freq": 851012500,
    "freq_error": -112,
    "signal": -48,
    "noise": -98,
    "source_num": 0,
    "recorder_num": 2,
    "tdma_slot": 0,
    "phase2_tdma": 0,
    "start_time": 1793145600,
    "stop_time": 1793145612,
    "start_time_ms": 1793145600250,
    "stop_time_ms": 1793145612730,
    "emergency": 0,
    "priority": 1,
    "mode": 0,
    "duplex": 0,
    "encrypted": 0,
    "call_length": 12,
    "call_length_ms": 12480,
    "talkgroup": 101,
    "talkgroup_tag": "PD Dispatch",
    "talkgroup_description": "Police Dispatch",
    "talkgroup_group_tag": "Law Dispatch",
    "talkgroup_group": "Police",
    "color_code": -1,
    "audio_type": "digital",
    "short_name": "drb",
    "freqList": [
        {"freq": 851012500, "time": 1793145600, "pos": 0.0, "len": 5.2, "error_count": 3, "spike_count": 1},
        {"freq": 851012500, "time": 1793145606, "pos": 5.2, "len": 7.28, "error_count": 2, "spike_count": 0},
    ],
    "srcList": [
        {"src": 1234567, "time": 1793145600, "pos": 0.0, "emergency": 0, "signal_system": "", "tag": "Car 12",
         "tag_ota": ""},
        {"src": 7654321, "time": 1793145606, "pos": 5.2, "emergency": 1, "signal_system": "", "tag": "",
         "tag_ota": ""},
        {"src": 1234567, "time": 1793145610, "pos": 9.9, "emergency": 0, "signal_system": "", "tag": "Car 12",
         "tag_ota": ""},
        {"src": -1, "time": 1793145611, "pos": 11.0, "emergency": 0, "signal_system": "", "tag": "", "tag_ota": ""},
    ],
}

TR_KW = dict(device_serial="00000101", ppm=1.5, sample_rate=2_400_000, gain=34.0, digital_recorders=4,
             min_duration=1.0, squelch_db=-50.0, live_port=9123)


# ---------------------------------------------------------------------------
# Config generation
# ---------------------------------------------------------------------------

def test_trunked_config_from_c2_system():
    cfg = {"control_channels": [851.0125, 851.2625], "talkgroups": [{"id": 101, "name": "PD Dispatch, Main"},
                                                                    {"id": 202, "name": "Fire"}]}
    tr, files = tr_config.build_tr_config("P25", "County", cfg, **TR_KW)
    assert tr["ver"] == 2
    src, sys_ = tr["sources"][0], tr["systems"][0]
    assert src["driver"] == "osmosdr" and src["device"] == "rtl=00000101"
    assert src["ppm"] == 1.5 and src["gain"] == 34.0 and src["rate"] == 2_400_000
    assert src["digitalRecorders"] == 4
    assert all(abs(f - src["center"]) <= 960_000 for f in (851_012_500, 851_262_500))
    assert all(abs(f - src["center"]) >= tr_config.DC_SPIKE_GUARD_HZ for f in (851_012_500, 851_262_500))
    assert sys_["type"] == "p25" and sys_["control_channels"] == [851_012_500, 851_262_500]
    assert sys_["talkgroupsFile"] == "/app/talkgroups.csv" and sys_["recordUnknown"] is False
    assert sys_["uploadScript"] == "/drb/drb-upload.sh"
    assert sys_["audioArchive"] is False and sys_["callLog"] is False and sys_["compressWav"] is False
    assert tr["tempDir"] == "/dev/shm" and tr["audioStreaming"] is True
    stream = tr["plugins"][0]["streams"][0]
    assert tr["plugins"][0]["library"] == "libsimplestream.so"
    assert stream == {"TGID": 0, "address": "127.0.0.1", "port": 9123, "sendTGID": True}
    lines = files["talkgroups.csv"].splitlines()
    assert lines[0].startswith("Decimal,")       # TR: header required, Decimal first
    assert lines[1] == '101,D,"PD Dispatch, Main","PD Dispatch, Main",,'


def test_trunked_without_talkgroups_records_everything():
    tr, files = tr_config.build_tr_config("P25", "County", {"control_channels": [851.0125]}, **TR_KW)
    assert tr["systems"][0]["recordUnknown"] is True
    assert "talkgroupsFile" not in tr["systems"][0] and not files


def test_conventional_p25_uses_synthetic_tgids():
    cfg = {"mode": "conventional", "channels": [{"freq": 460.400, "name": "PD1", "nac": "293"},
                                                {"freq": 460.425, "name": "PD3"}]}
    tr, files = tr_config.build_tr_config("P25", "WPPD", cfg, **TR_KW)
    sys_ = tr["systems"][0]
    assert sys_["type"] == "conventionalP25" and sys_["channelFile"] == "/app/channels.csv"
    assert "digitalRecorders" not in tr["sources"][0]
    rows = files["channels.csv"].splitlines()
    assert rows[0].startswith("TG Number,Frequency")
    # Same id the OP25 path reports for the channel, so C2 names both alike.
    assert rows[1] == f"{channel_tgid(460_400_000)},460400000,PD1,PD1"


def test_analog_squelch_threshold_carries_over_else_default():
    cfg = {"channels": [{"freq": 154.25, "name": "FD"}], "squelch_mode": "power", "squelch_threshold": -62}
    tr, files = tr_config.build_tr_config("NBFM", "FD", cfg, **TR_KW)
    assert tr["systems"][0]["type"] == "conventional" and tr["systems"][0]["squelch"] == -62.0
    tr, _ = tr_config.build_tr_config("NBFM", "FD", {"channels": [{"freq": 154.25}]}, **TR_KW)
    assert tr["systems"][0]["squelch"] == -50.0
    # Single channel: centre moved off it (DC spike).
    assert abs(tr["sources"][0]["center"] - 154_250_000) >= tr_config.DC_SPIKE_GUARD_HZ
    assert tr_config.live_sample_rate("NBFM", {}) == 16000 and tr_config.live_sample_rate("P25", {}) == 8000


def test_legacy_analog_and_unpinned_sdr():
    kw = {**TR_KW, "device_serial": None}
    tr, _ = tr_config.build_tr_config("NBFM", "Old", {"control_channels": [155.1]}, **kw)
    assert tr["sources"][0]["device"] == "rtl=0"
    assert "155100000" in _["channels.csv"]


@pytest.mark.parametrize("stype,cfg", [
    ("DMR", {"control_channels": [450.0]}),
    ("P25", {"control_channels": []}),
    ("P25", {"mode": "conventional", "channels": [{"freq": 851.0}, {"freq": 855.0}]}),  # 4 MHz: every channel must fit
    ("P25", {"mode": "conventional", "channels": []}),
])
def test_unbuildable_systems_raise(stype, cfg):
    with pytest.raises(ValueError):
        tr_config.build_tr_config(stype, "x", cfg, **TR_KW)


def test_center_override_is_validated():
    assert tr_config.choose_center([851_000_000], 2_400_000, override=851_500_000) == 851_500_000
    with pytest.raises(ValueError):
        tr_config.choose_center([851_000_000], 2_400_000, override=853_000_000)


# ---------------------------------------------------------------------------
# Call JSON -> MQTT + /upload
# ---------------------------------------------------------------------------

def test_parse_call_maps_tr_json():
    call = parse_call(SAMPLE_CALL, "node-001", "101-1793145600.250_851012500-call_7")
    assert call.tgid == 101 and call.tgid_name == "PD Dispatch" and call.freq == 851_012_500
    assert call.start_epoch == pytest.approx(1793145600.25) and call.stop_epoch == pytest.approx(1793145612.73)
    assert call.units == [1234567, 7654321]          # unique, in order, undecoded (-1) dropped
    assert call.emergency is True                     # a unit's emergency flag counts
    assert call.encrypted is False
    assert call.decode_errors == 5 and call.spikes == 1 and call.call_length == pytest.approx(12.48)
    # Deterministic: same JSON -> same id (restart safety), different node -> different id.
    assert parse_call(SAMPLE_CALL, "node-001", "101-1793145600.250_851012500-call_7").call_id == call.call_id
    assert parse_call(SAMPLE_CALL, "node-002", "101-1793145600.250_851012500-call_7").call_id != call.call_id

    start = call.start_payload()
    assert start["tgid"] == 101 and start["tgid_name"] == "PD Dispatch" and start["srcaddr"] == 1234567
    assert start["attributed"] is True and start["driver"] == "trunk-recorder"
    assert start["started_at"].startswith("2026-")
    end = call.end_payload()
    assert end["ended_at_epoch"] == pytest.approx(1793145612.73)
    assert end["audio_start_epoch"] == call.start_epoch and end["units"] == [1234567, 7654321]
    assert end["emergency"] is True and end["encrypted"] is False


def test_upload_form_carries_tr_fields():
    call = parse_call(SAMPLE_CALL, "node-001")
    meta = UploadMeta(call_id=call.call_id, talkgroup_id=call.tgid, talkgroup_name=call.tgid_name,
                      system_id="sys1", audio_start_epoch=call.start_epoch, audio_end_epoch=call.stop_epoch,
                      extra=call.upload_extra())
    form = upload_spool._form(meta)
    # Fields C2's /upload declares today:
    assert form["call_id"] == call.call_id and form["talkgroup_id"] == "101"
    assert form["talkgroup_name"] == "PD Dispatch" and form["system_id"] == "sys1"
    # Extra fields (ignored by C2 until it declares them):
    assert form["units"] == "1234567,7654321" and form["emergency"] == "1" and form["encrypted"] == "0"
    assert form["freq"] == "851012500" and form["recorder_backend"] == "trunk-recorder"
    assert json.loads(form["src_list"])[1] == {"src": 7654321, "time": 1793145606, "pos": 5.2, "emergency": True}
    # Extra can never overwrite a core field.
    meta.extra = {"call_id": "evil"}
    assert upload_spool._form(meta)["call_id"] == call.call_id


def test_parse_call_old_tr_without_ms_and_bad_json():
    old = {k: v for k, v in SAMPLE_CALL.items() if not k.endswith("_ms")}
    call = parse_call(old, "n")
    assert call.start_epoch == 1793145600.0 and call.stop_epoch == 1793145612.0
    with pytest.raises(ValueError):
        parse_call({**SAMPLE_CALL, "talkgroup": 0}, "n")
    with pytest.raises(ValueError):
        parse_call({k: v for k, v in old.items() if k != "start_time"}, "n")


def test_mime_follows_suffix():
    assert upload_spool.mime_for(Path("a.wav")) == "audio/wav"
    assert upload_spool.mime_for(Path("a.flac")) == "audio/flac"


# ---------------------------------------------------------------------------
# Inbox hand-off
# ---------------------------------------------------------------------------

class FakeMQTT:
    is_connected = True

    def __init__(self):
        self.events = []

    async def publish_metadata(self, event, data):
        self.events.append((event, dict(data)))


class FakeRecorder:
    def __init__(self, url="gs://b/calls/x.wav"):
        self.url = url
        self.uploads = []

        class _Spool:
            last_result = {"kind": "retry"}
        self.spool = _Spool()

    async def upload_recording(self, path, call_id, **kw):
        assert path.exists()
        self.uploads.append((path.name, call_id, kw))
        path.unlink()
        return self.url


@pytest.fixture
def tr_env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "recorder_backend", "trunk-recorder")
    monkeypatch.setattr(settings, "tr_inbox_dir", str(tmp_path / "inbox"))
    monkeypatch.setattr(settings, "tr_config_dir", str(tmp_path / "tr"))
    monkeypatch.setattr(settings, "c2_url", "https://c2.example")
    monkeypatch.setattr(trmod, "CALL_START_LEAD_SECONDS", 0)
    mqtt, rec = FakeMQTT(), FakeRecorder()
    import app.internal.call_recorder as cr
    import app.internal.mqtt_manager as mm
    monkeypatch.setattr(mm, "mqtt_manager", mqtt)
    monkeypatch.setattr(cr, "call_recorder", rec)
    (tmp_path / "inbox").mkdir()
    return tmp_path / "inbox", mqtt, rec


def _wav(path: Path, seconds: float = 2.0, rate: int = 8000) -> None:
    import wave
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(seconds * rate))


def _drop(inbox: Path, data=SAMPLE_CALL, stem="101-1793145600.250_851012500-call_7", wav=True, seconds=11.68):
    if wav:
        _wav(inbox / f"{stem}.wav", seconds)
    (inbox / f"{stem}.json").write_text(json.dumps(data))


async def test_inbox_call_is_published_and_uploaded_once(tr_env):
    inbox, mqtt, rec = tr_env
    _drop(inbox)
    t = trmod.TrunkRecorder()
    assert await t.scan_once(wait=True) == 1
    assert [e for e, _ in mqtt.events] == ["call_start", "call_end"]
    start, end = mqtt.events[0][1], mqtt.events[1][1]
    assert len(rec.uploads) == 1
    name, call_id, kw = rec.uploads[0]
    assert call_id == start["call_id"] == end["call_id"]
    # Renamed to the spool's recoverable <date>_<time>_<call_id> form.
    assert name.endswith(f"_{call_id}.wav") and name.count("_") == 2
    assert kw["talkgroup_id"] == 101 and kw["extra"]["units"] == "1234567,7654321"
    assert end["audio_url"] == "gs://b/calls/x.wav"
    # TR drops the gaps between transmissions: the audio ends at start + WAV
    # length (11.68 s), not at stop_time (12.48 s after start).
    assert end["audio_end_epoch"] == pytest.approx(1793145600.25 + 11.68)
    assert kw["audio_end_epoch"] == pytest.approx(1793145600.25 + 11.68)
    assert end["ended_at_epoch"] == pytest.approx(1793145612.73)
    assert json.loads(kw["extra"]["freq_list"])[1]["pos"] == 5.2
    assert list(inbox.iterdir()) == []
    assert await t.scan_once(wait=True) == 0


async def test_encrypted_call_is_reported_not_uploaded(tr_env):
    inbox, mqtt, rec = tr_env
    _drop(inbox, {**SAMPLE_CALL, "encrypted": 1})
    t = trmod.TrunkRecorder()
    await t.scan_once(wait=True)
    assert rec.uploads == []
    assert mqtt.events[-1][1]["audio_skipped"] == "encrypted" and t.encrypted_skipped == 1
    assert list(inbox.iterdir()) == []


async def test_spooled_upload_is_reported(tr_env):
    inbox, mqtt, rec = tr_env
    rec.url = None
    _drop(inbox)
    await trmod.TrunkRecorder().scan_once(wait=True)
    assert mqtt.events[-1][1]["audio_upload"] == "spooled"


async def test_json_waits_for_wav_then_orphan_is_dropped(tr_env, monkeypatch):
    inbox, mqtt, rec = tr_env
    _drop(inbox, wav=False)
    t = trmod.TrunkRecorder()
    await t.scan_once(wait=True)
    assert mqtt.events == [] and len(list(inbox.glob("*.json"))) == 1
    monkeypatch.setattr(trmod, "ORPHAN_JSON_SECONDS", -1)
    await t.scan_once(wait=True)
    # Closed in C2 anyway: a previous pass may have uploaded it and died.
    assert [e for e, _ in mqtt.events] == ["call_end"] and mqtt.events[0][1]["audio_skipped"] == "no_audio"
    assert list(inbox.glob("*.json")) == []


def test_stray_wavs_are_swept_but_claimed_ones_kept(tr_env):
    inbox, _, _ = tr_env
    (inbox / "stray-call_1.wav").write_bytes(b"x")
    (inbox / "waiting-call_2.wav").write_bytes(b"x")
    (inbox / "waiting-call_2.json").write_text("{}")
    claimed = "20261027_120000_" + "0" * 8 + "-0000-0000-0000-" + "0" * 12 + ".wav"
    (inbox / claimed).write_bytes(b"x")
    t = trmod.TrunkRecorder()
    assert t.sweep_stray_wavs() == 0                       # all younger than an hour
    assert t.sweep_stray_wavs(now=4_000_000_000) == 1
    assert sorted(p.name for p in inbox.iterdir()) == sorted([claimed, "waiting-call_2.json", "waiting-call_2.wav"])


async def test_waits_for_mqtt_so_call_start_isnt_lost(tr_env):
    inbox, mqtt, rec = tr_env
    mqtt.is_connected = False
    _drop(inbox)
    assert await trmod.TrunkRecorder().scan_once(wait=True) == 0
    assert rec.uploads == []


async def test_unreadable_json_is_dropped(tr_env):
    inbox, mqtt, rec = tr_env
    (inbox / "bad.wav").write_bytes(b"x")
    (inbox / "bad.json").write_text("{not json")
    await trmod.TrunkRecorder().scan_once(wait=True)
    assert list(inbox.iterdir()) == [] and mqtt.events == []


# ---------------------------------------------------------------------------
# Config files on disk, supervisor requests, backend dispatch
# ---------------------------------------------------------------------------

async def test_apply_config_writes_files_and_restart(tr_env, monkeypatch, tmp_path):
    from app.models import SystemConfig
    import app.internal.secondary_sdr_client as ssc

    async def devices():
        return [{"serial": "00000101", "index": 0}]
    monkeypatch.setattr(ssc.secondary_sdr_client, "devices", devices)
    t = trmod.TrunkRecorder()
    ok = await t.apply_config(SystemConfig(system_id="s1", name="County", type="P25",
                                           config={"control_channels": [851.0125], "talkgroups": [{"id": 1}]}))
    assert ok and t.config_error is None
    d = tmp_path / "tr"
    cfg = json.loads((d / "config.json").read_text())
    assert cfg["sources"][0]["device"] == "rtl=00000101" and (d / "talkgroups.csv").exists()

    bad = await t.apply_config(SystemConfig(system_id="s2", name="Wide", type="P25",
                                            config={"mode": "conventional",
                                                    "channels": [{"freq": 851.0}, {"freq": 856.0}]}))
    assert not bad and "MHz" in t.config_error
    # radio_backend reports the config error instead of pretending to start.
    monkeypatch.setattr(trmod, "trunk_recorder", t)
    assert "MHz" in await radio_backend.start_with_reason()
    t.config_error = None
    monkeypatch.setattr(trmod, "RESTART_POLL_SECONDS", 0.01)
    # start() waits for run.sh to report a restart, so decoders aren't handed
    # the dongle before TR has it.
    monkeypatch.setattr(trmod, "RESTART_WAIT_SECONDS", 0.05)
    assert "isn't running" in await radio_backend.start_with_reason()
    assert (d / "restart-request").exists()
    import time as _t
    (d / "drb-status.json").write_text(json.dumps({"state": "running", "epoch": _t.time(),
                                                   "started_at": _t.time() - 600}))
    assert "didn't restart" in await radio_backend.start_with_reason()   # still the old process
    (d / "drb-status.json").write_text(json.dumps({"state": "running", "epoch": _t.time() + 5,
                                                   "started_at": _t.time() + 5}))
    assert await radio_backend.start_with_reason() is None
    assert await radio_backend.start_with_reason(wait=False) is None
    assert await radio_backend.stop() and (d / "stop-request").exists()
    assert await radio_backend.start() and not (d / "stop-request").exists()


async def test_op25_backend_still_drives_op25(monkeypatch):
    monkeypatch.setattr(settings, "recorder_backend", "op25")
    calls = []

    async def start_with_reason():
        calls.append("start")
        return None
    monkeypatch.setattr(radio_backend.op25_client, "start_with_reason", start_with_reason)
    assert await radio_backend.start() and calls == ["start"]


def test_supervisor_status_and_decode_rate(tr_env, tmp_path):
    d = tmp_path / "tr"
    (d / "logs").mkdir(parents=True)
    t = trmod.TrunkRecorder()
    assert t.supervisor_status() is None
    (d / "drb-status.json").write_text(json.dumps({"state": "running", "epoch": 1000, "started_at": 900}))
    st = t.supervisor_status(now=1005)
    assert st["state"] == "running" and not st["stale"]
    assert t.supervisor_status(now=1100)["stale"]
    (d / "logs" / "10-27-2026_1200_00.log").write_text(
        "[2026-10-27 12:00:03.1] (info)   [drb]\tControl Channel Message Decode Rate: 38/sec, count:  114\n"
        "[2026-10-27 12:00:06.1] (error)  [drb]\tControl Channel Message Decode Rate: 0/sec, count:  0\n")
    assert t.log_facts()["decode_rate"] == 0.0
    assert t.log_tail(1)[0].endswith("count:  0")


# ---------------------------------------------------------------------------
# Live audio: follow one talkgroup
# ---------------------------------------------------------------------------

def test_live_bridge_follows_one_talkgroup():
    b = trmod.LiveAudioBridge()
    b._write = lambda pcm: None
    pkt = lambda tg: tg.to_bytes(4, "little") + b"\x01\x00" * 80   # noqa: E731
    assert b.feed(pkt(101), now=0.0) is not None
    assert b.feed(pkt(202), now=0.5) is None                      # simultaneous call: not mixed in
    assert b.feed(pkt(101), now=1.0) is not None
    assert b.feed(pkt(202), now=1.0 + b.HOLD_SECONDS + 0.1) is not None   # 101 went quiet: switch
    assert b.current_tgid == 202 and b.packets == 4
    assert b.feed(b"\x00\x00", now=9.0) is None


# ---------------------------------------------------------------------------
# Health checklist
# ---------------------------------------------------------------------------

def _facts(tr, mode="trunked", **over):
    now = 1_000_000.0
    f = {
        "now": now, "node_id": "n", "default_password": False, "c2_url": "https://c2", "has_api_key": True,
        "mqtt_connected": True, "mqtt_broker": "m", "mqtt_port": 8883, "sdr_count": 1,
        "source": {"name": "County", "type": "P25", "system_id": "s1", "local": False, "mode": mode},
        "op25_status": "stopped", "console": {}, "audio_capturing": False, "last_voice_epoch": None,
        "icecast_live": False, "icecast_port": 8000, "backend": "trunk-recorder", "tr": tr,
    }
    f.update(over)
    return {c["id"]: c for c in health.build_checks(f)}


def _tr(**over):
    now = 1_000_000.0
    t = {"status": {"state": "running", "started_at": now - 600, "restarts": 0, "stale": False, "age_seconds": 2},
         "log_age_seconds": 3, "decode_rate": 38.0, "config_error": None, "inbox_backlog": 0,
         "calls_ingested": 5, "encrypted_skipped": 0, "last_call_epoch": now - 30, "last_call": None,
         "live": {"last_packet_epoch": now - 30}}
    t.update(over)
    return t


def test_health_tr_running_and_decoding():
    c = _facts(_tr())
    assert "op25" not in c and c["recorder"]["state"] == "ok"
    assert c["decode"]["state"] == "ok" and "38" in c["decode"]["summary"]
    assert c["audio"]["state"] == "ok" and c["stream"]["state"] == "ok"


@pytest.mark.parametrize("tr,cid,state", [
    (None, "recorder", "error"),
    ({"status": None}, "recorder", "error"),                               # profile not enabled
    ({"status": {"state": "crashed", "last_exit": 1, "stale": False}}, "recorder", "error"),
    ({"status": {"state": "running", "stale": True, "age_seconds": 60}}, "recorder", "error"),
    ({"config_error": "too wide"}, "recorder", "error"),
    ({"decode_rate": 0.0}, "decode", "error"),
    ({"decode_rate": None}, "decode", "warn"),
    ({"inbox_backlog": 50}, "audio", "warn"),
])
def test_health_tr_failures(tr, cid, state):
    facts = None if tr is None else _tr(**tr)
    assert _facts(facts)[cid]["state"] == state


def test_health_tr_conventional_has_no_decode_rate():
    assert _facts(_tr(decode_rate=None), mode="conventional")["decode"]["state"] == "ok"


def test_wide_control_channel_list_is_cut_to_what_one_sdr_covers():
    # MTA Zone 2 (node-002): five control channels spanning 4.15 MHz.
    ccs = [770_156_250, 770_431_250, 773_293_750, 773_843_750, 774_306_250]
    assert tr_config.widest_cover(ccs, 2_400_000) == ccs[2:]
