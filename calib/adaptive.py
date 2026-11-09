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


def rig_covariance(result: CalibrationResult) -> np.ndarray:
    cov = result.covariance
    if cov is None:
        return np.eye(NRIG) * 1e6
    rr = cov[:NRIG, :NRIG]
    rv = cov[:NRIG, NRIG:]
    vv = cov[NRIG:, NRIG:]
    if vv.size:
        try:
            return rr - rv @ np.linalg.pinv(vv, rcond=1e-12) @ rv.T
        except np.linalg.LinAlgError:
            return rr
    return rr


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


def information_gain(cov_rig: np.ndarray, J: np.ndarray) -> tuple[float, np.ndarray]:
    Jr = J[:, :NRIG]
    Jv = J[:, NRIG:]
    Hv = Jv.T @ Jv
    try:
        Hv_inv = np.linalg.pinv(Hv, rcond=1e-12)
    except np.linalg.LinAlgError:
        return -np.inf, cov_rig
    H_add = Jr.T @ Jr - Jr.T @ Jv @ Hv_inv @ Jv.T @ Jr
    H0 = np.linalg.pinv(cov_rig, rcond=1e-12)
    H1 = H0 + H_add
    cov1 = np.linalg.pinv(H1, rcond=1e-12)
    return float(np.trace(cov_rig) - np.trace(cov1)), cov1


def select_next(candidates: list[ViewCandidate], x_rig: np.ndarray,
                cov_rig: np.ndarray, pitch: float,
                used: set[str] | None = None) -> Optional[tuple[ViewCandidate, float, np.ndarray]]:
    used = used or set()
    best = None
    for cand in candidates:
        if cand.label in used:
            continue
        J = candidate_jacobian(x_rig, cand.R, cand.t, pitch)
        if J is None:
            continue
        gain, cov1 = information_gain(cov_rig, J)
        if best is None or gain > best[1]:
            best = (cand, gain, cov1)
    return best


def plan_views(candidates: list[ViewCandidate], result: CalibrationResult,
               pitch: float, n: int = 8) -> dict:
    x_rig = rig_vector(result)
    cov = rig_covariance(result)
    chosen = []
    for _ in range(n):
        best = select_next(candidates, x_rig, cov, pitch,
                           used={c.label for c in chosen})
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


def stopping_report(result: CalibrationResult,
                    max_sigma_fx_px: float = 0.35,
                    max_sigma_baseline_mm: float = 0.08) -> dict:
    cov = rig_covariance(result)
    sd = np.sqrt(np.maximum(np.diag(cov), 0.0))
    d = _sigma_dict(sd)
    checks = {}
    for c in CAMERAS:
        checks[f"{c}.fx"] = (d[f"{c}.fx"], max_sigma_fx_px)
        checks[f"{c}.fy"] = (d[f"{c}.fy"], max_sigma_fx_px)
    # baseline sigma: first-order propagation through the rig parameters
    Rr = result.extrinsics["right"][0]
    tr = result.extrinsics["right"][1]
    # d|t|/dt = t/|t|
    t = tr - Rr @ np.zeros(3)
    n = t / max(np.linalg.norm(t), 1e-12)
    idx = 27 + 3  # right translation block starts at 27+3
    b_sigma = float(np.sqrt(max(n @ cov[idx:idx + 3, idx:idx + 3] @ n, 0.0)))
    ok = all(v[0] <= v[1] for v in checks.values()) and b_sigma <= max_sigma_baseline_mm
    return {"ok": bool(ok), "baseline_mm": float(np.linalg.norm(t)),
            "baseline_sigma_mm": b_sigma,
            "checks": {k: {"value": float(v[0]), "limit": float(v[1])}
                       for k, v in checks.items()}}
