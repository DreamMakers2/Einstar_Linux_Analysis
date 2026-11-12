"""Solver/covariance scaling from 20 to 60 views on synthetic ground truth."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from calib import solvers, synth
from calib.calibrate import CAMERAS, CameraIntrinsics


def run(n_views: int, seed: int = 0) -> dict:
    scene = synth.make_scene(n_views=n_views, pitch_mm=28.0, noise_px=0.2,
                             outlier_frac=0.0, seed=seed)
    init = {c: CameraIntrinsics(1000.0, 1000.0, 640.0, 512.0,
                                __import__("numpy").zeros(5))
            for c in CAMERAS}
    t0 = time.perf_counter()
    prob = solvers.make_problem(scene.observations, scene.pitch_mm, init)
    out = solvers.solve_joint(prob, backend="lm", free_kc=False,
                              covariance=True, max_iter=100,
                              depths=(300.0, 450.0, 600.0))
    wall = time.perf_counter() - t0
    return {
        "views": n_views,
        "residuals": int(prob.m),
        "parameters": int(prob.n),
        "free_parameters": int(out["diagnostics"]["n_free"]),
        "wall_s": wall,
        "solve_s": out["timings"]["solve_s"],
        "covariance_s": out["timings"]["covariance_s"],
        "iterations": out["diagnostics"].get("nit"),
        "status": out["diagnostics"].get("solver_status"),
        "rms_px": out["residual_rms"],
        "condition": (out["diagnostics"].get("condition")),
        "baseline_mm": out["derived"]["baseline_mm"],
        "baseline_sigma_mm": out["derived"]["baseline_sigma_mm"],
        "point_sigma_mm_450": out["derived"]["points"][450.0]["sigma_total_mm"],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--views", default="20,35,60")
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    rows, failures = [], []
    for n in (int(x) for x in args.views.split(",") if x.strip()):
        try:
            rows.append(run(n))
        except Exception as exc:
            failures.append({"views": n, "error": f"{type(exc).__name__}: {exc}"})
    out = {"schema": "einstar-bench-scaling/1", "rows": rows,
           "failures": failures}
    text = json.dumps(out, indent=1, sort_keys=True)
    print(text)
    if args.out:
        p = Path(args.out); p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text + "\n")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
