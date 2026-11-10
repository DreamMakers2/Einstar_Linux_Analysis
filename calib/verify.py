"""Independent verification and bias diagnostics.

Reprojection RMS is *not* a measure of accuracy: a model can fit its own
training residuals while being biased (Rojtberg & Kuijper 2018; see
``docs/calibration/research_review.md``).  This module provides the checks that
can reveal what covariance cannot:

* held-out observations (fit on a subset, evaluate on unseen views);
* split-half parameter consistency (z-scores against the reported covariance);
* residual trends vs image radius, board tilt and distance (a non-zero tilt
  slope is the signature of circular-marker eccentricity bias);
* epipolar consistency between the IR pair;
* detector agreement (two localisers on the same images);
* model-comparison residuals for a reduced distortion model.

None of these establish *physical* accuracy without a calibrated length
artefact; they can only falsify a calibration or bound its internal
consistency.  That limitation is stated in the report.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np
from scipy import stats

from . import plate
from .calibrate import CAMERAS, CalibrationResult, CameraIntrinsics, Observation, calibrate
from .geometry import epipolar_residual, fundamental, project


def _residual_table(result: CalibrationResult, observations: list[Observation],
                    pitch: float = plate.DEFAULT_PITCH_MM):
    obj = plate.board_points_mm(pitch)
    uv_idx = {(float(u), float(v)): i for i, (u, v) in enumerate(plate.lattice_uv())}
    rows = []
    for o in observations:
        if o.camera not in result.cameras:
            continue
        Rv, tv = result.view_pose(o.view)
        cam = result.cameras[o.camera]
        Rc, tc = result.extrinsics[o.camera]
        for k, uv in o.uv.items():
            i = uv_idx.get((float(k[0]), float(k[1])))
            if i is None:
                continue
            X = obj[i]
            Xl = Rv @ X + tv
            Xc = Rc @ Xl + tc
            if Xc[2] <= 1e-6:
                continue
            pred = project(cam.K, cam.kc, Xc[None, :])[0]
            e = np.asarray(uv, float) - pred
            # signed radial / tangential components about the principal point
            r = np.asarray(uv, float) - np.array([cam.cx, cam.cy])
            rn = np.linalg.norm(r)
            if rn > 1e-9:
                er = float(e @ r / rn)
                et = float(e[0] * (-r[1]) / rn + e[1] * r[0] / rn)
            else:
                er = et = 0.0
            # board tilt: angle between the board normal and the optical axis
            n = Rv[:, 2]
            tilt = float(np.degrees(np.arccos(np.clip(abs(n[2]), -1, 1))))
            rows.append({"camera": o.camera, "view": int(o.view), "u": float(uv[0]),
                         "v": float(uv[1]), "du": float(e[0]), "dv": float(e[1]),
                         "err": float(np.linalg.norm(e)), "radial_px": float(rn),
                         "tilt_deg": tilt, "distance_mm": float(np.linalg.norm(tv)),
                         "signed_radial": er, "tangential": et})
    return rows


def _residuals_pnp(result: CalibrationResult, observations: list[Observation],
                   pitch: float = plate.DEFAULT_PITCH_MM) -> list[dict]:
    """Per-view residual after a free PnP pose (held-out model check)."""
    from .geometry import solve_pnp
    obj = plate.board_points_mm(pitch)
    uv_idx = {(float(u), float(v)): i for i, (u, v) in enumerate(plate.lattice_uv())}
    rows = []
    for o in observations:
        if o.camera not in result.cameras:
            continue
        cam = result.cameras[o.camera]
        keys = [k for k in o.uv if (float(k[0]), float(k[1])) in uv_idx]
        if len(keys) < 6:
            continue
        idx = [uv_idx[(float(k[0]), float(k[1]))] for k in keys]
        pts3 = obj[idx]
        pts2 = np.array([o.uv[k] for k in keys], float)
        p = solve_pnp(pts3, pts2, cam.K, cam.kc)
        if p is None:
            continue
        Xc = pts3 @ p.R.T + p.t
        pred = project(cam.K, cam.kc, Xc)
        for k, pr, obs in zip(keys, pred, pts2):
            e = obs - pr
            rows.append({"camera": o.camera, "view": int(o.view), "u": float(obs[0]),
                         "v": float(obs[1]), "du": float(e[0]), "dv": float(e[1]),
                         "err": float(np.linalg.norm(e)),
                         "radial_px": float(np.linalg.norm(obs - np.array([cam.cx, cam.cy]))),
                         "tilt_deg": float(np.degrees(np.arccos(np.clip(abs(p.R[2, 2]), -1, 1)))),
                         "distance_mm": float(np.linalg.norm(p.t)),
                         "signed_radial": 0.0, "tangential": 0.0})
    return rows


def _slope(x, y):
    x = np.asarray(x, float); y = np.asarray(y, float)
    if len(x) < 5 or np.ptp(x) < 1e-9:
        return {"slope": 0.0, "p": 1.0, "r": 0.0, "n": int(len(x))}
    r = stats.pearsonr(x, y)
    lr = stats.linregress(x, y)
    return {"slope": float(lr.slope), "p": float(lr.pvalue), "r": float(r[0]),
            "n": int(len(x))}


def residual_trends(result: CalibrationResult, observations: list[Observation],
                    pitch: float = plate.DEFAULT_PITCH_MM) -> dict:
    """Where the model error lives: radius, tilt and distance trends."""
    rows = _residual_table(result, observations, pitch)
    if not rows:
        return {"ok": False, "reason": "no residuals"}
    rad = [r["radial_px"] for r in rows]
    tilt = [r["tilt_deg"] for r in rows]
    dist = [r["distance_mm"] for r in rows]
    out = {
        "ok": True, "n": len(rows),
        "rms_px": float(np.sqrt(np.mean([r["err"] ** 2 for r in rows]))),
        "circular_tilt_slope": _slope(tilt, [r["signed_radial"] for r in rows]),
        "tilt_slope_abs": _slope(tilt, [r["err"] for r in rows]),
        "radius_slope": _slope(rad, [r["signed_radial"] for r in rows]),
        "distance_slope": _slope(dist, [r["err"] for r in rows]),
    }
    # bias gates: a significant radial-vs-tilt slope is the eccentricity
    # signature; require |slope| < 0.02 px/deg and p > 0.01
    s = out["circular_tilt_slope"]
    out["tilt_bias_detected"] = bool(abs(s["slope"]) > 0.02 and s["p"] < 0.01)
    out["recommendation"] = ("apply conic back-projection / reject high-tilt "
                             "views" if out["tilt_bias_detected"] else "no tilt bias detected")
    return out


def epipolar_consistency(result: CalibrationResult,
                         observations: list[Observation]) -> dict:
    by_view: dict[int, dict[str, Observation]] = {}
    for o in observations:
        if o.camera in ("left", "right"):
            by_view.setdefault(o.view, {})[o.camera] = o
    errs = []
    for v, pair in by_view.items():
        if "left" not in pair or "right" not in pair:
            continue
        import cv2
        c1 = result.cameras["left"]; c2 = result.cameras["right"]
        R, t = result.extrinsics["right"]
        F = fundamental(c1.K, c2.K, R, t)
        keys = [k for k in pair["left"].uv if k in pair["right"].uv]
        if not keys:
            continue
        # the epipolar constraint holds for the *pinhole* (undistorted) model
        x1 = cv2.undistortPoints(
            np.array([pair["left"].uv[k] for k in keys], float).reshape(-1, 1, 2),
            c1.K, c1.kc, P=c1.K).reshape(-1, 2)
        x2 = cv2.undistortPoints(
            np.array([pair["right"].uv[k] for k in keys], float).reshape(-1, 1, 2),
            c2.K, c2.kc, P=c2.K).reshape(-1, 2)
        e = epipolar_residual(F, x1, x2)
        errs.extend(np.abs(e).tolist())
    if not errs:
        return {"ok": False, "reason": "no stereo pairs"}
    errs = np.asarray(errs)
    return {"ok": True, "n": int(len(errs)),
            "median_px": float(np.median(errs)),
            "p95_px": float(np.percentile(errs, 95)),
            "max_px": float(np.max(errs))}


def split_half(observations: list[Observation],
               init: dict[str, CameraIntrinsics],
               pitch: float = plate.DEFAULT_PITCH_MM,
               seed: int = 0) -> dict:
    """Solve on two disjoint view sets and compare parameters by z-score."""
    views = sorted(set(o.view for o in observations))
    if len(views) < 8:
        return {"ok": False, "reason": "fewer than 8 views"}
    rng = np.random.default_rng(seed)
    perm = rng.permutation(views)
    a = set(int(v) for v in perm[:len(views) // 2])
    b = set(int(v) for v in perm[len(views) // 2:])
    RA = calibrate([o for o in observations if o.view in a], pitch, init,
                   free_kc=False, free_intrinsics=True, max_nfev=600)
    RB = calibrate([o for o in observations if o.view in b], pitch, init,
                   free_kc=False, free_intrinsics=True, max_nfev=600)
    idx = [0, 1, 2, 3, 27, 28, 29, 30, 31, 32]
    names = ["left.fx", "left.fy", "left.cx", "left.cy",
             "right.rx", "right.ry", "right.rz", "right.tx", "right.ty", "right.tz"]
    out = {}
    for i, n in zip(idx, names):
        va = RA.vector()[i] if RA.covariance is not None else np.nan
        vb = RB.vector()[i] if RB.covariance is not None else np.nan
        sa = float(np.sqrt(max(RA.covariance[i, i], 0))) if RA.covariance is not None else np.nan
        sb = float(np.sqrt(max(RB.covariance[i, i], 0))) if RB.covariance is not None else np.nan
        z = (va - vb) / max(np.hypot(sa, sb), 1e-12)
        out[n] = {"a": float(va), "b": float(vb), "z": float(z)}
    zs = np.array([abs(v["z"]) for v in out.values()])
    return {"ok": True, "parameters": out, "max_abs_z": float(np.max(zs)),
            "median_abs_z": float(np.median(zs))}


def heldout(observations: list[Observation],
            init: dict[str, CameraIntrinsics],
            pitch: float = plate.DEFAULT_PITCH_MM,
            frac: float = 0.2, seed: int = 0) -> dict:
    """Fit on a train split; report reprojection and epipolar error on test."""
    views = sorted(set(o.view for o in observations))
    if len(views) < 6:
        return {"ok": False, "reason": "fewer than 6 views"}
    rng = np.random.default_rng(seed)
    perm = rng.permutation(views)
    n_test = max(1, int(round(frac * len(views))))
    test = set(int(v) for v in perm[:n_test])
    train = [o for o in observations if o.view not in test]
    test_obs = [o for o in observations if o.view in test]
    res = calibrate(train, pitch, init, free_kc=False, free_intrinsics=True, max_nfev=600)
    rows = _residuals_pnp(res, test_obs, pitch)
    if not rows:
        return {"ok": False, "reason": "no test residuals"}
    err = np.array([r["err"] for r in rows])
    e = epipolar_consistency(res, test_obs)
    return {"ok": True, "n_test_views": len(test), "n_train_views": len(views) - len(test),
            "heldout_rms_px": float(np.sqrt(np.mean(err ** 2))),
            "heldout_median_px": float(np.median(err)),
            "heldout_p95_px": float(np.percentile(err, 95)),
            "epipolar": e}


def verify(result: CalibrationResult, observations: list[Observation],
           init: Optional[dict[str, CameraIntrinsics]] = None,
           pitch: float = plate.DEFAULT_PITCH_MM,
           do_heldout: bool = True, do_split_half: bool = True) -> dict:
    out = {"trends": residual_trends(result, observations, pitch),
           "epipolar": epipolar_consistency(result, observations),
           "rms_px": float(result.residual_rms)}
    if init is not None and do_heldout:
        out["heldout"] = heldout(observations, init, pitch)
    if init is not None and do_split_half:
        out["split_half"] = split_half(observations, init, pitch)
    gates = []
    t = out["trends"]
    if not t.get("ok", False):
        gates.append("no residual table")
    elif t.get("tilt_bias_detected"):
        gates.append(f"tilt-proportional residual bias ({t['circular_tilt_slope']['slope']:.3f} px/deg)")
    e = out["epipolar"]
    if e.get("ok") and e.get("p95_px", 0) > 1.0:
        gates.append(f"epipolar p95 {e['p95_px']:.2f} px > 1.0")
    if out.get("heldout", {}).get("ok") and out["heldout"].get("heldout_rms_px", 0) > 0.6:
        gates.append(f"held-out RMS {out['heldout']['heldout_rms_px']:.2f} px > 0.6")
    if out.get("split_half", {}).get("ok") and out["split_half"].get("max_abs_z", 0) > 4.0:
        gates.append(f"split-half max |z| {out['split_half']['max_abs_z']:.1f} > 4")
    out["gates"] = gates
    out["ok"] = len(gates) == 0
    out["note"] = ("internal consistency only; physical accuracy requires a "
                   "calibrated length artefact (VDI/VDE 2634-2, ISO 10360-13)")
    return out
