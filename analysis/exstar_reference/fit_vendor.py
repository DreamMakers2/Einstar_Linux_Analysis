#!/usr/bin/env python
"""Part A: refit the NEW EXStar 2026-11-14 corpus with the Linux estimator.

Configurations (all on the 25 vendor triples, plate pitch 28.0 mm unless stated):

  ``ours_free``    factory distortion fixed, fx/fy/cx/cy free, right/tex extrinsics
                   free  -- the analogue of the 2026-02 vendor-corpus fit.
  ``ours_prod``    factory distortion fixed, cx/cy HARD-FIXED at the factory CCF,
                   fx/fy free -- the production candidate model.
  ``ours_prod_ip`` same, but cx/cy hard-fixed at the in-flash 22-double record
                   (the alternative vendor principal point) -- a PP counterfactual.
  ``ours_freekc``  distortion free (5 coefs/camera), fx/fy/cx/cy free --
                   "how far would a free-distortion 25-view fit move?"

Plus: the vendor CCFs used as fixed models (per-view pose only), see
``eval_vendor_model.py``.

Writes ``fit_vendor_20261114.json``.
"""
from __future__ import annotations

import json
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

import vendor_lib as V  # noqa: E402
import cv2  # noqa: E402

from calib import solvers  # noqa: E402
from calib.calibrate import CameraIntrinsics  # noqa: E402

DET = os.path.join(HERE, "vendor_20261114_detections_rescued.json")
PROFILE = "analysis/calibration/solver/final/profile.json"


def free_mask(problem, *, kc: bool, pp: bool) -> np.ndarray:
    m = np.zeros(problem.n, bool)
    for i, c in enumerate(V.CAMERAS):
        b = 9 * i
        m[b + 0] = True            # fx
        m[b + 1] = True            # fy
        m[b + 2] = bool(pp)        # cx
        m[b + 3] = bool(pp)        # cy
        m[b + 4:b + 9] = bool(kc)  # k1,k2,p1,p2,k3
    m[27:39] = True                # right + tex extrinsics
    m[39:] = True                  # per-view board poses (nuisance parameters)
    return m


def set_pp(init: dict, pp: dict):
    out = {}
    for c in V.CAMERAS:
        k = init[c]
        out[c] = CameraIntrinsics(k.fx, k.fy, pp.get(f"{c}.cx", k.cx),
                                  pp.get(f"{c}.cy", k.cy), np.asarray(k.kc, float),
                                  skew=k.skew)
    return out


def cam_table(model) -> dict:
    return {c: {"fx": model[c].fx, "fy": model[c].fy, "cx": model[c].cx,
                "cy": model[c].cy, "kc": np.asarray(model[c].kc, float).tolist()}
            for c in V.CAMERAS}


def rig_x0(prob, x0: np.ndarray, model: dict) -> np.ndarray:
    """Overwrite the rig block of x0 with (R, T) relative to the left camera.

    ``calib``'s own initialiser averages per-view PnP rigs; one view's planar
    PnP lands in the two-fold ambiguity (view 14 right: |t| 556 mm instead of
    161 mm) and biases the averaged translation by ~16 mm.  Starting the rig
    from a vendor record removes that bias without touching the optimum (the
    rig is free in every fit below).
    """
    x = np.array(x0, float, copy=True)
    names = {n: i for i, n in enumerate(prob.names)}
    for cam in ("right", "tex"):
        Rrel = model[cam]["R"] @ model["left"]["R"].T
        trel = model[cam]["T"] - Rrel @ model["left"]["T"]
        rvec = cv2.Rodrigues(Rrel)[0].ravel()
        for k, val in zip(("rx", "ry", "rz"), rvec):
            x[names[f"{cam}.{k}"]] = float(val)
        for k, val in zip(("tx", "ty", "tz"), trel):
            x[names[f"{cam}.{k}"]] = float(val)
    return x


def fit(obs, init, *, kc_free: bool, pp_free: bool, tag: str,
        rig_init: dict | None = None) -> dict:
    prob = solvers.make_problem(obs, pitch_mm=V.PITCH, init_cameras=init)
    mask = free_mask(prob, kc=kc_free, pp=pp_free)
    x0 = prob.start(free=mask)
    if rig_init is not None:
        x0 = rig_x0(prob, x0, rig_init)
    t0 = time.time()
    out = solvers.solve_joint(prob, backend="lm", robust="huber", f_scale=1.0,
                              priors=None, free_kc=kc_free,
                              free_intrinsics=True, covariance=True,
                              covariance_estimator="sandwich",
                              max_iter=150, max_nfev=1200, allow_fallback=True,
                              depths=(300.0, 450.0, 600.0), free=mask, x0=x0)
    res = out["result"]
    dt = time.time() - t0
    cams = cam_table(res.cameras)
    Rl, tl = res.extrinsics["left"]
    Rr, tr = res.extrinsics["right"]
    Rt, tt = res.extrinsics["tex"]
    Rrel = Rr @ Rl.T
    trel = tr - Rrel @ tl
    Rrel_t = Rt @ Rl.T
    trel_t = tt - Rrel_t @ tl
    # per-camera residual statistics by projecting the fitted solution
    cams_model = {}
    for c in V.CAMERAS:
        Rc, Tc = res.extrinsics[c]
        k = res.cameras[c]
        cams_model[c] = {"fx": k.fx, "fy": k.fy, "cx": k.cx, "cy": k.cy,
                         "skew": k.skew, "kc": np.asarray(k.kc, float),
                         "R": Rc, "T": Tc,
                         "K": np.array([[k.fx, k.skew * k.fx, k.cx],
                                        [0, k.fy, k.cy], [0, 0, 1]], float)}
    cams_model["_dir"] = "fit:" + tag
    cams_model["_key_mode"] = "fit"
    ev = V.evaluate_model(cams_model, obs, tag=tag)
    rig = V.rig_metrics(cams_model)
    return {"tag": tag, "seconds": round(dt, 2),
            "success": bool(res.info.get("success", True)),
            "rms_px": float(res.residual_rms),
            "n_obs": int(res.n_observations), "n_params": int(res.n_parameters),
            "cameras": cams, "extrinsics": {c: {"R": res.extrinsics[c][0].tolist(),
                                                "T": res.extrinsics[c][1].tolist()}
                                            for c in V.CAMERAS},
            "rig": rig,
            "model_eval": ev,
            "solver_info": {k: v for k, v in res.info.items()
                            if isinstance(v, (int, float, str, bool, type(None)))},
            }


def main() -> int:
    obs = V.obs_from_detections(DET)
    print(f"{len(obs)} observation groups, "
          f"{sum(len(o.uv) for o in obs)} points, "
          f"{len(set(o.view for o in obs))} views")
    fac_best = V.decode_ccf_set(V.FACTORY_REPO, "best")
    new_best = V.decode_ccf_set(V.NEW_DIR, "best")
    fac_prov = V.decode_ccf_set(V.FACTORY_REPO, "proven")
    prof = V.model_from_profile(PROFILE)

    init_fac = V.intrinsics(fac_best)
    # in-flash 22-double principal points
    ip = json.load(open(os.path.join(HERE, "inflash_records.json")))
    pp_ip = {}
    for c in V.CAMERAS:
        pp_ip[f"{c}.cx"] = ip["records"][c]["cx"]
        pp_ip[f"{c}.cy"] = ip["records"][c]["cy"]
    init_ip = set_pp(init_fac, pp_ip)
    ip_model = {c: {"R": np.array(ip["records"][c]["R"], float),
                    "T": np.array(ip["records"][c]["T"], float),
                    "fx": ip["records"][c]["fx"], "fy": ip["records"][c]["fy"],
                    "cx": ip["records"][c]["cx"], "cy": ip["records"][c]["cy"],
                    "kc": np.array(ip["records"][c]["kc"], float), "skew": 0.0}
                for c in V.CAMERAS}

    out = {
        "schema": "einstar.exstar-vendor-refit/1",
        "corpus": {
            "dir": V.NEW_DIR,
            "images": 75, "views": 25,
            "detections": DET,
            "pitch_mm": V.PITCH,
        },
        "init": {"factory_ccf_best": {c: init_fac[c].as_dict() for c in V.CAMERAS},
                 "factory_ccf_proven_fx": {c: fac_prov[c]["fx"] for c in V.CAMERAS},
                 "inflash_pp": pp_ip},
        "fits": {},
    }

    fits = [
        ("ours_free", dict(kc_free=False, pp_free=True), init_fac),
        ("ours_prod", dict(kc_free=False, pp_free=False), init_fac),
        ("ours_prod_ip", dict(kc_free=False, pp_free=False), init_ip),
        ("ours_freekc", dict(kc_free=True, pp_free=True), init_fac),
        # start from the "proven" (raw stored) decode to prove the optimum does
        # not depend on the initial intrinsics
        ("ours_free_initproven", dict(kc_free=False, pp_free=True),
         V.intrinsics(fac_prov)),
    ]
    for tag, kw, init in fits:
        print("fitting", tag, "...", flush=True)
        out["fits"][tag] = fit(obs, init, tag=tag,
                               rig_init=(fac_best if "initproven" not in tag
                                         else ip_model), **kw)
        f = out["fits"][tag]
        print(f"  rms {f['rms_px']:.4f} px  fx "
              f"{[round(f['cameras'][c]['fx'], 4) for c in V.CAMERAS]}  "
              f"baseline {f['rig']['right_from_left']['baseline_mm']:.4f} mm  "
              f"({f['seconds']} s)", flush=True)

    # effective pitch to reproduce each vendor dataset from each fit
    vend = {
        "ccf_A_20260209": V.rig_metrics(fac_best)["right_from_left"]["baseline_mm"],
        "ccf_NEW_20261114": V.rig_metrics(new_best)["right_from_left"]["baseline_mm"],
        "inflash_22double": ip["rig"]["left_right"]["baseline_mm"],
    }
    out["vendor_datasets"] = {
        "baselines_mm": vend,
        "inflash_rig": ip["rig"],
        "ccf_A_rig": V.rig_metrics(fac_best),
        "ccf_NEW_rig": V.rig_metrics(new_best),
    }
    for tag, f in out["fits"].items():
        raw = f["rig"]["right_from_left"]["baseline_mm"]
        f["effective_pitch_mm"] = {
            k: V.PITCH * v / raw for k, v in vend.items()}
    V.save_json(out, os.path.join(HERE, "fit_vendor_20261114.json"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
