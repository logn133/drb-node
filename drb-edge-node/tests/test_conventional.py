"""
Conventional channels: P25 conventional (node-26#24) and analog (node-26#20).

Covers the generate-config payload (radio_config), the console normalizer
(conventional.py) and how the watcher attributes conventional audio. Channel
rows are shaped like tk_drb_conv's / tk_p25's conventional `channel_update`
entries; grants like tk_p25.log_call entries.
"""
import pytest
from unittest.mock import AsyncMock

from app.config import settings
from app.internal import radio_config as rc
from app.internal.conventional import ConventionalChannels, channel_tgid, NON_DISTINCT_TGIDS
from app.internal.metadata_watcher import MetadataWatcher
from tests.test_metadata_watcher import FakeAudio, FakeClock, tick, update

PD1, PD3 = 460_400_000, 460_425_000


def conv_row(msgqid=0, freq=PD1, name="PD1 Dispatch", tgid=None, srcaddr=0, voice=False,
             encrypted=0, **extra):
    row = {
        "freq": freq, "name": name, "tgid": tgid, "tag": name, "srcaddr": srcaddr,
        "encrypted": encrypted, "conventional": True, "msgqid": msgqid, "voice": voice,
    }
    row.update(extra)
    return row


def conv_grant(rcvr, tgid, at, freq=PD1, rid=4242):
    return {"time": at, "sysid": 0, "rcvr": rcvr, "rcvrtag": "", "freq": freq, "slot": 0,
            "prio": 0, "tgid": tgid, "tgtag": "", "rid": rid, "rtag": ""}


# ---------------------------------------------------------------------------
# radio_config: system document -> op25 generate-config payload
# ---------------------------------------------------------------------------

ICE = {"icecast_host": "localhost", "icecast_port": 8000, "icecast_mountpoint": "/n", "icecast_password": "p"}

WHITE_PLAINS = {
    "mode": "conventional",
    "channels": [
        {"freq": 460.4, "name": "PD1 Dispatch"},
        {"freq": 460.425, "name": "PD3 Tac", "nac": "293"},
    ],
    "talkgroups": [],
}


def test_conventional_payload():
    p = rc.build_generate_payload("P25", "White Plains", WHITE_PLAINS, ICE, "rtl-sdr-v3", None)
    assert p["type"] == "P25" and p["p25_mode"] == "conventional"
    assert p["channels"] == []    # nothing that could become a control channel
    assert p["conv_channels"] == [
        {"frequency": PD1, "name": "PD1 Dispatch", "nac": None},
        {"frequency": PD3, "name": "PD3 Tac", "nac": 0x293},
    ]
    assert "ppm_override" not in p


def test_trunked_payload_unchanged_when_mode_missing():
    cfg = {"control_channels": [851.0125, 851.5125], "talkgroups": [{"id": 9048, "name": "Ossining PD"}]}
    p = rc.build_generate_payload("P25", "County", cfg, ICE, "other", 1.5)
    assert "p25_mode" not in p
    assert p["channels"] == [851_012_500, 851_512_500]
    assert p["tags"] == [{"talkgroup": "Ossining PD", "tagDec": 9048}]
    assert p["whitelist"] == [9048]
    assert p["ppm_override"] == 1.5


@pytest.mark.parametrize("cfg", [
    {"channels": [{"freq": 154.43, "name": "Fire"}]},
    {"control_channels": [154.43]},                     # saved before #24
])
def test_analog_payload_reads_channels_or_legacy(cfg):
    p = rc.build_generate_payload("NBFM", "Fire", cfg, ICE, "rtl-sdr-v3", None)
    assert p["channels"] == [154_430_000]
    assert "p25_mode" not in p
    assert p["nbfm_squelch_mode"] == "noise"   # node-26#46 default


def test_analog_squelch_is_plumbed_and_validated():
    cfg = {"channels": [{"freq": 154.43}], "squelch_mode": "Power", "squelch_threshold": "-55", "squelch_db": 12}
    p = rc.build_generate_payload("NBFM", "Fire", cfg, ICE, "rtl-sdr-v3", None)
    assert p["nbfm_squelch_mode"] == "power" and p["nbfm_squelch"] == -55.0 and p["nbfm_noise_squelch_db"] == 12.0
    with pytest.raises(ValueError):
        rc.analog_squelch({"squelch_mode": "loud"})
    with pytest.raises(ValueError):
        rc.analog_squelch({"squelch_db": "lots"})


def test_system_mode():
    assert rc.system_mode("P25", {}) == rc.TRUNKED
    assert rc.system_mode("P25", {"mode": "Conventional"}) == rc.CONVENTIONAL
    assert rc.system_mode("NBFM", {"mode": "conventional"}) == rc.ANALOG
    assert rc.system_mode("DMR", {}) == rc.DMR


@pytest.mark.parametrize("raw, want", [("293", 0x293), ("0x293", 0x293), (0x293, 0x293), ("", None), (None, None)])
def test_parse_nac(raw, want):
    assert rc.parse_nac(raw) == want


@pytest.mark.parametrize("raw", ["xyz", "1000", 0, True])
def test_parse_nac_rejects(raw):
    with pytest.raises(ValueError):
        rc.parse_nac(raw)


def test_conventional_channel_without_freq_rejected():
    with pytest.raises(ValueError):
        rc.conventional_channels({"channels": [{"name": "no freq"}]})


# ---------------------------------------------------------------------------
# Normalizer
# ---------------------------------------------------------------------------

def test_synthetic_tgid_is_frequency_in_100hz_and_clear_of_p25_space():
    assert channel_tgid(PD1) == 4_604_000
    assert channel_tgid(460.4) == 4_604_000
    assert channel_tgid(460_406_250) != channel_tgid(460_412_500)   # 6.25 kHz steps stay distinct
    assert channel_tgid(24_000_000) > 0xFFFF                        # lowest RTL-SDR frequency


def test_non_distinct_link_control_tgid_becomes_the_channel():
    conv = ConventionalChannels()
    up = update(channels=[conv_row(0, PD1, tgid=1, srcaddr=7, voice=True),
                          conv_row(1, PD3, "PD3 Tac", tgid=1, srcaddr=8, voice=True)])
    conv.normalize(up)
    assert [r["tgid"] for r in up.channels] == [4_604_000, 4_604_250]
    assert [r["lc_tgid"] for r in up.channels] == [1, 1]
    assert [r["tag"] for r in up.channels] == ["PD1 Dispatch", "PD3 Tac"]
    assert all(r["drb_active"] for r in up.channels)
    assert 1 in NON_DISTINCT_TGIDS and 0xFFFF in NON_DISTINCT_TGIDS


def test_distinct_link_control_tgid_is_kept():
    conv = ConventionalChannels()
    up = update(channels=[conv_row(0, tgid=101, srcaddr=7)],
                call_log=[conv_grant(0, 101, 1.0)])
    conv.normalize(up)
    assert up.channels[0]["tgid"] == 101
    assert up.call_log[0]["tgid"] == 101
    assert up.call_log[0]["tgtag"] == "PD1 Dispatch"


def test_grants_are_mapped_per_receiver():
    conv = ConventionalChannels()
    up = update(channels=[conv_row(0, PD1), conv_row(1, PD3, "PD3 Tac")],
                call_log=[conv_grant(1, 1, 1.0, freq=PD3)])
    conv.normalize(up)
    assert up.call_log[0]["tgid"] == 4_604_250
    assert up.call_log[0]["tgtag"] == "PD3 Tac"


def test_voice_without_link_control_is_still_a_talker():
    conv = ConventionalChannels()
    up = update(channels=[conv_row(0, voice=True)])
    conv.normalize(up)
    assert up.channels[0]["tgid"] == 4_604_000 and up.channels[0]["drb_active"]


def test_idle_and_encrypted_channels_are_not_talkers():
    conv = ConventionalChannels()
    up = update(channels=[conv_row(0), conv_row(1, PD3, "PD3 Tac", tgid=1, voice=True, encrypted=1)])
    conv.normalize(up)
    assert [r["tgid"] for r in up.channels] == [None, None]


def test_trunked_rows_untouched():
    conv = ConventionalChannels()
    row = {"freq": 851_000_000, "tgid": 9048, "srcaddr": 5, "tag": "PD", "msgqid": 0}
    up = update(channels=[dict(row)], call_log=[conv_grant(0, 9048, 1.0)])
    conv.normalize(up)
    assert up.channels[0] == row
    assert up.call_log[0]["tgid"] == 9048
    assert conv.sole_channel() is None


def test_sole_channel_only_with_exactly_one_conventional_channel():
    conv = ConventionalChannels()
    conv.normalize(update(channels=[conv_row(0, 154_430_000, "Fire")]))
    assert conv.sole_channel() == {"tgid": 1_544_300, "name": "Fire", "freq": 154_430_000}
    conv.normalize(update(channels=[conv_row(0), conv_row(1, PD3)]))
    assert conv.sole_channel() is None
    # A poll without channel_update does not forget the channel list.
    conv.normalize(update())
    assert conv.sole_channel() is None


def test_encryption_report():
    conv = ConventionalChannels()
    conv.normalize(update(channels=[
        conv_row(0, enc_calls=3, clear_calls=0, last_enc_time=50.0, last_enc_algid=0x84,
                 last_enc_keyid=0x1234, nac=0x293),
        conv_row(1, PD3, "PD3 Tac", enc_calls=1, clear_calls=5, last_enc_time=10.0, last_clear_time=20.0),
    ]))
    pd1, pd3 = conv.encryption_report()
    assert pd1["encrypted"] is True and pd1["algid"] == 0x84 and pd1["keyid"] == 0x1234 and pd1["nac"] == 0x293
    assert pd3["encrypted"] is False   # its most recent call was clear


# ---------------------------------------------------------------------------
# Watcher attribution
# ---------------------------------------------------------------------------

@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def audio(clock):
    return FakeAudio(clock)


@pytest.fixture
def hearing(clock, audio):
    w = MetadataWatcher()
    w._clock = clock
    w.on_call_start = AsyncMock()
    w.on_call_end = AsyncMock()
    w.audio_activity = audio
    return w


async def _finish(hearing, clock, channels):
    clock.advance(settings.call_silence_timeout + 0.5)
    await tick(hearing, update(channels=channels))
    return hearing.on_call_end.call_args[0][0]


@pytest.mark.asyncio
async def test_conventional_call_attributed_to_its_channel(hearing, clock, audio):
    """White Plains: both channels carry the default TGID 1; the keyed one wins."""
    audio.speak()
    await tick(hearing, update(
        channels=[conv_row(0, tgid=1, srcaddr=4242, voice=True), conv_row(1, PD3, "PD3 Tac")],
        call_log=[conv_grant(0, 1, clock.now)],
    ))
    assert hearing.is_active and hearing.current_tgid == 4_604_000

    ended = await _finish(hearing, clock, [conv_row(0), conv_row(1, PD3, "PD3 Tac")])
    assert ended["attributed"] is True
    assert (ended["tgid"], ended["tgid_name"], ended["srcaddr"]) == (4_604_000, "PD1 Dispatch", 4242)
    assert hearing.unattributed_segments == 0


@pytest.mark.asyncio
async def test_other_channel_keying_splits(hearing, clock, audio):
    audio.speak()
    await tick(hearing, update(
        channels=[conv_row(0, tgid=1, voice=True), conv_row(1, PD3, "PD3 Tac")],
        call_log=[conv_grant(0, 1, clock.now)],
    ))
    clock.advance(0.5)
    audio.speak()
    await tick(hearing, update(
        channels=[conv_row(0), conv_row(1, PD3, "PD3 Tac", tgid=1, voice=True)],
        call_log=[conv_grant(1, 1, clock.now, freq=PD3)],
    ))
    first = hearing.on_call_end.call_args[0][0]
    assert first["tgid"] == 4_604_000 and first["end_reason"] == "tgid_change"
    assert hearing.current_tgid == 4_604_250


@pytest.mark.asyncio
async def test_single_analog_channel_audio_is_kept_not_orphaned(hearing, clock, audio):
    """node-26#20: analog never reports a talker; the only channel is the answer."""
    audio.speak()
    rows = [conv_row(0, 154_430_000, "County Fire")]
    await tick(hearing, update(channels=rows))
    # Not labelled at open — only at close.
    assert hearing.on_call_start.call_args[0][0]["tgid"] is None

    ended = await _finish(hearing, clock, rows)
    assert ended["attributed"] is True
    assert (ended["tgid"], ended["tgid_name"]) == (1_544_300, "County Fire")
    assert hearing.unattributed_segments == 0


@pytest.mark.asyncio
async def test_two_channels_with_no_evidence_is_still_orphan(hearing, clock, audio):
    audio.speak()
    rows = [conv_row(0), conv_row(1, PD3, "PD3 Tac")]
    await tick(hearing, update(channels=rows))
    ended = await _finish(hearing, clock, rows)
    assert ended["attributed"] is False
    assert hearing.unattributed_segments == 1


@pytest.mark.asyncio
async def test_late_real_talkgroup_on_single_channel_does_not_split(hearing, clock, audio):
    """Audio first, link-control grant a poll later: adopt it, don't split."""
    audio.speak()
    await tick(hearing, update(channels=[conv_row(0)]))
    clock.advance(0.5)
    audio.speak()
    await tick(hearing, update(channels=[conv_row(0, tgid=101, srcaddr=9, voice=True)],
                               call_log=[conv_grant(0, 101, clock.now - 0.3)]))
    assert hearing.current_tgid == 101
    hearing.on_call_end.assert_not_called()
    ended = await _finish(hearing, clock, [conv_row(0)])
    assert ended["tgid"] == 101 and ended["end_reason"] == "audio_silence"


@pytest.mark.asyncio
async def test_encrypted_channel_does_not_label_clear_audio(hearing, clock, audio):
    """PD3 is keyed encrypted (muted) while PD1 talks in the clear."""
    audio.speak()
    await tick(hearing, update(channels=[
        conv_row(0, tgid=1, voice=True),
        conv_row(1, PD3, "PD3 Tac", tgid=1, voice=True, encrypted=1, algid=0x84),
    ]))
    ended = await _finish(hearing, clock, [conv_row(0), conv_row(1, PD3, "PD3 Tac")])
    assert ended["tgid"] == 4_604_000


@pytest.mark.asyncio
async def test_conventional_channels_property(hearing, clock):
    await tick(hearing, update(channels=[conv_row(0, encrypted=1, voice=True, algid=0x84, keyid=7)]))
    (only,) = hearing.conventional_channels
    assert only["encrypted"] and only["encrypted_now"] and only["algid"] == 0x84


# drb-audio-review findings on #24: a header frame (voice, no link control yet)
# must not label the call with the synthetic id and then split against the
# real talkgroup a poll later; nor may a reply's header split it back.

@pytest.mark.asyncio
async def test_header_before_link_control_does_not_split(hearing, clock, audio):
    audio.speak()
    await tick(hearing, update(channels=[conv_row(0, voice=True)]))       # HDU only
    clock.advance(0.5)
    audio.speak()
    await tick(hearing, update(channels=[conv_row(0, tgid=101, srcaddr=9, voice=True)],
                               call_log=[conv_grant(0, 101, clock.now - 0.2)]))
    hearing.on_call_end.assert_not_called()
    assert hearing.current_tgid == 101
    ended = await _finish(hearing, clock, [conv_row(0)])
    assert ended["tgid"] == 101 and ended["end_reason"] == "audio_silence"
    assert hearing.on_call_end.call_count == 1


@pytest.mark.asyncio
async def test_reply_header_inside_conversation_does_not_ping_pong(hearing, clock, audio):
    audio.speak()
    await tick(hearing, update(channels=[conv_row(0, tgid=101, srcaddr=9, voice=True)],
                               call_log=[conv_grant(0, 101, clock.now)]))
    clock.advance(0.5)
    await tick(hearing, update(channels=[conv_row(0)]))                   # TDU
    clock.advance(0.5)
    audio.speak()
    await tick(hearing, update(channels=[conv_row(0, voice=True)]))       # reply's HDU
    clock.advance(0.5)
    audio.speak()
    await tick(hearing, update(channels=[conv_row(0, tgid=101, srcaddr=12, voice=True)],
                               call_log=[conv_grant(0, 101, clock.now)]))
    hearing.on_call_end.assert_not_called()
    ended = await _finish(hearing, clock, [conv_row(0)])
    assert ended["tgid"] == 101 and hearing.on_call_end.call_count == 1


@pytest.mark.asyncio
async def test_never_decoded_link_control_then_real_tg_relabels(hearing, clock, audio):
    """First-ever call on a channel: synthetic label, then the real TG adopts it."""
    audio.speak()
    await tick(hearing, update(channels=[conv_row(0, voice=True)]))
    clock.advance(0.5)
    audio.speak()
    await tick(hearing, update(channels=[conv_row(0, voice=True)]))
    assert hearing.current_tgid == 4_604_000
    clock.advance(0.5)
    audio.speak()
    await tick(hearing, update(channels=[conv_row(0, tgid=101, voice=True)],
                               call_log=[conv_grant(0, 101, clock.now)]))
    hearing.on_call_end.assert_not_called()
    assert hearing.current_tgid == 101


def test_voice_without_link_control_uses_channels_last_talkgroup():
    conv = ConventionalChannels()
    conv.normalize(update(channels=[conv_row(0, tgid=101, voice=True)]))
    up = update(channels=[conv_row(0, voice=True)])
    conv.normalize(up)
    assert up.channels[0]["tgid"] == 101


def test_same_channel_relabel_is_conventional_only():
    conv = ConventionalChannels()
    assert conv.same_channel_relabel(4_604_000, PD1, 101, PD1) is None    # no conventional rows known
    conv.normalize(update(channels=[conv_row(0), conv_row(1, PD3)]))
    assert conv.same_channel_relabel(4_604_000, PD1, 101, PD1) == "adopt"
    assert conv.same_channel_relabel(101, PD1, 4_604_000, PD1) == "keep"
    assert conv.same_channel_relabel(4_604_000, PD1, 4_604_250, PD3) is None   # other channel: split


def test_remembered_talkgroup_expires():
    conv = ConventionalChannels()
    t = [1000.0]
    conv._clock = lambda: t[0]
    conv.normalize(update(channels=[conv_row(0, tgid=101, voice=True)]))
    t[0] += 10
    up = update(channels=[conv_row(0, voice=True)])
    conv.normalize(up)
    assert up.channels[0]["tgid"] == 101
    t[0] += 3600
    up = update(channels=[conv_row(0, voice=True)])
    conv.normalize(up)
    assert up.channels[0]["tgid"] == 4_604_000
