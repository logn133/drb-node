"""
tk_drb_conv — DRB's trunking module for P25 CONVENTIONAL channels (node-26#24).

Copied into OP25's apps directory by the Dockerfile and named as the trunking
module by op25-container's conventional config, so multi_rx imports it in place
of tk_p25. It is tk_p25.rx_ctl with the conventional-channel handling replaced;
trunked receivers (none in a conventional config) still go to the base class.

Why not stock tk_p25: for an FDMA conventional channel it reports the talkgroup
and unit id from LDU1 link control, but

  * never reports encryption — the encrypted/algid/keyid JSON OP25 sends after
    every LDU2 carries no grpaddr, and tk_p25 only records `encrypted` when
    grpaddr != 0 — so an encrypted channel looks exactly like a quiet one;
  * never reports the NAC it is hearing, and has no per-channel NAC filter;
  * has no "voice frames are arriving" signal of its own, so a transmission
    whose link control is unreadable is invisible to the console.

All three matter to the edge node: it attributes audio to a channel from what
this module reports, and must tell the owner when a channel is encrypted rather
than silently recording nothing.

Each conventional channel row in `channel_update` keeps every key tk_p25 sends
(freq, tgid, srcaddr, tag, encrypted, conventional, ...) and adds:

    voice            True while voice frames are arriving (HDU/LDU1/LDU2 seen,
                     no TDU or sync timeout since)
    nac              last NAC heard on the channel (int) or None
    nac_filter       the configured NAC filter (int) or None
    algid, keyid     ALGID/key id of the current call (None when clear/idle)
    enc_calls, clear_calls     calls heard since start, by encryption
    last_enc_algid, last_enc_keyid, last_enc_time, last_clear_time

Message types handled (lower 16 bits of msg.type(), P25 protocol only):
    0          HDU                 -> voice start
    3, 15      TDU / TDULC         -> transmission end
    19         LDU1 link control   -> tgid, srcaddr, service options (E bit)
    -3         LDU2 ESS JSON       -> encrypted / algid / keyid
    -1         sync timeout        -> transmission end
"""
import ctypes
import json
import sys
import time

import tk_p25
from helper_funcs import from_dict, get_ordinals
from log_ts import log_ts

ALGID_CLEAR = 0x80
SVCOPT_ENCRYPTED = 0x40
LCW_PROTECTED = 0x80
LCO_GROUP_VOICE = 0x00
DUID_HDU, DUID_TDU, DUID_TDULC, DUID_LCW = 0, 3, 15, 19
M_TIMEOUT, M_JSON_DATA = -1, -3


def _parse_nac(value):
    if value is None or value == "":
        return None
    try:
        nac = int(value, 16) if isinstance(value, str) else int(value)
    except (TypeError, ValueError):
        return None
    return nac if 0 < nac <= 0xFFF else None


class rx_ctl(tk_p25.rx_ctl):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.drb_conv = {}   # msgq_id -> state dict

    def add_receiver(self, msgq_id, config, meta_q=None, freq=0):
        super().add_receiver(msgq_id, config, meta_q=meta_q, freq=freq)
        rcvr = self.receivers.get(msgq_id)
        if rcvr is not None and rcvr.get('rx_rcvr') is not None:
            return   # trunked receiver: base class owns it
        self.drb_conv[msgq_id] = {
            'name': from_dict(config, 'name', str(msgq_id)),
            'freq': freq,
            'nac_filter': _parse_nac(config.get('nac')),
            'nac': None,
            'voice': False,
            'tgid': None,
            'srcaddr': 0,
            'svc_encrypted': False,
            'ess_encrypted': False,
            'algid': None,
            'keyid': None,
            'call_start': None,
            'call_encrypted': False,
            'logged_tgid': None,
            'prev_type': None,
            'enc_calls': 0,
            'clear_calls': 0,
            'last_enc_algid': None,
            'last_enc_keyid': None,
            'last_enc_time': None,
            'last_clear_time': None,
            'last_voice_time': None,
        }

    def post_init(self):
        super().post_init()
        for msgq_id, cs in self.drb_conv.items():
            if cs['nac_filter'] is not None and self.fa_ctrl is not None:
                self.fa_ctrl({'tuner': msgq_id, 'cmd': 'set_nac', 'nac': cs['nac_filter']})
                sys.stderr.write("%s [%d] conventional NAC filter 0x%03x\n" % (log_ts.get(), msgq_id, cs['nac_filter']))

    # ------------------------------------------------------------------

    def _encrypted(self, cs):
        return cs['svc_encrypted'] or cs['ess_encrypted']

    def _voice_start(self, cs, now):
        if not cs['voice']:
            cs['voice'] = True
            cs['call_start'] = now
            cs['call_encrypted'] = False
            cs['logged_tgid'] = None
        cs['last_voice_time'] = now

    def _voice_end(self, cs, now):
        if cs['voice']:
            if cs['call_encrypted']:
                cs['enc_calls'] += 1
                cs['last_enc_time'] = now
            else:
                cs['clear_calls'] += 1
                cs['last_clear_time'] = now
        cs['voice'] = False
        cs['tgid'] = None
        cs['srcaddr'] = 0
        cs['svc_encrypted'] = False
        cs['ess_encrypted'] = False
        cs['algid'] = None
        cs['keyid'] = None
        cs['call_start'] = None
        cs['logged_tgid'] = None

    def _note_encryption(self, cs, now):
        if self._encrypted(cs):
            cs['call_encrypted'] = True
            if cs['algid'] is not None:
                cs['last_enc_algid'] = cs['algid']
                cs['last_enc_keyid'] = cs['keyid']
            cs['last_enc_time'] = now

    def _maybe_log_call(self, msgq_id, cs):
        # One call_log entry per (call, talkgroup), and none for an encrypted
        # call: it will produce no audio, so a "grant" would only mislead the
        # edge node's attribution.
        if cs['tgid'] is None or self._encrypted(cs) or cs['logged_tgid'] == cs['tgid']:
            return
        cs['logged_tgid'] = cs['tgid']
        try:
            self.log_call(0, msgq_id, cs['freq'], 0, 0, cs['tgid'], cs['name'], cs['srcaddr'], '')
        except Exception as e:   # older/newer tk_p25 log_call signature
            sys.stderr.write("%s [%d] conventional call_log failed: %s\n" % (log_ts.get(), msgq_id, e))

    def _process_conv(self, msgq_id, cs, m_type, msg, now):
        if m_type == M_TIMEOUT or m_type in (DUID_TDU, DUID_TDULC):
            self._voice_end(cs, now)
        elif m_type == DUID_HDU:
            s = msg.to_string()
            cs['nac'] = get_ordinals(s[0:2]) if len(s) >= 2 else cs['nac']
            self._voice_start(cs, now)
        elif m_type == DUID_LCW:
            s = msg.to_string()
            if len(s) < 11:
                return
            cs['nac'] = get_ordinals(s[0:2])
            if cs['prev_type'] in (DUID_TDU, DUID_TDULC):
                return   # TDULC's link control follows its TDU: the call is over
            lcw = s[2:]
            self._voice_start(cs, now)
            pb_sf_lco = get_ordinals(lcw[0:1])
            if pb_sf_lco & LCW_PROTECTED:
                # Link control itself is encrypted — the call certainly is.
                cs['svc_encrypted'] = True
            elif (pb_sf_lco & 0x3f) == LCO_GROUP_VOICE:
                svcopts = get_ordinals(lcw[2:3])
                ga = get_ordinals(lcw[4:6])
                sa = get_ordinals(lcw[6:9])
                cs['svc_encrypted'] = bool(svcopts & SVCOPT_ENCRYPTED)
                if ga:
                    cs['tgid'] = ga
                if sa:
                    cs['srcaddr'] = sa
            self._note_encryption(cs, now)
            self._maybe_log_call(msgq_id, cs)
        elif m_type == M_JSON_DATA:
            try:
                js = json.loads(msg.to_string())
            except (ValueError, TypeError):
                return
            if 'encrypted' in js:
                self._voice_start(cs, now)
                algid = from_dict(js, 'algid', None)
                cs['ess_encrypted'] = bool(js.get('encrypted')) and algid != ALGID_CLEAR
                cs['algid'] = algid if cs['ess_encrypted'] else None
                cs['keyid'] = from_dict(js, 'keyid', None) if cs['ess_encrypted'] else None
                self._note_encryption(cs, now)
            grpaddr = from_dict(js, 'grpaddr', 0)
            if grpaddr:
                cs['tgid'] = grpaddr
                cs['srcaddr'] = from_dict(js, 'srcaddr', 0) or cs['srcaddr']
                self._maybe_log_call(msgq_id, cs)

    def process_qmsg(self, msg):
        m_proto = ctypes.c_int16(msg.type() >> 16).value
        m_rxid = int(msg.arg1()) >> 1
        if m_proto != 0 or m_rxid not in self.drb_conv:
            return super().process_qmsg(msg)
        m_type = ctypes.c_int16(msg.type() & 0xffff).value
        cs = self.drb_conv[m_rxid]
        try:
            self._process_conv(m_rxid, cs, m_type, msg, time.time())
        except Exception as e:   # never take multi_rx down over a bad frame
            sys.stderr.write("%s [%d] conventional message type %d failed: %s\n" % (log_ts.get(), m_rxid, m_type, e))
        cs['prev_type'] = m_type

    def get_chan_status(self):
        d = json.loads(super().get_chan_status())
        ids = list(d.get('channels') or [])
        for msgq_id, cs in self.drb_conv.items():
            key = str(msgq_id)
            row = d.get(key) or {}
            row.update({
                'freq': cs['freq'],
                'name': cs['name'],
                'system': cs['name'],
                'tdma': 0,
                'tgid': cs['tgid'],
                'tag': cs['name'],
                'srcaddr': cs['srcaddr'],
                'srctag': '',
                'svcopts': 0,
                'encrypted': 1 if self._encrypted(cs) else 0,
                'emergency': 0,
                'hold_tgid': 0,
                'mode': None,
                'stream': '',
                'msgqid': msgq_id,
                'conventional': True,
                'voice': cs['voice'],
                'nac': cs['nac'],
                'nac_filter': cs['nac_filter'],
                'algid': cs['algid'],
                'keyid': cs['keyid'],
                'enc_calls': cs['enc_calls'],
                'clear_calls': cs['clear_calls'],
                'last_enc_algid': cs['last_enc_algid'],
                'last_enc_keyid': cs['last_enc_keyid'],
                'last_enc_time': cs['last_enc_time'],
                'last_clear_time': cs['last_clear_time'],
                'last_voice_time': cs['last_voice_time'],
            })
            d[key] = row
            if key not in ids:
                ids.append(key)
        d['channels'] = ids
        return json.dumps(d)
