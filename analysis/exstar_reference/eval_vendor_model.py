#!/usr/bin/env python
"""Part A (second half): how well does each vendor calibration explain its own
2026-11-14 images?

Every model below is FIXED (intrinsics, distortion and the rig extrinsics); only
one 6-DoF board pose per view is solved, against all visible cameras jointly.  A
model that does not match reality leaves a residual it cannot absorb -- the board
pitch and the rig scale are not free.

Models compared on the same 75 images / 25 views:

  ``ccf_A_20260209``   host CCF before the recalibration (= backup/bak, repo copy)
  ``ccf_NEW_20261114`` host CCF written by the 2026-11-14 recalibration
  ``ccf_*_proven``     the same records decoded with only the proven key entries
                       (raw stored fx/fy/cx/cy) -- a decode counterfactual
  ``inflash_22double`` the older unkeyed camera records in the device info dump
  ``ours_free``        the Linux fit on these images (fx/fy/cx/cy free)
  ``ours_prod``        the Linux fit with cx/cy hard-fixed at the factory CCF
  ``linux_candidate297`` the 293-view candidate profile (analysis, never activated)

Writes ``eval_vendor_model_20261114.json``.
"""
from __future__ import annotations

import json
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

import vendor_lib as V  # noqa: E402

DET = os.path.join(HERE, "vendor_20261114_detections_rescued.json")
PROFILE = "analysis/calibration/solver/final/profile.json"


def fit_model_from_json(path: str, tag: str) -> dict:
    f = json.load(open(path))["fits"][tag]
    model = {}
    for c in V.CAMERAS:
        cm = f["cameras"][c]
        K = np.array([[cm["fx"], 0, cm["cx"]], [0, cm["fy"], cm["cy"]],
                      [0, 0, 1]], float)
        model[c] = {"fx": cm["fx"], "fy": cm["fy"], "cx": cm["cx"],
                    "cy": cm["cy"], "skew": 0.0,
                    "kc": np.array(cm["kc"], float),
                    "R": np.array(f["extrinsics"][c]["R"], float),
                    "T": np.array(f["extrinsics"][c]["T"], float), "K": K}
    model["_dir"] = f"fit:{tag}"
    model["_key_mode"] = "fit"
    return model


def inflash_model() -> dict:
    ip = json.load(open(os.path.join(HERE, "inflash_records.json")))
    model = {}
    for c in V.CAMERAS:
        r = ip["records"][c]
        model[c] = {"fx": r["fx"], "fy": r["fy"], "cx": r["cx"], "cy": r["cy"],
                    "skew": 0.0, "kc": np.array(r["kc"], float),
                    "R": np.array(r["R"], float), "T": np.array(r["T"], float),
                    "K": np.array([[r["fx"], 0, r["cx"]],
                                   [0, r["fy"], r["cy"]], [0, 0, 1]], float)}
    model["_dir"] = "device dump 22-double"
    model["_key_mode"] = "unkeyed"
    return model


def main() -> int:
    obs = V.obs_from_detections(DET)
    fits = os.path.join(HERE, "fit_vendor_20261114.json")
    models = {
        "ccf_A_20260209": V.decode_ccf_set(V.FACTORY_REPO, "best"),
        "ccf_NEW_20261114": V.decode_ccf_set(V.NEW_DIR, "best"),
        "ccf_A_proven": V.decode_ccf_set(V.FACTORY_REPO, "proven"),
        "ccf_NEW_proven": V.decode_ccf_set(V.NEW_DIR, "proven"),
        "inflash_22double": inflash_model(),
        "ours_free": fit_model_from_json(fits, "ours_free"),
        "ours_prod": fit_model_from_json(fits, "ours_prod"),
        "linux_candidate297": V.model_from_profile(PROFILE),
    }
    out = {"schema": "einstar.exstar-vendor-model-eval/1",
           "corpus": DET, "n_views": 25,
           "note": ("each model is fixed; only one 6-DoF board pose per view is "
                    "solved (all visible cameras jointly), so the numbers are a "
                    "genuine test of the model, not a re-fit"),
           "models": {}}
    for tag, m in models.items():
        ev = V.evaluate_model(m, obs, tag=tag)
        rig = V.rig_metrics(m)
        ev["rig"] = rig
        ev["sampson_LR_undist"] = V.sampson(m, obs, ("left", "right"), True)
        ev["sampson_LR_raw"] = V.sampson(m, obs, ("left", "right"), False)
        ev["sampson_Ltex_undist"] = V.sampson(m, obs, ("left", "tex"), True)
        ev["intrinsics"] = {c: {"fx": m[c]["fx"], "fy": m[c]["fy"],
                                "cx": m[c]["cx"], "cy": m[c]["cy"]}
                            for c in V.CAMERAS}
        out["models"][tag] = ev
        print(f"{tag:20s} rmsIR {ev['rms_px_ir_pair']:.4f} px  "
              f"median {ev['median_px']:.4f}  "
              f"L/R/T {ev['per_cam']['left']['rms_px']:.3f}/"
              f"{ev['per_cam']['right']['rms_px']:.3f}/"
              f"{ev['per_cam']['tex']['rms_px']:.3f}  "
              f"sampson LR {ev['sampson_LR_undist']['median_px']:.3f}  "
              f"base {rig['right_from_left']['baseline_mm']:.3f}",
              flush=True)
    # ---- rig-scale scan: which board-size / baseline ratio do the images want?
    # Scaling every camera's T by s scales the relative camera positions by s,
    # i.e. it is exactly a change of the model's baseline-to-pitch ratio, which
    # is the observable the images constrain (a free per-view pose cannot absorb
    # it).  The minimum therefore says which vendor dataset the images support.
    def scaled(base, sv):
        m = {k: dict(v) for k, v in base.items() if isinstance(v, dict)}
        for c in V.CAMERAS:
            m[c]["T"] = np.asarray(base[c]["T"], float) * sv
        return m

    grid = np.round(np.arange(0.985, 1.0201, 0.0025), 5)
    scan = {}
    for tag in ("ccf_A_20260209", "ccf_NEW_20261114", "inflash_22double"):
        base = models[tag]
        rows = []
        for sv in (float(x) for x in grid):
            ev = V.evaluate_model(scaled(base, sv), obs)
            rows.append({"s": sv, "rms_ir_px": ev["rms_px_ir_pair"],
                         "median_ir_px": ev["median_px"],
                         "rms_left": ev["per_cam"]["left"]["rms_px"],
                         "rms_right": ev["per_cam"]["right"]["rms_px"]})
        best = min(rows, key=lambda r: r["rms_ir_px"])
        scan[tag] = {"rows": rows, "best_s": best["s"],
                     "best_rms_ir_px": best["rms_ir_px"],
                     "rms_at_s1": [r for r in rows
                                   if abs(r["s"] - 1.0) < 1e-9][0]["rms_ir_px"]}
        print(f"scale scan {tag:20s} best s = {best['s']:.4f} "
              f"(rms {best['rms_ir_px']:.4f} px; at s=1 "
              f"{scan[tag]['rms_at_s1']:.4f} px)")
    out["rig_scale_scan"] = scan
    V.save_json(out, os.path.join(HERE, "eval_vendor_model_20261114.json"))

if __name__ == "__main__":
    raise SystemExit(main())
