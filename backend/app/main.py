import asyncio
import math
import os
import pathlib
import time

import cv2
from fastapi import FastAPI, HTTPException, Query, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from .altitude import AltitudeEstimator, load_config
from .db import TrackSampleWriter, get_connection, init_db
from .detector import BlobDetector, YoloDetector
from .tracker import MultiObjectTracker

VIDEO_PATH = pathlib.Path(__file__).parent.parent / "data" / "perdix_swarm_demo.mp4"

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^http://(localhost|127\.0\.0\.1):\d+$",
    allow_methods=["GET"],
    allow_headers=["*"],
)

# shared state written by the pipeline task, read by WebSocket clients
_current_tracks: list[dict] = []
_current_media_t_sec: float = 0.0
_frame_meta: dict = {"frame_w": 1024, "frame_h": 576, "fps": 30}

_writer = TrackSampleWriter()


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

        if frame_index % sample_every == 0:
            ts_ms = int(time.time() * 1000)
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
        await asyncio.sleep(0.1)
