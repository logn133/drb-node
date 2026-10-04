"""
P25 conventional (node-26#24): config generation, the liquidsoap two-channel
variant, and the tk_drb_conv state machine — all without OP25/GNU Radio.

tk_drb_conv imports tk_p25/helper_funcs/log_ts from OP25's apps dir; those are
stubbed here with the minimum of tk_p25.rx_ctl it relies on. The message
shapes fed in are the ones boatbod gr310 p25p1_fdma.cc sends: LCW = 2-byte NAC
+ 9 LC bytes (type 19), LDU2 ESS = JSON (type -3), TDU = 2-byte NAC (type 3/15),
sync timeout = type -1.
"""
import json
import os
import sys
import types

import pytest

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, "..", "app"))
sys.path.insert(0, os.path.join(HERE, "..", "op25_addons"))

from fastapi.testclient import TestClient  # noqa: E402
from fastapi import FastAPI  # noqa: E402

from models import ConventionalChannel  # noqa: E402
from internal import conventional_config as cc  # noqa: E402
from internal.liquidsoap_config_utils import render_liquid_script  # noqa: E402
from models import IcecastConfig  # noqa: E402


# ---------------------------------------------------------------------------
# Config generation
# ---------------------------------------------------------------------------

WP = [
    ConventionalChannel(frequency=460_400_000, name="PD1 Dispatch"),
    ConventionalChannel(frequency=460_425_000, name="PD3 Tac", nac=0x293),
]


def test_white_plains_two_channels_one_sdr():
    config, two = cc.build_conventional_config("White Plains", WP, "lna:34", 0.0)
    assert two is True
    assert "trunking" in config and config["trunking"]["module"] == "tk_drb_conv.py"
    assert config["trunking"]["chans"] == []          # nothing for tk_p25 to hunt
    a, b = config["channels"]
    for ch in (a, b):
        assert ch["demod_type"] == "fsk4"
        assert ch["trunking_sysname"] == ""
        assert ch["enable_analog"] == "off"
        assert ch["crypt_behavior"] == 2
        assert "control_channel_list" not in ch
    assert (a["frequency"], b["frequency"]) == (460_400_000, 460_425_000)
    assert a["destination"].endswith(":23456") and b["destination"].endswith(":23457")
    assert a["name"] == "PD1 Dispatch" and b["nac"] == 0x293
    dev = config["devices"][0]
    assert dev["tunable"] is False
    # 25 kHz apart: centre is offset off both channels (DC spike), and both
    # still sit inside the usable passband.
    half = dev["rate"] * dev["usable_bw_pct"] / 2
    for f in (460_400_000, 460_425_000):
        assert 100_000 < abs(f - dev["frequency"]) < half


def test_single_channel_is_not_two_channel_audio():
    config, two = cc.build_conventional_config("X", WP[:1], "lna:34", 0.0)
    assert two is False
    assert len(config["channels"]) == 1


def test_wide_pair_centres_between_channels():
    chans = [ConventionalChannel(frequency=453_000_000), ConventionalChannel(frequency=454_000_000)]
    config, _ = cc.build_conventional_config("X", chans, "lna:34", 0.0)
    assert config["devices"][0]["frequency"] == 453_500_000


@pytest.mark.parametrize("chans, needle", [
    ([], "at least one"),
    ([ConventionalChannel(frequency=f) for f in (460_000_000, 460_025_000, 460_050_000)], "up to 2"),
    ([ConventionalChannel(frequency=460_000_000), ConventionalChannel(frequency=462_000_000)], "too far"),
    ([ConventionalChannel(frequency=460_000_000), ConventionalChannel(frequency=460_000_000)], "twice"),
    ([ConventionalChannel(frequency=460_000_000, nac=0x1000)], "NAC"),
])
def test_rejects_bad_channel_sets(chans, needle):
    with pytest.raises(ValueError, match=needle):
        cc.build_conventional_config("X", chans, "lna:34", 0.0)


ICE = IcecastConfig(icecast_host="localhost", icecast_port=8000, icecast_mountpoint="/n", icecast_password="p")


def test_liquidsoap_single_channel_unchanged_command():
    liq = render_liquid_script(ICE, two_channel=False)
    assert '"./audio.py -x 2.5 -s"' in liq
    assert "amplify" not in liq and "${" not in liq


def test_liquidsoap_two_channel():
    liq = render_liquid_script(ICE, two_channel=True)
    assert '"./audio.py -x 2.5 -s -2"' in liq
    assert "input = amplify(2.0, input)" in liq
    assert "${" not in liq


def test_generate_config_route(tmp_path, monkeypatch):
    from routers import op25_controller
    monkeypatch.setattr(op25_controller, "_ACTIVE_CFG", str(tmp_path / "active.cfg.json"))
    written = {}
    monkeypatch.setattr(op25_controller, "generate_liquid_script",
                        lambda ice, two_channel=False: written.setdefault("two", two_channel))
    monkeypatch.setattr(op25_controller, "save_talkgroup_tags", lambda tags: None)
    monkeypatch.setattr(op25_controller, "save_whitelist", lambda wl: None)
    app = FastAPI()
    app.include_router(op25_controller.create_op25_router(), prefix="/op25")
    client = TestClient(app)
    ice = ICE.model_dump()
    r = client.post("/op25/generate-config", json={
        "type": "P25", "systemName": "White Plains", "p25_mode": "conventional",
        "conv_channels": [{"frequency": 460400000, "name": "PD1"}, {"frequency": 460425000, "name": "PD3"}],
        "icecastConfig": ice,
    })
    assert r.status_code == 200, r.text
    cfg = json.loads((tmp_path / "active.cfg.json").read_text())
    assert cfg["trunking"]["module"] == "tk_drb_conv.py"
    assert [c["frequency"] for c in cfg["channels"]] == [460400000, 460425000]
    assert written["two"] is True

    # Trunked P25 without p25_mode is exactly what it was.
    r = client.post("/op25/generate-config", json={
        "type": "P25", "systemName": "County", "channels": [851012500], "icecastConfig": ice,
    })
    assert r.status_code == 200, r.text
    cfg = json.loads((tmp_path / "active.cfg.json").read_text())
    assert cfg["trunking"]["module"] == "tk_p25.py"
    assert cfg["trunking"]["chans"][0]["control_channel_list"] == "851012500"
    assert cfg["channels"][0]["demod_type"] == "cqpsk"

    r = client.post("/op25/generate-config", json={
        "type": "P25", "systemName": "X", "p25_mode": "conventional", "conv_channels": [],
    })
    assert r.status_code == 400

    # Analog uses the same module, so the edge node sees a `conventional` row
    # whatever tk_p25 the image was built with (node-26#20).
    r = client.post("/op25/generate-config", json={
        "type": "NBFM", "systemName": "Fire", "channels": [154430000], "icecastConfig": ice,
    })
    assert r.status_code == 200, r.text
    cfg = json.loads((tmp_path / "active.cfg.json").read_text())
    assert cfg["trunking"]["module"] == "tk_drb_conv.py"
    assert cfg["channels"][0]["frequency"] == 154430000


# ---------------------------------------------------------------------------
# tk_drb_conv state machine
# ---------------------------------------------------------------------------

class _BaseRxCtl:
    """The slice of tk_p25.rx_ctl that tk_drb_conv relies on."""

    def __init__(self, debug=0, frequency_set=None, nbfm_ctrl=None, fa_ctrl=None, chans={}):
        self.fa_ctrl = fa_ctrl
        self.receivers = {}
        self.call_log = []

    def add_receiver(self, msgq_id, config, meta_q=None, freq=0):
        self.receivers[msgq_id] = {'msgq_id': msgq_id, 'config': config, 'rx_rcvr': None,
                                   'conv_state': {'tgid': None}}

    def post_init(self):
        pass

    def process_qmsg(self, msg):
        self.base_saw = msg

    def log_call(self, sysid, rcvr, freq, slot, prio, tgid, tgtag, rid, rtag):
        self.call_log.append({"rcvr": rcvr, "freq": freq, "tgid": tgid, "tgtag": tgtag, "rid": rid})

    def get_chan_status(self):
        return json.dumps({'json_type': 'channel_update', 'channels': []})


def _stub_op25_modules():
    tk = types.ModuleType("tk_p25")
    tk.rx_ctl = _BaseRxCtl
    hf = types.ModuleType("helper_funcs")

    def from_dict(d, key, def_val):
        return d[key] if key in d and d[key] != "" else def_val

    def get_ordinals(s):
        t = 0
        for c in s:
            t = (t << 8) + c
        return t
    hf.from_dict = from_dict
    hf.get_ordinals = get_ordinals
    lt = types.ModuleType("log_ts")
    lt.log_ts = types.SimpleNamespace(get=lambda *a: "ts")
    sys.modules.update({"tk_p25": tk, "helper_funcs": hf, "log_ts": lt})


class _Msg:
    def __init__(self, m_type, rxid, payload=b""):
        self._type = m_type & 0xffff   # proto 0 in the upper 16 bits
        self._arg1 = rxid << 1
        self._payload = payload

    def type(self):
        return self._type

    def arg1(self):
        return self._arg1

    def to_string(self):
        return self._payload


def _lcw(nac, tgid, src, svcopts=0, lco=0x00):
    lc = bytes([lco, 0x00, svcopts, 0x00]) + tgid.to_bytes(2, "big") + src.to_bytes(3, "big")
    return nac.to_bytes(2, "big") + lc


def _ess(encrypted, algid, keyid):
    return json.dumps({"encrypted": encrypted, "algid": algid, "keyid": keyid}).encode()


@pytest.fixture
def tk():
    _stub_op25_modules()
    sys.modules.pop("tk_drb_conv", None)
    import tk_drb_conv
    sent = []
    rx = tk_drb_conv.rx_ctl(fa_ctrl=sent.append, chans=[])
    rx.add_receiver(0, {"name": "PD1 Dispatch", "nac": 0x293}, freq=460_400_000)
    rx.add_receiver(1, {"name": "PD3 Tac"}, freq=460_425_000)
    rx.post_init()
    rx.sent = sent
    return rx


def _rows(rx):
    d = json.loads(rx.get_chan_status())
    return {cid: d[cid] for cid in d["channels"]}


def test_nac_filter_applied_only_where_configured(tk):
    assert tk.sent == [{'tuner': 0, 'cmd': 'set_nac', 'nac': 0x293}]


def test_clear_call_reports_tgid_unit_nac_and_logs_once(tk):
    tk.process_qmsg(_Msg(0, 0, b"\x02\x93"))                       # HDU
    tk.process_qmsg(_Msg(19, 0, _lcw(0x293, 101, 4242)))            # LDU1
    tk.process_qmsg(_Msg(-3, 0, _ess(0, 0x80, 0)))                  # LDU2 clear
    tk.process_qmsg(_Msg(19, 0, _lcw(0x293, 101, 4242)))
    row = _rows(tk)["0"]
    assert row["conventional"] and row["voice"]
    assert (row["tgid"], row["srcaddr"], row["nac"], row["encrypted"]) == (101, 4242, 0x293, 0)
    assert row["algid"] is None
    assert [e["tgid"] for e in tk.call_log] == [101]
    assert tk.call_log[0]["tgtag"] == "PD1 Dispatch"
    # The other channel is idle.
    assert _rows(tk)["1"]["voice"] is False

    tk.process_qmsg(_Msg(3, 0, b"\x02\x93"))                         # TDU
    row = _rows(tk)["0"]
    assert row["voice"] is False and row["tgid"] is None
    assert (row["clear_calls"], row["enc_calls"]) == (1, 0)


def test_tdulc_link_control_does_not_reopen_the_call(tk):
    tk.process_qmsg(_Msg(19, 1, _lcw(0x111, 7, 9)))
    tk.process_qmsg(_Msg(15, 1, b"\x01\x11"))                        # TDULC
    tk.process_qmsg(_Msg(19, 1, _lcw(0x111, 7, 9)))                 # its LC
    assert _rows(tk)["1"]["voice"] is False


def test_encrypted_call_is_reported_and_never_logged(tk):
    tk.process_qmsg(_Msg(19, 1, _lcw(0x111, 7, 9, svcopts=0x40)))  # E bit
    tk.process_qmsg(_Msg(-3, 1, _ess(1, 0x84, 0x1234)))
    row = _rows(tk)["1"]
    assert row["encrypted"] == 1 and row["algid"] == 0x84 and row["keyid"] == 0x1234
    assert tk.call_log == []
    tk.process_qmsg(_Msg(-1, 1))                                    # sync timeout
    row = _rows(tk)["1"]
    assert row["encrypted"] == 0 and row["voice"] is False
    assert (row["enc_calls"], row["last_enc_algid"], row["last_enc_keyid"]) == (1, 0x84, 0x1234)


def test_encryption_seen_only_in_ess_still_counts(tk):
    tk.process_qmsg(_Msg(-3, 0, _ess(1, 0x81, 0x0001)))
    assert _rows(tk)["0"]["encrypted"] == 1
    tk.process_qmsg(_Msg(3, 0, b"\x02\x93"))
    assert _rows(tk)["0"]["enc_calls"] == 1


def test_protected_link_control_marks_encrypted(tk):
    tk.process_qmsg(_Msg(19, 0, _lcw(0x293, 0, 0, lco=0x80)))
    assert _rows(tk)["0"]["encrypted"] == 1


def test_trunked_receivers_and_other_protocols_go_to_base(tk):
    msg = _Msg(7, 5)
    tk.process_qmsg(msg)
    assert tk.base_saw is msg
