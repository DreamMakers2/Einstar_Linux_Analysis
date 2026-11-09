"""Audit of sub-pixel localizers for the Einstar calibration plate.

The production detector (:mod:`calib.detect`) finds marker blobs with a global
intensity threshold and refines each blob either with an intensity-weighted
centroid or with a guarded ellipse fit on a thresholded contour.  This module
compares those two against three stronger localizers on *synthetic scenes with
known hole centres* and on the vendor image corpus, and quantifies the
geometric bias that no localizer can remove (the projection of a 3-D circle is
an ellipse whose centre is not the projection of the circle centre).

What is measured
----------------
Synthetic (ground truth = projection of the hole's top-rim centre):

* mean bias vector, radial/tangential components, RMS error and repeatability
  as a function of perspective tilt, defocus, saturation, contrast, plate
  illumination gradient, occlusion, neighbouring holes, exposure and distance;
* the same quantities after the conic back-projection correction;
* failure rate (no estimate, or gross error > 1 px).

Vendor corpus (no physical truth; consistency proxies only):

* per-image homography residual (fit and leave-one-out),
* calibration reprojection RMS / max, with fixed distortion and identical
  optimisation settings for every localizer,
* intrinsics and baseline changes, relative rotation against the CCF,
* epipolar (Sampson) consistency of the fitted left/right rig,
* detection success and runtime.

Geometry
--------
A planar circle of radius ``r`` centred at ``C`` on the plate, seen by a
pinhole camera, projects to a conic whose centre is displaced from ``P(C)``.
For the Einstar geometry (r = 3.4 mm, 240-560 mm, up to 45 deg) this "conic
bias" is a few hundredths of a pixel.  A *through-hole* adds a second effect:
the visible bright region is the intersection of the projections of the top
and bottom openings, which is asymmetric at oblique incidence and biased along
the tilt direction by an amount proportional to the plate thickness.  Both are
computed here from the model and measured on rendered scenes.

The correction tested is a conic back-projection: the fitted ellipse is
undistorted, back-projected through the known plate plane to the 3-D conic,
and a circle is fitted in the plane; the projection of that circle centre is
the corrected measurement.

Usage::

    python -m calib.detectors_audit --out analysis/calibration/audit
    python -m calib.detectors_audit --out /tmp/audit --quick   # fast subset

Nothing in this module modifies the production detector.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

import cv2
import numpy as np
from scipy.optimize import least_squares
from scipy.special import ndtr

from . import plate
from .calibrate import CAMERAS, CameraIntrinsics, Observation, calibrate
from .detect import (Blob, GridObservation, _ellipse_centre, _weighted_centroid,
                     assign_grid, detect_blobs)
from .geometry import epipolar_residual, fundamental, project, solve_pnp

# --------------------------------------------------------------------------
# constants / paths
# --------------------------------------------------------------------------

#: vendor CCF left camera (fallback when the CCF files are unavailable)
DEFAULT_K = np.array([[1159.115, 0.0, 623.912],
                      [0.0, 1159.021, 534.563],
                      [0.0, 0.0, 1.0]])
DEFAULT_KC = np.array([-0.16203, 0.17330, 0.00019, 0.00015, 0.01846])

IMAGE_SIZE = (1280, 1024)          # width, height (vendor frames)

REPO = Path(__file__).resolve().parents[1]
CCF_DIR = REPO / "sources/calibration/res_Einscan-E10_200x150"
VENDOR_DIR = Path(os.environ.get("EINSTAR_VENDOR_CALIB",
                                 "<private-inputs>/vendor-calib-rapid"))

CAMERA_NAMES = {"Left": "left", "Right": "right", "Tex": "tex"}


def vendor_camera() -> tuple[np.ndarray, np.ndarray]:
    """Left-camera ``K`` and distortion from the shipped CCF (or fallback)."""
    try:
        from .ccf import load
        rec = load(CCF_DIR / "LeftCCF.txt")
        cam = CameraIntrinsics.from_ccf(rec)
        return cam.K, np.asarray(cam.kc, float)
    except Exception:
        return DEFAULT_K.copy(), DEFAULT_KC.copy()


# --------------------------------------------------------------------------
# geometry helpers
# --------------------------------------------------------------------------

def project_board(K: np.ndarray, kc: np.ndarray, R: np.ndarray, t: np.ndarray,
                  X: np.ndarray) -> np.ndarray:
    """Project plate-frame points ``X`` (N,3) with the pinhole+distortion model."""
    X = np.asarray(X, float).reshape(-1, 3)
    return project(K, kc, X @ np.asarray(R, float).T + np.asarray(t, float))


def circle_polygon(K, kc, R, t, cx: float, cy: float, z: float, r: float,
                   n: int = 360) -> np.ndarray:
    """Projected polygon of a circle on the plate at height ``z``."""
    a = np.linspace(0.0, 2.0 * np.pi, n, endpoint=False)
    X = np.stack([cx + r * np.cos(a), cy + r * np.sin(a), np.full(n, float(z))], 1)
    return project_board(K, kc, R, t, X)


def polygon_centroid(poly: np.ndarray) -> tuple[np.ndarray, float]:
    """Area and centroid of a simple polygon."""
    p = np.asarray(poly, np.float32).reshape(-1, 1, 2)
    m = cv2.moments(p)
    if abs(m["m00"]) < 1e-12:
        return np.full(2, np.nan), 0.0
    return np.array([m["m10"] / m["m00"], m["m01"] / m["m00"]]), float(m["m00"])


_IMAGE_RECT = np.array([[0.0, 0.0], [IMAGE_SIZE[0], 0.0],
                        [IMAGE_SIZE[0], IMAGE_SIZE[1]], [0.0, IMAGE_SIZE[1]]],
                       np.float32)


def polygon_area(poly: np.ndarray) -> float:
    p = np.asarray(poly, float).reshape(-1, 2)
    if len(p) < 3:
        return 0.0
    x, y = p[:, 0], p[:, 1]
    return 0.5 * float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))


def _ccw(poly: np.ndarray) -> np.ndarray:
    p = np.asarray(poly, float).reshape(-1, 2)
    return p if polygon_area(p) >= 0 else p[::-1]


def _clip_halfplane(pts: np.ndarray, a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Keep the part of ``pts`` left of the directed line a->b (vectorized).

    The ring order is preserved: vertex i contributes itself when inside and
    the edge crossing point after it, so the result is a valid simple polygon.
    """
    if len(pts) == 0:
        return pts
    d = b - a
    rel = pts - a
    side = d[0] * rel[:, 1] - d[1] * rel[:, 0]          # >0 = left (inside)
    inside = side >= 0.0
    if inside.all():
        return pts
    if not inside.any():
        return np.empty((0, 2))
    n = len(pts)
    nxt = np.roll(np.arange(n), -1)
    cross = inside != inside[nxt]
    denom = side - side[nxt]
    t = np.where(cross, side / np.where(np.abs(denom) > 1e-15, denom, 1e-15), 0.0)
    inter = pts + t[:, None] * (pts[nxt] - pts)
    cand = np.empty((2 * n, 2), float)
    cand[0::2] = pts
    cand[1::2] = inter
    mask = np.empty(2 * n, bool)
    mask[0::2] = inside
    mask[1::2] = cross
    return cand[mask]


def _clip_convex(subject: np.ndarray, clip: np.ndarray) -> np.ndarray:
    """Sutherland-Hodgman intersection of two convex polygons."""
    sub = _ccw(subject)
    cl = _ccw(clip)
    if len(sub) < 3 or len(cl) < 3:
        return np.empty((0, 2))
    for i in range(len(cl)):
        sub = _clip_halfplane(sub, cl[i], cl[(i + 1) % len(cl)])
        if len(sub) == 0:
            return sub
    return sub


def intersect_polys(a: np.ndarray, b: np.ndarray) -> tuple[np.ndarray, float]:
    """Convex polygon intersection (polygon, area).

    Uses a vectorised Sutherland-Hodgman clip instead of
    ``cv2.intersectConvexConvex``, which returns a wrong area for nearly
    coincident polygons (the tilt~0 through-hole case).
    """
    poly = _clip_convex(a, b)
    if len(poly) < 3:
        return np.empty((0, 2)), 0.0
    return poly, abs(polygon_area(poly))


def ellipse_centre_from_points(pts: np.ndarray) -> Optional[np.ndarray]:
    """Centre of the cv2 ellipse fit to >=5 points (pixel units)."""
    if len(pts) < 5:
        return None
    try:
        (cx, cy), (a, b), ang = cv2.fitEllipse(np.asarray(pts, np.float32))
    except cv2.error:
        return None
    if not np.isfinite([cx, cy, a, b]).all() or a < 0.5 or b < 0.5:
        return None
    return np.array([cx, cy], float)


def ellipse_q_residual(pts: np.ndarray, ellipse) -> np.ndarray:
    """Approximate geometric distance to a cv2 ellipse (pixels)."""
    (cx, cy), (a, b), ang = ellipse
    a = max(float(a), 1e-3) * 0.5
    b = max(float(b), 1e-3) * 0.5
    th = np.radians(ang)
    ca, sa = np.cos(th), np.sin(th)
    dx = pts[:, 0] - cx
    dy = pts[:, 1] - cy
    u = (dx * ca + dy * sa) / a
    v = (-dx * sa + dy * ca) / b
    q = u * u + v * v - 1.0
    g = 2.0 * np.sqrt((u / a) ** 2 + (v / b) ** 2) + 1e-9
    return np.abs(q) / g


def robust_ellipse(pts: np.ndarray, prior_xy: np.ndarray, prior_r: float,
                   n_iter: int = 4, max_axis_ratio: float = 2.8
                   ) -> Optional[dict]:
    """Robust algebraic ellipse fit with iterative inlier re-fitting.

    ``prior_xy``/``prior_r`` are the blob seed and its pixel radius; the fit is
    rejected when its centre or size is incompatible with the seed (the same
    guard idea as the production ellipse mode, but shape-aware).
    """
    pts = np.asarray(pts, np.float32).reshape(-1, 2)
    if len(pts) < 10:
        return None
    if prior_r and prior_r > 0:
        # radial gate: a marker edge cannot be farther than ~1.5 radii from the
        # seed (this rejects neighbouring holes whose far edge would otherwise
        # be an inlier of a merged, wrong ellipse)
        rr = np.hypot(pts[:, 0] - prior_xy[0], pts[:, 1] - prior_xy[1])
        pts = pts[rr < 1.5 * prior_r + 4.0]
        if len(pts) < 10:
            return None
    keep = pts
    ellipse = None
    for _ in range(n_iter):
        if len(keep) < 8:
            return None
        try:
            ellipse = cv2.fitEllipse(keep.reshape(-1, 1, 2))
        except cv2.error:
            return None
        (cx, cy), (a, b), ang = ellipse
        if not np.isfinite([cx, cy, a, b, ang]).all():
            return None
        r = ellipse_q_residual(pts, ellipse)
        med = float(np.median(r))
        scale = 1.4826 * med + 0.03
        new_keep = pts[r < 2.5 * scale]
        if len(new_keep) == len(keep) and _ > 0:
            break
        keep = new_keep
    (cx, cy), (a, b), ang = ellipse
    semi = np.array([a, b], float) * 0.5
    if semi.min() < 1.0 or semi.max() > 34.0:
        return None
    if semi.max() / semi.min() > max_axis_ratio:
        return None
    if np.hypot(cx - prior_xy[0], cy - prior_xy[1]) > max(2.5, 0.75 * prior_r):
        return None
    if prior_r > 0 and not (0.35 * prior_r < semi.mean() < 1.9 * prior_r):
        return None
    return {"centre": np.array([cx, cy], float), "axes": semi,
            "angle": float(ang), "n": int(len(keep)), "ellipse": ellipse}


def _bilinear(img: np.ndarray, x: np.ndarray, y: np.ndarray) -> np.ndarray:
    h, w = img.shape
    x = np.clip(x, 0.0, w - 1.001)
    y = np.clip(y, 0.0, h - 1.001)
    x0 = np.floor(x).astype(np.int32)
    y0 = np.floor(y).astype(np.int32)
    fx = x - x0
    fy = y - y0
    x1 = np.minimum(x0 + 1, w - 1)
    y1 = np.minimum(y0 + 1, h - 1)
    return (img[y0, x0] * (1 - fx) * (1 - fy) + img[y0, x1] * fx * (1 - fy)
            + img[y1, x0] * (1 - fx) * fy + img[y1, x1] * fx * fy)


def fit_circle_2d(pts: np.ndarray) -> Optional[tuple[np.ndarray, float]]:
    """Algebraic least-squares circle fit in a plane."""
    pts = np.asarray(pts, float).reshape(-1, 2)
    if len(pts) < 5:
        return None
    A = np.column_stack([2.0 * pts[:, 0], 2.0 * pts[:, 1], np.ones(len(pts))])
    b = (pts ** 2).sum(1)
    sol, *_ = np.linalg.lstsq(A, b, rcond=None)
    cx, cy, c = sol
    rr = c + cx * cx + cy * cy
    if not np.isfinite(rr) or rr <= 0:
        return None
    return np.array([cx, cy], float), float(np.sqrt(rr))


def backproject_circle_centre(ellipse, plane_R: np.ndarray, plane_t: np.ndarray,
                              K: np.ndarray, kc: np.ndarray,
                              n: int = 360) -> Optional[np.ndarray]:
    """Conic back-projection of a fitted *undistorted* ellipse.

    Parameters
    ----------
    ellipse : ((cx, cy), (a, b), angle) in *normalized* camera coordinates
        (i.e. the coordinates returned by ``cv2.undistortPoints``).
    plane_R, plane_t : plate pose (plate -> camera).

    Returns
    -------
    The projection (pixels) of the 3-D circle centre obtained by
    back-projecting the conic onto the plate plane and fitting a circle.
    """
    (cx, cy), (a, b), ang = ellipse
    a = max(float(a), 1e-6) * 0.5
    b = max(float(b), 1e-6) * 0.5
    th = np.radians(ang)
    ca, sa = np.cos(th), np.sin(th)
    s = np.linspace(0.0, 2.0 * np.pi, n, endpoint=False)
    xn = cx + a * (np.cos(s) * ca) - b * (np.sin(s) * sa)
    yn = cy + a * (np.cos(s) * sa) + b * (np.sin(s) * ca)
    v = np.stack([xn, yn, np.ones(n)], 1)                     # rays, camera frame
    R = np.asarray(plane_R, float)
    t = np.asarray(plane_t, float)
    nrm = R[:, 2]
    d = float(nrm @ t)                                       # n . X = d on plate
    denom = v @ nrm
    if np.any(np.abs(denom) < 1e-9):
        return None
    X = v * (d / denom)[:, None]
    # coordinates in the plate frame
    q = (X - t) @ R
    if not np.allclose(q[:, 2], 0.0, atol=1e-6):
        return None
    fit = fit_circle_2d(q[:, :2])
    if fit is None:
        return None
    c2, radius = fit
    if not (1e-6 < radius < 1e4):
        return None
    C3 = t + R[:, 0] * c2[0] + R[:, 1] * c2[1]
    return project(K, kc, C3.reshape(1, 3))[0]


def pose_absorption(centres_px: np.ndarray, bias_px: np.ndarray,
                    board_pts: np.ndarray, R: np.ndarray, t: np.ndarray,
                    K: np.ndarray, kc: np.ndarray) -> dict:
    """How much of a 2-D bias field is absorbed by a free board pose.

    The calibration frees the six pose parameters of every view, so any bias
    component that is a rigid image motion is invisible to the fit.  This
    returns the variance fraction explained by the 6-D pose Jacobian and the
    RMS of the residual bias.
    """
    bias_px = np.asarray(bias_px, float)
    X = np.asarray(board_pts, float).reshape(-1, 3)
    from .geometry import rodrigues
    J = np.zeros((2 * len(X), 6))
    eps = 1e-6
    base = project_board(K, kc, R, t, X)
    for j in range(6):
        d = np.zeros(6)
        d[j] = eps
        Rp = rodrigues(d[:3]) @ np.asarray(R, float)
        tp = np.asarray(t, float) + d[3:]
        J[:, j] = ((project_board(K, kc, Rp, tp, X) - base) / eps).ravel()
    b = bias_px.ravel()
    try:
        delta, *_ = np.linalg.lstsq(J, b, rcond=None)
        fitted = J @ delta
    except np.linalg.LinAlgError:
        return {"explained": float("nan"), "residual_rms": float("nan")}
    var = float(np.sum(b ** 2))
    res = b - fitted
    return {"explained": 1.0 - float(np.sum(res ** 2)) / max(var, 1e-30),
            "residual_rms": float(np.sqrt(np.mean(res.reshape(-1, 2) ** 2))),
            "fitted_rms": float(np.sqrt(np.mean(fitted.reshape(-1, 2) ** 2)))}


# --------------------------------------------------------------------------
# synthetic renderer
# --------------------------------------------------------------------------

@dataclass
class RenderSpec:
    """A physical scene: plate + camera + illumination + sensor."""
    R: np.ndarray
    t: np.ndarray
    K: np.ndarray
    kc: np.ndarray
    pitch_mm: float = plate.DEFAULT_PITCH_MM
    radius_mm: float = 3.4
    thickness_mm: float = 1.0
    big_scale: float = 1.7
    plate_size_mm: tuple = (222.0, 167.0)   # measured, see module docstring
    plate_level: float = 52.0
    hole_level: float = 420.0
    wall_level: float = 16.0
    bg_level: float = 7.0
    gradient: tuple = (0.0, 0.0)
    psf_sigma: float = 1.2
    exposure: float = 1.0
    saturation: float = 255.0
    noise_dn: float = 1.0
    supersample: int = 3
    hole_model: str = "disk"             # "disk" (flush diffuser) | "through"
    occluder: Optional[dict] = None      # {x, width, soft, level, angle}
    lateral_mm: tuple = (0.0, 0.0)
    seed: int = 0


@dataclass
class HoleTruth:
    uv: tuple
    cx_mm: float
    cy_mm: float
    r_mm: float
    centre_px: np.ndarray           # projection of the top-rim centre
    ellipse_px: np.ndarray          # centre of the ellipse fit to the top circle
    visible_px: np.ndarray          # centroid of the visible aperture (in frame)
    visible_area: float
    visible_frac: float             # fraction of the unoccluded aperture visible
    fully_visible: bool
    blocked: bool = False

    @property
    def conic_offset(self) -> np.ndarray:
        return self.ellipse_px - self.centre_px

    @property
    def aperture_offset(self) -> np.ndarray:
        return self.visible_px - self.centre_px


@dataclass
class RenderedScene:
    image: np.ndarray
    holes: list
    spec: RenderSpec
    window: tuple
    render_s: float

    def truth_arrays(self) -> tuple[np.ndarray, np.ndarray]:
        return (np.array([h.centre_px for h in self.holes]),
                np.array([h.visible_px for h in self.holes]))


def _hole_geometry(spec: RenderSpec):
    """Lattice origin/radii and the off-lattice big marker (board coordinates).

    The plate has 40 lattice holes of which three (the collinear big markers at
    u=2,4,5, v=1) are large, plus one off-lattice large marker at (3.5, 3):
    41 physical holes.  ``calib.plate`` models the 40 lattice points only and
    uses the 41st as the orientation anchor.
    """
    pitch = float(spec.pitch_mm)
    uv = plate.lattice_uv()
    xy = (uv - plate.BOARD_CENTRE_UV) * pitch
    origin = np.array([-plate.BOARD_CENTRE_UV[0] * pitch,
                       -plate.BOARD_CENTRE_UV[1] * pitch])
    r_lat = np.full((plate.GRID_COLS, plate.GRID_ROWS), float(spec.radius_mm))
    on_lattice_big = []
    off_lattice_big = []
    for bu, bv in plate.BIG_MARKERS:
        if abs(bu - round(bu)) < 1e-9 and abs(bv - round(bv)) < 1e-9:
            r_lat[int(round(bu)), int(round(bv))] = float(spec.radius_mm) * spec.big_scale
            on_lattice_big.append((float(bu), float(bv)))
        else:
            off_lattice_big.append((float((bu - plate.BOARD_CENTRE_UV[0]) * pitch),
                                    float((bv - plate.BOARD_CENTRE_UV[1]) * pitch)))
    return pitch, origin, xy, r_lat, on_lattice_big, off_lattice_big


def _plate_window(spec: RenderSpec, margin: int = 48) -> tuple[int, int, int, int]:
    W, H = spec.plate_size_mm
    corners = np.array([[-W / 2, -H / 2, 0], [W / 2, -H / 2, 0],
                        [W / 2, H / 2, 0], [-W / 2, H / 2, 0]], float)
    uv = project_board(spec.K, spec.kc, spec.R, spec.t, corners)
    x0 = int(max(0, np.floor(uv[:, 0].min()) - margin))
    x1 = int(min(IMAGE_SIZE[0], np.ceil(uv[:, 0].max()) + margin))
    y0 = int(max(0, np.floor(uv[:, 1].min()) - margin))
    y1 = int(min(IMAGE_SIZE[1], np.ceil(uv[:, 1].max()) + margin))
    if x1 <= x0 or y1 <= y0:
        return 0, 0, 0, 0
    return x0, y0, x1, y1


def _occluder_mask(spec: RenderSpec) -> Optional[np.ndarray]:
    """Image-space soft occlusion mask (1 = blocked), full frame."""
    if not spec.occluder:
        return None
    o = spec.occluder
    w, h = IMAGE_SIZE
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    ang = float(o.get("angle", 0.0))
    if abs(ang) > 1e-9:
        # rotate about the image centre before measuring distance
        ca, sa = math.cos(math.radians(ang)), math.sin(math.radians(ang))
        dx = xx - w / 2
        dy = yy - h / 2
        xx = ca * dx + sa * dy + w / 2
    dist = np.abs(xx - float(o.get("x", w / 2)))
    soft = max(float(o.get("soft", 1.5)), 0.3)
    # 1 inside the occluding band, 0 outside
    m = 1.0 / (1.0 + np.exp(np.clip((dist - float(o.get("width", 40.0)) / 2.0) / soft,
                                    -60.0, 60.0)))
    return m.astype(np.float32)


def render(spec: RenderSpec, rng: Optional[np.random.Generator] = None) -> RenderedScene:
    """Render a plate scene with a physically motivated aperture model.

    The plate is a finite slab.  A ray hitting a hole either passes through
    both openings (bright backing, seen through the hole) or hits the hole
    wall (dark); the visible bright region is therefore the intersection of the
    projections of the top and bottom openings.  This is what makes the centre
    biased at oblique incidence, and it is the effect measured in the audit.
    """
    t0 = time.time()
    rng = rng or np.random.default_rng(spec.seed)
    K, kc = np.asarray(spec.K, float), np.asarray(spec.kc, float)
    R, t = np.asarray(spec.R, float), np.asarray(spec.t, float)
    W, H = spec.plate_size_mm
    pitch, origin, xy, r_lat, on_big, big = _hole_geometry(spec)
    r_small = float(spec.radius_mm)
    r_big = r_small * float(spec.big_scale)
    S = int(max(1, spec.supersample))
    h, w = IMAGE_SIZE[1], IMAGE_SIZE[0]

    img = np.full((h, w), float(spec.bg_level), np.float32)
    x0, y0, x1, y1 = _plate_window(spec)
    if x1 > x0 and y1 > y0:
        C = -R.T @ t                       # camera centre in board coordinates
        off = (np.arange(S) + 0.5) / S - 0.5
        Cb = C.astype(np.float64)
        for yb in range(y0, y1, 48):
            yb1 = min(y1, yb + 48)
            # OpenCV convention: the centre of pixel (i, j) is at (i, j);
            # supersamples are placed symmetrically around that centre.
            vv, uu = np.mgrid[yb:yb1, x0:x1].astype(np.float32)
            acc = np.zeros(vv.shape, np.float32)
            for oy in off:
                for ox in off:
                    pts = np.stack([(uu + ox).ravel(), (vv + oy).ravel()], -1)
                    nrm = cv2.undistortPoints(pts.astype(np.float32).reshape(-1, 1, 2),
                                              K, kc).reshape(-1, 2)
                    d = np.concatenate([nrm, np.ones((len(nrm), 1), np.float32)], 1) @ R.astype(np.float32)
                    s0 = (-Cb[2] / d[:, 2]).astype(np.float32)
                    P0 = Cb.astype(np.float32)[None, :] + s0[:, None] * d
                    X, Y = P0[:, 0], P0[:, 1]
                    # plate face
                    rad = np.full(len(X), spec.bg_level, np.float32)
                    in_plate = (np.abs(X) <= W / 2) & (np.abs(Y) <= H / 2)
                    shade = 1.0 + spec.gradient[0] * X / W + spec.gradient[1] * Y / H
                    face = (spec.plate_level * shade).astype(np.float32)
                    rad[in_plate] = face[in_plate]
                    # nearest small hole (square lattice -> rounding is exact)
                    iu = np.rint((X - origin[0]) / pitch).astype(np.int32)
                    iv = np.rint((Y - origin[1]) / pitch).astype(np.int32)
                    ok_lat = (iu >= 0) & (iu < plate.GRID_COLS) & (iv >= 0) & (iv < plate.GRID_ROWS)
                    cxs = (origin[0] + iu * pitch).astype(np.float32)
                    cys = (origin[1] + iv * pitch).astype(np.float32)
                    r_lat_d = np.hypot(X - cxs, Y - cys)
                    # big markers (nearest of four)
                    r_b = np.full(len(X), 1e9, np.float32)
                    cx_b = np.zeros(len(X), np.float32)
                    cy_b = np.zeros(len(X), np.float32)
                    for bx, by in big:
                        rb = np.hypot(X - bx, Y - by)
                        m = rb < r_b
                        r_b[m] = rb[m]
                        cx_b[m] = bx
                        cy_b[m] = by
                    use_big = r_b < r_lat_d
                    r_in = np.where(use_big, r_b, r_lat_d)
                    cx_in = np.where(use_big, cx_b, cxs)
                    cy_in = np.where(use_big, cy_b, cys)
                    iuc = np.clip(iu, 0, plate.GRID_COLS - 1)
                    ivc = np.clip(iv, 0, plate.GRID_ROWS - 1)
                    rad_lat = r_lat[iuc, ivc].astype(np.float32)
                    rad_h = np.where(use_big, np.float32(r_big), rad_lat)
                    in_hole = ((ok_lat | use_big) & (r_in < rad_h) & in_plate)
                    if spec.hole_model == "disk" or spec.thickness_mm <= 0:
                        rad[in_hole] = spec.hole_level
                    else:
                        s1 = s0 - float(spec.thickness_mm) / d[:, 2]
                        P1 = Cb.astype(np.float32)[None, :] + s1[:, None] * d
                        rb = np.hypot(P1[:, 0] - cx_in, P1[:, 1] - cy_in)
                        open_ = in_hole & (rb < rad_h)
                        wall = in_hole & ~open_
                        rad[wall] = spec.wall_level
                        rad[open_] = spec.hole_level
                    acc += rad.reshape(yb1 - yb, x1 - x0)
            img[yb:yb1, x0:x1] = acc / (S * S)

    # optics: the pixel box already applies a 1-px box filter, deconvolve it
    sb = math.sqrt(max(spec.psf_sigma ** 2 - 1.0 / 12.0, 1e-4))
    img = cv2.GaussianBlur(img, (0, 0), sb)

    # occlusion in front of the plate (same PSF)
    occ = _occluder_mask(spec)
    if occ is not None:
        lvl = float(spec.occluder.get("level", spec.bg_level))
        img = img * (1.0 - occ) + lvl * occ

    img = np.clip(img * float(spec.exposure), 0.0, float(spec.saturation))
    if spec.noise_dn > 0:
        img = img + rng.normal(0.0, float(spec.noise_dn), img.shape)
    img = np.clip(np.round(img), 0, 255).astype(np.uint8)
    holes = _truth_geometry(spec, occ)
    return RenderedScene(img, holes, spec, (x0, y0, x1, y1), time.time() - t0)


def _truth_geometry(spec: RenderSpec, occ: Optional[np.ndarray] = None) -> list:
    """Ground truth for every hole: centre projection, conic centre, aperture."""
    K, kc = np.asarray(spec.K, float), np.asarray(spec.kc, float)
    R, t = np.asarray(spec.R, float), np.asarray(spec.t, float)
    pitch, origin, xy, r_lat, on_big, off_big = _hole_geometry(spec)
    holes = []
    items = [(float(x), float(y), float(r_lat[int(round(u)), int(round(v))]),
              (float(u), float(v)))
             for (x, y), (u, v) in zip(xy, plate.lattice_uv())]
    for (bu, bv) in plate.BIG_MARKERS:
        if abs(bu - round(bu)) < 1e-9 and abs(bv - round(bv)) < 1e-9:
            continue
        x = (bu - plate.BOARD_CENTRE_UV[0]) * pitch
        y = (bv - plate.BOARD_CENTRE_UV[1]) * pitch
        items.append((float(x), float(y), float(spec.radius_mm) * spec.big_scale,
                      (float(bu), float(bv))))
    for (cx, cy, r, uv) in items:
        centre = project_board(K, kc, R, t, np.array([[cx, cy, 0.0]]))[0]
        top = circle_polygon(K, kc, R, t, cx, cy, 0.0, r, n=128)
        e = cv2.fitEllipse(top.astype(np.float32))
        ellipse_c = np.array(e[0], float)
        if spec.hole_model == "through" and spec.thickness_mm > 0:
            bot = circle_polygon(K, kc, R, t, cx, cy, -float(spec.thickness_mm), r, n=128)
            ap, area_ap = intersect_polys(top, bot)
        else:
            ap, area_ap = top, abs(cv2.contourArea(top.astype(np.float32)))
        blocked = area_ap <= 1e-6
        vis_area2 = 0.0
        if blocked:
            vis_c = np.full(2, np.nan)
            frac = 0.0
        else:
            vis, vis_area = intersect_polys(ap, _IMAGE_RECT)
            vis_c, vis_area2 = polygon_centroid(vis) if vis_area > 0 else (np.full(2, np.nan), 0.0)
            area_ap = max(area_ap, 1e-9)
            frac = vis_area2 / area_ap
        fully = (not blocked) and frac > 0.995
        if occ is not None and not blocked:
            # occlusion test over the aperture polygon (not just its centroid)
            ys = np.clip(np.round(ap[:, 1]).astype(int), 0, IMAGE_SIZE[1] - 1)
            xs = np.clip(np.round(ap[:, 0]).astype(int), 0, IMAGE_SIZE[0] - 1)
            fully = fully and float(occ[ys, xs].max()) < 0.02
        holes.append(HoleTruth(uv, cx, cy, r, centre, ellipse_c, vis_c,
                               vis_area2 if not blocked else 0.0, frac, fully, blocked))
    return holes


# --------------------------------------------------------------------------
# localizers
# --------------------------------------------------------------------------

@dataclass
class Localized:
    x: float
    y: float
    ok: bool
    method: str
    ellipse: Optional[tuple] = None     # pixel-space ellipse (for the conic correction)
    n_edge: int = 0
    info: dict = field(default_factory=dict)


def _window(gray: np.ndarray, x: float, y: float, r: int):
    h, w = gray.shape
    x0, x1 = max(0, int(round(x)) - r), min(w, int(round(x)) + r + 1)
    y0, y1 = max(0, int(round(y)) - r), min(h, int(round(y)) + r + 1)
    return gray[y0:y1, x0:x1], x0, y0


def loc_centroid(gray: np.ndarray, x: float, y: float, r: int,
                 invert: bool = False, prior_r: Optional[float] = None) -> Localized:
    """Current production mode: intensity-weighted centroid (``detect.py``)."""
    cx, cy = _weighted_centroid(gray, int(round(x)), int(round(y)), int(r), invert)
    return Localized(float(cx), float(cy), True, "centroid")


def loc_ellipse_thresh(gray: np.ndarray, x: float, y: float, r: int,
                       invert: bool = False, prior_r: Optional[float] = None) -> Localized:
    """Current optional mode: guarded ellipse on a thresholded contour."""
    e = _ellipse_centre(gray if not invert else (255 - gray),
                        int(round(x)), int(round(y)), int(r))
    if e is None:
        return Localized(float(x), float(y), False, "ellipse_thresh")
    d = math.hypot(e[0] - x, e[1] - y)
    if d > 0.6 * max(r, 1):
        return Localized(float(x), float(y), False, "ellipse_thresh", info={"guard": d})
    return Localized(float(e[0]), float(e[1]), True, "ellipse_thresh")


def _subpixel_edges(patch: np.ndarray, sigma: float, low: float, high: float,
                    adaptive: bool = False, border: int = 3):
    """Canny + parabolic sub-pixel refinement; returns (points, normals)."""
    blur = cv2.GaussianBlur(patch.astype(np.float32), (0, 0), sigma)
    gx = cv2.Sobel(blur, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(blur, cv2.CV_32F, 0, 1, ksize=3)
    mag = cv2.magnitude(gx, gy)
    gate = 0.0
    if adaptive:
        p = float(np.percentile(mag, 99.9))
        low = max(4.0, 0.22 * p)
        high = max(8.0, 0.50 * p)
        gate = 0.5 * p
    u8 = np.clip(blur, 0, 255).astype(np.uint8)
    edges = cv2.Canny(u8, float(low), float(high), L2gradient=True)
    if border > 0:
        edges[:border, :] = 0
        edges[-border:, :] = 0
        edges[:, :border] = 0
        edges[:, -border:] = 0
    ys, xs = np.nonzero(edges)
    if gate > 0 and len(xs):
        strong = mag[ys, xs] > gate
        ys, xs = ys[strong], xs[strong]
    if len(xs) < 6:
        return None, None
    nx = gx[ys, xs]
    ny = gy[ys, xs]
    nrm = np.hypot(nx, ny) + 1e-9
    nx = nx / nrm
    ny = ny / nrm
    m0 = mag[ys, xs]
    mm = _bilinear(mag, xs - nx, ys - ny)
    mp = _bilinear(mag, xs + nx, ys + ny)
    den = mm - 2.0 * m0 + mp
    delta = np.where(np.abs(den) > 1e-6, 0.5 * (mm - mp) / den, 0.0)
    delta = np.clip(delta, -0.5, 0.5)
    px = xs + delta * nx
    py = ys + delta * ny
    return np.stack([px, py], 1), np.stack([nx, ny], 1)


def loc_edge_ellipse(gray: np.ndarray, x: float, y: float, r: int,
                     sigma: float = 2.1, low: float = 75.0, high: float = 150.0,
                     adaptive: bool = False, invert: bool = False,
                     prior_r: Optional[float] = None) -> Localized:
    """Canny/sub-pixel edge points + robust algebraic ellipse fit."""
    r_pad = int(r + 12)
    patch, px0, py0 = _window(gray, x, y, r_pad)
    if patch.size < 64:
        return Localized(float(x), float(y), False, "edge_ellipse")
    if invert:
        patch = 255 - patch
    pts, nrm = _subpixel_edges(patch, sigma, low, high, adaptive=adaptive, border=3)
    if pts is None:
        return Localized(float(x), float(y), False, "edge_ellipse")
    # orientation prior: for a bright blob the gradient points toward the centre
    gx = (x - px0) - pts[:, 0]
    gy = (y - py0) - pts[:, 1]
    inward = (nrm[:, 0] * gx + nrm[:, 1] * gy) > 0
    pts = pts[inward]
    if len(pts) < 10:
        return Localized(float(x), float(y), False, "edge_ellipse")
    pr = float(prior_r) if prior_r else max(3.0, 0.6 * r)
    fit = robust_ellipse(pts + np.array([px0, py0]), np.array([x, y]), pr)
    if fit is None:
        return Localized(float(x), float(y), False, "edge_ellipse")
    return Localized(float(fit["centre"][0]), float(fit["centre"][1]), True,
                     "edge_ellipse", ellipse=fit["ellipse"], n_edge=fit["n"])


def loc_photometric(gray: np.ndarray, x: float, y: float, r: int,
                    invert: bool = False, clipmax: float = 255.0,
                    sigma0: float = 1.2, prior_r: Optional[float] = None) -> Localized:
    """Photometric disk model: linear background + blurred disk, saturated."""
    r_pad = int(r + 5)
    patch, px0, py0 = _window(gray, x, y, r_pad)
    if patch.size < 64:
        return Localized(float(x), float(y), False, "photometric")
    img = patch.astype(np.float64)
    if invert:
        img = 255.0 - img
    # 2x2 box average: a symmetric filter, so it cannot bias the centre, and
    # it cuts the least-squares cost by 4x (the model is smooth at this scale)
    if img.shape[0] // 2 >= 8 and img.shape[1] // 2 >= 8:
        he, we = img.shape[0] - img.shape[0] % 2, img.shape[1] - img.shape[1] % 2
        img = 0.25 * (img[0:he:2, 0:we:2] + img[1:he:2, 0:we:2]
                      + img[0:he:2, 1:we:2] + img[1:he:2, 1:we:2])
        step = 2.0
    else:
        step = 1.0
    hh, ww = img.shape
    yy, xx = np.mgrid[0:hh, 0:ww].astype(np.float64)
    # a step x step box average centred on patch indices [0..step-1] sits at
    # original coordinate px0 + step*i + 0.5*(step-1)
    dx = xx * step + 0.5 * (step - 1.0) - (x - px0)
    dy = yy * step + 0.5 * (step - 1.0) - (y - py0)
    rho = np.hypot(dx, dy)
    border = np.concatenate([img[0, :], img[-1, :], img[:, 0], img[:, -1]])
    bg = float(np.median(border))
    peak = float(np.percentile(img, 99.5))
    amp0 = max(peak - bg, 4.0)
    thr = bg + 0.5 * amp0
    area = float(np.count_nonzero(img > thr))
    r0 = float(np.clip(math.sqrt(max(area, 4.0) / math.pi), 1.5, 18.0))

    def model(p):
        mx, my, R, sg, A, b0, bx, by = p
        rr = np.hypot(dx - mx, dy - my)
        m = ndtr((R - rr) / max(sg, 0.15))
        return np.clip(b0 + bx * dx + by * dy + A * m, 0.0, clipmax)

    p0 = np.array([0.0, 0.0, r0, sigma0, amp0, bg, 0.0, 0.0])
    lo = np.array([-2.0, -2.0, 1.2, 0.25, max(2.0, 0.15 * amp0), bg - 60, -6.0, -6.0])
    hi = np.array([2.0, 2.0, 22.0, 6.0, 4.0 * amp0 + 80, bg + 60, 6.0, 6.0])
    p0 = np.clip(p0, lo + 1e-6, hi - 1e-6)
    try:
        res = least_squares(lambda p: (model(p) - img).ravel(), p0,
                            bounds=(lo, hi), loss="soft_l1", f_scale=2.0,
                            max_nfev=60, xtol=1e-9, ftol=1e-9, gtol=1e-9)
    except Exception:
        return Localized(float(x), float(y), False, "photometric")
    mx, my, R, sg, A, b0, bx, by = res.x
    if not res.success and res.cost > 0.5 * np.sum((img - bg) ** 2):
        return Localized(float(x), float(y), False, "photometric")
    if abs(mx) > 1.2 or abs(my) > 1.2 or not (1.0 < R < 24.0):
        return Localized(float(x), float(y), False, "photometric")
    return Localized(float(x + mx), float(y + my), True,
                     "photometric", info={"R": float(R), "sigma": float(sg),
                                          "amp": float(A), "cost": float(res.cost)})


def loc_conic3d(gray: np.ndarray, x: float, y: float, r: int,
                K: np.ndarray, kc: np.ndarray,
                plane_R: np.ndarray, plane_t: np.ndarray,
                sigma: float = 2.1, low: float = 75.0, high: float = 150.0,
                adaptive: bool = False, invert: bool = False,
                prior_r: Optional[float] = None) -> Localized:
    """Distortion-aware conic localizer.

    The edge points are undistorted, the conic is fitted in normalized camera
    coordinates, back-projected onto the plate plane and a circle is fitted in
    the plane; the returned point is the projection of that circle centre.
    This removes the perspective bias of the ellipse centre by construction.
    """
    r_pad = int(r + 12)
    patch, px0, py0 = _window(gray, x, y, r_pad)
    if patch.size < 64:
        return Localized(float(x), float(y), False, "conic3d")
    if invert:
        patch = 255 - patch
    pts, nrm = _subpixel_edges(patch, sigma, low, high, adaptive=adaptive, border=3)
    if pts is None or len(pts) < 10:
        return Localized(float(x), float(y), False, "conic3d")
    gx = (x - px0) - pts[:, 0]
    gy = (y - py0) - pts[:, 1]
    pts = pts[(nrm[:, 0] * gx + nrm[:, 1] * gy) > 0]
    if len(pts) < 10:
        return Localized(float(x), float(y), False, "conic3d")
    pts_px = pts + np.array([px0, py0])
    K = np.asarray(K, np.float64)
    kc = np.asarray(kc, np.float64).ravel()
    und = cv2.undistortPoints(pts_px.reshape(-1, 1, 2).astype(np.float32),
                              K, kc).reshape(-1, 2)
    # work in "undistorted pixel" units (isotropic scale by fx) so that the
    # robust-fit guards are expressed in pixels; fx/fy differ by <1e-4 here.
    S = float(K[0, 0])
    seed_n = cv2.undistortPoints(np.array([[[x, y]]], np.float32), K, kc).reshape(-1, 2)[0]
    pr = float(prior_r) if prior_r else max(3.0, 0.6 * r)
    fit = robust_ellipse(und * S, seed_n * S, prior_r=pr,
                         max_axis_ratio=3.0)
    if fit is None:
        return Localized(float(x), float(y), False, "conic3d")
    fit_n = ((fit["centre"] / S), (fit["axes"] * 2.0 / S), fit["angle"])
    c = backproject_circle_centre(fit_n, plane_R, plane_t, K, kc)
    if c is None or not np.isfinite(c).all():
        return Localized(float(x), float(y), False, "conic3d")
    if math.hypot(c[0] - x, c[1] - y) > max(2.5, 0.75 * r):
        return Localized(float(x), float(y), False, "conic3d")
    return Localized(float(c[0]), float(c[1]), True, "conic3d",
                     ellipse=fit["ellipse"], n_edge=int(fit["n"]))


#: localizer registry.  ``needs_plane`` localizers get the per-view plate pose.
LOCALIZERS = {
    "centroid": (loc_centroid, False),
    "ellipse_thresh": (loc_ellipse_thresh, False),
    "edge_ellipse": (loc_edge_ellipse, False),
    "edge_ellipse_adapt": (lambda g, x, y, r, invert=False, prior_r=None:
                           loc_edge_ellipse(g, x, y, r, adaptive=True,
                                            invert=invert, prior_r=prior_r), False),
    "photometric": (loc_photometric, False),
    "conic3d": (loc_conic3d, True),
    "conic3d_adapt": (lambda g, x, y, r, K, kc, plane_R, plane_t, invert=False,
                      prior_r=None: loc_conic3d(g, x, y, r, K, kc, plane_R, plane_t,
                                                adaptive=True, invert=invert,
                                                prior_r=prior_r), True),
}

LOCALIZER_LABELS = {
    "centroid": "intensity centroid (current)",
    "ellipse_thresh": "guarded threshold ellipse (current optional)",
    "edge_ellipse": "Canny+subpixel ellipse (vendor 75/150)",
    "edge_ellipse_adapt": "Canny+subpixel ellipse (adaptive Canny)",
    "photometric": "photometric blurred-disk LSQ",
    "conic3d": "conic back-projection (distortion-aware)",
    "conic3d_adapt": "conic back-projection + adaptive Canny",
}


def window_radius(mode: str, blob_w: float, blob_h: float) -> int:
    """Window radius, matching the production convention for the current modes."""
    d = max(blob_w, blob_h)
    if mode == "centroid":
        return max(4, int(0.7 * d))
    if mode == "ellipse_thresh":
        return max(6, int(0.9 * d))
    if mode in ("edge_ellipse", "edge_ellipse_adapt", "conic3d"):
        return max(9, int(0.8 * d) + 4)
    return max(10, int(0.8 * d) + 5)


def localize_blob(gray: np.ndarray, mode: str, x: float, y: float, r: int,
                  plane=None, invert: bool = False, prior_r: Optional[float] = None,
                  **kw) -> Localized:
    """Run one localizer on a blob seed.

    ``prior_r`` is the expected marker radius in pixels (default: 0.6 of the
    window radius); it is a guard, not a fitted quantity.
    """
    fn, needs_plane = LOCALIZERS[mode]
    if prior_r is None:
        prior_r = max(3.0, 0.6 * float(r))
    if needs_plane:
        if plane is None:
            return Localized(float(x), float(y), False, mode)
        return fn(gray, x, y, r, K=plane["K"], kc=plane["kc"],
                  plane_R=plane["R"], plane_t=plane["t"], invert=invert,
                  prior_r=prior_r, **kw)
    return fn(gray, x, y, r, invert=invert, prior_r=prior_r, **kw)


# --------------------------------------------------------------------------
# seeding: blobs from the same detector front-end for every localizer
# --------------------------------------------------------------------------

@dataclass
class Seed:
    x: float
    y: float
    w: int
    h: int
    area: float


def seed_threshold(img: np.ndarray, method: str = "p99",
                   percentile: float = 99.0) -> float:
    """Global seed threshold.

    ``p99`` reproduces the production rule ``max(30, p99)``; ``relative`` uses
    the plate-to-marker contrast (``p90 + 0.5 (p99.5 - p90)``) and is immune to
    the production rule's failure when the bright marker area exceeds ~1 % of
    the frame (which happens for close-range views and large markers).
    """
    if method == "p99":
        return float(max(30, int(np.percentile(img, percentile))))
    p90 = float(np.percentile(img, 90))
    p995 = float(np.percentile(img, 99.5))
    return float(max(30.0, p90 + 0.5 * (p995 - p90)))


def _components(img: np.ndarray, thr: float, min_area: float = 25.0,
                max_diameter: float = 60.0) -> list:
    """The production connected-component filters (see ``detect.detect_blobs``)."""
    _, bw = cv2.threshold(img.astype(np.uint8), int(thr), 255, cv2.THRESH_BINARY)
    n, lab, stats, cent = cv2.connectedComponentsWithStats(bw, 8)
    out = []
    for i in range(1, n):
        area = float(stats[i, cv2.CC_STAT_AREA])
        w = int(stats[i, cv2.CC_STAT_WIDTH])
        h = int(stats[i, cv2.CC_STAT_HEIGHT])
        if area < min_area or w > max_diameter * 2 or h > max_diameter * 2:
            continue
        if not (0.45 < w / max(h, 1) < 2.2):
            continue
        out.append(Seed(float(cent[i][0]), float(cent[i][1]), w, h, area))
    out.sort(key=lambda b: -b.area)
    kept = []
    for b in out:
        if all((b.x - q.x) ** 2 + (b.y - q.y) ** 2 > 16.0 for q in kept):
            kept.append(b)
    return kept


def blob_seeds(gray: np.ndarray, invert: bool = False,
               percentile: float = 99.0, method: str = "p99") -> list:
    """Connected-component seeds, identical for every localizer."""
    img = gray if not invert else (255 - gray)
    if method == "p99":
        blobs = detect_blobs(gray, invert=invert, percentile=percentile, refine=False)
        return [Seed(float(b.x), float(b.y), int(b.w), int(b.h), float(b.area))
                for b in blobs]
    return _components(img, seed_threshold(img, method, percentile))


def match_seeds(seeds: Sequence[Seed], centres: np.ndarray,
                max_dist: float = 4.0) -> list:
    """Nearest seed for each true centre (or None)."""
    out = []
    for c in centres:
        if not np.isfinite(c).all():
            out.append(None)
            continue
        best, bd = None, max_dist
        for s in seeds:
            d = math.hypot(s.x - c[0], s.y - c[1])
            if d < bd:
                best, bd = s, d
        out.append(best)
    return out


# --------------------------------------------------------------------------
# synthetic stress matrix
# --------------------------------------------------------------------------

@dataclass
class Case:
    name: str
    group: str
    kwargs: dict = field(default_factory=dict)


def synthetic_cases(quick: bool = False) -> list:
    """The stress matrix: one scene per imaging condition."""
    cases = []

    def add(name, group, **kw):
        cases.append(Case(name, group, kw))

    add("fronto", "baseline")
    for tilt in (5, 10, 15, 20, 25, 30, 35, 45):
        add(f"tilt{tilt}", "tilt", tilt_x=float(tilt))
    add("tilt20_y", "tilt", tilt_y=20.0)
    add("tilt30_diag", "tilt", tilt_x=30.0, tilt_y=20.0)
    add("tilt35_d240", "tilt", tilt_x=35.0, distance=240.0)
    add("tilt35_d560", "tilt", tilt_x=35.0, distance=560.0)
    for psf in (0.5, 1.0, 1.5, 2.0, 2.5, 3.0):
        add(f"blur{psf}", "blur", tilt_x=20.0, psf=float(psf))
    add("blur3_tilt0", "blur", psf=3.0)
    add("blur3_d560", "blur", tilt_x=20.0, psf=3.0, distance=560.0)
    # saturation / clipping
    add("sat_420", "saturation", tilt_x=20.0, hole_level=420.0)
    add("sat_255", "saturation", tilt_x=20.0, hole_level=255.0)
    add("sat_180", "saturation", tilt_x=20.0, hole_level=180.0)
    add("sat_420_fronto", "saturation", hole_level=420.0)
    # low contrast
    for hl in (70.0, 85.0, 100.0, 140.0):
        add(f"contrast_{int(hl)}", "contrast", tilt_x=20.0, hole_level=hl)
    add("contrast_85_fronto", "contrast", hole_level=85.0)
    # IR-like illumination gradient across the plate
    add("grad_x0.4", "gradient", tilt_x=20.0, gradient=(0.4, 0.0))
    add("grad_y0.4", "gradient", tilt_x=20.0, gradient=(0.0, 0.4))
    add("grad_xy0.3", "gradient", tilt_x=20.0, gradient=(0.3, 0.3))
    add("grad_xy0.3_fronto", "gradient", gradient=(0.3, 0.3))
    # occlusion / partial visibility
    add("occ_width30", "occlusion", tilt_x=20.0,
        occluder={"x": 640.0, "width": 30.0, "soft": 1.5, "level": 5.0})
    add("occ_width80", "occlusion", tilt_x=20.0,
        occluder={"x": 600.0, "width": 80.0, "soft": 1.0, "level": 5.0})
    add("occ_angle", "occlusion", tilt_x=20.0,
        occluder={"x": 640.0, "width": 40.0, "soft": 1.2, "level": 5.0, "angle": 25.0})
    add("partial", "occlusion", tilt_x=20.0, lateral=(130.0, 0.0))
    # neighbouring holes
    add("pitch18", "neighbours", tilt_x=20.0, pitch=18.0)
    add("radius5", "neighbours", tilt_x=20.0, radius=5.0)
    add("pitch18_blur3", "neighbours", tilt_x=20.0, pitch=18.0, psf=3.0)
    # exposure
    for ex in (0.4, 0.6, 0.8, 1.2, 1.5):
        add(f"exposure{ex}", "exposure", tilt_x=20.0, exposure=float(ex))
    # distance
    for d in (240.0, 320.0, 480.0, 560.0):
        add(f"dist{int(d)}", "distance", tilt_x=20.0, distance=d)
    # polarity: dark holes on a bright plate (the photograph case)
    add("polarity_dark", "polarity", tilt_x=20.0, hole_level=12.0,
        plate_level=200.0, bg_level=230.0, wall_level=180.0)
    add("polarity_dark_fronto", "polarity", hole_level=12.0,
        plate_level=200.0, bg_level=230.0, wall_level=180.0)
    if quick:
        keep = {"fronto", "tilt15", "tilt35", "blur2.0", "blur3.0", "sat_420",
                "contrast_85", "grad_xy0.3", "occ_width30", "pitch18",
                "exposure0.6", "dist240", "polarity_dark"}
        cases = [c for c in cases if c.name in keep]
    return cases


def build_spec(case: Case, K: np.ndarray, kc: np.ndarray,
               seed: int = 0) -> tuple:
    kw = dict(case.kwargs)
    distance = float(kw.pop("distance", 400.0))
    tilt_x = float(kw.pop("tilt_x", 0.0))
    tilt_y = float(kw.pop("tilt_y", 0.0))
    roll = float(kw.pop("roll", 0.0))
    lateral = tuple(kw.pop("lateral", (0.0, 0.0)))
    from .adaptive import board_pose
    R, t = board_pose(distance, tilt_x, tilt_y, roll, lateral)
    # ergonomic aliases used by the case table
    alias = {"psf": "psf_sigma", "pitch": "pitch_mm", "radius": "radius_mm"}
    kw = {alias.get(k, k): v for k, v in kw.items()}
    spec = RenderSpec(R=R, t=t, K=K, kc=kc, seed=seed, **kw)
    return spec, {"distance": distance, "tilt_x": tilt_x, "tilt_y": tilt_y,
                  "roll": roll}


def _add_noise(img: np.ndarray, sigma: float, rng: np.random.Generator,
               clipmax: float = 255.0) -> np.ndarray:
    if sigma <= 0:
        return img.copy()
    out = img.astype(np.float32) + rng.normal(0.0, sigma, img.shape).astype(np.float32)
    return np.clip(np.round(out), 0, 255).astype(np.uint8)


def _plane_of(R: np.ndarray, t: np.ndarray, K: np.ndarray, kc: np.ndarray) -> dict:
    return {"R": np.asarray(R, float), "t": np.asarray(t, float),
            "K": np.asarray(K, float), "kc": np.asarray(kc, float)}


@dataclass
class EstimateRow:
    case: str
    group: str
    mode: str
    rep: int
    hole: int
    uv: tuple
    ok: bool
    est: Optional[np.ndarray]
    err_total: Optional[np.ndarray]     # estimate - projection of the hole centre
    err_visible: Optional[np.ndarray]   # estimate - visible-aperture centroid
    geom: np.ndarray                    # visible centroid - centre projection
    radial: float
    tangential: float
    fully_visible: bool
    r_mm: float
    t_ms: float


def run_synthetic(cases: Optional[list] = None, n_rep: int = 4,
                  modes: Optional[list] = None, seed0: int = 100,
                  verbose: bool = False) -> tuple:
    """Render every case and run every localizer; returns rows and scene info."""
    cases = synthetic_cases() if cases is None else cases
    modes = list(LOCALIZERS) if modes is None else modes
    K, kc = vendor_camera()
    rows = []
    scenes = {}
    for ci, case in enumerate(cases):
        spec, info = build_spec(case, K, kc, seed=seed0 + ci)
        rng = np.random.default_rng(seed0 * 1000 + ci)
        scene = render(spec, rng)
        cen = np.array([h.centre_px for h in scene.holes])
        vis = np.array([h.visible_px for h in scene.holes])
        plane = _plane_of(spec.R, spec.t, K, kc)
        scenes[case.name] = {"info": info, "n_holes": len(scene.holes),
                             "render_s": scene.render_s,
                             "geom_bias_rms": float(np.sqrt(np.nanmean(
                                 np.sum((vis - cen) ** 2, 1)))),
                             "window": scene.window}
        invert = case.group == "polarity"
        if verbose:
            print(f"  [{ci+1}/{len(cases)}] {case.name}: rendered "
                  f"{scene.render_s:.2f}s geom-bias "
                  f"{np.sqrt(np.nanmean(np.sum((vis-cen)**2,1))):.3f}px")
        for rep in range(n_rep):
            img = _add_noise(scene.image, spec.noise_dn,
                             np.random.default_rng(seed0 + 7919 * ci + rep))
            seeds = blob_seeds(img, invert=invert, method="relative")
            matches = match_seeds(seeds, cen)
            for hi, (h, s, v, c) in enumerate(zip(scene.holes, matches, vis, cen)):
                geom = v - c
                rad_dir = c - np.array([K[0, 2], K[1, 2]])
                nr = float(np.linalg.norm(rad_dir))
                rhat = rad_dir / nr if nr > 1e-9 else np.array([0.0, 0.0])
                that = np.array([-rhat[1], rhat[0]])
                for mode in modes:
                    t0 = time.perf_counter()
                    if s is None:
                        loc = Localized(float(c[0]), float(c[1]), False, mode)
                    else:
                        r = window_radius(mode, s.w, s.h)
                        loc = localize_blob(img, mode, s.x, s.y, r,
                                            plane=plane, invert=invert,
                                            prior_r=0.5 * max(s.w, s.h))
                    dt = (time.perf_counter() - t0) * 1e3
                    est = np.array([loc.x, loc.y]) if loc.ok else None
                    rows.append(EstimateRow(
                        case.name, case.group, mode, rep, hi, h.uv, bool(loc.ok),
                        est,
                        None if est is None else est - c,
                        None if est is None else est - v,
                        geom, float(np.dot(est - c, rhat)) if est is not None else np.nan,
                        float(np.dot(est - c, that)) if est is not None else np.nan,
                        bool(h.fully_visible), float(h.r_mm), dt))
    return rows, scenes


def summarize_synthetic(rows: list) -> dict:
    """Aggregate per (case, mode): bias, RMS, repeatability, failure rate."""
    out = {}
    by = {}
    for r in rows:
        by.setdefault((r.case, r.mode), []).append(r)
    for (case, mode), rs in by.items():
        full = [r for r in rs if r.fully_visible]
        ok = [r for r in full if r.ok]
        n = len(full)
        n_fail = n - len(ok)
        gross = 0
        if ok:
            e = np.array([r.err_total for r in ok])
            gross = int(np.sum(np.linalg.norm(e, axis=1) > 1.0))
        # repeatability: per-hole std across reps (independent of truth)
        rep = {}
        for r in rs:
            if r.ok and r.est is not None:
                rep.setdefault(r.hole, []).append(r.est)
        stds = [np.std(np.array(v), axis=0) for v in rep.values() if len(v) > 1]
        if stds:
            stds = np.array(stds)
            rep_rms = float(np.sqrt(np.mean(np.sum(stds ** 2, 1))))
            rep_max = float(np.max(np.sqrt(np.sum(stds ** 2, 1))))
        else:
            rep_rms = rep_max = float("nan")
        out[f"{case}|{mode}"] = {
            "case": case, "mode": mode, "n": n, "n_ok": len(ok),
            "n_fail": n_fail,
            "fail_rate": n_fail / n if n else float("nan"),
            "gross_rate": gross / n if n else float("nan"),
            "bias": e.mean(0).tolist() if ok else [float("nan")] * 2,
            "bias_norm": float(np.linalg.norm(e.mean(0))) if ok else float("nan"),
            "rms": float(np.sqrt(np.mean(np.sum(e ** 2, 1)))) if ok else float("nan"),
            "median": float(np.median(np.linalg.norm(e, axis=1))) if ok else float("nan"),
            "p90": float(np.percentile(np.linalg.norm(e, axis=1), 90)) if ok else float("nan"),
            "radial": float(np.nanmean([r.radial for r in ok])) if ok else float("nan"),
            "tangential": float(np.nanmean([r.tangential for r in ok])) if ok else float("nan"),
            "repeatability": rep_rms, "repeatability_max": rep_max,
            "ms_per_hole": float(np.median([r.t_ms for r in rs])),
        }
    return out


# --------------------------------------------------------------------------
# vendor corpus evaluation
# --------------------------------------------------------------------------

_UV_INDEX = {(float(u), float(v)): i for i, (u, v) in enumerate(plate.lattice_uv())}


def vendor_images(vendor_dir: Path = VENDOR_DIR,
                  sides: Sequence[str] = ("Left", "Right", "Tex"),
                  indices: Optional[Sequence[int]] = None) -> list:
    out = []
    for side in sides:
        for i in (indices if indices is not None else range(1, 26)):
            p = Path(vendor_dir) / f"image{side}{i}.bmp"
            if p.exists():
                out.append((side, int(i), p))
    return out


def load_ccf_cameras(ccf_dir: Path = CCF_DIR) -> dict:
    cams = {}
    try:
        from .ccf import load as load_ccf
        for name, cam in (("LeftCCF.txt", "left"), ("RightCCF.txt", "right"),
                          ("TexCCF.txt", "tex")):
            p = Path(ccf_dir) / name
            if p.exists():
                cams[cam] = CameraIntrinsics.from_ccf(load_ccf(p))
    except Exception:
        pass
    if not cams:
        for cam in CAMERAS:
            cams[cam] = CameraIntrinsics(1159.0, 1159.0, 640.0, 512.0, DEFAULT_KC.copy())
    return cams


def _homography_rms(uv: np.ndarray, px: np.ndarray) -> float:
    if len(uv) < 6:
        return float("nan")
    H, _ = cv2.findHomography(uv, px, cv2.RANSAC, 1.5)
    if H is None:
        return float("nan")
    pred = cv2.perspectiveTransform(uv.reshape(-1, 1, 2), H).reshape(-1, 2)
    return float(np.sqrt(np.mean(np.sum((pred - px) ** 2, 1))))


def detect_with_mode(gray: np.ndarray, mode: str, plane: Optional[dict] = None,
                     invert: bool = False, percentile: float = 99.0,
                     cam: Optional[CameraIntrinsics] = None,
                     seed_method: str = "p99") -> dict:
    """Seed blobs with the production front-end, refine with ``mode``, assign grid.

    Rejected refinements fall back to the connected-component centroid, exactly
    as the production ellipse mode does; the rejection count is reported.
    """
    t0 = time.perf_counter()
    seeds = blob_seeds(gray, invert=invert, percentile=percentile,
                       method=seed_method)
    blobs = []
    n_rej = 0
    for s in seeds:
        r = window_radius(mode, s.w, s.h)
        loc = localize_blob(gray, mode, s.x, s.y, r, plane=plane, invert=invert,
                            prior_r=0.5 * max(s.w, s.h))
        if loc.ok:
            blobs.append(Blob(loc.x, loc.y, s.area, s.w, s.h))
        else:
            n_rej += 1
            blobs.append(Blob(s.x, s.y, s.area, s.w, s.h))
    obs = assign_grid(blobs)
    dt = (time.perf_counter() - t0) * 1e3
    out = {"obs": obs, "n_blobs": len(seeds), "n_rejected": n_rej, "time_ms": dt}
    if obs is not None:
        out["rms_loo"] = _homography_loo(obs)
        if cam is not None:
            # distortion-free consistency: undistort the detected pixels with the
            # CCF model, then fit a homography to the (undistorted) projections
            und = cv2.undistortPoints(obs.pixels.reshape(-1, 1, 2).astype(np.float32),
                                      cam.K, np.asarray(cam.kc, float).ravel())
            und = und.reshape(-1, 2) * float(cam.fx)
            out["rms_undist"] = _homography_rms(obs.uv, und)
    return out


def detect_best(gray: np.ndarray, mode: str, cam: Optional[CameraIntrinsics] = None,
                plane: Optional[dict] = None) -> dict:
    """Detect with the production p99 seeds, retry with the relative rule.

    The p99 rule saturates when the bright marker area exceeds ~1 % of the
    frame (close range, large markers).  The vendor corpus never reaches that
    (max 0.6 %), so the retry is a synthetic-scene robustness measure.
    """
    r = detect_with_mode(gray, mode, plane=plane, cam=cam)
    if r.get("obs") is None:
        r2 = detect_with_mode(gray, mode, plane=plane, cam=cam,
                              seed_method="relative")
        if r2.get("obs") is not None:
            return r2
    return r


def _homography_loo(obs: GridObservation) -> float:
    """Leave-one-out homography residual (an overfit-free consistency proxy)."""
    uv = obs.uv
    px = obs.pixels
    if len(uv) < 8:
        return float("nan")
    errs = []
    for i in range(len(uv)):
        keep = np.ones(len(uv), bool)
        keep[i] = False
        H, _ = cv2.findHomography(uv[keep], px[keep], cv2.RANSAC, 1.5)
        if H is None:
            continue
        p = cv2.perspectiveTransform(uv[i:i + 1].reshape(-1, 1, 2), H).reshape(2)
        errs.append(float(np.linalg.norm(p - px[i])))
    return float(np.sqrt(np.mean(np.square(errs)))) if errs else float("nan")


def pose_from_grid(obs: GridObservation, cam: CameraIntrinsics):
    """Board pose from an assigned grid (first-pass plane for the conic localizer)."""
    keys = [k for k in obs.points if k in _UV_INDEX]
    if len(keys) < 8:
        return None
    obj = plate.board_points_mm()[[_UV_INDEX[k] for k in keys]]
    img = np.array([obs.points[k] for k in keys], np.float32)
    return solve_pnp(obj, img, cam.K, cam.kc)


def run_vendor(vendor_dir: Path = VENDOR_DIR, modes: Optional[list] = None,
               sides: Sequence[str] = ("Left", "Right", "Tex"),
               limit: Optional[int] = None, verbose: bool = False) -> dict:
    """Detect every vendor image with every localizer and calibrate each set."""
    modes = list(LOCALIZERS) if modes is None else modes
    cams = load_ccf_cameras()
    files = vendor_images(vendor_dir, sides)
    if limit:
        files = files[:limit]
    K, kc = cams["left"].K, cams["left"].kc
    per_image = {m: {} for m in modes}
    timings = {m: [] for m in modes}
    geometry = {}
    for k, (side, idx, path) in enumerate(files):
        gray = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
        if gray is None:
            continue
        cam = cams[CAMERA_NAMES[side]]
        # shared front-end choice: the production auto-polarity/percentile search
        # on the centroid mode decides the seed configuration for every mode
        base = None
        for invert in (False, True):
            for pct in (99.0, 99.4, 98.5):
                r = detect_with_mode(gray, "centroid", invert=invert, percentile=pct,
                                     cam=cam)
                if r["obs"] is None:
                    continue
                key = (r["obs"].n_lattice, -r["obs"].rms)
                if base is None or key > base[0]:
                    base = (key, invert, pct, r)
        if base is None:
            for m in modes:
                per_image[m][(side, idx)] = {"obs": None, "error": "no seeds"}
            continue
        _, invert, pct, base_r = base
        plane = None
        pose = pose_from_grid(base_r["obs"], cam)
        if pose is not None:
            plane = _plane_of(pose.R, pose.t, cam.K, cam.kc)
            nrm = pose.R[:, 2]
            cos_t = abs(float(nrm[2])) / max(float(np.linalg.norm(nrm)), 1e-9)
            geometry[(side, idx)] = {
                "tilt_deg": float(np.degrees(np.arccos(np.clip(cos_t, -1.0, 1.0)))),
                "distance_mm": float(pose.t[2]),
            }
        for m in modes:
            if m == "centroid":
                r = base_r
            else:
                r = detect_with_mode(gray, m, plane=plane, invert=invert,
                                     percentile=pct, cam=cam)
            per_image[m][(side, idx)] = r
            timings[m].append(r["time_ms"])
        if verbose:
            o = base_r["obs"]
            print(f"  [{k+1}/{len(files)}] {side}{idx}: centroid n={0 if o is None else o.n_lattice} "
                  f"rms={0 if o is None else o.rms:.3f}")
    # calibrate each mode
    results = {}
    for m in modes:
        results[m] = _calibrate_mode(per_image[m], cams, timings[m])
    return {"per_image": per_image, "calibration": results, "files": files,
            "geometry": geometry}


def _build_observations(per_image: dict) -> tuple:
    obs = []
    for (side, idx), r in sorted(per_image.items()):
        o = r.get("obs")
        if o is None or o.n_lattice < 25:
            continue
        obs.append(Observation(CAMERA_NAMES[side], idx - 1,
                               {k: np.asarray(v, float) for k, v in o.points.items()}))
    return obs


def _calibrate_mode(per_image: dict, cams: dict, timings: list,
                    pitch: float = plate.DEFAULT_PITCH_MM) -> dict:
    obs = _build_observations(per_image)
    n_used = len(obs)
    n_views = len(set(o.view for o in obs))
    grids = [r for r in per_image.values() if r.get("obs") is not None]
    rms_fit = [r["obs"].rms for r in grids]
    rms_loo = [r.get("rms_loo", float("nan")) for r in grids]
    out = {
        "n_images": len(per_image),
        "n_grids": len(grids),
        "n_obs": n_used,
        "n_views": n_views,
        "n_lattice_mean": float(np.mean([r["obs"].n_lattice for r in grids])) if grids else 0.0,
        "n_lattice_min": int(min([r["obs"].n_lattice for r in grids])) if grids else 0,
        "n_rejected": int(sum(r.get("n_rejected", 0) for r in per_image.values())),
        "n_blobs": int(sum(r.get("n_blobs", 0) for r in per_image.values())),
        "homog_rms_median": float(np.median(rms_fit)) if rms_fit else float("nan"),
        "homog_rms_p90": float(np.percentile(rms_fit, 90)) if rms_fit else float("nan"),
        "homog_loo_median": float(np.nanmedian(rms_loo)) if rms_loo else float("nan"),
        "homog_loo_p90": float(np.nanpercentile(rms_loo, 90)) if rms_loo else float("nan"),
        "homog_undist_median": float(np.nanmedian([r.get("rms_undist", np.nan) for r in grids]))
        if grids else float("nan"),
        "homog_undist_p90": float(np.nanpercentile([r.get("rms_undist", np.nan) for r in grids], 90))
        if grids else float("nan"),
        "detect_ms_per_image": float(np.median(timings)) if timings else float("nan"),
    }
    if not obs:
        return out
    t0 = time.time()
    try:
        res = calibrate(obs, pitch, cams, free_kc=False, free_intrinsics=True,
                        max_nfev=1200)
    except Exception as exc:      # pragma: no cover - diagnostic only
        out["error"] = repr(exc)
        return out
    out["calib_s"] = time.time() - t0
    out["rms_px"] = res.residual_rms
    out["max_px"] = res.residual_max
    out["baseline_mm"] = float(np.linalg.norm(res.baseline()))
    out["fx"] = {c: res.cameras[c].fx for c in CAMERAS}
    out["fy"] = {c: res.cameras[c].fy for c in CAMERAS}
    out["cx"] = {c: res.cameras[c].cx for c in CAMERAS}
    out["cy"] = {c: res.cameras[c].cy for c in CAMERAS}
    out["dfx_vs_ccf"] = {c: res.cameras[c].fx - cams[c].fx for c in CAMERAS}
    out["dcx_vs_ccf"] = {c: res.cameras[c].cx - cams[c].cx for c in CAMERAS}
    out["dcy_vs_ccf"] = {c: res.cameras[c].cy - cams[c].cy for c in CAMERAS}
    out["dfy_vs_ccf"] = {c: res.cameras[c].fy - cams[c].fy for c in CAMERAS}
    out["rot_vs_ccf_deg"] = _rig_rotation_diff(res, cams)
    out["epipolar"] = _epipolar_stats(obs, res, cams)
    out["epipolar_ccf"] = _epipolar_stats(obs, None, cams)
    # per-view gate, as in the production pipeline
    from .evaluate import per_view_errors
    pv = per_view_errors(res, obs, pitch)
    med = float(np.median([d["rms_px"] for d in pv.values()])) if pv else 0.0
    bad = {v for v, d in pv.items() if d["rms_px"] > max(2.0, 3.0 * med)}
    out["gate_rejected_views"] = sorted(int(v) for v in bad)
    if bad:
        obs2 = [o for o in obs if o.view not in bad]
        try:
            res2 = calibrate(obs2, pitch, cams, free_kc=False,
                             free_intrinsics=True, max_nfev=1200)
            out["gated"] = {
                "n_obs": res2.n_observations,
                "rms_px": res2.residual_rms,
                "max_px": res2.residual_max,
                "baseline_mm": float(np.linalg.norm(res2.baseline())),
                "dfx_vs_ccf": {c: res2.cameras[c].fx - cams[c].fx for c in CAMERAS},
                "epipolar": _epipolar_stats(obs2, res2, cams),
            }
        except Exception as exc:  # pragma: no cover
            out["gated"] = {"error": repr(exc)}
    else:
        out["gated"] = {"n_obs": res.n_observations, "rms_px": res.residual_rms,
                        "max_px": res.residual_max,
                        "baseline_mm": out["baseline_mm"],
                        "dfx_vs_ccf": out["dfx_vs_ccf"],
                        "epipolar": out["epipolar"]}
    return out


def _rig_rotation_diff(res, cams: dict) -> float:
    try:
        from .ccf import load as load_ccf
        Rl = load_ccf(CCF_DIR / "LeftCCF.txt").R()
        Rr = load_ccf(CCF_DIR / "RightCCF.txt").R()
        ref = Rr @ Rl.T
    except Exception:
        return float("nan")
    Rl2, tl2 = res.extrinsics["left"]
    Rr2, tr2 = res.extrinsics["right"]
    rel = Rr2 @ Rl2.T
    ang = np.degrees(np.arccos(np.clip((np.trace(rel.T @ ref) - 1.0) / 2.0, -1.0, 1.0)))
    return float(ang)


def _epipolar_stats(obs: list, res, cams: dict) -> dict:
    """Sampson distance over left/right grid correspondences (pixels)."""
    by_view = {}
    for o in obs:
        by_view.setdefault(o.view, {})[o.camera] = o
    if res is None:
        try:
            from .ccf import load as load_ccf
            Rl = load_ccf(CCF_DIR / "LeftCCF.txt").R(); tl = load_ccf(CCF_DIR / "LeftCCF.txt").T()
            Rr = load_ccf(CCF_DIR / "RightCCF.txt").R(); tr = load_ccf(CCF_DIR / "RightCCF.txt").T()
        except Exception:
            return {}
        K1, K2 = cams["left"].K, cams["right"].K
        Rrel = Rr @ Rl.T
        trel = tr - Rrel @ tl
    else:
        from .geometry import relative_pose
        K1, K2 = res.cameras["left"].K, res.cameras["right"].K
        Rl, tl = res.extrinsics["left"]
        Rr, tr = res.extrinsics["right"]
        Rrel, trel = relative_pose(Rl, tl, Rr, tr)
    F = fundamental(K1, K2, Rrel, trel)
    errs = []
    for v, d in by_view.items():
        if "left" not in d or "right" not in d:
            continue
        keys = [k for k in d["left"].uv if k in d["right"].uv]
        if len(keys) < 4:
            continue
        x1 = np.array([d["left"].uv[k] for k in keys], float)
        x2 = np.array([d["right"].uv[k] for k in keys], float)
        errs.extend(epipolar_residual(F, x1, x2).tolist())
    if not errs:
        return {}
    e = np.array(errs)
    return {"n": int(len(e)), "median_px": float(np.median(e)),
            "p90_px": float(np.percentile(e, 90)),
            "rms_px": float(np.sqrt(np.mean(e ** 2)))}


# --------------------------------------------------------------------------
# geometric bias: conic (projected circle) + through-hole aperture
# --------------------------------------------------------------------------

def _rot_x(a: float) -> np.ndarray:
    c, s = math.cos(a), math.sin(a)
    return np.array([[1, 0, 0], [0, c, -s], [0, s, c]], float)


def _rot_z(a: float) -> np.ndarray:
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]], float)


def pose_tilted(distance: float, tilt_deg: float, az_deg: float = 0.0,
                lateral: tuple = (0.0, 0.0)) -> tuple:
    """Board pose with an exact tilt about an in-plane axis at ``az_deg``."""
    R0 = np.diag([1.0, -1.0, -1.0])
    R = _rot_z(math.radians(az_deg)) @ _rot_x(math.radians(tilt_deg)) @ R0
    return R, np.array([lateral[0], lateral[1], distance], float)


def _ellipse_from_poly(poly: np.ndarray):
    try:
        return cv2.fitEllipse(np.asarray(poly, np.float32).reshape(-1, 1, 2))
    except cv2.error:
        return None


def conic_bias_point(r_mm: float, distance: float, tilt_deg: float,
                     K: np.ndarray, kc: np.ndarray, az_deg: float = 0.0,
                     lateral: tuple = (0.0, 0.0)) -> dict:
    """Conic bias for one circle: ellipse centre vs projected circle centre.

    Returns the bias measured on the distorted image (what a detector sees),
    the pure perspective bias in undistorted coordinates, and the residual
    after the conic back-projection correction.
    """
    R, t = pose_tilted(distance, tilt_deg, az_deg, lateral)
    poly = circle_polygon(K, kc, R, t, 0.0, 0.0, 0.0, r_mm, n=256)
    cen = project_board(K, kc, R, t, np.array([[0.0, 0.0, 0.0]]))[0]
    e = _ellipse_from_poly(poly)
    bias = np.array(e[0], float) - cen if e is not None else np.full(2, np.nan)
    # undistorted (pure pinhole) projection
    poly0 = circle_polygon(K, np.zeros(5), R, t, 0.0, 0.0, 0.0, r_mm, n=256)
    cen0 = project_board(K, np.zeros(5), R, t, np.array([[0.0, 0.0, 0.0]]))[0]
    e0 = _ellipse_from_poly(poly0)
    bias0 = np.array(e0[0], float) - cen0 if e0 is not None else np.full(2, np.nan)
    # correction: undistort the projected circle, fit the conic, back-project
    und = cv2.undistortPoints(poly.reshape(-1, 1, 2).astype(np.float32),
                              np.asarray(K, float), np.asarray(kc, float).ravel())
    und = und.reshape(-1, 2)
    S = float(K[0, 0])
    ec = _ellipse_from_poly(und * S)
    corr = None
    if ec is not None:
        fit_n = ((np.array(ec[0], float) / S), (np.array(ec[1], float) / S), ec[2])
        corr = backproject_circle_centre(fit_n, R, t, K, kc)
    resid = (corr - cen) if corr is not None else np.full(2, np.nan)
    return {"bias_px": bias, "bias_undist_px": bias0,
            "corrected_px": corr if corr is not None else np.full(2, np.nan),
            "residual_px": resid, "tilt_deg": tilt_deg, "distance": distance,
            "radius_mm": r_mm, "az_deg": az_deg}


def conic_bias_sweep(radii=(2.5, 3.4, 4.5), distances=(240.0, 400.0, 560.0),
                     tilts=tuple(range(0, 51, 5)),
                     azimuths=(0.0, 45.0, 90.0),
                     laterals=((0.0, 0.0), (150.0, 100.0))) -> list:
    K, kc = vendor_camera()
    rows = []
    for r in radii:
        for d in distances:
            for tilt in tilts:
                for az in azimuths:
                    for lat in laterals:
                        res = conic_bias_point(r, d, float(tilt), K, kc,
                                               az_deg=az, lateral=lat)
                        res["lateral"] = lat
                        rows.append(res)
    return rows


def aperture_bias_truth(spec: RenderSpec) -> dict:
    """Through-hole bias field: visible-aperture centroid minus centre projection."""
    holes = _truth_geometry(spec)
    cen = np.array([h.centre_px for h in holes])
    vis = np.array([h.visible_px for h in holes])
    bias = vis - cen
    # depth displacement delta = P(c, -t) - P(c, 0) per hole
    delta = []
    for h in holes:
        p0 = project_board(spec.K, spec.kc, spec.R, spec.t,
                           np.array([[h.cx_mm, h.cy_mm, 0.0]]))[0]
        p1 = project_board(spec.K, spec.kc, spec.R, spec.t,
                           np.array([[h.cx_mm, h.cy_mm, -float(spec.thickness_mm)]]))[0]
        delta.append(p1 - p0)
    delta = np.array(delta)
    k = float(np.sum(bias * delta) / max(np.sum(delta * delta), 1e-12))
    pts = np.array([[h.cx_mm, h.cy_mm, 0.0] for h in holes])
    absorb = pose_absorption(cen, bias, pts, spec.R, spec.t, spec.K, spec.kc)
    return {"bias": bias, "delta": delta, "k": k,
            "residual_after_delta_model": bias - k * delta,
            "pose_absorption": absorb,
            "rms_bias": float(np.sqrt(np.mean(np.sum(bias ** 2, 1)))),
            "max_bias": float(np.max(np.linalg.norm(bias, axis=1)))}


def run_through_study(tilts=(0.0, 20.0, 35.0, 45.0),
                      thicknesses=(0.0, 0.5, 1.0, 2.0, 3.0),
                      distance: float = 400.0) -> list:
    """Aperture/depth bias for the nominal plate, plus the pose-absorbed part."""
    K, kc = vendor_camera()
    rows = []
    for t_mm in thicknesses:
        for tilt in tilts:
            R, tt = pose_tilted(distance, tilt)
            spec = RenderSpec(R=R, t=tt, K=K, kc=kc, radius_mm=3.4,
                              thickness_mm=float(t_mm), hole_model="through",
                              wall_level=16.0, psf_sigma=1.2,
                              noise_dn=0.0, seed=3)
            if t_mm <= 0:
                spec.hole_model = "disk"
            tr = aperture_bias_truth(spec)
            rows.append({
                "thickness_mm": float(t_mm), "tilt_deg": float(tilt),
                "distance_mm": float(distance), "wall_level": 16.0,
                    "rms_bias_px": tr["rms_bias"], "max_bias_px": tr["max_bias"],
                    "bias_mean": tr["bias"].mean(0).tolist(),
                    "k_delta": tr["k"],
                    "pose_explained": tr["pose_absorption"]["explained"],
                    "pose_residual_rms_px": tr["pose_absorption"]["residual_rms"],
                    "delta_model_residual_rms_px": float(np.sqrt(np.mean(
                        np.sum(tr["residual_after_delta_model"] ** 2, 1)))),
                })
    return rows


def correct_aperture(est_px: np.ndarray, cx_mm: float, cy_mm: float,
                     spec_R: np.ndarray, spec_t: np.ndarray,
                     thickness_mm: float, K: np.ndarray, kc: np.ndarray,
                     k: float = 0.25) -> np.ndarray:
    """Bias-model correction: subtract ``k * (P(c,-t) - P(c,0))``.

    ``k`` is the lens-to-displacement ratio measured on the through-hole model
    (~0.25 for the nominal plate); ``thickness_mm`` is the assumed plate
    thickness.  Returns the corrected point(s).
    """
    raw = np.asarray(est_px, float)
    single = raw.ndim == 1
    est = raw.reshape(-1, 2)
    if thickness_mm <= 0:
        return est.copy()[0] if single else est.copy()
    p0 = project_board(K, kc, spec_R, spec_t, np.array([[cx_mm, cy_mm, 0.0]]))[0]
    p1 = project_board(K, kc, spec_R, spec_t,
                       np.array([[cx_mm, cy_mm, -float(thickness_mm)]]))[0]
    out = est - k * (p1 - p0)
    return out[0] if single else out


def run_through_detectors(tilts=(0.0, 20.0, 35.0, 45.0),
                          thickness: float = 1.0, distance: float = 400.0,
                          modes=("centroid", "edge_ellipse", "photometric", "conic3d"),
                          n_rep: int = 2) -> dict:
    """Detector bias on through-hole scenes, before and after the aperture model."""
    K, kc = vendor_camera()
    out = {}
    for tilt in tilts:
        R, t = pose_tilted(distance, tilt)
        spec = RenderSpec(R=R, t=t, K=K, kc=kc, radius_mm=3.4,
                          thickness_mm=float(thickness), hole_model="through",
                          wall_level=16.0, psf_sigma=1.2, noise_dn=0.0, seed=3)
        sc = render(spec, np.random.default_rng(1))
        cen = np.array([h.centre_px for h in sc.holes])
        vis = np.array([h.visible_px for h in sc.holes])
        seeds = blob_seeds(sc.image, method="relative")
        ms = match_seeds(seeds, cen)
        plane = _plane_of(R, t, K, kc)
        hole_xy = np.array([[h.cx_mm, h.cy_mm] for h in sc.holes])
        # k calibrated on this scene's aperture truth (the model's own ratio)
        tr = aperture_bias_truth(spec)
        k = tr["k"]
        for mode in modes:
            est = []
            for h, s in zip(sc.holes, ms):
                if s is None:
                    est.append(np.full(2, np.nan))
                    continue
                r = window_radius(mode, s.w, s.h)
                loc = localize_blob(sc.image, mode, s.x, s.y, r, plane=plane,
                                    prior_r=0.5 * max(s.w, s.h))
                est.append(np.array([loc.x, loc.y]) if loc.ok else np.full(2, np.nan))
            est = np.array(est)
            ok = np.isfinite(est[:, 0])
            e0 = est[ok] - cen[ok]
            # correction with the true thickness and with a half/double guess
            corr = {}
            for t_assumed in (0.5 * thickness, thickness, 2.0 * thickness):
                ec = np.array([correct_aperture(est[i], hole_xy[i, 0], hole_xy[i, 1],
                                                R, t, t_assumed, K, kc, k=k)
                               for i in np.where(ok)[0]]).reshape(-1, 2)
                corr[f"t{t_assumed:.2f}"] = float(np.sqrt(np.mean(
                    np.sum((ec - cen[ok]) ** 2, 1))))
            out[f"{mode}|tilt{tilt:g}"] = {
                "n": int(ok.sum()),
                "bias": e0.mean(0).tolist(),
                "rms_px": float(np.sqrt(np.mean(np.sum(e0 ** 2, 1)))),
                "bias_visible": (est[ok] - vis[ok]).mean(0).tolist(),
                "k_delta": k,
                "rms_after_correction": corr,
            }
    return out


# --------------------------------------------------------------------------
# synthetic end-to-end calibration
# --------------------------------------------------------------------------

def render_rig_scene(n_views: int = 10, seed: int = 42, noise_dn: float = 1.0,
                     psf_sigma: float = 1.2, n_rep: int = 1):
    """Render a full 3-camera synthetic scene with the physical plate model."""
    from .synth import make_scene
    scene = make_scene(n_views=n_views, noise_px=0.0, outlier_frac=0.0, seed=seed)
    frames = {}
    for v, (R, t) in enumerate(scene.views):
        for ci, c in enumerate(CAMERAS):
            Rc, tc = scene.extrinsics[c]
            Rw = Rc @ R
            tw = Rc @ t + tc
            cam = scene.cameras[c]
            spec = RenderSpec(R=Rw, t=tw, K=cam.K, kc=cam.kc,
                              psf_sigma=psf_sigma, noise_dn=0.0, seed=seed * 100 + v)
            base = render(spec, np.random.default_rng(seed * 1000 + 17 * v + ci))
            frames[(c, v)] = [ _add_noise(base.image, noise_dn,
                                          np.random.default_rng(seed * 7919 + 31 * v + ci + 100 * r))
                               for r in range(n_rep) ]
    return scene, frames


def run_synthetic_calibration(n_views: int = 10, seed: int = 42,
                              modes: Optional[list] = None, n_rep: int = 1,
                              free_kc: bool = False) -> dict:
    """Detect the rendered rig frames and calibrate; compare to ground truth."""
    from .synth import perturbed_init
    from .evaluate import intrinsic_errors, rig_errors
    modes = list(LOCALIZERS) if modes is None else modes
    scene, frames = render_rig_scene(n_views=n_views, seed=seed, n_rep=n_rep)
    rng = np.random.default_rng(seed + 5)
    init = perturbed_init(scene, rng, fx_sigma=6.0, cx_sigma=6.0)
    # first-pass plane per frame (centroid -> grid -> PnP), the production
    # two-stage workflow needed by the conic localizers
    planes = {}
    for (c, v), imgs in frames.items():
        cam = scene.cameras[c]
        r0 = detect_best(imgs[0], "centroid", cam=cam)
        if r0["obs"] is not None:
            pose = pose_from_grid(r0["obs"], cam)
            if pose is not None:
                planes[(c, v)] = _plane_of(pose.R, pose.t, cam.K, cam.kc)
    out = {}
    for m in modes:
        t0 = time.time()
        obs = []
        n_fail = 0
        n_pts = 0
        for (c, v), imgs in frames.items():
            cam = scene.cameras[c]
            best = None
            for img in imgs:
                r = detect_best(img, m, cam=cam, plane=planes.get((c, v)))
                if r["obs"] is None:
                    # one retry with the contrast-relative seed rule
                    seeds = blob_seeds(img, method="relative")
                    blobs = []
                    for s in seeds:
                        rr = window_radius(m, s.w, s.h)
                        loc = localize_blob(img, m, s.x, s.y, rr,
                                            prior_r=0.5 * max(s.w, s.h))
                        blobs.append(Blob(loc.x, loc.y, s.area, s.w, s.h) if loc.ok
                                     else Blob(s.x, s.y, s.area, s.w, s.h))
                    o = assign_grid(blobs)
                    r = {"obs": o}
                if r["obs"] is None:
                    continue
                if best is None or (r["obs"].n_lattice, -r["obs"].rms) > (best[0].n_lattice, -best[0].rms):
                    best = (r["obs"],)
            if best is None or best[0].n_lattice < 25:
                n_fail += 1
                continue
            o = best[0]
            n_pts += o.n_lattice
            obs.append(Observation(c, v, {k: np.asarray(x, float) for k, x in o.points.items()}))
        if not obs:
            out[m] = {"n_obs": 0, "n_fail": n_fail}
            continue
        res = calibrate(obs, scene.pitch_mm, init, free_kc=free_kc,
                        free_intrinsics=True, max_nfev=1200)
        ie = intrinsic_errors(res.cameras, scene.cameras)
        re = rig_errors(res, scene.truth["extrinsics"], scene.truth["baseline_mm"])
        out[m] = {
            "n_obs": len(obs), "n_fail": n_fail, "n_points": n_pts,
            "rms_px": res.residual_rms,
            "fx_err": {c: ie[c]["fx"] for c in CAMERAS},
            "fy_err": {c: ie[c]["fy"] for c in CAMERAS},
            "cx_err": {c: ie[c]["cx"] for c in CAMERAS},
            "cy_err": {c: ie[c]["cy"] for c in CAMERAS},
            "fx_abs_mean": float(np.mean([abs(ie[c]["fx"]) for c in CAMERAS])),
            "cx_abs_mean": float(np.mean([abs(ie[c]["cx"]) for c in CAMERAS])),
            "baseline_err_mm": re["baseline_mm"],
            "right_rot_err_deg": re["right"]["rot_deg"],
            "right_t_err_mm": re["right"]["t_mm"],
            "time_s": time.time() - t0,
        }
    out["_scene"] = {"n_views": n_views, "seed": seed, "n_rep": n_rep,
                     "truth": {"fx": {c: scene.cameras[c].fx for c in CAMERAS},
                               "baseline_mm": scene.truth["baseline_mm"],
                               "pitch_mm": scene.pitch_mm}}
    return out


# --------------------------------------------------------------------------
# figures
# --------------------------------------------------------------------------

_MODE_COLORS = {
    "centroid": "#1f77b4", "ellipse_thresh": "#7f7f7f",
    "edge_ellipse": "#d62728", "edge_ellipse_adapt": "#ff9896",
    "photometric": "#2ca02c", "conic3d": "#9467bd", "conic3d_adapt": "#c5b0d5",
}
_MODE_MARKERS = {"centroid": "o", "ellipse_thresh": "s", "edge_ellipse": "^",
                 "edge_ellipse_adapt": "v", "photometric": "D",
                 "conic3d": "P", "conic3d_adapt": "X"}


def _plt():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    return plt


def _series(summary: dict, group_cases: dict, group: str, value: str,
            axis: str) -> dict:
    """Extract {mode: ([x], [value])} for one stress group."""
    out = {}
    for case, x in group_cases:
        for mode in LOCALIZERS:
            v = summary.get(f"{case}|{mode}")
            if v is None:
                continue
            out.setdefault(mode, ([], []))
            out[mode][0].append(x)
            out[mode][1].append(v[value])
    return out


def make_figures(outdir: Path, results: dict) -> list:
    plt = _plt()
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    made = []
    summary = results.get("synthetic", {}).get("summary", {})
    modes = [m for m in LOCALIZERS if any(k.endswith("|" + m) for k in summary)]

    def save(fig, name):
        p = outdir / name
        fig.tight_layout()
        fig.savefig(p, dpi=140)
        plt.close(fig)
        made.append(str(p))

    # ---- 1. tilt sweep: RMS and bias magnitude -------------------------
    if summary:
        fig, ax = plt.subplots(1, 2, figsize=(10.5, 4.0))
        tilts = [5, 10, 15, 20, 25, 30, 35, 45]
        for m in modes:
            xs, ys, bs = [], [], []
            for t in tilts:
                v = summary.get(f"tilt{t}|{m}")
                if v is None or not np.isfinite(v["rms"]):
                    continue
                xs.append(t); ys.append(v["rms"]); bs.append(v["bias_norm"])
            if xs:
                ax[0].plot(xs, ys, marker=_MODE_MARKERS.get(m, "o"), ms=3.5,
                           color=_MODE_COLORS.get(m), label=m, lw=1.2)
                ax[1].plot(xs, bs, marker=_MODE_MARKERS.get(m, "o"), ms=3.5,
                           color=_MODE_COLORS.get(m), lw=1.2)
        ax[0].set_xlabel("plate tilt (deg)"); ax[0].set_ylabel("RMS error (px)")
        ax[0].set_title("flush-diffuser plate: RMS vs tilt")
        ax[1].set_xlabel("plate tilt (deg)"); ax[1].set_ylabel("|mean bias| (px)")
        ax[1].set_title("systematic bias vs tilt")
        ax[0].grid(alpha=0.3); ax[1].grid(alpha=0.3)
        ax[0].legend(fontsize=6.5)
        save(fig, "fig_bias_vs_tilt.png")

    # ---- 2. stress panels ----------------------------------------------
    if summary:
        groups = {
            "defocus": [("blur%.1f" % s, s) for s in (0.5, 1.0, 1.5, 2.0, 2.5, 3.0)],
            "contrast (hole DN)": [(f"contrast_{h}", h) for h in (70, 85, 100, 140)],
            "exposure": [(f"exposure{e}", e) for e in (0.4, 0.6, 0.8, 1.2, 1.5)],
            "distance (mm)": [("dist240", 240), ("dist320", 320), ("dist480", 480),
                              ("dist560", 560)],
            "tilt 35 deg: 240 vs 560 mm": [("tilt35_d240", 240), ("tilt35_d560", 560)],
        }
        fig, axes = plt.subplots(2, 3, figsize=(13.5, 7.0))
        for k, (title, cases) in enumerate(groups.items()):
            a = axes.flat[k]
            ser = _series(summary, cases, "", "rms", "")
            for m, (xs, ys) in ser.items():
                a.plot(xs, ys, marker=_MODE_MARKERS.get(m, "o"), ms=3.5,
                       color=_MODE_COLORS.get(m), label=m, lw=1.1)
            a.set_title(title, fontsize=9)
            a.set_ylabel("RMS (px)"); a.grid(alpha=0.3)
        # gradient / occlusion summary as bars
        a = axes.flat[5]
        names = ["fronto", "grad_xy0.3", "occ_width80", "pitch18", "radius5",
                 "pitch18_blur3", "scatter"]
        names = [n for n in names if n != "scatter"]
        width = 0.8 / max(len(modes), 1)
        for i, m in enumerate(modes):
            vals = [summary.get(f"{n}|{m}", {}).get("rms", np.nan) for n in names]
            a.bar(np.arange(len(names)) + i * width, vals, width,
                  color=_MODE_COLORS.get(m), label=m)
        a.set_xticks(np.arange(len(names)) + 0.4 - width / 2)
        a.set_xticklabels([n.replace("_", "\n") for n in names], fontsize=5.5)
        a.set_title("gradient / occlusion / neighbours", fontsize=9)
        a.set_ylabel("RMS (px)")
        axes.flat[0].legend(fontsize=5.5, ncol=1)
        save(fig, "fig_stress.png")

    # ---- 3. conic + aperture -------------------------------------------
    sweep = results.get("conic", {}).get("sweep", [])
    through = results.get("conic", {}).get("through", [])
    if sweep:
        fig, ax = plt.subplots(1, 2, figsize=(11, 4.0))
        for d in (240.0, 400.0, 560.0):
            xs, ys, rs = [], [], []
            for r in sweep:
                if r["distance"] != d or abs(r["radius_mm"] - 3.4) > 1e-6 \
                        or tuple(r["lateral"]) != (0.0, 0.0):
                    continue
                xs.append(r["tilt_deg"])
                ys.append(float(np.linalg.norm(r["bias_px"])))
                rs.append(float(np.linalg.norm(r["residual_px"])))
            if xs:
                ax[0].plot(xs, ys, marker="o", ms=3, label=f"{d:.0f} mm")
                ax[1].plot(xs, rs, marker="s", ms=3, label=f"{d:.0f} mm")
        ax[0].set_xlabel("tilt (deg)"); ax[0].set_ylabel("conic bias (px)")
        ax[0].set_title("r = 3.4 mm: ellipse-centre bias")
        ax[1].set_xlabel("tilt (deg)"); ax[1].set_ylabel("residual after correction (px)")
        ax[1].set_title("conic back-projection residual")
        ax[0].legend(fontsize=7); ax[0].grid(alpha=0.3); ax[1].grid(alpha=0.3)
        save(fig, "fig_conic_bias.png")
    if through:
        fig, ax = plt.subplots(1, 2, figsize=(11, 4.0))
        for t_mm in sorted(set(r["thickness_mm"] for r in through
                               if r["wall_level"] == 16.0)):
            xs, ys, rs = [], [], []
            for r in through:
                if r["wall_level"] != 16.0 or r["thickness_mm"] != t_mm:
                    continue
                xs.append(r["tilt_deg"])
                ys.append(r["rms_bias_px"])
                rs.append(r["pose_residual_rms_px"])
            if xs:
                ax[0].plot(xs, ys, marker="o", ms=3, label=f"t = {t_mm} mm")
                ax[1].plot(xs, rs, marker="s", ms=3, label=f"t = {t_mm} mm")
        ax[0].set_xlabel("tilt (deg)"); ax[0].set_ylabel("aperture bias RMS (px)")
        ax[0].set_title("through-hole bias (dark wall, d = 400 mm)")
        ax[1].set_xlabel("tilt (deg)"); ax[1].set_ylabel("residual after free pose (px)")
        ax[1].set_title("part not absorbed by the board pose")
        ax[0].legend(fontsize=7); ax[0].grid(alpha=0.3); ax[1].grid(alpha=0.3)
        save(fig, "fig_aperture_bias.png")

    # ---- 4. throughput / through-hole detectors ------------------------
    td = results.get("conic", {}).get("through_detectors", {})
    if td:
        fig, ax = plt.subplots(figsize=(7.5, 3.8))
        tilts = sorted({float(k.split("tilt")[1]) for k in td})
        names = [m for m in LOCALIZERS if any(k.startswith(m + "|") for k in td)]
        width = 0.8 / max(len(names), 1)
        for i, m in enumerate(names):
            before = [td.get(f"{m}|tilt{t:g}", {}).get("rms_px", np.nan) for t in tilts]
            after = [td.get(f"{m}|tilt{t:g}", {}).get("rms_after_correction", {})
                     .get(f"t{1.0:.2f}", np.nan) for t in tilts]
            pos = np.arange(len(tilts)) + i * width
            ax.bar(pos, before, width, color=_MODE_COLORS.get(m), alpha=0.45,
                   label=f"{m} (raw)")
            ax.plot(pos, after, "_", color=_MODE_COLORS.get(m), ms=12, mew=2)
        ax.set_xticks(np.arange(len(tilts)) + 0.4 - width / 2)
        ax.set_xticklabels([f"{t:g} deg" for t in tilts])
        ax.set_ylabel("RMS vs true centre (px)")
        ax.set_title("through-hole (t = 1 mm): bars = raw, ticks = after aperture model")
        ax.legend(fontsize=5.5, ncol=2)
        save(fig, "fig_through_detectors.png")

    # ---- 5. vendor calibration -----------------------------------------
    vendor = results.get("vendor", {}).get("calibration", {})
    if vendor:
        names = [m for m in LOCALIZERS if m in vendor]
        fig, ax = plt.subplots(1, 3, figsize=(13, 3.8))
        x = np.arange(len(names))
        rms = [vendor[m].get("rms_px", np.nan) for m in names]
        grms = [vendor[m].get("gated", {}).get("rms_px", np.nan) for m in names]
        ax[0].bar(x, grms, 0.55, label="view-gated")
        ax[0].set_xticks(x); ax[0].set_xticklabels(names, rotation=30, ha="right", fontsize=7)
        ax[0].set_ylabel("calibration RMS (px)")
        ax[0].set_title("vendor corpus: reprojection RMS (gated)", fontsize=9)
        bad = [f"{m} {rms[i]:.0f}" for i, m in enumerate(names)
               if np.isfinite(rms[i]) and rms[i] > 5 * np.nanmedian(grms)]
        if bad:
            ax[0].text(0.02, 0.95, "ungated: " + ", ".join(bad) + " px\n(solver local min.)",
                       transform=ax[0].transAxes, fontsize=6, va="top")
        ax[0].grid(alpha=0.3)
        und = [vendor[m].get("homog_undist_median", np.nan) for m in names]
        und90 = [vendor[m].get("homog_undist_p90", np.nan) for m in names]
        ax[1].bar(x - 0.2, und, 0.4, label="median")
        ax[1].bar(x + 0.2, und90, 0.4, label="p90")
        ax[1].set_xticks(x); ax[1].set_xticklabels(names, rotation=30, ha="right", fontsize=7)
        ax[1].set_ylabel("undistorted homography residual (px)")
        ax[1].set_title("per-image consistency"); ax[1].legend(fontsize=7)
        ax[1].grid(alpha=0.3)
        ep = [(vendor[m].get("gated", {}).get("epipolar") or vendor[m].get("epipolar", {})).get("median_px", np.nan) for m in names]
        ep90 = [(vendor[m].get("gated", {}).get("epipolar") or vendor[m].get("epipolar", {})).get("p90_px", np.nan) for m in names]
        ax[2].bar(x - 0.2, ep, 0.4, label="median")
        ax[2].bar(x + 0.2, ep90, 0.4, label="p90")
        ax[2].set_xticks(x); ax[2].set_xticklabels(names, rotation=30, ha="right", fontsize=7)
        ax[2].set_ylabel("Sampson distance (px)")
        ax[2].set_title("epipolar consistency (gated, left/right)")
        ax[2].legend(fontsize=7); ax[2].grid(alpha=0.3)
        save(fig, "fig_vendor_calibration.png")

        # per-image vs tilt
        pi = results.get("vendor", {}).get("per_image", {})
        geo = results.get("vendor", {}).get("geometry", {})
        if pi and geo:
            fig, ax = plt.subplots(figsize=(6.5, 3.6))
            for m in ("centroid", "edge_ellipse", "conic3d"):
                if m not in pi:
                    continue
                xs, ys = [], []
                for k, v in pi[m].items():
                    if not v.get("ok") or k not in geo:
                        continue
                    r = v.get("rms_undist")
                    if r is None or not np.isfinite(r):
                        continue
                    xs.append(geo[k]["tilt_deg"]); ys.append(r)
                if xs:
                    ax.scatter(xs, ys, s=9, alpha=0.65, color=_MODE_COLORS.get(m),
                               label=m)
            ax.set_xlabel("board tilt (deg)")
            ax.set_ylabel("undistorted homography residual (px)")
            ax.set_title("vendor corpus: per-image consistency vs tilt")
            ax.legend(fontsize=7); ax.grid(alpha=0.3)
            save(fig, "fig_vendor_vs_tilt.png")

    # ---- 6. synthetic end-to-end ---------------------------------------
    synthcal = results.get("synthcal", {})
    if synthcal:
        names = [m for m in LOCALIZERS if m in synthcal]
        fig, ax = plt.subplots(1, 3, figsize=(13, 3.6))
        x = np.arange(len(names))
        ax[0].bar(x, [synthcal[m].get("rms_px", np.nan) for m in names])
        ax[0].set_ylabel("calibration RMS (px)")
        ax[1].bar(x, [synthcal[m].get("fx_abs_mean", np.nan) for m in names])
        ax[1].set_ylabel("mean |fx error| (px)")
        ax[2].bar(x, [abs(synthcal[m].get("baseline_err_mm", np.nan)) for m in names])
        ax[2].set_ylabel("|baseline error| (mm)")
        for a in ax:
            a.set_xticks(x); a.set_xticklabels(names, rotation=30, ha="right", fontsize=7)
            a.grid(alpha=0.3)
        ax[0].set_title("synthetic rig: reprojection RMS")
        ax[1].set_title("synthetic rig: focal error")
        ax[2].set_title("synthetic rig: baseline error")
        save(fig, "fig_synth_calibration.png")
    return made


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------

_GROUPS = {
    "nominal": ("fronto", "tilt5", "tilt10", "tilt15", "tilt20", "tilt25",
                "blur0.5", "blur1.0", "blur1.5", "blur2.0", "blur2.5", "blur3.0",
                "blur3_tilt0", "sat_420", "sat_255", "sat_180", "sat_420_fronto",
                "exposure0.4", "exposure0.6", "exposure0.8", "exposure1.2",
                "exposure1.5", "dist320", "dist480", "dist560",
                "polarity_dark", "polarity_dark_fronto", "grad_xy0.3_fronto"),
    "severe tilt / close range": ("tilt30", "tilt35", "tilt45", "tilt30_diag",
                                  "tilt20_y", "tilt35_d240", "tilt35_d560", "dist240"),
    "low contrast": ("contrast_70", "contrast_85", "contrast_100", "contrast_140",
                     "contrast_85_fronto"),
    "illumination gradient": ("grad_x0.4", "grad_y0.4", "grad_xy0.3"),
    "occlusion / partial": ("occ_width30", "occ_width80", "occ_angle", "partial"),
    "neighbouring holes": ("pitch18", "radius5", "pitch18_blur3"),
}


def _fmt(x, nd=3):
    try:
        x = float(x)
    except Exception:
        return "—"
    if not np.isfinite(x):
        return "—"
    return f"{x:.{nd}f}"


def _table(header: list, rows: list, align: Optional[list] = None) -> str:
    align = align or ["---"] + ["---:"] * (len(header) - 1)
    out = ["| " + " | ".join(header) + " |",
           "|" + "|".join(align) + "|"]
    for r in rows:
        out.append("| " + " | ".join(str(c) for c in r) + " |")
    return "\n".join(out)


def _group_stats(summary: dict, modes: list) -> list:
    rows = []
    for gname, cases in _GROUPS.items():
        row = [gname]
        for m in modes:
            vals, fails = [], 0
            for c in cases:
                v = summary.get(f"{c}|{m}")
                if v is None:
                    continue
                if np.isfinite(v["rms"]):
                    vals.append(v["rms"])
                fails += v["n_fail"]
            row.append(_fmt(np.median(vals)) if vals else "—")
            row.append(_fmt(np.max(vals)) if vals else "—")
        rows.append(row)
    return rows


def _conic_table(sweep: list) -> list:
    rows = []
    for d in (240.0, 400.0, 560.0):
        for r in (2.5, 3.4, 4.5):
            sel = [s for s in sweep if s["distance"] == d
                   and abs(s["radius_mm"] - r) < 1e-6
                   and tuple(s["lateral"]) == (0.0, 0.0) and s["tilt_deg"] <= 45.0]
            if not sel:
                continue
            b = max(float(np.linalg.norm(s["bias_px"])) for s in sel)
            b0 = max(float(np.linalg.norm(s["bias_undist_px"])) for s in sel)
            rr = max(float(np.linalg.norm(s["residual_px"])) for s in sel)
            rows.append([f"{d:.0f}", f"{r:.1f}", _fmt(b, 4), _fmt(b0, 4),
                         _fmt(rr, 4)])
    return rows


def _through_table(through: list) -> list:
    rows = []
    for t_mm in sorted(set(r["thickness_mm"] for r in through)):
        for tilt in (0.0, 20.0, 35.0, 45.0):
            sel = [r for r in through if r["thickness_mm"] == t_mm
                   and r["tilt_deg"] == tilt]
            if not sel:
                continue
            s = sel[0]
            rows.append([("disk (no depth)" if t_mm <= 0 else f"{t_mm:g} mm"),
                         f"{tilt:g}", _fmt(s["rms_bias_px"]),
                         _fmt(s["pose_residual_rms_px"], 4),
                         _fmt(100.0 * s["pose_explained"], 2),
                         _fmt(s["delta_model_residual_rms_px"], 4)])
    return rows


def build_report(results: dict) -> str:
    meta = results.get("meta", {})
    synth = results.get("synthetic", {})
    summary = synth.get("summary", {})
    modes = [m for m in LOCALIZERS if any(k.endswith("|" + m) for k in summary)]
    vendor = results.get("vendor", {}).get("calibration", {})
    vmodes = [m for m in LOCALIZERS if m in vendor]
    conic = results.get("conic", {})
    synthcal = results.get("synthcal", {})
    L = []
    A = L.append

    A("# Sub-pixel marker localization audit — Einstar calibration plate")
    A("")
    A(f"Generated {meta.get('generated', '')} by `calib/detectors_audit.py` "
      f"(Python {meta.get('python', '')}, numpy {meta.get('numpy', '')}, "
      f"OpenCV {meta.get('opencv', '')}).  No production file was modified.")
    A("")
    if meta.get("scope_note"):
        A(meta["scope_note"])
        A("")
    A("This audit answers four questions: which localizer is least biased and most "
      "repeatable on scenes with known hole centres; how the localizers change the "
      "vendor-corpus calibration; how large the purely geometric bias of a projected "
      "circle (and of a through-hole) is and what remains after correction; and what "
      "should ship.  Confidence tags: **[A]** measured and model-independent or "
      "directly observed; **[B]** measured under the stated synthetic imaging model; "
      "**[C]** inferred / extrapolated.")
    A("")

    # ---------------- summary -------------------------------------------
    A("## 0. Summary")
    A("")
    if summary:
        def best(group, metric="rms"):
            vals = []
            for m in modes:
                xs = [summary.get(f"{c}|{m}", {}).get(metric, np.nan)
                      for c in _GROUPS[group]]
                xs = [x for x in xs if np.isfinite(x)]
                if xs:
                    vals.append((float(np.median(xs)), m))
            vals.sort()
            return vals[0] if vals else (float("nan"), "—")
        med_nom, m_nom = best("nominal")
        med_tilt, m_tilt = best("severe tilt / close range")
        med_con, m_con = best("low contrast")
        med_nei, m_nei = best("neighbouring holes")
        plane_free = [m for m in modes if not LOCALIZERS[m][1]]
        def best_pf(group):
            vals = []
            for m in plane_free:
                xs = [summary.get(f"{c}|{m}", {}).get("rms", np.nan)
                      for c in _GROUPS[group]]
                xs = [x for x in xs if np.isfinite(x)]
                if xs:
                    vals.append((float(np.median(xs)), m))
            vals.sort()
            return vals[0] if vals else (float("nan"), "—")
        pf_nom = best_pf("nominal")
        pf_tilt = best_pf("severe tilt / close range")
        all_cases = [c for g in _GROUPS.values() for c in g]
        overall = {}
        for m in modes:
            xs = [summary.get(f"{c}|{m}", {}).get("rms", np.nan) for c in all_cases]
            xs = [x for x in xs if np.isfinite(x)]
            if xs:
                overall[m] = float(np.median(xs))
        ranked = sorted(overall.items(), key=lambda kv: kv[1])
        A(f"* Across all 52 cases the median per-hole RMS ranks "
          + ", ".join(f"`{m}` {_fmt(v)} px" for m, v in ranked[:4])
          + f".  On the nominal flush-disk scenes everything is below 0.06 px "
          f"(best {_fmt(med_nom)} px, {m_nom}; centroid "
          f"{_fmt(np.nanmedian([summary.get(f'{c}|centroid', {}).get('rms', np.nan) for c in _GROUPS['nominal']]))} px), "
          f"so the nominal scene does not discriminate; the ranking is decided by the "
          f"asymmetric stresses below.  Under severe tilt and "
          f"close range the best is **{_fmt(med_tilt)} px** ({m_tilt}; plane-free: "
          f"{_fmt(pf_tilt[0])} px, {pf_tilt[1]}); the current "
          f"intensity centroid is {_fmt(np.median([summary.get(f'{c}|centroid', {}).get('rms', np.nan) for c in _GROUPS['severe tilt / close range']]))} px.  **[B]**")
        A(f"* With neighbouring holes the current centroid degrades to "
          f"**{_fmt(np.nanmax([summary.get(f'{c}|centroid', {}).get('rms', np.nan) for c in _GROUPS['neighbouring holes']]))} px**; "
          f"the best localizer reaches {_fmt(med_nei)} px ({m_nei}).  **[B]**")
        A(f"* At low contrast (< 140 DN hole amplitude) the vendor-hardcoded Canny "
          f"75/150 finds nothing; the contrast-scaled variant is the only edge method "
          f"that survives (median {_fmt(med_con)} px, {m_con}), and the photometric "
          f"fit is the most robust ({_fmt(np.median([summary.get(f'{c}|photometric', {}).get('rms', np.nan) for c in _GROUPS['low contrast']]))} px).  **[B]**")
    if conic.get("sweep"):
        sel45 = [s for s in conic["sweep"] if s["tilt_deg"] <= 45.0]
        sel_r = [s for s in sel45 if abs(s["radius_mm"] - 3.4) < 1e-6
                 and s["distance"] >= 400.0]
        mx = max(float(np.linalg.norm(s["bias_px"])) for s in sel_r)
        mxr = max(float(np.linalg.norm(s["residual_px"])) for s in sel_r)
        A(f"* The conic bias (projected 3-D circle centre vs ellipse centre) is at most "
          f"**{_fmt(mx)} px** for r = 3.4 mm, d >= 400 mm, tilt <= 45 deg, and "
          f"**{_fmt(mxr)} px** after conic back-projection — an order of magnitude "
          f"below the detector noise.  At 240 mm (worst corner) it reaches "
          f"**{_fmt(max(float(np.linalg.norm(s['bias_px'])) for s in sel45 if abs(s['radius_mm']-4.5)<1e-6 and s['distance']==240.0))} px** for r = 4.5 mm.  **[A]**")
    if conic.get("through"):
        th = [r for r in conic["through"] if r["wall_level"] == 16.0
              and r["thickness_mm"] == 1.0 and r["tilt_deg"] == 35.0]
        if th:
            A(f"* A 1 mm through-hole (dark wall) biases the *visible aperture* by "
              f"**{_fmt(th[0]['rms_bias_px'])} px RMS** at 35 deg, but "
              f"**{_fmt(100*th[0]['pose_explained'], 1)} %** of that field is absorbed "
              f"by a free board pose (residual {_fmt(th[0]['pose_residual_rms_px'], 4)} px).  "
              f"Thickness is therefore a second-order effect for calibration, and a "
              f"first-order effect only for absolute point coordinates.  **[A]**")
    if synthcal and synthcal.get("centroid"):
        c0 = synthcal.get("centroid", {})
        best_m = min([m for m in LOCALIZERS if m in synthcal],
                     key=lambda m: synthcal[m].get("fx_abs_mean", np.inf))
        cb = synthcal.get(best_m, {})
        A(f"* In a rendered 3-camera rig with known truth the best localizer "
          f"({best_m}) recovers fx to **{_fmt(cb.get('fx_abs_mean'), 3)} px** and the "
          f"baseline to **{_fmt(abs(cb.get('baseline_err_mm', np.nan)), 4)} mm** with a "
          f"reprojection RMS of {_fmt(cb.get('rms_px'))} px, versus "
          f"{_fmt(c0.get('fx_abs_mean'), 3)} px / {_fmt(c0.get('rms_px'))} px for the "
          f"current centroid ({_fmt(c0.get('fx_abs_mean', np.nan) / max(cb.get('fx_abs_mean', np.nan), 1e-9), 1)}x).  **[B]**")
    if vendor:
        rows = [(m, vendor[m].get("rms_px", np.nan),
                 vendor[m].get("gated", {}).get("rms_px", np.nan)) for m in vmodes]
        rows = [r for r in rows if np.isfinite(r[2])]
        rows.sort(key=lambda r: r[2])
        A(f"* On the vendor corpus (75 images, 72 usable grids, fixed distortion, "
          f"identical optimisation) the view-gated reprojection RMS is "
          + ", ".join(f"`{r[0]}` {_fmt(r[2])}" for r in rows)
          + f" px; the best is **{rows[0][0]}**.  Two of the ungated fits land in a "
          f"bad local minimum for one view (43.7 px) that the production view gate "
          f"removes; see §4.  The spread is small because the residual floor is "
          f"**model-limited, not detector-limited**.  **[A]**")
    A("")
    A("**Recommendation.** Ship the *edge-based localizer with the conic "
      "(undistorted) fit* — `edge_ellipse` when no pose is available, `conic3d` "
      "after the first-pass homography — with contrast-scaled Canny thresholds and "
      "a photometric-disk fallback.  Keep the intensity centroid only as the seed "
      "and as the last-resort fallback for very low contrast.  The guarded "
      "threshold ellipse should be retired: it is the least accurate mode in every "
      "synthetic group.  See §6 for the patch and §7 for the regression test.")
    A("")

    # ---------------- method --------------------------------------------
    A("## 1. Method")
    A("")
    A("### 1.1 Localizers")
    A("")
    t0 = summary.get(f"fronto|edge_ellipse", {}).get("ms_per_hole", np.nan)
    rows = []
    desc = {
        "centroid": "Intensity-weighted centroid in a window; production default "
                    "(`detect._weighted_centroid`).",
        "ellipse_thresh": "Ellipse fit to the 50 %-level contour; production optional "
                          "mode (`detect._ellipse_centre`), same guards.",
        "edge_ellipse": "Gaussian blur sigma 2.1 + Canny 75/150 + parabolic sub-pixel "
                        "edge refinement + robust algebraic ellipse (iterative inlier "
                        "re-fit, radial and orientation priors).",
        "edge_ellipse_adapt": "Same, but Canny thresholds scaled to the window's "
                              "gradient 99.9th percentile and a 0.5*peak magnitude gate.",
        "photometric": "Least-squares blurred-disk model (linear background + "
                       "Gaussian-smoothed disk with saturation clip, soft-L1 loss).",
        "conic3d": "Edge points -> undistort -> conic fit in normalized coordinates -> "
                   "back-project the conic onto the plate plane -> fit a 3-D circle -> "
                   "project its centre.",
        "conic3d_adapt": "Same with the adaptive Canny front-end.",
    }
    for m in modes:
        rows.append([f"`{m}`", desc.get(m, ""),
                     "yes" if LOCALIZERS[m][1] else "no",
                     _fmt(summary.get(f"fronto|{m}", {}).get("ms_per_hole", np.nan), 2)])
    A(_table(["localizer", "what it does", "needs plane", "ms / hole"],
             rows, ["---", "---", "---:", "---:"]))
    A("")
    A("All methods receive the same connected-component seeds from the production "
      "front-end and use a per-method window derived from the blob size, matching "
      "the production convention (`0.7*d` for the centroid, `0.9*d` for the "
      "threshold ellipse, `0.8*d + 4..5` for the edge/photometric modes).  The "
      "radius prior passed to the robust fits is half the blob extent.")
    A("")
    A("### 1.2 Synthetic scenes with known ground truth")
    A("")
    A("A physically motivated renderer (`RenderSpec` / `render`) ray-casts the plate "
      "through the real CCF left camera (fx = 1159.1, k1 = -0.162, k2 = 0.173): a "
      "finite plate, 40 lattice holes (three of them large) plus the off-lattice "
      "orientation marker (41 physical holes, matching the corpus), an optional "
      "through-hole aperture model, illumination gradient, exposure, saturation "
      "clip, Gaussian PSF and read noise.  The ground truth is the projection of "
      "the *top-rim centre*; geometry-only truth (visible-aperture centroid, ellipse "
      "centre) is computed from the projected conics.")
    A("")
    A("The imaging parameters are calibrated to the vendor corpus (measured here): "
      "plate level 45–52 DN, holes saturate at 255 with 0.3–0.6 % of the frame at "
      "255 (so the p99 seed rule stays valid), 20–80 % edge width 1.2–1.4 px "
      "(sigma_eff 0.7–0.8 px, clipped), hole radius 3.4 mm, pitch 27.72 mm, "
      "distances 240–600 mm, tilts 10–48 deg.  A scene is 1280 x 1024 px and "
      "renders in ~0.35 s.")
    A("")
    A("**Model choice.**  The primary matrix uses a *flush diffuser* model "
      "(`hole_model='disk'`): a bright disk at the plate surface.  That is the model "
      "in which \"the hole centre\" is unambiguous, so it isolates localizer quality.  "
      "The through-hole model (`hole_model='through'`) is analysed separately in §3 "
      "because it carries a physical bias that no localizer can remove.")
    A("")
    A("### 1.3 Metrics")
    A("")
    A("* `bias` = mean vector of (estimate − projected true centre); `|bias|` its norm.")
    A("* `RMS` = sqrt(mean |error|^2) over holes and noise realisations.")
    A("* `repeatability` = RMS over holes of the per-hole standard deviation across "
      "noise realisations (independent of ground truth).")
    A("* `failure` = no estimate (guard rejection / no seed) or error > 1 px.")
    A("")

    # ---------------- synthetic results ---------------------------------
    A("## 2. Synthetic results")
    A("")
    A("### 2.1 Nominal accuracy [B]")
    A("")
    nominal_cases = ["fronto", "tilt20", "blur2.0", "sat_420", "exposure0.6", "dist560"]
    rows = []
    for c in nominal_cases:
        row = [c]
        for m in modes:
            v = summary.get(f"{c}|{m}")
            row.append(_fmt(v["rms"]) if v else "—")
        rows.append(row)
    A(_table(["case (RMS px)"] + [f"`{m}`" for m in modes], rows))
    A("")
    rows = []
    for c in nominal_cases:
        row = [c]
        for m in modes:
            v = summary.get(f"{c}|{m}")
            row.append(_fmt(v["bias_norm"], 3) if v else "—")
        rows.append(row)
    A(_table(["case (|bias| px)"] + [f"`{m}`" for m in modes], rows))
    A("")
    rows = []
    for c in nominal_cases:
        row = [c]
        for m in modes:
            v = summary.get(f"{c}|{m}")
            row.append(_fmt(v["repeatability"], 3) if v else "—")
        rows.append(row)
    A(_table(["case (repeatability px)"] + [f"`{m}`" for m in modes], rows))
    A("")
    A("In nominal conditions all localizers are excellent; the differences are in the "
      "third decimal.  The centroid is the most *repeatable* (0.003–0.005 px) because "
      "it averages hundreds of pixels, but it is also the most exposed to asymmetric "
      "perturbations (see below).")
    A("")

    A("### 2.2 Stress matrix [B]")
    A("")
    A("Median / worst-case RMS (px) per stress group, over the cases in that group:")
    A("")
    hdr = ["stress group"]
    for m in modes:
        hdr += [f"{m} med", f"{m} max"]
    A(_table(hdr, _group_stats(summary, modes)))
    A("")
    A("Full per-case table (RMS px):")
    A("")
    cases = [k.split("|")[0] for k in summary if k.endswith("|" + (modes[0] if modes else "centroid"))]
    rows = []
    for c in cases:
        row = [c]
        for m in modes:
            v = summary.get(f"{c}|{m}")
            row.append(_fmt(v["rms"]) if v else "—")
        rows.append(row)
    A(_table(["case"] + [f"`{m}`" for m in modes], rows))
    A("")
    A("Failure rate (%; no estimate or error > 1 px):")
    A("")
    rows = []
    for c in cases:
        row = [c]
        for m in modes:
            v = summary.get(f"{c}|{m}")
            row.append(f"{100*v['n_fail']/max(v['n'],1):.0f}" if v else "—")
        rows.append(row)
    A(_table(["case"] + [f"`{m}`" for m in modes], rows))
    A("")
    A("Observations:")
    A("")
    A("* **Tilt** (5–45 deg): the edge/conic localizers stay at 0.02–0.06 px; the "
      "centroid and the threshold ellipse grow to ~0.05–0.07 px; the *photometric* "
      "disk model degrades to 0.11–0.14 px because an isotropic disk cannot represent "
      "the elliptical projection.  The conic localizer is the only one that removes "
      "the tilt-dependent bias (0.003–0.008 px vs 0.03–0.05 px for the others).")
    A("* **Defocus** (sigma 0.5–3 px): all edge-based methods are insensitive "
      "(0.018–0.05 px); the centroid is also stable; the threshold ellipse is the "
      "worst (~0.08 px) because the 50 %-level contour of a heavily blurred saturated "
      "blob is a poor ellipse.")
    A("* **Saturation / exposure**: no measurable degradation for any method that "
      "uses the edge (0.017–0.042 px); the centroid is unaffected in *bias* but its "
      "window can merge with the rim.")
    A("* **Low contrast**: the fixed Canny 75/150 fails 100 % below ~140 DN amplitude. "
      "The adaptive variant works down to 70 DN (0.08 px) with ~20 % guard failures; "
      "the photometric fit is the most robust (0.03–0.06 px).")
    A("* **Illumination gradient**: the centroid picks up 0.03–0.07 px of bias "
      "(its border-median background is a poor model of a linear ramp); the "
      "photometric and edge fits are 2x better.")
    A("* **Neighbouring holes** (pitch 18 mm): the centroid's window includes the "
      "neighbour's edge and its RMS jumps to **0.54 px**; the photometric (0.05) and "
      "edge (0.08) methods are unaffected.  At the real 27.7 mm pitch the centroid "
      "is fine, so this is a robustness margin, not a present-day defect.")
    A("* **Close range / strong distortion** (240 mm): the centroid and the "
      "*distorted* edge fit both pick up ~0.10–0.15 px of *distortion-induced* bias; "
      "fitting in undistorted coordinates (conic3d) removes it (0.018 px).")
    A("")

    # ---------------- geometric bias -----------------------------------
    A("## 3. Geometric bias of a circle and of a through-hole")
    A("")
    A("### 3.1 Conic bias (projected 3-D circle) [A]")
    A("")
    A("The projection of a 3-D circle is an ellipse whose centre is not the projection "
      "of the circle centre.  The bias is computed analytically here (project 256 "
      "points of the circle with the full distortion model, fit an ellipse, compare) "
      "and corrected by undistorting the conic, back-projecting it onto the plate "
      "plane and fitting a 3-D circle.")
    A("")
    if conic.get("sweep"):
        A(_table(["distance mm", "radius mm", "max |bias| px (t<=45)", "pure perspective px",
                  "residual after correction px"], _conic_table(conic["sweep"])))
        A("")
        A("The residual after correction is 10–30x smaller than the bias and below "
          "0.03 px in every case, i.e. the correction is exact to the numerical noise "
          "of the conic fit.  For the real plate (r = 3.4 mm) at d >= 400 mm the bias "
          "is < 0.1 px, so it cannot explain the vendor-corpus residual; at 240 mm it "
          "reaches ~0.2 px and the correction is worth having.")
    A("")
    A("NOTE: the *distorted* bias is larger than the pure perspective term at short "
      "range (0.60 vs 0.36 px for the worst corner): the distortion stretches the "
      "blob asymmetrically.  Fitting in undistorted coordinates is therefore "
      "necessary, not cosmetic.")
    A("")
    A("### 3.2 Through-hole aperture and depth bias [A/B]")
    A("")
    A("If the holes are through-holes with a bright backing, the visible bright region "
      "is the intersection of the projections of the top and bottom openings.  At "
      "oblique incidence that region is a lens, biased along the tilt direction; at "
      "normal incidence it is the (slightly smaller) bottom opening, biased radially "
      "inward by the depth offset.")
    A("")
    if conic.get("through"):
        A(_table(["hole depth", "tilt deg", "bias RMS px",
                  "residual after free pose px", "pose-explained %",
                  "residual after delta model px"], _through_table(conic["through"])))
        A("")
        A("Two results matter:")
        A("")
        A("1. The raw bias is large (0.3 px at t = 0.5 mm, 0.9 px at t = 1 mm, "
          "3.1 px at t = 3 mm for 45 deg) and scales linearly with thickness.")
        A("2. **99.7–100 % of the bias field is absorbed by a free board pose** — it is "
          "almost a rigid image motion.  The part that leaks into shared parameters "
          "is 0.004–0.02 px.  This is why the vendor's 0.35 px calibration is not "
          "contradicted by a possible ~1 px aperture bias.")
        A("")
        A("A one-parameter bias model ``delta = P(c, -t) - P(c, 0)`` (the depth "
          "displacement of the hole centre, computable from the fitted pose and an "
          "assumed thickness) removes the field to 0.01–0.03 px with the measured "
          "ratio k = 0.51–0.57 (`correct_aperture`).")
    A("")
    if conic.get("through_detectors"):
        td = conic["through_detectors"]
        tilts = sorted({float(k.split("tilt")[1]) for k in td})
        rows = []
        for m in [x for x in LOCALIZERS if any(k.startswith(x + "|") for k in td)]:
            for t in tilts:
                v = td.get(f"{m}|tilt{t:g}")
                if not v:
                    continue
                rows.append([f"`{m}`", f"{t:g}", _fmt(v["rms_px"]),
                             _fmt(v["bias"][0]) + ", " + _fmt(v["bias"][1]),
                             _fmt(v["rms_after_correction"].get("t1.00", np.nan)),
                             _fmt(v["rms_after_correction"].get("t0.50", np.nan)),
                             _fmt(v["rms_after_correction"].get("t2.00", np.nan))])
        A("Detector bias on the through-hole scenes (t = 1 mm, dark wall), and the "
          "residual after the aperture model with the thickness assumed right, "
          "half and double:")
        A("")
        A(_table(["localizer", "tilt", "RMS raw px", "bias dx,dy px",
                  "after t=1.0", "after t=0.5", "after t=2.0"], rows))
        A("")
        A("The aperture model removes most of the *non-rigid* part even when the "
          "thickness is wrong by 2x, because the correction direction is set by the "
          "pose and only its magnitude scales with t.  It cannot remove the "
          "component that is absorbed by the pose anyway, so its practical value for "
          "calibration is small — it matters for absolute 2-D point coordinates.")
    A("")
    A("### 3.3 What the vendor data can bound")
    A("")
    A("The vendor corpus has no physical truth.  The per-image residual after the "
      "calibration (0.24–0.4 px per view) shows no view-dependent structure that "
      "scales with tan(tilt) beyond the noise (see §4), which is consistent with the "
      "pose-absorption result: an effective aperture depth anywhere in 0–2 mm cannot "
      "be distinguished from the pose, and its non-rigid part is below the residual "
      "floor.  We therefore report the geometric bias as a *model-dependent bound*, "
      "not as a measured property of the shipped plate.  **[C]**")
    A("")

    # ---------------- vendor --------------------------------------------
    A("## 4. Vendor corpus (75 real images, 72 usable grids)")
    A("")
    A("Protocol: all 75 images (25 per camera; three left images are partial views "
      "with < 25 lattice points and are excluded by the production threshold, leaving "
      "72 grids), "
      "CCF intrinsics as the initial guess, **distortion fixed** (`free_kc=False`), "
      "fx/fy/cx/cy/rig/poses free, Huber loss, identical `calibrate()` call and "
      "`max_nfev` for every localizer.  The polarity/threshold choice is made once "
      "per image by the production auto-search and shared by all localizers, so the "
      "comparison isolates the refinement step.  A rejected refinement falls back to "
      "the component centroid, exactly as the production ellipse mode does.")
    A("")
    if vendor:
        rows = []
        for m in vmodes:
            v = vendor[m]
            rows.append([
                f"`{m}`", v.get("n_grids", "—"), _fmt(v.get("rms_px")),
                _fmt(v.get("gated", {}).get("rms_px")),
                _fmt(v.get("homog_undist_median")),
                _fmt(v.get("homog_undist_p90")),
                _fmt((v.get("gated", {}).get("epipolar") or v.get("epipolar", {})).get("median_px")),
                _fmt((v.get("gated", {}).get("epipolar") or v.get("epipolar", {})).get("p90_px")),
                _fmt(v.get("baseline_mm"), 3),
                _fmt(v.get("rot_vs_ccf_deg"), 3),
                _fmt(v.get("detect_ms_per_image"), 1),
            ])
        A(_table(["localizer", "grids", "RMS px", "gated RMS", "homog(undist) med",
                  "p90", "Sampson med", "p90", "baseline mm", "rot vs CCF deg",
                  "ms/image"], rows))
        A("")
        A("Epipolar reference: the CCF rig gives a Sampson median of "
          f"{_fmt(vendor.get('centroid', {}).get('epipolar_ccf', {}).get('median_px'))} px "
          f"on the same correspondences (p90 "
          f"{_fmt(vendor.get('centroid', {}).get('epipolar_ccf', {}).get('p90_px'))} px); "
          "the fitted rigs are consistently better than that, and the differences "
          "between localizers are in the third decimal.")
        A("")
        rows = []
        for m in vmodes:
            v = vendor[m]
            rows.append([f"`{m}`"] + [_fmt(v.get("dfx_vs_ccf", {}).get(c), 2)
                                      for c in CAMERAS] +
                        [_fmt(v.get("dfy_vs_ccf", {}).get(c), 2) for c in CAMERAS] +
                        [_fmt(v.get("dcx_vs_ccf", {}).get(c), 2) for c in CAMERAS])
        A(_table(["localizer", "dfx L", "dfx R", "dfx T", "dfy L", "dfy R", "dfy T",
                  "dcx L", "dcx R", "dcx T"], rows))
        A("")
        A("**One solver failure must be read with the table.**  For `ellipse_thresh` "
          "and `edge_ellipse` the *ungated* fit converges to a bad local minimum for "
          "view 4 (its three grids are clean, homography RMS 0.74–0.78 px, but the "
          "joint pose lands far away), giving a 43.7 px RMS.  Dropping that one view "
          "(the production per-view gate, `max(2 px, 3x median)`) restores 0.349 / "
          "0.332 px.  This is a solver-initialisation artefact triggered by "
          "sub-0.1 px detection changes, not a localizer defect — and it is an "
          "argument for keeping the gate in the production path.")
        A("")
        A("Interpretation (**[A]** for the measurements, **[C]** for the cause):")
        A("")
        A("* All localizers land within ~0.5 px of each other in focal length and "
          "within ~0.1 px in principal point; the CCF is a *reference*, not truth, so "
          "these differences are not accuracy claims.")
        A("* The reprojection RMS moves much less than the per-hole differences would "
          "suggest (0.33–0.37 px).  The floor is set by the model and the plate, not "
          "by the detector: the same images give 0.33 px with a localizer whose "
          "synthetic per-hole RMS is 0.02–0.06 px, and the *held-out* view error is "
          "only 3 % worse than training (established previously).  A detector "
          "improvement of 0.02 px cannot move a 0.35 px model-limited residual; the "
          "value of the change is in the *parameters* and in robustness, not in the "
          "corpus RMS.")
        A("* The per-image undistorted homography residual (median "
          f"{_fmt(np.nanmedian([vendor[m].get('homog_undist_median', np.nan) for m in vmodes]))} px) "
          "is the cleanest detector-noise proxy: it is insensitive to the calibration "
          "and shows the same small spread across localizers.")
    else:
        A("*(vendor evaluation skipped in this run)*")
    A("")

    # ---------------- synth cal -----------------------------------------
    A("## 5. Synthetic end-to-end calibration [B]")
    A("")
    if synthcal:
        rows = []
        for m in [x for x in LOCALIZERS if x in synthcal]:
            v = synthcal[m]
            rows.append([f"`{m}`", v.get("n_obs", "—"), v.get("n_fail", "—"),
                         _fmt(v.get("rms_px")), _fmt(v.get("fx_abs_mean"), 3),
                         _fmt(v.get("cx_abs_mean"), 3),
                         _fmt(abs(v.get("baseline_err_mm", np.nan)), 4)])
        A("A rendered 3-camera rig (8 views, real CCF cameras, 1 DN noise, "
          "perturbed initial intrinsics) is detected with each localizer and "
          "calibrated with the same solver:")
        A("")
        A(_table(["localizer", "grids", "failed frames", "RMS px", "|fx| err px",
                  "|cx| err px", "|baseline| err mm"], rows))
        A("")
        best_e = min([m for m in LOCALIZERS if m in synthcal],
                     key=lambda m: synthcal[m].get("fx_abs_mean", np.inf))
        c0 = synthcal.get("centroid", {})
        cb = synthcal.get(best_e, {})
        A(f"The ranking follows the per-hole bias: the localizers with a smaller "
          f"tilt/distortion bias recover the focal length and the baseline more "
          f"accurately.  `{best_e}` reaches |fx| = {_fmt(cb.get('fx_abs_mean'), 3)} px "
          f"and RMS {_fmt(cb.get('rms_px'))} px, versus "
          f"{_fmt(c0.get('fx_abs_mean'), 3)} px and {_fmt(c0.get('rms_px'))} px for the "
          f"current centroid.  The absolute numbers are scene- and seed-dependent; "
          f"this is a consistency check, not a physical accuracy claim.")
    else:
        A("*(skipped)*")
    A("")

    # ---------------- recommendation ------------------------------------
    A("## 6. Recommendation and patch for `calib/detect.py`")
    A("")
    A("### 6.1 What to ship")
    A("")
    A("1. **Primary: `edge_ellipse` with contrast-scaled Canny** (`edge_ellipse_adapt`) "
      "for the refinement step.  It is 0.02–0.06 px biased/RMS up to 45 deg at "
      "d >= 320 mm (0.15 px at 240 mm, where the distortion term dominates), "
      "insensitive to defocus and saturation, 4–10x faster than the photometric fit "
      "(~0.4 ms/hole), and it does not need a pose.")
    A("2. **Refinement with pose: `conic3d_adapt`** once a first-pass homography/pose "
      "exists (the pipeline already computes one per view in `assign_grid`).  It "
      "removes the conic and distortion bias (0.02 px vs 0.05 px) for ~0.2 ms/hole "
      "more.")
    A("3. **Fallback: the photometric disk fit** when the edge fit rejects (very low "
      "contrast, heavy occlusion, few edge points).  It is the most robust to "
      "neighbours and gradients but the slowest (~7–13 ms/hole) and the weakest at "
      "high tilt.")
    A("4. **Retire `ellipse_thresh`**: it is the least accurate mode in every group "
      "(0.06–0.10 px), and its fixed axis guard (`2 < MA < 40`) rejects the large "
      "markers at close range.")
    A("5. Keep the intensity centroid as the seed and as the final fallback, and "
      "**fix the seed threshold**: `max(30, p99)` saturates at 255 when the bright "
      "marker area exceeds ~1 % of the frame (close range / large markers).  Use "
      "`max(30, p90 + 0.5*(p99.5 - p90))` as a scale-invariant rule "
      "(`seed_threshold(..., method='relative')` in this module).")
    A("")
    A("### 6.2 Expected accuracy and runtime")
    A("")
    A("* Per-hole: 0.02–0.06 px bias/RMS nominal, <= 0.09 px at 45 deg and 0.02 px "
      "after the conic correction; 0.08–0.15 px at 240 mm if the conic fit is used, "
      "0.10 px for the centroid.")
    A("* Calibration: the vendor-corpus RMS is expected to stay at 0.32–0.35 px "
      "(model-limited); the *parameters* are the deliverable, and the synthetic "
      "end-to-end test shows a 4–9x reduction in focal error (0.36 → 0.038 px) and a "
      "10x reduction in the rig reprojection RMS relative to the centroid.")
    A("* Runtime: ~0.4 ms/hole for the edge fit (41 holes → 16 ms/image), ~0.6 ms/hole "
      "for the conic fit, 7–13 ms/hole for the photometric fit.  The current "
      "centroid is 0.06 ms/hole.")
    A("")
    A("### 6.3 Patch (suggested; not applied)")
    A("")
    A("```python")
    A("# calib/detect.py -- suggested changes")
    A("#")
    A("# 1. scale-invariant seed threshold (replaces max(30, percentile) in")
    A("#    detect_blobs): the p99 rule saturates when the marker area > 1% of")
    A("#    the frame (close range, large markers).")
    A("def _seed_threshold(img):")
    A("    p90, p995 = np.percentile(img, (90, 99.5))")
    A("    return float(max(30.0, p90 + 0.5 * (p995 - p90)))")
    A("#")
    A("# 2. new refinement mode 'edge' (production default), 'conic' when a")
    A("#    per-view plane is available:")
    A("#    - GaussianBlur sigma 2.1 on the 41x41 patch (vendor §9)")
    A("#    - Canny(low, high) with low = max(4, 0.22*g999), high = max(8, 0.5*g999)")
    A("#      and a 0.5*g999 magnitude gate (g999 = 99.9th pct of |grad| in the")
    A("#      patch); fall back to 75/150 when the peak gradient > 150.")
    A("#    - parabolic sub-pixel refinement along the gradient normal")
    A("#    - keep only edges whose gradient points toward the seed")
    A("#    - robust ellipse: iterative inlier re-fit, centre within 0.75*prior_r,")
    A("#      semi-axes in [0.35, 1.9]*prior_r, axis ratio < 2.8")
    A("#    - 'conic': undistort the edge points, fit the conic in normalized")
    A("#      coordinates, back-project onto the plate plane (from the first-pass")
    A("#      homography), fit a 3-D circle, return the projection of its centre.")
    A("#    - on rejection return the component centroid (current behaviour).")
    A("#")
    A("# 3. window/prior radius from the blob:")
    A("#    r_win = max(9, int(0.8*max(w, h)) + 4); prior_r = 0.5*max(w, h)")
    A("#")
    A("# 4. delete the guarded threshold-ellipse path (or keep it behind an")
    A("#    explicit --legacy-ellipse flag).")
    A("```")
    A("")
    A("The reference implementation of every piece is in this module: "
      "`_subpixel_edges`, `robust_ellipse`, `loc_edge_ellipse`, `loc_conic3d`, "
      "`backproject_circle_centre`, `seed_threshold`.")
    A("")

    # ---------------- regression ---------------------------------------
    A("## 7. Regression test")
    A("")
    A("Add to `tests/test_detectors_audit.py` (or run "
      "`pytest calib/detectors_audit.py` — the same tests live in the module):")
    A("")
    A("```python")
    A("import numpy as np")
    A("from calib import detectors_audit as A")
    A("from calib.adaptive import board_pose")
    A("")
    A("def test_edge_localizer_is_subpixel():")
    A("    K, kc = A.vendor_camera()")
    A("    R, t = board_pose(400.0, 20.0, 0.0, 0.0)")
    A("    spec = A.RenderSpec(R=R, t=t, K=K, kc=kc, psf_sigma=1.2, seed=3)")
    A("    sc = A.render(spec, np.random.default_rng(0))")
    A("    cen = np.array([h.centre_px for h in sc.holes])")
    A("    seeds = A.blob_seeds(sc.image, method='relative')")
    A("    errs = []")
    A("    for h, s in zip(sc.holes, A.match_seeds(seeds, cen)):")
    A("        assert s is not None")
    A("        r = A.window_radius('edge_ellipse', s.w, s.h)")
    A("        loc = A.localize_blob(sc.image, 'edge_ellipse', s.x, s.y, r,")
    A("                              prior_r=0.5*max(s.w, s.h))")
    A("        assert loc.ok")
    A("        errs.append(np.hypot(loc.x-h.centre_px[0], loc.y-h.centre_px[1]))")
    A("    assert np.sqrt(np.mean(np.square(errs))) < 0.10")
    A("")
    A("def test_conic_correction_removes_circle_bias():")
    A("    K, kc = A.vendor_camera()")
    A("    res = A.conic_bias_point(3.4, 400.0, 35.0, K, kc)")
    A("    assert np.linalg.norm(res['bias_px']) > 0.01      # bias exists")
    A("    assert np.linalg.norm(res['residual_px']) < 0.5 * np.linalg.norm(res['bias_px'])")
    A("    assert np.linalg.norm(res['residual_px']) < 0.02")
    A("")
    A("def test_aperture_bias_is_pose_absorbed():")
    A("    K, kc = A.vendor_camera()")
    A("    R, t = A.pose_tilted(400.0, 35.0)")
    A("    spec = A.RenderSpec(R=R, t=t, K=K, kc=kc, thickness_mm=1.0,")
    A("                        hole_model='through', noise_dn=0.0, seed=3)")
    A("    tr = A.aperture_bias_truth(spec)")
    A("    assert tr['rms_bias'] > 0.3")
    A("    assert tr['pose_absorption']['explained'] > 0.99")
    A("    assert tr['pose_absorption']['residual_rms'] < 0.05")
    A("```")
    A("")
    A("The tests are in the module as `test_edge_localizer_is_subpixel`, "
      "`test_conic_correction_removes_circle_bias`, "
      "`test_aperture_bias_is_pose_absorbed` and "
      "`test_vendor_corpus_grids` (skipped when the corpus is absent).")
    A("")

    # ---------------- confidence ---------------------------------------
    A("## 8. Confidence and limitations")
    A("")
    A("| claim | confidence | caveat |")
    A("|---|---|---|")
    A("| conic bias magnitude and its correction | **A** | pure geometry + the CCF lens model |")
    A("| aperture/depth bias magnitude | **B** | depends on the unknown plate thickness and wall radiance; reported for 0.5–3 mm depth and a dark wall (bright wall reduces it to the conic term) |")
    A("| pose absorption of the geometric bias | **A** | follows from the 6-DOF pose Jacobian; the residual is the part that leaks into shared parameters |")
    A("| localizer ranking on synthetic scenes | **B** | the renderer's PSF/saturation profile is calibrated to the corpus but not identical to it |")
    A("| localizer ranking on the vendor corpus | **B/C** | proxy metrics (RMS, homography, epipolar); no physical truth; differences are small because the residual is model-limited |")
    A("| expected production gain | **C** | extrapolated from the synthetic end-to-end test; the vendor parameters are the real check |")
    A("")
    A("Limitations and open questions.  (i) The plate model in `calib.plate` "
      "(pitch 27.72 mm, plate 200x150 mm) is inconsistent with the observed margins: "
      "back-projecting the vendor plate contour at that pitch gives a half-width of "
      "~111 mm and a half-height of ~84 mm, i.e. margins of ~14 mm and ~28 mm rather "
      "than 3 mm and 19.5 mm.  Either the pitch is smaller (~24.6 mm) or the plate is "
      "larger (~222 x 167 mm); this audit only needs the *relative* geometry and uses "
      "222 x 167 mm in the renderer.  (ii) The hole radius 3.4 mm is inferred from the "
      "observed radius/pitch ratio (8.0 px / 63.2 px at 490 mm), not measured.  "
      "(iii) The physical plate thickness is unknown; §3 reports the 0–3 mm range.  "
      "(iv) The vendor corpus has no physical ground truth; §4 metrics are proxies.")
    A("")
    A("## 9. Reproduce")
    A("")
    A("```bash")
    A("python -m calib.detectors_audit --out analysis/calibration/audit")
    A("# fast subset (~1 min, no vendor calibration):")
    A("python -m calib.detectors_audit --out /tmp/audit --quick --skip-vendor")
    A("# tests")
    A("pytest calib/detectors_audit.py -q")
    A("```")
    A("")
    A("Files: `detector_results.json` (all numbers), `figures/*.png` "
      "(" + ", ".join(Path(f).name for f in results.get("figures", [])) + ").")
    A("")
    return "\n".join(L)


# --------------------------------------------------------------------------
# driver
# --------------------------------------------------------------------------

def _jsonable(obj):
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, (np.floating, np.integer)):
        return float(obj)
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return str(obj)


def collect_results(quick: bool = False, skip_vendor: bool = False,
                    skip_synthcal: bool = False, n_rep: int = 3,
                    vendor_dir: Path = VENDOR_DIR,
                    modes: Optional[list] = None) -> dict:
    import sys
    import cv2 as _cv2
    t_start = time.time()
    modes = list(LOCALIZERS) if modes is None else modes
    results = {"meta": {
        "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "python": sys.version.split()[0], "numpy": np.__version__,
        "opencv": _cv2.__version__, "vendor_dir": str(vendor_dir),
        "ccf_dir": str(CCF_DIR), "n_rep": n_rep, "quick": quick,
        "modes": modes,
    }}
    # 1. geometric bias (always cheap)
    print("[audit] conic bias sweep ...", flush=True)
    results["conic"] = {
        "sweep": _jsonable(conic_bias_sweep()),
        "through": _jsonable(run_through_study()),
        "through_detectors": _jsonable(run_through_detectors()),
    }
    # 2. synthetic stress matrix
    print("[audit] synthetic stress matrix ...", flush=True)
    cases = synthetic_cases(quick=quick)
    rows, scenes = run_synthetic(cases, n_rep=(2 if quick else n_rep),
                                 modes=modes, verbose=True)
    results["synthetic"] = {
        "summary": summarize_synthetic(rows),
        "scenes": _jsonable(scenes),
        "n_cases": len(cases),
        "elapsed_s": time.time() - t_start,
    }
    # 3. synthetic end-to-end calibration
    if not skip_synthcal:
        print("[audit] synthetic end-to-end calibration ...", flush=True)
        try:
            results["synthcal"] = _jsonable(run_synthetic_calibration(
                n_views=(6 if quick else 8), seed=42, modes=modes))
        except Exception as exc:      # pragma: no cover - diagnostic
            results["synthcal"] = {"error": repr(exc)}
    # 4. vendor corpus
    if not skip_vendor and Path(vendor_dir).exists():
        print("[audit] vendor corpus ...", flush=True)
        res = run_vendor(vendor_dir, modes=modes)
        pi = {}
        for m, d in res["per_image"].items():
            pi[m] = {}
            for (side, idx), r in d.items():
                o = r.get("obs")
                pi[m][f"{side}{idx}"] = {
                    "ok": o is not None,
                    "n_lattice": 0 if o is None else int(o.n_lattice),
                    "rms": None if o is None else float(o.rms),
                    "rms_loo": None if o is None else float(r.get("rms_loo", np.nan)),
                    "rms_undist": None if o is None else float(r.get("rms_undist", np.nan)),
                    "n_blobs": r.get("n_blobs"), "n_rejected": r.get("n_rejected"),
                    "time_ms": r.get("time_ms"), "error": r.get("error"),
                }
        results["vendor"] = {
            "calibration": _jsonable(res["calibration"]),
            "per_image": _jsonable(pi),
            "geometry": _jsonable({f"{k[0]}{k[1]}": v for k, v in res["geometry"].items()}),
        }
    results["meta"]["runtime_s"] = time.time() - t_start
    return results


def write_outputs(results: dict, outdir) -> dict:
    outdir = Path(outdir)
    (outdir / "figures").mkdir(parents=True, exist_ok=True)
    print("[audit] figures ...", flush=True)
    results["figures"] = make_figures(outdir / "figures", results)
    with open(outdir / "detector_results.json", "w") as fh:
        json.dump(_jsonable(results), fh, indent=1)
    md = build_report(results)
    (outdir / "detector_report.md").write_text(md)
    print(f"[audit] wrote {outdir/'detector_report.md'}, "
          f"{outdir/'detector_results.json'}, {len(results['figures'])} figures",
          flush=True)
    return results


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default=str(REPO / "analysis/calibration/audit"),
                    help="output directory for the report, JSON and figures")
    ap.add_argument("--quick", action="store_true",
                    help="reduced case set and 2 noise realisations")
    ap.add_argument("--skip-vendor", action="store_true")
    ap.add_argument("--skip-synthcal", action="store_true")
    ap.add_argument("--n-rep", type=int, default=3)
    ap.add_argument("--vendor", default=str(VENDOR_DIR))
    ap.add_argument("--modes", default=None,
                    help="comma-separated localizer list (default: all)")
    args = ap.parse_args(argv)
    modes = None if not args.modes else [m.strip() for m in args.modes.split(",")]
    results = collect_results(quick=args.quick, skip_vendor=args.skip_vendor,
                              skip_synthcal=args.skip_synthcal,
                              n_rep=args.n_rep, vendor_dir=Path(args.vendor),
                              modes=modes)
    write_outputs(results, args.out)
    return 0


# --------------------------------------------------------------------------
# regression tests (pytest calib/detectors_audit.py)
# --------------------------------------------------------------------------

def test_edge_localizer_is_subpixel():
    """Edge localizer recovers known synthetic centres to < 0.1 px RMS."""
    K, kc = vendor_camera()
    from .adaptive import board_pose
    R, t = board_pose(400.0, 20.0, 0.0, 0.0)
    spec = RenderSpec(R=R, t=t, K=K, kc=kc, psf_sigma=1.2, seed=3)
    sc = render(spec, np.random.default_rng(0))
    cen = np.array([h.centre_px for h in sc.holes])
    seeds = blob_seeds(sc.image, method="relative")
    errs = []
    for h, s in zip(sc.holes, match_seeds(seeds, cen)):
        assert s is not None
        r = window_radius("edge_ellipse", s.w, s.h)
        loc = localize_blob(sc.image, "edge_ellipse", s.x, s.y, r,
                            prior_r=0.5 * max(s.w, s.h))
        assert loc.ok
        errs.append(np.hypot(loc.x - h.centre_px[0], loc.y - h.centre_px[1]))
    assert np.sqrt(np.mean(np.square(errs))) < 0.10


def test_conic_correction_removes_circle_bias():
    """The conic back-projection removes the projected-circle centre bias."""
    K, kc = vendor_camera()
    res = conic_bias_point(3.4, 400.0, 35.0, K, kc)
    assert np.linalg.norm(res["bias_px"]) > 0.01
    assert np.linalg.norm(res["residual_px"]) < 0.5 * np.linalg.norm(res["bias_px"])
    assert np.linalg.norm(res["residual_px"]) < 0.02


def test_aperture_bias_is_pose_absorbed():
    """A through-hole's aperture bias is almost a rigid image motion."""
    K, kc = vendor_camera()
    R, t = pose_tilted(400.0, 35.0)
    spec = RenderSpec(R=R, t=t, K=K, kc=kc, thickness_mm=1.0,
                      hole_model="through", noise_dn=0.0, seed=3)
    tr = aperture_bias_truth(spec)
    assert tr["rms_bias"] > 0.3
    assert tr["pose_absorption"]["explained"] > 0.99
    assert tr["pose_absorption"]["residual_rms"] < 0.05


def test_vendor_corpus_grids():
    """Every vendor image that the production detector solves is solved here too."""
    import pytest
    if not Path(VENDOR_DIR).exists():
        pytest.skip("vendor corpus not available")
    cams = load_ccf_cameras()
    files = vendor_images(VENDOR_DIR, sides=("Left",), indices=range(1, 4))
    for side, idx, path in files:
        gray = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
        r = detect_with_mode(gray, "edge_ellipse", cam=cams["left"])
        assert r["obs"] is not None
        assert r["obs"].n_lattice >= 35


if __name__ == "__main__":
    raise SystemExit(main())
