import asyncio
import math
import pathlib
import time

import cv2
from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware

from .detector import BlobDetector
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
_frame_meta: dict = {"frame_w": 1024, "frame_h": 576, "fps": 30}


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


def _build_track(tr, frame_w, frame_h):
    bearing, range_u = _pixel_to_radar(tr.cx, tr.cy, frame_w, frame_h)
    heading, speed = _velocity_to_heading_speed(tr.vx, tr.vy)
    conf = round(max(0.22, min(0.98, tr.confidence)), 2)
    flags = []
    if conf < 0.50:
        flags.append("LOW_CONF")
    if tr.misses > 10:
        flags.append("OCCLUDED")
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
    }


async def _video_pipeline():
    global _current_tracks, _frame_meta

    if not VIDEO_PATH.exists():
        print(f"[pipeline] video not found: {VIDEO_PATH}")
        return

    detector = BlobDetector()
    tracker = MultiObjectTracker()

    cap = cv2.VideoCapture(str(VIDEO_PATH))
    frame_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    frame_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    print(f"[pipeline] {VIDEO_PATH.name} — {frame_w}×{frame_h} @ {fps:.2f} fps — {total} frames")
    _frame_meta = {"frame_w": frame_w, "frame_h": frame_h, "fps": fps}

    frame_delay = 1.0 / fps

    while True:
        ok, frame = cap.read()
        if not ok:
            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            tracker = MultiObjectTracker()   # reset IDs on loop
            continue

        detections = detector.detect(frame)
        tracks = tracker.update(detections)
        _current_tracks = [_build_track(t, frame_w, frame_h) for t in tracks]

        await asyncio.sleep(frame_delay)


@app.on_event("startup")
async def startup():
    asyncio.create_task(_video_pipeline())


@app.get("/health")
def health():
    return {"ok": True}


@app.websocket("/ws/tracks")
async def ws_tracks(websocket: WebSocket):
    await websocket.accept()
    await websocket.send_json({"type": "hello", **_frame_meta})
    while True:
        await websocket.send_json({"type": "tracks_snapshot", "tracks": _current_tracks})
        await asyncio.sleep(0.1)
