#!/usr/bin/env python
"""Collate every headline number of the EXStar vendor-corpus refit into one
JSON, so each figure in docs/calibration/exstar_vendor_corpus_refit.md has a
machine-readable source.

Reads the sibling result files (produced by detect_vendor.py, rescue_views.py,
inflash.py, fit_vendor.py, eval_vendor_model.py, part_b.py,
model_selection_297.py) and writes ``summary.json``.
"""
from __future__ import annotations

import glob
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))


def load(name):
    with open(os.path.join(HERE, name)) as fh:
        return json.load(fh)


def main() -> int:
    det = load("vendor_20261114_detections_rescued.json")
    fit = load("fit_vendor_20261114.json")
    ev = load("eval_vendor_model_20261114.json")
    ip = load("inflash_records.json")
    pb = load("partb_scale_stability.json")

    det_summary = {}
    for cam in ("left", "right", "tex"):
        rows = [r for r in det["images"] if r["camera"] == cam]
        det_summary[cam] = {
            "n_images": len(rows),
            "n_lattice_mean": float(sum(r["n_lattice"] for r in rows) / len(rows)),
            "n_lattice_min": int(min(r["n_lattice"] for r in rows)),
            "n_views_40_of_40": int(sum(r["n_lattice"] == 40 for r in rows)),
            "views_below_40": {r["index"]: r["n_lattice"] for r in rows
                               if r["n_lattice"] < 40},
            "rescued": [r for r in det["rescue"] if r["camera"] == cam],
        }

    vendor = {
        "ccf_A_20260209_baseline_mm": ip["comparison"]["ccf_A_20260209"]["left_right"]["baseline_mm"],
        "ccf_NEW_20261114_baseline_mm": ip["comparison"]["ccf_NEW_20261114"]["left_right"]["baseline_mm"],
        "inflash_22double_baseline_mm": ip["rig"]["left_right"]["baseline_mm"],
        "ccf_A_minus_inflash_pct": 100.0 * (ip["comparison"]["ccf_A_20260209"]["left_right"]["baseline_mm"]
                                            / ip["rig"]["left_right"]["baseline_mm"] - 1.0),
        "distortion_bit_identity": ip["distortion_bit_identity"],
        "dump_sha256": ip["dump"]["sha256"],
        "ccf_block_byte_identical_to_setA": ip["ccf_block"]["byte_identical_to_setA"],
        "ccf_NEW_present_in_dump": ip["ccf_block"]["new_ccf_present"],
    }

    fits = {}
    for tag, f in fit["fits"].items():
        fits[tag] = {
            "rms_px": f["rms_px"],
            "fx": [f["cameras"][c]["fx"] for c in ("left", "right", "tex")],
            "fy": [f["cameras"][c]["fy"] for c in ("left", "right", "tex")],
            "cx": [f["cameras"][c]["cx"] for c in ("left", "right", "tex")],
            "baseline_mm": f["rig"]["right_from_left"]["baseline_mm"],
            "tex_baseline_mm": f["rig"]["tex_from_left"]["baseline_mm"],
            "ir_rot_deg": f["rig"]["right_from_left"]["rel_rot_deg"],
            "effective_pitch_mm": f["effective_pitch_mm"],
            "model_eval": f["model_eval"],
        }

    models = {}
    for tag, m in ev["models"].items():
        models[tag] = {
            "rms_ir_pair_px": m["rms_px_ir_pair"],
            "rms_all_cams_px": m["rms_px_all_cams"],
            "median_px": m["median_px"],
            "per_cam_rms_px": {c: m["per_cam"][c]["rms_px"] for c in ("left", "right", "tex")},
            "sampson_LR_undist_median_px": m["sampson_LR_undist"]["median_px"],
            "sampson_LR_raw_median_px": m["sampson_LR_raw"]["median_px"],
            "baseline_mm": m["rig"]["right_from_left"]["baseline_mm"],
            "ir_rot_deg": m["rig"]["right_from_left"]["rel_rot_deg"],
            "intrinsics": m["intrinsics"],
        }

    scan = {tag: {"best_s": v["best_s"], "best_rms_ir_px": v["best_rms_ir_px"],
                  "rms_at_s1": v["rms_at_s1"]} for tag, v in ev["rig_scale_scan"].items()}

    ms = {}
    for f in sorted(glob.glob(os.path.join(HERE, "ms297_*.json"))):
        d = json.load(open(f))
        r = d["result"]
        ms[d["config"]] = {
            "rms_px": r["rms_px"], "heldout_rms_px": r["heldout"]["rms_px"],
            "heldout_per_cam": r["heldout"]["per_cam"],
            "fx": [r["cameras"][c]["fx"] for c in ("left", "right", "tex")],
            "fy": [r["cameras"][c]["fy"] for c in ("left", "right", "tex")],
            "cx": [r["cameras"][c]["cx"] for c in ("left", "right", "tex")],
            "baseline_mm": r["rig"]["right_from_left"]["baseline_mm"],
            "tex_baseline_mm": r["rig"]["tex_from_left"]["baseline_mm"],
            "ir_rot_deg": r["rig"]["right_from_left"]["rel_rot_deg"],
            "sampson_LR_undist_median_px": r["sampson_LR_undist"]["median_px"],
            "sampson_LR_raw_median_px": r["sampson_LR_raw"]["median_px"],
            "sampson_Ltex_undist_median_px": r["sampson_Ltex_undist"]["median_px"],
            "seconds": r["seconds"],
        }

    out = {
        "schema": "einstar.exstar-vendor-refit-summary/1",
        "corpus": {"dir": fit["corpus"]["dir"], "images": 75, "views": 25,
                   "pitch_mm": 28.0},
        "detections": det_summary,
        "vendor_datasets": vendor,
        "vendor_corpus_fits": fits,
        "model_evaluations_fixed_pose": models,
        "rig_scale_scan": scan,
        "partb": {"vendor_movement": pb["vendor_movement"],
                  "data_driven_movement": pb["data_driven_movement_25views"],
                  "jackknife": {k: v for k, v in pb["jackknife_25view"].items()
                                if k != "rows"},
                  "residual_shape": pb["residual_shape"]},
        "model_selection_297": ms,
        "cached_observability_held_kc": {
            "source": "analysis/calibration/solver/gate_fixed.json",
            "sigma_left_fx_px": 0.2018, "sigma_right_fx_px": 0.1814,
            "sigma_tex_fx_px": 0.1957, "condition_scaled": 1697.83,
            "rank": "18/18", "verdict_counts": "15 constrained / 3 weak / 0 unobservable",
        },
    }
    path = os.path.join(HERE, "summary.json")
    with open(path, "w") as fh:
        json.dump(out, fh, indent=1)
    print("wrote", path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
