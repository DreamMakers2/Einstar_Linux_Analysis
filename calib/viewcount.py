#!/usr/bin/env python3
"""Capture-count comparison for the Einstar three-camera calibration engine.

This module answers a deployment question with an explicit leakage-controlled
design: **how many calibration captures are actually needed** for a stated
accuracy target, and does the vendor's 25-step EXStar sequence allow an earlier
stop?

Design (see ``analysis/calibration/audit/viewcount_report.md`` for results)
--------------------------------------------------------------------------
Synthetic ground truth
    30+ scenes.  Each scene has a 25-view *candidate* pool laid out exactly like
    the recovered EXStar protocol (5 orientations x 5 distance bands) and 10
    independent *validation* views that are never used for fitting or for
    selection by any deployable method.  For k in {8,10,12,15,20,25} we compare

    fixed      first k views in EXStar acquisition order (the "stop early" case)
    uniform    k views evenly spaced in the 25-view acquisition order
    random     10 random orderings, prefix of length k (>=10 seeds)
    adaptive   greedy Fisher-information selection with a geometry-diversity
               penalty; refitted at every evaluated count
    oracle     per-metric best subset among *all evaluated* subsets plus two
               ground-truth Fisher orderings; diagnostic upper bound only

Real vendor corpus
    25 views at ``<private-inputs>/vendor-calib-rapid`` with cached
    detections.  K=5 folds; each fold holds out one view per orientation
    (5 views) and fits on the other 20.  k in {8,10,12,15,20} is selected from
    the 20 and validated only on the 5 held-out views.  k=25 is a separate
    all-25 fit and is *not* mixed with the fold results; a 25-fold
    leave-one-view-out experiment is reported separately for k=25.

Metrics
-------
intrinsic error (synthetic vs truth; real vs the shipped CCF, a *reference*
not truth), stereo baseline, IR relative rotation, RGB-to-IR relative
rotation/translation and its pixel effect, independent held-out reprojection
(view pose solved from the left camera only, right/tex predicted through the
rig), held-out IR epipolar residual, NEES/n and 68/95 % coverage, reported
sigma vs empirical spread, plate-pitch sensitivity (+-0.1/0.5/1 %), predicted
depth sigma at 300/400/500 mm, runtime, and coverage/diversity.

Selection/validation leakage controls are encoded in the data structures, not
in prose: candidate/validation ids are disjoint by construction, real folds
never put a held-out view in a fit or in a selection pool, and all-25 / LOO
rows carry explicit ``leakage`` labels.

Usage
-----
    .venv/bin/python -m calib.viewcount --mode quick    # smoke test (~minutes)
    .venv/bin/python -m calib.viewcount --mode full     # full experiment
    .venv/bin/python -m calib.viewcount --mode full --only synth --scenes 30

The JSON cache is content-addressed by the exact engine file hashes, so a
changed ``calib/*.py`` never silently reuses old fits.
"""

from __future__ import annotations

# Keep BLAS single-threaded: each fit is small and the experiment is
# parallelised across processes.  Must be set before numpy is imported.
import os

for _v in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
           "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
    os.environ.setdefault(_v, "1")

import argparse
import hashlib
import json
import math
import multiprocessing as mp
import platform
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from calib import adaptive, ccf, evaluate, plate, quality  # noqa: E402
from calib.calibrate import (CAMERAS, CalibrationResult, CameraIntrinsics,  # noqa: E402
                             Observation, calibrate)
from calib.geometry import (epipolar_residual, fundamental, project,  # noqa: E402
                            rodrigues, rvec_from_R, solve_pnp)

AUDIT_DIR = ROOT / "analysis" / "calibration" / "audit"
FIG_DIR = AUDIT_DIR / "figures"
CACHE_PATH = AUDIT_DIR / "viewcount_cache.json"
RESULTS_PATH = AUDIT_DIR / "viewcount_results.json"
REPORT_PATH = AUDIT_DIR / "viewcount_report.md"

K_VALUES = (8, 10, 12, 15, 20, 25)
SYNTH_DIST = (240.0, 320.0, 400.0, 480.0, 560.0)
# recovered EXStar group order: vertical, x-18, x+18, y+18, y-18 (workflow_report)
SYNTH_ORIENT = ((0.0, 0.0), (-18.0, 0.0), (18.0, 0.0), (0.0, 18.0), (0.0, -18.0))
SYNTH_ROLL = 180.0
CCF_BASELINE_MM = 158.55702724431998
N_FREE_RIG_FIXED_KC = 24  # 4 intrinsics x 3 cameras + 12 extrinsics

#: primary accuracy targets (the experiment derives the smallest k for each)
TARGETS = (
    ("fx_abs_max", 0.5, "fx <= 0.5 px"),
    ("fx_abs_max", 1.0, "fx <= 1.0 px"),
    ("baseline_abs_mm", 0.05, "baseline <= 0.05 mm"),
    ("baseline_abs_mm", 0.10, "baseline <= 0.10 mm"),
    ("heldout_rt_mean", 0.5, "held-out <= 0.5 px"),
)

NFEV = 600
BOOT_N = 10_000
BOOT_SEED = 20261110
PYTHON = sys.executable


# ==========================================================================
# small utilities
# ==========================================================================

def _json_default(o):
    if isinstance(o, (np.floating,)):
        return float(o)
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, np.ndarray):
        return o.tolist()
    if isinstance(o, Path):
        return str(o)
    if isinstance(o, set):
        return sorted(o)
    raise TypeError(f"not JSON serialisable: {type(o)}")


def _clean(o):
    """Recursively convert numpy scalars and non-finite floats to JSON safe."""
    if isinstance(o, dict):
        return {str(k): _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if isinstance(o, (np.floating, float)):
        v = float(o)
        return v if math.isfinite(v) else None
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, np.bool_):
        return bool(o)
    if isinstance(o, np.ndarray):
        return [_clean(v) for v in o.tolist()]
    return o


def _median(v):
    v = [float(x) for x in v if _finite(x)]
    return float(np.median(v)) if v else float("nan")


def _finite(x) -> bool:
    try:
        return math.isfinite(float(x))
    except Exception:
        return False


def _bootstrap_ci(values: Sequence[float], stat=np.mean, level: float = 0.95,
                  n: int = BOOT_N, seed: int = BOOT_SEED) -> dict:
    """Percentile bootstrap CI for a statistic of a 1-D sample."""
    v = np.asarray([float(x) for x in values if _finite(x)], float)
    if v.size == 0:
        return {"n": 0, "mean": None, "ci_lo": None, "ci_hi": None,
                "ucb": None, "lcb": None}
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, v.size, size=(n, v.size))
    boot = np.asarray([stat(v[i]) for i in idx], float)
    lo, hi = np.percentile(boot, [(1 - level) / 2 * 100, (1 + level) / 2 * 100])
    return {"n": int(v.size), "mean": float(np.mean(v)) if stat is np.mean else float(stat(v)),
            "ci_lo": float(lo), "ci_hi": float(hi),
            "ucb": float(np.percentile(boot, 95.0)),
            "lcb": float(np.percentile(boot, 5.0))}


def _percentile_ucb(values: Sequence[float], q: float = 90.0,
                    level: float = 0.95, n: int = BOOT_N,
                    seed: int = BOOT_SEED) -> Optional[float]:
    v = np.asarray([float(x) for x in values if _finite(x)], float)
    if v.size < 3:
        return None
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, v.size, size=(n, v.size))
    boot = np.percentile(v[idx], q, axis=1)
    return float(np.percentile(boot, level * 100))


def _wilcoxon(a, b):
    try:
        from scipy.stats import wilcoxon
        d = np.asarray(a, float) - np.asarray(b, float)
        d = d[np.isfinite(d)]
        if len(d) < 8 or np.allclose(d, 0):
            return None
        return float(wilcoxon(d, zero_method="wilcox").pvalue)
    except Exception:
        return None


# ==========================================================================
# engine fingerprint
# ==========================================================================

ENGINE_FILES = ("calibrate.py", "adaptive.py", "quality.py", "plate.py",
                "pipeline.py", "evaluate.py", "ccf.py", "detect.py",
                "experiments.py", "profiles.py", "synth.py", "geometry.py")


def engine_files() -> list[Path]:
    return [ROOT / "calib" / n for n in ENGINE_FILES if (ROOT / "calib" / n).exists()]


def engine_fingerprint() -> dict:
    files = engine_files()
    h = hashlib.sha256()
    hashes = {}
    for p in files:
        b = p.read_bytes()
        h.update(p.name.encode())
        h.update(b)
        hashes[p.name] = hashlib.sha256(b).hexdigest()
    try:
        head = subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"],
                                       text=True, stderr=subprocess.DEVNULL).strip()
        dirty = bool(subprocess.check_output(
            ["git", "-C", str(ROOT), "status", "--porcelain", "--", "calib"],
            text=True, stderr=subprocess.DEVNULL).strip())
    except Exception:
        head, dirty = None, None
    harness = ROOT / "calib" / "viewcount.py"
    return {"git_head": head, "git_dirty_calib": dirty,
            "files_sha256": hashes, "engine_hash": h.hexdigest()[:16],
            "harness_sha256": (hashlib.sha256(harness.read_bytes()).hexdigest()
                               if harness.exists() else None),
            "python": platform.python_version(), "numpy": np.__version__,
            "platform": platform.platform()}


_ENGINE: dict = {}


def engine() -> dict:
    if not _ENGINE:
        _ENGINE.update(engine_fingerprint())
    return _ENGINE


# ==========================================================================
# design geometry (synthetic scenes use the recovered EXStar rig layout)
# ==========================================================================

def _load_ccf(repo_root: Path = ROOT):
    base = repo_root / "sources" / "calibration" / "res_Einscan-E10_200x150"
    out = {}
    for name, key in (("Left", "left"), ("Right", "right"), ("Tex", "tex")):
        p = base / f"{name}CCF.txt"
        if p.exists():
            out[key] = ccf.load(p, ir=(name != "Tex"))
    return out


def _rel_pose(rec_a, rec_b):
    Ra, ta = rec_a.R(), rec_a.T()
    Rb, tb = rec_b.R(), rec_b.T()
    R = Rb @ Ra.T
    return R, tb - R @ ta


def design_rig() -> dict:
    """CCF-derived relative rig geometry used as *synthetic design*, not truth.

    The factory CCF is a reference for the physical layout (IR baseline
    158.557 mm, IR toe-in ~22.14 deg, RGB offset ~125.3 mm).  Synthetic scenes
    use this geometry with independently drawn intrinsics; no synthetic result
    is therefore a re-statement of the CCF's own numbers.
    """
    recs = _load_ccf()
    if all(k in recs for k in ("left", "right", "tex")):
        Rr, tr = _rel_pose(recs["left"], recs["right"])
        Rt, tt = _rel_pose(recs["left"], recs["tex"])
        return {"R_right": Rr, "t_right": tr, "R_tex": Rt, "t_tex": tt,
                "source": "CCF relative geometry"}
    # fallback: rounded CCF values
    return {"R_right": rodrigues(np.array([0.0, math.radians(22.138), 0.0])),
            "t_right": np.array([-155.469, -0.235, 31.139]),
            "R_tex": rodrigues(np.array([0.0, math.radians(17.005), 0.0])),
            "t_tex": np.array([-122.986, 16.894, 17.134]),
            "source": "fallback constants"}


# ==========================================================================
# synthetic scenes
# ==========================================================================

@dataclass
class Scene:
    seed: int
    cams: dict
    extr: dict
    views: list
    truth: dict
    obs_by_view: dict
    groups: list
    steps: list
    candidate_ids: list
    validation_ids: list
    design_rig: dict
    init: dict


def _distort_project(K, kc, X):
    return project(K, kc, X)


def make_exstar_scene(seed: int, noise_px: float = 0.25,
                      outlier_frac: float = 0.005, outlier_sigma: float = 3.0,
                      n_validation: int = 10) -> Scene:
    """25 EXStar-layout candidate views + independent validation views."""
    from calib.synth import make_camera
    rng = np.random.default_rng(seed)
    cams = {c: make_camera(c, rng) for c in CAMERAS}
    dr = design_rig()
    extr = {"left": (np.eye(3), np.zeros(3)),
            "right": (dr["R_right"], dr["t_right"].copy()),
            "tex": (dr["R_tex"], dr["t_tex"].copy())}
    obj = plate.board_points_mm(plate.DEFAULT_PITCH_MM)
    views, obs_by_view, groups, steps = [], {}, [], []

    def add_view(g, s, R, t):
        vid = len(views)
        views.append((R, t))
        groups.append(g)
        steps.append(s)
        per_cam = {}
        for c in CAMERAS:
            Rc, tc = extr[c]
            Xc = obj @ (Rc @ R).T + (Rc @ t + tc)
            uv = _distort_project(cams[c].K, cams[c].kc, Xc)
            uv = uv + rng.normal(0.0, noise_px, uv.shape)
            if outlier_frac > 0:
                m = rng.random(len(uv)) < outlier_frac
                if m.any():
                    uv[m] += rng.normal(0.0, outlier_sigma, (int(m.sum()), 2))
            dct = {(float(u), float(v)): uv[i]
                   for i, (u, v) in enumerate(plate.lattice_uv())}
            per_cam[c] = Observation(c, vid, dct)
        obs_by_view[vid] = per_cam

    for g, (tx, ty) in enumerate(SYNTH_ORIENT):
        for s, d in enumerate(SYNTH_DIST):
            # small per-scene jitter so scenes are not identical; the design
            # cell (g, s) stays fixed and is used only for coverage reporting.
            dd = d + rng.normal(0.0, 4.0)
            ax = tx + rng.normal(0.0, 1.2)
            ay = ty + rng.normal(0.0, 1.2)
            roll = SYNTH_ROLL + rng.normal(0.0, 1.5)
            lat = (rng.normal(0.0, 8.0), rng.normal(0.0, 8.0))
            R, t = adaptive.board_pose(dd, ax, ay, roll, lat)
            add_view(g, s, R, t)
    n_cand = len(views)
    for _ in range(n_validation):
        # independent poses: off-grid diagonal tilts, random distance/roll,
        # lateral offsets.  They are never members of any selection pool.
        dd = rng.uniform(250.0, 550.0)
        ax = rng.uniform(-20.0, 20.0)
        ay = rng.uniform(-20.0, 20.0)
        roll = SYNTH_ROLL + rng.normal(0.0, 5.0)
        lat = (rng.uniform(-25.0, 25.0), rng.uniform(-25.0, 25.0))
        R, t = adaptive.board_pose(dd, ax, ay, roll, lat)
        add_view(-1, -1, R, t)

    truth = {
        "cameras": {c: cams[c].as_dict() for c in CAMERAS},
        "extrinsics": {c: {"R": extr[c][0].tolist(), "t": extr[c][1].tolist()}
                       for c in CAMERAS},
        "views": [{"R": R.tolist(), "t": t.tolist()} for R, t in views],
        "baseline_mm": float(np.linalg.norm(extr["right"][1])),
        "baseline_rgb_mm": float(np.linalg.norm(extr["tex"][1])),
        "pitch_mm": plate.DEFAULT_PITCH_MM,
        "groups": groups, "steps": steps,
    }
    candidate_ids = list(range(n_cand))
    validation_ids = list(range(n_cand, len(views)))
    # observation lists in deterministic order
    obs_by_view = {v: {c: obs_by_view[v][c] for c in CAMERAS} for v in sorted(obs_by_view)}
    # initial intrinsics: perturbed truth (fx/fy/cx/cy ~ N(0,6 px)); distortion
    # is held fixed at the synthetic factory-like value by the main protocol.
    ri = np.random.default_rng(seed + 90001)
    init = {}
    for c in CAMERAS:
        cam = cams[c]
        init[c] = CameraIntrinsics(cam.fx + ri.normal(0, 6.0),
                                   cam.fy + ri.normal(0, 6.0),
                                   cam.cx + ri.normal(0, 6.0),
                                   cam.cy + ri.normal(0, 6.0), cam.kc.copy())
    return Scene(seed, cams, extr, views, truth, obs_by_view, groups, steps,
                 candidate_ids, validation_ids, dr, init)


def scene_obs(scene: Scene, view_ids: Iterable[int]) -> list[Observation]:
    out = []
    for v in sorted(int(x) for x in view_ids):
        out.extend(scene.obs_by_view[v][c] for c in CAMERAS if c in scene.obs_by_view[v])
    return out


def scene_truth_rig(scene: Scene) -> np.ndarray:
    return truth_rig_vector(scene.truth)


def truth_rig_vector(truth: dict) -> np.ndarray:
    x = np.zeros(39)
    for i, c in enumerate(CAMERAS):
        g = truth["cameras"][c]
        x[9 * i:9 * i + 9] = [g["fx"], g["fy"], g["cx"], g["cy"], *g["kc"]]
    for i, c in enumerate(("right", "tex")):
        R = np.asarray(truth["extrinsics"][c]["R"], float)
        t = np.asarray(truth["extrinsics"][c]["t"], float)
        x[27 + 6 * i:27 + 6 * i + 6] = np.concatenate([rvec_from_R(R), t])
    return x


# ==========================================================================
# real vendor corpus
# ==========================================================================

@dataclass
class RealData:
    init: dict
    obs_by_view: dict
    detections: dict
    ref: dict
    ccf_params: dict
    groups: list
    steps: list
    view_ids: list
    fold_holdout: dict
    fold_train: dict
    ccf_baseline_mm: float


def load_vendor(repo_root: Path = ROOT) -> RealData:
    import pickle
    pkl = repo_root / "analysis" / "calibration" / "engine" / "vendor_detections.pkl"
    if not pkl.exists():
        raise FileNotFoundError(pkl)
    det = pickle.loads(pkl.read_bytes())
    recs = _load_ccf(repo_root)
    if not recs:
        raise FileNotFoundError("CCF records not found")
    init = {c: CameraIntrinsics.from_ccf(recs[c]) for c in CAMERAS}
    side = {"left": "Left", "right": "Right", "tex": "Tex"}
    obs_by_view = {}
    for v1 in range(1, 26):
        v = v1 - 1
        per = {}
        for c in CAMERAS:
            z = det.get((side[c], v1))
            if z:
                per[c] = Observation(c, v, dict(z))
        if per:
            obs_by_view[v] = per
    ref = {
        "ir": _rel_pose(recs["left"], recs["right"]),
        "rgb": _rel_pose(recs["left"], recs["tex"]),
        "K": {c: recs[c].K() for c in CAMERAS},
        "baseline_mm": float(np.linalg.norm(_rel_pose(recs["left"], recs["right"])[1])),
    }
    ccf_params = {}
    for c in CAMERAS:
        r = recs[c]
        ccf_params[c] = {"fx": float(r.K()[0, 0]), "fy": float(r.K()[1, 1]),
                         "cx": float(r.K()[0, 2]), "cy": float(r.K()[1, 2]),
                         "kc": [float(x) for x in r.dist()]}
    view_ids = sorted(obs_by_view)
    fold_holdout, fold_train = {}, {}
    for f in range(5):
        held = [5 * g + f for g in range(5)]  # zero-based: one view per orientation
        held = [v for v in held if v in obs_by_view]
        fold_holdout[f] = held
        fold_train[f] = [v for v in view_ids if v not in held]
    groups = [v // 5 for v in view_ids]
    steps = [v % 5 for v in view_ids]
    return RealData(init, obs_by_view, det, ref, ccf_params, groups, steps,
                    view_ids, fold_holdout, fold_train,
                    float(ref["baseline_mm"]))


def real_obs(data: RealData, view_ids: Iterable[int]) -> list[Observation]:
    out = []
    for v in sorted(int(x) for x in view_ids):
        per = data.obs_by_view.get(v, {})
        out.extend(per[c] for c in CAMERAS if c in per)
    return out


def fold_labels(data: RealData, view_ids: Iterable[int]) -> tuple[list[int], list[int]]:
    ids = sorted(int(v) for v in view_ids)
    return [v // 5 for v in ids], [v % 5 for v in ids]


# ==========================================================================
# metrics
# ==========================================================================

_UV_INDEX = {k: i for i, k in enumerate(map(tuple, plate.lattice_uv()))}


def _keys(o: Observation) -> list:
    return [k for k in o.uv if k in _UV_INDEX]


def _rel(result: CalibrationResult, a: str, b: str):
    Ra, ta = result.extrinsics[a]
    Rb, tb = result.extrinsics[b]
    R = Rb @ Ra.T
    return R, tb - R @ ta


def _angle_deg(Ra: np.ndarray, Rb: np.ndarray) -> float:
    c = (np.trace(np.asarray(Ra).T @ np.asarray(Rb)) - 1.0) / 2.0
    return float(np.degrees(np.arccos(np.clip(c, -1.0, 1.0))))


def _cam_errors(result: CalibrationResult, truth_cams: dict) -> dict:
    out = {}
    for c in CAMERAS:
        e = result.cameras[c]
        g = truth_cams[c]
        out[c] = {
            "fx": float(e.fx - g.fx), "fy": float(e.fy - g.fy),
            "cx": float(e.cx - g.cx), "cy": float(e.cy - g.cy),
            "fx_rel": float((e.fx - g.fx) / g.fx),
            "fy_rel": float((e.fy - g.fy) / g.fy),
            "cx_rel": float((e.cx - g.cx) / g.cx),
            "cy_rel": float((e.cy - g.cy) / g.cy),
        }
    return out


def _probe_plane(distance_mm: float = 400.0, n: int = 7) -> np.ndarray:
    """Points on a Z=distance plane covering the left image FOV (7x7)."""
    fx, fy, cx, cy = 1150.0, 1150.0, 640.0, 512.0
    us = np.linspace(0.12, 0.88, n) * 1280.0
    vs = np.linspace(0.12, 0.88, n) * 1024.0
    uu, vv = np.meshgrid(us, vs)
    x = (uu.ravel() - cx) / fx * distance_mm
    y = (vv.ravel() - cy) / fy * distance_mm
    return np.column_stack([x, y, np.full(uu.size, distance_mm)])


def pixel_effect(K_est: np.ndarray, rel_est, K_ref: np.ndarray,
                 rel_ref, distance_mm: float = 400.0) -> dict:
    """Pixel displacement (px) between two RGB-to-IR relative poses.

    Probe points live on a plane at ``distance_mm`` in the left/IR frame and
    are mapped into the RGB camera with each relative pose.  ``total`` uses
    each model's own K; ``pose_only`` projects both with ``K_est`` so intrinsic
    differences do not leak into the pose metric.
    """
    X = _probe_plane(distance_mm)
    Re, te = rel_est
    Rr, tr = rel_ref
    Xe = X @ Re.T + te
    Xr = X @ Rr.T + tr
    pe = project(K_est, np.zeros(5), Xe)
    pr = project(K_ref, np.zeros(5), Xr)
    pr_same = project(K_est, np.zeros(5), Xr)
    d_total = np.linalg.norm(pe - pr, axis=1)
    d_pose = np.linalg.norm(pe - pr_same, axis=1)
    return {"median_px": float(np.median(d_total)),
            "p95_px": float(np.percentile(d_total, 95)),
            "median_pose_only_px": float(np.median(d_pose)),
            "p95_pose_only_px": float(np.percentile(d_pose, 95)),
            "distance_mm": float(distance_mm)}


def nees_metrics(result: CalibrationResult, truth: dict) -> dict:
    out = {"nees_rig": None, "nees_n": None, "n_free_rig": 0,
           "cov68": None, "cov95": None, "cov68_intr": None,
           "cov68_extr": None, "chi2_95": None, "nees_pass": None,
           "z_fx": None, "z_cx": None, "z_baseline": None}
    if result.covariance is None:
        return out
    fm = np.asarray(result.info.get("free_mask", np.ones(39, bool)), bool)[:39]
    idx = np.where(fm)[0]
    if idx.size == 0:
        return out
    d = adaptive.rig_vector(result) - truth_rig_vector(truth)
    C = result.covariance[:39, :39][np.ix_(idx, idx)]
    try:
        nees = float(d[idx] @ np.linalg.pinv(C, rcond=1e-12) @ d[idx])
    except np.linalg.LinAlgError:
        return out
    sd = np.sqrt(np.maximum(np.diag(C), 0.0))
    ok = sd > 0
    z = np.zeros_like(d[idx])
    z[ok] = d[idx][ok] / sd[ok]
    n = int(idx.size)
    intr = [i for i, j in enumerate(idx) if j < 27]
    extr = [i for i, j in enumerate(idx) if j >= 27]
    out.update({
        "nees_rig": nees, "nees_n": nees / n if n else None, "n_free_rig": n,
        "cov68": float(np.mean(np.abs(z[ok]) <= 1.0)) if ok.any() else None,
        "cov95": float(np.mean(np.abs(z[ok]) <= 1.96)) if ok.any() else None,
        "cov68_intr": float(np.mean(np.abs(z[intr]) <= 1.0)) if intr else None,
        "cov68_extr": float(np.mean(np.abs(z[extr]) <= 1.0)) if extr else None,
        "z_fx": [float(z[i]) for i in intr if i % 9 == 0],
        "z_cx": [float(z[i]) for i in intr if i % 9 == 2],
        "z_baseline": None,
    })
    try:
        from scipy.stats import chi2
        out["chi2_95"] = float(chi2.ppf(0.95, n))
        out["nees_pass"] = bool(nees <= out["chi2_95"])
    except Exception:
        pass
    bs = quality.baseline_sigma(result)
    b_err = float(np.linalg.norm(result.baseline()) -
                  float(truth.get("baseline_mm", np.nan)))
    if _finite(bs) and bs > 0 and _finite(b_err):
        out["z_baseline"] = float(b_err / bs)
    return out


def _rig_truth(result: CalibrationResult, truth: dict) -> dict:
    Rg_ir = np.asarray(truth["extrinsics"]["right"]["R"], float)
    tg_ir = np.asarray(truth["extrinsics"]["right"]["t"], float)
    Rg_rgb = np.asarray(truth["extrinsics"]["tex"]["R"], float)
    tg_rgb = np.asarray(truth["extrinsics"]["tex"]["t"], float)
    Re_ir, te_ir = _rel(result, "left", "right")
    Re_rgb, te_rgb = _rel(result, "left", "tex")
    g = truth["cameras"]["tex"]
    K_gt_tex = np.array([[g["fx"], 0.0, g["cx"]],
                         [0.0, g["fy"], g["cy"]],
                         [0.0, 0.0, 1.0]], float)
    return {
        "baseline_err_mm": float(np.linalg.norm(result.baseline()) - truth["baseline_mm"]),
        "ir_rot_err_deg": _angle_deg(Re_ir, Rg_ir),
        "ir_trans_err_mm": float(np.linalg.norm(te_ir - tg_ir)),
        "rgb_rot_err_deg": _angle_deg(Re_rgb, Rg_rgb),
        "rgb_trans_err_mm": float(np.linalg.norm(te_rgb - tg_rgb)),
        "pixel": pixel_effect(result.cameras["tex"].K, (Re_rgb, te_rgb),
                              K_gt_tex, (Rg_rgb, tg_rgb)),
    }


def ccf_metrics(result: CalibrationResult, ref: dict) -> dict:
    Re_ir, te_ir = _rel(result, "left", "right")
    Re_rgb, te_rgb = _rel(result, "left", "tex")
    Rr_ir, tr_ir = ref["ir"]
    Rr_rgb, tr_rgb = ref["rgb"]
    K_ref_tex = ref["K"]["tex"]
    pe = pixel_effect(result.cameras["tex"].K, (Re_rgb, te_rgb),
                      K_ref_tex, (Rr_rgb, tr_rgb))
    out = {
        "dfx": [float(result.cameras[c].fx - ref["K"][c][0, 0]) for c in CAMERAS],
        "dfy": [float(result.cameras[c].fy - ref["K"][c][1, 1]) for c in CAMERAS],
        "dcx": [float(result.cameras[c].cx - ref["K"][c][0, 2]) for c in CAMERAS],
        "dcy": [float(result.cameras[c].cy - ref["K"][c][1, 2]) for c in CAMERAS],
        "dfx_rel": [float((result.cameras[c].fx - ref["K"][c][0, 0]) / ref["K"][c][0, 0])
                    for c in CAMERAS],
        "baseline_vs_ccf_mm": float(np.linalg.norm(result.baseline()) - ref["baseline_mm"]),
        "baseline_vs_ccf_rel": float(np.linalg.norm(result.baseline()) / ref["baseline_mm"] - 1.0),
        "ir_rot_vs_ccf_deg": _angle_deg(Re_ir, Rr_ir),
        "ir_trans_vs_ccf_mm": float(np.linalg.norm(te_ir - tr_ir)),
        "rgb_rot_vs_ccf_deg": _angle_deg(Re_rgb, Rr_rgb),
        "rgb_trans_vs_ccf_mm": float(np.linalg.norm(te_rgb - tr_rgb)),
        "rgb_pixel_median_px": pe["median_px"],
        "rgb_pixel_p95_px": pe["p95_px"],
        "rgb_pixel_pose_only_median_px": pe["median_pose_only_px"],
    }
    out["dfx_max_abs"] = float(max(abs(x) for x in out["dfx"]))
    out["dcx_max_abs"] = float(max(abs(x) for x in out["dcx"]))
    return out


def heldout_view_errors(result: CalibrationResult, obs_by_view: dict,
                        view: int, pitch: float) -> Optional[dict]:
    per = obs_by_view.get(int(view), {})
    lo = per.get("left")
    if lo is None:
        return None
    obj = plate.board_points_mm(pitch)
    keys = _keys(lo)
    if len(keys) < 8:
        return None
    X = obj[[_UV_INDEX[k] for k in keys]]
    pose = solve_pnp(X, np.array([lo.uv[k] for k in keys]),
                     result.cameras["left"].K, result.cameras["left"].kc)
    if pose is None:
        return None
    out = {"view": int(view), "cameras": {}, "errors": {}}
    for c in CAMERAS:
        o = per.get(c)
        if o is None:
            continue
        kk = _keys(o)
        if len(kk) < 8:
            continue
        Xc = obj[[_UV_INDEX[k] for k in kk]]
        pred = project(result.cameras[c].K, result.cameras[c].kc,
                       (Xc @ pose.R.T + pose.t) @ result.extrinsics[c][0].T
                       + result.extrinsics[c][1])
        e = np.linalg.norm(pred - np.array([o.uv[k] for k in kk]), axis=1)
        out["cameras"][c] = {"rms_px": float(np.sqrt(np.mean(e ** 2))),
                             "max_px": float(e.max()), "n": int(len(e))}
        out["errors"][c] = e
    if not out["cameras"]:
        return None
    rt = [out["cameras"][c]["rms_px"] for c in ("right", "tex") if c in out["cameras"]]
    out["rt_rms_px"] = float(np.mean(rt)) if rt else float("nan")
    errs = np.concatenate([out["errors"][c] for c in ("right", "tex")
                           if c in out["errors"]]) if rt else np.zeros(0)
    if errs.size:
        out["rt_pooled_rms_px"] = float(np.sqrt(np.mean(errs ** 2)))
        med = float(np.median(errs))
        mad = float(np.median(np.abs(errs - med)))
        thr = med + 3.0 * 1.4826 * mad if mad > 0 else med
        keep = errs[errs <= thr]
        out["rt_robust_rms_px"] = float(np.sqrt(np.mean(keep ** 2))) if keep.size else float("nan")
        out["rt_trimmed_rms_px"] = float(np.sqrt(np.mean(np.sort(errs)[:max(1, int(np.ceil(0.98 * errs.size)))] ** 2)))
        out["n_rt"] = int(errs.size)
    return out


def heldout_metrics(result: CalibrationResult, obs_by_view: dict,
                    holdout_ids: Iterable[int], pitch: float) -> dict:
    per_view = []
    for v in holdout_ids:
        r = heldout_view_errors(result, obs_by_view, v, pitch)
        if r is not None:
            per_view.append(r)
    out = {"per_view": [{k: v for k, v in p.items() if k != "errors"}
                        for p in per_view],
           "n_views": len(per_view), "heldout_rt_mean": None,
           "heldout_rt_median": None, "heldout_rt_max": None,
           "heldout_rt_pooled": None, "heldout_rt_robust_mean": None,
           "heldout_left_mean": None, "heldout_rt_trimmed_mean": None}
    if not per_view:
        return out
    rt = [p["rt_rms_px"] for p in per_view if _finite(p.get("rt_rms_px"))]
    ro = [p["rt_robust_rms_px"] for p in per_view if _finite(p.get("rt_robust_rms_px"))]
    tr = [p["rt_trimmed_rms_px"] for p in per_view if _finite(p.get("rt_trimmed_rms_px"))]
    lf = [p["cameras"]["left"]["rms_px"] for p in per_view if "left" in p["cameras"]]
    if rt:
        out.update({"heldout_rt_mean": float(np.mean(rt)),
                    "heldout_rt_median": float(np.median(rt)),
                    "heldout_rt_max": float(np.max(rt))})
    if ro:
        out["heldout_rt_robust_mean"] = float(np.mean(ro))
    if tr:
        out["heldout_rt_trimmed_mean"] = float(np.mean(tr))
    if lf:
        out["heldout_left_mean"] = float(np.mean(lf))
    # pooled across views
    pooled = []
    for p in per_view:
        e = []
        for c in ("right", "tex"):
            if c in p["errors"]:
                e.append(p["errors"][c])
        if e:
            pooled.append(np.concatenate(e))
    if pooled:
        allv = np.concatenate(pooled)
        out["heldout_rt_pooled"] = float(np.sqrt(np.mean(allv ** 2)))
    return out


def epipolar_metrics(result: CalibrationResult, obs_by_view: dict,
                     view_ids: Iterable[int]) -> dict:
    Re, te = _rel(result, "left", "right")
    F = fundamental(result.cameras["left"].K, result.cameras["right"].K, Re, te)
    vals, n = [], 0
    for v in view_ids:
        per = obs_by_view.get(int(v), {})
        lo, ro = per.get("left"), per.get("right")
        if lo is None or ro is None:
            continue
        ks = [k for k in lo.uv if k in ro.uv]
        if len(ks) < 8:
            continue
        x1 = np.array([lo.uv[k] for k in ks])
        x2 = np.array([ro.uv[k] for k in ks])
        e = epipolar_residual(F, x1, x2)
        vals.append(e)
        n += len(e)
    out = {"median_px": None, "rms_px": None, "p90_px": None, "n": 0,
           "n_pairs": len(vals)}
    if vals:
        e = np.concatenate(vals)
        out.update({"median_px": float(np.median(e)),
                    "rms_px": float(np.sqrt(np.mean(e ** 2))),
                    "p90_px": float(np.percentile(e, 90)), "n": int(len(e))})
    return out


def depth_metrics(result: CalibrationResult) -> dict:
    return {str(z): quality.depth_uncertainty(result, float(z)) for z in (300, 400, 500)}


def diversity_metrics(result: CalibrationResult, obs: list[Observation],
                      view_ids: Iterable[int], groups: Sequence[int],
                      steps: Sequence[int]) -> dict:
    div = quality.view_diversity(result)
    cov = quality.image_coverage(obs)
    ids = sorted(int(v) for v in view_ids)
    cells = set()
    for v in ids:
        if 0 <= v < len(groups) and groups[v] >= 0:
            cells.add((int(groups[v]), int(steps[v])))
    out = {
        "distance_bins": div["distance_bin_count"],
        "tilt_x_signs": div["tilt_x_signs"],
        "tilt_y_signs": div["tilt_y_signs"],
        "quadrant_fraction_min": float(min(cov[c]["quadrant_fraction"] for c in CAMERAS)),
        "cell_count": len(cells),
        "n_groups": len(set(groups[v] for v in ids if 0 <= v < len(groups) and groups[v] >= 0)),
        "n_steps": len(set(steps[v] for v in ids if 0 <= v < len(steps) and steps[v] >= 0)),
        "cell_fraction": float(len(cells) / 25.0),
    }
    return out


def make_record(domain: str, method: str, k: int, view_ids: Iterable[int],
                result: CalibrationResult, dt: float, *,
                obs_by_view: dict, train_obs: list[Observation],
                scene: Optional[int] = None, fold: Optional[int] = None,
                seed: Optional[int] = None, pitch: float, free_kc: bool,
                holdout_ids: Iterable[int], truth: Optional[dict] = None,
                ref: Optional[dict] = None, groups: Optional[Sequence[int]] = None,
                steps: Optional[Sequence[int]] = None, pool_size: Optional[int] = None,
                leakage: str = "train_only", tags: Optional[dict] = None) -> dict:
    view_ids = sorted(int(v) for v in view_ids)
    sd = result.param_std()
    sig = {f"{c}.{f}": sd.get(f"{c}.{f}") for c in CAMERAS
           for f in ("fx", "fy", "cx", "cy")}
    sig.update({f"{c}.{f}": sd.get(f"{c}.{f}") for c in CAMERAS
                for f in ("k1", "k2", "p1", "p2", "k3")})
    Re_ir, te_ir = _rel(result, "left", "right")
    Re_rgb, te_rgb = _rel(result, "left", "tex")
    rgb_sig = quality.rgb_alignment(result)
    bsig = quality.baseline_sigma(result)
    rec = {
        "domain": domain, "method": method, "k": int(k), "scene": scene,
        "fold": fold, "seed": seed, "view_ids": view_ids, "n_views": len(view_ids),
        "holdout_ids": sorted(int(v) for v in holdout_ids),
        "pitch_mm": float(pitch), "free_kc": bool(free_kc),
        "pool_size": int(pool_size) if pool_size is not None else None,
        "full_pool": bool(pool_size is not None and len(view_ids) == pool_size),
        "leakage": leakage, "tags": tags or {},
        "time_s": float(dt),
        "rms_train_px": float(result.residual_rms),
        "max_train_px": float(result.residual_max),
        "n_obs": int(result.n_observations),
        "n_parameters": int(result.n_parameters),
        "fit": {
            "success": bool(result.info.get("success", False)),
            "nfev": int(result.info.get("nfev", 0)),
            "condition": float(result.info.get("covariance", {}).get("condition", np.nan)),
            "rank_kept": int(result.info.get("covariance", {}).get("rank_kept", 0)),
            "n_free": int(result.info.get("covariance", {}).get("n_free", 0)),
            "estimator": result.info.get("covariance", {}).get("estimator"),
            "warning": result.info.get("covariance", {}).get("warning"),
        },
        "cameras": {c: {"fx": float(result.cameras[c].fx),
                        "fy": float(result.cameras[c].fy),
                        "cx": float(result.cameras[c].cx),
                        "cy": float(result.cameras[c].cy),
                        "kc": [float(x) for x in result.cameras[c].kc],
                        "skew": float(result.cameras[c].skew)}
                    for c in CAMERAS},
        "sigma": {kk: (float(v) if _finite(v) else None) for kk, v in sig.items()},
        "baseline_mm": float(np.linalg.norm(result.baseline())),
        "baseline_vec": [float(x) for x in result.baseline()],
        "ir_rot_deg": _angle_deg(Re_ir, np.eye(3)),
        "rgb_rot_deg": _angle_deg(Re_rgb, np.eye(3)),
        "rgb_translation_mm": float(np.linalg.norm(te_rgb)),
        "sigma_baseline_mm": (float(bsig) if _finite(bsig) else None),
        "sigma_ir": {kk: (float(v) if _finite(v) else None) for kk, v in
                     quality.relative_pose_sigma(result, "left", "right").items()},
        "sigma_rgb": {kk: (float(v) if _finite(v) else None) for kk, v in
                      quality.relative_pose_sigma(result, "left", "tex").items()},
        "sigma_rgb_pixel_px": (float(rgb_sig["pixel_at_distance"])
                               if _finite(rgb_sig["pixel_at_distance"]) else None),
        "depth": {z: {kk: (float(v) if _finite(v) else None) for kk, v in d.items()}
                  for z, d in depth_metrics(result).items()},
        "diversity": diversity_metrics(result, train_obs, view_ids,
                                       groups or [], steps or []),
        "heldout": heldout_metrics(result, obs_by_view, holdout_ids, pitch),
        "epipolar": epipolar_metrics(result, obs_by_view, holdout_ids),
        "tag": tags or {},
    }
    if domain == "synthetic" and truth is not None:
        ce = {c: _cam_errors(result, {cc: CameraIntrinsics(
            truth["cameras"][cc]["fx"], truth["cameras"][cc]["fy"],
            truth["cameras"][cc]["cx"], truth["cameras"][cc]["cy"],
            np.asarray(truth["cameras"][cc]["kc"])) for cc in CAMERAS})[c]
            for c in CAMERAS}
        rt = _rig_truth(result, truth)
        rec["gt"] = {
            "cameras": ce,
            "fx_abs_max": float(max(abs(ce[c]["fx"]) for c in CAMERAS)),
            "fy_abs_max": float(max(abs(ce[c]["fy"]) for c in CAMERAS)),
            "cx_abs_max": float(max(abs(ce[c]["cx"]) for c in CAMERAS)),
            "cy_abs_max": float(max(abs(ce[c]["cy"]) for c in CAMERAS)),
            "fx_rel_max": float(max(abs(ce[c]["fx_rel"]) for c in CAMERAS)),
            "fx_abs_ir_max": float(max(abs(ce[c]["fx"]) for c in ("left", "right"))),
            **rt,
            "nees": nees_metrics(result, truth),
        }
    if domain == "real" and ref is not None:
        rec["ccf"] = ccf_metrics(result, ref)
    return rec



# ==========================================================================
# subset selection
# ==========================================================================

def fixed_subset(pool: Sequence[int], k: int) -> list[int]:
    return sorted(int(v) for v in list(pool)[:k])


def uniform_subset(pool: Sequence[int], k: int) -> list[int]:
    pool = list(pool)
    if k >= len(pool):
        return sorted(int(v) for v in pool)
    idx = np.unique(np.round(np.linspace(0, len(pool) - 1, k)).astype(int))
    return sorted(int(pool[i]) for i in idx)


def random_subset_order(pool: Sequence[int], seed: int) -> list[int]:
    rng = np.random.default_rng(1000003 + int(seed) * 7919)
    return [int(v) for v in rng.permutation(list(pool))]


def free_rig_indices(free_kc: bool) -> np.ndarray:
    free = np.zeros(39, bool)
    for i in range(3):
        free[9 * i:9 * i + 4] = True
        if free_kc:
            free[9 * i + 4:9 * i + 9] = True
    free[27:39] = True
    return np.where(free)[0]


def candidate_poses(obs_by_view: dict, view_ids: Iterable[int],
                    cams: dict, extr: dict) -> dict:
    """Board pose in the left frame for every view, using PnP only."""
    out = {}
    for v in view_ids:
        per = obs_by_view.get(int(v), {})
        pose = None
        used_cam = None
        for c in ("left", "right", "tex"):
            o = per.get(c)
            if o is None:
                continue
            keys = _keys(o)
            if len(keys) < 8:
                continue
            obj = plate.board_points_mm(plate.DEFAULT_PITCH_MM)[[_UV_INDEX[k] for k in keys]]
            pose = solve_pnp(obj, np.array([o.uv[k] for k in keys]),
                             cams[c].K, cams[c].kc)
            if pose is not None:
                used_cam = c
                break
        if pose is None:
            out[int(v)] = None
            continue
        Rc, tc = extr[used_cam]
        R = Rc.T @ pose.R
        t = Rc.T @ (pose.t - tc)
        z = float(t[2])
        n = R[:, 2]
        tx = float(np.degrees(np.arctan2(n[1], max(abs(n[2]), 1e-9))))
        ty = float(np.degrees(np.arctan2(n[0], max(abs(n[2]), 1e-9))))
        out[int(v)] = {"R": R, "t": t, "z": z, "tilt_x": tx, "tilt_y": ty}
    return out


def farthest_seed(poses: dict, pool: Sequence[int], n: int) -> list[int]:
    valid = [int(v) for v in pool if poses.get(int(v)) is not None]
    if not valid:
        return [int(v) for v in list(pool)[:n]]
    feats = np.array([[poses[v]["z"] / 80.0, poses[v]["tilt_x"] / 18.0,
                       poses[v]["tilt_y"] / 18.0] for v in valid], float)
    mu = feats.mean(0)
    chosen = [valid[int(np.argmax(np.linalg.norm(feats - mu, axis=1)))]]
    while len(chosen) < min(n, len(valid)):
        sel = [valid.index(c) for c in chosen]
        d = np.min(np.linalg.norm(feats[:, None, :] - feats[sel][None, :, :], axis=2), axis=1)
        d[sel] = -1.0
        chosen.append(valid[int(np.argmax(d))])
    if len(chosen) < n:
        rest = [int(v) for v in pool if int(v) not in chosen]
        chosen.extend(rest[:n - len(chosen)])
    return chosen


def _diverse_actual(cands: list[int], selected: list[int], poses: dict) -> list[int]:
    """Same diversity rule as adaptive._diverse_pool, applied to real views."""
    def binof(v):
        p = poses[v]
        db = round(p["z"] / 40.0)
        sx = 0 if abs(p["tilt_x"]) < 4 else (1 if p["tilt_x"] > 0 else -1)
        sy = 0 if abs(p["tilt_y"]) < 4 else (1 if p["tilt_y"] > 0 else -1)
        return db, (sx, sy)

    dcount, scount = {}, {}
    for v in selected:
        if poses.get(v) is None:
            continue
        db, ss = binof(v)
        dcount[db] = dcount.get(db, 0) + 1
        scount[ss] = scount.get(ss, 0) + 1
    out = []
    for v in cands:
        if poses.get(v) is None:
            continue
        db, ss = binof(v)
        if dcount.get(db, 0) >= 2 or scount.get(ss, 0) >= 2:
            continue
        out.append(v)
    return out if out else list(cands)


def gt_fisher_orders(scene: Scene, free_kc: bool = False) -> list[list[int]]:
    """Two ground-truth Fisher orderings (A- and D-optimal greedy).

    Uses the *true* rig parameters and the *true* candidate poses.  These are
    diagnostic oracles, never deployable selectors.
    """
    x_gt = scene_truth_rig(scene)
    free = free_rig_indices(free_kc)
    H_add = {}
    for v in scene.candidate_ids:
        R, t = scene.views[v]
        J = adaptive.candidate_jacobian(x_gt, R, t, plate.DEFAULT_PITCH_MM)
        if J is None:
            continue
        Jr = J[:, :39][:, free]
        Jv = J[:, 39:]
        try:
            Hv = np.linalg.pinv(Jv.T @ Jv, rcond=1e-10)
        except np.linalg.LinAlgError:
            continue
        h = Jr.T @ Jr - Jr.T @ Jv @ Hv @ Jv.T @ Jr
        h = 0.5 * (h + h.T)
        H_add[v] = h
    if not H_add:
        return [list(scene.candidate_ids)]
    pool = [v for v in scene.candidate_ids if v in H_add]
    orders = []
    for criterion in ("A", "D"):
        H = np.zeros((len(free), len(free)))
        chosen = []
        remaining = list(pool)
        while remaining:
            best = None
            for v in remaining:
                S = H + H_add[v]
                try:
                    if criterion == "A":
                        score = float(np.trace(np.linalg.pinv(S, rcond=1e-12)))
                    else:
                        w = np.linalg.eigvalsh(0.5 * (S + S.T))
                        w = np.clip(w, 1e-12, None)
                        score = -float(np.sum(np.log(w)))
                except np.linalg.LinAlgError:
                    continue
                if best is None or score < best[0]:
                    best = (score, v)
            if best is None:
                break
            v = best[1]
            chosen.append(v)
            H = H + H_add[v]
            remaining.remove(v)
        orders.append(chosen)
    return orders


# ==========================================================================
# fitting, jobs, worker
# ==========================================================================

_G: dict = {"scenes": {}, "real": None}


def fit_calibration(obs: list[Observation], pitch: float, init: dict,
                    free_kc: bool) -> tuple[CalibrationResult, float]:
    t0 = time.perf_counter()
    res = calibrate(obs, pitch, init, free_kc=free_kc,
                    free_intrinsics=True, max_nfev=NFEV)
    return res, time.perf_counter() - t0


def _data_for(job: dict):
    if job["domain"] == "synthetic":
        return _G["scenes"][int(job["scene"])]
    return _G["real"]


def _obs_for(job: dict, view_ids: Iterable[int]) -> list[Observation]:
    if job["domain"] == "synthetic":
        return scene_obs(_G["scenes"][int(job["scene"])], view_ids)
    return real_obs(_G["real"], view_ids)


def _build_record(job: dict, view_ids: list[int], res: CalibrationResult,
                  dt: float, method: str, k: int) -> dict:
    data = _data_for(job)
    if job["domain"] == "synthetic":
        obs_by_view = data.obs_by_view
        truth, ref = data.truth, None
        groups, steps = data.groups, data.steps
    else:
        obs_by_view = data.obs_by_view
        truth, ref = None, data.ref
        groups, steps = data.groups, data.steps
    return make_record(
        job["domain"], method, k, view_ids, res, dt,
        obs_by_view=obs_by_view, train_obs=_obs_for(job, view_ids),
        scene=job.get("scene"), fold=job.get("fold"), seed=job.get("seed"),
        pitch=job["pitch"], free_kc=job["free_kc"],
        holdout_ids=job.get("holdout", []), truth=truth, ref=ref,
        groups=groups, steps=steps, pool_size=job.get("pool_size"),
        leakage=job.get("leakage", "train_only"), tags=job.get("tags"))


def run_single_job(job: dict) -> dict:
    try:
        res, dt = fit_calibration(_obs_for(job, job["view_ids"]),
                                  job["pitch"], _data_for(job).init, job["free_kc"])
        rec = _build_record(job, list(job["view_ids"]), res, dt,
                            job.get("method", "single"), int(job.get("k", len(job["view_ids"]))))
        return {"job_key": job["key"], "records": [rec], "error": None}
    except Exception:
        return {"job_key": job.get("key"), "records": [],
                "error": traceback.format_exc()}


def run_adaptive_job(job: dict) -> dict:
    try:
        data = _data_for(job)
        pool = [int(v) for v in job["pool"]]
        targets = sorted(int(k) for k in job["k_targets"])
        poses = candidate_poses(data.obs_by_view, pool, data.init,
                                _planning_extr(data))
        order = farthest_seed(poses, pool, int(job.get("seed_n", 5)))
        res, dt = fit_calibration(_obs_for(job, order), job["pitch"], data.init,
                                  job["free_kc"])
        records = []
        timeline = []
        guard = 0
        while len(order) < len(pool) and guard < 200:
            guard += 1
            nxt = [t for t in targets if t > len(order)]
            stop = min(nxt) if nxt else len(pool)
            while len(order) < stop:
                x_rig = adaptive.rig_vector(res)
                cov = adaptive.rig_covariance(res)
                free = adaptive.free_rig_mask(res)
                cands = [v for v in pool if v not in order and poses.get(v) is not None]
                search = _diverse_actual(cands, order, poses)
                best = None
                for v in search:
                    p = poses[v]
                    J = adaptive.candidate_jacobian(x_rig, p["R"], p["t"],
                                                    job["pitch"])
                    if J is None:
                        continue
                    gain, _ = adaptive.information_gain(cov, J, free)
                    if best is None or gain > best[0]:
                        best = (gain, v)
                if best is None:
                    rest = [v for v in pool if v not in order]
                    if not rest:
                        break
                    v = rest[0]
                    gain = float("nan")
                else:
                    gain, v = best
                order.append(int(v))
                timeline.append({"step": len(order), "view": int(v),
                                 "gain": float(gain) if _finite(gain) else None,
                                 "z": poses[v]["z"] if poses.get(v) else None})
            res, dt = fit_calibration(_obs_for(job, order), job["pitch"],
                                      data.init, job["free_kc"])
            if len(order) in targets:
                records.append(_build_record(job, list(order), res, dt,
                                             job.get("method", "adaptive"), len(order)))
        return {"job_key": job["key"], "records": records, "error": None,
                "selection": {"order": order, "timeline": timeline}}
    except Exception:
        return {"job_key": job.get("key"), "records": [],
                "error": traceback.format_exc()}


def _planning_extr(data) -> dict:
    """Extrinsics used only to convert PnP poses into the left frame.

    Synthetic candidate/validation views all have left detections, so this is
    never consulted there (identity is returned, not the ground truth, to
    avoid leaking the synthetic rig into selection).  Real views without a
    left detection use the shipped factory CCF as the initialisation prior.
    """
    if isinstance(data, Scene):
        return {"left": (np.eye(3), np.zeros(3)),
                "right": (np.eye(3), np.zeros(3)),
                "tex": (np.eye(3), np.zeros(3))}
    return {"left": (np.eye(3), np.zeros(3)),
            "right": data.ref["ir"], "tex": data.ref["rgb"]}


def run_job(job: dict) -> dict:
    if job.get("kind") == "adaptive":
        return run_adaptive_job(job)
    return run_single_job(job)


def _init_worker(scenes, real):
    _G["scenes"] = scenes
    _G["real"] = real


# ==========================================================================
# job construction
# ==========================================================================

def _job(domain: str, kind: str, *, scene=None, fold=None, method="single",
         k=0, seed=None, view_ids=(), holdout=(), pitch=0.0, free_kc=False,
         pool_size=None, leakage="train_only", tags=None, pool=None,
         k_targets=(), seed_n=5) -> dict:
    j = {"domain": domain, "kind": kind, "scene": scene, "fold": fold,
         "method": method, "k": int(k), "seed": seed,
         "view_ids": [int(v) for v in view_ids],
         "holdout": [int(v) for v in holdout],
         "pitch": float(pitch), "free_kc": bool(free_kc),
         "pool_size": pool_size, "leakage": leakage, "tags": tags or {}}
    if kind == "adaptive":
        j["pool"] = [int(v) for v in pool]
        j["k_targets"] = [int(x) for x in k_targets]
        j["seed_n"] = int(seed_n)
    return j


def build_jobs(cfg: dict, scenes: list[Scene], real: RealData) -> list[dict]:
    P = plate.DEFAULT_PITCH_MM
    kk = [k for k in cfg["k_values"] if 8 <= k <= 25]
    jobs = []
    for si, scene in enumerate(scenes):
        pool = scene.candidate_ids
        kk_s = [k for k in kk if k <= len(pool)]
        jobs.append(_job("synthetic", "single", scene=si, method="full",
                         k=len(pool), view_ids=pool, holdout=scene.validation_ids,
                         pitch=P, free_kc=cfg["free_kc"], pool_size=len(pool)))
        for method, fn in (("fixed", fixed_subset), ("uniform", uniform_subset)):
            for k in kk_s:
                if k == len(pool):
                    continue
                jobs.append(_job("synthetic", "single", scene=si, method=method,
                                 k=k, view_ids=fn(pool, k), holdout=scene.validation_ids,
                                 pitch=P, free_kc=cfg["free_kc"], pool_size=len(pool)))
        for s in range(int(cfg["seeds"])):
            order = random_subset_order(pool, s + 1)
            for k in kk_s:
                if k == len(pool):
                    continue
                jobs.append(_job("synthetic", "single", scene=si, method="random",
                                 k=k, seed=s + 1, view_ids=order[:k],
                                 holdout=scene.validation_ids, pitch=P,
                                 free_kc=cfg["free_kc"], pool_size=len(pool)))
        jobs.append(_job("synthetic", "adaptive", scene=si, method="adaptive",
                         k=0, pool=pool, k_targets=[k for k in kk_s if k < len(pool)],
                         holdout=scene.validation_ids, pitch=P,
                         free_kc=cfg["free_kc"], pool_size=len(pool)))
        if cfg.get("oracle", True):
            for oi, order in enumerate(gt_fisher_orders(scene, cfg["free_kc"])):
                for k in kk_s:
                    if k == len(pool):
                        continue
                    jobs.append(_job("synthetic", "single", scene=si,
                                     method="gt_fisher", k=k, seed=oi,
                                     view_ids=order[:k], holdout=scene.validation_ids,
                                     pitch=P, free_kc=cfg["free_kc"],
                                     pool_size=len(pool),
                                     tags={"ordering": oi}))

    for f in range(int(cfg["folds"])):
        pool = real.fold_train.get(f, [])
        hold = real.fold_holdout.get(f, [])
        if not pool:
            continue
        kk_f = [k for k in kk if k <= len(pool)]
        jobs.append(_job("real", "single", fold=f, method="full", k=len(pool),
                         view_ids=pool, holdout=hold, pitch=P,
                         free_kc=cfg["free_kc"], pool_size=len(pool)))
        for method, fn in (("fixed", fixed_subset), ("uniform", uniform_subset)):
            for k in kk_f:
                if k == len(pool):
                    continue
                jobs.append(_job("real", "single", fold=f, method=method, k=k,
                                 view_ids=fn(pool, k), holdout=hold, pitch=P,
                                 free_kc=cfg["free_kc"], pool_size=len(pool)))
        for s in range(int(cfg["seeds"])):
            order = random_subset_order(pool, s + 1)
            for k in kk_f:
                if k == len(pool):
                    continue
                jobs.append(_job("real", "single", fold=f, method="random", k=k,
                                 seed=s + 1, view_ids=order[:k], holdout=hold,
                                 pitch=P, free_kc=cfg["free_kc"], pool_size=len(pool)))
        jobs.append(_job("real", "adaptive", fold=f, method="adaptive", k=0,
                         pool=pool, k_targets=[k for k in kk_f if k < len(pool)],
                         holdout=hold, pitch=P, free_kc=cfg["free_kc"],
                         pool_size=len(pool)))

    # --- real all-25 and leave-one-view-out (independent at the view level) ---
    allv = list(real.view_ids)
    jobs.append(_job("real", "single", method="full25", k=25, view_ids=allv,
                     holdout=[], pitch=P, free_kc=cfg["free_kc"], pool_size=25,
                     leakage="all_views"))
    if cfg.get("loo", True):
        for v in allv:
            jobs.append(_job("real", "single", method="loo", k=25,
                             view_ids=[x for x in allv if x != v], holdout=[v],
                             pitch=P, free_kc=cfg["free_kc"], pool_size=25,
                             leakage="loo"))

    # --- real EXStar acquisition-order prefixes on all 25 (train-only,
    #     explicitly NOT the 20-view fold held-out design) ---
    for k in (8, 12, 20):
        jobs.append(_job("real", "single", method="exstar_prefix", k=k,
                         view_ids=allv[:k], holdout=[], pitch=P,
                         free_kc=cfg["free_kc"], pool_size=25,
                         leakage="prefix_no_holdout",
                         tags={"note": "prefix on all 25; not fold held-out"}))

    # --- plate pitch sensitivity ---
    for scale in cfg.get("pitch_scales", ()):
        jobs.append(_job("real", "single", method="pitch", k=25, view_ids=allv,
                         holdout=[], pitch=P * scale, free_kc=cfg["free_kc"],
                         pool_size=25, leakage="all_views",
                         tags={"pitch_scale": float(scale), "domain": "real"}))
        for si in range(min(int(cfg.get("pitch_scenes", 0)), len(scenes))):
            scene = scenes[si]
            jobs.append(_job("synthetic", "single", scene=si, method="pitch",
                             k=20, view_ids=scene.candidate_ids[:20],
                             holdout=scene.validation_ids, pitch=P * scale,
                             free_kc=cfg["free_kc"], pool_size=25,
                             tags={"pitch_scale": float(scale),
                                   "domain": "synthetic"}))

    # --- free-distortion sensitivity ---
    if cfg.get("freekc", True):
        jobs.append(_job("real", "single", method="freekc", k=25, view_ids=allv,
                         holdout=[], pitch=P, free_kc=True, pool_size=25,
                         leakage="all_views", tags={"reference": "full25"}))
        if 0 in real.fold_train:
            jobs.append(_job("real", "single", method="freekc", k=20,
                             view_ids=real.fold_train[0], holdout=real.fold_holdout[0],
                             pitch=P, free_kc=True, pool_size=20,
                             tags={"reference": "fold0_20"}))
        for si in range(min(int(cfg.get("freekc_scenes", 0)), len(scenes))):
            for k in (8, 12, 20):
                jobs.append(_job("synthetic", "single", scene=si, method="freekc",
                                 k=k, view_ids=scenes[si].candidate_ids[:k],
                                 holdout=scenes[si].validation_ids, pitch=P,
                                 free_kc=True, pool_size=25,
                                 tags={"reference": f"fixed_k{k}"}))
    for j in jobs:
        j["key"] = job_key(j)
    return jobs


def job_key(job: dict) -> str:
    payload = {k: v for k, v in job.items() if k != "key"}
    blob = json.dumps(payload, sort_keys=True, default=_json_default).encode()
    h = hashlib.sha1(blob)
    h.update(engine()["engine_hash"].encode())
    return h.hexdigest()[:20]


def load_cache(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        d = json.loads(path.read_text())
    except Exception:
        return {}
    if d.get("engine_hash") != engine()["engine_hash"]:
        return {}
    return d.get("jobs", {})


def save_cache(path: Path, cache: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"engine_hash": engine()["engine_hash"],
                               "jobs": cache}, default=_json_default))
    tmp.replace(path)


def execute_jobs(jobs: list[dict], cache: dict, workers: int,
                 scenes: list[Scene], real: RealData, progress: bool = True,
                 cache_path: Optional[Path] = None) -> dict:
    todo = [j for j in jobs if j["key"] not in cache]
    if progress:
        print(f"[viewcount] jobs: {len(jobs)} total, {len(jobs) - len(todo)} cached, "
              f"{len(todo)} to run with {workers} worker(s)", flush=True)
    if not todo:
        return cache
    _G["scenes"] = {i: s for i, s in enumerate(scenes)}
    _G["real"] = real
    t0 = time.time()
    done = 0
    if workers <= 1:
        for j in todo:
            r = run_job(j)
            cache[j["key"]] = r
            done += 1
            if progress and done % 20 == 0:
                print(f"[viewcount] {done}/{len(todo)} jobs, {time.time() - t0:.0f}s",
                      flush=True)
            if cache_path is not None and done % 25 == 0:
                save_cache(cache_path, cache)
    else:
        ctx = mp.get_context("fork")
        with ctx.Pool(processes=workers) as pool:
            for r in pool.imap_unordered(run_job, todo, chunksize=1):
                cache[r.get("job_key") or ""] = r
                done += 1
                if progress and done % 25 == 0:
                    print(f"[viewcount] {done}/{len(todo)} jobs, {time.time() - t0:.0f}s",
                          flush=True)
                if cache_path is not None and done % 25 == 0:
                    save_cache(cache_path, cache)
    return cache


# ==========================================================================
# aggregation / statistics
# ==========================================================================

DEPLOYABLE = ("fixed", "uniform", "random", "adaptive")
METHODS = ("fixed", "uniform", "random", "adaptive", "gt_fisher")
K_SYNTH_POOL = 25
K_REAL_POOL = 20


def _f(x):
    try:
        v = float(x)
        return v if math.isfinite(v) else float("nan")
    except Exception:
        return float("nan")


def synth_metric(r: dict, name: str) -> float:
    try:
        g = r["gt"]
        if name == "fx_abs_max":
            return _f(g["fx_abs_max"])
        if name == "fx_abs_ir_max":
            return _f(g["fx_abs_ir_max"])
        if name == "fx_rel_max":
            return _f(g["fx_rel_max"])
        if name == "baseline_err_mm":
            return _f(g["baseline_err_mm"])
        if name == "baseline_abs_mm":
            return abs(_f(g["baseline_err_mm"]))
        if name == "ir_rot_err_deg":
            return _f(g["ir_rot_err_deg"])
        if name == "ir_trans_err_mm":
            return _f(g["ir_trans_err_mm"])
        if name == "rgb_rot_err_deg":
            return _f(g["rgb_rot_err_deg"])
        if name == "rgb_trans_err_mm":
            return _f(g["rgb_trans_err_mm"])
        if name == "rgb_pixel_px":
            return _f(g["pixel"]["median_px"])
        if name == "rgb_pixel_p95_px":
            return _f(g["pixel"]["p95_px"])
        if name == "nees_n":
            return _f(g["nees"]["nees_n"])
        if name == "nees_rig":
            return _f(g["nees"]["nees_rig"])
        if name == "cov68":
            return _f(g["nees"]["cov68"])
        if name == "cov95":
            return _f(g["nees"]["cov95"])
        if name == "nees_pass":
            return 1.0 if g["nees"].get("nees_pass") else 0.0
        if name == "z_baseline":
            return _f(g["nees"]["z_baseline"])
        if name == "heldout_rt_mean":
            return _f(r["heldout"].get("heldout_rt_mean"))
        if name == "heldout_rt_robust_mean":
            return _f(r["heldout"].get("heldout_rt_robust_mean"))
        if name == "heldout_rt_pooled":
            return _f(r["heldout"].get("heldout_rt_pooled"))
        if name == "heldout_left_mean":
            return _f(r["heldout"].get("heldout_left_mean"))
        if name == "epipolar_median_px":
            return _f(r["epipolar"].get("median_px"))
        if name == "epipolar_rms_px":
            return _f(r["epipolar"].get("rms_px"))
        if name == "rms_train_px":
            return _f(r["rms_train_px"])
        if name == "time_s":
            return _f(r["time_s"])
        if name == "sigma_fx_max":
            return max(_f(r["sigma"].get(f"{c}.fx")) for c in CAMERAS)
        if name == "sigma_baseline_mm":
            return _f(r.get("sigma_baseline_mm"))
        if name == "sigma_rgb_pixel_px":
            return _f(r.get("sigma_rgb_pixel_px"))
        if name == "distance_bins":
            return _f(r["diversity"]["distance_bins"])
        if name == "cell_count":
            return _f(r["diversity"]["cell_count"])
        if name == "quadrant_fraction_min":
            return _f(r["diversity"]["quadrant_fraction_min"])
        if name == "n_views":
            return _f(r["n_views"])
    except Exception:
        return float("nan")
    return float("nan")


def real_metric(r: dict, name: str) -> float:
    try:
        c = r["ccf"]
        if name == "dfx_max_abs":
            return _f(c["dfx_max_abs"])
        if name == "dcx_max_abs":
            return _f(c["dcx_max_abs"])
        if name == "baseline_vs_ccf_mm":
            return _f(c["baseline_vs_ccf_mm"])
        if name == "baseline_ccf_abs":
            return abs(_f(c["baseline_vs_ccf_mm"]))
        if name == "ir_rot_vs_ccf_deg":
            return _f(c["ir_rot_vs_ccf_deg"])
        if name == "ir_trans_vs_ccf_mm":
            return _f(c["ir_trans_vs_ccf_mm"])
        if name == "rgb_rot_vs_ccf_deg":
            return _f(c["rgb_rot_vs_ccf_deg"])
        if name == "rgb_trans_vs_ccf_mm":
            return _f(c["rgb_trans_vs_ccf_mm"])
        if name == "rgb_pixel_vs_ccf_px":
            return _f(c["rgb_pixel_median_px"])
        if name == "dfx_rel_max":
            return max(abs(_f(x)) for x in c["dfx_rel"])
        if name == "heldout_rt_mean":
            return _f(r["heldout"].get("heldout_rt_mean"))
        if name == "heldout_rt_robust_mean":
            return _f(r["heldout"].get("heldout_rt_robust_mean"))
        if name == "heldout_rt_pooled":
            return _f(r["heldout"].get("heldout_rt_pooled"))
        if name == "heldout_left_mean":
            return _f(r["heldout"].get("heldout_left_mean"))
        if name == "epipolar_median_px":
            return _f(r["epipolar"].get("median_px"))
        if name == "epipolar_rms_px":
            return _f(r["epipolar"].get("rms_px"))
        if name == "rms_train_px":
            return _f(r["rms_train_px"])
        if name == "time_s":
            return _f(r["time_s"])
        if name == "sigma_fx_max":
            return max(_f(r["sigma"].get(f"{c}.fx")) for c in CAMERAS)
        if name == "sigma_baseline_mm":
            return _f(r.get("sigma_baseline_mm"))
        if name == "sigma_rgb_pixel_px":
            return _f(r.get("sigma_rgb_pixel_px"))
        if name == "distance_bins":
            return _f(r["diversity"]["distance_bins"])
        if name == "cell_count":
            return _f(r["diversity"]["cell_count"])
        if name == "quadrant_fraction_min":
            return _f(r["diversity"]["quadrant_fraction_min"])
        if name == "n_views":
            return _f(r["n_views"])
    except Exception:
        return float("nan")
    return float("nan")


def _stats(values: Iterable[float]) -> dict:
    v = np.asarray([float(x) for x in values if _finite(x)], float)
    if v.size == 0:
        return {"n": 0, "mean": None, "median": None, "q25": None, "q75": None,
                "sd": None, "min": None, "max": None, "ucb_mean": None,
                "lcb_mean": None, "ucb_p90": None}
    ci = _bootstrap_ci(v, np.mean)
    return {"n": int(v.size), "mean": float(np.mean(v)),
            "median": float(np.median(v)),
            "q25": float(np.percentile(v, 25)),
            "q75": float(np.percentile(v, 75)),
            "sd": float(np.std(v, ddof=1)) if v.size > 1 else 0.0,
            "min": float(np.min(v)), "max": float(np.max(v)),
            "ucb_mean": ci.get("ucb"), "lcb_mean": ci.get("lcb"),
            "ucb_p90": _percentile_ucb(v, 90.0)}


def _method_records(recs: list[dict], domain: str, method: str, k: int,
                    pool: int) -> list[dict]:
    m = method
    if method in DEPLOYABLE and k == pool:
        m = "full"
    return [r for r in recs if r["domain"] == domain and r["method"] == m
            and int(r["k"]) == int(k)]


def _per_scene(rs: list[dict], metric: str, domain: str) -> dict:
    fn = synth_metric if domain == "synthetic" else real_metric
    groups: dict = {}
    for r in rs:
        groups.setdefault(int(r.get("scene") if r.get("scene") is not None
                              else r.get("fold")), []).append(fn(r, metric))
    out = {}
    for sid, vals in groups.items():
        vv = [v for v in vals if _finite(v)]
        if vv:
            out[sid] = float(np.median(vv))
    return out


def _per_fold(rs: list[dict], metric: str) -> dict:
    groups: dict = {}
    for r in rs:
        groups.setdefault(int(r["fold"]), []).append(real_metric(r, metric))
    out = {}
    for f, vals in groups.items():
        vv = [v for v in vals if _finite(v)]
        if vv:
            out[f] = float(np.median(vv))
    return out


def _curve(recs: list[dict], domain: str, method: str, k: int, metric: str,
           pool: int) -> dict:
    rs = _method_records(recs, domain, method, k, pool)
    if domain == "synthetic":
        return _per_scene(rs, metric, domain)
    return _per_fold(rs, metric)


def _oracle_curve(recs: list[dict], k: int, metric: str, pool: int) -> dict:
    """Per-scene minimum over every evaluated subset (diagnostic upper bound)."""
    out: dict = {}
    by_scene: dict = {}
    for m in METHODS:
        rs = _method_records(recs, "synthetic", m, k, pool)
        for r in rs:
            by_scene.setdefault(int(r["scene"]), []).append(synth_metric(r, metric))
    for sid, vals in by_scene.items():
        vv = [v for v in vals if _finite(v)]
        if vv:
            out[sid] = float(np.min(vv))
    return out


SYNTH_SUMMARY_METRICS = (
    "fx_abs_max", "fx_abs_ir_max", "fx_rel_max", "baseline_abs_mm",
    "ir_rot_err_deg", "ir_trans_err_mm", "rgb_rot_err_deg",
    "rgb_trans_err_mm", "rgb_pixel_px", "rgb_pixel_p95_px",
    "heldout_rt_mean", "heldout_rt_robust_mean", "heldout_rt_pooled",
    "heldout_left_mean", "epipolar_median_px", "epipolar_rms_px",
    "nees_n", "cov68", "cov95", "nees_pass", "z_baseline",
    "rms_train_px", "sigma_fx_max", "sigma_baseline_mm",
    "sigma_rgb_pixel_px", "distance_bins", "cell_count",
    "quadrant_fraction_min", "time_s",
)
REAL_SUMMARY_METRICS = (
    "dfx_max_abs", "dcx_max_abs", "dfx_rel_max", "baseline_vs_ccf_mm",
    "baseline_ccf_abs", "ir_rot_vs_ccf_deg", "ir_trans_vs_ccf_mm",
    "rgb_rot_vs_ccf_deg", "rgb_trans_vs_ccf_mm", "rgb_pixel_vs_ccf_px",
    "heldout_rt_mean", "heldout_rt_robust_mean", "heldout_rt_pooled",
    "heldout_left_mean", "epipolar_median_px", "epipolar_rms_px",
    "rms_train_px", "sigma_fx_max", "sigma_baseline_mm",
    "sigma_rgb_pixel_px", "distance_bins", "cell_count",
    "quadrant_fraction_min", "time_s",
)


def _real_pooled_views(recs: list[dict], method: str, k: int, pool: int) -> list[float]:
    rs = _method_records(recs, "real", method, k, pool)
    by_fold: dict = {}
    for r in rs:
        by_fold.setdefault(int(r["fold"]), []).append(r)
    vals = []
    for f, group in by_fold.items():
        # per-view median across seeds, then pooled across views and folds
        per_view: dict = {}
        for r in group:
            for p in r["heldout"].get("per_view", []):
                if _finite(p.get("rt_rms_px")):
                    per_view.setdefault(int(p["view"]), []).append(float(p["rt_rms_px"]))
        for v, vv in per_view.items():
            vals.append(float(np.median(vv)))
    return vals


def aggregate(recs: list[dict], cfg: dict, engine_info: dict,
              real: RealData, scenes: list[Scene]) -> dict:
    t0 = time.time()
    kk = [k for k in cfg["k_values"] if 8 <= k <= 25]
    synth: dict = {}
    for method in METHODS + ("full",):
        per_k = {}
        for k in kk:
            if method == "full" and k != K_SYNTH_POOL:
                continue
            block = {}
            for metric in SYNTH_SUMMARY_METRICS:
                vals = _curve(recs, "synthetic", method, k, metric, K_SYNTH_POOL)
                block[metric] = _stats(vals.values())
            per_k[str(k)] = block
        synth[method] = per_k
    oracle: dict = {}
    for k in kk:
        block = {}
        for metric in SYNTH_SUMMARY_METRICS:
            block[metric] = _stats(_oracle_curve(recs, k, metric, K_SYNTH_POOL).values())
        oracle[str(k)] = block

    real_sum: dict = {}
    for method in DEPLOYABLE + ("full25", "exstar_prefix", "loo"):
        per_k = {}
        if method in DEPLOYABLE:
            kset = [k for k in kk if k <= K_REAL_POOL]
        elif method == "full25":
            kset = [25]
        elif method == "exstar_prefix":
            kset = [8, 12, 20]
        else:
            kset = [25]
        for k in kset:
            block = {}
            for metric in REAL_SUMMARY_METRICS:
                if method in DEPLOYABLE:
                    vals = _curve(recs, "real", method, k, metric, K_REAL_POOL)
                elif method == "full25":
                    vals = [real_metric(r, metric) for r in recs
                            if r["domain"] == "real" and r["method"] == "full25"]
                elif method == "exstar_prefix":
                    vals = [real_metric(r, metric) for r in recs
                            if r["domain"] == "real" and r["method"] == "exstar_prefix"
                            and int(r["k"]) == k]
                else:
                    vals = [real_metric(r, metric) for r in recs
                            if r["domain"] == "real" and r["method"] == "loo"]
                block[metric] = _stats(vals.values() if isinstance(vals, dict) else vals)
            if method in DEPLOYABLE:
                block["heldout_pooled_views"] = _stats(
                    _real_pooled_views(recs, method, k, K_REAL_POOL))
            if method == "loo":
                loo = [r for r in recs if r["domain"] == "real" and r["method"] == "loo"]
                block["heldout_pooled_views"] = _stats(
                    [float(p["rt_rms_px"]) for r in loo
                     for p in r["heldout"].get("per_view", [])
                     if _finite(p.get("rt_rms_px"))])
                block["epipolar_pooled"] = _stats(
                    [real_metric(r, "epipolar_median_px") for r in loo])
            per_k[str(k)] = block
        real_sum[method] = per_k

    # ---------------- minimum reliable count ----------------
    min_k: dict = {}
    for method in DEPLOYABLE + ("gt_fisher", "oracle"):
        entries = []
        for metric, target, label in TARGETS:
            curve = synth.get(method, {}) if method != "oracle" else oracle
            rows = []
            k_mean = k_ucb = k_p90 = None
            for k in sorted(kk):
                block = curve.get(str(k), {}).get(metric)
                if not block:
                    continue
                m = block.get("mean")
                u = block.get("ucb_mean")
                p = block.get("ucb_p90")
                rows.append({"k": k, "mean": m, "ucb_mean": u, "ucb_p90": p,
                             "target": target,
                             "pass_mean": bool(m is not None and m <= target),
                             "pass_ucb": bool(u is not None and u <= target),
                             "pass_ucb_p90": bool(p is not None and p <= target)})
                if k_mean is None and m is not None and m <= target:
                    k_mean = k
                if k_ucb is None and u is not None and u <= target:
                    k_ucb = k
                if k_p90 is None and p is not None and p <= target:
                    k_p90 = k
            entries.append({"metric": metric, "target": target, "label": label,
                            "k_mean": k_mean, "k_ucb": k_ucb, "k_ucb_p90": k_p90,
                            "curve": rows})
        min_k[method] = entries

    # ---------------- real consistency ----------------
    real_cons: dict = {}
    for method in DEPLOYABLE:
        rows = []
        for k in [x for x in kk if x <= K_REAL_POOL]:
            pooled = _real_pooled_views(recs, method, k, K_REAL_POOL)
            st = _stats(pooled)
            held_full = _per_fold(_method_records(recs, "real", method, k, K_REAL_POOL),
                                  "heldout_rt_mean")
            # drift vs the fold's full 20-view fit (same estimator, no held-out leakage)
            full = _per_fold(_method_records(recs, "real", method, K_REAL_POOL,
                                             K_REAL_POOL), "dfx_max_abs")
            base = _per_fold(_method_records(recs, "real", method, k, K_REAL_POOL),
                             "baseline_vs_ccf_mm")
            base_full = _per_fold(_method_records(recs, "real", method, K_REAL_POOL,
                                                  K_REAL_POOL), "baseline_vs_ccf_mm")
            fx_drift, base_drift = {}, {}
            for f in base:
                if f in base_full and _finite(base[f]) and _finite(base_full[f]):
                    base_drift[f] = abs(base[f] - base_full[f])
            # fx drift needs per-camera values; recompute from records
            recs_k = _method_records(recs, "real", method, k, K_REAL_POOL)
            recs_full = _method_records(recs, "real", method, K_REAL_POOL, K_REAL_POOL)
            for f in range(int(cfg["folds"])):
                rk = [r for r in recs_k if int(r["fold"]) == f]
                rf = [r for r in recs_full if int(r["fold"]) == f]
                if not rk or not rf:
                    continue
                fx_k = np.median([[r["cameras"][c]["fx"] for c in CAMERAS] for r in rk], axis=0)
                fx_f = np.median([[r["cameras"][c]["fx"] for c in CAMERAS] for r in rf], axis=0)
                fx_drift[f] = float(np.max(np.abs(fx_k - fx_f)))
            fd = _stats(fx_drift.values())
            bd = _stats(base_drift.values())
            pass_held = bool(st.get("ucb_mean") is not None and st["ucb_mean"] <= 0.5)
            pass_fx = bool(fd.get("median") is not None and fd["median"] <= 1.0)
            pass_base = bool(bd.get("median") is not None and bd["median"] <= 0.10)
            rows.append({"k": k, "heldout_ucb": st.get("ucb_mean"),
                         "heldout_mean": st.get("mean"), "heldout_n": st.get("n"),
                         "fx_drift_median": fd.get("median"),
                         "fx_drift_max": fd.get("max"),
                         "baseline_drift_median": bd.get("median"),
                         "baseline_drift_max": bd.get("max"),
                         "pass_heldout": pass_held, "pass_fx": pass_fx,
                         "pass_baseline": pass_base,
                         "pass_all": bool(pass_held and pass_fx and pass_base)})
        real_cons[method] = rows
    k_real = {m: next((r["k"] for r in rows if r["pass_all"]), None)
              for m, rows in real_cons.items()}

    # combined recommendation per target/method
    combined = {}
    for method in DEPLOYABLE:
        out = []
        for e in min_k.get(method, []):
            k_syn = e["k_ucb"]
            k_comb = None
            if k_syn is not None:
                k_comb = next((r["k"] for r in real_cons[method]
                               if r["k"] >= k_syn and r["pass_all"]), None)
            out.append({"metric": e["metric"], "target": e["target"],
                        "k_synth_ucb": k_syn, "k_combined": k_comb,
                        "label": e["label"]})
        combined[method] = out

    # ---------------- paired comparisons ----------------
    paired = []
    for k in kk:
        for a, b in (("adaptive", "fixed"), ("uniform", "fixed"),
                     ("adaptive", "random"), ("fixed", "random")):
            for metric in ("fx_abs_max", "baseline_abs_mm", "heldout_rt_mean"):
                va = _per_scene(_method_records(recs, "synthetic", a, k, K_SYNTH_POOL),
                                metric, "synthetic")
                vb = _per_scene(_method_records(recs, "synthetic", b, k, K_SYNTH_POOL),
                                metric, "synthetic")
                common = sorted(set(va) & set(vb))
                if not common:
                    continue
                d = np.array([va[s] - vb[s] for s in common], float)
                ci = _bootstrap_ci(d, np.mean)
                paired.append({"k": k, "a": a, "b": b, "metric": metric,
                               "n": len(d), "mean_diff": float(np.mean(d)),
                               "median_diff": float(np.median(d)),
                               "ci_lo": ci["ci_lo"], "ci_hi": ci["ci_hi"],
                               "win_fraction": float(np.mean(d < 0)),
                               "p_wilcoxon": _wilcoxon([va[s] for s in common],
                                                       [vb[s] for s in common])})

    # ---------------- sigma calibration ----------------
    sigma_cal: dict = {"synthetic": [], "real": []}
    for method in DEPLOYABLE:
        for k in kk:
            rs = _method_records(recs, "synthetic", method, k, K_SYNTH_POOL)
            if not rs:
                continue
            rows = {}
            for c in CAMERAS:
                sg = [r["sigma"].get(f"{c}.fx") for r in rs
                      if _finite(r["sigma"].get(f"{c}.fx"))]
                if method == "random":
                    by_scene = {}
                    for r in rs:
                        by_scene.setdefault(r.get("scene"), []).append(
                            r["cameras"][c]["fx"])
                    sds = [float(np.std(v, ddof=1)) for v in by_scene.values()
                           if len(v) > 1]
                    emp = float(np.mean(sds)) if sds else float("nan")
                else:
                    fx = [r["cameras"][c]["fx"] for r in rs]
                    emp = float(np.std(fx, ddof=1)) if len(fx) > 1 else float("nan")
                if _finite(emp) and sg:
                    rows[c] = {"emp_sd": emp,
                               "mean_sigma": float(np.mean(sg)),
                               "ratio": float(emp / np.mean(sg))}
            sigma_cal["synthetic"].append({"method": method, "k": k, "fx": rows})
    # real: seed-to-seed spread vs reported sigma (random subsets only)
    covs = {"z68": [], "z95": []}
    for f in range(int(cfg["folds"])):
        for k in [x for x in kk if x <= K_REAL_POOL]:
            rs = [r for r in recs if r["domain"] == "real" and r["method"] == "random"
                  and int(r["k"]) == k and int(r["fold"]) == f]
            if len(rs) < 2:
                continue
            for c in CAMERAS:
                fx = np.array([r["cameras"][c]["fx"] for r in rs], float)
                sg = np.array([_f(r["sigma"].get(f"{c}.fx")) for r in rs])
                good = sg > 0
                if good.sum() < 2:
                    continue
                for i in range(len(fx)):
                    others = np.delete(fx, i)
                    loo = float(np.mean(others))
                    z = (fx[i] - loo) / sg[i]
                    covs["z68"].append(abs(z) <= 1.0)
                    covs["z95"].append(abs(z) <= 1.96)
                sigma_cal["real"].append({
                    "fold": f, "k": k, "camera": c,
                    "emp_sd": float(np.std(fx, ddof=1)),
                    "mean_sigma": float(np.mean(sg[good])),
                    "ratio": float(np.std(fx, ddof=1) / np.mean(sg[good]))})
    sigma_cal["real_coverage"] = {
        "n": len(covs["z68"]),
        "cov68": float(np.mean(covs["z68"])) if covs["z68"] else None,
        "cov95": float(np.mean(covs["z95"])) if covs["z95"] else None,
    }

    # ---------------- pitch sensitivity ----------------
    pitch_rows = []
    for domain in ("synthetic", "real"):
        rs = [r for r in recs if r["method"] == "pitch" and r["domain"] == domain]
        groups: dict = {}
        for r in rs:
            key = (r.get("scene"), r.get("fold"), r["tags"].get("domain", domain))
            groups.setdefault(key, []).append(r)
        for key, group in groups.items():
            base = next((r for r in group if abs(float(r["pitch_mm"]) /
                                                 plate.DEFAULT_PITCH_MM - 1.0) < 1e-12), None)
            if base is None:
                continue
            for r in sorted(group, key=lambda z: z["pitch_mm"]):
                scale = float(r["pitch_mm"]) / plate.DEFAULT_PITCH_MM
                fx_shift = max(abs(r["cameras"][c]["fx"] - base["cameras"][c]["fx"])
                               for c in CAMERAS)
                cx_shift = max(abs(r["cameras"][c]["cx"] - base["cameras"][c]["cx"])
                               for c in CAMERAS)
                row = {"domain": domain, "scene": r.get("scene"), "fold": r.get("fold"),
                       "scale": scale, "baseline_mm": r["baseline_mm"],
                       "baseline_ratio": r["baseline_mm"] / base["baseline_mm"],
                       "fx_shift_px": fx_shift, "cx_shift_px": cx_shift,
                       "rms_train_px": r["rms_train_px"],
                       "ir_rot_shift_deg": abs(r["ir_rot_deg"] - base["ir_rot_deg"]),
                       "rgb_rot_shift_deg": abs(r["rgb_rot_deg"] - base["rgb_rot_deg"]),
                       "sigma_fx_max": max(_f(r["sigma"].get(f"{c}.fx")) for c in CAMERAS)}
                if domain == "real":
                    row["baseline_vs_ccf_mm"] = r["ccf"]["baseline_vs_ccf_mm"]
                    row["dfx_vs_ccf_max"] = r["ccf"]["dfx_max_abs"]
                else:
                    row["baseline_err_mm"] = r["gt"]["baseline_err_mm"]
                    row["fx_abs_max"] = r["gt"]["fx_abs_max"]
                pitch_rows.append(row)

    # ---------------- free-kc sensitivity ----------------
    freekc_rows = []
    for r in [x for x in recs if x["method"] == "freekc"]:
        ref = None
        for z in recs:
            if z["domain"] != r["domain"] or z["method"] not in ("full25", "full",
                                                                  "fixed", "fold0"):
                continue
            if z["domain"] == "synthetic" and z.get("scene") != r.get("scene"):
                continue
            if z["domain"] == "real":
                if r.get("fold") is None and z["method"] != "full25":
                    continue
                if r.get("fold") == 0 and z["method"] not in ("full", "fold0", "full25"):
                    continue
            if z["method"] == "fixed" and int(z["k"]) != int(r["k"]):
                continue
            if z["domain"] == "synthetic" and z["method"] == "fixed" \
                    and z["view_ids"] == r["view_ids"]:
                ref = z
                break
            if z["domain"] == "real" and r.get("fold") is None and z["method"] == "full25":
                ref = z
                break
            if z["domain"] == "real" and r.get("fold") == 0 and z["method"] == "full" \
                    and int(z["k"]) == 20:
                ref = z
                break
        row = {"domain": r["domain"], "scene": r.get("scene"), "fold": r.get("fold"),
               "k": r["k"], "view_ids": r["view_ids"],
               "rms_train_px": r["rms_train_px"],
               "sigma_fx_max": max(_f(r["sigma"].get(f"{c}.fx")) for c in CAMERAS),
               "sigma_baseline_mm": r.get("sigma_baseline_mm"),
               "baseline_mm": r["baseline_mm"],
               "n_free": r["fit"]["n_free"], "condition": r["fit"]["condition"]}
        if r["domain"] == "synthetic":
            row.update({"fx_abs_max": r["gt"]["fx_abs_max"],
                        "baseline_abs_mm": abs(r["gt"]["baseline_err_mm"]),
                        "nees_n": r["gt"]["nees"]["nees_n"]})
        else:
            row.update({"dfx_max_abs": r["ccf"]["dfx_max_abs"],
                        "baseline_vs_ccf_mm": r["ccf"]["baseline_vs_ccf_mm"],
                        "heldout_rt_mean": r["heldout"].get("heldout_rt_mean")})
        if ref is not None:
            row["fixed_kc"] = {
                "rms_train_px": ref["rms_train_px"],
                "sigma_fx_max": max(_f(ref["sigma"].get(f"{c}.fx")) for c in CAMERAS),
                "baseline_mm": ref["baseline_mm"],
            }
            if r["domain"] == "synthetic":
                row["fixed_kc"].update({"fx_abs_max": ref["gt"]["fx_abs_max"],
                                        "baseline_abs_mm": abs(ref["gt"]["baseline_err_mm"]),
                                        "nees_n": ref["gt"]["nees"]["nees_n"]})
            else:
                row["fixed_kc"].update({"dfx_max_abs": ref["ccf"]["dfx_max_abs"],
                                        "baseline_vs_ccf_mm": ref["ccf"]["baseline_vs_ccf_mm"],
                                        "heldout_rt_mean": ref["heldout"].get("heldout_rt_mean")})
        freekc_rows.append(row)

    # ---------------- depth sigma ----------------
    depth = {}
    for domain, pool in (("synthetic", K_SYNTH_POOL), ("real", K_REAL_POOL)):
        depth[domain] = {}
        for method in ("fixed", "adaptive", "full"):
            for k in ([8, 12, 20, 25] if domain == "synthetic" else [8, 12, 15, 20]):
                if method == "full" and k != pool:
                    continue
                rs = _method_records(recs, domain, method, k, pool)
                if not rs:
                    continue
                block = {}
                for z in ("300", "400", "500"):
                    vals = [_f(r["depth"][z]["sigma_mm"]) for r in rs]
                    block[z] = _stats(vals)
                depth[domain].setdefault(method, {})[str(k)] = block
        if domain == "real":
            rs = [r for r in recs if r["domain"] == "real" and r["method"] == "full25"]
            if rs:
                depth[domain]["full25"] = {}
                for z in ("300", "400", "500"):
                    depth[domain]["full25"][z] = _stats([_f(r["depth"][z]["sigma_mm"]) for r in rs])

    # ---------------- runtime / diversity ----------------
    runtime = {}
    for domain, methods, pool in (
            ("synthetic", METHODS + ("full",), K_SYNTH_POOL),
            ("real", DEPLOYABLE + ("full25", "loo"), K_REAL_POOL)):
        runtime[domain] = {}
        for method in methods:
            if method == "full25":
                ks = [25]
            elif method == "loo":
                ks = [25]
            else:
                ks = [k for k in kk if not (method == "full" and k != pool)]
            for k in ks:
                rs = _method_records(recs, domain, method, k, pool)
                if rs:
                    runtime[domain].setdefault(method, {})[str(k)] = _stats(
                        [_f(r["time_s"]) for r in rs])

    errors = []
    for j in cfg.get("_jobs", []):
        pass
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    data_info = {
        "real_views": len(real.view_ids),
        "real_missing_left": [v + 1 for v in real.view_ids
                              if "left" not in real.obs_by_view.get(v, {})],
        "ccf_baseline_mm": real.ccf_baseline_mm,
        "pitch_mm": plate.DEFAULT_PITCH_MM,
        "fold_holdout": {str(f): [v + 1 for v in hold]
                         for f, hold in real.fold_holdout.items()},
        "fold_train_size": {str(f): len(v) for f, v in real.fold_train.items()},
        "scene_seeds": [s.seed for s in scenes],
        "synthetic_validation_views": len(scenes[0].validation_ids) if scenes else 0,
        "synthetic_noise_px": cfg.get("noise_px"),
        "synthetic_outlier_frac": cfg.get("outlier_frac"),
        "synthetic_outlier_sigma": cfg.get("outlier_sigma"),
    }
    leakage = {
        "synthetic": {
            "candidate_pool": "25 EXStar-layout views per scene",
            "validation": "10 independent views, never fitted and never in a selection pool",
            "overlap": "empty by construction",
            "oracle": "best-of-pool per metric; uses ground truth / validation metrics; "
                      "diagnostic upper bound, not deployable",
            "gt_fisher": "subset ordering computed at ground-truth parameters and poses; "
                         "diagnostic only",
        },
        "real": {
            "folds": "5 folds; each holds out one view per orientation (5 views); "
                     "fit/selection pool is the other 20",
            "selection_pool": "only the 20 training views; held-out views never enter a fit",
            "k=20": "full training pool (identical for all methods)",
            "k=25_all25": "separate fit on all 25; NOT mixed with fold rows; no held-out claim",
            "k=25_loo": "25 leave-one-view-out fits; each excludes its own validation view",
            "exstar_prefix": "first k of the 25 in acquisition order; train-only, "
                             "explicitly not the fold held-out design",
            "ccf": "factory/user CCF is a REFERENCE, not ground truth",
        },
    }
    sh = engine_info.get("files_sha256", {})
    cal_post = sh.get("calibrate.py") == POST_FREEZE_CALIBRATE_SHA256
    q_post = sh.get("quality.py") == POST_FREEZE_QUALITY_SHA256
    engine_info = dict(engine_info)
    if all(engine_info.get("frozen_match", {}).values()):
        note = "engine files match the frozen hash set"
    else:
        bits = []
        if cal_post:
            bits.append("calibrate.py post-freeze float32 init fix (free_kc=True "
                        "only; free_kc=False numerically equivalent to 05a8a60b)")
        elif not engine_info.get("frozen_match", {}).get("calibrate.py", True):
            bits.append("calibrate.py differs from the frozen hash")
        if q_post:
            bits.append("quality.py post-freeze stopping hardening; the metric "
                        "helpers used here (baseline_sigma, relative_pose_sigma, "
                        "depth_uncertainty, rgb_alignment, view_diversity, "
                        "image_coverage) were inspected unchanged")
        elif not engine_info.get("frozen_match", {}).get("quality.py", True):
            bits.append("quality.py differs from the frozen hash")
        note = "; ".join(bits) or "engine differs from the frozen hash set"
    engine_info["freeze_note"] = note
    out = {
        "schema": "einstar-viewcount-v1",
        "generated_utc": ts,
        "engine": engine_info,
        "config": {k: v for k, v in cfg.items() if not k.startswith("_")},
        "data": data_info,
        "leakage_controls": leakage,
        "synthetic": synth,
        "oracle": oracle,
        "real": real_sum,
        "real_consistency": real_cons,
        "real_k_min": k_real,
        "min_k": min_k,
        "min_k_combined": combined,
        "paired": paired,
        "sigma_calibration": sigma_cal,
        "pitch": pitch_rows,
        "freekc": freekc_rows,
        "depth": depth,
        "runtime": runtime,
        "summary_seconds": time.time() - t0,
        "n_records": len(recs),
    }
    return out


# ==========================================================================
# CLI / orchestration
# ==========================================================================

FROZEN_ENGINE_SHA256 = {
    "calibrate.py": "05a8a60ba67214cc8d8f9b2ceae2f3e33e9b2d08208cb281af9b482256a6548b",
    "adaptive.py": "3078855775f9df56c5a345ce8dc2bd8bedc550b71348babf2382d11ee016700d",
    "quality.py": "ec8f6d91893e46a4b662212cd488143b9a2accca3d9d9c80dc6e5106a4a72c6c",
    "plate.py": "1c36b079ff14236d87316ba75f3eb68e0d090f5c13dc6eee4299b5a6759b443c",
    "pipeline.py": "a1e00ccd3307bed9b8dd420c887bdbd1b9061b03d3815b2edf309c6709b6f48a",
    "evaluate.py": "93a24d3c7fcbeabfcaad7d375cec18a329fb519c0ec9ef7216f94b823d981e1e",
    "ccf.py": "49138df50f097cb00f438b61e9f639f413c0e6e7316a0c076df89e02b436482c",
    "detect.py": "72400323e364f2d085fdd95ab280809411ae9cd63765c40c48886b4758585ff4",
    "experiments.py": "682b973848a4a2a6312a7379c48c5893f0379cc212d270f56fcbc277818b1384",
    "profiles.py": "8a61f5cd40d683c7c30576e81422a9e3b376348e1df61172cdef2a04cf62058c",
    "synth.py": "64e69dda52d9ea5804abcb727b4c7d53c15cba5a1fabea558dff6ae8370fc4e8",
}


POST_FREEZE_CALIBRATE_SHA256 = (
    "db637ace81f3cba110855c70f00536d6430e6ffd4733aa314f2b460f1a2b64e0")
POST_FREEZE_QUALITY_SHA256 = (
    "0ca3476a1e16bfca34c90d609b0063615d5a184a727b382b11b6d1dd03e0010f")


def engine_frozen_match() -> dict:
    h = engine()["files_sha256"]
    return {n: (h.get(n) == v) for n, v in FROZEN_ENGINE_SHA256.items()}


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=("quick", "full"), default="quick")
    ap.add_argument("--scenes", type=int, default=None,
                    help="synthetic scenes (default: full 30, quick 10)")
    ap.add_argument("--seeds", type=int, default=None,
                    help="random-subset seeds (default: full 10, quick 5)")
    ap.add_argument("--folds", type=int, default=None,
                    help="real folds (default 5, max 5)")
    ap.add_argument("--k", type=str, default="8,10,12,15,20,25")
    ap.add_argument("--workers", type=int, default=None)
    ap.add_argument("--only", type=str, default="synth,real,pitch,freekc",
                    help="comma list of synth,real,pitch,freekc")
    ap.add_argument("--cache", type=Path, default=CACHE_PATH)
    ap.add_argument("--out-dir", type=Path, default=AUDIT_DIR)
    ap.add_argument("--force", action="store_true", help="ignore cache")
    ap.add_argument("--no-loo", action="store_true")
    ap.add_argument("--no-pitch", action="store_true")
    ap.add_argument("--no-freekc", action="store_true")
    ap.add_argument("--no-plots", action="store_true")
    ap.add_argument("--no-report", action="store_true")
    ap.add_argument("--no-json", action="store_true")
    ap.add_argument("--tag", type=str, default="")
    return ap.parse_args(argv)


def make_config(args) -> dict:
    quick = args.mode == "quick"
    scenes = args.scenes if args.scenes is not None else (10 if quick else 30)
    seeds = args.seeds if args.seeds is not None else (5 if quick else 10)
    folds = args.folds if args.folds is not None else 5
    kvals = tuple(sorted(set(int(x) for x in args.k.split(",") if x.strip())))
    only = tuple(x.strip() for x in args.only.split(",") if x.strip())
    pitch_scales = () if args.no_pitch else (0.99, 0.995, 0.999, 1.0, 1.001, 1.005, 1.01)
    workers = args.workers if args.workers is not None else (8 if quick else 12)
    return {
        "mode": args.mode, "tag": args.tag,
        "scenes": int(scenes), "seeds": int(seeds), "folds": int(min(folds, 5)),
        "k_values": list(kvals), "only": list(only),
        "free_kc": False,
        "noise_px": 0.25, "outlier_frac": 0.005, "outlier_sigma": 3.0,
        "pitch_scales": pitch_scales,
        "pitch_scenes": 3 if quick else 6,
        "freekc_scenes": 3 if quick else 6,
        "loo": not args.no_loo, "freekc": not args.no_freekc,
        "oracle": True, "workers": int(workers),
        "bootstrap_n": BOOT_N,
        "reduced_design": bool(scenes < 30 or seeds < 10),
    }


def _filter_jobs(jobs: list[dict], only: Sequence[str]) -> list[dict]:
    out = []
    for j in jobs:
        m = j["method"]
        if j["domain"] == "synthetic" and m in ("pitch",):
            keep = "pitch" in only
        elif j["domain"] == "real" and m == "pitch":
            keep = "pitch" in only
        elif m == "freekc":
            keep = "freekc" in only
        elif j["domain"] == "synthetic":
            keep = "synth" in only
        else:
            keep = "real" in only
        if keep:
            out.append(j)
    return out


def main(argv=None) -> int:
    args = parse_args(argv)
    cfg = make_config(args)
    out_dir = Path(args.out_dir)
    fig_dir = out_dir / "figures"
    out_dir.mkdir(parents=True, exist_ok=True)
    fig_dir.mkdir(parents=True, exist_ok=True)
    info = engine()
    frozen = engine_frozen_match()
    print(f"[viewcount] engine {info['engine_hash']} git {str(info['git_head'])[:10]} "
          f"frozen_match={all(frozen.values())}", flush=True)
    if not all(frozen.values()):
        only_cal = [n for n, ok in frozen.items() if not ok] == ["calibrate.py"] and \
            info["files_sha256"].get("calibrate.py") == POST_FREEZE_CALIBRATE_SHA256
        if only_cal:
            print("[viewcount] NOTE: post-freeze calibrate.py (float32 init fix, "
                  "free_kc=True path only) is in use; primary protocol is "
                  "free_kc=False and is numerically unaffected.", flush=True)
        else:
            print("[viewcount] WARNING: engine differs from the frozen hash set:",
                  flush=True)
            for n, ok in frozen.items():
                if not ok:
                    print(f"    {n}: {info['files_sha256'].get(n)} != "
                          f"{FROZEN_ENGINE_SHA256[n]}", flush=True)
    real = load_vendor()
    scenes = [make_exstar_scene(1000 + i, cfg["noise_px"], cfg["outlier_frac"],
                                cfg["outlier_sigma"])
              for i in range(int(cfg["scenes"]))]
    print(f"[viewcount] {len(scenes)} synthetic scenes, {cfg['seeds']} seeds, "
          f"{cfg['folds']} real folds; k={cfg['k_values']}", flush=True)
    jobs = build_jobs(cfg, scenes, real)
    jobs = _filter_jobs(jobs, cfg["only"])
    cfg["_jobs"] = jobs
    cache = {} if args.force else load_cache(Path(args.cache))
    cache = execute_jobs(jobs, cache, cfg["workers"], scenes, real,
                         cache_path=Path(args.cache))
    if not args.force:
        save_cache(Path(args.cache), cache)
    recs, errors, selections = [], [], {}
    for j in jobs:
        r = cache.get(j["key"])
        if not r:
            errors.append({"job": {k: v for k, v in j.items() if k != "key"},
                           "error": "missing from cache"})
            continue
        if r.get("error"):
            errors.append({"job": {k: v for k, v in j.items() if k != "key"},
                           "error": r["error"]})
        recs.extend(r.get("records", []))
        if r.get("selection"):
            selections[f"{j['domain']}:{j.get('scene')}:{j.get('fold')}"] = r["selection"]
    print(f"[viewcount] {len(recs)} records, {len(errors)} job errors", flush=True)
    cfg_public = {k: v for k, v in cfg.items() if not k.startswith("_")}
    summary = aggregate(recs, cfg, {**info, "frozen_match": frozen,
                                    "frozen_expected": FROZEN_ENGINE_SHA256},
                        real, scenes)
    summary["job_errors"] = errors[:100]
    summary["job_error_count"] = len(errors)
    summary["records"] = recs
    summary["adaptive_selection"] = selections
    summary["config"] = cfg_public
    if not args.no_json:
        path = out_dir / "viewcount_results.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(_clean(summary), indent=1, default=_json_default))
        tmp.replace(path)
        print(f"[viewcount] wrote {path}", flush=True)
    if not args.no_plots:
        try:
            make_plots(summary, fig_dir)
            print(f"[viewcount] wrote figures to {fig_dir}", flush=True)
        except Exception:
            traceback.print_exc()
    if not args.no_report:
        try:
            write_report(summary, out_dir / "viewcount_report.md")
            print(f"[viewcount] wrote {out_dir / 'viewcount_report.md'}", flush=True)
        except Exception:
            traceback.print_exc()
    print("[viewcount] done", flush=True)
    return 0




# ==========================================================================
# plots
# ==========================================================================

def _ks_sorted(summary_or_block) -> list[int]:
    return sorted(int(k) for k in summary_or_block)


def _series(block: dict, metric: str, field: str = "mean") -> tuple[list[int], list[float]]:
    xs, ys = [], []
    for k in _ks_sorted(block):
        st = block[str(k)].get(metric)
        if st and st.get(field) is not None:
            xs.append(k)
            ys.append(float(st[field]))
    return xs, ys


def make_plots(summary: dict, fig_dir: Path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig_dir.mkdir(parents=True, exist_ok=True)
    synth = summary.get("synthetic", {})
    oracle = summary.get("oracle", {})
    colors = {"fixed": "C0", "uniform": "C1", "random": "C2", "adaptive": "C3",
              "gt_fisher": "C4", "oracle": "k"}

    # 1 fx error vs k
    fig, ax = plt.subplots(figsize=(6.4, 4.0))
    for m in ("fixed", "uniform", "random", "adaptive", "oracle"):
        block = oracle if m == "oracle" else synth.get(m, {})
        xs, ys = _series(block, "fx_abs_max", "mean")
        if xs:
            ax.plot(xs, ys, marker="o", label=m, color=colors.get(m))
            if m != "oracle":
                _, u = _series(block, "fx_abs_max", "ucb_mean")
                if len(u) == len(ys):
                    ax.fill_between(xs, ys, u, alpha=0.12, color=colors.get(m))
    for t in (0.5, 1.0):
        ax.axhline(t, ls="--", color="grey", lw=0.8)
    ax.set_xlabel("captures k")
    ax.set_ylabel("|fx| max over cameras (px)")
    ax.set_title("Synthetic: intrinsic error vs capture count")
    ax.legend(fontsize=8)
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(fig_dir / "fx_error_vs_k.png", dpi=160)
    plt.close(fig)

    # 2 baseline vs k
    fig, ax = plt.subplots(figsize=(6.4, 4.0))
    for m in ("fixed", "uniform", "random", "adaptive", "oracle"):
        block = oracle if m == "oracle" else synth.get(m, {})
        xs, ys = _series(block, "baseline_abs_mm", "mean")
        if xs:
            ax.plot(xs, ys, marker="o", label=m, color=colors.get(m))
            _, u = _series(block, "baseline_abs_mm", "ucb_mean")
            if len(u) == len(ys) and m != "oracle":
                ax.fill_between(xs, ys, u, alpha=0.12, color=colors.get(m))
    for t in (0.05, 0.10):
        ax.axhline(t, ls="--", color="grey", lw=0.8)
    ax.set_xlabel("captures k")
    ax.set_ylabel("|baseline error| (mm)")
    ax.set_title("Synthetic: IR baseline error vs capture count")
    ax.legend(fontsize=8)
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(fig_dir / "baseline_error_vs_k.png", dpi=160)
    plt.close(fig)

    # 3 held-out vs k (synthetic + real)
    fig, axes = plt.subplots(1, 2, figsize=(9.6, 3.8))
    ax = axes[0]
    for m in ("fixed", "uniform", "random", "adaptive", "oracle"):
        block = oracle if m == "oracle" else synth.get(m, {})
        xs, ys = _series(block, "heldout_rt_mean", "mean")
        if xs:
            ax.plot(xs, ys, marker="o", label=m, color=colors.get(m))
    ax.axhline(0.5, ls="--", color="grey", lw=0.8)
    ax.set_xlabel("k"); ax.set_ylabel("held-out right+tex RMS (px)")
    ax.set_title("Synthetic (10 independent views)")
    ax.legend(fontsize=7); ax.grid(alpha=0.3)
    ax = axes[1]
    for m in ("fixed", "uniform", "random", "adaptive"):
        rows = summary.get("real_consistency", {}).get(m, [])
        if rows:
            ax.plot([r["k"] for r in rows], [r["heldout_ucb"] for r in rows],
                    marker="o", label=f"{m} (UCB95)")
    ax.axhline(0.5, ls="--", color="grey", lw=0.8)
    ax.set_xlabel("k"); ax.set_ylabel("pooled held-out RMS (px)")
    ax.set_title("Vendor corpus (fold held-out, CCF reference)")
    if ax.get_legend_handles_labels()[0]:
        ax.legend(fontsize=7)
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(fig_dir / "heldout_vs_k.png", dpi=160)
    plt.close(fig)

    # 4 uncertainty vs k
    fig, axes = plt.subplots(1, 2, figsize=(9.6, 3.8))
    ax = axes[0]
    for m in ("fixed", "adaptive"):
        xs, ys = _series(synth.get(m, {}), "nees_n", "median")
        if xs:
            ax.plot(xs, ys, marker="o", label=m)
    ax.axhline(1.0, ls="--", color="grey", lw=0.8)
    ax.set_xlabel("k"); ax.set_ylabel("NEES / n_free (median)")
    ax.set_title("Synthetic rig NEES")
    ax.legend(fontsize=7); ax.grid(alpha=0.3)
    ax = axes[1]
    for m in ("fixed", "adaptive"):
        xs, ys = _series(synth.get(m, {}), "cov68", "mean")
        if xs:
            ax.plot(xs, ys, marker="o", label=f"{m} 68%")
        xs, ys = _series(synth.get(m, {}), "cov95", "mean")
        if xs:
            ax.plot(xs, ys, marker="s", ls="--", label=f"{m} 95%")
    ax.axhline(0.68, ls=":", color="grey", lw=0.8)
    ax.axhline(0.95, ls=":", color="grey", lw=0.8)
    ax.set_xlabel("k"); ax.set_ylabel("coverage")
    ax.set_title("Synthetic interval coverage")
    ax.legend(fontsize=6); ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(fig_dir / "uncertainty_vs_k.png", dpi=160)
    plt.close(fig)

    # 5 real parameter drift vs k
    fig, axes = plt.subplots(1, 2, figsize=(9.6, 3.8))
    ax = axes[0]
    for m in ("fixed", "uniform", "random", "adaptive"):
        xs, ys = _series(summary.get("real", {}).get(m, {}), "dfx_max_abs", "mean")
        if xs:
            ax.plot(xs, ys, marker="o", label=m)
    ax.set_xlabel("k"); ax.set_ylabel("|fx - fx_CCF| max (px)")
    ax.set_title("Vendor corpus: CCF-relative focal shift")
    ax.legend(fontsize=7); ax.grid(alpha=0.3)
    ax = axes[1]
    for m in ("fixed", "uniform", "random", "adaptive"):
        xs, ys = _series(summary.get("real", {}).get(m, {}), "baseline_vs_ccf_mm", "mean")
        if xs:
            ax.plot(xs, ys, marker="o", label=m)
    ax.axhline(0.0, ls="--", color="grey", lw=0.8)
    ax.set_xlabel("k"); ax.set_ylabel("B - B_CCF (mm)")
    ax.set_title("Vendor corpus: baseline vs CCF")
    if ax.get_legend_handles_labels()[0]:
        ax.legend(fontsize=7)
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(fig_dir / "real_params_vs_k.png", dpi=160)
    plt.close(fig)

    # 6 pitch sensitivity
    rows = summary.get("pitch", [])
    if rows:
        fig, axes = plt.subplots(1, 2, figsize=(9.6, 3.8))
        ax = axes[0]
        for dom, color in (("real", "C3"), ("synthetic", "C0")):
            rr = [r for r in rows if r["domain"] == dom]
            if not rr:
                continue
            by = {}
            for r in rr:
                by.setdefault(r["scale"], []).append(r["baseline_ratio"])
            xs = sorted(by)
            ax.plot(xs, [np.mean(by[x]) for x in xs], marker="o", color=color,
                    label=dom)
        ax.plot([0.99, 1.01], [0.99, 1.01], ls="--", color="grey", lw=0.8,
                label="B proportional to pitch")
        ax.set_xlabel("pitch scale")
        ax.set_ylabel("B / B(scale=1)")
        ax.set_title("Plate-pitch sensitivity of baseline")
        ax.legend(fontsize=7); ax.grid(alpha=0.3)
        ax = axes[1]
        for dom, color in (("real", "C3"), ("synthetic", "C0")):
            rr = [r for r in rows if r["domain"] == dom]
            if not rr:
                continue
            by = {}
            for r in rr:
                by.setdefault(r["scale"], []).append(r["fx_shift_px"])
            xs = sorted(by)
            ax.plot(xs, [np.max(by[x]) for x in xs], marker="o", color=color, label=dom)
        ax.set_xlabel("pitch scale"); ax.set_ylabel("max |dfx| (px)")
        ax.set_title("Intrinsics are scale-invariant")
        ax.legend(fontsize=7); ax.grid(alpha=0.3)
        fig.tight_layout()
        fig.savefig(fig_dir / "pitch_sensitivity.png", dpi=160)
        plt.close(fig)

    # 7 depth sigma
    depth = summary.get("depth", {}).get("synthetic", {})
    if depth:
        fig, ax = plt.subplots(figsize=(5.6, 3.8))
        for m, ls in (("full", "-"), ("adaptive", "--"), ("fixed", ":")):
            block = depth.get(m, {})
            for k in sorted(block, key=int):
                if m != "full" and int(k) not in (8, 12, 20):
                    continue
                ys = [block[k][z]["mean"] for z in ("300", "400", "500")]
                ax.plot([300, 400, 500], ys, marker="o", ls=ls,
                        label=f"{m} k={k}")
        ax.set_xlabel("distance (mm)"); ax.set_ylabel("predicted depth sigma (mm)")
        ax.set_title("Synthetic: predicted depth uncertainty")
        ax.legend(fontsize=6); ax.grid(alpha=0.3)
        fig.tight_layout()
        fig.savefig(fig_dir / "depth_sigma.png", dpi=160)
        plt.close(fig)

    # 8 runtime
    fig, ax = plt.subplots(figsize=(6.0, 3.8))
    for m in ("fixed", "uniform", "random", "adaptive"):
        xs, ys = _series(synth.get(m, {}), "time_s", "median")
        if xs:
            ax.plot(xs, ys, marker="o", label=m)
    ax.set_xlabel("k"); ax.set_ylabel("calibration time (s, median)")
    ax.set_title("Runtime (detection excluded, single-threaded worker)")
    ax.legend(fontsize=7); ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(fig_dir / "runtime_vs_k.png", dpi=160)
    plt.close(fig)


# ==========================================================================
# report
# ==========================================================================

def _fmt(v, nd=3):
    if v is None:
        return "—"
    try:
        v = float(v)
    except Exception:
        return str(v)
    if not math.isfinite(v):
        return "—"
    return f"{v:.{nd}f}"


def _md_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    out = ["| " + " | ".join(headers) + " |",
           "|" + "|".join(["---"] * len(headers)) + "|"]
    for r in rows:
        out.append("| " + " | ".join(str(x) for x in r) + " |")
    return "\n".join(out)


def _st_cell(st: dict, nd=3) -> str:
    if not st or st.get("mean") is None:
        return "—"
    return (f"{_fmt(st['mean'], nd)} [{_fmt(st.get('q25'), nd)}, "
            f"{_fmt(st.get('q75'), nd)}] UCB {_fmt(st.get('ucb_mean'), nd)}")


def _row_metric(summary: dict, method: str, k: int, metric: str, nd=3) -> str:
    block = summary.get("synthetic", {}).get(method, {}).get(str(k), {})
    return _st_cell(block.get(metric, {}), nd)


def write_report(summary: dict, path: Path):
    cfg = summary.get("config", {})
    eng = summary.get("engine", {})
    data = summary.get("data", {})
    synth = summary.get("synthetic", {})
    real = summary.get("real", {})
    kk = [int(k) for k in cfg.get("k_values", K_VALUES)]
    kk = [k for k in kk if 8 <= k <= 25]
    reduced = cfg.get("reduced_design", False)
    L = []
    L.append("# Capture-count comparison for the Einstar three-camera calibration")
    L.append("")
    L.append(f"Generated: {summary.get('generated_utc')}  ")
    L.append(f"Engine: git `{str(eng.get('git_head'))[:12]}` "
             f"(calib dirty: {eng.get('git_dirty_calib')}), engine hash "
             f"`{eng.get('engine_hash')}`  ")
    L.append(f"Harness: `calib/viewcount.py` sha256 `{str(eng.get('harness_sha256'))[:16]}`  ")
    if reduced:
        L.append(f"**Reduced design:** {cfg.get('scenes')} synthetic scenes, "
                 f"{cfg.get('seeds')} random seeds (requested interim table).")
    L.append("")
    # ---------------- headline ----------------
    L.append("## Answer in brief")
    L.append("")
    method_txt = []
    for method in ("fixed", "uniform", "random", "adaptive"):
        rows = summary.get("min_k_combined", {}).get(method, [])
        if not rows:
            continue
        parts = []
        for r in rows:
            if r["metric"] in ("fx_abs_max", "baseline_abs_mm", "heldout_rt_mean"):
                parts.append(f"{r['label']}: k_ucb={r['k_synth_ucb']}, "
                             f"combined={r['k_combined']}")
        method_txt.append(f"- **{method}**: " + "; ".join(parts))
    L.extend(method_txt)
    L.append("")
    L.append("_k_ucb_ is the smallest k whose one-sided 95 % upper confidence bound "
             "on the mean synthetic error meets the target; _combined_ additionally "
             "requires the real-corpus consistency checks (held-out UCB, fx drift, "
             "baseline drift) to pass at that k.  See Section 4._")
    L.append("")
    # ---------------- design ----------------
    L.append("## 1. Design and leakage controls")
    L.append("")
    L.append("**Synthetic ground truth.** Each scene has 25 candidate views on the "
             "recovered EXStar grid (5 orientations x 5 distance bands 240/320/400/"
             "480/560 mm, roll 180 deg) and 10 independent validation views with "
             "off-grid diagonal tilts, random distance and lateral offset.  The 10 "
             "validation views are never used for fitting or for any deployable "
             "selection.  Intrinsics are perturbed by ~6 px from truth at "
             "initialisation; distortion is held fixed at its true value in the main "
             "protocol (`free_kc=False`).")
    L.append("")
    L.append(f"**Real corpus.** {data.get('real_views')} vendor views; left detections "
             f"are missing for views {data.get('real_missing_left')} (1-based).  Five "
             "folds, each holding out one view per orientation (5 views) and fitting/"
             "selecting on the other 20.  Held-out reprojection solves the view pose "
             "from the left camera only and predicts right/tex through the calibrated "
             "rig; held-out IR epipolar uses the calibrated fundamental matrix.  The "
             "factory CCF (baseline "
             f"{_fmt(data.get('ccf_baseline_mm'), 3)} mm) is a **reference, not truth**.")
    L.append("")
    L.append("**Not mixed with the fold rows.** (i) a separate all-25 fit, (ii) 25 "
             "leave-one-view-out fits, (iii) EXStar acquisition-order prefixes "
             "(train-only).  These are labelled in the tables and never enter the "
             "fold curves or the minimum-k decision.")
    L.append("")
    L.append("**Methods.** `fixed` = first k in EXStar acquisition order (what an "
             "early stop actually captures); `uniform` = k evenly spaced in that "
             "order; `random` = prefix of a random ordering, "
             f"{cfg.get('seeds')} seeds; `adaptive` = greedy marginal Fisher gain "
             "with the engine's distance/tilt diversity rule, refitted at each "
             "evaluated count; `gt_fisher` = ordering computed at ground-truth "
             "parameters/poses (diagnostic); `oracle` = per-metric best subset among "
             "all evaluated subsets (diagnostic upper bound, not deployable).")
    L.append("")
    # ---------------- engine ----------------
    L.append("## 2. Engine and data provenance")
    L.append("")
    fm = eng.get("frozen_match", {})
    mism = [n for n, ok in fm.items() if not ok]
    L.append(f"- frozen engine files: {sum(1 for v in fm.values() if v)}/{len(fm)} match")
    if mism:
        L.append(f"- post-freeze difference: {mism}.")
        if "calibrate.py" in mism:
            L.append("  `calibrate.py` is the post-freeze float32 `_initial_intrinsics` "
                     "fix, reached **only when `free_kc=True`**; the primary protocol "
                     "here is `free_kc=False`, so the frozen `05a8a60b` numerics apply "
                     "and the free-distortion sensitivity rows are labelled post-fix.")
        if "quality.py" in mism:
            L.append("  `quality.py` is the post-freeze stopping hardening.  This study "
                     "does not call `stopping_report`; the metric helpers it does use "
                     "(baseline_sigma, relative_pose_sigma, depth_uncertainty, "
                     "rgb_alignment, view_diversity, image_coverage) were inspected and "
                     "are unchanged from the frozen semantics.")
    L.append(f"- pitch: {_fmt(data.get('pitch_mm'), 3)} mm; CCF IR baseline: "
             f"{_fmt(data.get('ccf_baseline_mm'), 4)} mm")
    L.append(f"- synthetic noise: {cfg.get('noise_px')} px Gaussian, "
             f"{cfg.get('outlier_frac')} outliers at {cfg.get('outlier_sigma')} px")
    L.append(f"- folds (1-based held-out views): {data.get('fold_holdout')}")
    L.append("")
    # ---------------- synthetic tables ----------------
    L.append("## 3. Synthetic accuracy vs capture count")
    L.append("")
    L.append("Values are mean [IQR] and one-sided 95 % UCB over scenes; UCB is the "
             "quantity compared with the target.")
    L.append("")
    for method in ("fixed", "uniform", "random", "adaptive", "gt_fisher"):
        if method not in synth:
            continue
        L.append(f"### {method}")
        rows = []
        for k in kk:
            b = synth[method].get(str(k))
            if not b:
                continue
            rows.append([k,
                         _st_cell(b["fx_abs_max"]),
                         _st_cell(b["baseline_abs_mm"], 4),
                         _st_cell(b["heldout_rt_mean"]),
                         _st_cell(b["epipolar_median_px"]),
                         _st_cell(b["nees_n"], 2),
                         _st_cell(b["cov68"], 3),
                         _st_cell(b["cov95"], 3),
                         _st_cell(b["time_s"], 1)])
        if rows:
            L.append(_md_table(["k", "|fx|max px", "|B err| mm",
                                "held-out right+tex px", "epi median px",
                                "NEES/n", "cov68", "cov95", "time s"], rows))
            L.append("")
    L.append("### oracle (diagnostic upper bound)")
    rows = []
    for k in kk:
        b = summary.get("oracle", {}).get(str(k))
        if not b:
            continue
        rows.append([k, _st_cell(b["fx_abs_max"]), _st_cell(b["baseline_abs_mm"], 4),
                     _st_cell(b["heldout_rt_mean"]), _st_cell(b["epipolar_median_px"]),
                     _st_cell(b["nees_n"], 2)])
    if rows:
        L.append(_md_table(["k", "|fx|max", "|B err|", "held-out", "epi", "NEES/n"], rows))
    L.append("")
    # ---------------- min k ----------------
    L.append("## 4. Minimum reliable capture count")
    L.append("")
    L.append("`k_mean` = point estimate meets target; `k_ucb` = 95 % UCB of the mean "
             "meets target; `k_p90` = 95 % UCB of the 90th percentile meets target.  "
             "`k_combined` requires the real-corpus consistency checks at or above "
             "`k_ucb`.")
    L.append("")
    for method in ("fixed", "uniform", "random", "adaptive", "gt_fisher", "oracle"):
        entries = summary.get("min_k", {}).get(method, [])
        if not entries:
            continue
        rows = []
        for e in entries:
            if e["metric"] not in ("fx_abs_max", "baseline_abs_mm", "heldout_rt_mean"):
                continue
            rows.append([e["label"], e["target"], _fmt(e["k_mean"], 0),
                         _fmt(e["k_ucb"], 0), _fmt(e["k_ucb_p90"], 0),
                         _fmt(next((c["k_combined"] for c in
                                    summary.get("min_k_combined", {}).get(method, [])
                                    if c["metric"] == e["metric"]
                                    and c["target"] == e["target"]), None), 0)])
        L.append(f"### {method}")
        L.append(_md_table(["target", "value", "k_mean", "k_ucb", "k_p90",
                            "k_combined"], rows))
        L.append("")
    # ---------------- deltas ----------------
    L.append("## 5. k = 8 vs 12 vs 25 deltas")
    L.append("")
    L.append("### Synthetic (adaptive; k=25 is the full candidate pool)")
    dk = [8, 12, 25]
    rows = []
    for metric, label, nd in (("fx_abs_max", "|fx|max px", 3),
                              ("baseline_abs_mm", "|B err| mm", 4),
                              ("heldout_rt_mean", "held-out px", 3),
                              ("epipolar_median_px", "epi median px", 3),
                              ("nees_n", "NEES/n", 2),
                              ("cov68", "cov68", 3)):
        row = [label]
        for k in dk:
            st = synth.get("adaptive", {}).get(str(k), {}).get(metric)
            row.append(_fmt(st.get("mean") if st else None, nd))
        rows.append(row)
    L.append(_md_table(["metric", "k=8", "k=12", "k=25"], rows))
    L.append("")
    L.append("### Real corpus (fold held-out; k=25 row is the separate 25x "
             "leave-one-view-out experiment)")
    rows = []
    for metric, label, nd in (("dfx_max_abs", "|fx-fx_CCF|max px", 2),
                              ("baseline_vs_ccf_mm", "B-B_CCF mm", 3),
                              ("heldout_rt_mean", "held-out px", 3),
                              ("epipolar_median_px", "epi median px", 3),
                              ("sigma_fx_max", "sigma fx px", 3)):
        row = [label]
        for k in (8, 12, 20):
            st = real.get("adaptive", {}).get(str(k), {}).get(metric)
            row.append(_fmt(st.get("mean") if st else None, nd))
        loo = real.get("loo", {}).get("25", {}).get("heldout_pooled_views")
        if metric == "heldout_rt_mean":
            row.append(_fmt(loo.get("mean") if loo else None, nd))
        elif metric == "epipolar_median_px":
            row.append(_fmt(real.get("loo", {}).get("25", {}).get("epipolar_pooled", {}).get("mean"), nd))
        else:
            row.append("—")
        rows.append(row)
    L.append(_md_table(["metric", "k=8", "k=12", "k=20", "k=25 LOO"], rows))
    L.append("")
    # ---------------- real detail ----------------
    L.append("## 6. Vendor corpus detail (CCF reference)")
    L.append("")
    L.append("### Pooled held-out right+tex RMS and real-consistency checks")
    rows = []
    for method in ("fixed", "uniform", "random", "adaptive"):
        for r in summary.get("real_consistency", {}).get(method, []):
            rows.append([method, r["k"], _fmt(r["heldout_mean"]),
                         _fmt(r["heldout_ucb"]), r["heldout_n"],
                         _fmt(r["fx_drift_median"], 3),
                         _fmt(r["fx_drift_max"], 3),
                         _fmt(r["baseline_drift_median"], 4),
                         "yes" if r["pass_all"] else "no"])
    L.append(_md_table(["method", "k", "held-out mean", "UCB95", "n views",
                        "fx drift med", "fx drift max", "B drift med", "pass"],
                       rows))
    L.append("")
    L.append("_fx drift_ = max over cameras of |fx(k) - fx(20-view fit)|; "
             "_B drift_ = |B(k) - B(20-view fit)|.  Passing requires held-out "
             "UCB95 <= 0.5 px, fx drift median <= 1.0 px, B drift median <= 0.1 mm.")
    L.append("")
    L.append("### Parameter and epipolar behaviour by k (mean over folds)")
    rows = []
    for method in ("fixed", "uniform", "random", "adaptive"):
        for k in [x for x in kk if x <= 20]:
            b = real.get(method, {}).get(str(k))
            if not b:
                continue
            rows.append([method, k, _st_cell(b["dfx_max_abs"], 2),
                         _st_cell(b["baseline_ccf_abs"], 4),
                         _st_cell(b["heldout_rt_mean"], 3),
                         _st_cell(b["epipolar_median_px"], 3),
                         _st_cell(b["sigma_fx_max"], 3)])
    L.append(_md_table(["method", "k", "|fx-fx_CCF|max", "|B-B_CCF| mm",
                        "held-out px", "epi median px", "sigma fx"], rows))
    L.append("")
    # per-fold table for adaptive
    L.append("### Per-fold held-out (adaptive)")
    recs = summary.get("records", [])
    rows = []
    for f in range(int(cfg.get("folds", 5))):
        row = [f]
        for k in [x for x in kk if x <= 20]:
            rr = [r for r in recs if r["domain"] == "real" and r["method"] == "adaptive"
                  and int(r["k"]) == k and int(r.get("fold", -1)) == f]
            st = _stats([r["heldout"].get("heldout_rt_mean") for r in rr])
            row.append(_fmt(st["mean"]))
        rows.append(row)
    L.append(_md_table(["fold"] + [f"k={k}" for k in [x for x in kk if x <= 20]], rows))
    L.append("")
    # ---------------- prefix ----------------
    L.append("## 7. Real EXStar acquisition-order prefixes (train-only, NOT held-out)")
    L.append("")
    L.append("These fits use the first k views of the 25-view acquisition sequence.  "
             "They are shown separately because they are **not** the 20-view fold "
             "design and have no independent held-out views; only training RMS and "
             "CCF-relative parameters are claimed.")
    rows = []
    for k in (8, 12, 20):
        rr = [r for r in summary.get("records", [])
              if r["domain"] == "real" and r["method"] == "exstar_prefix"
              and int(r["k"]) == k]
        if not rr:
            continue
        r = rr[0]
        rows.append([k, _fmt(r["rms_train_px"], 4),
                     _fmt(r["ccf"]["dfx_max_abs"], 2),
                     _fmt(r["ccf"]["baseline_vs_ccf_mm"], 4),
                     _fmt(r["ccf"]["ir_rot_vs_ccf_deg"], 3),
                     _fmt(max(abs(x) for x in r["ccf"]["dcx"]), 2)])
    L.append(_md_table(["k", "train RMS px", "|dfx| max", "B-B_CCF mm",
                        "IR rot vs CCF deg", "|dcx| max"], rows))
    L.append("")
    # ---------------- all25/loo ----------------
    L.append("## 8. All-25 and leave-one-view-out")
    L.append("")
    rr = [r for r in summary.get("records", []) if r["domain"] == "real"
          and r["method"] == "full25"]
    if rr:
        r = rr[0]
        L.append(f"- all-25 fit: train RMS {_fmt(r['rms_train_px'], 4)} px, "
                 f"B = {_fmt(r['baseline_mm'], 4)} mm, "
                 f"B-B_CCF = {_fmt(r['ccf']['baseline_vs_ccf_mm'], 4)} mm, "
                 f"|dfx|max = {_fmt(r['ccf']['dfx_max_abs'], 2)} px, "
                 f"sigma fx = {_fmt(max(r['sigma'][f'{c}.fx'] for c in CAMERAS), 3)} px.")
    loo = real.get("loo", {}).get("25")
    if loo:
        hv = loo.get("heldout_pooled_views", {})
        L.append(f"- 25x LOO: held-out right+tex mean {_fmt(hv.get('mean'))} px "
                 f"(UCB95 {_fmt(hv.get('ucb_mean'))}, n={hv.get('n')}), "
                 f"epipolar median {_fmt(loo.get('epipolar_pooled', {}).get('mean'))} px.")
        L.append("- LOO fits use 24 views each and are not comparable to the k<=20 "
                 "fold curves; they only answer whether the all-25 level is stable "
                 "when each view is excluded in turn.")
    L.append("")
    # ---------------- pitch ----------------
    L.append("## 9. Plate-pitch sensitivity")
    L.append("")
    L.append("Baseline scales linearly with the assumed plate pitch; the parameter "
             "shifts show the scale is carried entirely by translations.")
    rows = []
    for dom in ("real", "synthetic"):
        rr = [r for r in summary.get("pitch", []) if r["domain"] == dom]
        by = {}
        for r in rr:
            by.setdefault(r["scale"], []).append(r)
        for s in sorted(by):
            g = by[s]
            rows.append([dom, f"{s:.3f}",
                         _fmt(np.mean([x["baseline_ratio"] for x in g]), 5),
                         _fmt(np.mean([x["baseline_mm"] for x in g]), 4),
                         _fmt(np.max([x["fx_shift_px"] for x in g]), 4),
                         _fmt(np.max([x["cx_shift_px"] for x in g]), 4),
                         _fmt(np.max([x["ir_rot_shift_deg"] for x in g]), 5),
                         _fmt(np.max([x["rms_train_px"] for x in g]), 4)])
    L.append(_md_table(["domain", "pitch scale", "B ratio", "B mm",
                        "max |dfx| px", "max |dcx| px", "max rot shift deg",
                        "train RMS"], rows))
    L.append("")
    L.append("At +-0.1 % pitch the baseline moves by +-0.159 mm, already above the "
             "0.05 mm target; the baseline target is therefore conditional on pitch "
             "accuracy, not an image-only property.")
    L.append("")
    # ---------------- depth ----------------
    L.append("## 10. Predicted depth uncertainty")
    L.append("")
    rows = []
    for m in ("full", "adaptive", "fixed"):
        for k in sorted(summary.get("depth", {}).get("synthetic", {}).get(m, {}),
                        key=int):
            b = summary["depth"]["synthetic"][m][k]
            rows.append(["synthetic", m, k,
                         _fmt(b["300"]["mean"], 3), _fmt(b["400"]["mean"], 3),
                         _fmt(b["500"]["mean"], 3)])
    for m in ("full", "adaptive", "fixed", "full25"):
        for k in sorted(summary.get("depth", {}).get("real", {}).get(m, {}), key=int):
            b = summary["depth"]["real"][m][k]
            rows.append(["real", m, k, _fmt(b["300"]["mean"], 3),
                         _fmt(b["400"]["mean"], 3), _fmt(b["500"]["mean"], 3)])
    L.append(_md_table(["domain", "method", "k", "sigma@300 mm", "sigma@400 mm",
                        "sigma@500 mm"], rows))
    L.append("")
    # ---------------- free kc ----------------
    L.append("## 11. Free-distortion sensitivity")
    L.append("")
    L.append("Main protocol fixes distortion at the factory/true value.  These rows "
             "release k1,k2,p1,p2,k3; real rows use the post-freeze float32 fix.")
    rows = []
    for r in summary.get("freekc", []):
        row = [r["domain"], r["k"], _fmt(r["rms_train_px"], 4),
               _fmt(r["sigma_fx_max"], 3), _fmt(r.get("nees_n"), 2)
               if r["domain"] == "synthetic" else "—"]
        fk = r.get("fixed_kc") or {}
        row.append(_fmt(fk.get("rms_train_px"), 4))
        if r["domain"] == "synthetic":
            row += [_fmt(r.get("fx_abs_max")), _fmt(fk.get("fx_abs_max")),
                    _fmt(r.get("baseline_abs_mm"), 4),
                    _fmt(fk.get("baseline_abs_mm"), 4)]
        else:
            row += ["—", "—", _fmt(r.get("baseline_vs_ccf_mm"), 4),
                    _fmt(fk.get("baseline_vs_ccf_mm"), 4)]
        rows.append(row)
    if rows:
        L.append(_md_table(["domain", "k", "RMS free", "sigma fx",
                            "NEES/n free", "RMS fixed", "fx err free",
                            "fx err fixed", "B err free", "B err fixed"], rows))
    L.append("")
    # ---------------- sigma ----------------
    L.append("## 12. Uncertainty calibration")
    L.append("")
    sc = summary.get("sigma_calibration", {})
    rows = []
    for method in ("fixed", "adaptive"):
        for k in (8, 12, 20, 25):
            b = summary.get("synthetic", {}).get(method, {}).get(str(k))
            if not b:
                continue
            rows.append([method, k, _fmt(b["nees_n"]["median"], 2),
                         _fmt(b["cov68"]["mean"], 3), _fmt(b["cov95"]["mean"], 3),
                         _fmt(sum((r["fx"].get("ratio", float("nan"))
                                   for r in sc.get("synthetic", [])
                                   if r["method"] == method and int(r["k"]) == k)) /
                              max(1, sum(1 for r in sc.get("synthetic", [])
                                         if r["method"] == method
                                         and int(r["k"]) == k)), 3)])
    L.append(_md_table(["method", "k", "NEES/n median", "cov68", "cov95",
                        "empirical sd / reported sigma"], rows))
    L.append("")
    rc = sc.get("real_coverage", {})
    L.append(f"Real seed-to-seed coverage of the reported sigma (random subsets): "
             f"68 % nominal -> {_fmt(rc.get('cov68'), 3)}; 95 % nominal -> "
             f"{_fmt(rc.get('cov95'), 3)} (n={rc.get('n')}).  This is a weak check: "
             "the 10 random subsets are not independent draws of the same estimator.")
    L.append("")
    # ---------------- paired ----------------
    L.append("## 13. Paired comparisons (same scenes/seeds)")
    L.append("")
    rows = []
    for p in summary.get("paired", []):
        if int(p["k"]) not in (8, 12, 20, 25):
            continue
        if p["metric"] not in ("fx_abs_max", "baseline_abs_mm", "heldout_rt_mean"):
            continue
        rows.append([p["k"], f"{p['a']} - {p['b']}", p["metric"],
                     _fmt(p["mean_diff"]), _fmt(p["ci_lo"]), _fmt(p["ci_hi"]),
                     _fmt(p["win_fraction"], 2), _fmt(p["p_wilcoxon"], 3)])
    L.append(_md_table(["k", "pair", "metric", "mean diff", "CI lo", "CI hi",
                        "win frac", "Wilcoxon p"], rows))
    L.append("")
    L.append("Negative diff = first method better.  CI is a percentile bootstrap "
             "over paired scenes; p is a two-sided Wilcoxon signed-rank test.")
    L.append("")
    # ---------------- caveats ----------------
    L.append("## 14. Caveats")
    L.append("")
    L.append("1. **CCF is a reference, not truth.**  The vendor corpus can show "
             "agreement and subset stability, not absolute accuracy.  The "
             "CCF-relative fx offset (a few px) is never called an error.")
    L.append("2. **Pitch/scale ambiguity dominates the baseline target.**  A +-0.1 % "
             "pitch error is +-0.159 mm on the 158.6 mm baseline, so 0.05 mm cannot "
             "be established from images alone; it is conditional on the plate pitch.  "
             "The body of the CCF agreement is at the assumed 27.72 mm pitch.")
    L.append("3. **Synthetic model is cleaner than the vendor detector.**  "
             "Distortion is fixed at truth, the plate is exact, noise is iid, and "
             "there is no rolling-shutter/model mismatch; per-view held-out errors on "
             "the real corpus include view-specific model error that synthetic scenes "
             "do not reproduce.")
    L.append("4. **Small real-sample inference.**  5 folds x 4-5 held-out views; the "
             "pooled UCB is over views, and fold-level parameter drift CIs are wide.")
    L.append("5. **Local minima.**  Some synthetic scenes are non-monotone in k "
             "(a k=20 fit can land in a worse local minimum than k=25); this is part "
             "of the estimator, not smoothed away.")
    L.append("6. **Adaptive planner is greedy and batched.**  It refits at the "
             "evaluated counts, not after every single added view; it is not a "
             "globally optimal design.")
    L.append("")
    # ---------------- conclusion ----------------
    L.append("## 15. Conclusion")
    L.append("")
    # derive a concise machine-ish statement
    def kc(method, metric, target):
        for r in summary.get("min_k_combined", {}).get(method, []):
            if r["metric"] == metric and r["target"] == target:
                return r["k_combined"], r["k_synth_ucb"]
        return None, None
    lines = []
    for metric, target, label in TARGETS:
        if metric not in ("fx_abs_max", "baseline_abs_mm", "heldout_rt_mean"):
            continue
        kf, sf = kc("fixed", metric, target)
        ka, sa = kc("adaptive", metric, target)
        lines.append(f"- {label}: fixed k_synth={sf}/combined={kf}; "
                     f"adaptive k_synth={sa}/combined={ka}.")
    L.extend(lines)
    L.append("")
    L.append("`8-12` is defensible only where the table reaches the target at "
             "k<=12 with UCB, i.e. typically the looser fx (1.0 px) and held-out "
             "(0.5 px) targets for a diversity-aware selector; it is **not** "
             "defensible for the fixed EXStar-order stop (the first 8 views cover "
             "only two orientations) nor for the 0.05 mm baseline target.")
    L.append("")
    # ---------------- appendix ----------------
    L.append("## Appendix A. Per-seed random-subset values")
    L.append("")
    recs = summary.get("records", [])
    rows = []
    for k in (8, 12, 20):
        for s in sorted(set(int(r["seed"]) for r in recs
                            if r["domain"] == "synthetic" and r["method"] == "random"
                            and int(r["k"]) == k and r.get("seed") is not None)):
            rr = [r for r in recs if r["domain"] == "synthetic" and r["method"] == "random"
                  and int(r["k"]) == k and int(r["seed"]) == s]
            if not rr:
                continue
            rows.append([k, s, _fmt(np.mean([x["gt"]["fx_abs_max"] for x in rr])),
                         _fmt(np.mean([abs(x["gt"]["baseline_err_mm"]) for x in rr]), 4),
                         _fmt(np.mean([x["heldout"].get("heldout_rt_mean") for x in rr]))])
    L.append(_md_table(["k", "seed", "mean |fx|max", "mean |B err|", "mean held-out"], rows))
    L.append("")
    L.append("## Appendix B. Per-scene adaptive values")
    L.append("")
    rows = []
    for k in (8, 12, 20, 25):
        rr = [r for r in recs if r["domain"] == "synthetic" and r["method"] in
              ("adaptive", "full") and int(r["k"]) == k]
        for r in sorted(rr, key=lambda z: int(z.get("scene", -1))):
            rows.append([r.get("scene"), k, _fmt(r["gt"]["fx_abs_max"]),
                         _fmt(abs(r["gt"]["baseline_err_mm"]), 4),
                         _fmt(r["heldout"].get("heldout_rt_mean")),
                         _fmt(r["gt"]["nees"]["nees_n"], 2)])
    L.append(_md_table(["scene", "k", "|fx|max", "|B err|", "held-out", "NEES/n"], rows))
    L.append("")
    L.append("## Appendix C. Output files")
    L.append("")
    L.append("- `analysis/calibration/audit/viewcount_results.json` — every record, "
             "per-seed/per-scene values, all summary statistics and CIs.")
    L.append("- `analysis/calibration/audit/figures/` — plots.")
    L.append("- `analysis/calibration/audit/viewcount_cache.json` — content-addressed "
             "fit cache keyed by engine hash.")
    L.append("")
    path.write_text("\n".join(L))


if __name__ == "__main__":
    raise SystemExit(main())
