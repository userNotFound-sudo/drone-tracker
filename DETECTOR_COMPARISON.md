# Detector Comparison — BlobDetector vs YoloDetector

**Video:** `perdix_swarm_demo.mp4`  
**Slice tested:** frames 0–150 (~5 seconds, opening aerial footage)  
**Machine:** CPU only (no GPU)

---

## Benchmark Results

| Metric | BlobDetector | YoloDetector (yolov8n.pt) |
|---|---|---|
| Processing FPS | **134.2** | 15.2 |
| Avg detections / frame | 56.1 | **1.2** |
| Unique track IDs created | 108 | 2 |
| Tracks alive ≥ 2 s | 64 | 1 |
| Avg track lifetime | 3.15 s | 2.94 s |

---

## Which Detector Found More Targets

**BlobDetector** found far more objects — 56 detections per frame on average versus 1.2 for YOLO. However, quantity is not quality. The opening 5 seconds of the clip show two large F/A-18 aircraft against bright sky, instrument panels, and cockpit glare — all of which produce large, high-contrast blobs. BlobDetector finds all of them plus a large number of background specks (sky texture, compression artefacts, reflections).

YoloDetector, trained on general objects, detected only the most recognisable targets in those frames — roughly one aircraft-shaped region per frame — and ignored most of the sky noise.

---

## Major False Positives

**BlobDetector:**
- Sky texture and cloud edges treated as targets
- JPEG/MP4 compression artefacts in high-contrast regions
- Cockpit instrument panels and window reflections
- The entire body of the F/A-18 jets occasionally split into 10+ overlapping boxes

**YoloDetector:**
- Occasionally classified the jet fuselage as an `airplane` with low confidence (~0.14), which is technically correct but not a useful "drone" detection
- Near-zero false positives by count — but that is partly because it found almost nothing

---

## Major Missed Detections

**BlobDetector:**
- Very few misses on any high-contrast region; the problem is the opposite (too sensitive)
- Tiny Perdix drones (sub-10 px) at moment of ejection may fall below the 16 px² area filter

**YoloDetector:**
- Missed the actual Perdix drones entirely — they are 4–15 px wide and yolov8n was not trained on objects that small
- Missed most of the frame content; conf=0.12 threshold is already very low but the model has no drone class that matches these targets

---

## FPS Comparison

| Detector | FPS (CPU) | Real-time at 29.97 fps? |
|---|---|---|
| BlobDetector | 134 | Yes — 4.5× real-time |
| YoloDetector | 15 | No — ~0.5× real-time |

YOLO at CPU speeds cannot keep up with 30 fps video without frame-skipping. BlobDetector runs well above real-time even with no optimisation.

---

## Preference and Reasoning

**Preferred for this project: BlobDetector.**

YoloDetector's low detection rate on this specific footage is a fundamental mismatch — yolov8n was trained on COCO, which contains no class for a 6-inch military micro-drone seen from 1 km away. At 15 FPS on CPU it also cannot run the live pipeline at full frame rate.

BlobDetector's high false-positive rate is a real problem but a tractable one: area filters, aspect-ratio filters, and temporal consistency in the tracker already suppress most noise. The Perdix drones are exactly the kind of small, high-contrast moving targets that Otsu thresholding was designed to find.

A fine-tuned YOLO model trained specifically on aerial drone footage would likely outperform BlobDetector on precision. That is a Week 22+ improvement; for the current stage BlobDetector is the practical choice.

---

*Benchmark script: `backend/scripts/compare_detectors.py`*  
*No precision/recall statistics — eyeball judgment on a 5-second clip only.*
