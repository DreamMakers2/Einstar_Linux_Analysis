#!/usr/bin/env python3
"""Summarise strategy_bench.py results into the tables used in the report.

Usage: python3 analyze_strategy.py /tmp/strategy_mc10.json
"""
from __future__ import annotations

import json
import sys

import numpy as np

BUDGETS = ("8", "12", "16", "25")
METRICS = ("recon_model_err_med_mm", "heldout_3d_med_mm", "baseline_rel_err",
           "rot_err_deg", "task_sigma_med_mm", "rms_px", "solve_s")


def median_ci(values, n=4000, seed=0):
    v = np.asarray([x for x in values if x is not None], float)
    if len(v) == 0:
        return float("nan"), float("nan"), float("nan")
    rng = np.random.default_rng(seed)
    m = [np.median(rng.choice(v, len(v))) for _ in range(n)]
    return float(np.median(v)), float(np.percentile(m, 2.5)), float(np.percentile(m, 97.5))


def main(path):
    d = json.load(open(path))
    runs = d["runs"]
    trials = sorted({r["trial"] for r in runs})
    strategies = sorted({r["strategy"] for r in runs})
    idx = {(r["strategy"], r["trial"]): r for r in runs}

    def get(s, t, k, key):
        b = idx[(s, t)]["budgets"].get(k)
        return None if b is None else b.get(key)

    lines = ["# strategy benchmark summary", "",
             f"trials: {len(trials)}  scenes: {len(trials)}  "
             f"noise: {d['meta']['noise']} px  outliers: {d['meta']['outlier']}  "
             f"missing: {d['meta']['missing']}", ""]
    for k in BUDGETS:
        lines += [f"## budget {k}", "",
                  "| strategy | 3-D recon med (mm) | held-out 3-D med (mm) | baseline rel err | "
                  "rotation err (deg) | task σ med (mm) | RMS (px) | solve (s) |",
                  "|---|---|---|---|---|---|---|---|"]
        for s in strategies:
            row = []
            for key in METRICS:
                m, lo, hi = median_ci([get(s, t, k, key) for t in trials])
                if key in ("baseline_rel_err",):
                    row.append(f"{100 * m:+.3f}%")
                else:
                    row.append(f"{m:.3f} [{lo:.3f}, {hi:.3f}]" if key.startswith("recon")
                               else f"{m:.3f}")
            lines.append(f"| {s} | " + " | ".join(row) + " |")
        lines.append("")
    lines += ["## paired difference vs fixed_vendor (budget 25, 3-D recon)",
              "", "| strategy | median diff (mm) | 95% CI | wins |", "|---|---|---|---|"]
    for s in strategies:
        if s == "fixed_vendor":
            continue
        diffs = []
        for t in trials:
            a = get(s, t, "25", "recon_model_err_med_mm")
            b = get("fixed_vendor", t, "25", "recon_model_err_med_mm")
            if a is not None and b is not None:
                diffs.append(a - b)
        if not diffs:
            continue
        diffs = np.asarray(diffs)
        rng = np.random.default_rng(1)
        m = [np.median(rng.choice(diffs, len(diffs))) for _ in range(4000)]
        lines.append(f"| {s} | {np.median(diffs):+.3f} | "
                     f"[{np.percentile(m, 2.5):+.3f}, {np.percentile(m, 97.5):+.3f}] | "
                     f"{int((diffs < 0).sum())}/{len(diffs)} |")
    text = "\n".join(lines)
    print(text)
    return text


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "/tmp/strategy_mc10.json")
