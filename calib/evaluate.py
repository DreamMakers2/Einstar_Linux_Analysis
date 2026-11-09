"""Offline evaluation harness: synthetic ground truth + vendor corpus ablations.

No result in this module is a physical accuracy claim unless the input is a
real measurement with a known reference.  The synthetic scenes measure
estimator behaviour; the vendor corpus measures agreement with EXStar's own
CCF and sensitivity to the workflow stages.
"""

from __future__ import annotations

from dataclasses import dataclass
import itertools
import json
import time
from typing import Iterable, Optional

import numpy as np

from . import plate
from .calibrate import (CAMERAS, CalibrationResult, CameraIntrinsics, Observation,
                        calibrate)
from .geometry import project, project_world, rodrigues, rvec_from_R
from .synth import SyntheticScene, make_scene, perturbed_init


# --------------------------------------------------------------------------
# metrics
# --------------------------------------------------------------------------

def intrinsic_errors(est: dict[str, CameraIntrinsics],
                     gt: dict[str, CameraIntrinsics]) -> dict:
    out = {}
    for c in CAMERAS:
        e, g = est[c], gt[c]
        out[c] = {"fx": e.fx - g.fx, "fy": e.fy - g.fy,
                  "cx": e.cx - g.cx, "cy": e.cy - g.cy,
                  "kc": (e.kc - g.kc).tolist()}
    return out


def rig_errors(est: CalibrationResult, gt_extr: dict, gt_baseline: float) -> dict:
    out = {}
    for c in ("right", "tex"):
        Re, te = est.extrinsics[c]
        Rg = np.asarray(gt_extr[c]["R"], float)
        tg = np.asarray(gt_extr[c]["t"], float)
        ang = np.degrees(np.arccos(np.clip((np.trace(Re.T @ Rg) - 1) / 2, -1, 1)))
        out[c] = {"rot_deg": float(ang), "t_mm": float(np.linalg.norm(te - tg))}
    out["baseline_mm"] = float(np.linalg.norm(est.baseline()) - gt_baseline)
    return out


def nees(result: CalibrationResult, truth: dict, free_kc: bool) -> float:
    """Normalised estimation error squared for the free parameters."""
    if result.covariance is None:
        return float("nan")
    # build the truth vector in the same layout
    from .calibrate import _Packer
    n_views = len(result.views)
    pk = _Packer(n_views, free_kc)
    xt = np.zeros(pk.n)
    for c in CAMERAS:
        g = truth["cameras"][c]
        xt[pk.slices[c]] = [g["fx"], g["fy"], g["cx"], g["cy"], *g["kc"]]
    for c in ("right", "tex"):
        R = np.asarray(truth["extrinsics"][c]["R"], float)
        t = np.asarray(truth["extrinsics"][c]["t"], float)
        xt[pk.extr[c]] = np.concatenate([rvec_from_R(R).ravel(), t])
    for i, v in enumerate(truth["views"]):
        R = np.asarray(v["R"], float); t = np.asarray(v["t"], float)
        xt[pk.views.start + 6 * i: pk.views.start + 6 * (i + 1)] = np.concatenate([rvec_from_R(R).ravel(), t])
    xe = np.zeros(pk.n)
    for c in CAMERAS:
        xe[pk.slices[c]] = result.cameras[c].as_vec()
    for c in ("right", "tex"):
        R, t = result.extrinsics[c]
        xe[pk.extr[c]] = np.concatenate([rvec_from_R(R).ravel(), t])
    for i, (R, t) in enumerate(result.views):
        xe[pk.views.start + 6 * i: pk.views.start + 6 * (i + 1)] = np.concatenate([rvec_from_R(R).ravel(), t])
    mask = np.ones(pk.n, bool)
    if not free_kc:
        for c in CAMERAS:
            mask[pk.slices[c].start + 4:pk.slices[c].stop] = False
    d = (xe - xt)[mask]
    C = result.covariance[np.ix_(mask, mask)]
    try:
        return float(d @ np.linalg.pinv(C, rcond=1e-12) @ d)
    except np.linalg.LinAlgError:
        return float("nan")


def reprojection(result: CalibrationResult, obs: list[Observation],
                 pitch: float) -> dict:
    obj = plate.board_points_mm(pitch)
    idx = {(float(u), float(v)): i for i, (u, v) in enumerate(plate.lattice_uv())}
    errs = []
    for o in obs:
        R, t = result.views[o.view]
        Rc, tc = result.extrinsics[o.camera]
        keys = [k for k in o.uv if k in idx]
        X = obj[[idx[k] for k in keys]]
        Xl = X @ R.T + t
        Xc = Xl @ Rc.T + tc
        pred = project(result.cameras[o.camera].K, result.cameras[o.camera].kc, Xc)
        uv = np.array([o.uv[k] for k in keys])
        errs.append(np.linalg.norm(pred - uv, axis=1))
    e = np.concatenate(errs) if errs else np.zeros(0)
    return {"n": int(len(e)), "rms_px": float(np.sqrt(np.mean(e ** 2))) if len(e) else float("nan"),
            "median_px": float(np.median(e)) if len(e) else float("nan"),
            "p90_px": float(np.percentile(e, 90)) if len(e) else float("nan")}


# --------------------------------------------------------------------------
# synthetic experiments
# --------------------------------------------------------------------------

def run_synthetic(view_counts: Iterable[int] = (3, 5, 8, 12, 20, 25),
                  n_trials: int = 5, noise_px: float = 0.2,
                  outlier_frac: float = 0.02, free_kc: bool = False,
                  seed0: int = 1000) -> dict:
    rows = []
    for k in view_counts:
        for trial in range(n_trials):
            rng = np.random.default_rng(seed0 + trial * 31 + k)
            scene = make_scene(n_views=k, noise_px=noise_px,
                               outlier_frac=outlier_frac, seed=seed0 + trial)
            init = perturbed_init(scene, rng)
            t0 = time.time()
            res = calibrate(scene.observations, scene.pitch_mm, init,
                            free_kc=free_kc, free_intrinsics=True,
                            max_nfev=800)
            dt = time.time() - t0
            ie = intrinsic_errors(res.cameras, scene.cameras)
            re = rig_errors(res, scene.truth["extrinsics"], scene.truth["baseline_mm"])
            rows.append({"k": k, "trial": trial, "time_s": dt,
                         "rms_px": res.residual_rms,
                         "fx_err": [ie[c]["fx"] for c in CAMERAS],
                         "fy_err": [ie[c]["fy"] for c in CAMERAS],
                         "cx_err": [ie[c]["cx"] for c in CAMERAS],
                         "cy_err": [ie[c]["cy"] for c in CAMERAS],
                         "baseline_err_mm": re["baseline_mm"],
                         "right_rot_err_deg": re["right"]["rot_deg"],
                         "right_t_err_mm": re["right"]["t_mm"],
                         "nees": nees(res, scene.truth, free_kc)})
    return {"rows": rows, "summary": _summarize(rows)}


def _summarize(rows: list[dict]) -> dict:
    out = {}
    ks = sorted(set(r["k"] for r in rows))
    for k in ks:
        rs = [r for r in rows if r["k"] == k]
        def med(key):
            vals = [r[key] for r in rs]
            return float(np.median(vals))
        def p90(key):
            vals = [r[key] for r in rs]
            return float(np.percentile(vals, 90))
        fx = np.array([r["fx_err"] for r in rs])
        out[str(k)] = {
            "n": len(rs),
            "fx_abs_med": float(np.median(np.abs(fx))),
            "fx_abs_p90": float(np.percentile(np.abs(fx), 90)),
            "baseline_abs_med_mm": float(np.median(np.abs([r["baseline_err_mm"] for r in rs]))),
            "rms_med_px": med("rms_px"),
            "time_med_s": med("time_s"),
            "nees_med": med("nees"),
        }
    return out


# --------------------------------------------------------------------------
# vendor corpus experiments
# --------------------------------------------------------------------------

def vendor_subsets(detections: dict, groups=(0, 1, 2, 3, 4),
                   steps=(0, 1, 2, 3, 4)) -> list[Observation]:
    obs = []
    for side, cam in (("Left", "left"), ("Right", "right"), ("Tex", "tex")):
        for i in range(1, 26):
            g, s = (i - 1) // 5, (i - 1) % 5
            if g in groups and s in steps and (side, i) in detections:
                obs.append(Observation(cam, i - 1, detections[(side, i)]))
    return obs


def run_vendor_ablation(detections: dict, init: dict[str, CameraIntrinsics],
                        reference_ccf: dict, pitch: float,
                        free_kc: bool = False) -> dict:
    scenarios = {}
    full = vendor_subsets(detections)
    scenarios["full_25"] = full
    for g in range(5):
        scenarios[f"group{g}_5"] = vendor_subsets(detections, groups=(g,))
    for s in range(5):
        scenarios[f"step{s}_5"] = vendor_subsets(detections, steps=(s,))
    for k in (1, 3, 5, 10, 15, 20):
        # evenly spaced views over the 25-step sequence
        idxs = sorted(set(int(round(x)) for x in np.linspace(0, 24, k)))
        scenarios[f"even{k}"] = [o for o in full if o.view in idxs]
    rows = {}
    for name, obs in scenarios.items():
        if not obs:
            continue
        t0 = time.time()
        res = calibrate(obs, pitch, init, free_kc=free_kc, free_intrinsics=True,
                        max_nfev=800)
        dt = time.time() - t0
        # compare to the reference rig (CCF IR pair, or a nested dict)
        if "right" in reference_ccf and isinstance(reference_ccf["right"], dict):
            ref_R = np.asarray(reference_ccf["right"]["R"], float)
            ref_base = float(reference_ccf.get("baseline", np.linalg.norm(reference_ccf["right"]["t"])))
        else:
            ref_R = np.asarray(reference_ccf["R"], float)
            ref_base = float(reference_ccf["baseline"])
        Rl, tl = res.extrinsics["left"]; Rr, tr = res.extrinsics["right"]
        Rrel = Rr @ Rl.T; trel = tr - Rrel @ tl
        ang = np.degrees(np.arccos(np.clip((np.trace(Rrel.T @ ref_R) - 1) / 2, -1, 1)))
        rows[name] = {
            "n_obs": len(obs),
            "n_views": len(sorted(set(o.view for o in obs))),
            "rms_px": res.residual_rms,
            "baseline_mm": float(np.linalg.norm(trel)),
            "baseline_vs_ccf_mm": float(np.linalg.norm(trel) - ref_base),
            "rot_vs_ccf_deg": float(ang),
            "fx": [res.cameras[c].fx for c in CAMERAS],
            "cx": [res.cameras[c].cx for c in CAMERAS],
            "dfx_vs_ccf": [res.cameras[c].fx - init[c].fx for c in CAMERAS],
            "time_s": dt,
            "success": res.info["success"],
        }
    return {"rows": rows}


# --------------------------------------------------------------------------
# repeatability
# --------------------------------------------------------------------------

def run_repeatability(detections: dict, init: dict[str, CameraIntrinsics],
                      pitch: float, n_boot: int = 8, seed: int = 7) -> dict:
    rng = np.random.default_rng(seed)
    full = vendor_subsets(detections)
    views = sorted(set(o.view for o in full))
    base_fx, base_bl = [], []
    for b in range(n_boot):
        pick = list(rng.choice(views, size=max(8, len(views) - 4), replace=False))
        obs = [o for o in full if o.view in pick]
        res = calibrate(obs, pitch, init, free_kc=False, free_intrinsics=True,
                        max_nfev=600)
        base_fx.append([res.cameras[c].fx for c in CAMERAS])
        base_bl.append(float(np.linalg.norm(res.baseline())))
    fx = np.array(base_fx); bl = np.array(base_bl)
    return {"n_boot": n_boot, "fx_mean": fx.mean(0).tolist(),
            "fx_sd": fx.std(0, ddof=1).tolist(),
            "baseline_mean_mm": float(bl.mean()),
            "baseline_sd_mm": float(bl.std(ddof=1))}


def heldout_reprojection(observations: list[Observation], result: CalibrationResult,
                         pitch: float, holdout_view: int) -> dict:
    """Reprojection of a view that was NOT used in the calibration.

    The held-out board pose is solved with the calibrated left camera only;
    the other cameras are then predicted through the calibrated rig.  This is
    a genuine generalisation check, not a training-set residual.
    """
    from .geometry import solve_pnp
    import cv2
    obj = plate.board_points_mm(pitch)
    idx = {(float(u), float(v)): i for i, (u, v) in enumerate(plate.lattice_uv())}
    left_obs = [o for o in observations if o.view == holdout_view and o.camera == "left"]
    if not left_obs:
        return {"error": "no left observation for holdout view"}
    o = left_obs[0]
    keys = [k for k in o.uv if k in idx]
    X = obj[[idx[k] for k in keys]]
    uv = np.array([o.uv[k] for k in keys], np.float32)
    pose = solve_pnp(X, uv, result.cameras["left"].K, result.cameras["left"].kc)
    if pose is None:
        return {"error": "holdout PnP failed"}
    errs = {}
    for cam in CAMERAS:
        oc = [z for z in observations if z.view == holdout_view and z.camera == cam]
        if not oc:
            continue
        oo = oc[0]
        kk = [k for k in oo.uv if k in idx]
        Xc = obj[[idx[k] for k in kk]]
        Rc, tc = result.extrinsics[cam]
        Xcam = (Xc @ pose.R.T + pose.t) @ Rc.T + tc
        pred = project(result.cameras[cam].K, result.cameras[cam].kc, Xcam)
        obs = np.array([oo.uv[k] for k in kk])
        e = np.linalg.norm(pred - obs, axis=1)
        errs[cam] = {"rms_px": float(np.sqrt(np.mean(e ** 2))),
                     "max_px": float(e.max()), "n": int(len(e))}
    allv = [v["rms_px"] for v in errs.values()]
    return {"view": holdout_view, "cameras": errs,
            "rms_px": float(np.sqrt(np.mean(np.square(allv)))) if allv else float("nan")}


def per_view_errors(result: CalibrationResult, observations: list[Observation],
                    pitch: float) -> dict[int, dict]:
    """Per-view reprojection RMS/max, useful for rejecting bad detections."""
    obj = plate.board_points_mm(pitch)
    idx = {(float(u), float(v)): i for i, (u, v) in enumerate(plate.lattice_uv())}
    out: dict[int, dict] = {}
    for o in observations:
        R, t = result.views[o.view]
        Rc, tc = result.extrinsics[o.camera]
        keys = [k for k in o.uv if k in idx]
        if not keys:
            continue
        X = obj[[idx[k] for k in keys]]
        Xc = (X @ R.T + t) @ Rc.T + tc
        pred = project(result.cameras[o.camera].K, result.cameras[o.camera].kc, Xc)
        e = np.linalg.norm(pred - np.array([o.uv[k] for k in keys]), axis=1)
        d = out.setdefault(o.view, {"errors": [], "cameras": []})
        d["errors"].extend(e.tolist())
        d["cameras"].append(o.camera)
    for v, d in out.items():
        e = np.array(d.pop("errors"))
        d["rms_px"] = float(np.sqrt(np.mean(e ** 2)))
        d["max_px"] = float(e.max())
        d["n"] = int(len(e))
    return out
