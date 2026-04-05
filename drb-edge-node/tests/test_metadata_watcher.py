"""
Unit tests for MetadataWatcher state machine.
All OP25 HTTP calls are mocked — no running services required.
"""
import pytest
from unittest.mock import AsyncMock, patch
from app.internal.metadata_watcher import MetadataWatcher, HANG_THRESHOLD


@pytest.fixture
def watcher():
    w = MetadataWatcher()
    w.on_call_start = AsyncMock()
    w.on_call_end = AsyncMock()
    return w


# ---------------------------------------------------------------------------
# Call start
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_call_starts_when_tgid_appears(watcher):
    status = [{"tgid": 1234, "tag": "Police Dispatch"}]
    with patch("app.internal.metadata_watcher.op25_client.get_terminal_status", new=AsyncMock(return_value=status)):
        await watcher._tick()

    assert watcher.is_active
    assert watcher.current_tgid == 1234
    watcher.on_call_start.assert_called_once()
    payload = watcher.on_call_start.call_args[0][0]
    assert payload["tgid"] == 1234
    assert payload["tgid_name"] == "Police Dispatch"
    assert "call_id" in payload
    assert "started_at" in payload


@pytest.mark.asyncio
async def test_tgid_zero_does_not_start_call(watcher):
    status = [{"tgid": 0}]
    with patch("app.internal.metadata_watcher.op25_client.get_terminal_status", new=AsyncMock(return_value=status)):
        await watcher._tick()

    assert not watcher.is_active
    watcher.on_call_start.assert_not_called()


@pytest.mark.asyncio
async def test_tgid_none_string_does_not_start_call(watcher):
    status = [{"tgid": "None"}]
    with patch("app.internal.metadata_watcher.op25_client.get_terminal_status", new=AsyncMock(return_value=status)):
        await watcher._tick()

    assert not watcher.is_active


@pytest.mark.asyncio
async def test_op25_offline_does_not_start_call(watcher):
    with patch("app.internal.metadata_watcher.op25_client.get_terminal_status", new=AsyncMock(return_value=None)):
        await watcher._tick()

    assert not watcher.is_active
    watcher.on_call_start.assert_not_called()


# ---------------------------------------------------------------------------
# Hang / call end
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_hang_below_threshold_keeps_call_alive(watcher):
    with patch("app.internal.metadata_watcher.op25_client.get_terminal_status", new=AsyncMock(return_value=[{"tgid": 1234}])):
        await watcher._tick()

    assert watcher.is_active

    with patch("app.internal.metadata_watcher.op25_client.get_terminal_status", new=AsyncMock(return_value=[{"tgid": 0}])):
        for _ in range(HANG_THRESHOLD - 1):
            await watcher._tick()

    assert watcher.is_active
    watcher.on_call_end.assert_not_called()


@pytest.mark.asyncio
async def test_hang_at_threshold_ends_call(watcher):
    with patch("app.internal.metadata_watcher.op25_client.get_terminal_status", new=AsyncMock(return_value=[{"tgid": 1234}])):
        await watcher._tick()

    with patch("app.internal.metadata_watcher.op25_client.get_terminal_status", new=AsyncMock(return_value=[{"tgid": 0}])):
        for _ in range(HANG_THRESHOLD):
            await watcher._tick()

    assert not watcher.is_active
    watcher.on_call_end.assert_called_once()
    payload = watcher.on_call_end.call_args[0][0]
    assert "call_id" in payload
    assert "ended_at" in payload


@pytest.mark.asyncio
async def test_op25_offline_triggers_hang_and_ends_call(watcher):
    with patch("app.internal.metadata_watcher.op25_client.get_terminal_status", new=AsyncMock(return_value=[{"tgid": 1234}])):
        await watcher._tick()

    with patch("app.internal.metadata_watcher.op25_client.get_terminal_status", new=AsyncMock(return_value=None)):
        for _ in range(HANG_THRESHOLD):
            await watcher._tick()

    assert not watcher.is_active
    watcher.on_call_end.assert_called_once()


@pytest.mark.asyncio
async def test_hang_counter_resets_when_tgid_returns(watcher):
    with patch("app.internal.metadata_watcher.op25_client.get_terminal_status", new=AsyncMock(return_value=[{"tgid": 1234}])):
        await watcher._tick()

    # Partial hang — not enough to end
    with patch("app.internal.metadata_watcher.op25_client.get_terminal_status", new=AsyncMock(return_value=[{"tgid": 0}])):
        for _ in range(HANG_THRESHOLD - 1):
            await watcher._tick()

    assert watcher.is_active

    # tgid returns — counter resets
    with patch("app.internal.metadata_watcher.op25_client.get_terminal_status", new=AsyncMock(return_value=[{"tgid": 1234}])):
        await watcher._tick()

    assert watcher._hang_counter == 0
    assert watcher.is_active


# ---------------------------------------------------------------------------
# Talkgroup changes
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_talkgroup_change_closes_old_and_opens_new(watcher):
    with patch("app.internal.metadata_watcher.op25_client.get_terminal_status", new=AsyncMock(return_value=[{"tgid": 1111}])):
        await watcher._tick()

    first_call_id = watcher.active_call_id

    with patch("app.internal.metadata_watcher.op25_client.get_terminal_status", new=AsyncMock(return_value=[{"tgid": 2222}])):
        await watcher._tick()

    assert watcher.is_active
    assert watcher.current_tgid == 2222
    assert watcher.active_call_id != first_call_id
    watcher.on_call_end.assert_called_once()
    assert watcher.on_call_start.call_count == 2


# ---------------------------------------------------------------------------
# Status format variations
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_single_dict_status_instead_of_list(watcher):
    """OP25 terminal may return a bare dict instead of a list."""
    status = {"tgid": 9999, "tag": "Fire"}
    with patch("app.internal.metadata_watcher.op25_client.get_terminal_status", new=AsyncMock(return_value=status)):
        await watcher._tick()

    assert watcher.current_tgid == 9999


@pytest.mark.asyncio
async def test_tg_id_key_alias(watcher):
    """Some OP25 builds use 'tg_id' instead of 'tgid'."""
    status = [{"tg_id": 5555, "tag": "EMS"}]
    with patch("app.internal.metadata_watcher.op25_client.get_terminal_status", new=AsyncMock(return_value=status)):
        await watcher._tick()

    assert watcher.current_tgid == 5555


@pytest.mark.asyncio
async def test_multichannel_uses_first_active(watcher):
    """When multiple channels are returned, first active tgid wins."""
    status = [
        {"tgid": 0},
        {"tgid": 7777, "tag": "Roads"},
        {"tgid": 8888, "tag": "Other"},
    ]
    with patch("app.internal.metadata_watcher.op25_client.get_terminal_status", new=AsyncMock(return_value=status)):
        await watcher._tick()

    assert watcher.current_tgid == 7777
