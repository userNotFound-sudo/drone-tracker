import asyncio
import math
import time

from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^http://(localhost|127\.0\.0\.1):\d+$",
    allow_methods=["GET"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    return {"ok": True}


@app.websocket("/ws/tracks")
async def ws_tracks(websocket: WebSocket):
    await websocket.accept()
    await websocket.send_json({
        "type": "hello",
        "frame_w": 1024,
        "frame_h": 576,
        "fps": 30,
    })
    start = time.monotonic()
    while True:
        elapsed = time.monotonic() - start
        bearing = (elapsed * 36) % 360  # one full rotation every 10 s
        range_u = 0.5 + math.sin(elapsed * 0.3) * 0.25

        track = {
            "id": 1,
            "callsign": "UAV-01",
            "type": "unknown",
            "bearing": round(bearing, 1),
            "range_u": round(range_u, 3),
            "heading": round((bearing + 90) % 360, 1),
            "rel_speed_u": 12.0,
            "alt_band": "MED",
            "confidence": 0.80,
            "flags": [],
        }

        await websocket.send_json({"type": "tracks_snapshot", "tracks": [track]})
        await asyncio.sleep(0.1)
