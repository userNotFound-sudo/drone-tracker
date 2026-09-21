import unittest
from app.zone_engine import ZoneEventEngine

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_RECT_ZONE = {"id": 1, "shape": "rectangle", "coords": [[0.2, 0.2], [0.8, 0.8]]}
_POLY_ZONE = {
    "id": 2,
    "shape": "polygon",
    "coords": [[0.0, 0.0], [0.5, 0.0], [0.5, 0.5], [0.0, 0.5]],  # bottom-left quadrant
}
_ZONES = [_RECT_ZONE, _POLY_ZONE]

_INSIDE_RECT  = {"track_id": 1, "cx": 0.5, "cy": 0.5}   # centre of rect zone
_OUTSIDE_RECT = {"track_id": 1, "cx": 0.1, "cy": 0.1}   # outside rect zone, inside poly zone
_INSIDE_POLY  = {"track_id": 1, "cx": 0.2, "cy": 0.2}   # inside poly zone, inside rect zone


def _events_of(events, event_type):
    return [e for e in events if e["event_type"] == event_type]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _engine():
    return ZoneEventEngine(dwell_ms=5_000)


def _tick(engine, point, zones, ts):
    return engine.update([point], zones, ts)


# ---------------------------------------------------------------------------
# ENTER / EXIT
# ---------------------------------------------------------------------------

class TestEnterExit(unittest.TestCase):

    def test_enter_fires_on_first_inside_tick(self):
        eng = _engine()
        events = _tick(eng, _INSIDE_RECT, [_RECT_ZONE], ts=1000)
        self.assertEqual(len(_events_of(events, "ENTER")), 1)
        self.assertEqual(events[0]["zone_id"], 1)
        self.assertEqual(events[0]["track_id"], 1)

    def test_no_event_when_already_outside(self):
        eng = _engine()
        events = _tick(eng, _OUTSIDE_RECT, [_RECT_ZONE], ts=1000)
        self.assertEqual(events, [])

    def test_exit_fires_on_leaving(self):
        eng = _engine()
        _tick(eng, _INSIDE_RECT, [_RECT_ZONE], ts=1000)
        events = _tick(eng, _OUTSIDE_RECT, [_RECT_ZONE], ts=2000)
        exits = _events_of(events, "EXIT")
        self.assertEqual(len(exits), 1)
        self.assertEqual(exits[0]["ts_ms"], 2000)

    def test_no_repeated_enter_while_inside(self):
        eng = _engine()
        _tick(eng, _INSIDE_RECT, [_RECT_ZONE], ts=1000)
        events2 = _tick(eng, _INSIDE_RECT, [_RECT_ZONE], ts=1100)
        events3 = _tick(eng, _INSIDE_RECT, [_RECT_ZONE], ts=1200)
        enters = _events_of(events2, "ENTER") + _events_of(events3, "ENTER")
        self.assertEqual(enters, [])

    def test_no_repeated_exit_while_outside(self):
        eng = _engine()
        _tick(eng, _OUTSIDE_RECT, [_RECT_ZONE], ts=1000)
        events2 = _tick(eng, _OUTSIDE_RECT, [_RECT_ZONE], ts=2000)
        self.assertEqual(_events_of(events2, "EXIT"), [])

    def test_reenter_fires_second_enter(self):
        eng = _engine()
        _tick(eng, _INSIDE_RECT, [_RECT_ZONE], ts=1000)
        _tick(eng, _OUTSIDE_RECT, [_RECT_ZONE], ts=2000)   # exit
        events = _tick(eng, _INSIDE_RECT, [_RECT_ZONE], ts=3000)
        enters = _events_of(events, "ENTER")
        self.assertEqual(len(enters), 1)

    def test_event_carries_correct_zone_and_track_ids(self):
        eng = _engine()
        events = _tick(eng, _INSIDE_RECT, [_RECT_ZONE], ts=1000)
        e = events[0]
        self.assertEqual(e["track_id"], 1)
        self.assertEqual(e["zone_id"], 1)
        self.assertEqual(e["ts_ms"], 1000)


# ---------------------------------------------------------------------------
# DWELL
# ---------------------------------------------------------------------------

class TestDwell(unittest.TestCase):

    def test_dwell_fires_after_threshold(self):
        eng = ZoneEventEngine(dwell_ms=500)
        _tick(eng, _INSIDE_RECT, [_RECT_ZONE], ts=0)       # ENTER
        _tick(eng, _INSIDE_RECT, [_RECT_ZONE], ts=400)     # still inside, not yet
        events = _tick(eng, _INSIDE_RECT, [_RECT_ZONE], ts=600)
        self.assertEqual(len(_events_of(events, "DWELL")), 1)

    def test_dwell_does_not_fire_before_threshold(self):
        eng = ZoneEventEngine(dwell_ms=500)
        _tick(eng, _INSIDE_RECT, [_RECT_ZONE], ts=0)
        events = _tick(eng, _INSIDE_RECT, [_RECT_ZONE], ts=499)
        self.assertEqual(_events_of(events, "DWELL"), [])

    def test_dwell_fires_exactly_once_per_stay(self):
        eng = ZoneEventEngine(dwell_ms=500)
        _tick(eng, _INSIDE_RECT, [_RECT_ZONE], ts=0)
        _tick(eng, _INSIDE_RECT, [_RECT_ZONE], ts=600)     # DWELL fires here
        events2 = _tick(eng, _INSIDE_RECT, [_RECT_ZONE], ts=1200)
        events3 = _tick(eng, _INSIDE_RECT, [_RECT_ZONE], ts=2000)
        repeated = _events_of(events2, "DWELL") + _events_of(events3, "DWELL")
        self.assertEqual(repeated, [])

    def test_reenter_resets_dwell_timer(self):
        eng = ZoneEventEngine(dwell_ms=500)
        _tick(eng, _INSIDE_RECT, [_RECT_ZONE], ts=0)
        _tick(eng, _INSIDE_RECT, [_RECT_ZONE], ts=600)     # first DWELL
        _tick(eng, _OUTSIDE_RECT, [_RECT_ZONE], ts=700)    # EXIT — resets
        _tick(eng, _INSIDE_RECT, [_RECT_ZONE], ts=800)     # re-ENTER, timer starts fresh
        no_dwell = _tick(eng, _INSIDE_RECT, [_RECT_ZONE], ts=1200)  # 400 ms since re-entry
        self.assertEqual(_events_of(no_dwell, "DWELL"), [])
        dwell = _tick(eng, _INSIDE_RECT, [_RECT_ZONE], ts=1400)     # 600 ms since re-entry
        self.assertEqual(len(_events_of(dwell, "DWELL")), 1)

    def test_dwell_not_fired_if_track_exits_before_threshold(self):
        eng = ZoneEventEngine(dwell_ms=500)
        _tick(eng, _INSIDE_RECT, [_RECT_ZONE], ts=0)
        _tick(eng, _INSIDE_RECT, [_RECT_ZONE], ts=300)
        events = _tick(eng, _OUTSIDE_RECT, [_RECT_ZONE], ts=400)    # EXIT before 500 ms
        self.assertEqual(_events_of(events, "DWELL"), [])


# ---------------------------------------------------------------------------
# Multiple zones / multiple tracks
# ---------------------------------------------------------------------------

class TestMultipleZonesAndTracks(unittest.TestCase):

    def test_events_for_each_zone_independently(self):
        eng = _engine()
        # track 1 is at (0.5, 0.5): inside rect, outside poly (poly is [0,0]-[0.5,0.5] square)
        point = {"track_id": 1, "cx": 0.5, "cy": 0.5}
        events = _tick(eng, point, _ZONES, ts=1000)
        zone_ids = {e["zone_id"] for e in _events_of(events, "ENTER")}
        self.assertIn(1, zone_ids)       # inside rect zone
        self.assertNotIn(2, zone_ids)    # outside poly zone (0.5, 0.5 is corner — undefined)

    def test_two_tracks_independent_state(self):
        eng = _engine()
        t1 = {"track_id": 1, "cx": 0.5, "cy": 0.5}
        t2 = {"track_id": 2, "cx": 0.1, "cy": 0.1}
        events = eng.update([t1, t2], [_RECT_ZONE], ts_ms=1000)
        enters = _events_of(events, "ENTER")
        entering_ids = {e["track_id"] for e in enters}
        self.assertIn(1, entering_ids)
        self.assertNotIn(2, entering_ids)

    def test_different_tracks_dont_share_dwell_state(self):
        eng = ZoneEventEngine(dwell_ms=500)
        t1 = {"track_id": 1, "cx": 0.5, "cy": 0.5}
        t2 = {"track_id": 2, "cx": 0.5, "cy": 0.5}
        eng.update([t1], [_RECT_ZONE], ts_ms=0)       # t1 enters
        eng.update([t1], [_RECT_ZONE], ts_ms=600)     # t1 dwells
        # t2 enters now — its own 500 ms timer starts fresh
        eng.update([t2], [_RECT_ZONE], ts_ms=700)
        events_early = eng.update([t2], [_RECT_ZONE], ts_ms=1000)   # 300 ms into t2's stay
        self.assertEqual(_events_of(events_early, "DWELL"), [])
        events_late = eng.update([t2], [_RECT_ZONE], ts_ms=1300)    # 600 ms into t2's stay
        self.assertEqual(len(_events_of(events_late, "DWELL")), 1)

    def test_polygon_zone_enter_exit(self):
        eng = _engine()
        inside_poly  = {"track_id": 3, "cx": 0.2, "cy": 0.2}
        outside_poly = {"track_id": 3, "cx": 0.9, "cy": 0.9}
        events_in  = _tick(eng, inside_poly,  [_POLY_ZONE], ts=1000)
        events_out = _tick(eng, outside_poly, [_POLY_ZONE], ts=2000)
        self.assertEqual(len(_events_of(events_in,  "ENTER")), 1)
        self.assertEqual(len(_events_of(events_out, "EXIT")),  1)

    def test_empty_zones_produces_no_events(self):
        eng = _engine()
        events = eng.update([_INSIDE_RECT], [], ts_ms=1000)
        self.assertEqual(events, [])

    def test_empty_tracks_produces_no_events(self):
        eng = _engine()
        events = eng.update([], [_RECT_ZONE], ts_ms=1000)
        self.assertEqual(events, [])


if __name__ == "__main__":
    unittest.main()
