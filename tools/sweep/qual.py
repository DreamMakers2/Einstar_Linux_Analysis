"""Per-frame image quality metrics for a captured sweep.

The metrics are chosen so that a *sweep* can be judged, not a single frame:

``mean``/``median``       sensor level (drifts with illumination/exposure)
``sat_frac``              fraction of pixels >= 250 (highlight clipping)
``zero_frac``             fraction of pixels == 0 (black clipping / no signal)
``contrast``              p99 - p1 over the whole frame (global contrast)
``blur_lapvar``           variance of the Laplacian over the whole frame
``blur_lapvar_norm``      the same divided by the frame intensity variance, so
                          the number does not move with exposure/contrast
``marker_sharp_px_inv``   mean over detected markers of the 95th percentile
                          gradient magnitude in a small marker window, divided
                          by the local marker contrast.  Units are 1/px: an edge
                          blurred over N pixels scores about 1/N, so the value
                          is directly an inverse blur width.
``blur_lapvar_roi``/``_norm``  the Laplacian variance over the plate bounding box
                          (what ``calib.guidance.focus_metric`` measures), for a
                          direct comparison with its ``min_focus`` thresholds
``plate_area_frac``       convex hull of the detected markers / image area

``blur_lapvar`` alone is not exposure invariant, which matters here: the sweep
shows a slow illumination drift, so the *normalised* variant and the
marker-edge sharpness are the metrics that can be compared across the sweep.
The colour stream is a raw Bayer mosaic (``bayer_raw: true``); its Laplacian
variance is dominated by the colour-filter checkerboard and is **not**
comparable with the IR numbers, so callers must not pool them.
"""

from __future__ import annotations

import cv2
import numpy as np

SATURATION_LEVEL = 250


def _gradient_magnitude(gray: np.ndarray) -> np.ndarray:
    """|grad I| in intensity per pixel (central differences, no Sobel gain)."""
    g = gray.astype(np.float32)
    gy, gx = np.gradient(g)
    return cv2.magnitude(gx, gy)


def marker_sharpness(gray: np.ndarray, points: np.ndarray,
                     pitch_px: float, n_max: int = 40) -> float:
    """Mean normalised marker-edge gradient (1/px)."""
    if points is None or len(points) == 0 or pitch_px <= 0:
        return float("nan")
    mag = _gradient_magnitude(gray)
    h, w = gray.shape[:2]
    r = int(np.clip(round(0.35 * pitch_px), 4, 24))
    vals = []
    for x, y in np.asarray(points, float)[:n_max]:
        xi, yi = int(round(x)), int(round(y))
        x0, x1 = max(0, xi - r), min(w, xi + r + 1)
        y0, y1 = max(0, yi - r), min(h, yi + r + 1)
        if x1 - x0 < 5 or y1 - y0 < 5:
            continue
        patch = gray[y0:y1, x0:x1].astype(np.float32)
        lo, hi = np.percentile(patch, [10, 95])
        contrast = float(hi - lo)
        if contrast < 6.0:
            continue
        vals.append(float(np.percentile(mag[y0:y1, x0:x1], 95)) / contrast)
    return float(np.mean(vals)) if vals else float("nan")


def image_quality(gray: np.ndarray, points: np.ndarray | None = None,
                  pitch_px: float = 81.0, bayer_raw: bool = False) -> dict:
    """Global + marker-local quality metrics for one 8-bit frame."""
    g = gray
    f = g.astype(np.float32)
    p1, p99 = np.percentile(g, [1, 99])
    var = float(f.var())
    lap = cv2.Laplacian(f, cv2.CV_32F)
    lapvar = float(lap.var())
    out = {
        "mean": round(float(g.mean()), 4),
        "median": round(float(np.median(g)), 2),
        "std": round(float(g.std()), 4),
        "p1": round(float(p1), 2),
        "p99": round(float(p99), 2),
        "contrast": round(float(p99 - p1), 2),
        "sat_frac": round(float(np.mean(g >= SATURATION_LEVEL)), 6),
        "zero_frac": round(float(np.mean(g == 0)), 6),
        "blur_lapvar": round(lapvar, 2),
        "blur_lapvar_norm": round(lapvar / var, 4) if var > 1e-9 else None,
        "bayer_raw": bool(bayer_raw),
    }
    if bayer_raw:
        out["marker_sharp_px_inv"] = None
        out["plate_area_frac"] = None
        return out
    out["marker_sharp_px_inv"] = (round(marker_sharpness(g, points, pitch_px), 5)
                                 if points is not None and len(points) else None)
    if points is not None and len(points):
        pt = np.asarray(points, float)
        pad = 24
        x0 = max(0, int(np.floor(pt[:, 0].min())) - pad)
        x1 = min(g.shape[1], int(np.ceil(pt[:, 0].max())) + pad)
        y0 = max(0, int(np.floor(pt[:, 1].min())) - pad)
        y1 = min(g.shape[0], int(np.ceil(pt[:, 1].max())) + pad)
        roi = f[y0:y1, x0:x1]
        rv = float(roi.var())
        out["blur_lapvar_roi"] = round(float(cv2.Laplacian(roi.astype(np.float32),
                                                          cv2.CV_32F).var()), 2)
        out["blur_lapvar_roi_norm"] = round(out["blur_lapvar_roi"] / rv, 4) if rv > 1e-9 else None
    else:
        out["blur_lapvar_roi"] = None
        out["blur_lapvar_roi_norm"] = None
    if points is not None and len(points) >= 4:
        hull = cv2.convexHull(np.asarray(points, np.float32).reshape(-1, 1, 2))
        out["plate_area_frac"] = round(
            float(cv2.contourArea(hull)) / float(g.shape[0] * g.shape[1]), 5)
    else:
        out["plate_area_frac"] = None
    return out
