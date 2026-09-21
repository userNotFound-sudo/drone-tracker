import asyncio
import json
import math
import os
import pathlib
import re
import time

import cv2
from fastapi import FastAPI, HTTPException, Query, Response, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .altitude import AltitudeEstimator, load_config
from .db import TrackSampleWriter, get_connection, init_db
from .detector import BlobDetector, YoloDetector
from .tracker import MultiObjectTracker
from .zone_engine import ZoneEventEngine

VIDEO_PATH = pathlib.Path(__file__).parent.parent / "data" / "perdix_swarm_demo.mp4"

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^http://(localhost|127\.0\.0\.1):\d+$",
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["*"],
)

# shared state written by the pipeline task, read by WebSocket clients
_current_tracks: list[dict] = []
_current_media_t_sec: float = 0.0
_frame_meta: dict = {"frame_w": 1024, "frame_h": 576, "fps": 30}

_writer        = TrackSampleWriter()
_zone_engine   = ZoneEventEngine()
_current_zones: list[dict] = []   # in-memory zone cache, refreshed on every write
_pending_events: list[dict] = []  # events queued for the next WS send


def _refresh_zones() -> None:
    global _current_zones
    with get_connection() as conn:
        cur = conn.execute(
            "SELECT id, name, shape, coords FROM zones ORDER BY id"
        )
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    for row in rows:
        row["coords"] = json.loads(row["coords"])
    _current_zones = rows


# Radar SVG constants — must match App.jsx (W=680, H=520, radius = min(W,H)/2 - 28)
_RADAR_W  = 680.0
_RADAR_H  = 520.0
_RADAR_CX = _RADAR_W / 2   # 340
_RADAR_CY = _RADAR_H / 2   # 260
_RADAR_R  = min(_RADAR_W, _RADAR_H) / 2 - 28  # 232


def _radar_norm(bearing: float, range_u: float) -> tuple[float, float]:
    """Map (bearing °, range_u) to the same [0,1] space as zones drawn on the SVG."""
    a = math.radians(bearing - 90)
    rr = _RADAR_R * range_u
    x = _RADAR_CX + rr * math.cos(a)
    y = _RADAR_CY + rr * math.sin(a)
    return x / _RADAR_W, y / _RADAR_H


def _pixel_to_radar(cx, cy, frame_w, frame_h):
    """Map a pixel centre to (bearing °, range_u 0-1)."""
    h_fov = 60.0
    offset_x = cx - frame_w / 2
    bearing = (offset_x / (frame_w / 2)) * (h_fov / 2)
    bearing = bearing % 360
    range_u = 1.0 - (cy / frame_h)          # top = far (1), bottom = near (0)
    range_u = max(0.06, min(0.98, range_u))
    return round(bearing, 1), round(range_u, 3)


def _velocity_to_heading_speed(vx, vy, norm=10.0):
    heading = math.degrees(math.atan2(vx, -vy)) % 360
    speed = math.sqrt(vx ** 2 + vy ** 2) / norm
    return round(heading, 1), round(min(speed, 30.0), 1)


def _alt_band(range_u):
    if range_u < 0.35:
        return "LOW"
    if range_u < 0.7:
        return "MED"
    return "HIGH"


def _build_track(tr, frame_w, frame_h, fps=30.0, alt_est=None):
    bearing, range_u = _pixel_to_radar(tr.cx, tr.cy, frame_w, frame_h)
    # convert tracker velocity from px/frame → px/s before computing heading and speed
    heading, speed = _velocity_to_heading_speed(tr.vx * fps, tr.vy * fps)
    conf = round(max(0.22, min(0.98, tr.confidence)), 2)
    flags = []
    if conf < 0.50:
        flags.append("LOW_CONF")
    if tr.misses > 10:
        flags.append("OCCLUDED")
    altitude_m = alt_est.estimate(tr.cy, range_u) if alt_est else None
    return {
        "id": tr.id,
        "callsign": f"UAV-{tr.id:02d}",
        "type": "unknown",
        "bearing": bearing,
        "range_u": range_u,
        "heading": heading,
        "rel_speed_u": speed,
        "alt_band": _alt_band(range_u),
        "confidence": conf,
        "flags": flags,
        "bbox": [tr.x1, tr.y1, tr.x2, tr.y2],
        "altitude_m": altitude_m,
    }


async def _video_pipeline():
    global _current_tracks, _current_media_t_sec, _frame_meta

    if not VIDEO_PATH.exists():
        print(f"[pipeline] video not found: {VIDEO_PATH}")
        return

    detector_name = os.environ.get("DETECTOR", "blob").lower()
    if detector_name == "yolo":
        detector = YoloDetector()
        print("[pipeline] detector: YoloDetector (yolov8n.pt, conf=0.12)")
    else:
        detector = BlobDetector()
        print("[pipeline] detector: BlobDetector")
    tracker = MultiObjectTracker()

    # altitude estimator — optional, continues without it if config is missing/invalid
    alt_est = None
    try:
        cfg = load_config()
        alt_est = None  # created after we know frame_h below
        _alt_cfg = cfg
    except Exception as e:
        print(f"[pipeline] altitude config skipped: {e}")
        _alt_cfg = None

    cap = cv2.VideoCapture(str(VIDEO_PATH))
    frame_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    frame_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    print(f"[pipeline] {VIDEO_PATH.name} — {frame_w}×{frame_h} @ {fps:.2f} fps — {total} frames")
    _frame_meta = {"frame_w": frame_w, "frame_h": frame_h, "fps": fps}

    if _alt_cfg:
        try:
            alt_est = AltitudeEstimator(_alt_cfg, frame_h)
            print(f"[pipeline] altitude estimator ready (horizon px={alt_est._y_horiz:.1f})")
        except Exception as e:
            print(f"[pipeline] altitude estimator failed: {e}")

    frame_delay = 1.0 / fps
    sample_every = max(1, round(fps / 10.0))  # record at ~10 Hz
    frame_index = 0

    while True:
        ok, frame = cap.read()
        if not ok:
            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            tracker = MultiObjectTracker()   # reset IDs on loop
            frame_index = 0
            continue

        _current_media_t_sec = frame_index / fps
        detections = detector.detect(frame)
        tracks = tracker.update(detections)
        _current_tracks = [_build_track(t, frame_w, frame_h, fps, alt_est) for t in tracks]

        ts_ms = int(time.time() * 1000)

        # zone event detection — uses radar SVG-normalised coords to match drawn zones
        if _current_zones and _current_tracks:
            track_points = [
                {"track_id": t["id"], "cx": cx, "cy": cy}
                for t in _current_tracks
                for cx, cy in [_radar_norm(t["bearing"], t["range_u"])]
            ]
            if track_points:
                zone_events = _zone_engine.update(track_points, _current_zones, ts_ms)
                if zone_events:
                    _pending_events.extend(zone_events)
                    with get_connection() as conn:
                        conn.executemany(
                            "INSERT INTO events (zone_id, ts_ms, event_type, track_id)"
                            " VALUES (?, ?, ?, ?)",
                            [(e["zone_id"], e["ts_ms"], e["event_type"], e["track_id"])
                             for e in zone_events],
                        )

        if frame_index % sample_every == 0:
            for t in _current_tracks:
                _writer.add_sample({
                    "ts_ms":       ts_ms,
                    "track_id":    t["id"],
                    "bearing":     t["bearing"],
                    "range_u":     t["range_u"],
                    "heading":     t["heading"],
                    "rel_speed_u": t["rel_speed_u"],
                    "altitude_m":  t["altitude_m"],
                    "confidence":  t["confidence"],
                })

        frame_index += 1

        await asyncio.sleep(frame_delay)


@app.on_event("startup")
async def startup():
    init_db()
    _refresh_zones()
    _writer.start()
    asyncio.create_task(_video_pipeline())


@app.on_event("shutdown")
async def shutdown():
    _writer.stop()


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/video")
def video():
    if not VIDEO_PATH.exists():
        raise HTTPException(status_code=404, detail="Video file not found")
    return FileResponse(str(VIDEO_PATH), media_type="video/mp4")


_MAX_REPLAY_MS = 10 * 60 * 1000  # 10 minutes

# ---------------------------------------------------------------------------
# Zones
# ---------------------------------------------------------------------------

_CTRL_RE   = re.compile(r'[\x00-\x1f\x7f]')
_SHAPES    = {'rectangle', 'polygon'}
_MAX_VERTS = 256
_MAX_NAME  = 128


class ZoneCreate(BaseModel):
    name:   str
    shape:  str
    coords: list[list[float]]


def _validate_zone(z: ZoneCreate) -> tuple[str, str]:
    name = _CTRL_RE.sub('', z.name)[:_MAX_NAME].strip()
    if not name:
        raise HTTPException(400, "name is empty after sanitization")
    if z.shape not in _SHAPES:
        raise HTTPException(400, f"shape must be 'rectangle' or 'polygon'")
    coords = z.coords
    if not coords or not isinstance(coords, list):
        raise HTTPException(400, "coords must be a non-empty list of [x, y] pairs")
    for i, pt in enumerate(coords):
        if not isinstance(pt, list) or len(pt) != 2:
            raise HTTPException(400, f"coords[{i}] must be [x, y]")
        x, y = pt
        if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
            raise HTTPException(400, f"coords[{i}] [{x}, {y}] is outside [0, 1]")
    if z.shape == 'rectangle':
        if len(coords) != 2:
            raise HTTPException(400, "rectangle requires exactly 2 points [[x1,y1],[x2,y2]]")
        x1, y1 = coords[0]
        x2, y2 = coords[1]
        if x1 >= x2 or y1 >= y2:
            raise HTTPException(400, "rectangle: x1 < x2 and y1 < y2 required")
    else:  # polygon
        if len(coords) < 3:
            raise HTTPException(400, "polygon requires at least 3 vertices")
        if len(coords) > _MAX_VERTS:
            raise HTTPException(400, f"polygon exceeds maximum of {_MAX_VERTS} vertices")
    return name, json.dumps(coords)


@app.get("/zones")
def get_zones():
    with get_connection() as conn:
        cur = conn.execute(
            "SELECT id, name, shape, coords, created_ms FROM zones ORDER BY id"
        )
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    for row in rows:
        row["coords"] = json.loads(row["coords"])
    return rows


@app.post("/zones", status_code=201)
def create_zone(z: ZoneCreate):
    name, coords_json = _validate_zone(z)
    ts_ms = int(time.time() * 1000)
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO zones (name, shape, coords, created_ms) VALUES (?, ?, ?, ?)",
            (name, z.shape, coords_json, ts_ms),
        )
        zone_id = cur.lastrowid
    _refresh_zones()
    return {"id": zone_id, "name": name, "shape": z.shape, "coords": z.coords, "created_ms": ts_ms}


@app.delete("/zones/{zone_id}", status_code=204)
def delete_zone(zone_id: int):
    with get_connection() as conn:
        cur = conn.execute("DELETE FROM zones WHERE id = ?", (zone_id,))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail=f"Zone {zone_id} not found")
    _refresh_zones()
    return Response(status_code=204)


@app.get("/events")
def get_events(
    start_ms: int = Query(...),
    end_ms:   int = Query(...),
    limit:    int = Query(1000),
):
    if start_ms >= end_ms:
        raise HTTPException(status_code=400, detail="start_ms must be less than end_ms")
    if limit < 1 or limit > 20_000:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 20000")
    with get_connection() as conn:
        cur = conn.execute(
            "SELECT e.id, e.zone_id, z.name AS zone_name,"
            "       e.ts_ms, e.event_type, e.track_id, e.detail"
            " FROM events e LEFT JOIN zones z ON e.zone_id = z.id"
            " WHERE e.ts_ms >= ? AND e.ts_ms <= ?"
            " ORDER BY e.ts_ms"
            " LIMIT ?",
            (start_ms, end_ms, limit),
        )
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    return {"count": len(rows), "events": rows}


@app.get("/replay")
def replay(
    start_ms: int = Query(...),
    end_ms: int = Query(...),
    limit: int = Query(1000),
):
    if start_ms >= end_ms:
        raise HTTPException(status_code=400, detail="start_ms must be less than end_ms")
    if end_ms - start_ms > _MAX_REPLAY_MS:
        raise HTTPException(status_code=400, detail="Time range exceeds maximum of 10 minutes (600 000 ms)")
    if limit < 1 or limit > 20_000:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 20000")

    with get_connection() as conn:
        cur = conn.execute(
            "SELECT ts_ms, track_id, bearing, range_u, heading, rel_speed_u, altitude_m, confidence"
            " FROM track_samples"
            " WHERE ts_ms >= ? AND ts_ms <= ?"
            " ORDER BY ts_ms"
            " LIMIT ?",
            (start_ms, end_ms, limit),
        )
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, row)) for row in cur.fetchall()]

    return {"count": len(rows), "rows": rows}


_MIN_RATE = 0.1
_MAX_RATE = 10.0
_MIN_HZ   = 1.0
_MAX_HZ   = 30.0


def _replay_track(row: dict) -> dict:
    """Reconstruct a frontend-compatible track dict from a stored DB row."""
    track_id = row["track_id"]
    range_u  = row["range_u"] or 0.5
    return {
        "id":          track_id,
        "callsign":    f"UAV-{track_id:02d}",
        "type":        "unknown",
        "bearing":     row["bearing"],
        "range_u":     range_u,
        "heading":     row["heading"],
        "rel_speed_u": row["rel_speed_u"],
        "alt_band":    _alt_band(range_u),
        "confidence":  row["confidence"],
        "flags":       [],
        "altitude_m":  row["altitude_m"],
    }


@app.websocket("/ws/replay")
async def ws_replay(
    websocket: WebSocket,
    start_ms: int   = Query(...),
    end_ms:   int   = Query(...),
    rate:     float = Query(1.0),
    hz:       float = Query(10.0),
):
    await websocket.accept()

    errors = []
    if start_ms >= end_ms:
        errors.append("start_ms must be less than end_ms")
    if end_ms - start_ms > _MAX_REPLAY_MS:
        errors.append("time range exceeds 10 minutes")
    if not (_MIN_RATE <= rate <= _MAX_RATE):
        errors.append(f"rate must be {_MIN_RATE}–{_MAX_RATE}")
    if not (_MIN_HZ <= hz <= _MAX_HZ):
        errors.append(f"hz must be {_MIN_HZ}–{_MAX_HZ}")

    if errors:
        await websocket.send_json({"type": "error", "detail": "; ".join(errors)})
        await websocket.close(code=4400)
        return

    # Load and group all rows by timestamp up front (max ~10 min × 10 Hz × N tracks)
    with get_connection() as conn:
        cur = conn.execute(
            "SELECT ts_ms, track_id, bearing, range_u, heading, rel_speed_u, altitude_m, confidence"
            " FROM track_samples"
            " WHERE ts_ms >= ? AND ts_ms <= ?"
            " ORDER BY ts_ms",
            (start_ms, end_ms),
        )
        cols = [d[0] for d in cur.description]
        raw_rows = cur.fetchall()

    frames: dict[int, list[dict]] = {}
    for row in raw_rows:
        r = dict(zip(cols, row))
        frames.setdefault(r["ts_ms"], []).append(r)

    timestamps = sorted(frames)

    if not timestamps:
        await websocket.send_json({"type": "done", "count": 0})
        await websocket.close()
        return

    # Replay: advance a cursor through the timeline at `rate` speed, ticking at `hz`
    tick_s      = 1.0 / hz
    advance_ms  = rate * (1000.0 / hz)   # ms of recorded time covered per tick
    cursor_ms   = float(start_ms)
    ts_idx      = 0

    try:
        while cursor_ms <= end_ms:
            # Move frame pointer forward to the last stored timestamp <= cursor
            while ts_idx + 1 < len(timestamps) and timestamps[ts_idx + 1] <= cursor_ms:
                ts_idx += 1

            current_ts = timestamps[ts_idx]
            await websocket.send_json({
                "type":        "tracks_snapshot",
                "media_t_sec": (current_ts - start_ms) / 1000.0,
                "tracks":      [_replay_track(r) for r in frames[current_ts]],
            })

            cursor_ms += advance_ms
            await asyncio.sleep(tick_s)

        await websocket.send_json({"type": "done", "count": len(timestamps)})
        await websocket.close()

    except Exception:
        pass  # client disconnected mid-replay


@app.websocket("/ws/tracks")
async def ws_tracks(websocket: WebSocket):
    await websocket.accept()
    await websocket.send_json({"type": "hello", **_frame_meta})
    while True:
        await websocket.send_json({
            "type": "tracks_snapshot",
            "media_t_sec": _current_media_t_sec,
            "tracks": _current_tracks,
        })
        if _pending_events:
            batch = _pending_events[:]
            del _pending_events[:]
            await websocket.send_json({"type": "events", "events": batch})
        await asyncio.sleep(0.1)
