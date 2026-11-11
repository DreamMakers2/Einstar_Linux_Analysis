"""Independent check of the factory stereo extrinsic.

Three quantities, in increasing strength:

1. **Per-frame relative pose.**  Each camera's plate pose is solved
   independently; the pair gives ``X_right = R_rel X_left + t_rel``.  Comparing
   that with the factory value tests the extrinsic, but only through the plate
   model (any error of the plate model cancels, a pitch error scales ``t_rel``).

2. **Held-out cross-camera prediction.**  Take the plate pose from the *left*
   camera, push the board points through the *factory* extrinsic into the right
   camera and compare with the right camera's observed pixels.  The right
   camera's own fit is never used, so this is a genuine prediction; its residual
   is the sharpest statement about the factory extrinsic.

3. **Global correction fit.**  One rigid 6-DoF correction ``(dR, dt)`` applied to
   the factory extrinsic, fitted to all frames at once.  If the held-out residual
   collapses to the level of the individual pose fits, the disagreement is a
   rigid extrinsic offset rather than random error, and the fitted correction is
   the extrinsic this capture supports.

Caveat that the report repeats: the correction absorbs any *differential* bias
between the two cameras' pose estimates (most plausibly the difference between
the factory distortion model and the true lens), so it is an **effective**
extrinsic for this data, not proof that the factory value is wrong.

Note the epipolar residual is a weak test: with a baseline nearly along x, a
small relative rotation about y moves points *along* the epipolar lines, so the
Sampson distance stays at 0.2 px while the full reprojection error is 5 px.
Both numbers are reported so the difference is visible.
"""

from __future__ import annotations

from typing import Sequence

import cv2
import numpy as np
from scipy.optimize import least_squares

from calib import geometry


def heldout_residual(R_src, t_src, R_rig, t_rig, K_dst, kc_dst, obj, uv_dst) -> dict:
    """Project the board seen by one camera into the other through the rig."""
    R = np.asarray(R_rig, float) @ np.asarray(R_src, float)
    t = np.asarray(R_rig, float) @ np.asarray(t_src, float) + np.asarray(t_rig, float)
    pred = geometry.project_world(K_dst, kc_dst, R, t, obj)
    e = np.linalg.norm(pred - np.asarray(uv_dst, float), axis=1)
    return {"rms_px": float(np.sqrt(np.mean(e ** 2))), "max_px": float(e.max()),
            "p50_px": float(np.median(e)), "n": int(len(e))}


def fit_correction(frames: Sequence[dict], R_rig, t_rig, K, kc,
                   max_nfev: int = 200) -> dict:
    """Fit one rigid correction to the factory extrinsic over all frames.

    ``frames`` items must provide ``obj`` (Nx3 board points), ``uv`` (Nx2
    observed pixels in the *right* camera), ``R1``/``t1`` (left-camera plate
    pose).  Parameterisation: ``R' = Rodrigues(dr) @ R_rig``,
    ``t' = t_rig + dt``.
    """
    data = [f for f in frames if f.get("obj") is not None and len(f["obj"]) >= 8]
    if len(data) < 3:
        return {"ok": False, "reason": "too few frames"}
    R_rig = np.asarray(R_rig, float)
    t_rig = np.asarray(t_rig, float)

    def resid(p):
        Rc = geometry.rodrigues(p[:3]) @ R_rig
        tc = t_rig + p[3:]
        out = []
        for f in data:
            R = Rc @ f["R1"]
            t = Rc @ f["t1"] + tc
            out.append((geometry.project_world(K, kc, R, t, f["obj"]) - f["uv"]).ravel())
        return np.concatenate(out)

    r0 = resid(np.zeros(6))
    per_point0 = np.linalg.norm(r0.reshape(-1, 2), axis=1)
    sol = least_squares(resid, np.zeros(6), method="lm", xtol=1e-12, ftol=1e-12,
                        max_nfev=max_nfev)
    r1 = resid(sol.x)
    per_point1 = np.linalg.norm(r1.reshape(-1, 2), axis=1)
    Rc = geometry.rodrigues(sol.x[:3]) @ R_rig
    tc = t_rig + sol.x[3:]
    dR = Rc @ R_rig.T
    dof = max(len(r1) - 6, 1)
    try:
        cov = np.linalg.inv(sol.jac.T @ sol.jac) * float(np.sum(r1 ** 2) / dof)
        sigma = np.sqrt(np.diag(cov))
    except np.linalg.LinAlgError:
        sigma = np.full(6, np.nan)
    return {
        "ok": True,
        "n_frames": len(data),
        "n_points": int(len(r1) // 2),
        "rms_before_px": float(np.sqrt(np.mean(per_point0 ** 2))),
        "rms_after_px": float(np.sqrt(np.mean(per_point1 ** 2))),
        "max_after_px": float(per_point1.max()),
        "p50_after_px": float(np.median(per_point1)),
        "dR_deg": float(np.degrees(np.arccos(
            np.clip((np.trace(dR) - 1.0) / 2.0, -1.0, 1.0)))),
        "dR_rvec_deg": [float(v) for v in np.degrees(cv2.Rodrigues(dR)[0].ravel())],
        "dt_mm": [float(v) for v in sol.x[3:]],
        "dt_norm_mm": float(np.linalg.norm(sol.x[3:])),
        "R_corrected": [float(v) for v in Rc.ravel()],
        "t_corrected": [float(v) for v in tc.ravel()],
        "baseline_corrected_mm": float(np.linalg.norm(tc)),
        "sigma_rot_deg": float(np.degrees(np.linalg.norm(sigma[:3]))),
        "sigma_t_mm": float(np.linalg.norm(sigma[3:])),
        "converged": bool(sol.success),
    }


def frames_for_correction(records: Sequence[dict], R_fac, t_fac,
                          obj_of, uv_index: dict) -> list[dict]:
    """Build the inputs of :func:`fit_correction` from per-frame records."""
    out = []
    for fr in records:
        p1 = fr["pose"].get("1")
        d0 = fr["detect"].get("0") or {}
        if not p1 or not d0.get("detected"):
            continue
        uv0 = d0.get("uv") or []
        px0 = d0.get("pixels") or []
        if len(uv0) < 8:
            continue
        keys = [tuple(k) for k in uv0]
        idx = [uv_index.get((float(u), float(v))) for (u, v) in keys]
        keep = [i for i in range(len(idx)) if idx[i] is not None]
        if len(keep) < 8:
            continue
        out.append({
            "obj": np.asarray([obj_of[idx[i]] for i in keep], float),
            "uv": np.asarray([px0[i] for i in keep], float),
            "R1": np.asarray(p1["R"], float).reshape(3, 3),
            "t1": np.asarray(p1["t"], float),
        })
    return out
