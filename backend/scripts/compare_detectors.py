"""
Compare BlobDetector vs YoloDetector on a short slice of the Perdix video.
Usage (from backend/):
    python scripts/compare_detectors.py
Requires ultralytics for the YOLO leg: pip install ultralytics
"""

import sys
import time
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

import cv2
from app.detector import BlobDetector, YoloDetector
from app.tracker import MultiObjectTracker

VIDEO = pathlib.Path(__file__).parent.parent / "data" / "perdix_swarm_demo.mp4"
START_FRAME = 0
NUM_FRAMES  = 150   # ~5 s at 29.97 fps


def run_detector(detector_name, detector):
    cap = cv2.VideoCapture(str(VIDEO))
    cap.set(cv2.CAP_PROP_POS_FRAMES, START_FRAME)
    fps_src = cap.get(cv2.CAP_PROP_FPS) or 30

    tracker = MultiObjectTracker()
    total_dets  = 0
    frame_count = 0
    track_lifetimes = {}   # id -> frame count seen

    t0 = time.perf_counter()
    for _ in range(NUM_FRAMES):
        ok, frame = cap.read()
        if not ok:
            break
        dets   = detector.detect(frame)
        tracks = tracker.update(dets)
        total_dets  += len(dets)
        frame_count += 1
        for tr in tracks:
            track_lifetimes[tr.id] = track_lifetimes.get(tr.id, 0) + 1

    elapsed = time.perf_counter() - t0
    cap.release()

    fps_proc      = frame_count / elapsed if elapsed > 0 else 0
    avg_dets      = total_dets / frame_count if frame_count else 0
    n_tracks      = len(track_lifetimes)
    long_tracks   = sum(1 for v in track_lifetimes.values() if v >= int(fps_src * 2))
    avg_life      = sum(track_lifetimes.values()) / n_tracks if n_tracks else 0

    print(f"\n{'='*50}")
    print(f"  {detector_name}")
    print(f"{'='*50}")
    print(f"  Frames processed : {frame_count}")
    print(f"  Wall time        : {elapsed:.1f}s")
    print(f"  Processing FPS   : {fps_proc:.2f}")
    print(f"  Total detections : {total_dets}")
    print(f"  Avg dets/frame   : {avg_dets:.1f}")
    print(f"  Unique track IDs : {n_tracks}")
    print(f"  Tracks alive >=2s: {long_tracks}")
    print(f"  Avg track life   : {avg_life:.1f} frames ({avg_life/fps_src:.2f}s)")

    return {
        "name": detector_name,
        "frames": frame_count,
        "elapsed": elapsed,
        "fps": fps_proc,
        "total_dets": total_dets,
        "avg_dets": avg_dets,
        "unique_tracks": n_tracks,
        "long_tracks": long_tracks,
        "avg_life_frames": avg_life,
        "avg_life_sec": avg_life / fps_src,
    }


if __name__ == "__main__":
    print(f"Video : {VIDEO}")
    print(f"Slice : frames {START_FRAME}–{START_FRAME+NUM_FRAMES} (~{NUM_FRAMES/30:.0f}s)")

    results = []

    # --- Blob ---
    results.append(run_detector("BlobDetector", BlobDetector()))

    # --- YOLO ---
    try:
        yolo = YoloDetector()
        results.append(run_detector("YoloDetector (yolov8n.pt, conf=0.12)", yolo))
    except Exception as e:
        print(f"\nYoloDetector skipped: {e}")
        print("Run: pip install ultralytics")

    # --- Summary table ---
    if len(results) == 2:
        b, y = results
        print("\n" + "="*50)
        print("  SUMMARY")
        print("="*50)
        print(f"  {'Metric':<28} {'Blob':>10} {'YOLO':>10}")
        print(f"  {'-'*48}")
        print(f"  {'Processing FPS':<28} {b['fps']:>10.1f} {y['fps']:>10.1f}")
        print(f"  {'Avg dets/frame':<28} {b['avg_dets']:>10.1f} {y['avg_dets']:>10.1f}")
        print(f"  {'Unique track IDs':<28} {b['unique_tracks']:>10} {y['unique_tracks']:>10}")
        print(f"  {'Tracks alive >=2s':<28} {b['long_tracks']:>10} {y['long_tracks']:>10}")
        print(f"  {'Avg track life (s)':<28} {b['avg_life_sec']:>10.2f} {y['avg_life_sec']:>10.2f}")
