"""
Unit tests for PulseAudio readiness helpers (app.internal.pulse).

The whole point of this module is that readiness means "a PulseAudio
connection actually succeeds," never "a socket file exists at this path" —
that was the exact bug reproduced on live hardware: a killed daemon left its
pid file and native socket behind in the shared `pulse_socket` volume, the
old file-existence check reported "ready", and FFmpeg launched against a
dead daemon.

No real PulseAudio daemon or `pactl` binary is required for these tests:
`_daemon_responds` (the one function that actually shells out) is monkeypatched
everywhere except the dedicated subprocess-layer tests, which fake out
`shutil.which`/`subprocess.run` directly so the "stale file, dead daemon" case
is proven at the layer that matters.
"""
import subprocess
from unittest.mock import Mock

from app.internal import pulse


# ---------------------------------------------------------------------------
# socket_path()
# ---------------------------------------------------------------------------

def test_socket_path_defaults_when_pulse_server_unset(monkeypatch):
    monkeypatch.delenv("PULSE_SERVER", raising=False)
    assert pulse.socket_path() == pulse.DEFAULT_SOCKET_PATH


def test_socket_path_parses_unix_prefixed_pulse_server(monkeypatch):
    monkeypatch.setenv("PULSE_SERVER", "unix:/tmp/somewhere/native")
    assert pulse.socket_path() == "/tmp/somewhere/native"


def test_socket_path_falls_back_on_malformed_pulse_server(monkeypatch):
    monkeypatch.setenv("PULSE_SERVER", "not-a-unix-uri")
    assert pulse.socket_path() == pulse.DEFAULT_SOCKET_PATH


# ---------------------------------------------------------------------------
# is_ready() / wait_until_ready() against a monkeypatched probe
# ---------------------------------------------------------------------------

def test_is_ready_true_when_daemon_responds(monkeypatch):
    monkeypatch.setattr(pulse, "_daemon_responds", lambda: True)
    assert pulse.is_ready() is True


def test_is_ready_false_when_daemon_does_not_respond(monkeypatch):
    monkeypatch.setattr(pulse, "_daemon_responds", lambda: False)
    assert pulse.is_ready() is False


async def test_wait_until_ready_short_circuits_when_already_live(monkeypatch):
    calls = Mock(return_value=True)
    monkeypatch.setattr(pulse, "_daemon_responds", calls)
    assert await pulse.wait_until_ready(timeout=5) is True
    assert calls.call_count == 1


async def test_wait_until_ready_polls_until_daemon_comes_up(monkeypatch):
    monkeypatch.setattr(pulse, "POLL_INTERVAL", 0.01)
    responses = iter([False, False, True])
    monkeypatch.setattr(pulse, "_daemon_responds", lambda: next(responses))
    assert await pulse.wait_until_ready(timeout=5) is True


async def test_wait_until_ready_times_out_when_daemon_never_responds(monkeypatch):
    monkeypatch.setattr(pulse, "POLL_INTERVAL", 0.01)
    monkeypatch.setattr(pulse, "_daemon_responds", lambda: False)
    assert await pulse.wait_until_ready(timeout=0.05) is False


async def test_wait_until_ready_uses_settings_default_when_timeout_omitted(monkeypatch):
    monkeypatch.setattr(pulse.settings, "pulse_wait_timeout", 0.05)
    monkeypatch.setattr(pulse, "POLL_INTERVAL", 0.01)
    monkeypatch.setattr(pulse, "_daemon_responds", lambda: False)
    assert await pulse.wait_until_ready() is False


# ---------------------------------------------------------------------------
# _daemon_responds() at the subprocess layer — proves a stale FILE is not
# enough, which is the actual regression this module fixes.
# ---------------------------------------------------------------------------

def test_daemon_responds_false_when_pactl_missing(monkeypatch):
    monkeypatch.setattr(pulse.shutil, "which", lambda name: None)
    assert pulse._daemon_responds() is False


def test_daemon_responds_false_on_probe_timeout(monkeypatch, tmp_path):
    monkeypatch.setattr(pulse.shutil, "which", lambda name: "/usr/bin/pactl")

    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="pactl", timeout=pulse.PROBE_TIMEOUT_SECONDS)

    monkeypatch.setattr(pulse.subprocess, "run", fake_run)
    assert pulse._daemon_responds() is False


def test_daemon_responds_false_when_stale_socket_file_exists_but_daemon_dead(monkeypatch, tmp_path):
    """
    The regression, reproduced at the layer that matters: a plain FILE sits
    at the socket path (exactly what a killed daemon leaves behind), but
    `pactl info` against it fails (nonzero exit — connection refused). This
    must NOT be treated as ready.
    """
    stale_socket = tmp_path / "native"
    stale_socket.write_bytes(b"")  # a stale file, not a live socket
    monkeypatch.setenv("PULSE_SERVER", f"unix:{stale_socket}")

    monkeypatch.setattr(pulse.shutil, "which", lambda name: "/usr/bin/pactl")
    monkeypatch.setattr(
        pulse.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(args=a, returncode=1),
    )

    assert stale_socket.exists()  # sanity: the old file-existence check would pass
    assert pulse._daemon_responds() is False


def test_daemon_responds_true_when_pactl_succeeds(monkeypatch, tmp_path):
    live_socket = tmp_path / "native"
    live_socket.write_bytes(b"")
    monkeypatch.setenv("PULSE_SERVER", f"unix:{live_socket}")

    monkeypatch.setattr(pulse.shutil, "which", lambda name: "/usr/bin/pactl")
    monkeypatch.setattr(
        pulse.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(args=a, returncode=0),
    )

    assert pulse._daemon_responds() is True
