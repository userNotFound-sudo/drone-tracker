import cv2


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


def _nms(boxes, iou_thresh=0.4):
    if not boxes:
        return []
    boxes = sorted(boxes, key=lambda b: b[4], reverse=True)
    kept = []
    for box in boxes:
        if all(_iou(box[:4], k[:4]) < iou_thresh for k in kept):
            kept.append(box)
    return kept


def _contours_to_boxes(mask, conf):
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    boxes = []
    for c in contours:
        area = cv2.contourArea(c)
        if area < 16 or area > 80_000:
            continue
        x, y, w, h = cv2.boundingRect(c)
        boxes.append((x, y, x + w, y + h, conf))
    return boxes


class BlobDetector:
    def __init__(self):
        self._kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))

    def detect(self, frame_bgr):
        gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
        blur = cv2.GaussianBlur(gray, (5, 5), 0)

        # dark targets against bright background
        _, mask_inv = cv2.threshold(255 - blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        mask_inv = cv2.morphologyEx(mask_inv, cv2.MORPH_OPEN, self._kernel)

        # bright targets against dark background
        _, mask_fwd = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        mask_fwd = cv2.morphologyEx(mask_fwd, cv2.MORPH_OPEN, self._kernel)

        boxes = _contours_to_boxes(mask_inv, 0.6) + _contours_to_boxes(mask_fwd, 0.5)
        boxes = _nms(boxes, iou_thresh=0.4)
        return boxes[:80]
