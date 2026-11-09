"""
Upload spool (node-26#44): a failed upload must never delete the recording.

Before the spool, upload_recording unlinked the file in a `finally`, so any C2
outage, 5xx or timeout lost that call's audio for good.
"""
import asyncio
import json
from pathlib import Path

import pytest

from app.config import settings
from app.internal import upload_spool
from app.internal.call_recorder import CallRecorder
from app.internal.upload_spool import OK, REJECT, RETRY, UploadMeta, UploadResult, UploadSpool, classify


class Clock:
    def __init__(self, t: float = 1_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


class FakeUploader:
    def __init__(self, *results: UploadResult):
        self.results = list(results)
        self.calls = []

    async def __call__(self, path: Path, meta: UploadMeta) -> UploadResult:
        assert path.exists(), "the uploader must be handed a file that exists"
        self.calls.append((path.name, meta))
        return self.results.pop(0) if self.results else UploadResult(OK, url="gs://x")


def _recording(tmp_path: Path, name: str = "20261027_call-1.flac", size: int = 100) -> Path:
    p = tmp_path / "rec" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"f" * size)
    return p


def _spool(tmp_path: Path, uploader, clock=None, **kw) -> UploadSpool:
    args = dict(max_bytes=10_000, max_age_seconds=3600, backoff_min=10, backoff_max=80)
    args.update(kw)
    return UploadSpool(tmp_path / "spool", uploader, clock=clock or Clock(), **args)


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status,url,kind", [
    (200, "gs://b/o", OK),
    (200, None, RETRY),      # C2's GCS write failed: nothing stored
    (401, None, RETRY),      # a reissued key is on its way
    (408, None, RETRY),
    (429, None, RETRY),
    (500, None, RETRY),
    (503, None, RETRY),
    (400, None, REJECT),
    (403, None, RETRY),      # not approved yet: approval may still come
    (413, None, REJECT),
])
def test_classify(status, url, kind):
    assert classify(status, url) == kind


# ---------------------------------------------------------------------------
# upload_recording: delete only on success
# ---------------------------------------------------------------------------

@pytest.fixture
def recorder(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "c2_url", "https://c2.example")
    rec = CallRecorder()
    rec.spool = _spool(tmp_path, rec._post)
    return rec


def _patch_post(monkeypatch, result: UploadResult):
    async def fake(path, meta, mime, timeout=None):
        return result
    monkeypatch.setattr(upload_spool, "post_recording", fake)


async def test_failed_upload_keeps_the_file_in_the_spool(tmp_path, recorder, monkeypatch):
    _patch_post(monkeypatch, UploadResult(RETRY, detail="ConnectError: refused"))
    path = _recording(tmp_path)
    url = await recorder.upload_recording(path, "call-1", talkgroup_id=101, talkgroup_name="Dispatch",
                                          system_id="sys-1", audio_start_epoch=1.5, audio_end_epoch=9.25)
    assert url is None
    assert not path.exists()
    spooled = tmp_path / "spool" / path.name
    assert spooled.read_bytes() == b"f" * 100
    meta = json.loads((tmp_path / "spool" / (path.name + ".json")).read_text())
    assert meta["call_id"] == "call-1" and meta["talkgroup_id"] == 101 and meta["system_id"] == "sys-1"
    assert meta["audio_start_epoch"] == 1.5 and meta["audio_end_epoch"] == 9.25
    assert meta["last_error"] == "ConnectError: refused"
    assert recorder.spool.stats()["files"] == 1


async def test_successful_upload_deletes_the_file(tmp_path, recorder, monkeypatch):
    _patch_post(monkeypatch, UploadResult(OK, url="gs://b/call-1.flac", status=200))
    path = _recording(tmp_path)
    assert await recorder.upload_recording(path, "call-1") == "gs://b/call-1.flac"
    assert not path.exists()
    assert recorder.spool.stats()["files"] == 0


async def test_rejected_upload_is_not_spooled(tmp_path, recorder, monkeypatch):
    _patch_post(monkeypatch, UploadResult(REJECT, status=413, detail="HTTP 413: File too large"))
    path = _recording(tmp_path)
    assert await recorder.upload_recording(path, "call-1") is None
    assert not path.exists()
    assert recorder.spool.stats()["files"] == 0
    assert recorder.spool.last_result["kind"] == REJECT


async def test_real_post_network_error_is_retryable(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "c2_url", "http://127.0.0.1:9")  # discard port: refused
    path = _recording(tmp_path)
    result = await upload_spool.post_recording(path, UploadMeta(call_id="c"), "audio/flac")
    assert result.kind == RETRY and result.status is None
    assert path.exists(), "post_recording must never touch the file"


# ---------------------------------------------------------------------------
# Retry, startup drain, caps
# ---------------------------------------------------------------------------

async def test_retry_backs_off_then_uploads(tmp_path):
    clock = Clock()
    up = FakeUploader(UploadResult(RETRY, status=503, detail="HTTP 503"), UploadResult(OK, url="gs://x"))
    sp = _spool(tmp_path, up, clock=clock)
    sp.add(_recording(tmp_path), UploadMeta(call_id="c1"), UploadResult(RETRY, detail="first"))

    assert await sp.drain_once() == 0 and up.calls == []      # not due yet
    clock.t += 10
    assert await sp.drain_once() == 0 and len(up.calls) == 1  # retry 2 fails
    data = json.loads(next((tmp_path / "spool").glob("*.json")).read_text())
    assert data["attempts"] == 2 and data["next_attempt_at"] == clock.t + 20
    clock.t += 20
    assert await sp.drain_once() == 1
    assert list((tmp_path / "spool").iterdir()) == []


async def test_startup_drain_uploads_what_survived_a_restart(tmp_path):
    clock = Clock()
    first = _spool(tmp_path, FakeUploader(), clock=clock)
    first.add(_recording(tmp_path, "a_c1.flac"), UploadMeta(call_id="c1", talkgroup_id=7), UploadResult(RETRY))
    first.add(_recording(tmp_path, "b_c2.flac"), UploadMeta(call_id="c2"), UploadResult(RETRY))

    # A new process: same directory, nothing due by the sidecar timers.
    up = FakeUploader()
    restarted = _spool(tmp_path, up, clock=clock, poll_seconds=0.01)
    restarted.start()
    for _ in range(100):
        if len(up.calls) == 2:
            break
        await asyncio.sleep(0.01)
    await restarted.stop()
    assert [m.call_id for _, m in up.calls] == ["c1", "c2"]
    assert up.calls[0][1].talkgroup_id == 7
    assert list((tmp_path / "spool").iterdir()) == []


async def test_rejected_on_retry_is_deleted(tmp_path):
    clock = Clock()
    sp = _spool(tmp_path, FakeUploader(UploadResult(REJECT, status=413)), clock=clock)
    sp.add(_recording(tmp_path), UploadMeta(call_id="c1"), UploadResult(RETRY))
    await sp.drain_once(force=True)
    assert sp.stats()["files"] == 0


async def test_age_cap_drops_expired_entries(tmp_path):
    clock = Clock()
    sp = _spool(tmp_path, FakeUploader(), clock=clock, max_age_seconds=3600)
    sp.add(_recording(tmp_path, "old_c1.flac"), UploadMeta(call_id="c1"), UploadResult(RETRY))
    clock.t += 1800
    sp.add(_recording(tmp_path, "new_c2.flac"), UploadMeta(call_id="c2"), UploadResult(RETRY))
    clock.t += 1801
    assert sp.enforce_caps() == 1
    assert [p.name for p in (tmp_path / "spool").glob("*.flac")] == ["new_c2.flac"]


async def test_size_cap_evicts_oldest_first(tmp_path):
    clock = Clock()
    sp = _spool(tmp_path, FakeUploader(), clock=clock, max_bytes=250)
    for i in range(3):
        sp.add(_recording(tmp_path, f"{i}_c{i}.flac", size=100), UploadMeta(call_id=f"c{i}"), UploadResult(RETRY))
        clock.t += 1
    assert sp.enforce_caps() == 1
    assert sorted(p.name for p in (tmp_path / "spool").glob("*.flac")) == ["1_c1.flac", "2_c2.flac"]
    assert sp.stats()["bytes"] == 200


async def test_half_written_pairs_are_cleaned(tmp_path):
    sp = _spool(tmp_path, FakeUploader())
    d = tmp_path / "spool"
    d.mkdir()
    (d / "lost.flac.json").write_text(json.dumps({"call_id": "x", "spooled_at": 0}))  # no audio
    (d / "orphan.flac").write_bytes(b"a")                       # no sidecar, no call_id in name
    assert sp.stats()["files"] == 0
    assert list(d.iterdir()) == []


async def test_lost_or_corrupt_sidecar_is_rebuilt_from_the_filename(tmp_path):
    """A power cut can zero a sidecar; the audio must survive it."""
    up = FakeUploader()
    sp = _spool(tmp_path, up)
    d = tmp_path / "spool"
    d.mkdir()
    (d / "20261027_120000_call-a.flac").write_bytes(b"a")
    (d / "20261027_120001_call_b.flac").write_bytes(b"b")
    (d / "20261027_120001_call_b.flac.json").write_text("")
    assert sp.stats()["files"] == 2
    await sp.drain_once()
    assert sorted(m.call_id for _, m in up.calls) == ["call-a", "call_b"]


async def test_wake_makes_everything_due_now(tmp_path):
    clock = Clock()
    up = FakeUploader()
    sp = _spool(tmp_path, up, clock=clock)
    sp.add(_recording(tmp_path), UploadMeta(call_id="c1"), UploadResult(RETRY, status=401))
    sp.wake()
    assert await sp.drain_once() == 1


def test_form_carries_the_radio_list_and_omits_it_when_empty():
    """C2 starts correlation from /upload, so the radio list rides on it (server-26#304)."""
    radios = json.dumps([{"radio": 555, "start": 0.0, "end": 1.0}])
    assert upload_spool._form(UploadMeta(call_id="c1", radios=radios))["radios"] == radios
    assert "radios" not in upload_spool._form(UploadMeta(call_id="c1"))
