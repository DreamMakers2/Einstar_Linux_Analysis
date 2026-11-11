"""Repeatability, pose-noise floor and coverage statistics.

Three different quantities are computed and kept strictly separate, because
they answer different questions and are easy to confuse:

``adjacent``      frame-to-frame pose change at 10 Hz.  During a sweep this is
                  dominated by real motion, so it is *not* a noise floor; it is
                  the resolution limit of the trajectory.
``noise floor``   the high-frequency part of the pose sequence that a smooth
                  trajectory cannot explain: per frame, a quadratic is fitted to
                  a short window of the sequence and the residual at the centre
                  frame is kept.  Robust sigma of that residual is the best
                  available estimate of the per-frame pose noise (it includes
                  detector noise, distortion-model error and hand tremor).
``repeatability`` the pose difference between frames from **different legs**
                  that revisit the same pose.  This is the end-to-end
                  reproducibility of a viewpoint during a real sweep, i.e. what
                  a reconstruction would actually see.

A sweep is a 1-D curve through pose space: revisiting a pose on another leg is
*not* an independent geometric viewpoint, it is the same viewing direction
reached later.  The numbers here quantify that; they do not turn it into
diversity.
"""

from __future__ import annotations

from typing import Iterable, Sequence

import cv2
import numpy as np


def _chordal_mean_rotation(Rs: np.ndarray) -> np.ndarray:
    U, _, Vt = np.linalg.svd(np.sum(np.asarray(Rs, float), axis=0))
    R = U @ Vt
    if np.linalg.det(R) < 0:
        U[:, -1] *= -1
        R = U @ Vt
    return R


def rotation_angle_deg(Ra, Rb) -> float:
    A = np.asarray(Ra, float).reshape(3, 3)
    B = np.asarray(Rb, float).reshape(3, 3)
    c = (np.trace(A @ B.T) - 1.0) / 2.0
    return float(np.degrees(np.arccos(np.clip(c, -1.0, 1.0))))


def local_rotvec(recs: Sequence[dict]) -> tuple[np.ndarray, np.ndarray]:
    """Rotation vectors in degrees relative to the chordal mean rotation.

    A rotation near 180 deg (this capture holds the plate upside down) makes the
    raw ``cv2.Rodrigues`` vector ill-conditioned; expressing every pose relative
    to the mean rotation keeps the tangent vector small and well behaved.
    """
    Rs = [np.asarray(r["R"], float).reshape(3, 3) for r in recs]
    Rref = _chordal_mean_rotation(np.asarray(Rs))
    out = np.zeros((len(Rs), 3))
    for i, R in enumerate(Rs):
        rv = cv2.Rodrigues(R @ Rref.T)[0].ravel()
        out[i] = np.degrees(rv)
    return out, Rref


def pair_delta(a: dict, b: dict, rv_a: np.ndarray | None = None,
               rv_b: np.ndarray | None = None) -> dict:
    ta = np.asarray(a["t"], float)
    tb = np.asarray(b["t"], float)
    d_t = float(np.linalg.norm(ta - tb))
    d_rot = rotation_angle_deg(a["R"], b["R"])
    out = {"dt_mm": d_t, "drot_deg": d_rot,
           "dt_xyz_mm": (tb - ta).astype(float).tolist(),
           "dx_mm": float(abs(tb[0] - ta[0])),
           "dy_mm": float(abs(tb[1] - ta[1])),
           "dz_mm": float(abs(tb[2] - ta[2]))}
    if rv_a is not None and rv_b is not None:
        d = np.asarray(rv_b, float) - np.asarray(rv_a, float)
        out["drot_xyz_deg"] = d.tolist()
    return out


#: reference scales used only to rank candidate matches (not a threshold)
RANK_MM = 5.0
RANK_DEG = 1.0


def cross_leg_nearest(recs: Sequence[dict], legs: Sequence[tuple],
                      max_mm: float = float("inf"),
                      max_deg: float = float("inf")) -> list[dict]:
    """Nearest pose match for every frame among frames on a *different* leg.

    Candidates are ranked by ``dt/RANK_MM + drot/RANK_DEG`` and then filtered by
    ``max_mm``/``max_deg`` when finite.  Pairing across legs is what makes the
    difference a *repeatability* measurement rather than a motion measurement.
    """
    n = len(recs)
    leg_of = np.full(n, -1, int)
    for li, (a, b, _d) in enumerate(legs):
        leg_of[a:b + 1] = li
    t = np.asarray([r["t"] for r in recs], float)
    rv, _ = local_rotvec(recs)
    out = []
    for i in range(n):
        best = None
        for j in range(n):
            if leg_of[j] == leg_of[i]:
                continue
            dt = float(np.linalg.norm(t[i] - t[j]))
            dr = rotation_angle_deg(recs[i]["R"], recs[j]["R"])
            if dt > max_mm or dr > max_deg:
                continue
            score = dt / RANK_MM + dr / RANK_DEG
            if best is None or score < best[0]:
                best = (score, j, dt, dr)
        if best is not None:
            _s, j, dt, dr = best
            out.append({"i": int(i), "j": int(j),
                        "gi": int(recs[i]["group"]), "gj": int(recs[j]["group"]),
                        "leg_i": int(leg_of[i]), "leg_j": int(leg_of[j]),
                        "dt_mm": dt, "drot_deg": dr})
    return out


def adjacent_pairs(recs: Sequence[dict]) -> list[dict]:
    out = []
    for i in range(1, len(recs)):
        out.append(pair_delta(recs[i - 1], recs[i]))
    return out


def quadratic_noise_floor(recs: Sequence[dict], half_window: int = 2) -> dict:
    """Robust high-frequency pose noise per frame (local quadratic detrend)."""
    n = len(recs)
    if n < 2 * half_window + 3:
        return {"ok": False, "reason": "too few frames"}
    t = np.asarray([r["t"] for r in recs], float)
    rv, _ = local_rotvec(recs)
    tt = np.arange(n, dtype=float)
    resid_t = np.zeros_like(t)
    resid_r = np.zeros_like(rv)
    for i in range(n):
        a = max(0, i - half_window)
        b = min(n, i + half_window + 1)
        xs = tt[a:b] - tt[i]
        A = np.vstack([np.ones_like(xs), xs, xs * xs]).T
        for k in range(3):
            y = t[a:b, k]
            coef, *_ = np.linalg.lstsq(A, y, rcond=None)
            resid_t[i, k] = y[i - a] - coef[0]
            y = rv[a:b, k]
            coef, *_ = np.linalg.lstsq(A, y, rcond=None)
            resid_r[i, k] = y[i - a] - coef[0]

    def robust_sigma(v):
        v = np.asarray(v, float)
        med = float(np.median(v))
        return float(1.4826 * np.median(np.abs(v - med)))

    per_t = [robust_sigma(resid_t[:, k]) for k in range(3)]
    per_r = [robust_sigma(resid_r[:, k]) for k in range(3)]
    return {
        "ok": True,
        "half_window": int(half_window),
        "n_frames": int(n),
        "t_mm": {"x": per_t[0], "y": per_t[1], "z": per_t[2],
                 "norm": float(np.sqrt(np.sum(np.square(per_t))))},
        "rot_deg": {"rx": per_r[0], "ry": per_r[1], "rz": per_r[2],
                    "norm": float(np.sqrt(np.sum(np.square(per_r))))},
        "note": ("robust sigma (1.4826*MAD) of the residual of a local quadratic "
                 "fit; a smooth trajectory is removed, so this is the per-frame "
                 "pose noise including detector and hand-tremor effects"),
    }


def summarise(values: Iterable[float], prefix: str = "") -> dict:
    v = np.asarray([x for x in values if x is not None and np.isfinite(x)], float)
    if v.size == 0:
        return {f"{prefix}n": 0}
    q = np.percentile(v, [0, 5, 50, 90, 95, 99, 100])
    out = {f"{prefix}n": int(v.size),
           f"{prefix}min": round(float(q[0]), 4),
           f"{prefix}p05": round(float(q[1]), 4),
           f"{prefix}p50": round(float(q[2]), 4),
           f"{prefix}p90": round(float(q[3]), 4),
           f"{prefix}p95": round(float(q[4]), 4),
           f"{prefix}p99": round(float(q[5]), 4),
           f"{prefix}max": round(float(q[6]), 4),
           f"{prefix}mean": round(float(v.mean()), 4)}
    if v.size > 1:
        out[f"{prefix}std"] = round(float(v.std(ddof=1)), 4)
    return out
