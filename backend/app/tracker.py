import math

_EMA_ALPHA = 0.4
_IOU_THRESH = 0.1
_DIST_THRESH = 80
_MAX_MISSES = 20


def _iou(a, b):
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1 = max(ax1, bx1)
    iy1 = max(ay1, by1)
    ix2 = min(ax2, bx2)
    iy2 = min(ay2, by2)
    inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)
    if inter == 0:
        return 0.0
    area_a = (ax2 - ax1) * (ay2 - ay1)
    area_b = (bx2 - bx1) * (by2 - by1)
    return inter / (area_a + area_b - inter)


def _dist(ax, ay, bx, by):
    return math.sqrt((ax - bx) ** 2 + (ay - by) ** 2)


class Track:
    __slots__ = ("id", "cx", "cy", "vx", "vy", "age", "misses", "confidence",
                 "x1", "y1", "x2", "y2")

    def __init__(self, tid, x1, y1, x2, y2, conf):
        self.id = tid
        self.x1, self.y1, self.x2, self.y2 = x1, y1, x2, y2
        self.cx = (x1 + x2) / 2
        self.cy = (y1 + y2) / 2
        self.vx = 0.0
        self.vy = 0.0
        self.age = 1
        self.misses = 0
        self.confidence = conf

    def update(self, x1, y1, x2, y2, conf):
        new_cx = (x1 + x2) / 2
        new_cy = (y1 + y2) / 2
        dx = new_cx - self.cx
        dy = new_cy - self.cy
        self.vx = _EMA_ALPHA * dx + (1 - _EMA_ALPHA) * self.vx
        self.vy = _EMA_ALPHA * dy + (1 - _EMA_ALPHA) * self.vy
        self.cx, self.cy = new_cx, new_cy
        self.x1, self.y1, self.x2, self.y2 = x1, y1, x2, y2
        self.confidence = conf
        self.misses = 0
        self.age += 1


class MultiObjectTracker:
    def __init__(self):
        self._tracks: list[Track] = []
        self._next_id = 1

    def update(self, detections):
        unmatched_dets = list(range(len(detections)))
        matched_track_ids = set()

        for tr in self._tracks:
            best_score = -1
            best_det = -1
            for di in unmatched_dets:
                x1, y1, x2, y2, conf = detections[di]
                iou = _iou((tr.x1, tr.y1, tr.x2, tr.y2), (x1, y1, x2, y2))
                dcx = (x1 + x2) / 2
                dcy = (y1 + y2) / 2
                d = _dist(tr.cx, tr.cy, dcx, dcy)
                if iou >= _IOU_THRESH:
                    score = iou
                elif d <= _DIST_THRESH:
                    score = 1 - d / _DIST_THRESH * 0.5  # 0.5–1 range
                else:
                    continue
                if score > best_score:
                    best_score = score
                    best_det = di

            if best_det >= 0:
                x1, y1, x2, y2, conf = detections[best_det]
                tr.update(x1, y1, x2, y2, conf)
                matched_track_ids.add(tr.id)
                unmatched_dets.remove(best_det)
            else:
                tr.misses += 1

        # start new tracks for unmatched detections
        for di in unmatched_dets:
            x1, y1, x2, y2, conf = detections[di]
            self._tracks.append(Track(self._next_id, x1, y1, x2, y2, conf))
            self._next_id += 1

        # drop lost tracks
        self._tracks = [t for t in self._tracks if t.misses <= _MAX_MISSES]

        return self._tracks
