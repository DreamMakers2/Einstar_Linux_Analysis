#!/usr/bin/env python3
"""Fair vendor-plan baseline: EXStar freezes distortion (F78), so compare the
vendor 5x5 under its own configuration (kc fixed) and under the modern 5-term
configuration used by the strategy benchmark.

Also runs the task-space planner under k1k2 (the session default) for the
apples-to-apples comparison.
"""
from __future__ import annotations

import os
import sys
from multiprocessing import Pool

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, REPO)
sys.path.insert(0, HERE)

from calib import objective, solvers              # noqa: E402
from calib.calibrate import calibrate             # noqa: E402
from calib.simloop import observations_from_pose  # noqa: E402
from calib.synth import make_scene, perturbed_init  # noqa: E402
from strategy_bench import (build_pool, metrics, vendor_poses,  # noqa: E402
                            make_obs, _solve)


def run(args):
    trial, seed = args
    scene = make_scene(n_views=25, noise_px=0.2, outlier_frac=0.02, seed=seed)
    rng = np.random.default_rng(seed * 1000 + 7)
    init = perturbed_init(scene, rng)
    pool = build_pool()
    vendor = vendor_poses(pool)
    out = {"trial": trial, "vendor": {}, "task_k1k2": {}}
    for kc_mode, label in ((False, "kcfixed"), (True, "kc5")):
        obs = []
        for i, c in enumerate(vendor):
            obs += make_obs(scene, (c.R, c.t), i, 0.2, 0.02, 0.05,
                            np.random.default_rng(seed * 10000 + i))
        if kc_mode:
            res = _solve(obs, init, free_kc=True)
        else:
            prob = solvers.make_problem(obs, pitch_mm=28.0, init_cameras=init)
            res = solvers.solve_joint(prob, backend="lm", robust="huber",
                                      free_kc=False)["result"]
        m = metrics(scene, res, [(c.R, c.t) for c in vendor])
        out["vendor"][label] = {k: m[k] for k in ("recon_model_err_med_mm",
                                                  "heldout_3d_med_mm",
                                                  "baseline_rel_err", "rot_err_deg",
                                                  "rms_px")}
    # task-space planner, both distortion models, 25 views, online
    for kc_mode, tag in (("k1k2", "task_k1k2"), (True, "task_kc5")):
      selected = [vendor[0]]
      result = None
      cache = {}
      targets = objective.measurement_volume(n_z=4, n_xy=3)
      from strategy_bench import select_online
      for step in range(1, 25):
        if result is None or step % 2 == 0:
            obs = []
            for i, c in enumerate(selected):
                obs += make_obs(scene, (c.R, c.t), i, 0.2, 0.02, 0.05,
                                np.random.default_rng(seed * 10000 + i))
            prob = solvers.make_problem(obs, pitch_mm=28.0, init_cameras=init)
            result = solvers.solve_joint(prob, backend="lm", robust="huber",
                                         free_kc=(kc_mode if len(selected) >= 10 else False))["result"]
            cache = {}
        selected.append(select_online("task_volume", result, pool, selected,
                                      rng, targets, task_cache=cache))
      obs = []
      for i, c in enumerate(selected):
        obs += make_obs(scene, (c.R, c.t), i, 0.2, 0.02, 0.05,
                        np.random.default_rng(seed * 10000 + i))
      prob = solvers.make_problem(obs, pitch_mm=28.0, init_cameras=init)
      res = solvers.solve_joint(prob, backend="lm", robust="huber",
                                free_kc=kc_mode)["result"]
      m = metrics(scene, res, [(c.R, c.t) for c in selected])
      out[tag] = {k: m[k] for k in ("recon_model_err_med_mm",
                                    "heldout_3d_med_mm",
                                    "baseline_rel_err", "rot_err_deg",
                                    "rms_px")}
    return out


def main():
    jobs = [(t, 100 + t) for t in range(10)]
    with Pool(10) as p:
        res = p.map(run, jobs)
    import json
    json.dump(res, open("/tmp/einstar_cal2/out/vendor_faithful.json", "w"), indent=1)
    for key in ("vendor", "task_k1k2", "task_kc5"):
        print(key)
        for sub in (["kcfixed", "kc5"] if key == "vendor" else [None]):
            vals = [r[key][sub]["recon_model_err_med_mm"] for r in res] if sub else \
                   [r[key]["recon_model_err_med_mm"] for r in res]
            print("  ", sub or "", "median 3-D", round(float(np.median(vals)), 3),
                  "range", round(min(vals), 3), round(max(vals), 3))


if __name__ == "__main__":
    main()
