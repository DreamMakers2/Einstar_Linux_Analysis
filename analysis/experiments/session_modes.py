#!/usr/bin/env python3
"""Evaluate the guided session's progressive modes and stopping rule.

Each run is a closed-loop Monte Carlo: a synthetic operator follows the
session's recommendations, and the session decides when to stop.  The output
reports the number of captures, the realised accuracy against ground truth,
the operator movement and the computational cost, per mode.

Usage:
  python3 session_modes.py --trials 4 --workers 8 --out session_modes.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from multiprocessing import Pool

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, REPO)
sys.path.insert(0, HERE)

from calib import objective                       # noqa: E402
from calib.simloop import run_closed_loop         # noqa: E402
from calib.synth import make_scene                # noqa: E402
from strategy_bench import metrics                # noqa: E402


def _effort(poses):
    total = 0.0
    for a, b in zip(poses[:-1], poses[1:]):
        ang = np.degrees(np.arccos(np.clip(
            (np.trace(np.asarray(b[0]) @ np.asarray(a[0]).T) - 1) / 2, -1, 1)))
        total += (0.15 + np.linalg.norm(np.asarray(b[1]) - np.asarray(a[1])) / 150.0
                  + ang / 25.0)
    return float(total)


def run_one(args):
    mode, trial, seed, max_views = args
    scene = make_scene(n_views=25, noise_px=0.2, outlier_frac=0.02, seed=seed)
    t0 = time.time()
    out = run_closed_loop(scene, mode=mode, max_views=max_views, seed=seed,
                          render=False, noise_px=0.2, outlier_frac=0.02,
                          missing_frac=0.05)
    wall = time.time() - t0
    sess = out["session"]
    rec = {"mode": mode, "trial": trial, "seed": seed, "wall_s": wall,
           "n_accepted": out["n_accepted"], "n_captures": out["n_captures"],
           "n_rejected": len(out["rejected"]),
           "stop": bool(out["stop"]["stop"]),
           "stop_reason": out["stop"].get("reason"),
           "predicted_delta_sigma_mm": out["stop"].get("predicted_delta_sigma_mm"),
           "recent_sigma_drop_mm": out["stop"].get("recent_sigma_drop_mm"),
           "solve_seconds": sess.solve_seconds, "n_solves": sess.status()["n_solves"],
           "effort": _effort([a.pose for a in sess.accepted])}
    if sess.result is not None:
        m = metrics(scene, sess.result, [a.pose for a in sess.accepted])
        rec.update({k: m[k] for k in ("baseline_err_mm", "baseline_rel_err",
                                      "rot_err_deg", "recon_model_err_med_mm",
                                      "heldout_3d_med_mm", "task_sigma_med_mm",
                                      "rms_px", "dfx_med")})
    return rec


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=4)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--modes", default="quick,standard,high")
    ap.add_argument("--max-views", type=int, default=35)
    ap.add_argument("--out", default="session_modes.json")
    args = ap.parse_args(argv)
    jobs = []
    for t in range(args.trials):
        for m in args.modes.split(","):
            jobs.append((m, t, 200 + t, args.max_views))
    t0 = time.time()
    with Pool(min(args.workers, len(jobs))) as pool:
        res = pool.map(run_one, jobs)
    json.dump({"meta": {"trials": args.trials, "max_views": args.max_views,
                        "seconds": time.time() - t0, "modes": args.modes},
               "runs": res}, open(args.out, "w"), indent=1)
    print(f"wrote {args.out}: {len(res)} runs in {time.time() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
