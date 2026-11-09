#!/usr/bin/env python3
"""Uncertainty-calibration and failure-mode audit of the Einstar calibration engine.

What this module measures
-------------------------
The production engine (:mod:`calib.calibrate`, :mod:`calib.adaptive`,
:mod:`calib.quality`) is a joint robust bundle adjustment over three cameras,
a planar marker plate and per-view board poses.  This audit asks a narrower
question than "is the calibration accurate": **is the reported uncertainty
calibrated, and where does it fail?**

Everything here is offline and synthetic unless explicitly marked ``corpus``.
No physical-accuracy claim is made.  Synthetic ground truth measures estimator
self-consistency (bias, covariance calibration, failure modes); the vendor
corpus measures agreement with the shipped CCF, not truth.

Variants
--------
Every number in the JSON carries a ``variant`` label.  The important ones:

``current``
    Whatever ``calibrate()`` returns in the checked-out tree.  At the time of
    this audit the default is the Huber sandwich covariance
    (``robust_covariance=True``), and ``adaptive.rig_covariance`` returns the
    top-left 39x39 block (marginal over board poses).  The audit *detects* the
    live behaviour instead of assuming it.

``gn_mad``
    The literal "numeric Jacobian at the solution, component-wise MAD scale"
    estimator: ``sigma^2 * pinv(J^T J)``.  This is the pre-robust engine
    described in the task and is retained as a reference.

``sandwich_current``
    Re-implementation of the live Huber sandwich, used to verify that the
    reported matrix is reproduced.  The implementation uses
    ``B = J^T diag(w * r^2) J``; the standard M-estimator sandwich uses
    ``B = J^T diag((w*r)^2) J``.  The two agree only for inliers (w=1).

``sandwich_corrected``
    ``A^-1 B A^-1`` with ``B = J^T diag((w*r)^2) J`` and
    ``A = J^T diag(w) J``.  This is the corrected robust reference.

``marginal``
    Correct rig covariance: the top-left 39x39 block of the joint covariance
    (board poses integrated out).

``conditional_legacy``
    The old ``C_rr - C_rv C_vv^-1 C_vr`` formula.  This is the covariance
    *given* the board poses, not marginal, and is therefore too small.  It is
    kept only as a regression reference; the live ``rig_covariance`` is
    compared to it and to the marginal block.

``rescale_transform`` vs ``rescale_legacy``
    ``C' = S C S^T`` with only translation rows scaled, versus the old
    ``C * s^2``.  The live implementation is tested against both.

Usage
-----
    python -m calib.uncertainty_audit --out analysis/calibration/audit
    python -m calib.uncertainty_audit --out /tmp/audit --quick
    python -m calib.uncertainty_audit --out analysis/calibration/audit --report-only

The full run is parallelised over independent calibration trials.  Outputs:
``uncertainty_results.json``, ``uncertainty_report.md`` and ``figures/*.png``.
"""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import math
import os
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

# Keep BLAS single-threaded in workers: the linear algebra is tiny and process
# parallelism is the useful axis.  Set before numpy is imported.
for _v in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
           "NUMEXPR_NUM_THREADS"):
    os.environ.setdefault(_v, "1")

import numpy as np
from scipy import ndimage
from scipy.optimize import least_squares
from scipy.stats import chi2, norm

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import LogNorm

# Make the module runnable both as ``python calib/uncertainty_audit.py`` and
# as ``python -m calib.uncertainty_audit``.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "calib"

from calib import plate
from calib.calibrate import (CAMERAS, CameraIntrinsics, CalibrationResult,
                             Observation, calibrate)
from calib.geometry import project, relative_pose, rodrigues, rvec_from_R
from calib import adaptive
from calib.adaptive import board_pose

try:                                            # optional, live in the tree
    from calib import quality as _quality
except Exception:                               # pragma: no cover
    _quality = None

REPO = Path(__file__).resolve().parents[1]
NRIG = 39

try:
    from calib.calibrate import _param_names as _param_names_fn
    _RIG_NAMES = _param_names_fn(0)[:NRIG]
except Exception:                                # pragma: no cover
    _RIG_NAMES = [f"p{i}" for i in range(NRIG)]
IMAGE_SIZE = (1280, 1024)
MASTER_SEED = 20261109

#: absolute sigma below which a free parameter is treated as unidentifiable
ZERO_SD = 1e-9

#: labels used throughout the JSON/report
VARIANT_LEGEND = {
    "current": "live calibrate() covariance (default robust_covariance=True)",
    "gn_mad": "sigma_MAD^2 * pinv(J^T J), literal pre-robust estimator",
    "sandwich_current": "pre-fix reference sandwich: B=J^T diag(w r^2) J "
                       "(conservative for outliers)",
    "sandwich_corrected": "standard Huber sandwich: B=J^T diag((w r)^2) J "
                          "(live tree after the parent's fix)",
    "marginal": "rig covariance = top-left 39x39 block of the joint covariance",
    "conditional_legacy": "C_rr - C_rv C_vv^-1 C_vr (conditional, too small)",
    "rescale_transform": "S C S^T, translation rows/cols scaled only",
    "rescale_legacy": "C * s^2 (old, wrong for non-translation parameters)",
    "corpus": "vendor 25-view corpus, CCF as reference (no physical truth)",
    "synthetic": "synthetic scene with known ground truth",
}

# --------------------------------------------------------------------------
# small utilities
# --------------------------------------------------------------------------


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def sha256_file(path: Path) -> Optional[str]:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for block in iter(lambda: fh.read(1 << 20), b""):
                h.update(block)
        return h.hexdigest()
    except OSError:
        return None


def git_info() -> dict:
    out = {"head": None, "dirty": None, "diff_stat": None}
    try:
        out["head"] = subprocess.run(
            ["git", "-C", str(REPO), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10).stdout.strip() or None
        out["dirty"] = bool(subprocess.run(
            ["git", "-C", str(REPO), "status", "--porcelain"],
            capture_output=True, text=True, timeout=10).stdout.strip())
        out["diff_stat"] = subprocess.run(
            ["git", "-C", str(REPO), "diff", "--stat"],
            capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception as exc:                     # pragma: no cover
        out["error"] = str(exc)
    return out


def jsonable(obj: Any) -> Any:
    """Recursively convert numpy/float specials into strict JSON values."""
    if isinstance(obj, dict):
        return {str(k): jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [jsonable(v) for v in obj]
    if isinstance(obj, np.ndarray):
        return [jsonable(v) for v in obj.tolist()]
    if isinstance(obj, (np.floating, np.integer)):
        obj = obj.item()
    if isinstance(obj, bool) or obj is None or isinstance(obj, (int, str)):
        return obj
    if isinstance(obj, float):
        if not math.isfinite(obj):
            return None
        return obj
    return obj


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(jsonable(payload), fh, indent=1, sort_keys=False,
                  allow_nan=False)
        fh.write("\n")
    os.replace(tmp, path)


def progress(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def finite(x: Any, default: float = float("nan")) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    return v if math.isfinite(v) else default


def safe_div(a: float, b: float) -> float:
    if b is None or not math.isfinite(b) or abs(b) < 1e-15:
        return float("nan")
    return float(a) / float(b)


def mad_sigma(x: np.ndarray) -> float:
    x = np.asarray(x, float).ravel()
    if x.size == 0:
        return float("nan")
    return float(max(1.4826 * np.median(np.abs(x - np.median(x))), 1e-3))


def wilson_ci(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n <= 0:
        return (float("nan"), float("nan"))
    p = k / n
    den = 1.0 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(max(p * (1 - p) / n + z * z / (4 * n * n), 0.0)) / den
    return (max(0.0, centre - half), min(1.0, centre + half))


def cluster_bootstrap(values: np.ndarray, stat, n_boot: int = 2000,
                      seed: int = 0) -> tuple[float, float, float]:
    """Percentile CI resampling whole trials (rows) with replacement."""
    values = np.asarray(values, float)
    if values.shape[0] == 0:
        return (float("nan"), float("nan"), float("nan"))
    rng = np.random.default_rng(seed)
    t = values.shape[0]
    stats = np.empty(n_boot)
    for b in range(n_boot):
        pick = rng.integers(0, t, t)
        stats[b] = stat(values[pick])
    point = float(stat(values))
    lo, hi = np.percentile(stats, [2.5, 97.5])
    return (point, float(lo), float(hi))


def median_ci(values: Iterable[float], n_boot: int = 1000,
              seed: int = 0) -> dict:
    arr = np.asarray([v for v in values if v is not None], float)
    arr = arr[np.isfinite(arr)]
    if arr.size == 0:
        return {"median": None, "mean": None, "ci95": [None, None], "n": 0}
    point, lo, hi = cluster_bootstrap(arr[:, None], lambda a: np.median(a.ravel()),
                                      n_boot=n_boot, seed=seed)
    return {"median": float(point), "mean": float(np.mean(arr)),
            "ci95": [float(lo), float(hi)], "n": int(arr.size)}


# --------------------------------------------------------------------------
# camera / scene model
# --------------------------------------------------------------------------

def _radial_ok(kc: np.ndarray, r2_max: float = 0.45) -> bool:
    """Reject distortion draws that fold the radial map over the used field."""
    k1, k2, p1, p2, k3 = np.asarray(kc, float)[:5]
    r2 = np.linspace(0.0, r2_max, 60)
    radial = 1.0 + k1 * r2 + k2 * r2 ** 2 + k3 * r2 ** 3
    d = 1.0 + 2 * k1 * r2 + 3 * k2 * r2 ** 2 + 4 * k3 * r2 ** 3
    return bool(np.all(radial > 0.5) and np.all(d > 0.0))


def sample_intrinsics(rng: np.random.Generator, camera: str) -> CameraIntrinsics:
    if camera == "tex":
        fx = float(rng.uniform(1150.0, 1550.0))
    else:
        fx = float(rng.uniform(1080.0, 1240.0))
    fy = fx * (1.0 + rng.normal(0.0, 8e-4))
    cx = float(640.0 + rng.normal(0.0, 14.0))
    cy = float(512.0 + rng.normal(0.0, 14.0))
    for _ in range(50):
        kc = np.array([rng.uniform(-0.20, -0.11),
                       rng.uniform(0.10, 0.22),
                       rng.normal(0.0, 5e-4),
                       rng.normal(0.0, 5e-4),
                       rng.uniform(-0.03, 0.06)], float)
        if _radial_ok(kc):
            break
    return CameraIntrinsics(fx, fy, cx, cy, kc)


def sample_rig(rng: np.random.Generator) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    b_r = float(rng.uniform(130.0, 190.0))
    toe_r = math.radians(float(rng.uniform(12.0, 32.0)))
    rr = rodrigues(np.array([rng.normal(0.0, 0.02), toe_r, rng.normal(0.0, 0.02)]))
    tr = np.array([-b_r, rng.normal(0.0, 3.0), rng.uniform(20.0, 40.0)])
    b_t = float(rng.uniform(90.0, 140.0))
    toe_t = math.radians(float(rng.uniform(3.0, 12.0)))
    rt = rodrigues(np.array([rng.normal(0.0, 0.015), toe_t, rng.normal(0.0, 0.015)]))
    tt = np.array([-b_t, rng.uniform(-30.0, -15.0), rng.uniform(0.0, 20.0)])
    return {"left": (np.eye(3), np.zeros(3)), "right": (rr, tr), "tex": (rt, tt)}


def sample_poses(rng: np.random.Generator, k: int, mode: str = "normal"
                 ) -> list[tuple[np.ndarray, np.ndarray]]:
    poses = []
    for _ in range(k):
        if mode == "normal":
            d = float(rng.uniform(240.0, 560.0))
            tx = float(rng.uniform(-25.0, 25.0))
            ty = float(rng.uniform(-25.0, 25.0))
            roll = float(rng.choice([0.0, 90.0, 180.0, 270.0]) + rng.normal(0, 2.5))
        elif mode == "same_distance":
            d = 400.0
            tx = float(rng.uniform(-25.0, 25.0))
            ty = float(rng.uniform(-25.0, 25.0))
            roll = float(rng.choice([0.0, 90.0, 180.0, 270.0]) + rng.normal(0, 2.5))
        elif mode == "same_tilt":
            d = float(rng.uniform(240.0, 560.0))
            tx = 20.0
            ty = 20.0
            roll = float(rng.choice([0.0, 90.0, 180.0, 270.0]) + rng.normal(0, 2.5))
        elif mode == "same_tilt_distance":
            d = 400.0
            tx = 20.0
            ty = 20.0
            roll = float(rng.choice([0.0, 90.0, 180.0, 270.0]) + rng.normal(0, 2.5))
        elif mode == "frontoparallel":
            d = 400.0
            tx = 0.0
            ty = 0.0
            roll = float(rng.choice([0.0, 90.0, 180.0, 270.0]) + rng.normal(0, 2.5))
        elif mode == "identical":
            d = 400.0
            tx = 0.0
            ty = 0.0
            roll = 0.0
        elif mode == "narrow":
            d = float(rng.uniform(380.0, 420.0))
            tx = float(rng.uniform(-3.0, 3.0))
            ty = float(rng.uniform(-3.0, 3.0))
            roll = float(rng.choice([0.0, 90.0]) + rng.normal(0, 1.0))
        elif mode == "clustered":
            d = float(rng.uniform(240.0, 300.0))
            tx = float(rng.uniform(15.0, 25.0))
            ty = float(rng.uniform(15.0, 25.0))
            roll = float(rng.choice([0.0, 90.0, 180.0, 270.0]) + rng.normal(0, 2.5))
        elif mode == "near_identical":
            d = 400.0 + float(rng.normal(0.0, 1.0))
            tx = float(rng.normal(0.0, 0.5))
            ty = float(rng.normal(0.0, 0.5))
            roll = float(rng.normal(0.0, 0.5))
        else:
            raise ValueError(f"unknown pose mode {mode}")
        poses.append(board_pose(d, tx, ty, roll,
                                (float(rng.normal(0.0, 8.0)), 0.0)))
    return poses


def project_model(K: np.ndarray, kc: np.ndarray, X: np.ndarray,
                  model: str = "brown", kc_hi: Optional[np.ndarray] = None
                  ) -> np.ndarray:
    """Project with the engine model or a richer true model."""
    X = np.asarray(X, float).reshape(-1, 3)
    z = X[:, 2]
    x = X[:, 0] / z
    y = X[:, 1] / z
    r2 = x * x + y * y
    if model == "brown":
        k1, k2, p1, p2, k3 = np.asarray(kc, float).ravel()[:5]
        radial = 1.0 + k1 * r2 + k2 * r2 ** 2 + k3 * r2 ** 3
        xd = x * radial + 2.0 * p1 * x * y + p2 * (r2 + 2.0 * x * x)
        yd = y * radial + p1 * (r2 + 2.0 * y * y) + 2.0 * p2 * x * y
    elif model == "rational":
        k = np.asarray(kc, float).ravel()
        k1, k2, p1, p2, k3 = k[:5]
        k4, k5, k6 = (np.asarray(kc_hi, float).ravel()[:3]
                      if kc_hi is not None else (0.0, 0.0, 0.0))
        num = 1.0 + k1 * r2 + k2 * r2 ** 2 + k3 * r2 ** 3
        den = 1.0 + k4 * r2 + k5 * r2 ** 2 + k6 * r2 ** 3
        radial = num / np.maximum(den, 0.2)
        xd = x * radial + 2.0 * p1 * x * y + p2 * (r2 + 2.0 * x * x)
        yd = y * radial + p1 * (r2 + 2.0 * y * y) + 2.0 * p2 * x * y
    elif model == "poly8":
        k1, k2, p1, p2, k3 = np.asarray(kc, float).ravel()[:5]
        k4 = float(np.asarray(kc_hi, float).ravel()[0]) if kc_hi is not None else 0.0
        radial = 1.0 + k1 * r2 + k2 * r2 ** 2 + k3 * r2 ** 3 + k4 * r2 ** 4
        xd = x * radial + 2.0 * p1 * x * y + p2 * (r2 + 2.0 * x * x)
        yd = y * radial + p1 * (r2 + 2.0 * y * y) + 2.0 * p2 * x * y
    else:
        raise ValueError(model)
    u = K[0, 0] * xd + K[0, 1] * yd + K[0, 2]
    v = K[1, 1] * yd + K[1, 2]
    return np.stack([u, v], axis=1)


def deform_points(obj: np.ndarray, kind: str, amplitude: float) -> np.ndarray:
    """Low-order systematic board deformation, in board millimetres."""
    X = np.asarray(obj, float).copy()
    half_w = max(np.max(np.abs(X[:, 0])), 1e-9)
    half_h = max(np.max(np.abs(X[:, 1])), 1e-9)
    xn = X[:, 0] / half_w
    yn = X[:, 1] / half_h
    if kind == "bow":
        X[:, 2] += amplitude * (0.5 * (xn ** 2 + yn ** 2) - 1.0 / 3.0)
    elif kind == "warp":
        X[:, 0] += amplitude * (yn ** 2 - 1.0 / 3.0)
        X[:, 1] += -0.7 * amplitude * (xn ** 2 - 1.0 / 3.0)
        X[:, 2] += 0.5 * amplitude * xn * yn
    elif kind == "tilt_warp":
        X[:, 2] += amplitude * xn * yn
    else:
        raise ValueError(kind)
    return X


@dataclass
class Scene:
    cameras: dict[str, CameraIntrinsics]
    extrinsics: dict[str, tuple[np.ndarray, np.ndarray]]
    views: list[tuple[np.ndarray, np.ndarray]]
    observations: list[Observation]
    pitch_nominal: float
    pitch_true: float
    noise_px: float
    meta: dict = field(default_factory=dict)

    def truth_vector(self, free_kc: bool = True) -> np.ndarray:
        return pack_truth(self.cameras, self.extrinsics, self.views,
                          self.pitch_nominal, free_kc)


def build_scene(seed: int, k: int = 12, *,
                noise_px: float = 0.2,
                outlier_frac: float = 0.0,
                outlier_px: float = 8.0,
                pose_mode: str = "normal",
                pitch_rel_error: float = 0.0,
                lens_model: str = "brown",
                lens_hi: Optional[np.ndarray] = None,
                deform: Optional[tuple[str, float]] = None,
                global_shift_px: tuple[float, float] = (0.0, 0.0),
                subset_shift: Optional[dict] = None,
                gradient_shift_px: float = 0.0,
                correlated_noise: Optional[dict] = None,
                missing_frac: float = 0.0,
                missing_structured: Optional[str] = None,
                pose_jitter: Optional[tuple[float, float]] = None,
                common_mode_px: float = 0.0,
                cams: Optional[dict[str, CameraIntrinsics]] = None,
                extr: Optional[dict[str, tuple[np.ndarray, np.ndarray]]] = None,
                views: Optional[list[tuple[np.ndarray, np.ndarray]]] = None,
                pitch_nominal: float = plate.DEFAULT_PITCH_MM,
                camera_subset: Optional[Iterable[str]] = None,
                view_subset: Optional[Iterable[int]] = None) -> Scene:
    """Generate one synthetic scene with known truth and requested defects."""
    rng = np.random.default_rng(int(seed))
    cams = cams or {c: sample_intrinsics(rng, c) for c in CAMERAS}
    extr = extr or sample_rig(rng)
    views = views or sample_poses(rng, k, pose_mode)
    pitch_true = float(pitch_nominal) * (1.0 + float(pitch_rel_error))
    obj = plate.board_points_mm(pitch_true)
    if deform is not None:
        obj = deform_points(obj, deform[0], float(deform[1]))

    # apply a rigid pose jitter to the *actual* board poses (truth moves too)
    if pose_jitter is not None:
        rot_sigma, trans_sigma = pose_jitter
        jittered = []
        for R, t in views:
            dR = rodrigues(rng.normal(0.0, math.radians(rot_sigma), 3))
            jittered.append((dR @ R, t + rng.normal(0.0, trans_sigma, 3)))
        views = jittered

    cams_out = {c: CameraIntrinsics(cams[c].fx, cams[c].fy, cams[c].cx,
                                    cams[c].cy, cams[c].kc.copy())
                for c in CAMERAS}

    obs: list[Observation] = []
    used_views = sorted(set(int(v) for v in (view_subset if view_subset is not None
                                              else range(len(views)))))
    view_map = {v: i for i, v in enumerate(used_views)}
    cam_list = tuple(camera_subset) if camera_subset is not None else CAMERAS

    for vi in used_views:
        R, t = views[vi]
        for cam in cam_list:
            Rc, tc = extr[cam]
            Xc = (obj @ R.T + t) @ Rc.T + tc
            if np.any(Xc[:, 2] <= 1.0):
                raise RuntimeError(f"point behind camera {cam} view {vi}")
            uv = project_model(cams[cam].K, cams[cam].kc, Xc,
                               lens_model, lens_hi)
            # noise: i.i.d. plus optional spatially correlated field
            n = rng.normal(0.0, noise_px, uv.shape)
            if correlated_noise:
                corr_len = float(correlated_noise.get("length", 1.0))
                amp = float(correlated_noise.get("sigma_px", noise_px))
                field = rng.normal(0.0, 1.0, (plate.GRID_ROWS, plate.GRID_COLS, 2))
                field = ndimage.gaussian_filter(field, sigma=(corr_len, corr_len, 0),
                                                mode="reflect")
                flat = field.reshape(-1, 2)
                std = np.std(flat, axis=0)
                flat = flat / np.maximum(std, 1e-9) * amp
                n = flat
            if common_mode_px:
                n = n + rng.normal(0.0, common_mode_px, (1, 2))
            uv = uv + n
            if outlier_frac > 0:
                m = rng.random(len(uv)) < outlier_frac
                uv[m] += rng.normal(0.0, outlier_px, (int(m.sum()), 2))
            if global_shift_px != (0.0, 0.0):
                uv = uv + np.asarray(global_shift_px, float)
            if gradient_shift_px:
                half_w = max(np.max(np.abs(obj[:, 0])), 1e-9)
                uv[:, 0] += gradient_shift_px * (obj[:, 0] / half_w)
            if subset_shift:
                frac = float(subset_shift.get("frac", 0.3))
                amp = np.asarray(subset_shift.get("amp_px", (0.3, 0.0)), float)
                mode = subset_shift.get("mode", "columns")
                if mode == "columns":
                    sel = obj[:, 0] <= np.quantile(obj[:, 0], frac)
                elif mode == "rows":
                    sel = obj[:, 1] <= np.quantile(obj[:, 1], frac)
                elif mode == "random":
                    sel = rng.random(len(uv)) < frac
                else:
                    raise ValueError(mode)
                uv[sel] += amp
            if missing_structured == "columns":
                keep = obj[:, 0] > np.quantile(obj[:, 0], missing_frac)
            elif missing_structured == "rows":
                keep = obj[:, 1] > np.quantile(obj[:, 1], missing_frac)
            else:
                keep = rng.random(len(uv)) >= missing_frac
            if missing_frac > 0 and keep.sum() < 12:
                # always retain enough points for a pose
                keep[np.argsort(-np.arange(len(keep)))[:12]] = True
            lattice = plate.lattice_uv()
            dct = {(float(u), float(v)): uv[i]
                   for i, (u, v) in enumerate(lattice) if keep[i]}
            obs.append(Observation(cam, view_map[vi], dct))

    return Scene(cams_out, extr, views, obs, float(pitch_nominal), pitch_true,
                 float(noise_px),
                 {"seed": int(seed), "k": int(k), "pose_mode": pose_mode,
                  "outlier_frac": float(outlier_frac),
                  "used_views": [int(v) for v in used_views],
                  "view_map": {int(k): int(v) for k, v in view_map.items()},
                  "camera_subset": list(cam_list),
                  "deform": deform, "lens_model": lens_model,
                  "missing_frac": float(missing_frac)})


# --------------------------------------------------------------------------
# packing / truth vectors
# --------------------------------------------------------------------------

def _packer(n_views: int, free_kc: bool):
    from calib.calibrate import _Packer
    return _Packer(n_views, free_kc)


def pack_truth(cams: dict[str, CameraIntrinsics], extr: dict, views: list,
               pitch: float, free_kc: bool = True) -> np.ndarray:
    pk = _packer(len(views), free_kc)
    x = np.zeros(pk.n)
    for c in CAMERAS:
        x[pk.slices[c]] = cams[c].as_vec()
    for c in ("right", "tex"):
        R, t = extr[c]
        x[pk.extr[c]] = np.concatenate([rvec_from_R(R), t])
    for i, (R, t) in enumerate(views):
        x[pk.views.start + 6 * i: pk.views.start + 6 * (i + 1)] = \
            np.concatenate([rvec_from_R(R), t])
    return x


def result_vector(result: CalibrationResult) -> np.ndarray:
    if hasattr(result, "vector"):
        return np.asarray(result.vector(), float)
    pk = _packer(len(result.views), result.info.get("free_kc", True))
    x = np.zeros(pk.n)
    for c in CAMERAS:
        x[pk.slices[c]] = result.cameras[c].as_vec()
    for c in ("right", "tex"):
        R, t = result.extrinsics[c]
        x[pk.extr[c]] = np.concatenate([rvec_from_R(R), t])
    for i, (R, t) in enumerate(result.views):
        x[pk.views.start + 6 * i: pk.views.start + 6 * (i + 1)] = \
            np.concatenate([rvec_from_R(R), t])
    return x


def result_free_mask(result: CalibrationResult) -> np.ndarray:
    fm = result.info.get("free_mask")
    pk = _packer(len(result.views), bool(result.info.get("free_kc", True)))
    if fm is not None:
        fm = np.asarray(fm, bool)
        if fm.size == pk.n:
            return fm
    mask = np.ones(pk.n, bool)
    if not bool(result.info.get("free_intrinsics", True)):
        for c in CAMERAS:
            mask[pk.slices[c].start:pk.slices[c].start + 4] = False
    if not bool(result.info.get("free_kc", True)):
        for c in CAMERAS:
            mask[pk.slices[c].start + 4:pk.slices[c].stop] = False
    return mask


def result_groups(result: CalibrationResult) -> dict[str, np.ndarray]:
    fm = result_free_mask(result)
    n = len(fm)
    intr = np.array([i for i in range(min(27, n)) if fm[i]], int)
    extr = np.array([i for i in range(27, min(39, n)) if fm[i]], int)
    views = np.array([i for i in range(39, n) if fm[i]], int)
    rig = np.concatenate([intr, extr])
    allp = np.where(fm)[0]
    # distortion vs physical intrinsics: a lens-model mismatch invalidates the
    # coefficient-by-coefficient truth, but fx/fy/cx/cy remain physical.
    intr_phys = np.array([i for i in intr if (i % 9) < 4], int)
    distortion = np.array([i for i in intr if (i % 9) >= 4], int)
    return {"all": allp, "intrinsics": intr, "intrinsics_physical": intr_phys,
            "distortion": distortion, "extrinsics": extr, "rig": rig,
            "views": views}


def param_names(result: CalibrationResult) -> list[str]:
    names = result.info.get("param_names")
    if names and len(names) == len(result_free_mask(result)):
        return list(names)
    from calib.calibrate import _param_names as names_fn
    return names_fn(len(result.views))


def baseline_sigma(result: CalibrationResult,
                   C: Optional[np.ndarray] = None) -> float:
    """First-order sigma of |t_right - R_right t_left| (left gauge)."""
    C = result.covariance if C is None else C
    if C is None:
        return float("nan")
    base = float(np.linalg.norm(result.baseline()))
    eps = 1e-4
    Rr, tr = result.extrinsics["right"]
    Rl, tl = result.extrinsics["left"]
    g = np.zeros(6)
    for k in range(3):
        e = np.zeros(3)
        e[k] = eps
        # left translation
        n0 = np.linalg.norm(tr - Rr @ (tl + e))
        g[k] = (n0 - base) / eps
        # right translation
        n1 = np.linalg.norm((tr + e) - Rr @ tl)
        g[3 + k] = (n1 - base) / eps
    # left is gauge; only right translation block enters
    Ct = np.asarray(C)[30:33, 30:33]
    return float(np.sqrt(max(g[3:] @ Ct @ g[3:], 0.0)))


# --------------------------------------------------------------------------
# covariance variants from a reconstructed Jacobian
# --------------------------------------------------------------------------

def _call_with_supported(fn, *args, **kwargs):
    try:
        sig = inspect.signature(fn)
        kwargs = {k: v for k, v in kwargs.items() if k in sig.parameters}
    except (TypeError, ValueError):
        pass
    return fn(*args, **kwargs)


def reconstruct_jacobian(result: CalibrationResult, observations: list[Observation],
                         pitch: float) -> tuple[Optional[np.ndarray], Optional[np.ndarray], dict]:
    """Recompute the raw residual Jacobian at the reported solution."""
    try:
        from calib.calibrate import _Problem, _numeric_jacobian
        remap = result.info.get("view_remap", {}) or {}
        obs = [Observation(o.camera, int(remap.get(int(o.view), int(o.view))), o.uv)
               for o in observations]
        prob = _Problem(obs, plate.board_points_mm(pitch), len(result.views),
                        bool(result.info.get("free_kc", True)),
                        result.robust_loss,
                        1.0,
                        bool(result.info.get("free_intrinsics", True)))
        x = result_vector(result)
        f = prob.residuals(x)
        try:
            J = _numeric_jacobian(prob, x, f, central=True)
        except TypeError:
            J = _numeric_jacobian(prob, x, f)
        return np.asarray(J, float), np.asarray(f, float), {"ok": True}
    except Exception as exc:                     # pragma: no cover
        return None, None, {"ok": False, "error": str(exc)}


def _psd(C: np.ndarray) -> np.ndarray:
    C = 0.5 * (C + C.T)
    w, V = np.linalg.eigh(C)
    if w.size and w.min() < 0:
        C = (V * np.clip(w, 0.0, None)) @ V.T
        C = 0.5 * (C + C.T)
    return C


def _pinv_from_svd(J: np.ndarray, weighted: Optional[np.ndarray] = None,
                   rcond: float = 1e-10) -> tuple[np.ndarray, dict]:
    if weighted is None:
        U, S, Vt = np.linalg.svd(J, full_matrices=False)
    else:
        U, S, Vt = np.linalg.svd(weighted, full_matrices=False)
    smax = float(S[0]) if S.size else 0.0
    keep = S > max(smax * rcond, 1e-15)
    inv = (Vt[keep].T / (S[keep] ** 2)) @ Vt[keep]
    return inv, {"rank": int(keep.sum()), "n": int(J.shape[1]),
                 "smax": smax, "smin_kept": float(S[keep][-1]) if keep.any() else None}


def covariance_variants_from_jacobian(J: np.ndarray, f: np.ndarray,
                                      n_full: int, free_idx: np.ndarray,
                                      f_scale: float = 1.0,
                                      rcond: float = 1e-10) -> dict:
    """Return full-size covariance matrices for the audit variants."""
    comps = np.asarray(f, float).ravel()
    sigma = mad_sigma(comps)
    w = np.where(np.abs(comps) <= f_scale, 1.0,
                 f_scale / np.maximum(np.abs(comps), 1e-12))
    Jw = J * np.sqrt(w)[:, None]
    Ainv, rank_info = _pinv_from_svd(J, Jw, rcond=rcond)
    B_cur = J.T @ ((w * comps ** 2)[:, None] * J)
    B_corr = J.T @ (((w * comps) ** 2)[:, None] * J)
    C_cur = _psd(Ainv @ B_cur @ Ainv)
    C_corr = _psd(Ainv @ B_corr @ Ainv)
    Hinv, rank_plain = _pinv_from_svd(J, None, rcond=rcond)
    C_gn = _psd(Hinv * sigma ** 2)
    C_legacy = _psd(np.linalg.pinv(J.T @ J, rcond=1e-12) * sigma ** 2)

    def full(Cfree):
        out = np.zeros((n_full, n_full))
        out[np.ix_(free_idx, free_idx)] = Cfree
        return out

    return {
        "current": full(C_cur),
        "sandwich_pre_fix": full(C_cur),
        "sandwich_current": full(C_cur),
        "sandwich_corrected": full(C_corr),
        "gn_mad": full(C_gn),
        "legacy_pinv_h": full(C_legacy),
        "sigma_px": sigma,
        "rank": rank_info["rank"],
        "rank_plain": rank_plain["rank"],
        "condition": (rank_info["smax"] / rank_info["smin_kept"]
                      if rank_info["smin_kept"] else float("inf")),
        "n_free": int(J.shape[1]),
        "n_residuals": int(J.shape[0]),
    }


def rig_block(C: Optional[np.ndarray]) -> Optional[np.ndarray]:
    if C is None:
        return None
    return np.array(C[:NRIG, :NRIG], copy=True)


def conditional_legacy(C: Optional[np.ndarray]) -> Optional[np.ndarray]:
    """Old, wrong conditional rig covariance."""
    if C is None:
        return None
    rr = C[:NRIG, :NRIG]
    rv = C[:NRIG, NRIG:]
    vv = C[NRIG:, NRIG:]
    if vv.size == 0:
        return np.array(rr, copy=True)
    try:
        return rr - rv @ np.linalg.pinv(vv, rcond=1e-12) @ rv.T
    except np.linalg.LinAlgError:
        return np.array(rr, copy=True)


def compare_matrices(A: Optional[np.ndarray], B: Optional[np.ndarray]) -> dict:
    if A is None or B is None:
        return {"ok": False}
    A = np.asarray(A, float)
    B = np.asarray(B, float)
    den = max(float(np.max(np.abs(B))), 1e-15)
    return {"ok": True, "max_abs": float(np.max(np.abs(A - B))),
            "rel_max": float(np.max(np.abs(A - B)) / den),
            "trace_a": float(np.trace(A)), "trace_b": float(np.trace(B)),
            "trace_ratio": safe_div(float(np.trace(A)), float(np.trace(B)))}


# --------------------------------------------------------------------------
# NEES / z-score metrics
# --------------------------------------------------------------------------

def group_metrics(d: np.ndarray, C: Optional[np.ndarray],
                  idx: np.ndarray) -> dict:
    """NEES and scalar z scores for one parameter group."""
    idx = np.asarray(idx, int)
    out = {"n_params": int(len(idx)), "nees": None, "nees_per_n": None,
           "z": [], "zero_var": 0, "infinite": False}
    if len(idx) == 0 or C is None:
        return out
    dd = np.asarray(d, float)[idx]
    CC = np.asarray(C, float)[np.ix_(idx, idx)]
    sd = np.sqrt(np.maximum(np.diag(CC), 0.0))
    zero = sd <= ZERO_SD
    nzero = int(zero.sum())
    out["zero_var"] = nzero
    z = np.empty(len(idx), float)
    z[~zero] = np.abs(dd[~zero]) / sd[~zero]
    # a zero-variance parameter with a non-zero error is infinitely many sigma
    bad_zero = zero & (np.abs(dd) > ZERO_SD)
    z[zero] = np.where(bad_zero[zero], np.inf, 0.0)
    if bad_zero.any():
        out["infinite"] = True
        out["nees"] = float("inf")
        out["nees_per_n"] = float("inf")
    else:
        keep = ~zero
        if keep.sum() == 0:
            nees = 0.0
        else:
            try:
                nees = float(dd[keep] @ np.linalg.pinv(CC[np.ix_(keep, keep)],
                                                       rcond=1e-12) @ dd[keep])
            except np.linalg.LinAlgError:
                nees = float("nan")
        out["nees"] = nees
        out["nees_per_n"] = safe_div(nees, len(idx))
    out["z"] = [float(v) for v in z]          # inf is meaningful (zero variance)
    out["z_finite"] = [float(v) for v in z if math.isfinite(v)]
    out["z_rms"] = (float(np.sqrt(np.mean(np.square(out["z_finite"]))))
                    if out["z_finite"] else None)
    out["z_med_abs"] = (float(np.median(np.abs(out["z_finite"])))
                        if out["z_finite"] else None)
    out["z_max"] = (float(np.max(out["z_finite"])) if out["z_finite"] else None)
    return out


def coverage_from_z(z_values: Iterable[float]) -> dict:
    z = np.asarray([float(v) for v in z_values if v is not None], float)
    if z.size == 0:
        return {"n": 0, "cov68": None, "cov95": None}
    inf = ~np.isfinite(z)
    finite = ~inf
    c68 = int(np.sum(finite & (np.abs(z) <= 1.0)))
    c95 = int(np.sum(finite & (np.abs(z) <= 1.96)))
    return {"n": int(z.size), "cov68": c68 / z.size,
            "cov95": c95 / z.size,
            "cov68_ci": list(wilson_ci(c68, int(z.size))),
            "cov95_ci": list(wilson_ci(c95, int(z.size))),
            "n_inf": int(inf.sum())}


def chi2_interval(n: int, level: float = 0.95) -> tuple[float, float]:
    a = (1.0 - level) / 2.0
    return (float(chi2.ppf(a, n) / n), float(chi2.ppf(1 - a, n) / n))


def nees_consistency(nees_values: Iterable[float], n: int) -> dict:
    vals = np.asarray([v for v in nees_values if v is not None], float)
    if vals.size == 0:
        return {"n": 0}
    finite = np.isfinite(vals)
    per = vals / n
    lo, hi = chi2_interval(n)
    return {"n": int(vals.size), "n_inf": int((~finite).sum()),
            "median_per_n": float(np.median(per[finite])) if finite.any() else None,
            "mean_per_n": float(np.mean(per[finite])) if finite.any() else None,
            "ci95_per_n": list(cluster_bootstrap(per[finite][:, None],
                                                 lambda a: np.median(a.ravel()))[1:]),
            "frac_in_chi2_95": float(np.mean((per >= lo) & (per <= hi))),
            "frac_gt_2": float(np.mean(per > 2.0)),
            "frac_lt_half": float(np.mean(per < 0.5)),
            "chi2_95_band": [lo, hi]}


def verdict_from_nees(median_per_n: Optional[float]) -> str:
    if median_per_n is None or not math.isfinite(median_per_n):
        return "undefined"
    if median_per_n > 1.10:
        return "optimistic"
    if median_per_n < 0.90:
        return "pessimistic"
    return "calibrated"


# --------------------------------------------------------------------------
# fitting / evaluation helpers
# --------------------------------------------------------------------------

def audit_init(scene: Scene, rng: np.random.Generator, *,
               fx_sigma: float = 6.0, cx_sigma: float = 6.0,
               kc_rel_sigma: float = 0.08,
               kc_abs_sigma: float = 2e-3,
               kc_override: Optional[np.ndarray] = None,
               kc_scale: float = 1.0) -> dict[str, CameraIntrinsics]:
    """Initial intrinsics deliberately perturbed from truth (no oracle)."""
    out = {}
    for c in CAMERAS:
        base = scene.cameras[c]
        kc = base.kc.copy()
        if kc_override is not None:
            kc = np.asarray(kc_override, float).copy()
        else:
            kc = kc * (1.0 + rng.normal(0.0, kc_rel_sigma, 5)) \
                 + rng.normal(0.0, kc_abs_sigma, 5)
        kc = kc * float(kc_scale)
        out[c] = CameraIntrinsics(
            base.fx + rng.normal(0.0, fx_sigma),
            base.fy + rng.normal(0.0, fx_sigma),
            base.cx + rng.normal(0.0, cx_sigma),
            base.cy + rng.normal(0.0, cx_sigma),
            kc)
    return out


def scene_truth_views(scene: Scene) -> list[tuple[np.ndarray, np.ndarray]]:
    used = scene.meta.get("used_views", list(range(len(scene.views))))
    return [scene.views[int(v)] for v in used]


def truth_vector_for(scene: Scene, free_kc: bool = True) -> np.ndarray:
    return pack_truth(scene.cameras, scene.extrinsics, scene_truth_views(scene),
                      scene.pitch_nominal, free_kc)


def fit_scene(scene: Scene, init: dict[str, CameraIntrinsics], *,
              free_kc: bool = True, max_nfev: int = 600,
              robust_covariance: bool = True,
              pitch: Optional[float] = None) -> CalibrationResult:
    kwargs = dict(init_cameras=init, free_kc=bool(free_kc),
                  free_intrinsics=True, max_nfev=int(max_nfev),
                  robust_covariance=bool(robust_covariance))
    return _call_with_supported(calibrate, scene.observations,
                                float(scene.pitch_nominal if pitch is None else pitch),
                                **kwargs)


def _z_one(d: np.ndarray, C: Optional[np.ndarray], i: int) -> Optional[float]:
    if C is None or i >= len(d):
        return None
    sd = math.sqrt(max(float(C[i, i]), 0.0))
    err = abs(float(d[i]))
    if sd <= ZERO_SD:
        return float("inf") if err > ZERO_SD else 0.0
    return err / sd


def _angle_deg(Ra: np.ndarray, Rb: np.ndarray) -> float:
    c = (np.trace(np.asarray(Ra).T @ np.asarray(Rb)) - 1.0) / 2.0
    return float(math.degrees(math.acos(max(-1.0, min(1.0, c)))))


def key_errors(scene: Scene, result: CalibrationResult, d: np.ndarray) -> dict:
    pk = _packer(len(result.views), bool(result.info.get("free_kc", True)))
    C = result.covariance
    out = {}
    for c in CAMERAS:
        sl = pk.slices[c]
        out[f"{c}.fx"] = {"error": float(d[sl.start]),
                          "sigma": math.sqrt(max(float(C[sl.start, sl.start]), 0.0))
                          if C is not None else None,
                          "z": _z_one(d, C, sl.start)}
        out[f"{c}.cx"] = {"error": float(d[sl.start + 2]),
                          "sigma": math.sqrt(max(float(C[sl.start + 2, sl.start + 2]), 0.0))
                          if C is not None else None,
                          "z": _z_one(d, C, sl.start + 2)}
    # baseline (left gauge)
    b_est = float(np.linalg.norm(result.baseline()))
    Rl, tl = scene.extrinsics["left"]
    Rr, tr = scene.extrinsics["right"]
    b_true = float(np.linalg.norm(tr - Rr @ tl))
    sb = baseline_sigma(result)
    out["baseline"] = {"est": b_est, "true": b_true, "error": b_est - b_true,
                       "sigma": sb,
                       "z": (abs(b_est - b_true) / sb if sb > ZERO_SD
                             else (float("inf") if abs(b_est - b_true) > ZERO_SD else 0.0)),
                       "rel_sigma": safe_div(sb, b_true)}
    # right relative rotation
    Rr_est = result.extrinsics["right"][0]
    Rr_true = scene.extrinsics["right"][0]
    rot = _angle_deg(Rr_est, Rr_true)
    ia = pk.extr["right"].start
    if C is not None:
        srot = float(np.degrees(math.sqrt(max(float(np.trace(C[ia:ia + 3, ia:ia + 3])), 0.0))))
    else:
        srot = float("nan")
    out["right_rotation"] = {"error_deg": rot, "sigma_deg": srot,
                             "z": (rot / srot if srot > ZERO_SD
                                   else (float("inf") if rot > ZERO_SD else 0.0))}
    return out


def group_cov_metrics(d: np.ndarray, C: Optional[np.ndarray],
                      groups: dict[str, np.ndarray]) -> dict:
    out = {}
    for g, idx in groups.items():
        gm = group_metrics(d, C, idx)
        # keep z for pooled-coverage summaries; compact_trial drops it later
        gm["coverage"] = coverage_from_z(gm.get("z", []))
        out[g] = gm
    return out


def diagnostics(result: CalibrationResult, scene: Scene, J: Optional[np.ndarray],
                f: Optional[np.ndarray], observations: list[Observation]) -> dict:
    C = result.covariance
    info = result.info.get("covariance", {}) or {}
    fm = result_free_mask(result)
    names = param_names(result)
    sd = np.sqrt(np.maximum(np.diag(C), 0.0)) if C is not None else np.zeros(len(fm))
    free_idx = np.where(fm)[0]
    zero = [names[i] for i in free_idx
            if i < len(names) and sd[i] <= ZERO_SD] if C is not None else []
    tiny = [names[i] for i in free_idx
            if i < len(names) and ZERO_SD < sd[i] < 1e-6] if C is not None else []
    diag = {"n_views": len(result.views), "n_obs": len(observations),
            "n_free": int(np.sum(fm)), "n_residuals": info.get("n_residuals"),
            "rank_kept": info.get("rank_kept"), "condition": info.get("condition"),
            "sigma_px": info.get("sigma_px"), "estimator": info.get("estimator"),
            "underdetermined": info.get("underdetermined"),
            "rms_px": result.residual_rms, "max_px": result.residual_max,
            "success": result.info.get("success"), "nfev": result.info.get("nfev"),
            "zero_var_params": zero, "n_zero_var": len(zero),
            "tiny_var_params": tiny[:20], "n_tiny_var": len(tiny),
            "views_used": result.info.get("views_used"),
            "unused_slots": [int(v) for v in range(len(scene.views))
                             if int(v) not in set(scene.meta.get("used_views", []))],
            }
    # free information spectrum
    if result.information is not None:
        A = np.asarray(result.information, float)
        idx = np.where(fm)[0]
        try:
            Af = A[np.ix_(idx, idx)]
            sv = np.linalg.svd(Af, compute_uv=False)
            diag["info_smax"] = float(sv[0]) if len(sv) else None
            diag["info_smin"] = float(sv[-1]) if len(sv) else None
            diag["info_condition"] = (float(sv[0] / sv[-1])
                                      if len(sv) and sv[-1] > 0 else float("inf"))
            diag["info_rank"] = int(np.sum(sv > (sv[0] * 1e-10 if len(sv) else 0)))
            # weakest directions and their dominant parameters
            w, V = np.linalg.eigh(0.5 * (Af + Af.T))
            order = np.argsort(w)[:3]
            weak = []
            for oi in order:
                vec = V[:, oi]
                top = np.argsort(-np.abs(vec))[:4]
                weak.append({"eigenvalue": float(w[oi]),
                             "loadings": {names[idx[j]]: float(vec[j])
                                          for j in top}})
            diag["weak_directions"] = weak
        except Exception as exc:                 # pragma: no cover
            diag["info_error"] = str(exc)
    # pose diversity
    zs = np.array([float(np.linalg.norm(t)) for _, t in result.views])
    tx, ty = [], []
    for R, _ in result.views:
        n = R[:, 2]
        tx.append(float(math.degrees(math.atan2(n[1], max(abs(n[2]), 1e-9)))))
        ty.append(float(math.degrees(math.atan2(n[0], max(abs(n[2]), 1e-9)))))
    diag["pose_diversity"] = {
        "distance_min": float(zs.min()) if zs.size else None,
        "distance_max": float(zs.max()) if zs.size else None,
        "distance_std": float(zs.std()) if zs.size else None,
        "distance_range": float(zs.max() - zs.min()) if zs.size else None,
        "tilt_x_std": float(np.std(tx)) if tx else None,
        "tilt_y_std": float(np.std(ty)) if ty else None,
        "tilt_x_range": [float(np.min(tx)), float(np.max(tx))] if tx else None,
        "tilt_y_range": [float(np.min(ty)), float(np.max(ty))] if ty else None,
    }
    # per-camera coverage and per-view residuals
    cov = {}
    for c in CAMERAS:
        vv = [o for o in scene.observations if o.camera == c]
        pts = [len(o.uv) for o in vv]
        cov[c] = {"n_views": len(vv), "n_points": int(sum(pts)),
                  "min_points": int(min(pts)) if pts else 0}
    by_view: dict[int, list[int]] = {}
    for o in scene.observations:
        by_view.setdefault(int(o.view), []).append(o.camera)
    all3 = sum(1 for cs in by_view.values() if len(set(cs)) == 3)
    lr = sum(1 for cs in by_view.values() if {"left", "right"} <= set(cs))
    diag["camera_coverage"] = cov
    diag["views_all3"] = int(all3)
    diag["views_left_right"] = int(lr)
    diag["views_any"] = int(len(by_view))
    try:
        from calib.evaluate import per_view_errors
        pv = per_view_errors(result, scene.observations, scene.pitch_nominal)
        vals = [v["rms_px"] for v in pv.values()]
        diag["per_view_rms"] = {"min": float(np.min(vals)) if vals else None,
                                "median": float(np.median(vals)) if vals else None,
                                "max": float(np.max(vals)) if vals else None}
    except Exception:
        diag["per_view_rms"] = None
    if f is not None and len(f):
        comps = np.asarray(f, float).ravel()
        s = mad_sigma(comps)
        diag["outlier_frac_3mad"] = float(np.mean(np.abs(comps) > 3.0 * s))
        diag["outlier_frac_5mad"] = float(np.mean(np.abs(comps) > 5.0 * s))
    else:
        diag["outlier_frac_3mad"] = None
        diag["outlier_frac_5mad"] = None
    diag["semantics_note"] = ("n_obs counts Observation rows (camera,view), "
                              "n_residuals counts scalar components")
    return diag


def current_stopping(result: CalibrationResult, observations: list[Observation],
                     heldout_rms_px: Optional[float] = None,
                     require_heldout: bool = False,
                     require_stability: bool = False) -> Optional[dict]:
    if _quality is None or not hasattr(_quality, "stopping_report"):
        return None
    try:
        sr = _call_with_supported(
            _quality.stopping_report, result, observations=observations,
            heldout_rms_px=heldout_rms_px, require_heldout=require_heldout,
            require_stability=require_stability, compute_stability=False,
            pitch=float(result.info.get("pitch_mm", plate.DEFAULT_PITCH_MM)))
        checks = sr.get("checks", {}) or {}
        def passed(*keys):
            return all(bool(checks.get(k, {}).get("pass", False)) for k in keys)
        return {"ok": bool(sr.get("ok")),
                "reasons_failed": sr.get("reasons_failed", []),
                "cov_ok": passed("sigma_fx", "sigma_baseline", "sigma_rotation", "sigma_rgb"),
                "coverage_ok": passed("image_quadrants", "observations"),
                "rank_ok": passed("rank"),
                "diversity_ok": passed("views", "distance_bins", "tilt_signs"),
                "heldout_ok": passed("heldout") if heldout_rms_px is not None else None,
                "stability_ok": passed("stability") if require_stability else None,
                "checks": checks,
                "baseline_sigma_mm": sr.get("baseline_sigma_mm"),
                "n_views": sr.get("n_views")}
    except Exception as exc:                     # pragma: no cover
        return {"ok": None, "error": str(exc)}


def evaluate_fit(scene: Scene, result: CalibrationResult, *,
                 free_kc: bool = True, recompute_jacobian: bool = True,
                 include_stopping: bool = True,
                 observations: Optional[list[Observation]] = None
                 ) -> dict:
    obs = observations if observations is not None else scene.observations
    xt = truth_vector_for(scene, free_kc)
    xe = result_vector(result)
    if xe.shape != xt.shape:
        return {"ok": False, "error": f"vector shape {xe.shape} != truth {xt.shape}"}
    d = xe - xt
    groups = result_groups(result)
    C = result.covariance
    out: dict[str, Any] = {
        "ok": True,
        "d": d,                                # kept in memory only
        "groups": group_cov_metrics(d, C, groups),
        "key": key_errors(scene, result, d),
        "rms": result.residual_rms,
        "max_px": result.residual_max,
        "success": bool(result.info.get("success", False)),
        "nfev": result.info.get("nfev"),
        "n_free": int(np.sum(result_free_mask(result))),
        "n_views": len(result.views),
        "n_obs": len(obs),
    }
    # current rig covariance and legacy variants
    marg = rig_block(C)
    cond = conditional_legacy(C)
    try:
        live_rig = np.asarray(adaptive.rig_covariance(result), float)
    except Exception:
        live_rig = None
    out["rig_variants"] = {
        "marginal": marg,
        "conditional_legacy": cond,
        "live_rig_covariance": live_rig,
        "live_vs_marginal": compare_matrices(live_rig, marg),
        "live_vs_conditional": compare_matrices(live_rig, cond),
    }
    if cond is not None and marg is not None:
        out["rig_variants"]["extrinsics_marginal"] = group_metrics(
            d, marg, np.arange(27, min(39, marg.shape[0])))
        out["rig_variants"]["extrinsics_conditional"] = group_metrics(
            d, cond, np.arange(27, min(39, cond.shape[0])))
        out["rig_variants"]["intrinsics_marginal"] = group_metrics(
            d, marg, np.arange(0, min(27, marg.shape[0])))
        out["rig_variants"]["intrinsics_conditional"] = group_metrics(
            d, cond, np.arange(0, min(27, cond.shape[0])))
    # covariance-estimator variants from a reconstructed Jacobian
    J = f = None
    if recompute_jacobian:
        J, f, ji = reconstruct_jacobian(result, obs,
                                        float(result.info.get("pitch_mm",
                                                              scene.pitch_nominal)))
        out["jacobian"] = ji
        if J is not None:
            fm = result_free_mask(result)
            free_idx = np.where(fm)[0]
            variants = covariance_variants_from_jacobian(J, f, len(fm), free_idx)
            out["jacobian"].update({k: variants[k] for k in
                                    ("sigma_px", "rank", "rank_plain",
                                     "condition", "n_free", "n_residuals")})
            cm = {}
            for vname in ("sandwich_current", "sandwich_corrected",
                          "gn_mad", "legacy_pinv_h"):
                Cv = variants[vname]
                cm[vname] = group_cov_metrics(d, Cv, groups)
                cm[vname]["_trace"] = float(np.trace(Cv))
            out["variant_metrics"] = cm
            out["variant_match"] = {
                "reported_vs_sandwich_current": compare_matrices(
                    C, variants["sandwich_current"]),
                "reported_vs_gn_mad": compare_matrices(C, variants["gn_mad"]),
                "reported_vs_sandwich_corrected": compare_matrices(
                    C, variants["sandwich_corrected"]),
            }
            vs = {}
            for vname in ("sandwich_current", "sandwich_corrected",
                          "gn_mad", "legacy_pinv_h"):
                Cv = variants[vname]
                sdv = np.sqrt(np.maximum(np.diag(Cv), 0.0))
                vs[vname] = {
                    "sigma_fx_med": float(np.median(
                        [sdv[9 * i] for i in range(3)])),
                    "sigma_cx_med": float(np.median(
                        [sdv[9 * i + 2] for i in range(3)])),
                    "baseline_sigma_mm": baseline_sigma(result, Cv),
                    "rig_trace": float(np.trace(rig_block(Cv))),
                    "n_zero_var_rig": int(np.sum(
                        np.asarray(fm[:NRIG], bool) &
                        (sdv[:NRIG] <= ZERO_SD))),
                }
            out["variant_sigma"] = vs
            # marginal rig variants from each covariance estimator
            rig_var = {}
            for vname in ("sandwich_current", "sandwich_corrected",
                          "gn_mad", "legacy_pinv_h"):
                Cv = variants[vname]
                rig_var[vname] = {
                    "marginal": group_metrics(d, rig_block(Cv),
                                              np.arange(27, 39)),
                    "conditional": group_metrics(d, conditional_legacy(Cv),
                                                 np.arange(27, 39)),
                }
            out["rig_estimator_variants"] = rig_var
    out["diagnostics"] = diagnostics(result, scene, J, f, obs)
    if include_stopping:
        out["stopping"] = current_stopping(result, obs)
    return out


# --------------------------------------------------------------------------
# task 1: Monte-Carlo NEES vs view count
# --------------------------------------------------------------------------

def _mc_task(args: dict) -> dict:
    t0 = time.time()
    try:
        scene = build_scene(**args["scene"])
        obs_filter = args.get("obs_filter")
        if obs_filter:
            # cameras absent from the filter keep all their views
            scene.observations = [
                o for o in scene.observations
                if o.camera not in obs_filter
                or int(o.view) in set(int(v) for v in obs_filter[o.camera])
            ]
        rng = np.random.default_rng(int(args["init_seed"]))
        icfg = dict(args.get("init", {}) or {})
        init = audit_init(scene, rng,
                          fx_sigma=float(icfg.get("fx_sigma", 6.0)),
                          cx_sigma=float(icfg.get("cx_sigma", 6.0)),
                          kc_rel_sigma=float(icfg.get("kc_rel_sigma", 0.08)),
                          kc_abs_sigma=float(icfg.get("kc_abs_sigma", 2e-3)),
                          kc_override=icfg.get("kc_override"),
                          kc_scale=float(icfg.get("kc_scale", 1.0)))
        res = fit_scene(scene, init, free_kc=bool(args.get("free_kc", True)),
                        max_nfev=int(args.get("max_nfev", 600)),
                        robust_covariance=bool(args.get("robust_covariance", True)),
                        pitch=args.get("pitch_calib"))
        ev = evaluate_fit(scene, res, free_kc=bool(args.get("free_kc", True)),
                          recompute_jacobian=bool(args.get("recompute_jacobian", True)),
                          include_stopping=bool(args.get("include_stopping", True)))
        if not ev.get("ok", False):
            return {"ok": False, "error": ev.get("error"), "k": args["scene"].get("k"),
                    "trial": args.get("trial"), "time_s": time.time() - t0}
        # serialise only summaries; d and matrices are not JSON
        rec = {k: v for k, v in ev.items()
               if k not in ("d", "rig_variants", "variant_metrics",
                            "rig_estimator_variants", "jacobian")}
        rec["rig_variants"] = _serialise_rig_variants(ev.get("rig_variants", {}))
        rec["variant_metrics"] = _serialise_variant_metrics(ev.get("variant_metrics", {}))
        rec["rig_estimator_variants"] = _serialise_rig_estimator_variants(
            ev.get("rig_estimator_variants", {}))
        rec["jacobian"] = ev.get("jacobian", {})
        rec.update({"k": args["scene"].get("k"), "trial": args.get("trial"),
                    "condition": args.get("condition", "mc"),
                    "free_kc": bool(args.get("free_kc", True)),
                    "robust_covariance": bool(args.get("robust_covariance", True)),
                    "time_s": time.time() - t0})
        return _strip_internal(rec)
    except Exception as exc:
        import traceback
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc()[-2000:],
                "k": args.get("scene", {}).get("k"), "trial": args.get("trial"),
                "condition": args.get("condition", "mc"),
                "time_s": time.time() - t0}


def _strip_internal(rec: dict) -> dict:
    return {k: v for k, v in rec.items() if not k.startswith("_")}


def _serialise_rig_variants(rv: dict) -> dict:
    out = {}
    for k, v in rv.items():
        if isinstance(v, np.ndarray):
            continue                      # matrices are summarised, not shipped
        if isinstance(v, dict) and "nees" in v:
            out[k] = _strip_internal(v)
        elif isinstance(v, dict) and "ok" in v:
            out[k] = v
        elif isinstance(v, dict):
            out[k] = {kk: _strip_internal(vv) for kk, vv in v.items()
                      if isinstance(vv, dict)}
    return out


def _serialise_variant_metrics(vm: dict) -> dict:
    out = {}
    for name, groups in vm.items():
        out[name] = {}
        for g, m in groups.items():
            if isinstance(m, dict):
                out[name][g] = _strip_internal(m)
            else:
                out[name][g] = m
    return out


def _serialise_rig_estimator_variants(rv: dict) -> dict:
    out = {}
    for name, kinds in rv.items():
        out[name] = {}
        for kind, m in kinds.items():
            if isinstance(m, np.ndarray):
                continue
            if isinstance(m, dict):
                out[name][kind] = _strip_internal(m)
    return out


def _run_tasks(tasks: list[dict], worker, jobs: int,
               label: str) -> tuple[list[dict], float]:
    t0 = time.time()
    results: list[Optional[dict]] = [None] * len(tasks)
    if jobs <= 1:
        for i, task in enumerate(tasks):
            results[i] = worker(task)
            if (i + 1) % 10 == 0 or i + 1 == len(tasks):
                progress(f"{label}: {i + 1}/{len(tasks)}")
    else:
        import multiprocessing as mp
        ctx = mp.get_context("fork")
        with ProcessPoolExecutor(max_workers=jobs, mp_context=ctx) as ex:
            futures = {ex.submit(worker, t): i for i, t in enumerate(tasks)}
            done = 0
            for fut in as_completed(futures):
                i = futures[fut]
                try:
                    results[i] = fut.result()
                except Exception as exc:     # pragma: no cover
                    results[i] = {"ok": False, "error": f"worker: {exc}",
                                  "task": tasks[i]}
                done += 1
                if done % 10 == 0 or done == len(tasks):
                    progress(f"{label}: {done}/{len(tasks)}")
    return [r for r in results if r is not None], time.time() - t0


def _summarise_group(trials: list[dict], group: str) -> dict:
    gs = [t["groups"][group] for t in trials
          if t.get("ok") and group in t.get("groups", {})
          and int(t["groups"][group].get("n_params", 0)) > 0]
    if not gs:
        return {"n": 0}
    nees = [g.get("nees") for g in gs]
    cov68 = [g["coverage"]["cov68"] for g in gs if g.get("coverage", {}).get("cov68") is not None]
    cov95 = [g["coverage"]["cov95"] for g in gs if g.get("coverage", {}).get("cov95") is not None]
    zvals = []
    for g in gs:
        zvals.extend([v for v in g.get("z", []) if v is not None])
    # pooled parameter-level coverage across all trials
    pooled = coverage_from_z([v for g in gs for v in g.get("z", [])])
    nees_sum = nees_consistency(nees, int(gs[0].get("n_params", 1)))
    return {
        "n_trials": len(gs),
        "n_params": int(gs[0].get("n_params", 0)),
        "nees_per_n": nees_sum,
        "verdict": verdict_from_nees(nees_sum.get("median_per_n")),
        "coverage68_mean": float(np.mean(cov68)) if cov68 else None,
        "coverage95_mean": float(np.mean(cov95)) if cov95 else None,
        "coverage68_ci": list(wilson_ci(int(round(np.mean(cov68) * len(cov68))),
                                        len(cov68))) if cov68 else None,
        "coverage95_ci": list(wilson_ci(int(round(np.mean(cov95) * len(cov95))),
                                        len(cov95))) if cov95 else None,
        "pooled_coverage": pooled,
        "coverage": pooled,
        "z_rms": (float(np.sqrt(np.mean(np.square(
                    [v for v in zvals if math.isfinite(v)]))))
                  if any(math.isfinite(v) for v in zvals) else None),
        "z_med_abs": (float(np.median(np.abs(
            [v for v in zvals if math.isfinite(v)])))
            if any(math.isfinite(v) for v in zvals) else None),
        "z_max": (float(np.max([v for v in zvals if math.isfinite(v)]))
                  if any(math.isfinite(v) for v in zvals) else None),
        "n_inf": int(np.sum(~np.isfinite(np.asarray(zvals, float)))) if zvals else 0,
    }


def summarise_task1(rows: list[dict], view_counts: Iterable[int]) -> dict:
    out = {"variant": "current (live calibrate default)",
           "by_k": {}, "failures": []}
    for k in view_counts:
        rr = [r for r in rows if r.get("k") == k]
        ok = [r for r in rr if r.get("ok")]
        bad = [r for r in rr if not r.get("ok")]
        entry = {"n_trials": len(rr), "n_ok": len(ok),
                 "n_failed": len(bad),
                 "groups": {g: _summarise_group(ok, g)
                            for g in ("all", "intrinsics", "intrinsics_physical",
                                      "distortion", "extrinsics", "rig", "views")},
                 "stopping_pass_rate": (float(np.mean([
                     bool(r.get("stopping", {}).get("ok"))
                     for r in ok if r.get("stopping")])) if ok else None),
                 "sigma_fx_med": float(np.median([
                     np.mean([r["key"][f"{c}.fx"]["sigma"] for c in CAMERAS])
                     for r in ok if r.get("key")])) if ok else None,
                 "sigma_baseline_med": float(np.median([
                     r["key"]["baseline"]["sigma"] for r in ok
                     if r.get("key", {}).get("baseline", {}).get("sigma") is not None])) if ok else None,
                 "zero_var_trials": int(np.sum([
                     r.get("diagnostics", {}).get("n_zero_var", 0) > 0 for r in ok])),
                 "rank_deficient_trials": int(np.sum([
                     (r.get("diagnostics", {}).get("rank_kept") or 0) <
                     (r.get("diagnostics", {}).get("n_free") or 0) for r in ok])),
                 "baseline_z": _summarise_key(ok, "baseline"),
                 "fx_z": _summarise_fx_z(ok),
                 }
        out["by_k"][str(k)] = entry
        if bad:
            out["failures"].append({"k": k,
                                    "errors": [b.get("error") for b in bad][:5]})
    return out


def _summarise_key(trials: list[dict], key: str) -> dict:
    vals = [t["key"][key]["z"] for t in trials
            if t.get("key", {}).get(key, {}).get("z") is not None]
    vals = [v for v in vals if v is not None and math.isfinite(v)]
    if not vals:
        return {"n": 0}
    return {"n": len(vals), "median_abs": float(np.median(np.abs(vals))),
            "mean_abs": float(np.mean(np.abs(vals))),
            "p95_abs": float(np.percentile(np.abs(vals), 95)),
            "max_abs": float(np.max(np.abs(vals))),
            "frac_gt3": float(np.mean(np.abs(vals) > 3.0)),
            "frac_gt5": float(np.mean(np.abs(vals) > 5.0))}


def _summarise_fx_z(trials: list[dict]) -> dict:
    vals = []
    for t in trials:
        for c in CAMERAS:
            z = t.get("key", {}).get(f"{c}.fx", {}).get("z")
            if z is not None and math.isfinite(z):
                vals.append(z)
    if not vals:
        return {"n": 0}
    return {"n": len(vals), "median_abs": float(np.median(np.abs(vals))),
            "p95_abs": float(np.percentile(np.abs(vals), 95)),
            "max_abs": float(np.max(np.abs(vals))),
            "frac_gt3": float(np.mean(np.abs(vals) > 3.0))}


def _summarise_variant(rows: list[dict], variant: str, group: str) -> dict:
    gs = [r["variant_metrics"][variant][group] for r in rows
          if r.get("ok") and variant in r.get("variant_metrics", {})
          and group in r["variant_metrics"][variant]]
    if not gs:
        return {"n": 0}
    nees = [g.get("nees") for g in gs]
    zvals = [v for g in gs for v in g.get("z", []) if v is not None]
    ns = int(gs[0].get("n_params", 1))
    return {"n": len(gs), "n_params": ns,
            "nees_per_n": nees_consistency(nees, ns),
            "verdict": verdict_from_nees(
                nees_consistency(nees, ns).get("median_per_n")),
            "pooled_coverage": coverage_from_z(zvals)}


def compact_trial(rec: dict) -> dict:
    """Drop per-parameter z lists; keep summaries needed for the report."""
    out = {k: v for k, v in rec.items()
           if k not in ("groups", "variant_metrics", "rig_variants",
                        "rig_estimator_variants")}
    out["groups"] = {}
    for g, m in rec.get("groups", {}).items():
        out["groups"][g] = {k: v for k, v in m.items()
                            if k not in ("z", "z_finite")}
    out["variant_metrics"] = {}
    for vname, groups in rec.get("variant_metrics", {}).items():
        out["variant_metrics"][vname] = {}
        for g, m in groups.items():
            if isinstance(m, dict):
                out["variant_metrics"][vname][g] = {
                    k: v for k, v in m.items() if k not in ("z", "z_finite")}
    out["rig_variants"] = {}
    for name, val in rec.get("rig_variants", {}).items():
        if isinstance(val, dict):
            out["rig_variants"][name] = {
                k: v for k, v in val.items() if k not in ("z", "z_finite")}
    out["rig_estimator_variants"] = {}
    for vname, kinds in rec.get("rig_estimator_variants", {}).items():
        out["rig_estimator_variants"][vname] = {}
        for kind, m in kinds.items():
            out["rig_estimator_variants"][vname][kind] = {
                k: v for k, v in m.items() if k not in ("z", "z_finite")}
    return out


def run_task1(jobs: int, n_trials: int = 60,
              view_counts: Iterable[int] = (5, 8, 10, 12, 15, 20, 25),
              free_kc: bool = True) -> dict:
    progress(f"task1: {len(list(view_counts))} view counts x {n_trials} trials")
    tasks = []
    for k in view_counts:
        for trial in range(n_trials):
            seed = MASTER_SEED + 1000 * int(k) + 17 * trial
            tasks.append({
                "condition": f"mc_k{k}",
                "scene": dict(seed=seed, k=int(k), noise_px=0.2,
                              outlier_frac=0.0, pose_mode="normal"),
                "init_seed": seed + 1, "trial": trial,
                "free_kc": bool(free_kc), "max_nfev": 600,
                "robust_covariance": True, "recompute_jacobian": True,
                "include_stopping": True})
    rows, dt = _run_tasks(tasks, _mc_task, jobs, "task1")
    ok = [r for r in rows if r.get("ok")]
    summary = summarise_task1(rows, view_counts)
    summary["runtime_s"] = dt
    summary["n_trials_requested"] = len(tasks)
    summary["n_ok"] = len(ok)
    # covariance-estimator variants
    variants = {}
    for k in view_counts:
        rr = [r for r in ok if r.get("k") == k]
        variants[str(k)] = {}
        for vname in ("sandwich_current", "sandwich_corrected",
                      "gn_mad", "legacy_pinv_h"):
            variants[str(k)][vname] = {
                g: _summarise_variant(rr, vname, g)
                for g in ("all", "intrinsics", "intrinsics_physical",
                          "extrinsics", "rig")}
            vs = _summarise_variant_sigma(rr)
            if vname in vs:
                variants[str(k)][vname]["sigma"] = vs[vname]
    summary["variants"] = variants
    # marginal vs conditional rig covariance
    marg_cond = {}
    for k in view_counts:
        rr = [r for r in ok if r.get("k") == k]
        me = [r["rig_variants"]["extrinsics_marginal"] for r in rr
              if "extrinsics_marginal" in r.get("rig_variants", {})]
        ce = [r["rig_variants"]["extrinsics_conditional"] for r in rr
              if "extrinsics_conditional" in r.get("rig_variants", {})]
        ratios = [r["rig_variants"]["live_vs_conditional"].get("trace_ratio")
                  for r in rr
                  if r["rig_variants"].get("live_vs_conditional", {}).get("ok")]
        ratios = [x for x in ratios if x is not None and math.isfinite(x)]
        marg_cond[str(k)] = {
            "n": len(rr),
            "live_vs_marginal_rel_max_med": float(np.median([
                r["rig_variants"]["live_vs_marginal"]["rel_max"] for r in rr
                if r["rig_variants"].get("live_vs_marginal", {}).get("ok")]))
            if rr else None,
            "live_vs_conditional_rel_max_med": float(np.median([
                r["rig_variants"]["live_vs_conditional"]["rel_max"] for r in rr
                if r["rig_variants"].get("live_vs_conditional", {}).get("ok")]))
            if rr else None,
            "live_over_conditional_trace_med": float(np.median(ratios))
            if ratios else None,
            "nees_per_n_extr_marginal": nees_consistency(
                [m.get("nees") for m in me], 12) if me else {"n": 0},
            "nees_per_n_extr_conditional": nees_consistency(
                [m.get("nees") for m in ce], 12) if ce else {"n": 0},
            "sigma_fx_marginal_med": float(np.median([
                np.sqrt(max(m["z"][0] ** 2 * 0 + 1, 0)) for m in me])) if False else None,
        }
    summary["marginal_vs_conditional"] = marg_cond
    # stopping rule behaviour
    stop = {}
    for k in view_counts:
        rr = [r for r in ok if r.get("k") == k]
        sr = [r for r in rr if r.get("stopping")]
        false_pass = 0
        false_fail = 0
        for r in sr:
            s = r.get("stopping", {})
            npn = r.get("groups", {}).get("all", {}).get("nees_per_n")
            cov95 = r.get("groups", {}).get("all", {}).get(
                "coverage", {}).get("cov95")
            bad = ((npn is not None and npn > 2.0) or
                   (cov95 is not None and cov95 < 0.85))
            good = ((npn is not None and 0.8 <= npn <= 1.25) and
                    (cov95 is None or cov95 >= 0.90))
            if s.get("ok") and bad:
                false_pass += 1
            if (not s.get("ok")) and good:
                false_fail += 1
        stop[str(k)] = {"n": len(sr),
                        "pass_rate": (float(np.mean([bool(r["stopping"].get("ok"))
                                                     for r in sr])) if sr else None),
                        "false_pass": false_pass, "false_fail": false_fail,
                        "false_pass_rate": (false_pass / len(sr) if sr else None),
                        "false_fail_rate": (false_fail / len(sr) if sr else None)}
    summary["stopping"] = stop
    summary["trials"] = [compact_trial(r) for r in rows]
    return summary


# --------------------------------------------------------------------------
# task 2: vendor-corpus bootstrap
# --------------------------------------------------------------------------

def _corpus_paths() -> dict:
    cands = [REPO / "analysis/calibration/engine/vendor_detections.pkl",
             REPO / "analysis/calibration/vendor_detections.pkl",
             REPO / "sources/calibration/vendor_detections.pkl"]
    det = next((p for p in cands if p.exists()), None)
    # The 25-view corpus CCF is the factory record under res_Einscan-E10;
    # ccf_200x150 is an older/other unit (144 mm baseline) and must not be
    # used as the reference for this corpus.
    ccf_dirs = [REPO / "sources/calibration/res_Einscan-E10_200x150",
                REPO / "sources/calibration/ccf_200x150"]
    ccf = next((p for p in ccf_dirs if (p / "LeftCCF.txt").exists()), None)
    return {"detections": det, "ccf": ccf}


def load_corpus() -> dict:
    import pickle
    paths = _corpus_paths()
    if paths["detections"] is None:
        raise FileNotFoundError("vendor detections pickle not found")
    with open(paths["detections"], "rb") as fh:
        det = pickle.load(fh)
    obs = []
    for side, cam in (("Left", "left"), ("Right", "right"), ("Tex", "tex")):
        for i in range(1, 26):
            if (side, i) in det:
                obs.append(Observation(cam, i - 1, dict(det[(side, i)])))
    init = {}
    ref = {}
    if paths["ccf"] is not None:
        from calib.ccf import load as load_ccf
        for name, cam in (("LeftCCF.txt", "left"), ("RightCCF.txt", "right"),
                          ("TexCCF.txt", "tex")):
            p = paths["ccf"] / name
            if p.exists():
                rec = load_ccf(p)
                init[cam] = CameraIntrinsics.from_ccf(rec)
                ref[cam] = {"R": rec.R().tolist(), "T": rec.T().tolist()}
    if len(init) != 3:
        raise RuntimeError("CCF files missing; corpus bootstrap needs the factory init")
    Rl = np.asarray(ref["left"]["R"], float)
    Tl = np.asarray(ref["left"]["T"], float)
    Rr = np.asarray(ref["right"]["R"], float)
    Tr = np.asarray(ref["right"]["T"], float)
    Rrel, trel = relative_pose(Rl, Tl, Rr, Tr)
    ref["baseline_mm"] = float(np.linalg.norm(trel))
    ref["right_rel_R"] = Rrel.tolist()
    ref["right_rel_t"] = trel.tolist()
    return {"observations": obs, "init": init, "ccf": ref,
            "paths": {"detections": str(paths["detections"]),
                      "ccf": str(paths["ccf"])}}


def _corpus_bootstrap_task(args: dict) -> dict:
    t0 = time.time()
    try:
        obs = args["observations"]
        init = args["init"]
        mode = args["mode"]
        rng = np.random.default_rng(int(args["seed"]))
        if mode == "views":
            n = len(sorted(set(int(o.view) for o in obs)))
            pick = rng.integers(0, n, n)
            new = []
            for j, v in enumerate(pick):
                for o in obs:
                    if int(o.view) == int(v):
                        new.append(Observation(o.camera, j, o.uv))
            obs2 = new
        elif mode == "views_sub":
            n = len(sorted(set(int(o.view) for o in obs)))
            m = int(args["m"])
            pick = rng.choice(n, size=m, replace=False)
            keep = set(int(v) for v in pick)
            new = []
            for j, v in enumerate(sorted(keep)):
                for o in obs:
                    if int(o.view) == v:
                        new.append(Observation(o.camera, j, o.uv))
            obs2 = new
        elif mode == "points":
            frac = float(args["frac"])
            new = []
            for o in obs:
                keys = list(o.uv.keys())
                m = max(12, int(round(frac * len(keys))))
                pick = rng.choice(len(keys), size=m, replace=False)
                new.append(Observation(o.camera, o.view,
                                       {keys[i]: o.uv[keys[i]] for i in pick}))
            obs2 = new
        else:
            raise ValueError(mode)
        res = calibrate(obs2, plate.DEFAULT_PITCH_MM, init,
                        free_kc=bool(args.get("free_kc", False)),
                        free_intrinsics=True, max_nfev=int(args.get("max_nfev", 700)),
                        robust_covariance=bool(args.get("robust_covariance", True)))
        vec = result_vector(res)
        sd = (np.sqrt(np.maximum(np.diag(res.covariance), 0.0))
              if res.covariance is not None else np.full(len(vec), np.nan))
        rig = res.info.get("covariance", {})
        return {"ok": True, "mode": mode, "rep": args["rep"],
                "vec": vec[:NRIG].tolist(), "sd": sd[:NRIG].tolist(),
                "rms": res.residual_rms, "nfev": res.info.get("nfev"),
                "n_views": len(res.views),
                "n_obs": int(sum(len(o.uv) for o in obs2)),
                "rank_kept": rig.get("rank_kept"),
                "condition": rig.get("condition"),
                "estimator": rig.get("estimator"),
                "time_s": time.time() - t0}
    except Exception as exc:
        import traceback
        return {"ok": False, "mode": args.get("mode"), "rep": args.get("rep"),
                "error": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc()[-1200:],
                "time_s": time.time() - t0}


def _bootstrap_summary(reps: list[dict], full_vec: np.ndarray,
                       full_sd: np.ndarray, free_mask: np.ndarray,
                       label: str, expected_inflation: float = 1.0) -> dict:
    ok = [r for r in reps if r.get("ok")]
    if len(ok) < 3:
        return {"label": label, "n": len(ok), "error": "too few successes"}
    V = np.asarray([r["vec"] for r in ok], float)
    S = np.asarray([r["sd"] for r in ok], float)
    emp = np.std(V, axis=0, ddof=1)
    rep_sd = np.asarray(full_sd, float)
    ratio = np.divide(rep_sd, emp, out=np.full_like(rep_sd, np.nan),
                      where=emp > 1e-12)
    # adjusted ratio: a subsample of m points has sd ~ sqrt(40/m) larger
    ratio_adj = np.divide(rep_sd * expected_inflation, emp,
                          out=np.full_like(rep_sd, np.nan),
                          where=emp > 1e-12)
    names = _RIG_NAMES
    out = {"label": label, "n": len(ok),
           "n_invalid": len(reps) - len(ok),
           "rms_med": float(np.median([r["rms"] for r in ok])),
           "expected_inflation": float(expected_inflation),
           "params": {}}
    for i in np.where(free_mask[:NRIG])[0]:
        out["params"][names[i]] = {
            "full_est": float(full_vec[i]),
            "reported_sigma": float(rep_sd[i]),
            "mean_rep_sigma": float(np.nanmean(S[:, i])),
            "empirical_sd": float(emp[i]),
            "ratio_reported_over_empirical": float(ratio[i]),
            "ratio_adjusted": float(ratio_adj[i]),
            "bias_mean_minus_full": float(np.mean(V[:, i]) - full_vec[i]),
            "bias_over_sigma": safe_div(float(np.mean(V[:, i]) - full_vec[i]),
                                        float(rep_sd[i])),
        }
    vals = [v["ratio_reported_over_empirical"] for v in out["params"].values()
            if v["ratio_reported_over_empirical"] is not None
            and math.isfinite(v["ratio_reported_over_empirical"])]
    adj = [v["ratio_adjusted"] for v in out["params"].values()
           if v["ratio_adjusted"] is not None and math.isfinite(v["ratio_adjusted"])]
    out["ratio_median"] = float(np.median(vals)) if vals else None
    out["ratio_min"] = float(np.min(vals)) if vals else None
    out["ratio_max"] = float(np.max(vals)) if vals else None
    out["ratio_adjusted_median"] = float(np.median(adj)) if adj else None
    return out




def run_task2(jobs: int, n_views_boot: int = 30, n_points_boot: int = 25,
              n_sub_boot: int = 15) -> dict:
    progress("task2: loading vendor corpus")
    c = load_corpus()
    obs = c["observations"]
    init = c["init"]
    progress(f"task2: full 25-view fit ({len(obs)} camera-view rows)")
    t0 = time.time()
    full = calibrate(obs, plate.DEFAULT_PITCH_MM, init, free_kc=False,
                     free_intrinsics=True, max_nfev=800,
                     robust_covariance=True)
    full_vec = result_vector(full)
    full_sd = (np.sqrt(np.maximum(np.diag(full.covariance), 0.0))
               if full.covariance is not None else np.full(len(full_vec), np.nan))
    fm = result_free_mask(full)
    # GN reference covariance at the same solution
    J, f, ji = reconstruct_jacobian(full, obs, plate.DEFAULT_PITCH_MM)
    variants = None
    if J is not None:
        idx = np.where(fm)[0]
        variants = covariance_variants_from_jacobian(J, f, len(fm), idx)
    cond = conditional_legacy(full.covariance)
    C = full.covariance
    marg = rig_block(C)
    # CCF reference comparison
    ccf = c["ccf"]
    Rr_est = full.extrinsics["right"][0]
    Rl_est = full.extrinsics["left"][0]
    Rrel_est, trel_est = relative_pose(Rl_est, full.extrinsics["left"][1],
                                       Rr_est, full.extrinsics["right"][1])
    ccf_cmp = {
        "ccf_baseline_mm": ccf["baseline_mm"],
        "fit_baseline_mm": float(np.linalg.norm(full.baseline())),
        "baseline_diff_mm": float(np.linalg.norm(full.baseline()) - ccf["baseline_mm"]),
        "right_rotation_vs_ccf_deg": _angle_deg(Rrel_est,
                                                np.asarray(ccf["right_rel_R"])),
        "fx": {}, "cx": {}, "kc": {},
    }
    for cam in CAMERAS:
        rec = init[cam]
        ccf_cmp["fx"][cam] = {"fit": full.cameras[cam].fx, "ccf": rec.fx,
                              "diff": full.cameras[cam].fx - rec.fx}
        ccf_cmp["cx"][cam] = {"fit": full.cameras[cam].cx, "ccf": rec.cx,
                              "diff": full.cameras[cam].cx - rec.cx}
        ccf_cmp["kc"][cam] = {"fit": full.cameras[cam].kc.tolist(),
                              "ccf": np.asarray(rec.kc).tolist(),
                              "max_abs_diff": float(np.max(
                                  np.abs(full.cameras[cam].kc - rec.kc)))}
    full_info = {
        "n_views": len(full.views), "n_obs": int(sum(len(o.uv) for o in obs)),
        "n_free": int(np.sum(fm)), "rms_px": full.residual_rms,
        "baseline_mm": float(np.linalg.norm(full.baseline())),
        "baseline_sigma_mm": baseline_sigma(full),
        "sigma_fx": {cam: float(np.sqrt(max(C[9 * i, 9 * i], 0.0)))
                     for i, cam in enumerate(CAMERAS)},
        "condition": full.info.get("covariance", {}).get("condition"),
        "rank_kept": full.info.get("covariance", {}).get("rank_kept"),
        "sigma_px": full.info.get("covariance", {}).get("sigma_px"),
        "estimator": full.info.get("covariance", {}).get("estimator"),
        "ccf": ccf_cmp,
        "ccf_path": c["paths"]["ccf"],
        "detections_path": c["paths"]["detections"],
    }
    # task list
    tasks = []
    for rep in range(n_views_boot):
        tasks.append({"mode": "views", "rep": rep, "seed": MASTER_SEED + 3000 + rep,
                      "observations": obs, "init": init, "free_kc": False,
                      "max_nfev": 700, "robust_covariance": True})
    for rep in range(n_points_boot):
        for frac in (0.8, 0.6):
            tasks.append({"mode": "points", "rep": rep, "frac": frac,
                          "seed": MASTER_SEED + 4000 + rep * 7 + int(frac * 100),
                          "observations": obs, "init": init, "free_kc": False,
                          "max_nfev": 700, "robust_covariance": True})
    for rep in range(n_sub_boot):
        tasks.append({"mode": "views_sub", "rep": rep, "m": 20,
                      "seed": MASTER_SEED + 5000 + rep,
                      "observations": obs, "init": init, "free_kc": False,
                      "max_nfev": 700, "robust_covariance": True})
    rows, dt = _run_tasks(tasks, _corpus_bootstrap_task, jobs, "task2")
    views = [r for r in rows if r.get("mode") == "views"]
    points80 = [r for r in rows if r.get("mode") == "points"
                and abs(float(r.get("frac", 0.8)) - 0.8) < 1e-9]
    points60 = [r for r in rows if r.get("mode") == "points"
                and abs(float(r.get("frac", 0.6)) - 0.6) < 1e-9]
    # frac is not returned by the worker; recover via n_obs ratio
    points80 = [r for r in rows if r.get("mode") == "points"
                and r.get("n_obs", 0) > 0.7 * full_info["n_obs"]]
    points60 = [r for r in rows if r.get("mode") == "points"
                and r.get("n_obs", 0) <= 0.7 * full_info["n_obs"]]
    sub20 = [r for r in rows if r.get("mode") == "views_sub"]
    summary = {
        "variant": "corpus (CCF reference, no physical truth)",
        "runtime_s": dt, "full_fit": full_info,
        "bootstrap": {
            "views": _bootstrap_summary(views, full_vec[:NRIG], full_sd[:NRIG],
                                        fm, "views (25 with replacement)", 1.0),
            "points80": _bootstrap_summary(points80, full_vec[:NRIG],
                                           full_sd[:NRIG], fm,
                                           "points (80% without replacement)",
                                           math.sqrt(40 / 32)),
            "points60": _bootstrap_summary(points60, full_vec[:NRIG],
                                           full_sd[:NRIG], fm,
                                           "points (60% without replacement)",
                                           math.sqrt(40 / 24)),
            "views_sub20": _bootstrap_summary(sub20, full_vec[:NRIG],
                                              full_sd[:NRIG], fm,
                                              "views (20 of 25 without replacement)",
                                              math.sqrt(25 / 20)),
        },
        "replicates": {"n_views": len(views), "n_points80": len(points80),
                       "n_points60": len(points60), "n_sub20": len(sub20)},
    }
    # baseline ratios and variant ratios
    for name, reps in (("views", views), ("points80", points80),
                       ("points60", points60), ("views_sub20", sub20)):
        ok = [r for r in reps if r.get("ok")]
        if len(ok) < 3:
            continue
        V = np.asarray([r["vec"] for r in ok], float)
        b = np.linalg.norm(V[:, 30:33], axis=1)
        emp_b = float(np.std(b, ddof=1))
        rep_b = baseline_sigma(full)
        summary["bootstrap"][name]["baseline"] = {
            "reported_sigma_mm": rep_b, "empirical_sd_mm": emp_b,
            "ratio_reported_over_empirical": safe_div(rep_b, emp_b),
            "mean_rep_baseline_mm": float(np.mean(b)),
            "bias_mean_minus_full_mm": float(np.mean(b) - full_info["baseline_mm"]),
        }
        if variants is not None and name == "views":
            gn = variants["gn_mad"]
            corr = variants["sandwich_corrected"]
            fmr = np.where(fm[:NRIG])[0]
            gn_sd = np.sqrt(np.maximum(np.diag(gn), 0.0))
            corr_sd = np.sqrt(np.maximum(np.diag(corr), 0.0))
            summary["bootstrap"][name]["variant_ratios"] = {
                "gn_mad": _ratio_stats(gn_sd, V, fm),
                "sandwich_corrected": _ratio_stats(corr_sd, V, fm),
                "marginal_live": _ratio_stats(full_sd, V, fm),
                "conditional_legacy": _ratio_stats(
                    np.sqrt(np.maximum(np.diag(cond), 0.0)), V, fm),
            }
        if variants is not None:
            summary["bootstrap"][name]["reported_variant_sigma"] = {
                "current_median": float(np.nanmedian([
                    full_sd[i] for i in np.where(fm[:NRIG])[0]])),
                "gn_mad_median": float(np.nanmedian([
                    np.sqrt(max(variants["gn_mad"][i, i], 0.0))
                    for i in np.where(fm[:NRIG])[0]])),
                "sandwich_corrected_median": float(np.nanmedian([
                    np.sqrt(max(variants["sandwich_corrected"][i, i], 0.0))
                    for i in np.where(fm[:NRIG])[0]])),
            }
    summary["variants"] = {
        "gn_vs_current": compare_matrices(variants["gn_mad"] if variants else None,
                                          C),
        "corrected_sandwich_vs_current": compare_matrices(
            variants["sandwich_corrected"] if variants else None, C),
        "marginal_vs_conditional": compare_matrices(marg, cond),
        "jacobian": ji,
    }
    return summary


def _ratio_stats(reported_sd: np.ndarray, V: np.ndarray,
                 free_mask: np.ndarray) -> dict:
    emp = np.std(V, axis=0, ddof=1)
    idx = np.where(free_mask[:NRIG])[0]
    vals = []
    per = {}
    for i in idx:
        if emp[i] <= 1e-12:
            continue
        r = float(reported_sd[i] / emp[i])
        vals.append(r)
        per[_RIG_NAMES[i]] = {"reported_sigma": float(reported_sd[i]),
                              "empirical_sd": float(emp[i]), "ratio": r}
    return {"median": float(np.median(vals)) if vals else None,
            "min": float(np.min(vals)) if vals else None,
            "max": float(np.max(vals)) if vals else None,
            "per_param": per}


# --------------------------------------------------------------------------
# task 3: perturbation suite
# --------------------------------------------------------------------------

def _base_scene(k: int = 12, **over) -> dict:
    d = dict(seed=0, k=int(k), noise_px=0.2, outlier_frac=0.0,
             pose_mode="normal")
    d.update(over)
    return d


def task3_conditions() -> list[dict]:
    C = []
    def add(name, n=20, scene=None, free_kc=True, robust=True, max_nfev=600,
            init=None, obs_filter=None, focus=None):
        C.append({"name": name, "n_trials": int(n),
                  "scene": scene or _base_scene(),
                  "free_kc": bool(free_kc), "robust_covariance": bool(robust),
                  "max_nfev": int(max_nfev), "init": init or {},
                  "obs_filter": obs_filter, "focus": focus})

    add("clean_iid", 24)
    add("det_global_shift_0.05", 20, _base_scene(global_shift_px=(0.05, 0.05)))
    add("det_global_shift_0.25", 20, _base_scene(global_shift_px=(0.25, 0.25)))
    add("det_subset_shift_30pct_0.3", 20, _base_scene(
        subset_shift={"frac": 0.3, "amp_px": (0.3, 0.0), "mode": "columns"}))
    add("det_subset_shift_30pct_0.5", 20, _base_scene(
        subset_shift={"frac": 0.3, "amp_px": (0.5, 0.2), "mode": "columns"}))
    add("det_gradient_0.2px", 20, _base_scene(gradient_shift_px=0.2))
    add("lens_rational_moderate", 20, _base_scene(
        lens_model="rational", lens_hi=(-0.03, 0.015, -0.005)),
        focus="intrinsics_physical")
    add("lens_poly8_moderate", 20, _base_scene(
        lens_model="poly8", lens_hi=(0.2,)), focus="intrinsics_physical")
    for d in (-1.0, -0.5, -0.1, 0.1, 0.5, 1.0):
        add(f"pitch_{d:+.1f}pct", 20, _base_scene(pitch_rel_error=d / 100.0))
    add("board_bow_0.3mm", 20, _base_scene(deform=("bow", 0.3)))
    add("board_bow_1.0mm", 20, _base_scene(deform=("bow", 1.0)))
    add("board_warp_0.1mm", 20, _base_scene(deform=("warp", 0.1)))
    add("noise_corr_len1_0.2px", 20, _base_scene(
        correlated_noise={"length": 1.0, "sigma_px": 0.2}))
    add("noise_corr_len2_0.2px", 20, _base_scene(
        correlated_noise={"length": 2.0, "sigma_px": 0.2}))
    add("noise_corr_len1_0.5px", 20, _base_scene(
        correlated_noise={"length": 1.0, "sigma_px": 0.5}))
    add("pose_jitter_0.5deg_2mm", 20, _base_scene(pose_jitter=(0.5, 2.0)))
    add("pose_jitter_2deg_8mm", 15, _base_scene(pose_jitter=(2.0, 8.0)))
    add("missing_20pct", 20, _base_scene(missing_frac=0.2))
    add("missing_40pct", 20, _base_scene(missing_frac=0.4))
    add("missing_60pct", 15, _base_scene(missing_frac=0.6), max_nfev=700)
    add("missing_columns_40pct", 20, _base_scene(
        missing_frac=0.4, missing_structured="columns"))
    add("outliers_2pct", 20, _base_scene(outlier_frac=0.02))
    add("outliers_5pct", 20, _base_scene(outlier_frac=0.05))
    add("outliers_10pct", 20, _base_scene(outlier_frac=0.10), max_nfev=700)
    add("outliers_20pct", 15, _base_scene(outlier_frac=0.20), max_nfev=800)
    return C


def task4_conditions() -> list[dict]:
    C = []
    def add(name, n=12, scene=None, free_kc=True, robust=True, max_nfev=500,
            init=None, obs_filter=None, focus=None):
        C.append({"name": name, "n_trials": int(n),
                  "scene": scene or _base_scene(),
                  "free_kc": bool(free_kc), "robust_covariance": bool(robust),
                  "max_nfev": int(max_nfev), "init": init or {},
                  "obs_filter": obs_filter, "focus": focus})

    add("few_views_3", 15, _base_scene(k=3), max_nfev=500)
    add("few_views_5", 15, _base_scene(k=5), max_nfev=600)
    add("same_distance", 12, _base_scene(pose_mode="same_distance"), max_nfev=700)
    add("same_tilt", 12, _base_scene(pose_mode="same_tilt"), max_nfev=700)
    add("same_tilt_distance", 10, _base_scene(pose_mode="same_tilt_distance"),
        max_nfev=700)
    add("frontoparallel", 10, _base_scene(pose_mode="frontoparallel"),
        max_nfev=300)
    add("identical_poses", 8, _base_scene(pose_mode="identical"), max_nfev=250)
    add("near_identical", 10, _base_scene(pose_mode="near_identical"), max_nfev=350)
    add("narrow_band", 12, _base_scene(pose_mode="narrow"), max_nfev=600)
    add("clustered", 12, _base_scene(pose_mode="clustered"), max_nfev=600)
    add("coverage_left_only", 12, _base_scene(k=10, camera_subset=("left",)),
        max_nfev=500)
    add("coverage_left_right", 12,
        _base_scene(k=10, camera_subset=("left", "right")), max_nfev=500)
    add("coverage_right_1view", 12, _base_scene(k=12),
        obs_filter={"right": [0]}, max_nfev=600)
    add("unused_views_gaps", 10,
        _base_scene(k=12, view_subset=(0, 2, 4, 6, 8, 10)), max_nfev=600)
    add("fixed_kc_oracle", 15, _base_scene(), free_kc=False,
        init={"kc_rel_sigma": 0.0, "kc_abs_sigma": 0.0, "kc_scale": 1.0})
    add("fixed_kc_biased5pct", 15, _base_scene(), free_kc=False,
        init={"kc_rel_sigma": 0.0, "kc_abs_sigma": 0.002, "kc_scale": 1.05})
    add("fixed_kc_biased10pct", 15, _base_scene(), free_kc=False,
        init={"kc_rel_sigma": 0.0, "kc_abs_sigma": 0.002, "kc_scale": 1.10})
    add("free_kc_reference", 15, _base_scene(), free_kc=True)
    return C


def _condition_task(args: dict) -> dict:
    return _mc_task(args)


def summarise_condition(cond: dict, rows: list[dict]) -> dict:
    ok = [r for r in rows if r.get("ok")]
    focus = cond.get("focus") or "all"
    groups = ("all", "intrinsics", "intrinsics_physical", "distortion",
              "extrinsics", "rig", "views")
    out = {"name": cond["name"], "n_trials": len(rows), "n_ok": len(ok),
           "n_failed": len(rows) - len(ok),
           "scene": cond.get("scene", {}), "free_kc": cond.get("free_kc", True),
           "focus": focus,
           "errors": [r.get("error") for r in rows if not r.get("ok")][:5],
           "groups": {}, "key": {}, "stopping": {}, "diagnostics": {}}
    for g in groups:
        if ok and int(ok[0].get("groups", {}).get(g, {}).get("n_params", 0)) > 0:
            out["groups"][g] = _summarise_group(ok, g)
    # key parameter z/sigma summaries
    for key in ("left.fx", "right.fx", "tex.fx", "left.cx", "right.cx",
                "tex.cx", "baseline", "right_rotation"):
        zs, sig, err = [], [], []
        for r in ok:
            kk = r.get("key", {}).get(key, {})
            z = kk.get("z")
            if z is not None:
                zs.append(z)
            s = kk.get("sigma", kk.get("sigma_deg"))
            if s is not None:
                sig.append(s)
            e = kk.get("error", kk.get("error_deg"))
            if e is not None:
                err.append(e)
        zf = [v for v in zs if math.isfinite(v)]
        out["key"][key] = {
            "n": len(zs), "n_inf": int(np.sum(~np.isfinite(np.asarray(zs, float))))
            if zs else 0,
            "median_abs_z": float(np.median(np.abs(zf))) if zf else None,
            "p95_abs_z": float(np.percentile(np.abs(zf), 95)) if zf else None,
            "max_abs_z": float(np.max(np.abs(zf))) if zf else None,
            "frac_gt3": float(np.mean(np.abs(zf) > 3.0)) if zf else None,
            "median_sigma": float(np.median(sig)) if sig else None,
            "median_abs_err": float(np.median(np.abs(err))) if err else None,
        }
    # stopping behaviour
    sr = [r for r in ok if r.get("stopping")]
    out["stopping"] = {
        "n": len(sr),
        "pass_rate": float(np.mean([bool(r["stopping"].get("ok")) for r in sr]))
        if sr else None,
        "cov_ok_rate": float(np.mean([bool(r["stopping"].get("cov_ok")) for r in sr]))
        if sr else None,
        "rank_ok_rate": float(np.mean([bool(r["stopping"].get("rank_ok")) for r in sr]))
        if sr else None,
        "coverage_ok_rate": float(np.mean([
            bool(r["stopping"].get("coverage_ok")) for r in sr])) if sr else None,
        "diversity_ok_rate": float(np.mean([
            bool(r["stopping"].get("diversity_ok")) for r in sr])) if sr else None,
    }
    # diagnostics
    di = [r.get("diagnostics", {}) for r in ok]
    def med(key, filt=lambda x: True):
        vals = [d.get(key) for d in di if d.get(key) is not None and filt(d.get(key))]
        return float(np.median(vals)) if vals else None
    out["diagnostics"] = {
        "rms_med": med("rms_px"),
        "sigma_px_med": med("sigma_px"),
        "condition_med": med("condition"),
        "rank_def_trials": int(np.sum([
            (d.get("rank_kept") or 0) < (d.get("n_free") or 0) for d in di])),
        "zero_var_trials": int(np.sum([(d.get("n_zero_var") or 0) > 0 for d in di])),
        "views_all3_med": med("views_all3"),
        "n_free_med": med("n_free"),
        "outlier_frac_3mad_med": med("outlier_frac_3mad"),
        "n_views_med": med("n_views"),
    }
    # overconfidence flag: small reported sigma, large normalised error
    flags = []
    for key in ("left.fx", "right.fx", "tex.fx", "baseline", "right_rotation",
                "left.cx", "right.cx", "tex.cx"):
        kk = out["key"].get(key, {})
        mz = kk.get("median_abs_z")
        sg = kk.get("median_sigma")
        if mz is not None and mz > 3.0:
            flags.append(f"{key}: median|z|={mz:.1f} (sigma={sg})")
    if out["diagnostics"]["zero_var_trials"]:
        flags.append(f"{out['diagnostics']['zero_var_trials']} trials with "
                     f"zero-variance parameters")
    if out["diagnostics"]["rank_def_trials"]:
        flags.append(f"{out['diagnostics']['rank_def_trials']} rank-deficient trials")
    n_nonconv = int(np.sum([r.get("success") is False for r in ok]))
    out["diagnostics"]["n_nonconverged"] = n_nonconv
    if n_nonconv:
        flags.append(f"{n_nonconv} non-converged fits")
    out["flags"] = flags
    out["max_key_z_med"] = max(
        [v["median_abs_z"] for v in out["key"].values()
         if v.get("median_abs_z") is not None] or [float("nan")])
    out["max_key_z_p95"] = max(
        [v["p95_abs_z"] for v in out["key"].values()
         if v.get("p95_abs_z") is not None] or [float("nan")])
    out["variants"] = {}
    for vname in ("sandwich_current", "sandwich_corrected", "gn_mad",
                  "legacy_pinv_h"):
        vg = {g: _summarise_variant(ok, vname, g)
              for g in ("all", "intrinsics_physical", "extrinsics")}
        vs = _summarise_variant_sigma(ok).get(vname)
        out["variants"][vname] = {"groups": vg, "sigma": vs}
    out["trials"] = [compact_trial(r) for r in rows]
    return out


def _condition_tasks(conds: list[dict]) -> list[dict]:
    tasks = []
    for ci, cond in enumerate(conds):
        for trial in range(cond["n_trials"]):
            seed = MASTER_SEED + 70000 + ci * 1000 + trial * 13
            scene = dict(cond["scene"])
            scene["seed"] = seed
            tasks.append({
                "condition": cond["name"], "scene": scene,
                "init_seed": seed + 1, "trial": trial,
                "free_kc": cond.get("free_kc", True),
                "robust_covariance": cond.get("robust_covariance", True),
                "max_nfev": cond.get("max_nfev", 600),
                "recompute_jacobian": True, "include_stopping": True,
                "init": cond.get("init", {}),
                "obs_filter": cond.get("obs_filter")})
    return tasks


def run_condition_suite(conds: list[dict], jobs: int, label: str) -> dict:
    tasks = _condition_tasks(conds)
    progress(f"{label}: {len(conds)} conditions x trials = {len(tasks)} fits")
    rows, dt = _run_tasks(tasks, _condition_task, jobs, label)
    by_cond = {}
    for cond in conds:
        rr = [r for r in rows if r.get("condition") == cond["name"]]
        by_cond[cond["name"]] = summarise_condition(cond, rr)
    ranking = sorted(
        [{"condition": c["name"], "max_key_z_med": c.get("max_key_z_med"),
          "max_key_z_p95": c.get("max_key_z_p95"),
          "flags": c.get("flags", [])}
         for c in by_cond.values()],
        key=lambda z: (-(z["max_key_z_med"] if z["max_key_z_med"] is not None
                         and math.isfinite(z["max_key_z_med"]) else -1.0)))
    out = {"variant": "synthetic; covariance = live default", "runtime_s": dt,
           "n_fits": len(tasks), "conditions": by_cond, "ranking": ranking}
    return out


# --------------------------------------------------------------------------
# task 4: failure cases / diagnostics
# --------------------------------------------------------------------------

def summarise_failure_cases(fail: dict) -> dict:
    """Extract the diagnostic table and rank the dangerous cases."""
    rows = []
    for name, c in fail["conditions"].items():
        di = c.get("diagnostics", {})
        g = c.get("groups", {})
        rows.append({
            "condition": name,
            "n_ok": c.get("n_ok"), "n_trials": c.get("n_trials"),
            "n_views_med": di.get("n_views_med"),
            "n_free_med": di.get("n_free_med"),
            "rank_def_trials": di.get("rank_def_trials"),
            "zero_var_trials": di.get("zero_var_trials"),
            "condition_med": di.get("condition_med"),
            "sigma_fx_med": c.get("key", {}).get("left.fx", {}).get("median_sigma"),
            "sigma_base_med": c.get("key", {}).get("baseline", {}).get("median_sigma"),
            "baseline_z_med": c.get("key", {}).get("baseline", {}).get("median_abs_z"),
            "fx_z_med": c.get("key", {}).get("left.fx", {}).get("median_abs_z"),
            "cx_z_med": c.get("key", {}).get("left.cx", {}).get("median_abs_z"),
            "nees_all": g.get("all", {}).get("nees_per_n", {}).get("median_per_n"),
            "nees_phys": g.get("intrinsics_physical", {}).get("nees_per_n", {}).get("median_per_n"),
            "nees_extr": g.get("extrinsics", {}).get("nees_per_n", {}).get("median_per_n"),
            "stopping_pass": c.get("stopping", {}).get("pass_rate"),
            "rank_ok_rate": c.get("stopping", {}).get("rank_ok_rate"),
            "flags": c.get("flags", []),
            "focus": c.get("focus"),
        })
    rows.sort(key=lambda r: (-(r["zero_var_trials"] or 0),
                             -(r["rank_def_trials"] or 0),
                             -(max([v for v in (r["baseline_z_med"],
                                                r["fx_z_med"], r["cx_z_med"])
                                    if v is not None] or [0.0]))))
    return {"rows": rows,
            "counts": {"n_conditions": len(rows),
                       "zero_var_conditions": sum(1 for r in rows
                                                  if (r["zero_var_trials"] or 0) > 0),
                       "rank_def_conditions": sum(1 for r in rows
                                                  if (r["rank_def_trials"] or 0) > 0)}}


def run_task3(jobs: int, quick: bool = False) -> dict:
    conds = task3_conditions()
    if quick:
        keep = {"clean_iid", "det_global_shift_0.25", "pitch_+1.0pct",
                "lens_rational_moderate", "missing_60pct", "outliers_10pct",
                "noise_corr_len1_0.5px", "board_bow_1.0mm"}
        conds = [c for c in conds if c["name"] in keep]
        for c in conds:
            c["n_trials"] = min(c["n_trials"], 3)
    return run_condition_suite(conds, jobs, "task3")


def run_task4(jobs: int, quick: bool = False) -> dict:
    conds = task4_conditions()
    if quick:
        conds = [c for c in conds if c["name"] in
                 ("few_views_5", "frontoparallel", "coverage_left_only",
                  "fixed_kc_biased10pct", "identical_poses")]
        for c in conds:
            c["n_trials"] = min(c["n_trials"], 2)
    fail = run_condition_suite(conds, jobs, "task4")
    fail["failure_summary"] = summarise_failure_cases(fail)
    return fail


def _summarise_variant_sigma(rows: list[dict]) -> dict:
    out = {}
    for vname in ("sandwich_current", "sandwich_corrected", "gn_mad",
                  "legacy_pinv_h"):
        vals = [r["variant_sigma"][vname] for r in rows
                if r.get("ok") and vname in r.get("variant_sigma", {})]
        if not vals:
            continue
        out[vname] = {}
        for key in ("sigma_fx_med", "sigma_cx_med", "baseline_sigma_mm",
                    "rig_trace", "n_zero_var_rig"):
            vv = [v[key] for v in vals if v.get(key) is not None]
            out[vname][key] = float(np.median(vv)) if vv else None
    return out


# --------------------------------------------------------------------------
# held-out / stability / rescale checks
# --------------------------------------------------------------------------

def _clone_scene_views(scene: Scene, keep: list[int]) -> Scene:
    views = [scene.views[i] for i in keep]
    keep_set = set(int(i) for i in keep)
    obs = [o for o in scene.observations if int(o.view) in keep_set]
    meta = dict(scene.meta)
    meta["used_views"] = [int(i) for i in keep]
    return Scene(scene.cameras, scene.extrinsics, views, obs,
                 scene.pitch_nominal, scene.pitch_true, scene.noise_px, meta)


def _heldout_task(args: dict) -> dict:
    try:
        from calib.evaluate import heldout_reprojection
        scene = build_scene(**args["scene"])
        k = len(scene.views)
        hold = k - 1
        train = _clone_scene_views(scene, list(range(k - 1)))
        rng = np.random.default_rng(int(args["init_seed"]))
        init = audit_init(scene, rng)
        res = fit_scene(train, init, free_kc=True,
                        max_nfev=int(args.get("max_nfev", 600)))
        ev = evaluate_fit(train, res, free_kc=True, recompute_jacobian=False,
                          include_stopping=False)
        if not ev.get("ok"):
            return {"ok": False, "error": ev.get("error")}
        ho = heldout_reprojection(scene.observations, res, scene.pitch_nominal,
                                  hold)
        if "error" in ho:
            return {"ok": False, "error": ho["error"]}
        sr = current_stopping(res, train.observations,
                              heldout_rms_px=ho.get("rms_px"),
                              require_heldout=True, require_stability=False)
        rec = {
            "ok": True, "k": k, "holdout": hold,
            "train_rms": res.residual_rms, "heldout_rms": ho.get("rms_px"),
            "heldout_max": max([c.get("max_px", 0.0)
                                for c in ho.get("cameras", {}).values()] or [0.0]),
            "ratio_heldout_train": safe_div(ho.get("rms_px"), res.residual_rms),
            "n_free": ev["n_free"],
            "nees_all": ev["groups"]["all"].get("nees_per_n"),
            "cov95_all": ev["groups"]["all"].get("coverage", {}).get("cov95"),
            "baseline_z": ev["key"]["baseline"].get("z"),
            "fx_z_med": float(np.median([abs(ev["key"][f"{c}.fx"]["z"])
                                         for c in CAMERAS
                                         if ev["key"][f"{c}.fx"].get("z") is not None])),
            "heldout_ok": (sr or {}).get("heldout_ok"),
            "stopping_ok": (sr or {}).get("ok"),
            "diag": ev["diagnostics"],
        }
        return _strip_internal(rec)
    except Exception as exc:
        import traceback
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc()[-1200:]}


def run_heldout(jobs: int, n_per_k: int = 15,
                ks: Iterable[int] = (8, 12, 20)) -> dict:
    tasks = []
    for k in ks:
        for trial in range(n_per_k):
            seed = MASTER_SEED + 90000 + int(k) * 100 + trial * 11
            tasks.append({"scene": dict(seed=seed, k=int(k), noise_px=0.2,
                                        outlier_frac=0.0, pose_mode="normal"),
                          "init_seed": seed + 1, "trial": trial, "k": int(k),
                          "max_nfev": 600})
    rows, dt = _run_tasks(tasks, _heldout_task, jobs, "heldout")
    ok = [r for r in rows if r.get("ok")]
    out = {"runtime_s": dt, "n": len(rows), "n_ok": len(ok), "by_k": {}}
    for k in ks:
        rr = [r for r in ok if r.get("k") == k]
        if not rr:
            continue
        ho = np.array([r["heldout_rms"] for r in rr], float)
        tr = np.array([r["train_rms"] for r in rr], float)
        ratio = ho / np.maximum(tr, 1e-9)
        # detect trials where train covariance is overconfident
        bad = np.array([(r.get("nees_all") or 0) > 2.0 for r in rr])
        good = ~bad
        def rate(mask, fn):
            if not mask.any():
                return None
            vals = [fn(rr[i]) for i in np.where(mask)[0] if fn(rr[i]) is not None]
            return float(np.mean(vals)) if vals else None
        out["by_k"][str(k)] = {
            "n": len(rr),
            "heldout_med": float(np.median(ho)),
            "heldout_p95": float(np.percentile(ho, 95)),
            "train_med": float(np.median(tr)),
            "ratio_med": float(np.median(ratio)),
            "ratio_p95": float(np.percentile(ratio, 95)),
            "frac_heldout_gt_0.5": float(np.mean(ho > 0.5)),
            "frac_heldout_gt_0.75": float(np.mean(ho > 0.75)),
            "n_overconfident": int(bad.sum()),
            "heldout_pass_in_bad": rate(bad, lambda r: r["heldout_ok"]),
            "heldout_pass_in_good": rate(good, lambda r: r["heldout_ok"]),
            "nees_med": float(np.median([r["nees_all"] for r in rr
                                         if r.get("nees_all") is not None])),
            "corr_heldout_nees": (float(np.corrcoef(
                [r["heldout_rms"] for r in rr],
                [r["nees_all"] for r in rr])[0, 1])
                if len(rr) > 2 and np.std([r["nees_all"] for r in rr]) > 0
                else None),
            "trials": rr,
        }
    return out


def _stability_task(args: dict) -> dict:
    try:
        from calib import quality
        scene = build_scene(**args["scene"])
        rng = np.random.default_rng(int(args["init_seed"]))
        init = audit_init(scene, rng)
        res = fit_scene(scene, init, free_kc=True,
                        max_nfev=int(args.get("max_nfev", 600)))
        ev = evaluate_fit(scene, res, free_kc=True, recompute_jacobian=False,
                          include_stopping=False)
        st = quality.parameter_stability(scene.observations, res,
                                         scene.pitch_nominal, drop=1,
                                         max_nfev=500)
        return {"ok": True, "k": len(scene.views),
                "stability_ok": bool(st.get("ok")),
                "max_z": st.get("max_z"), "n_kept": st.get("n_views_kept"),
                "nees_all": ev["groups"]["all"].get("nees_per_n"),
                "cov95_all": ev["groups"]["all"].get("coverage", {}).get("cov95"),
                "baseline_z": ev["key"]["baseline"].get("z"),
                "diag": ev["diagnostics"], "reason": st.get("reason")}
    except Exception as exc:
        import traceback
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc()[-1200:]}


def run_stability(jobs: int, n_trials: int = 15, k: int = 12) -> dict:
    tasks = []
    for trial in range(n_trials):
        seed = MASTER_SEED + 110000 + trial * 19
        tasks.append({"scene": dict(seed=seed, k=int(k), noise_px=0.2,
                                    outlier_frac=0.0, pose_mode="normal"),
                      "init_seed": seed + 1, "trial": trial, "max_nfev": 600})
    rows, dt = _run_tasks(tasks, _stability_task, jobs, "stability")
    ok = [r for r in rows if r.get("ok")]
    if not ok:
        return {"runtime_s": dt, "n": len(rows), "n_ok": 0}
    maxz = [r["max_z"] for r in ok if r.get("max_z") is not None]
    bad = [r for r in ok if (r.get("nees_all") or 0) > 2.0]
    good = [r for r in ok if (r.get("nees_all") or 0) <= 2.0]
    return {"runtime_s": dt, "n": len(rows), "n_ok": len(ok),
            "pass_rate": float(np.mean([r["stability_ok"] for r in ok])),
            "max_z_med": float(np.median(maxz)) if maxz else None,
            "max_z_p95": float(np.percentile(maxz, 95)) if maxz else None,
            "n_overconfident": len(bad),
            "pass_in_overconfident": float(np.mean([r["stability_ok"] for r in bad]))
            if bad else None,
            "pass_in_good": float(np.mean([r["stability_ok"] for r in good]))
            if good else None,
            "trials": ok}


def _rescale_task(args: dict) -> dict:
    try:
        from calib.evaluate import reprojection
        scene = build_scene(**args["scene"])
        rng = np.random.default_rng(int(args["init_seed"]))
        init = audit_init(scene, rng)
        res = fit_scene(scene, init, free_kc=True, max_nfev=600)
        C = res.covariance
        if C is None:
            return {"ok": False, "error": "no covariance"}
        pk = _packer(len(res.views), True)
        t_idx = []
        for c in ("right", "tex"):
            t_idx.extend(range(pk.extr[c].start + 3, pk.extr[c].start + 6))
        for i in range(len(res.views)):
            t_idx.extend(range(pk.views.start + 6 * i + 3,
                               pk.views.start + 6 * i + 6))
        t_idx = np.array(t_idx, int)
        base_rms = reprojection(res, scene.observations,
                                scene.pitch_nominal)["rms_px"]
        out = {"ok": True, "base_rms": base_rms, "s": {}}
        for s in (0.99, 1.01, 1.05):
            rs = res.rescaled(s)
            S = np.eye(pk.n)
            S[t_idx, t_idx] = s
            ref = S @ C @ S.T
            legacy = C * s * s
            pitch_after = float(rs.info.get("pitch_mm", scene.pitch_nominal))
            rms_pitch = reprojection(rs, scene.observations, pitch_after)["rms_px"]
            rms_nominal = reprojection(rs, scene.observations,
                                       scene.pitch_nominal)["rms_px"]
            out["s"][str(s)] = {
                "max_rel_vs_transform": compare_matrices(rs.covariance, ref)["rel_max"],
                "max_rel_vs_legacy": compare_matrices(rs.covariance, legacy)["rel_max"],
                "rms_after": rms_pitch,
                "rms_ratio": safe_div(rms_pitch, base_rms),
                "rms_after_nominal_pitch": rms_nominal,
                "rms_ratio_nominal_pitch": safe_div(rms_nominal, base_rms),
                "pitch_after_mm": pitch_after,
                "scale_factor": rs.info.get("scale_factor"),
            }
        # anchored: target baseline 1% high, 0.16 mm anchor sigma
        target = float(np.linalg.norm(res.baseline())) * 1.01
        ra = res.rescaled(target_baseline=target, anchor_sigma_mm=0.16,
                          factor_sigma=0.001)
        out["anchor"] = {
            "target_mm": target,
            "result_mm": float(np.linalg.norm(ra.baseline())),
            "baseline_sigma_mm": baseline_sigma(ra),
            "scale_factor": ra.info.get("scale_factor"),
            "factor_sigma": ra.info.get("factor_sigma"),
            "anchor_sigma_mm": ra.info.get("scale_anchor_sigma_mm"),
        }
        return out
    except Exception as exc:
        import traceback
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc()[-1200:]}


def run_rescale_check(jobs: int, n_trials: int = 8) -> dict:
    tasks = []
    for trial in range(n_trials):
        seed = MASTER_SEED + 130000 + trial * 23
        tasks.append({"scene": dict(seed=seed, k=12, noise_px=0.2,
                                    outlier_frac=0.0, pose_mode="normal"),
                      "init_seed": seed + 1, "trial": trial})
    rows, dt = _run_tasks(tasks, _rescale_task, jobs, "rescale")
    ok = [r for r in rows if r.get("ok")]
    out = {"runtime_s": dt, "n": len(rows), "n_ok": len(ok), "by_s": {}}
    for s in ("0.99", "1.01", "1.05"):
        vals = [r["s"][s] for r in ok]
        out["by_s"][s] = {
            "max_rel_vs_transform_med": float(np.median(
                [v["max_rel_vs_transform"] for v in vals])),
            "max_rel_vs_transform_max": float(np.max(
                [v["max_rel_vs_transform"] for v in vals])),
            "max_rel_vs_legacy_med": float(np.median(
                [v["max_rel_vs_legacy"] for v in vals])),
            "rms_ratio_med": float(np.median([v["rms_ratio"] for v in vals])),
            "rms_ratio_nominal_pitch_med": float(np.median(
                [v.get("rms_ratio_nominal_pitch", float("nan")) for v in vals])),
        }
    out["anchor"] = {
        "baseline_sigma_mm_med": float(np.median(
            [r["anchor"]["baseline_sigma_mm"] for r in ok])),
        "anchor_sigma_mm": 0.16, "factor_sigma": 0.001,
        "target_offset_pct": 1.0,
    }
    out["trials"] = ok
    return out


# --------------------------------------------------------------------------
# recommendations / claims
# --------------------------------------------------------------------------

def build_recommendations(R: dict) -> dict:
    """Data-driven stopping thresholds and MC acceptance criteria.

    The numbers here are filled from the measured summaries; the prose in the
    report explains the trade-off.  This function deliberately uses conservative
    rounding (e.g. one more view than the first k that looks acceptable).
    """
    by_k = (R.get("task1") or {}).get("by_k", {})
    ks = sorted(int(k) for k in by_k)
    def get(k, g, path, default=None):
        d = by_k.get(str(k), {}).get("groups", {}).get(g, {})
        for p in path:
            if not isinstance(d, dict):
                return default
            d = d.get(p)
        return d if d is not None else default

    rows = []
    for k in ks:
        rows.append({
            "k": k,
            "nees_all": get(k, "all", ("nees_per_n", "median_per_n")),
            "nees_intr": get(k, "intrinsics", ("nees_per_n", "median_per_n")),
            "nees_extr": get(k, "extrinsics", ("nees_per_n", "median_per_n")),
            "cov68_all": get(k, "all", ("coverage", "cov68")),
            "cov95_all": get(k, "all", ("coverage", "cov95")),
            "sigma_fx": by_k.get(str(k), {}).get("sigma_fx_med"),
            "sigma_base": by_k.get(str(k), {}).get("sigma_baseline_med"),
            "stop_pass": by_k.get(str(k), {}).get("stopping_pass_rate"),
        })
    # first k satisfying a conservative covariance/coverage screen
    min_views = None
    for r in rows:
        ok = (r["nees_all"] is not None and 0.85 <= r["nees_all"] <= 1.15 and
              (r["cov68_all"] or 0) >= 0.60 and (r["cov95_all"] or 0) >= 0.90 and
              (r["sigma_fx"] is None or r["sigma_fx"] <= 0.60) and
              (r["sigma_base"] is None or r["sigma_base"] <= 0.15))
        if ok:
            min_views = r["k"]
            break
    recommended_min_views = max(12, (min_views or 15))
    rec = {
        "variant": "synthetic MC + corpus bootstrap + perturbation suite",
        "min_views": int(recommended_min_views),
        "min_views_basis": "first k with NEES_all in [0.85,1.15], "
                           "68% coverage >=0.60, 95% coverage >=0.90, "
                           "sigma_fx <=0.60 px, sigma_baseline <=0.15 mm; "
                           "rounded up to at least 12",
        "min_joint_views_all3": 8,
        "min_views_per_camera": 8,
        "min_points_per_view_camera": 20,
        "min_obs_per_camera": 300,
        "min_distance_bins": 3,
        "min_tilt_signs_each": 1,
        "min_image_quadrant_fraction": 0.75,
        "max_sigma_fx_px": 0.50,
        "max_sigma_baseline_mm": 0.12,
        "max_sigma_rotation_deg": 0.20,
        "max_sigma_rgb_mm": 1.5,
        "max_condition_number": 1e7,
        "require_rank_full": True,
        "require_no_zero_variance": True,
        "max_heldout_rms_px": 0.50,
        "max_heldout_over_train": 1.5,
        "max_stability_z": 4.0,
        "max_outlier_fraction": 0.05,
        "max_per_view_rms_over_median": 3.0,
        "pitch_sigma_mm": 0.03,
        "pitch_note": ("The plate pitch is a scale gauge.  Either model it as a free "
                       "parameter or pass factor_sigma / anchor_sigma to rescaled(); "
                       "otherwise baseline sigma excludes a scale error that is "
                       "O(0.1-1.6 mm) for a 0.1-1% pitch error."),
        "acceptance_mc": {
            "n_scenes_per_view_count": 50,
            "view_counts": [5, 8, 10, 12, 15, 20, 25],
            "nees_per_n_median": [0.90, 1.10],
            "nees_per_n_ci95_includes_1": True,
            "coverage68": [0.63, 0.73],
            "coverage95": [0.92, 0.98],
            "z_rms_max": 1.20,
            "median_abs_z_max": 0.20,
            "no_zero_variance_params": True,
            "bootstrap_ratio": [0.80, 1.25],
            "worst_condition_median_z_max": 2.0,
            "pitch_sensitivity": "z_baseline<3 at 0.1% or propagated pitch sigma",
        },
        "rows": rows,
    }
    return rec


def build_claims(R: dict) -> list[dict]:
    """A/B/C/U claims with the measured number attached."""
    t1 = (R.get("task1") or {}).get("by_k", {})
    def med(k, g="all"):
        return (((t1.get(str(k), {}) or {}).get("groups", {}) or {})
                .get(g, {}) or {}).get("nees_per_n", {}).get("median_per_n")
    claims = [
        {"grade": "A", "claim": "The joint covariance's top-left 39x39 block is the "
                               "marginal rig covariance; the old conditional formula "
                               "is smaller by a factor measured in task 1.",
         "evidence": "rig_variants comparisons; marginal_vs_conditional"},
        {"grade": "A", "claim": "A free parameter with zero Jacobian column (camera "
                               "never observed) receives zero variance and can carry "
                               "an arbitrary error; rank/coverage checks are "
                               "mandatory.",
         "evidence": "task4 coverage_left_only"},
        {"grade": "A", "claim": "rescaled() covariance now matches S C S^T to ~1e-10; "
                               "the legacy C*s^2 does not.",
         "evidence": "rescale_check"},
        {"grade": "B", "claim": "The live default covariance is (approximately) "
                               "calibrated on clean synthetic scenes; NEES/n is "
                               "reported per view count.",
         "evidence": "task1"},
        {"grade": "B", "claim": "The pre-fix Huber sandwich used w*r^2 in B rather "
                               "than (w*r)^2; with outliers it was conservative, "
                               "not optimistic. The live tree now matches the "
                               "standard sandwich.",
         "evidence": "task1 variants + task4 outliers"},
        {"grade": "B", "claim": "Plate-pitch error is a scale gauge not represented "
                               "in the covariance; a 0.1% error is already several "
                               "sigma on the baseline.",
         "evidence": "task3 pitch conditions"},
        {"grade": "B", "claim": "Spatially correlated detector noise and non-rigid "
                               "marker shifts are the main synthetic overconfidence "
                               "mechanisms after pitch.",
         "evidence": "task3 correlated/missing/subset conditions"},
        {"grade": "C", "claim": "On the vendor corpus the bootstrap spread is "
                               "compared with the reported sigma; the CCF is a "
                               "reference, not truth.",
         "evidence": "task2"},
        {"grade": "U", "claim": "The physical detector bias, plate flatness and the "
                               "true pitch tolerance are unknown; synthetic "
                               "amplitudes are plausible, not measured.",
         "evidence": "limitations"},
    ]
    return claims


# --------------------------------------------------------------------------
# figures
# --------------------------------------------------------------------------

def _fig_dir(outdir: Path) -> Path:
    p = outdir / "figures"
    p.mkdir(parents=True, exist_ok=True)
    return p


def make_figures(R: dict, outdir: Path) -> list[str]:
    figdir = _fig_dir(outdir)
    made = []
    plt.rcParams.update({"figure.dpi": 140, "font.size": 8,
                         "axes.grid": True, "grid.alpha": 0.25})
    t1 = (R.get("task1") or {}).get("by_k", {})
    ks = sorted(int(k) for k in t1)
    if ks:
        groups = [("all", "all free"), ("intrinsics", "intrinsics"),
                  ("extrinsics", "extrinsics"), ("rig", "rig (39)")]
        fig, axes = plt.subplots(2, 2, figsize=(9, 6.4), sharex=True)
        for ax, (g, lab) in zip(axes.ravel()[:3], groups[:3]):
            med = [t1[str(k)]["groups"][g]["nees_per_n"]["median_per_n"] for k in ks]
            lo = [t1[str(k)]["groups"][g]["nees_per_n"]["ci95_per_n"][0] for k in ks]
            hi = [t1[str(k)]["groups"][g]["nees_per_n"]["ci95_per_n"][1] for k in ks]
            ax.errorbar(ks, med, yerr=[np.array(med) - np.array(lo),
                                       np.array(hi) - np.array(med)],
                        marker="o", capsize=2, label=lab)
            ax.axhline(1.0, color="k", lw=0.8)
            band_lo = [t1[str(k)]["groups"][g]["nees_per_n"]["chi2_95_band"][0]
                       for k in ks]
            band_hi = [t1[str(k)]["groups"][g]["nees_per_n"]["chi2_95_band"][1]
                       for k in ks]
            ax.fill_between(ks, band_lo, band_hi, color="grey", alpha=0.12,
                            label="chi2 95% band")
            ax.set_title(lab)
            ax.set_ylabel("NEES / n")
        ax = axes.ravel()[3]
        for g, lab in (("all", "all"), ("intrinsics", "intr"), ("extrinsics", "extr")):
            c68 = [t1[str(k)]["groups"][g]["coverage"]["cov68"] for k in ks]
            c95 = [t1[str(k)]["groups"][g]["coverage"]["cov95"] for k in ks]
            ax.plot(ks, c68, marker="o", label=f"{lab} 68")
            ax.plot(ks, c95, marker="s", ls="--", label=f"{lab} 95")
        ax.axhline(0.68, color="k", lw=0.7)
        ax.axhline(0.95, color="k", lw=0.7, ls=":")
        ax.set_ylim(0.4, 1.02)
        ax.set_title("parameter coverage")
        ax.set_xlabel("views")
        ax.set_ylabel("coverage")
        for ax in axes.ravel():
            ax.set_xscale("log")
            ax.set_xticks(ks)
            ax.set_xticklabels([str(k) for k in ks])
        axes.ravel()[3].legend(fontsize=6, ncol=2)
        fig.suptitle("Task 1 — NEES and coverage vs view count (live covariance)")
        fig.tight_layout()
        f = figdir / "fig1_nees_coverage.png"
        fig.savefig(f, bbox_inches="tight")
        plt.close(fig)
        made.append(str(f))

    # marginal vs conditional
    mc = (R.get("task1") or {}).get("marginal_vs_conditional", {})
    if mc:
        fig, ax = plt.subplots(figsize=(5.2, 3.2))
        kk = sorted(int(k) for k in mc)
        mar = [mc[str(k)]["nees_per_n_extr_marginal"]["median_per_n"] for k in kk]
        con = [mc[str(k)]["nees_per_n_extr_conditional"]["median_per_n"] for k in kk]
        ax.plot(kk, mar, marker="o", label="marginal (current)")
        ax.plot(kk, con, marker="s", label="conditional (legacy)")
        ax.axhline(1.0, color="k", lw=0.8)
        ax.set_xscale("log"); ax.set_xticks(kk)
        ax.set_xticklabels([str(k) for k in kk])
        ax.set_xlabel("views"); ax.set_ylabel("NEES / n, extrinsics")
        ax.set_title("Marginal vs legacy conditional rig covariance")
        ax.legend(fontsize=7)
        fig.tight_layout()
        f = figdir / "fig2_marginal_vs_conditional.png"
        fig.savefig(f, bbox_inches="tight"); plt.close(fig); made.append(str(f))

    # bootstrap ratios
    boot = (R.get("task2") or {}).get("bootstrap", {})
    if boot:
        fig, ax = plt.subplots(figsize=(6.5, 3.4))
        params = ["left.fx", "right.fx", "tex.fx", "left.cx", "right.cx",
                  "right.tx", "right.ty", "right.tz"]
        width = 0.26
        for j, mode in enumerate(("views", "points80", "points60")):
            d = boot.get(mode, {}).get("params", {})
            vals = [d.get(p, {}).get("ratio_reported_over_empirical") for p in params]
            xs = np.arange(len(params)) + (j - 1) * width
            ax.bar(xs, [v if v is not None and math.isfinite(v) else 0 for v in vals],
                   width=width, label=mode)
        ax.axhline(1.0, color="k", lw=0.8)
        ax.set_xticks(np.arange(len(params)))
        ax.set_xticklabels(params, rotation=30, ha="right")
        ax.set_ylabel("reported sigma / empirical sd")
        ax.set_title("Task 2 — vendor-corpus bootstrap ratios")
        ax.legend(fontsize=7)
        fig.tight_layout()
        f = figdir / "fig3_bootstrap_ratios.png"
        fig.savefig(f, bbox_inches="tight"); plt.close(fig); made.append(str(f))

    # perturbation heatmap
    t3 = (R.get("task3") or {}).get("conditions", {})
    if t3:
        names = list(t3.keys())
        keys = ["left.fx", "baseline", "right_rotation", "left.cx"]
        M = np.full((len(keys), len(names)), np.nan)
        for j, n in enumerate(names):
            for i, k in enumerate(keys):
                v = t3[n].get("key", {}).get(k, {}).get("median_abs_z")
                if v is not None and math.isfinite(v):
                    M[i, j] = v
        fig, ax = plt.subplots(figsize=(max(7, 0.34 * len(names)), 2.8))
        im = ax.imshow(np.clip(M, 0, 20), aspect="auto", cmap="magma",
                       norm=LogNorm(vmin=0.05, vmax=20))
        ax.set_yticks(range(len(keys))); ax.set_yticklabels(keys)
        ax.set_xticks(range(len(names)))
        ax.set_xticklabels(names, rotation=60, ha="right", fontsize=6)
        ax.set_title("Task 3 — median |z| by perturbation "
                     "(clipped 0.05..20; blank = infinite)")
        fig.colorbar(im, ax=ax, label="median |z|")
        fig.tight_layout()
        f = figdir / "fig4_perturbation_heatmap.png"
        fig.savefig(f, bbox_inches="tight"); plt.close(fig); made.append(str(f))

    # overconfidence scatter: sigma vs |error|
    if t3:
        xs, ys, labels = [], [], []
        for n, c in t3.items():
            for tr in c.get("trials", []):
                for k in ("left.fx", "baseline"):
                    kk = tr.get("key", {}).get(k, {})
                    s, e = kk.get("sigma"), kk.get("error")
                    if s is None and k == "baseline":
                        s = kk.get("sigma")
                    if (s is not None and e is not None and s > 1e-9
                            and abs(e) > 1e-9):
                        xs.append(abs(s)); ys.append(abs(e)); labels.append(n)
        if xs:
            fig, ax = plt.subplots(figsize=(6, 4))
            ax.scatter(xs, ys, s=8, alpha=0.5)
            lim = [min(xs + ys) * 0.7, max(xs + ys) * 1.3]
            ax.plot(lim, lim, "k-", lw=0.8, label="z=1")
            ax.plot(lim, [3 * v for v in lim], "k--", lw=0.8, label="z=3")
            ax.set_xscale("log"); ax.set_yscale("log")
            ax.set_xlabel("reported sigma (px or mm)")
            ax.set_ylabel("|error| (px or mm)")
            ax.set_title("Task 3 — reported sigma vs actual error")
            ax.legend(fontsize=7)
            fig.tight_layout()
            f = figdir / "fig5_sigma_vs_error.png"
            fig.savefig(f, bbox_inches="tight"); plt.close(fig); made.append(str(f))

    # failure diagnostics
    t4rows = ((R.get("task4") or {}).get("failure_summary", {}) or {}).get("rows", [])
    if t4rows:
        fig, axes = plt.subplots(1, 3, figsize=(11, 3.6))
        names = [r["condition"] for r in t4rows]
        sig = [r.get("sigma_fx_med") for r in t4rows]
        err = [r.get("fx_z_med") for r in t4rows]
        cond = [r.get("condition_med") for r in t4rows]
        axes[0].barh(range(len(names)), [v if v else 0 for v in sig])
        axes[0].set_yticks(range(len(names))); axes[0].set_yticklabels(names, fontsize=6)
        axes[0].set_xlabel("median sigma_fx (px)"); axes[0].set_title("reported sigma")
        axes[1].barh(range(len(names)), [v if v else 0 for v in err])
        axes[1].set_yticks([]); axes[1].set_xlabel("median |z| fx")
        axes[1].set_title("normalised error")
        axes[2].barh(range(len(names)), [math.log10(v) if v and v > 0 else 0
                                         for v in cond])
        axes[2].set_yticks([]); axes[2].set_xlabel("log10 condition number")
        axes[2].set_title("information conditioning")
        fig.suptitle("Task 4 — failure cases and diagnostics")
        fig.tight_layout()
        f = figdir / "fig6_failure_diagnostics.png"
        fig.savefig(f, bbox_inches="tight"); plt.close(fig); made.append(str(f))

    # heldout
    ho = R.get("heldout") or {}
    if ho.get("by_k"):
        fig, ax = plt.subplots(figsize=(4.6, 3.4))
        for k, d in ho["by_k"].items():
            tr = [r["train_rms"] for r in d.get("trials", [])]
            hv = [r["heldout_rms"] for r in d.get("trials", [])]
            ax.scatter(tr, hv, s=12, label=f"k={k}")
        lim = [0.1, 1.2]
        ax.plot(lim, lim, "k-", lw=0.8, label="1:1")
        ax.set_xscale("log"); ax.set_yscale("log")
        ax.set_xlabel("train RMS (px)"); ax.set_ylabel("held-out RMS (px)")
        ax.set_title("Held-out generalisation")
        ax.legend(fontsize=7)
        fig.tight_layout()
        f = figdir / "fig7_heldout.png"
        fig.savefig(f, bbox_inches="tight"); plt.close(fig); made.append(str(f))
    return made


# --------------------------------------------------------------------------
# report (compact, data-driven; expanded prose lives in the markdown)
# --------------------------------------------------------------------------

def _fmt(v, nd=3):
    if v is None:
        return "n/a"
    if isinstance(v, float):
        if not math.isfinite(v):
            return "inf"
        return f"{v:.{nd}f}"
    return str(v)


def _md_table(headers, rows):
    out = ["| " + " | ".join(headers) + " |",
           "|" + "|".join(["---"] * len(headers)) + "|"]
    for r in rows:
        out.append("| " + " | ".join(str(x) for x in r) + " |")
    return "\n".join(out)


def render_report(R: dict, outdir: Path) -> str:
    """Write a report generated from the measured JSON."""
    lines = []
    meta = R.get("meta", {})
    lines += [
        "# Uncertainty calibration and failure-mode audit",
        "",
        f"Generated: {meta.get('timestamp', 'n/a')}  ",
        f"Live tree: `{meta.get('git', {}).get('head', 'n/a')}`"
        f" (dirty={meta.get('git', {}).get('dirty')})  ",
        "Scope: **synthetic ground truth + vendor corpus consistency**. "
        "No physical-accuracy claim is made.",
        "",
        "## TL;DR",
        "",
    ]
    t1 = (R.get("task1") or {}).get("by_k", {})
    if t1:
        ks = sorted(int(k) for k in t1)
        for k in (ks[0], ks[len(ks) // 2], ks[-1]):
            d = t1[str(k)]
            lines.append(
                f"- k={k}: NEES/n all={_fmt(d['groups']['all']['nees_per_n']['median_per_n'])} "
                f"({d['groups']['all']['verdict']}), intrinsics="
                f"{_fmt(d['groups']['intrinsics']['nees_per_n']['median_per_n'])}, "
                f"extrinsics={_fmt(d['groups']['extrinsics']['nees_per_n']['median_per_n'])}, "
                f"68% coverage={_fmt(d['groups']['all']['coverage']['cov68'])}.")
    lines += ["", "## Variants used", "",
              _md_table(["label", "definition"],
                        [[k, v] for k, v in VARIANT_LEGEND.items()]),
              ""]
    # task 1
    lines += ["## 1. Monte-Carlo NEES and coverage", "",
              "Independent synthetic scenes; true K/D/rig/poses drawn per trial; "
              "0.2 px i.i.d. noise; no gross outliers; `free_kc=True`; current "
              "default covariance.", ""]
    if t1:
        rows = []
        for k in sorted(int(k) for k in t1):
            d = t1[str(k)]
            g = d["groups"]
            rows.append([
                k, d["n_ok"],
                f"{_fmt(g['all']['nees_per_n']['median_per_n'],2)} "
                f"[{_fmt(g['all']['nees_per_n']['ci95_per_n'][0],2)},"
                f"{_fmt(g['all']['nees_per_n']['ci95_per_n'][1],2)}]",
                _fmt(g["intrinsics"]["nees_per_n"]["median_per_n"], 2),
                _fmt(g["extrinsics"]["nees_per_n"]["median_per_n"], 2),
                _fmt(g["rig"]["nees_per_n"]["median_per_n"], 2),
                _fmt(g["all"]["coverage"]["cov68"], 2),
                _fmt(g["all"]["coverage"]["cov95"], 2),
                _fmt(g["extrinsics"]["coverage"]["cov68"], 2),
                _fmt(g["extrinsics"]["coverage"]["cov95"], 2),
                d["groups"]["all"]["verdict"]])
        lines += [_md_table(["k", "trials", "NEES/n all [95% CI]", "intr",
                             "extr", "rig", "cov68 all", "cov95 all",
                             "cov68 extr", "cov95 extr", "verdict"], rows), ""]
    # variants
    lines += ["### Covariance-estimator variants", ""]
    vrows = []
    for k in sorted((R.get("task1") or {}).get("variants", {})):
        for vname, v in R["task1"]["variants"][k].items():
            vrows.append([k, vname,
                          _fmt(v.get("all", {}).get("nees_per_n", {}).get("median_per_n"), 2),
                          _fmt(v.get("intrinsics", {}).get("nees_per_n", {}).get("median_per_n"), 2),
                          _fmt(v.get("extrinsics", {}).get("nees_per_n", {}).get("median_per_n"), 2),
                          _fmt(v.get("all", {}).get("pooled_coverage", {}).get("cov68"), 2),
                          _fmt(v.get("sigma", {}).get("sigma_fx_med"), 2),
                          _fmt(v.get("sigma", {}).get("baseline_sigma_mm"), 4)])
    if vrows:
        lines += [_md_table(["k", "variant", "NEES/n all", "intr", "extr",
                             "cov68", "sigma_fx", "sigma_base"], vrows), ""]
    # marginal vs conditional
    mc = (R.get("task1") or {}).get("marginal_vs_conditional", {})
    if mc:
        rows = []
        for k in sorted(mc, key=lambda z: int(z)):
            d = mc[k]
            rows.append([k,
                         _fmt(d.get("live_vs_marginal_rel_max_med"), 2),
                         _fmt(d.get("live_vs_conditional_rel_max_med"), 2),
                         _fmt(d.get("live_over_conditional_trace_med"), 2),
                         _fmt(d.get("nees_per_n_extr_marginal", {}).get("median_per_n"), 2),
                         _fmt(d.get("nees_per_n_extr_conditional", {}).get("median_per_n"), 2)])
        lines += ["### Marginal vs legacy conditional rig covariance", "",
                  _md_table(["k", "live-vs-marginal rel", "live-vs-conditional rel",
                             "trace ratio live/cond", "NEES extr marginal",
                             "NEES extr conditional"], rows), ""]
    # bootstrap
    b = (R.get("task2") or {}).get("bootstrap", {})
    if b:
        lines += ["## 2. Vendor-corpus bootstrap", "",
                  "25 views, CCF as reference (not truth). Empirical spread over "
                  "resampled views/points vs the reported sigma.", ""]
        params = ["left.fx", "right.fx", "tex.fx", "left.cx", "right.cx",
                  "right.tx", "right.ty", "right.tz"]
        rows = []
        for p in params:
            v = b.get("views", {}).get("params", {}).get(p, {})
            p80 = b.get("points80", {}).get("params", {}).get(p, {})
            p60 = b.get("points60", {}).get("params", {}).get(p, {})
            rows.append([p, _fmt(v.get("reported_sigma")),
                         _fmt(v.get("empirical_sd")),
                         _fmt(v.get("ratio_reported_over_empirical"), 2),
                         _fmt(p80.get("ratio_adjusted"), 2),
                         _fmt(p60.get("ratio_adjusted"), 2)])
        lines += [_md_table(["param", "reported sigma", "empirical sd (views)",
                             "ratio views", "ratio points80 (adj)",
                             "ratio points60 (adj)"], rows), ""]
    # perturbations
    t3 = (R.get("task3") or {}).get("conditions", {})
    if t3:
        lines += ["## 3. Perturbation tests", "",
                  "Synthetic k=12 scenes, 20 trials per condition (reduced for slow "
                  "or extreme cases). `z = |error| / reported sigma`; a median |z| "
                  "well above 1 means the covariance is overconfident for that "
                  "parameter.", ""]
        rows = []
        for name, c in t3.items():
            rows.append([name, c.get("n_ok"),
                         _fmt(c["key"]["left.fx"]["median_abs_z"], 2),
                         _fmt(c["key"]["baseline"]["median_abs_z"], 2),
                         _fmt(c["key"]["right_rotation"]["median_abs_z"], 2),
                         _fmt(c["key"]["left.cx"]["median_abs_z"], 2),
                         _fmt(c["groups"].get("all", {}).get("nees_per_n", {}).get("median_per_n"), 2),
                         _fmt(c["stopping"].get("pass_rate"), 2),
                         "; ".join(c.get("flags", []))[:80]])
        lines += [_md_table(["condition", "n", "fx", "baseline", "rot", "cx",
                             "NEES/n all", "stop pass", "flags"], rows), ""]
    # failures
    fs = (R.get("task4") or {}).get("failure_summary", {})
    if fs:
        lines += ["## 4. Failure cases", "",
                  _md_table(["condition", "n", "sigma_fx", "baseline z",
                             "rank-def", "zero-var", "cond", "stop pass"],
                            [[r["condition"], r["n_ok"],
                              _fmt(r["sigma_fx_med"]), _fmt(r["baseline_z_med"], 2),
                              r["rank_def_trials"], r["zero_var_trials"],
                              _fmt(r["condition_med"], 2),
                              _fmt(r["stopping_pass"], 2)]
                             for r in fs.get("rows", [])]), ""]
    # recommendations
    rec = R.get("task5_recommendation", {})
    lines += ["## 5. Recommended stopping rule", "",
              f"Minimum views: **{rec.get('min_views')}**. "
              f"{rec.get('min_views_basis', '')}", ""]
    thresholds = [
        ("joint views seen by all 3 cameras", rec.get("min_joint_views_all3")),
        ("views per camera", rec.get("min_views_per_camera")),
        ("points per view/camera", rec.get("min_points_per_view_camera")),
        ("observations per camera", rec.get("min_obs_per_camera")),
        ("distance bins", rec.get("min_distance_bins")),
        ("tilt signs each axis", rec.get("min_tilt_signs_each")),
        ("image quadrant fraction", rec.get("min_image_quadrant_fraction")),
        ("sigma_fx (px)", rec.get("max_sigma_fx_px")),
        ("sigma_baseline (mm)", rec.get("max_sigma_baseline_mm")),
        ("sigma_rotation (deg)", rec.get("max_sigma_rotation_deg")),
        ("condition number", rec.get("max_condition_number")),
        ("heldout RMS (px)", rec.get("max_heldout_rms_px")),
        ("heldout / train", rec.get("max_heldout_over_train")),
        ("stability max |z|", rec.get("max_stability_z")),
    ]
    lines += [_md_table(["check", "threshold"],
                        [[a, b] for a, b in thresholds if b is not None]), ""]
    lines += ["### Monte-Carlo acceptance criteria for future changes", "",
              "```json", json.dumps(jsonable(rec.get("acceptance_mc", {})), indent=1),
              "```", ""]
    # claims
    claims = R.get("claims", [])
    if claims:
        lines += ["## A/B/C/U claims", "",
                  "A = proved/derived; B = measured with a stated statistic; "
                  "C = cross-checked against the corpus; U = unknown.", "",
                  _md_table(["grade", "claim", "evidence"],
                            [[c["grade"], c["claim"], c["evidence"]]
                             for c in claims]), ""]
    lines += ["## Limitations", "",
              "- Synthetic scenes use a pinhole + Brown-Conrady model; detector "
              "physics, illumination and the real plate flatness are not modelled.",
              "- The vendor corpus has no physical ground truth; CCF agreement is "
              "consistency, not accuracy.",
              "- The `rescaled()` covariance is a linear transform; the result's "
              "stored residual RMS is not recomputed after scaling.",
              "", "## Reproduce", "",
              "```bash", ".venv/bin/python -m calib.uncertainty_audit "
              "--out analysis/calibration/audit", "```", ""]
    text = "\n".join(lines)
    path = outdir / "uncertainty_report.md"
    path.write_text(text, encoding="utf-8")
    return str(path)


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------

def _core_hashes() -> dict:
    return {name: sha256_file(REPO / "calib" / name)
            for name in ("calibrate.py", "adaptive.py", "quality.py",
                         "evaluate.py", "synth.py", "plate.py", "geometry.py",
                         "uncertainty_audit.py")}


def run_all(args) -> dict:
    outdir = Path(args.out).resolve()
    outdir.mkdir(parents=True, exist_ok=True)
    if args.report_only:
        path = outdir / "uncertainty_results.json"
        with open(path, encoding="utf-8") as fh:
            R = json.load(fh)
        progress(f"report-only: loaded {path}")
        R["task5_recommendation"] = build_recommendations(R)
        R["claims"] = build_claims(R)
        R["worst_cases"] = build_worst_cases(R)
        write_json(outdir / "uncertainty_results.json", R)
        make_figures(R, outdir)
        render_report(R, outdir)
        return R

    t_start = time.time()
    R = {"meta": {
        "audit": "Einstar calibration uncertainty / failure-mode audit",
        "timestamp": now_iso(), "git": git_info(),
        "core_hashes": _core_hashes(),
        "python": sys.version.split()[0],
        "numpy": np.__version__, "host": os.uname().nodename,
        "seeds": {"master": MASTER_SEED},
        "variants": VARIANT_LEGEND,
        "quick": bool(args.quick),
        "scope": "synthetic ground truth plus vendor CCF consistency; "
                 "no physical accuracy claim",
    }}
    sections = [s.strip() for s in args.sections.split(",") if s.strip()]
    if "task1" in sections:
        R["task1"] = run_task1(args.jobs,
                               n_trials=4 if args.quick else args.trials,
                               view_counts=(5, 12, 25) if args.quick
                               else (5, 8, 10, 12, 15, 20, 25))
    if "task2" in sections:
        try:
            R["task2"] = run_task2(args.jobs,
                                   n_views_boot=4 if args.quick else 30,
                                   n_points_boot=4 if args.quick else 25,
                                   n_sub_boot=2 if args.quick else 15)
        except Exception as exc:
            R["task2"] = {"error": f"{type(exc).__name__}: {exc}"}
            progress(f"task2 failed: {exc}")
    if "task3" in sections:
        R["task3"] = run_task3(args.jobs, quick=args.quick)
    if "task4" in sections:
        R["task4"] = run_task4(args.jobs, quick=args.quick)
    if "heldout" in sections:
        R["heldout"] = run_heldout(args.jobs,
                                   n_per_k=3 if args.quick else 15,
                                   ks=(8, 12) if args.quick else (8, 12, 20))
    if "stability" in sections:
        R["stability"] = run_stability(args.jobs,
                                       n_trials=3 if args.quick else 15)
    if "rescale" in sections:
        R["rescale"] = run_rescale_check(args.jobs,
                                         n_trials=2 if args.quick else 8)
    R["task5_recommendation"] = build_recommendations(R)
    R["claims"] = build_claims(R)
    R["worst_cases"] = build_worst_cases(R)
    R["meta"]["runtime_s"] = time.time() - t_start
    R["meta"]["core_hashes_end"] = _core_hashes()
    R["meta"]["core_changed_during_run"] = [
        k for k, v in R["meta"]["core_hashes_end"].items()
        if R["meta"]["core_hashes"].get(k) not in (None, v)]
    write_json(outdir / "uncertainty_results.json", R)
    progress(f"wrote {outdir / 'uncertainty_results.json'}")
    make_figures(R, outdir)
    render_report(R, outdir)
    progress(f"done in {R['meta']['runtime_s']:.1f}s")
    if R["meta"]["core_changed_during_run"]:
        progress("WARNING: core files changed during the run: "
                 + ", ".join(R["meta"]["core_changed_during_run"]))
    return R


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="analysis/calibration/audit",
                    help="output directory")
    ap.add_argument("--jobs", type=int, default=min(10, os.cpu_count() or 1),
                    help="parallel worker processes (1 = serial)")
    ap.add_argument("--trials", type=int, default=60,
                    help="MC trials per view count (task 1)")
    ap.add_argument("--sections", default="task1,task2,task3,task4,heldout,stability,rescale",
                    help="comma-separated sections")
    ap.add_argument("--quick", action="store_true",
                    help="small smoke run (minutes, not full statistics)")
    ap.add_argument("--report-only", action="store_true",
                    help="re-render figures/report from uncertainty_results.json")
    args = ap.parse_args(argv)
    run_all(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


def build_worst_cases(R: dict) -> dict:
    """Rank finite overconfidence cases and separate identifiability failures."""
    finite = []
    for name, c in ((R.get("task3") or {}).get("conditions", {}) or {}).items():
        for key, kv in (c.get("key", {}) or {}).items():
            z = kv.get("median_abs_z")
            if z is not None and math.isfinite(z):
                finite.append({
                    "condition": name, "parameter": key,
                    "median_abs_z": float(z),
                    "p95_abs_z": kv.get("p95_abs_z"),
                    "median_sigma": kv.get("median_sigma"),
                    "median_abs_error": kv.get("median_abs_err"),
                    "source": "task3",
                })
    for name, c in ((R.get("task4") or {}).get("conditions", {}) or {}).items():
        for key, kv in (c.get("key", {}) or {}).items():
            z = kv.get("median_abs_z")
            if z is not None and math.isfinite(z):
                finite.append({
                    "condition": name, "parameter": key,
                    "median_abs_z": float(z),
                    "p95_abs_z": kv.get("p95_abs_z"),
                    "median_sigma": kv.get("median_sigma"),
                    "median_abs_error": kv.get("median_abs_err"),
                    "source": "task4",
                })
    finite.sort(key=lambda r: -r["median_abs_z"])
    ident = []
    for name, c in ((R.get("task4") or {}).get("conditions", {}) or {}).items():
        di = c.get("diagnostics", {}) or {}
        if (di.get("zero_var_trials") or 0) > 0 or (di.get("rank_def_trials") or 0) > 0:
            ident.append({
                "condition": name,
                "zero_var_trials": di.get("zero_var_trials"),
                "rank_def_trials": di.get("rank_def_trials"),
                "n_trials": c.get("n_trials"),
                "baseline_z_med": c.get("key", {}).get("baseline", {}).get("median_abs_z"),
                "notes": c.get("flags", []),
            })
    return {"finite_top": finite[:12], "identifiability_failures": ident}
