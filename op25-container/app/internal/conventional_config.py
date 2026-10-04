"""
P25 conventional OP25 config (node-26#24).

A conventional P25 channel is a fixed voice frequency with no control channel:
radios key up on it directly, C4FM (fsk4), and identify themselves in the link
control of each LDU1 (talkgroup + unit id) and the ESS of each LDU2
(encryption algorithm + key id). Building a `tk_p25.py` trunking config for one
— what "P25" always meant before — makes OP25 hunt forever for a control
channel that does not exist, so nothing is ever decoded.

Layout this produces, per boatbod OP25 gr310 `p25_conventional_example.json`:

  * one non-tunable device parked so every channel sits inside its passband;
  * one channel per frequency, `trunking_sysname: ""` (so the trunking module
    treats it as conventional), `demod_type: fsk4`;
  * trunking module `tk_drb_conv.py` with no chans — our thin subclass of
    tk_p25.rx_ctl that reports encryption and the NAC for conventional
    channels and applies the per-channel NAC filter (tk_p25 itself does
    neither for FDMA conventional);
  * channel N's audio to UDP 23456+N; with two channels audio.py runs in
    two-channel mode (left = first channel, right = second).

Pure: no file I/O, so it is unit-testable outside the container.
"""
from typing import Any, Dict, List, Tuple

from models import ChannelConfig, ConventionalChannel, DeviceConfig, TerminalConfig

# Two is what one audio.py instance can carry (its A and B sockets). A third
# channel would need a second audio pipeline into PulseAudio.
MAX_CONVENTIONAL_CHANNELS = 2

AUDIO_UDP_BASE_PORT = 23456

DEVICE_RATE = 1_920_000
DEVICE_USABLE_BW_PCT = 0.85
# Half the occupied bandwidth of a 12.5 kHz P25 channel.
CHANNEL_HALF_BW_HZ = 6_250
# Keep the RTL-SDR's DC spike off the channels. A single channel (or two very
# close together) is offset from the centre; channels further apart than
# 2x this already straddle the spike, so the centre goes between them.
DC_SPIKE_OFFSET_HZ = 250_000
DC_SPIKE_CLEARANCE_HZ = 50_000

CONVENTIONAL_TRUNKING_MODULE = "tk_drb_conv.py"


def device_center(freqs: List[int]) -> int:
    lo, hi = min(freqs), max(freqs)
    mid = (lo + hi) // 2
    if (hi - lo) // 2 >= DC_SPIKE_CLEARANCE_HZ:
        return mid
    return mid + DC_SPIKE_OFFSET_HZ


def validate_channels(channels: List[ConventionalChannel]) -> None:
    """Raise ValueError with an operator-readable reason."""
    if not channels:
        raise ValueError("P25 conventional needs at least one channel frequency.")
    if len(channels) > MAX_CONVENTIONAL_CHANNELS:
        raise ValueError(
            f"P25 conventional supports up to {MAX_CONVENTIONAL_CHANNELS} channels on one SDR "
            f"(got {len(channels)})."
        )
    freqs = [c.frequency for c in channels]
    if len(set(freqs)) != len(freqs):
        raise ValueError("The same conventional frequency is listed twice.")
    for c in channels:
        if c.nac is not None and not (0 < c.nac <= 0xFFF):
            raise ValueError(f"NAC {c.nac:#x} on {c.frequency / 1e6:.5f} MHz is outside 0x001-0xFFF.")
    center = device_center(freqs)
    half = DEVICE_RATE * DEVICE_USABLE_BW_PCT / 2
    for f in freqs:
        if abs(f - center) + CHANNEL_HALF_BW_HZ > half:
            span = (max(freqs) - min(freqs)) / 1e6
            raise ValueError(
                f"These channels are {span:.3f} MHz apart — too far for one SDR "
                f"(max ~{(2 * (half - CHANNEL_HALF_BW_HZ)) / 1e6:.2f} MHz)."
            )


def build_conventional_config(
    system_name: str,
    channels: List[ConventionalChannel],
    gains: str,
    ppm: float,
) -> Tuple[Dict[str, Any], bool]:
    """Return (OP25 config dict, two_channel_audio)."""
    validate_channels(channels)
    freqs = [c.frequency for c in channels]
    center = device_center(freqs)

    chans = []
    for i, c in enumerate(channels):
        chans.append(ChannelConfig(
            name=c.name or f"{system_name} {c.frequency / 1e6:.4f}",
            trunking_sysname="",
            enable_analog="off",
            demod_type="fsk4",
            filter_type="rc",
            frequency=c.frequency,
            meta_stream_name="",
            destination=f"udp://127.0.0.1:{AUDIO_UDP_BASE_PORT + i}",
            nac=c.nac,
            crypt_behavior=2,
        ))

    device = DeviceConfig(
        gains=gains, ppm=ppm, tunable=False, frequency=center,
        rate=DEVICE_RATE, usable_bw_pct=DEVICE_USABLE_BW_PCT,
    )
    config = {
        "channels": [ch.model_dump() for ch in chans],
        "devices": [device.model_dump()],
        "trunking": {"module": CONVENTIONAL_TRUNKING_MODULE, "chans": []},
        "terminal": TerminalConfig().model_dump(),
    }
    return config, len(chans) > 1
