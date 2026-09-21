from .geometry import point_in_polygon, point_in_rect

_DWELL_MS = 5_000  # 5 seconds


class ZoneEventEngine:
    """Track-zone containment state machine.

    Caller provides normalized track centres (cx, cy in [0,1]) and the current
    zone list each tick.  The engine emits ENTER / EXIT / DWELL events and
    guarantees:
      - DWELL fires exactly once per continuous stay (reset on EXIT or re-ENTER)
      - Re-entering a zone starts a fresh 5-second dwell timer
      - No repeated DWELL events during the same stay
    """

    def __init__(self, dwell_ms: int = _DWELL_MS) -> None:
        self._dwell_ms = dwell_ms
        # (track_id, zone_id) -> {"inside", "enter_ts", "dwell_emitted"}
        self._state: dict[tuple[int, int], dict] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def update(
        self,
        track_points: list[dict],
        zones: list[dict],
        ts_ms: int,
    ) -> list[dict]:
        """Process one tick.

        track_points: [{"track_id": int, "cx": float, "cy": float}, ...]
            cx/cy must be normalized to [0, 1] (caller converts from pixels).
        zones: [{"id": int, "shape": str, "coords": list}, ...]
        ts_ms: wall-clock timestamp for this tick (milliseconds).

        Returns a list of event dicts ready for persistence or broadcast:
            {"event_type": "ENTER"|"EXIT"|"DWELL",
             "track_id": int, "zone_id": int, "ts_ms": int}
        """
        events: list[dict] = []

        for tp in track_points:
            tid   = tp["track_id"]
            point = (tp["cx"], tp["cy"])

            for zone in zones:
                zid = zone["id"]
                key = (tid, zid)

                state = self._state.setdefault(
                    key,
                    {"inside": False, "enter_ts": None, "dwell_emitted": False},
                )

                now_inside = (
                    point_in_rect(point, zone["coords"])
                    if zone["shape"] == "rectangle"
                    else point_in_polygon(point, zone["coords"])
                )
                was_inside = state["inside"]

                # --- state transition ---
                if not was_inside and now_inside:
                    state["inside"]       = True
                    state["enter_ts"]     = ts_ms
                    state["dwell_emitted"] = False
                    events.append(self._make("ENTER", tid, zid, ts_ms))

                elif was_inside and not now_inside:
                    state["inside"]       = False
                    state["enter_ts"]     = None
                    state["dwell_emitted"] = False
                    events.append(self._make("EXIT", tid, zid, ts_ms))

                # --- dwell check (fires once per continuous stay) ---
                if (
                    state["inside"]
                    and not state["dwell_emitted"]
                    and state["enter_ts"] is not None
                    and ts_ms - state["enter_ts"] >= self._dwell_ms
                ):
                    state["dwell_emitted"] = True
                    events.append(self._make("DWELL", tid, zid, ts_ms))

        return events

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def reset(self) -> None:
        """Clear all track-zone state. Call when track IDs are reassigned (e.g. video loop)."""
        self._state.clear()

    @staticmethod
    def _make(event_type: str, track_id: int, zone_id: int, ts_ms: int) -> dict:
        return {
            "event_type": event_type,
            "track_id":   track_id,
            "zone_id":    zone_id,
            "ts_ms":      ts_ms,
        }
