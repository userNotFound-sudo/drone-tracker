"""
Simple landmark-based altitude estimator.

Math
----
Given a camera with vertical FOV V degrees and frame height H pixels:

  deg_per_px  = V / H
  theta(y)    = (y_horiz - y) * deg_per_px          # elevation angle, deg

y_horiz is the pixel row that maps to 0° elevation (the horizon).
For a perfectly level camera y_horiz = H/2.

A landmark at (pixel_y_lm, elevation_m, distance_m) lets us calibrate tilt:

  alpha_lm   = degrees( arctan( (elev_lm - h_cam) / d_lm ) )
  y_horiz    = pixel_y_lm - alpha_lm / deg_per_px

Altitude of a tracked target:

  range_m    = range_u * max_range_m
  altitude_m = h_cam + range_m * sin( radians(theta) )

Clamped to >= 0 m (below-ground results are physically meaningless).
"""

import json
import math
import pathlib

CONFIG_PATH = pathlib.Path(__file__).parent.parent / "data" / "altitude_config.json"

_LAT_RANGE   = (-90.0, 90.0)
_LON_RANGE   = (-180.0, 180.0)
_HEIGHT_RANGE = (0.0, 9000.0)
_FOV_RANGE   = (10.0, 170.0)
_ELEV_RANGE  = (0.0, 9000.0)
_DIST_RANGE  = (1.0, 500_000.0)


def _in(value, lo, hi, name):
    if not (lo <= value <= hi):
        raise ValueError(f"{name} = {value} is outside [{lo}, {hi}]")


def load_config(path=CONFIG_PATH):
    with open(path) as f:
        cfg = json.load(f)

    _in(cfg["camera_lat"],    *_LAT_RANGE,    "camera_lat")
    _in(cfg["camera_lon"],    *_LON_RANGE,    "camera_lon")
    _in(cfg["camera_height_m"], *_HEIGHT_RANGE, "camera_height_m")
    _in(cfg["v_fov_deg"],     *_FOV_RANGE,    "v_fov_deg")

    for lm in cfg.get("landmarks", []):
        _in(lm["elevation_m"], *_ELEV_RANGE, f"landmark '{lm.get('name','?')}' elevation_m")
        if "distance_m" in lm:
            _in(lm["distance_m"], *_DIST_RANGE, f"landmark '{lm.get('name','?')}' distance_m")

    return cfg


class AltitudeEstimator:
    def __init__(self, cfg, frame_h: int):
        self._h_cam    = cfg["camera_height_m"]
        self._v_fov    = cfg["v_fov_deg"]
        self._max_range = cfg.get("max_range_m", 1000.0)
        self._deg_per_px = self._v_fov / frame_h

        # default: assume camera is level
        self._y_horiz = frame_h / 2.0

        landmarks = cfg.get("landmarks", [])
        if landmarks:
            lm = landmarks[0]          # use first landmark only
            if "distance_m" in lm:
                dh = lm["elevation_m"] - self._h_cam
                alpha_lm = math.degrees(math.atan(dh / lm["distance_m"]))
                self._y_horiz = lm["pixel_y"] - alpha_lm / self._deg_per_px

    def estimate(self, cy: float, range_u: float) -> float:
        """Return altitude in metres above ground for a track at pixel cy."""
        theta_deg = (self._y_horiz - cy) * self._deg_per_px
        range_m   = range_u * self._max_range
        alt = self._h_cam + range_m * math.sin(math.radians(theta_deg))
        return round(max(0.0, alt), 1)
