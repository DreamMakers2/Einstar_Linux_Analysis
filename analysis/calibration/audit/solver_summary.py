"""Consolidate benchmark runs into ``solver_results.json`` + markdown tables.

Usage:
    python analysis/calibration/audit/solver_summary.py \
        analysis/calibration/audit/solver_results_t1.json \
        [repeats.json ...] --out analysis/calibration/audit/solver_results.json
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))

ORDER = ["scipy_engine", "scipy_breakdown", "scipy_analytic_dense",
         "scipy_analytic_sparse", "cpp_ref", "opencv", "opencv_fixpp"]


def load(files):
    runs, baselines = [], {}
    for f in files:
        d = json.loads(Path(f).read_text())
        rs = d.get("runs", [])
        for r in rs:                      # older runs used the C++ solver's own name
            if r.get("solver") == "cpp_ref_lm":
                r["solver"] = "cpp_ref"
        runs.extend(rs)
        baselines.update(d.get("baselines", {}))
    return runs, baselines


def medians(runs):
    """Median runtime per (case, solver) across repetitions."""
    groups = {}
    for r in runs:
        if "error" in r:
            continue
        groups.setdefault((r["case"], r["solver"]), []).append(r)
    out = {}
    for k, rs in groups.items():
        rt = [x["runtime_s"] for x in rs]
        out[k] = {"n": len(rs), "runtime_s": float(statistics.median(rt)),
                  "runtime_min_s": float(min(rt)),
                  "runtime_max_s": float(max(rt)),
                  "rms_px": float(statistics.median([x["rms_px"] for x in rs])),
                  "rss_peak_mb": float(statistics.median([x["rss_peak_mb"] for x in rs])),
                  "nfev": rs[0].get("nfev"), "status": rs[0].get("status"),
                  "record": rs[0]}
    return out


def camera_params(x, n_views):
    x = np.asarray(x, float)
    out = {}
    for i, c in enumerate(("left", "right", "tex")):
        v = x[9 * i:9 * i + 9]
        out[c] = {"fx": float(v[0]), "fy": float(v[1]), "cx": float(v[2]),
                  "cy": float(v[3]), "kc": [float(t) for t in v[4:9]]}
    out["right_pose"] = {"rvec": x[27:30].tolist(), "t": x[30:33].tolist()}
    out["tex_pose"] = {"rvec": x[33:36].tolist(), "t": x[36:39].tolist()}
    return out


def delta(a, b):
    """Max rigid-parameter difference between two packed vectors."""
    a = np.asarray(a, float); b = np.asarray(b, float)
    d = a - b
    rot = float(np.degrees(np.max(np.abs(d[27:30])))) if d.size >= 36 else float("nan")
    return {"max_dfx": float(np.max(np.abs(d[[0, 9, 18]]))),
            "max_dfy": float(np.max(np.abs(d[[1, 10, 19]]))),
            "max_dcx": float(np.max(np.abs(d[[2, 11, 20]]))),
            "max_dcy": float(np.max(np.abs(d[[3, 12, 21]]))),
            "max_dkc": float(np.max(np.abs(d[[4, 5, 6, 7, 8, 13, 14, 15, 16, 17,
                                              22, 23, 24, 25, 26]]))),
            "max_drot_rad": rot,
            "d_baseline_mm": float(np.linalg.norm(d[30:33])),
            "max_dparam": float(np.max(np.abs(d)))}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+")
    ap.add_argument("--out", default=str(HERE / "solver_results.json"))
    ap.add_argument("--env", default="{}", help="JSON with environment facts")
    a = ap.parse_args()
    runs, baselines = load(a.files)
    med = medians(runs)

    cases = sorted({k[0] for k in med})
    derived = {"speedups": {}, "param_deltas": {}, "truth_errors": {},
               "open": {}}
    for case in cases:
        eng = med.get((case, "scipy_engine"))
        for (c, s), m in med.items():
            if c != case:
                continue
            rec = m["record"]
            if eng and s != "scipy_engine":
                derived["speedups"].setdefault(case, {})[s] = round(
                    eng["runtime_s"] / m["runtime_s"], 2)
            der = rec.get("truth_delta") or {}
            if der:
                derived["truth_errors"].setdefault(case, {})[s] = der
            ref = med.get((case, "cpp_ref"))
            if ref and s != "cpp_ref" and "x_final" in rec:
                derived["param_deltas"].setdefault(case, {})[s] = delta(
                    rec["x_final"], ref["record"]["x_final"])
            if s == "opencv":
                derived["open"].setdefault(case, {})[s] = {
                    "rms_cv_per_camera": rec.get("rms_cv_per_camera"),
                    "rms_cv_stereo": rec.get("rms_cv_stereo"),
                    "flags": rec.get("opencv_flags")}

    # parameter spread across solvers, per case
    spread = {}
    for case in cases:
        xs = {s: np.asarray(m["record"]["x_final"], float)
              for (c, s), m in med.items()
              if c == case and s != "scipy_analytic_sparse"
              and "x_final" in m["record"]}
        if len(xs) < 2:
            continue
        ref_name = "cpp_ref" if "cpp_ref" in xs else sorted(xs)[0]
        base = xs[ref_name]
        spread[case] = {s: delta(x, base) for s, x in xs.items() if s != ref_name}
        spread[case]["_reference"] = ref_name

    out = {"meta": json.loads(a.env), "cases": {c: {"n_views": med[(c, sorted(
        {k[1] for k in med if k[0] == c})[0])]["record"]["n_views"],
        "m": med[(c, sorted({k[1] for k in med if k[0] == c})[0])]["record"]["m"],
        "n": med[(c, sorted({k[1] for k in med if k[0] == c})[0])]["record"]["n"]}
        for c in cases},
        "baselines": baselines, "solvers": {
            f"{c}/{s}": {k: v for k, v in m.items() if k != "record"}
            for (c, s), m in sorted(med.items())},
        "param_spread_vs_cpp_ref": spread,
        "derived": derived,
        "runs": [{k: v for k, v in r.items() if k != "x_final"} for r in runs]}

    Path(a.out).write_text(json.dumps(out, indent=1))
    print(f"wrote {a.out}")

    # ---- markdown tables ---------------------------------------------------
    print("\n### runtime / RMS\n")
    print("| case | solver | runtime s | speedup vs engine | RMS px | max px | nfev/iters | peak RSS MB |")
    print("|---|---|---|---|---|---|---|---|")
    for case in cases:
        for s in ORDER:
            m = med.get((case, s))
            if not m:
                continue
            sp = derived["speedups"].get(case, {}).get(s)
            rec = m["record"]
            mx = rec.get("analysis", {}).get("max_px")
            print(f"| {case} | {s} | {m['runtime_s']:.3f} | "
                  f"{'' if sp is None else f'{sp:.1f}x'} | {m['rms_px']:.4f} | "
                  f"{'' if mx is None else f'{mx:.2f}'} | {m['nfev']} | "
                  f"{m['rss_peak_mb']:.0f} |")
    print("\n### parameter agreement (max abs difference vs cpp_ref)\n")
    print("| case | solver | dfx | dfy | dcx | dcy | dkc | d_baseline mm | max |dparam| |")
    print("|---|---|---|---|---|---|---|---|---|")
    for case, d in spread.items():
        for s, v in d.items():
            if s.startswith("_"):
                continue
            print(f"| {case} | {s} | {v['max_dfx']:.2e} | {v['max_dfy']:.2e} | "
                  f"{v['max_dcx']:.2e} | {v['max_dcy']:.2e} | {v['max_dkc']:.2e} | "
                  f"{v['d_baseline_mm']:.2e} | {v['max_dparam']:.2e} |")


if __name__ == "__main__":
    main()
