#!/usr/bin/env python
"""Part C: model selection on the existing 297-view session (no new recording).

Observations are the *existing* detections of ``<calibration-session>``
cached in ``analysis/session03/session.json`` -> ``resume.views`` (891 groups,
34202 points).  Nothing is re-detected and nothing is written to the session.

Parameter models compared (pitch 28.0 mm, world frame = ir_left):

  ``held_kc_ppfixed``          distortion held at the factory CCF, principal point
                               HARD-FIXED at the factory CCF, fx/fy + right/tex
                               extrinsics free            <- current candidate
  ``pp_free``                  distortion held, cx/cy free
  ``kc_free``                  distortion free (5/camera), cx/cy fixed
  ``both_free``                distortion and cx/cy free
  ``vendor_anchored_inflash``  cx/cy hard-fixed at the *device dump* record
  ``vendor_anchored_prior``    cx/cy free with a 2 px Gaussian prior at the
                               device-dump principal point (soft anchoring)

For each: training RMS, 3-fold held-out reprojection (board pose solved from the
left camera only, right/tex predicted through the fitted rig), epipolar
(Sampson) consistency, parameter sigma from the sandwich covariance, the
condition number of the rig information block, and the resulting baseline /
focal-length shift.

Writes ``model_selection_297.json``.
"""
from __future__ import annotations

import json
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)
sys.path.insert(0, REPO)

import vendor_lib as V  # noqa: E402
from calib import solvers  # noqa: E402
from calib.calibrate import CameraIntrinsics, Observation  # noqa: E402
from calib.evaluate import heldout_reprojection  # noqa: E402

SESSION = os.path.join(REPO, "analysis/session03/session.json")
PROFILE = os.path.join(REPO, "analysis/calibration/solver/final/profile.json")
INFLASH = os.path.join(HERE, "inflash_records.json")
PITCH = 28.0


CULLED = (6, 36, 129, 161)   # the session's own cull list (delivered candidate)


def load_obs(cull=CULLED) -> list:
    s = json.load(open(SESSION))
    out = []
    for g in s["resume"]["views"]:
        if int(g["view"]) in cull:
            continue
        uv = {}
        for u, v, x, y in g["uv"]:
            uv[(float(u), float(v))] = np.array([x, y], float)
        out.append(Observation(g["camera"], int(g["view"]), uv))
    return out


def init_from_profile() -> dict:
    p = json.load(open(PROFILE))
    out = {}
    for prof, cam in (("ir_left", "left"), ("ir_right", "right"), ("color", "tex")):
        c = p["cameras"][prof]
        out[cam] = CameraIntrinsics(c["fx"], c["fy"], c["cx"], c["cy"],
                                    np.array(c["dist"], float),
                                    skew=float(c.get("skew", 0.0)))
    return out


def init_pp(init: dict, pp: dict) -> dict:
    return {c: CameraIntrinsics(init[c].fx, init[c].fy,
                                pp.get(f"{c}.cx", init[c].cx),
                                pp.get(f"{c}.cy", init[c].cy),
                                np.asarray(init[c].kc, float), skew=init[c].skew)
            for c in V.CAMERAS}


def free_mask(problem, *, kc: bool, pp: bool) -> np.ndarray:
    m = np.zeros(problem.n, bool)
    for i in range(3):
        b = 9 * i
        m[b] = m[b + 1] = True
        m[b + 2] = m[b + 3] = bool(pp)
        m[b + 4:b + 9] = bool(kc)
    m[27:39] = True
    m[39:] = True
    return m


def rig_block(res, mask39) -> dict:
    """Scaled condition number of the free rig information block.

    The information matrix mixes px, mm and radians, so the raw eigenvalue
    ratio is unit-dependent; scaling the block to unit diagonal (correlation
    form) gives a scale-free conditioning number.  Held parameters are removed
    (their rows are exactly zero).
    """
    info = getattr(res, "marginal_information", None)
    out = {}
    if info is None:
        return out
    info = np.asarray(info, float)[:39, :39]
    free = np.asarray(mask39, bool)[:39]
    A = info[np.ix_(free, free)]
    out["n_free_rig"] = int(free.sum())
    d = np.sqrt(np.maximum(np.diag(A), 1e-300))
    C = A / np.outer(d, d)
    ev = np.linalg.eigvalsh(sym(C))
    out["condition_scaled"] = float(ev.max() / max(ev.min(), 1e-30))
    out["rank"] = int(np.sum(ev > ev.max() * 1e-10))
    out["min_correlation_eig"] = float(ev.min())
    return out


def sym(A):
    return 0.5 * (A + A.T)


def fit(obs, init, *, kc: bool, pp: bool, priors=None, tag="",
        covariance: bool = True) -> dict:
    prob = solvers.make_problem(obs, pitch_mm=PITCH, init_cameras=init)
    mask = free_mask(prob, kc=kc, pp=pp)
    x0 = prob.start(free=mask)
    t0 = time.time()
    out = solvers.solve_joint(prob, backend="lm", robust="huber", f_scale=1.0,
                              priors=priors, free_kc=kc, free_intrinsics=True,
                              covariance=bool(covariance),
                              covariance_estimator="sandwich",
                              max_iter=150, max_nfev=1200, allow_fallback=True,
                              depths=(300.0, 450.0, 600.0), free=mask, x0=x0)
    res = out["result"]
    dt = time.time() - t0
    names = {n: i for i, n in enumerate(prob.names)}
    std = res.param_std() or {}
    model = {}
    for c in V.CAMERAS:
        Rc, Tc = res.extrinsics[c]
        k = res.cameras[c]
        model[c] = {"fx": k.fx, "fy": k.fy, "cx": k.cx, "cy": k.cy,
                    "skew": k.skew, "kc": np.asarray(k.kc, float),
                    "R": Rc, "T": Tc,
                    "K": np.array([[k.fx, k.skew * k.fx, k.cx],
                                   [0, k.fy, k.cy], [0, 0, 1]], float)}
    rig = V.rig_metrics(model)
    return {"tag": tag, "seconds": round(dt, 1),
            "rms_px": float(res.residual_rms),
            "n_obs": int(res.n_observations), "n_params": int(res.n_parameters),
            "success": bool(res.info.get("success", True)),
            "nit": int(res.info.get("nit", -1)),
            "cameras": {c: {"fx": model[c]["fx"], "fy": model[c]["fy"],
                            "cx": model[c]["cx"], "cy": model[c]["cy"],
                            "kc": model[c]["kc"].tolist()} for c in V.CAMERAS},
            "extrinsics": {c: {"R": np.asarray(model[c]["R"]).tolist(),
                               "T": np.asarray(model[c]["T"]).tolist()}
                           for c in V.CAMERAS},
            "rig": rig,
            "sigma": {n: float(std.get(n, float("nan"))) for n in
                      ("left.fx", "left.fy", "left.cx", "left.cy",
                       "right.fx", "right.cx", "tex.fx")},
            "observability": rig_block(res, mask),
            "_result": res, "_model": model}


def heldout_folds(obs, res, folds=3) -> dict:
    views = sorted({int(o.view) for o in obs})
    rows = {}
    for k in range(folds):
        pick = [v for i, v in enumerate(views) if i % folds == k]
        errs = []
        per_cam = {c: [] for c in V.CAMERAS}
        for v in pick:
            h = heldout_reprojection(obs, res, PITCH, v)
            if "error" in h:
                continue
            errs.append(h["rms_px"])
            for c, d in h["cameras"].items():
                per_cam[c].append(d["rms_px"])
        rows[f"fold{k}"] = {
            "n_views": len(errs),
            "rms_px": float(np.sqrt(np.mean(np.square(errs)))) if errs else None,
            "per_cam": {c: float(np.sqrt(np.mean(np.square(v)))) if v else None
                        for c, v in per_cam.items()},
        }
    allv = [r["rms_px"] for r in rows.values() if r["rms_px"] is not None]
    per_cam = {}
    for c in V.CAMERAS:
        vals = [r["per_cam"][c] for r in rows.values() if r["per_cam"][c] is not None]
        per_cam[c] = float(np.mean(vals)) if vals else None
    return {"folds": rows, "rms_px": float(np.mean(allv)) if allv else None,
            "per_cam": per_cam}


def run_config(tag: str) -> dict:
    obs = load_obs()
    init0 = init_from_profile()
    ip = json.load(open(INFLASH))
    pp_ip = {}
    for c in V.CAMERAS:
        pp_ip[f"{c}.cx"] = ip["records"][c]["cx"]
        pp_ip[f"{c}.cy"] = ip["records"][c]["cy"]
    cfg = {
        "held_kc_ppfixed": dict(kc=False, pp=False, init=init0, priors=None),
        "pp_free": dict(kc=False, pp=True, init=init0, priors=None),
        "kc_free": dict(kc=True, pp=False, init=init0, priors=None),
        "both_free": dict(kc=True, pp=True, init=init0, priors=None),
        "vendor_anchored_inflash": dict(kc=False, pp=False,
                                        init=init_pp(init0, pp_ip), priors=None),
        "vendor_anchored_prior": dict(
            kc=False, pp=True, init=init0,
            priors={"parameters": {f"{c}.{k}": (pp_ip[f"{c}.{k}"], 2.0)
                                   for c in V.CAMERAS for k in ("cx", "cy")}}),
    }[tag]
    print(f"fitting {tag} on {len(obs)} groups ...", flush=True)
    r = fit(obs, priors=cfg["priors"], kc=cfg["kc"], pp=cfg["pp"],
            init=cfg["init"], tag=tag, covariance=False)
    res = r.pop("_result")
    model = r.pop("_model")
    print(f"  full rms {r['rms_px']:.4f} px  base "
          f"{r['rig']['right_from_left']['baseline_mm']:.4f}  "
          f"({r['seconds']} s)  held-out ...", flush=True)
    r["heldout"] = heldout_folds(obs, res)
    model["_dir"] = tag
    model["_key_mode"] = "fit"
    r["sampson_LR_undist"] = V.sampson(model, obs, ("left", "right"), True)
    r["sampson_LR_raw"] = V.sampson(model, obs, ("left", "right"), False)
    r["sampson_Ltex_undist"] = V.sampson(model, obs, ("left", "tex"), True)
    r["model"] = {c: {"fx": model[c]["fx"], "fy": model[c]["fy"],
                      "cx": model[c]["cx"], "cy": model[c]["cy"],
                      "kc": np.asarray(model[c]["kc"], float).tolist(),
                      "R": np.asarray(model[c]["R"], float).tolist(),
                      "T": np.asarray(model[c]["T"], float).tolist()}
                  for c in V.CAMERAS}
    V.save_json({"schema": "einstar.model-selection-297/1", "config": tag,
                 "session": SESSION, "pitch_mm": PITCH,
                 "culled_views": list(CULLED), "result": r},
                os.path.join(HERE, f"ms297_{tag}.json"))
    print(f"{tag}: rms {r['rms_px']:.4f}  heldout {r['heldout']['rms_px']:.4f}  "
          f"fx {[round(r['cameras'][c]['fx'], 3) for c in V.CAMERAS]} "
          f"cx {[round(r['cameras'][c]['cx'], 3) for c in V.CAMERAS]} "
          f"base {r['rig']['right_from_left']['baseline_mm']:.4f}", flush=True)
    return r


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True,
                    choices=("held_kc_ppfixed", "pp_free", "kc_free",
                             "both_free", "vendor_anchored_inflash",
                             "vendor_anchored_prior"))
    args = ap.parse_args()
    run_config(args.config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
