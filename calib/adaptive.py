"""Fisher-information viewpoint selection and uncertainty-driven stopping.

EXStar asks for a fixed 5 x 5 grid (5 board orientations x 5 distance bands).
This module replaces that with a greedy A/D-optimal selection over a candidate
pose pool, using the Gauss-Newton information from the current calibration.

The rig parameters are the 27 intrinsic values (3 cameras x 9) and the 12
extrinsic values (right and tex).  A candidate view adds six nuisance
parameters (board pose in the left frame); the gain is the reduction of the
marginal rig covariance after eliminating the candidate view by its Schur
complement.  This is the standard Fisher-information view-selection step and
is independent of any vendor implementation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

import numpy as np

from . import plate
from .calibrate import CAMERAS, CalibrationResult, CameraIntrinsics
from .geometry import project, rvec_from_R, rodrigues

NRIG = 39  # 27 intrinsics + 12 extrinsics


@dataclass
class ViewCandidate:
    distance_mm: float
    tilt_x_deg: float
    tilt_y_deg: float
    roll_deg: float
    R: np.ndarray
    t: np.ndarray
    label: str = ""

    @property
    def pose(self):
        return self.R, self.t


def _rx(a):
    c, s = np.cos(a), np.sin(a); return np.array([[1, 0, 0], [0, c, -s], [0, s, c]], float)


def _ry(a):
    c, s = np.cos(a), np.sin(a); return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]], float)


def _rz(a):
    c, s = np.cos(a), np.sin(a); return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]], float)


def board_pose(distance_mm: float, tilt_x_deg: float, tilt_y_deg: float,
               roll_deg: float = 0.0,
               lateral_mm: tuple[float, float] = (0.0, 0.0)) -> tuple[np.ndarray, np.ndarray]:
    """Board pose in the left camera frame (fronto-parallel at zero tilt)."""
    R0 = np.diag([1.0, -1.0, -1.0])
    R = _rz(np.radians(roll_deg)) @ _rx(np.radians(tilt_x_deg)) \
        @ _ry(np.radians(tilt_y_deg)) @ R0
    return R, np.array([lateral_mm[0], lateral_mm[1], distance_mm], float)


def candidate_pool(pitch: float = plate.DEFAULT_PITCH_MM,
                   distances: Iterable[float] = (240, 320, 400, 480, 560),
                   tilts: Iterable[float] = (-24, -12, 0, 12, 24),
                   rolls: Iterable[float] = (0, 90),
                   lateral: Iterable[float] = (0.0,)) -> list[ViewCandidate]:
    pool = []
    for d in distances:
        for tx in tilts:
            for ty in tilts:
                for r in rolls:
                    for lx in lateral:
                        R, t = board_pose(d, tx, ty, r, (lx, 0.0))
                        pool.append(ViewCandidate(float(d), float(tx), float(ty),
                                                  float(r), R, t,
                                                  f"d{int(d)}_x{int(tx)}_y{int(ty)}_r{int(r)}"))
    return pool


# --------------------------------------------------------------------------
# rig vector / covariance
# --------------------------------------------------------------------------

def rig_vector(result: CalibrationResult) -> np.ndarray:
    x = np.zeros(NRIG)
    for i, c in enumerate(CAMERAS):
        x[9 * i:9 * i + 9] = result.cameras[c].as_vec()
    for i, c in enumerate(("right", "tex")):
        R, t = result.extrinsics[c]
        x[27 + 6 * i:27 + 6 * i + 6] = np.concatenate([rvec_from_R(R), t])
    return x


def free_rig_mask(result: CalibrationResult) -> np.ndarray:
    """Boolean mask of the free rig parameters (fixed parameters excluded)."""
    fm = result.info.get("free_mask")
    if fm is None:
        return np.ones(NRIG, bool)
    return np.asarray(fm[:NRIG], bool)


def rig_covariance(result: CalibrationResult) -> np.ndarray:
    """Marginal covariance of the 39 rig parameters.

    The board poses are nuisance parameters: their uncertainty must be
    integrated out, not conditioned on.  The marginal covariance is the
    top-left block of the joint covariance.  The old expression
    ``C_rr - C_rv C_vv^-1 C_vr`` is the *conditional* covariance given the
    poses and is therefore too small (a classic uncertainty-audit failure).
    """
    cov = result.covariance
    if cov is None:
        return np.eye(NRIG) * 1e6
    return np.array(cov[:NRIG, :NRIG], copy=True)


# --------------------------------------------------------------------------
# candidate Jacobian
# --------------------------------------------------------------------------

def _split(x: np.ndarray):
    cams = {}
    for i, c in enumerate(CAMERAS):
        v = x[9 * i:9 * i + 9]
        cams[c] = CameraIntrinsics(float(v[0]), float(v[1]), float(v[2]),
                                   float(v[3]), v[4:9].copy())
    extr = {"left": (np.eye(3), np.zeros(3))}
    for i, c in enumerate(("right", "tex")):
        v = x[27 + 6 * i:27 + 6 * i + 6]
        extr[c] = (rodrigues(v[:3]), v[3:6].copy())
    return cams, extr


def candidate_jacobian(x_rig: np.ndarray, R: np.ndarray, t: np.ndarray,
                       pitch: float = plate.DEFAULT_PITCH_MM,
                       eps: float = 1e-6) -> Optional[np.ndarray]:
    """(M, 45) Jacobian of predicted pixel coordinates wrt [rig | view]."""
    obj = plate.board_points_mm(pitch)
    cams, extr = _split(x_rig)

    def pixels(xr, view6):
        cams2, extr2 = _split(xr)
        Rv = rodrigues(view6[:3]); tv = view6[3:6]
        out = []
        for c in CAMERAS:
            Rc, tc = extr2[c]
            Xc = obj @ (Rc @ Rv).T + (Rc @ tv + tc)
            if np.any(Xc[:, 2] <= 1.0):
                return None
            out.append(project(cams2[c].K, cams2[c].kc, Xc).ravel())
        return np.concatenate(out)

    v0 = np.concatenate([rvec_from_R(R), t])
    f0 = pixels(x_rig, v0)
    if f0 is None:
        return None
    J = np.zeros((len(f0), NRIG + 6))
    for j in range(NRIG):
        h = eps * max(1.0, abs(x_rig[j]))
        xp = x_rig.copy(); xp[j] += h
        fp = pixels(xp, v0)
        if fp is None:
            return None
        J[:, j] = (fp - f0) / h
    for j in range(6):
        h = eps * max(1.0, abs(v0[j]))
        vp = v0.copy(); vp[j] += h
        fp = pixels(x_rig, vp)
        if fp is None:
            return None
        J[:, NRIG + j] = (fp - f0) / h
    return J


def rig_information(result: CalibrationResult) -> np.ndarray:
    """Marginal information of the rig parameters (board poses eliminated).

    Uses the stored full information matrix when available; this is numerically
    preferable to inverting an ill-conditioned marginal covariance.
    """
    free = free_rig_mask(result)
    if getattr(result, "marginal_information", None) is not None:
        return np.array(result.marginal_information, copy=True)
    H = result.information
    if H is None:
        C = rig_covariance(result)
        out = np.zeros((NRIG, NRIG))
        out[np.ix_(free, free)] = np.linalg.pinv(C[np.ix_(free, free)], rcond=1e-10)
        return out
    fm = np.asarray(result.info.get("free_mask", np.ones(H.shape[0], bool)), bool)
    rig = np.where(fm[:NRIG])[0]
    view = np.where(fm[NRIG:])[0] + NRIG
    if len(view) == 0:
        schur = H[np.ix_(rig, rig)]
    else:
        Hrr = H[np.ix_(rig, rig)]
        Hrv = H[np.ix_(rig, view)]
        Hvv = H[np.ix_(view, view)]
        schur = Hrr - Hrv @ np.linalg.pinv(Hvv, rcond=1e-10) @ Hrv.T
    out = np.zeros((NRIG, NRIG))
    out[np.ix_(rig, rig)] = schur
    return out


def _inv_spd(A: np.ndarray, floor_rel: float = 1e-12) -> np.ndarray:
    """Symmetric positive-definite inverse with an eigenvalue floor."""
    A = 0.5 * (A + A.T)
    w, V = np.linalg.eigh(A)
    wmax = max(float(w.max()), 1e-300)
    w = np.clip(w, floor_rel * wmax, None)
    return (V / w) @ V.T


def information_gain(cov_rig: np.ndarray, J: np.ndarray,
                     free: np.ndarray | None = None,
                     prior_info: np.ndarray | None = None) -> tuple[float, np.ndarray]:
    """Marginal information gain of one candidate view.

    ``cov_rig`` must be the *marginal* rig covariance.  The candidate view's
    six pose parameters are eliminated by their Schur complement.  The prior
    information is ``inv(C_rr)`` (not the raw J^T J, whose scale differs by the
    residual variance); using J^T J directly made A-optimal gains negative.
    """
    free = np.ones(NRIG, bool) if free is None else np.asarray(free, bool)
    idx = np.where(free)[0]
    prior = cov_rig[np.ix_(idx, idx)]
    H0 = _inv_spd(prior) if prior_info is None else prior_info[np.ix_(idx, idx)]
    Jr = J[:, :NRIG][:, idx]
    Jv = J[:, NRIG:]
    Hv = Jv.T @ Jv
    try:
        Hv_inv = np.linalg.pinv(Hv, rcond=1e-10)
    except np.linalg.LinAlgError:
        return -np.inf, cov_rig
    H_add = Jr.T @ Jr - Jr.T @ Jv @ Hv_inv @ Jv.T @ Jr
    # clip numerical negative directions of the Schur complement
    wa, Va = np.linalg.eigh(0.5 * (H_add + H_add.T))
    H_add = (Va * np.clip(wa, 0.0, None)) @ Va.T
    cov1_free = _inv_spd(H0 + H_add)
    cov1 = np.zeros_like(cov_rig)
    cov1[np.ix_(idx, idx)] = cov1_free
    gain = float(np.trace(prior) - np.trace(cov1_free))
    return gain, cov1


def select_next(candidates: list[ViewCandidate], x_rig: np.ndarray,
                cov_rig: np.ndarray, pitch: float,
                used: set[str] | None = None,
                free: np.ndarray | None = None) -> Optional[tuple[ViewCandidate, float, np.ndarray]]:
    used = used or set()
    best = None
    for cand in candidates:
        if cand.label in used:
            continue
        J = candidate_jacobian(x_rig, cand.R, cand.t, pitch)
        if J is None:
            continue
        gain, cov1 = information_gain(cov_rig, J, free)
        if best is None or gain > best[1]:
            best = (cand, gain, cov1)
    return best


def _diverse_pool(pool: list[ViewCandidate], chosen: list[ViewCandidate],
                  max_per_distance: int = 2, max_per_tilt_sign: int = 2):
    dcount = {}; scount = {}
    for c in chosen:
        db = round(c.distance_mm / 40.0)
        dcount[db] = dcount.get(db, 0) + 1
        sx = 0 if abs(c.tilt_x_deg) < 4 else (1 if c.tilt_x_deg > 0 else -1)
        sy = 0 if abs(c.tilt_y_deg) < 4 else (1 if c.tilt_y_deg > 0 else -1)
        scount[(sx, sy)] = scount.get((sx, sy), 0) + 1
    out = []
    for c in pool:
        db = round(c.distance_mm / 40.0)
        sx = 0 if abs(c.tilt_x_deg) < 4 else (1 if c.tilt_x_deg > 0 else -1)
        sy = 0 if abs(c.tilt_y_deg) < 4 else (1 if c.tilt_y_deg > 0 else -1)
        if dcount.get(db, 0) >= max_per_distance:
            continue
        if scount.get((sx, sy), 0) >= max_per_tilt_sign:
            continue
        out.append(c)
    return out if out else pool


def plan_views(candidates: list[ViewCandidate], result: CalibrationResult,
               pitch: float, n: int = 8, diversity: bool = True) -> dict:
    x_rig = rig_vector(result)
    cov = rig_covariance(result)
    free = free_rig_mask(result)
    chosen = []
    for _ in range(n):
        search = _diverse_pool(candidates, chosen) if diversity else candidates
        best = select_next(search, x_rig, cov, pitch,
                           used={c.label for c in chosen}, free=free)
        if best is None:
            break
        cand, gain, cov = best
        chosen.append(cand)
        if gain <= 0:
            break
    sd = np.sqrt(np.maximum(np.diag(cov), 0.0))
    return {"selected": [c.label for c in chosen], "candidates": chosen,
            "sigma": _sigma_dict(sd), "trace": float(np.trace(cov))}


def _sigma_dict(sd: np.ndarray) -> dict:
    names = []
    for c in CAMERAS:
        for f in ("fx", "fy", "cx", "cy", "k1", "k2", "p1", "p2", "k3"):
            names.append(f"{c}.{f}")
    for c in ("right", "tex"):
        for f in ("rx", "ry", "rz", "tx", "ty", "tz"):
            names.append(f"{c}.{f}")
    return {n: float(s) for n, s in zip(names, sd)}


# The conservative, explainable stopping rule lives in quality.py; re-export
# it so existing callers keep working.
from .quality import stopping_report  # noqa: E402,F401
