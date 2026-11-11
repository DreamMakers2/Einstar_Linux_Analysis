"""Parameter-observability probes for a sweep.

Two questions that a residual number alone cannot answer:

*Focal length.*  On a planar target the reprojection residual is almost exactly
invariant to a uniform focal scale: scaling ``fx, fy`` by ``s`` and the depth by
``1/s`` leaves the projection unchanged.  The probe refits every sampled frame
at several focal scales and reports the depth and the residual for each.  If the
residual does not move, the sweep does not constrain the focal length and the
depth is only known relative to it.

*Skew.*  The factory profile stores ``K[0,1]`` as an **absolute pixel** value
(``tools/calib/export_profile.py`` writes ``K[0,1]``, and the C++ backend builds
``Matx33d(fx, skew, cx, 0, fy, cy, 0, 0, 1)``), while
``calib.calibrate.CameraIntrinsics.K`` interprets its own ``skew`` field as a
normalised ``alpha_c`` and multiplies by ``fx``.  The probe fits with both
readings and reports the residual, so the convention in use is visible in the
output instead of assumed.
"""

from __future__ import annotations

from typing import Sequence

import numpy as np

from calib import geometry


def _samples(records: Sequence[dict], every: int) -> list[dict]:
    return [r for r in records[:: max(1, int(every))]
            if r.get("detect") and r["detect"].get("1")]


def _fit(det: dict, obj: np.ndarray, uv_index: dict, K, kc):
    keys = [tuple(k) for k in det["uv"]]
    idx = [uv_index.get((float(u), float(v))) for (u, v) in keys]
    keep = [i for i in range(len(idx)) if idx[i] is not None]
    if len(keep) < 8:
        return None
    o = np.asarray([obj[idx[i]] for i in keep], float)
    im = np.asarray([det["pixels"][i] for i in keep], float)
    p = geometry.solve_pnp(o, im, K, kc)
    if p is None:
        return None
    e = geometry.reprojection_errors(K, kc, p.R, p.t, o, im)
    return float(np.sqrt(np.mean(e ** 2))), np.asarray(p.t, float)


def focal_scale_probe(records: Sequence[dict], base_K: np.ndarray, kc: np.ndarray,
                      obj: np.ndarray, uv_index: dict, cam: str = "1",
                      scales: Sequence[float] = (0.97, 0.985, 1.0, 1.015, 1.03),
                      every: int = 7) -> dict:
    """Refit at several uniform focal scales; report depth and residual."""
    samples = _samples(records, every)
    rows = []
    for s in scales:
        K = np.array(base_K, float).copy()
        K[0, 0] *= s
        K[1, 1] *= s
        rms, z, x = [], [], []
        for r in samples:
            det = r["detect"].get(cam)
            if not det:
                continue
            out = _fit(det, obj, uv_index, K, kc)
            if out is None:
                continue
            rms.append(out[0])
            z.append(out[1][2])
            x.append(out[1][0])
        if rms:
            rows.append({"scale": float(s),
                         "rms_px_p50": float(np.median(rms)),
                         "z_mm_p50": float(np.median(z)),
                         "x_mm_p50": float(np.median(x)),
                         "n": len(rms)})
    if not rows:
        return {"ok": False}
    base = next((r for r in rows if abs(r["scale"] - 1.0) < 1e-9), rows[0])
    out = {"ok": True, "n_frames": rows[0]["n"], "cam": cam, "rows": rows,
           "z_rel_change": [r["z_mm_p50"] / base["z_mm_p50"] - 1.0 for r in rows],
           "rms_delta_px": [r["rms_px_p50"] - base["rms_px_p50"] for r in rows],
           "z_scale_exponent": [float(np.log(r["z_mm_p50"] / base["z_mm_p50"]) /
                                      np.log(r["scale"] / base["scale"]))
                                for r in rows if abs(r["scale"] - base["scale"]) > 1e-9]}
    return out


def skew_convention_probe(records: Sequence[dict], cam_profile, kc: np.ndarray,
                          obj: np.ndarray, uv_index: dict, cam: str = "1",
                          every: int = 7) -> dict:
    """Fit with the profile skew read as absolute pixels vs as normalised alpha_c."""
    samples = _samples(records, every)
    rows = []
    for mode, value in (("absolute_px", float(cam_profile.skew)),
                        ("zero", 0.0),
                        ("normalised_alpha_c", float(cam_profile.skew) * cam_profile.fx)):
        K = np.array(cam_profile.K, float).copy()
        K[0, 1] = value
        rms = []
        for r in samples:
            det = r["detect"].get(cam)
            if not det:
                continue
            out = _fit(det, obj, uv_index, K, kc)
            if out is not None:
                rms.append(out[0])
        if rms:
            rows.append({"mode": mode, "K01": value,
                         "rms_px_p50": float(np.median(rms)),
                         "rms_px_max": float(np.max(rms)), "n": len(rms)})
    return {"ok": bool(rows), "rows": rows, "cam": cam}
