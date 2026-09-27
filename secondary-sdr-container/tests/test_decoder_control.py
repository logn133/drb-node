"""
node-26#11 — which dongle each secondary decoder may use.

Run from secondary-sdr-container/:  PYTHONPATH=app python -m pytest -q tests
"""
from unittest.mock import patch

import pytest

from internal import decoder_control as dc

# radio-box's real pair: op25's dongle and the one with the 1090 antenna.
DEVS = [
    {"index": 0, "serial": "69420", "name": "RTL", "duplicate_serial": False},
    {"index": 1, "serial": "00000001", "name": "RTL", "duplicate_serial": False},
]


def test_unpinned_mode_never_gets_op25s_dongle():
    assert dc._candidates("adsb", {}, ["00000001"], DEVS) == [0]


def test_pinned_mode_gets_exactly_its_dongle():
    assert dc._candidates("ais", {"ais": "69420"}, ["00000001"], DEVS) == [0]


def test_unpinned_mode_skips_a_dongle_pinned_to_another_service():
    assert dc._candidates("ais", {"adsb": "69420"}, ["00000001"], DEVS) == []


def test_missing_or_ambiguous_pin_gets_nothing():
    assert dc._candidates("adsb", {"adsb": "nope"}, [], DEVS) == []
    dup = [dict(d, serial="00000001") for d in DEVS]
    assert dc._candidates("adsb", {"adsb": "00000001"}, [], dup) == []


class FakeDecoders:
    """Stands in for real processes: one decoder per free index."""

    def __init__(self):
        self.live = {}

    def start(self, mode, candidates=None):
        free = [i for i in candidates if i not in self.live.values()]
        if not free:
            return False
        self.live[mode] = free[0]
        return True

    def stop(self, mode):
        self.live.pop(mode, None)


@pytest.fixture
def fake():
    f = FakeDecoders()
    with patch.object(dc, "devices", return_value=DEVS), \
         patch.object(dc, "_start_one", side_effect=f.start), \
         patch.object(dc, "_stop_one", side_effect=f.stop), \
         patch.object(dc, "running", side_effect=lambda: [m for m in dc.MODES if m in f.live]), \
         patch.dict(dc._indices, clear=True):
        dc._indices.update(f.live)
        yield f


def _apply(fake, priority, pins=None):
    dc.apply(priority, pins, ["00000001"])
    dc._indices.clear()
    dc._indices.update(fake.live)
    return fake.live


def test_one_spare_goes_to_the_top_pick(fake):
    assert _apply(fake, ["ais", "adsb"]) == {"ais": 0}


def test_reordering_hands_the_spare_to_the_new_top_pick(fake):
    _apply(fake, ["adsb", "ais"])
    assert _apply(fake, ["ais", "adsb"]) == {"ais": 0}


def test_pinned_lower_priority_still_runs_when_top_pick_has_no_dongle(fake):
    # AIS ranked first but its only candidate is ADS-B's pinned antenna dongle.
    assert _apply(fake, ["ais", "adsb"], {"adsb": "69420"}) == {"adsb": 0}
