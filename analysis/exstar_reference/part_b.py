#!/usr/bin/env python
"""Part B: what does the vendor's 0.035 % scale stability establish?

Three measurements on the same 25-image corpus:

1. **How far the vendor moved** between the two CCFs and between the device-dump
   record and the 2026-02 CCF (parameter deltas).
2. **How far the data move a free fit** (our estimator, same 25 views) and how
   repeatable that is (leave-one-distance-group-out jackknife, 5 x 20 views).
3. **What the residual of a fixed vendor model looks like**: direction
   (epipolar/parallax vs radial) and how much of it a pure rig-scale change
   removes -- i.e. whether the vendor's own images can see the 1 % scale.

Writes ``partb_scale_stability.json``.
"""
from __future__ import annotations

import json
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)
sys.path.insert(0, REPO)

import vendor_lib as V  # noqa: E402
from calib import solvers  # noqa: E402

DET = os.path.join(HERE, "vendor_20261114_detections_rescued.json")


def free_mask(problem, *, kc=False, pp=True):
    m = np.zeros(problem.n, bool)
    for i in range(3):
        b = 9 * i
        m[b] = m[b + 1] = True
        m[b + 2] = m[b + 3] = bool(pp)
        m[b + 4:b + 9] = bool(kc)
    m[27:39] = True
    m[39:] = True
    return m


def fit(obs, init, kc=False, pp=True):
    prob = solvers.make_problem(obs, pitch_mm=V.PITCH, init_cameras=init)
    mask = free_mask(prob, kc=kc, pp=pp)
    x0 = prob.start(free=mask)
    out = solvers.solve_joint(prob, backend="lm", robust="huber", f_scale=1.0,
                              priors=None, free_kc=kc, free_intrinsics=True,
                              covariance=False, max_iter=150, max_nfev=1200,
                              allow_fallback=True, depths=(300.0, 450.0, 600.0),
                              free=mask, x0=x0)
    res = out["result"]
    model = {}
    for c in V.CAMERAS:
        Rc, Tc = res.extrinsics[c]
        k = res.cameras[c]
        model[c] = {"fx": k.fx, "fy": k.fy, "cx": k.cx, "cy": k.cy,
                    "skew": k.skew, "kc": np.asarray(k.kc, float),
                    "R": Rc, "T": Tc,
                    "K": np.array([[k.fx, k.skew * k.fx, k.cx],
                                   [0, k.fy, k.cy], [0, 0, 1]], float)}
    return model, res


def summary(model) -> dict:
    rig = V.rig_metrics(model)
    return {"fx": [model[c]["fx"] for c in V.CAMERAS],
            "fy": [model[c]["fy"] for c in V.CAMERAS],
            "cx": [model[c]["cx"] for c in V.CAMERAS],
            "cy": [model[c]["cy"] for c in V.CAMERAS],
            "kc": [np.asarray(model[c]["kc"], float).tolist() for c in V.CAMERAS],
            "baseline_lr": rig["right_from_left"]["baseline_mm"],
            "rot_lr": rig["right_from_left"]["rel_rot_deg"],
            "baseline_ltex": rig["tex_from_left"]["baseline_mm"]}


def residual_shape(model, obs) -> dict:
    """Residual magnitude and direction for a fixed model (pose per view)."""
    by_view = {}
    for o in obs:
        by_view.setdefault(int(o.view), {})[o.camera] = o.uv
    dx, dy, dist = [], [], []
    for v, d in sorted(by_view.items()):
        r = V.solve_view_pose(model, d)
        if not r["ok"]:
            continue
        for c in ("left", "right"):
            if c not in d:
                continue
            keys = sorted(d[c])
            X = V._board_xyz(keys)
            pred = V.project(model, c, r["Rb"], r["tb"], X)
            e = pred - np.array([d[c][k] for k in keys], float)
            dx.append(e[:, 0]); dy.append(e[:, 1]); dist.append(np.linalg.norm(e, axis=1))
    ex = np.concatenate(dx); ey = np.concatenate(dy); em = np.concatenate(dist)
    return {"n": int(len(em)),
            "rms_px": float(np.sqrt(np.mean(em ** 2))),
            "rms_dx": float(np.sqrt(np.mean(ex ** 2))),
            "rms_dy": float(np.sqrt(np.mean(ey ** 2))),
            "mean_abs_dx": float(np.mean(np.abs(ex))),
            "mean_abs_dy": float(np.mean(np.abs(ey))),
            "frac_along_x": float(np.mean(np.abs(ex)) / max(np.mean(np.abs(ex)) + np.mean(np.abs(ey)), 1e-12))}


def main() -> int:
    obs = V.obs_from_detections(DET)
    fac = V.decode_ccf_set(V.FACTORY_REPO, "best")
    new = V.decode_ccf_set(V.NEW_DIR, "best")
    prov_f = V.decode_ccf_set(V.FACTORY_REPO, "proven")
    ip = json.load(open(os.path.join(HERE, "inflash_records.json")))
    ipm = {c: {"fx": ip["records"][c]["fx"], "fy": ip["records"][c]["fy"],
               "cx": ip["records"][c]["cx"], "cy": ip["records"][c]["cy"],
               "skew": 0.0, "kc": np.array(ip["records"][c]["kc"], float),
               "R": np.array(ip["records"][c]["R"], float),
               "T": np.array(ip["records"][c]["T"], float),
               "K": np.array([[ip["records"][c]["fx"], 0, ip["records"][c]["cx"]],
                              [0, ip["records"][c]["fy"], ip["records"][c]["cy"]],
                              [0, 0, 1]], float)} for c in V.CAMERAS}
    fitj = json.load(open(os.path.join(HERE, "fit_vendor_20261114.json")))
    ours = {c: {"fx": fitj["fits"]["ours_free"]["cameras"][c]["fx"],
                "fy": fitj["fits"]["ours_free"]["cameras"][c]["fy"],
                "cx": fitj["fits"]["ours_free"]["cameras"][c]["cx"],
                "cy": fitj["fits"]["ours_free"]["cameras"][c]["cy"],
                "skew": 0.0,
                "kc": np.array(fitj["fits"]["ours_free"]["cameras"][c]["kc"], float),
                "R": np.array(fitj["fits"]["ours_free"]["extrinsics"][c]["R"], float),
                "T": np.array(fitj["fits"]["ours_free"]["extrinsics"][c]["T"], float)}
            for c in V.CAMERAS}
    for c in V.CAMERAS:
        ours[c]["K"] = np.array([[ours[c]["fx"], 0.0, ours[c]["cx"]],
                                 [0.0, ours[c]["fy"], ours[c]["cy"]],
                                 [0.0, 0.0, 1.0]])
    freekc = {c: dict(ours[c], kc=np.array(fitj["fits"]["ours_freekc"]["cameras"][c]["kc"], float))
              for c in V.CAMERAS}

    sa, sn, si, so = summary(fac), summary(new), summary(ipm), summary(ours)
    out = {"schema": "einstar.partb-scale-stability/1",
           "vendor_movement": {
               "ccfNEW_minus_ccfA": {
                   "dfx_px": [b - a for a, b in zip(sa["fx"], sn["fx"])],
                   "dfy_px": [b - a for a, b in zip(sa["fy"], sn["fy"])],
                   "dcx_px": [b - a for a, b in zip(sa["cx"], sn["cx"])],
                   "dcy_px": [b - a for a, b in zip(sa["cy"], sn["cy"])],
                   "dbaseline_mm": sn["baseline_lr"] - sa["baseline_lr"],
                   "dbaseline_pct": 100.0 * (sn["baseline_lr"] / sa["baseline_lr"] - 1.0),
                   "drot_deg": sn["rot_lr"] - sa["rot_lr"],
                   "dkc_max_abs": float(max(np.abs(np.array(b) - np.array(a)).max()
                                            for a, b in zip(sa["kc"], sn["kc"]))),
               },
               "ccfA_minus_inflash": {
                   "dfx_px": [a - b for a, b in zip(si["fx"], sa["fx"])],
                   "dbaseline_mm": sa["baseline_lr"] - si["baseline_lr"],
                   "dbaseline_pct": 100.0 * (sa["baseline_lr"] / si["baseline_lr"] - 1.0),
                   "drot_deg": sa["rot_lr"] - si["rot_lr"],
                   "dkc_max_abs": float(max(np.abs(np.array(a) - np.array(b)).max()
                                            for a, b in zip(si["kc"], sa["kc"]))),
               },
               "note": ("the vendor's own two calibrations differ by 0.035 % in "
                        "baseline; its 2026-02 calibration differs from the 2024 "
                        "device record by 0.97 %; distortion is bit-identical in "
                        "all three"),
           },
           "datasets": {"ccf_A": sa, "ccf_NEW": sn, "inflash": si,
                        "ours_free": so, "ours_freekc": summary(freekc),
                        "factory_ccf_proven_fx": [prov_f[c]["fx"] for c in V.CAMERAS]},
           "data_driven_movement_25views": {
               "ours_minus_ccfA_dfx_px": [b - a for a, b in zip(sa["fx"], so["fx"])],
               "ours_minus_ccfA_dcx_px": [b - a for a, b in zip(sa["cx"], so["cx"])],
               "ours_minus_ccfA_dbaseline_mm": so["baseline_lr"] - sa["baseline_lr"],
               "ours_minus_ccfA_dbaseline_pct": 100.0 * (so["baseline_lr"] / sa["baseline_lr"] - 1.0),
               "ours_minus_inflash_dbaseline_mm": so["baseline_lr"] - si["baseline_lr"],
               "ours_minus_inflash_dbaseline_pct": 100.0 * (so["baseline_lr"] / si["baseline_lr"] - 1.0),
               "vendor_moved_dbaseline_mm": sn["baseline_lr"] - sa["baseline_lr"],
           },
           "freekc_movement_vs_factory": {
               "dkc": [np.asarray(freekc[c]["kc"], float).tolist()
                       for c in V.CAMERAS],
               "dbaseline_mm": summary(freekc)["baseline_lr"] - sa["baseline_lr"],
               "dfx_px": [summary(freekc)["fx"][i] - sa["fx"][i] for i in range(3)],
           }}

    # jackknife: leave one 5-view distance group out, refit
    init = V.intrinsics(fac)
    jk = []
    for g in range(5):
        drop = set(range(5 * g, 5 * g + 5))
        sub = [o for o in obs if int(o.view) not in drop]
        m, _ = fit(sub, init)
        jk.append(summary(m))
        print(f"jackknife without groups[{g}] done", flush=True)
    out["jackknife_25view"] = {
        "n_fits": len(jk),
        "fx_sd_px": [float(np.std([j["fx"][i] for j in jk], ddof=1)) for i in range(3)],
        "cx_sd_px": [float(np.std([j["cx"][i] for j in jk], ddof=1)) for i in range(3)],
        "fy_sd_px": [float(np.std([j["fy"][i] for j in jk], ddof=1)) for i in range(3)],
        "baseline_sd_mm": float(np.std([j["baseline_lr"] for j in jk], ddof=1)),
        "baseline_sd_pct": float(100.0 * np.std([j["baseline_lr"] for j in jk], ddof=1)
                                 / np.mean([j["baseline_lr"] for j in jk])),
        "baseline_range_mm": [float(np.min([j["baseline_lr"] for j in jk])),
                              float(np.max([j["baseline_lr"] for j in jk]))],
        "fx_range_px": [[float(np.min([j["fx"][i] for j in jk])),
                         float(np.max([j["fx"][i] for j in jk]))] for i in range(3)],
        "rows": jk,
    }

    # residual shape of a fixed vendor model, and after a pure scale change
    shapes = {}
    for tag, m in (("ccf_A_s1.0000", fac), ("ccf_A_s1.0100", None),
                   ("inflash_s1.0000", ipm), ("ours_free", ours)):
        if m is None:
            m = {k: dict(v) for k, v in fac.items() if isinstance(v, dict)}
            for c in V.CAMERAS:
                m[c]["T"] = np.asarray(fac[c]["T"], float) * 1.010
        shapes[tag] = residual_shape(m, obs)
        print(tag, {k: round(v, 4) for k, v in shapes[tag].items()
                    if isinstance(v, float)}, flush=True)
    out["residual_shape"] = shapes
    V.save_json(out, os.path.join(HERE, "partb_scale_stability.json"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
