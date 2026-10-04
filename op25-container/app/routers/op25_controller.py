from fastapi import HTTPException, APIRouter
import subprocess
import os
import re
import signal
import sys
import json
import threading
from collections import deque
from typing import Optional
from models import ConfigGenerator, DecodeMode, P25Mode, ChannelConfig, DeviceConfig, TrunkingConfig, TrunkingChannelConfig, TerminalConfig, MetadataConfig, MetadataStreamConfig, HARDWARE_PRESETS
from internal.conventional_config import build_conventional_config, CONVENTIONAL_TRUNKING_MODULE
from internal.logger import create_logger
from internal.op25_config_utls import save_talkgroup_tags, save_whitelist, del_none_in_dict, get_current_system_from_config
from internal.liquidsoap_config_utils import generate_liquid_script

LOGGER = create_logger(__name__)

OP25_PATH = "/op25/op25/gr-op25_repeater/apps/"
OP25_SCRIPT = "run_multi-rx_service.sh"
_PGID_FILE = "/tmp/op25.pgid"
_ACTIVE_CFG = "/configs/active.cfg.json"

# Analog: park the dongle's centre this far from the channel so the channel
# does not sit on the RTL-SDR's DC spike. Well inside the 1.92 MS/s window.
ANALOG_CENTER_OFFSET_HZ = 250_000

# Last lines multi_rx/liquidsoap printed — the only place the reason OP25
# died (SDR busy, bad config, no lock) is visible without SSH + docker logs.
_log: deque = deque(maxlen=300)
_proc: Optional[subprocess.Popen] = None


def _save_pgid(pgid: int) -> None:
    with open(_PGID_FILE, "w") as f:
        f.write(str(pgid))


def _read_pgid():
    try:
        return int(open(_PGID_FILE).read().strip())
    except Exception:
        return None


def _multi_rx_alive(pgid: int) -> bool:
    """True only if a live multi_rx.py process is in OP25's process group.

    node-26#12: killpg(pgid, 0) succeeds while the wrapper shell or
    liquidsoap outlives a dead multi_rx (and even for a zombie leader), so
    the old check reported a dead scanner as "running".
    """
    try:
        pids = [d for d in os.listdir("/proc") if d.isdigit()]
    except OSError:
        return False
    for pid in pids:
        try:
            with open(f"/proc/{pid}/stat") as f:
                fields = f.read().rsplit(")", 1)[1].split()
            state, pgrp = fields[0], int(fields[2])
            if pgrp != pgid or state == "Z":
                continue
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                if b"multi_rx.py" in f.read():
                    return True
        except (OSError, IndexError, ValueError):
            continue
    return False


def _reap() -> None:
    if _proc is not None:
        _proc.poll()


def _is_running() -> bool:
    pgid = _read_pgid()
    if pgid is None:
        return False
    _reap()
    return _multi_rx_alive(pgid)


def _kill_group(pgid: int) -> None:
    try:
        os.killpg(pgid, signal.SIGTERM)
    except OSError:
        pass


def _pump(proc: subprocess.Popen) -> None:
    """Mirror OP25's output to the container log and keep the tail for /log."""
    for line in proc.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()
        _log.append(line.rstrip("\n"))
    rc = proc.wait()
    _log.append(f"[OP25 exited with code {rc}]")


def create_op25_router():
    router = APIRouter()

    @router.post("/start")
    async def start_op25():
        global _proc
        if _is_running():
            return {"status": "OP25 already running"}
        if not os.path.exists(_ACTIVE_CFG):
            raise HTTPException(
                status_code=409,
                detail="No OP25 config yet — choose a system or frequency first.",
            )
        stale = _read_pgid()
        if stale is not None:
            # multi_rx died but liquidsoap/the wrapper may still hold the
            # Icecast mount — clear the old group before starting a new one.
            _kill_group(stale)
        try:
            _log.clear()
            proc = subprocess.Popen(
                os.path.join(OP25_PATH, OP25_SCRIPT), shell=True, preexec_fn=os.setsid, cwd=OP25_PATH,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
            )
            _proc = proc
            _save_pgid(proc.pid)
            threading.Thread(target=_pump, args=(proc,), daemon=True).start()
            LOGGER.debug(proc)
            return {"status": "OP25 started"}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @router.post("/stop")
    async def stop_op25():
        pgid = _read_pgid()
        if pgid is None:
            return {"status": "OP25 is not running"}
        try:
            _kill_group(pgid)
            os.remove(_PGID_FILE)
            return {"status": "OP25 stopped"}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @router.get("/status")
    async def get_status():
        if _is_running():
            return {"status": "running"}
        # Started (pgid file present, never stopped via the API) but multi_rx
        # is gone: it crashed. The tail of /log says why.
        if _read_pgid() is not None:
            return {"status": "crashed", "configured": os.path.exists(_ACTIVE_CFG)}
        return {"status": "stopped", "configured": os.path.exists(_ACTIVE_CFG)}

    @router.get("/log")
    async def get_log(lines: int = 80):
        lines = max(1, min(lines, _log.maxlen))
        return {"lines": list(_log)[-lines:]}

    @router.get("/devices")
    async def list_sdr_devices():
        """Enumerate connected SDR-looking USB devices via lsusb.

        Same match heuristic as install.sh's one-shot host check — good enough
        to answer "is a second SDR plugged in", not a serial-level device
        binding (op25's DeviceConfig.args has no serial concept yet either).
        """
        devices = []
        try:
            out = subprocess.run(["lsusb"], capture_output=True, text=True, timeout=5).stdout
            for line in out.splitlines():
                if re.search(r"rtl2838|realtek.*283[28]|sdr", line, re.IGNORECASE):
                    devices.append(line.strip())
        except Exception as e:
            LOGGER.warning(f"SDR device enumeration failed: {e}")
        return {"count": len(devices), "devices": devices}

    @router.post("/generate-config")
    async def generate_config(generator: ConfigGenerator):
        try:
            if generator.type == DecodeMode.P25 and generator.p25_mode == P25Mode.CONVENTIONAL:
                # node-26#24: fixed voice channels, no control channel to hunt.
                preset = HARDWARE_PRESETS.get(generator.hardware_preset, HARDWARE_PRESETS["other"])
                ppm = generator.ppm_override if generator.ppm_override is not None else preset["ppm"]
                try:
                    config_dict, two_channel = build_conventional_config(
                        generator.systemName, generator.conv_channels, preset["gains"], ppm,
                    )
                except ValueError as e:
                    raise HTTPException(status_code=400, detail=str(e))
                if generator.icecastConfig:
                    generate_liquid_script(generator.icecastConfig, two_channel=two_channel)

            elif generator.type == DecodeMode.P25:
                preset = HARDWARE_PRESETS.get(generator.hardware_preset, HARDWARE_PRESETS["other"])
                ppm = generator.ppm_override if generator.ppm_override is not None else preset["ppm"]
                channels = [ChannelConfig(
                    name=generator.systemName,
                    trunking_sysname=generator.systemName,
                    enable_analog="off",
                    demod_type="cqpsk",
                    cqpsk_tracking=preset["cqpsk_tracking"],
                    filter_type="rc",
                    meta_stream_name="stream_0"
                )]
                devices = [DeviceConfig(gains=preset["gains"], ppm=ppm)]
                save_talkgroup_tags(generator.tags or [])
                save_whitelist(generator.whitelist)
                has_talkgroups = bool(generator.whitelist)
                trunking = TrunkingConfig(
                    module="tk_p25.py",
                    chans=[TrunkingChannelConfig(
                        sysname=generator.systemName,
                        control_channel_list=','.join(str(ch) for ch in generator.channels),
                        tagsFile="/configs/active.cfg.tags.tsv" if has_talkgroups else None,
                        whitelist="/configs/active.cfg.whitelist.tsv" if has_talkgroups else None
                    )]
                )

                metadata = MetadataConfig(
                    streams=[
                        MetadataStreamConfig(
                            stream_name="stream_0",
                            icecastServerAddress = f"{generator.icecastConfig.icecast_host}:{generator.icecastConfig.icecast_port}",
                            icecastMountpoint = generator.icecastConfig.icecast_mountpoint,
                            icecastPass = generator.icecastConfig.icecast_password
                        )
                    ]
                )

                # Generate the op25.liq file
                generate_liquid_script(generator.icecastConfig)


                terminal = TerminalConfig()

                config_dict = {
                    "channels": [channel.dict() for channel in channels],
                    "devices": [device.dict() for device in devices],
                    "trunking": trunking.dict(),
                    "metadata": metadata.dict(),
                    "terminal": terminal.dict()
                }


            elif generator.type == DecodeMode.ANALOG:
                if not generator.channels:
                    raise HTTPException(status_code=400, detail="Analog needs a frequency.")
                preset = HARDWARE_PRESETS.get(generator.hardware_preset, HARDWARE_PRESETS["other"])
                ppm = generator.ppm_override if generator.ppm_override is not None else preset["ppm"]
                freq = int(float(generator.channels[0]))
                # One fixed conventional channel: a trunking module with no
                # chans, so the channel is reported as `conventional` on the
                # HTTP terminal.
                channels = [ChannelConfig(
                    name=generator.systemName,
                    trunking_sysname="",
                    enable_analog="on",
                    demod_type="fsk4",
                    filter_type="rc",
                    frequency=freq,
                    meta_stream_name="",
                    nbfm_squelch_threshold=generator.nbfm_squelch,
                )]
                devices = [DeviceConfig(
                    gains=preset["gains"], ppm=ppm, tunable=False,
                    frequency=freq + ANALOG_CENTER_OFFSET_HZ,
                )]
                if generator.icecastConfig:
                    generate_liquid_script(generator.icecastConfig)

                config_dict = {
                    "channels": [channel.dict() for channel in channels],
                    "devices": [device.dict() for device in devices],
                    # Same module as P25 conventional (node-26#24): it reports
                    # the channel as `conventional` whatever tk_p25 version the
                    # image was built with, which is what the edge node's
                    # single-channel attribution keys on (node-26#20).
                    "trunking": {"module": CONVENTIONAL_TRUNKING_MODULE, "chans": []},
                    "terminal": TerminalConfig().dict(),
                }

            else:
                raise HTTPException(status_code=400, detail="Unsupported system type. Must be 'P25' or 'NBFM' (DMR is not implemented).")

            with open(_ACTIVE_CFG, 'w') as f:
                json.dump(del_none_in_dict(config_dict), f, indent=2)

            return {"message": f"Config exported to '{_ACTIVE_CFG}'"}
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    return router
