#!/usr/bin/env python3
"""Monte-Carlo comparison of calibration capture strategies.

Strategies
----------
fixed_vendor    EXStar's 5 orientations x 5 distance levels (25 sets)
uniform         evenly spaced distance/tilt/roll grid
random_diverse  random poses with diversity rejection
aprilcal        online greedy expected-reprojection-error (ERE) over target
                points (the AprilCal idea, adapted to the circle plate)
kalibr_param    online greedy on the marginal rig-parameter covariance
                (information form of the Kalibr-style batch problem)
fisher_param    online A-optimal on rig parameters with a pose pool
task_volume     online A-optimal on 3-D uncertainty over the 200-600 mm volume
task_effort     task_volume with an operator-effort penalty
task_depth      online minimisation of depth variance over the volume
hybrid          coverage-first bootstrap, then task_effort with saturation
                stopping (the proposed production design)

Every strategy sees the same scene, noise model, initial perturbation and
candidate pool for a given trial (common random numbers).  The evaluation is
against the known synthetic ground truth: intrinsics, baseline, relative
rotation, 3-D reconstruction over the volume and held-out board poses.

Usage:
  python3 strategy_bench.py --trials 8 --workers 12 --out results.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from multiprocessing import Pool

import numpy as np

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

from calib import objective, plate                      # noqa: E402
from calib.adaptive import (ViewCandidate, board_pose, _inv_spd, candidate_jacobian,
                            free_rig_mask, rig_covariance, rig_vector)  # noqa: E402
from calib.calibrate import CAMERAS, CameraIntrinsics, Observation, calibrate  # noqa: E402
from calib.geometry import project                       # noqa: E402
from calib.simloop import observations_from_pose         # noqa: E402
from calib.synth import make_scene, perturbed_init       # noqa: E402

BUDGETS = (8, 12, 16, 25)


def _solve(obs, init, free_kc, max_nfev=800):
    """Fast LM backend with the engine as a fallback (see calib.solvers)."""
    try:
        from calib import solvers
        prob = solvers.make_problem(obs, pitch_mm=28.0, init_cameras=init)
        out = solvers.solve_joint(prob, backend="lm", robust="huber", f_scale=1.0,
                                  free_kc=free_kc, covariance=True)
        if out.get("result") is not None:
            return out["result"]
    except Exception:
        pass
    return calibrate(obs, 28.0, init, free_kc=free_kc, free_intrinsics=True,
                     max_nfev=max_nfev)


# ---------------------------------------------------------------------------
# candidate pool
# ---------------------------------------------------------------------------

def build_pool(pitch=28.0):
    pool = []
    for d in (240.0, 320.0, 400.0, 480.0, 560.0):
        for tx in (-20.0, 0.0, 20.0):
            for ty in (-20.0, 0.0, 20.0):
                for roll in (0.0, 90.0):
                    R, t = board_pose(d, tx, ty, roll)
                    pool.append(ViewCandidate(d, tx, ty, roll, R, t,
                                              f"d{int(d)}_x{int(tx)}_y{int(ty)}_r{int(roll)}"))
    # lateral offsets (field/periphery coverage)
    for d in (320.0, 440.0):
        for lx, ly in ((60, 0), (-60, 0), (0, 40), (0, -40)):
            R, t = board_pose(d, 0.0, 0.0, 0.0, (lx, ly))
            pool.append(ViewCandidate(d, 0.0, 0.0, 0.0, R, t,
                                      f"d{int(d)}_lat{lx}_{ly}"))
    return pool


def vendor_poses(pool):
    """EXStar: 5 orientations (0, +-18 about x/y) x 5 distances."""
    tilts = [(0, 0), (-18, 0), (18, 0), (0, 18), (0, -18)]
    dists = (240, 320, 400, 480, 560)
    out = []
    for d in dists:
        for tx, ty in tilts:
            R, t = board_pose(float(d), float(tx), float(ty), 0.0)
            out.append(ViewCandidate(float(d), tx, ty, 0.0, R, t,
                                     f"vendor_d{d}_x{tx}_y{ty}"))
    return out


def uniform_poses():
    """Twelve evenly spread poses, interleaved in distance."""
    specs = []
    for d in (260.0, 400.0, 540.0):
        for tx, ty in ((0, 0), (15, 15), (-15, -15)):
            specs.append((d, tx, ty, 0.0))
    specs += [(260.0, 0, 0, 90.0), (400.0, 0, 0, 90.0), (540.0, 0, 0, 90.0)]
    specs.sort(key=lambda z: (z[0], z[1]))
    out = []
    for d, tx, ty, roll in specs:
        R, t = board_pose(d, tx, ty, roll)
        out.append(ViewCandidate(d, tx, ty, roll, R, t,
                                 f"uni_d{int(d)}_x{tx}_y{ty}_r{int(roll)}"))
    return out


# ---------------------------------------------------------------------------
# observation generation and evaluation
# ---------------------------------------------------------------------------

def make_obs(scene, pose, view, noise, outlier, missing, rng):
    return observations_from_pose(scene, pose[0], pose[1], view=view,
                                  noise_px=noise, outlier_frac=outlier,
                                  missing_frac=missing, rng=rng)


def metrics(scene, result, selected_poses):
    ie = {}
    for c in CAMERAS:
        e = result.cameras[c]
        t = scene.cameras[c]
        ie[c] = {"dfx": float(e.fx - t.fx), "dfy": float(e.fy - t.fy),
                 "dcx": float(e.cx - t.cx), "dcy": float(e.cy - t.cy)}
    Rl, tl = result.extrinsics["right"]
    Rt, tt = scene.extrinsics["right"]
    ang = float(np.degrees(np.arccos(np.clip((np.trace(Rl.T @ Rt) - 1) / 2, -1, 1))))
    b_est = float(np.linalg.norm(result.baseline()))
    b_true = float(scene.truth["baseline_mm"])
    # model-induced 3-D error over the measurement volume
    pts = objective.measurement_volume(n_z=4, n_xy=3)
    rng = np.random.default_rng(12345)
    x_true = np.zeros(39)
    for i, c in enumerate(CAMERAS):
        x_true[9 * i:9 * i + 9] = scene.cameras[c].as_vec()
    for i, c in enumerate(("right", "tex")):
        R, t = scene.extrinsics[c]
        from calib.geometry import rvec_from_R
        x_true[27 + 6 * i:27 + 6 * i + 6] = np.concatenate([rvec_from_R(R), t])
    cams_t, extr_t = objective._split_rig(x_true)
    uv_l = objective._pixels(cams_t, extr_t, "left", pts)
    uv_r = objective._pixels(cams_t, extr_t, "right", pts)
    uv_l = uv_l + rng.normal(0, 0.2, uv_l.shape)
    uv_r = uv_r + rng.normal(0, 0.2, uv_r.shape)
    X_ref = objective.triangulate(x_true, uv_l, uv_r)
    x_est = rig_vector(result)
    X_est = objective.triangulate(x_est, uv_l, uv_r)
    d_model = np.linalg.norm(X_est - X_ref, axis=1)
    d_abs = np.linalg.norm(X_est - pts, axis=1)
    # held-out board poses: reconstruct the board markers and compare 3-D
    held_err = []
    for k in range(6):
        R = _random_board_rot(rng)
        t = np.array([rng.uniform(-40, 40), rng.uniform(-30, 30), rng.uniform(260, 540)])
        obs = observations_from_pose(scene, R, t, view=100 + k, noise_px=0.2,
                                     outlier_frac=0.0, missing_frac=0.0, rng=rng)
        ol = obs[0]; orr = obs[1]
        keys = [k2 for k2 in ol.uv if k2 in orr.uv]
        if len(keys) < 6:
            continue
        obj = plate.board_points_mm(scene.pitch_mm)
        uv_idx = [(float(u), float(v)) for (u, v) in plate.lattice_uv()]
        idx = [uv_idx.index(k2) for k2 in keys]
        uvl = np.array([ol.uv[k2] for k2 in keys], float)
        uvr = np.array([orr.uv[k2] for k2 in keys], float)
        Xe = objective.triangulate(x_est, uvl, uvr)
        Xt = obj[idx] @ np.asarray(R).T + np.asarray(t)
        held_err.extend(np.linalg.norm(Xe - Xt, axis=1).tolist())
    # task-space calibration uncertainty
    rep = objective.evaluate_task(result, "A", pts)
    return {
        "dfx_med": float(np.median([abs(ie[c]["dfx"]) for c in CAMERAS])),
        "dfy_med": float(np.median([abs(ie[c]["dfy"]) for c in CAMERAS])),
        "dcx_med": float(np.median([abs(ie[c]["dcx"]) for c in CAMERAS])),
        "baseline_mm": b_est, "baseline_err_mm": b_est - b_true,
        "baseline_rel_err": (b_est - b_true) / b_true,
        "rot_err_deg": ang,
        "rms_px": float(result.residual_rms),
        "task_sigma_med_mm": float(np.median(rep.sigma_mm)),
        "task_sigma_max_mm": float(np.max(rep.sigma_mm)),
        "depth_sigma_med_mm": float(np.median(rep.depth_sigma_mm)),
        "recon_model_err_med_mm": float(np.median(d_model)),
        "recon_model_err_p95_mm": float(np.percentile(d_model, 95)),
        "recon_abs_err_med_mm": float(np.median(d_abs)),
        "heldout_3d_med_mm": float(np.median(held_err)) if held_err else None,
        "heldout_3d_p95_mm": float(np.percentile(held_err, 95)) if held_err else None,
        "n_views": len(set(o.view for o in getattr(result, "_obs", []))) or result.info.get("n_views"),
    }


def _random_board_rot(rng):
    # random rotation within +-30 deg of fronto-parallel
    rx = np.radians(rng.uniform(-30, 30)); ry = np.radians(rng.uniform(-30, 30))
    rz = np.radians(rng.uniform(0, 360))
    Rx = np.array([[1, 0, 0], [0, np.cos(rx), -np.sin(rx)], [0, np.sin(rx), np.cos(rx)]])
    Ry = np.array([[np.cos(ry), 0, np.sin(ry)], [0, 1, 0], [-np.sin(ry), 0, np.cos(ry)]])
    Rz = np.array([[np.cos(rz), -np.sin(rz), 0], [np.sin(rz), np.cos(rz), 0], [0, 0, 1]])
    return Rz @ Rx @ Ry @ np.diag([1.0, -1.0, -1.0])


# ---------------------------------------------------------------------------
# selection strategies (online, given the current result and history)
# ---------------------------------------------------------------------------

def _param_gain(result, cand, kind="A"):
    x = rig_vector(result)
    free = free_rig_mask(result)
    idx = np.where(free)[0]
    J = candidate_jacobian(x, cand.R, cand.t, plate.DEFAULT_PITCH_MM)
    if J is None:
        return -1e30
    Jr = J[:, :39][:, idx]; Jv = J[:, 39:]
    H = Jr.T @ Jr - Jr.T @ Jv @ np.linalg.pinv(Jv.T @ Jv, rcond=1e-10) @ Jv.T @ Jr
    C = rig_covariance(result)[np.ix_(idx, idx)]
    H = H / max((result.info.get("covariance", {}) or {}).get("sigma_px", 0.3) ** 2, 1e-12)
    C1 = _inv_spd(_inv_spd(C) + H)
    if kind == "A":
        return float(np.trace(C) - np.trace(C1))
    w = np.linalg.eigvalsh(C1)
    return float(np.log(max(np.prod(np.maximum(np.linalg.eigvalsh(C), 1e-30)), 1e-300))
                 - np.log(max(np.prod(np.maximum(w, 1e-30)), 1e-300)))


def _ere_gain(result, cand, targets):
    """AprilCal-style expected reprojection error over target points.

    The predicted squared reprojection error of each target point in the left
    camera, using the rig-covariance update implied by the candidate.
    """
    x = rig_vector(result)
    free = free_rig_mask(result); idx = np.where(free)[0]
    J = candidate_jacobian(x, cand.R, cand.t, plate.DEFAULT_PITCH_MM)
    if J is None:
        return -1e30
    Jr = J[:, :39][:, idx]; Jv = J[:, 39:]
    H = Jr.T @ Jr - Jr.T @ Jv @ np.linalg.pinv(Jv.T @ Jv, rcond=1e-10) @ Jv.T @ Jr
    sig2 = max((result.info.get("covariance", {}) or {}).get("sigma_px", 0.3) ** 2, 1e-12)
    H = H / sig2
    # projection Jacobian of the target points wrt rig params (left camera)
    cams, extr = objective._split_rig(x)
    Gp = np.zeros((len(targets), 2, len(idx)))
    for j, jj in enumerate(idx):
        h = 1e-6 * max(1.0, abs(x[jj]))
        xp = np.array(x, copy=True); xp[jj] += h
        cams2, extr2 = objective._split_rig(xp)
        a = objective._pixels(cams, extr, "left", targets)
        b = objective._pixels(cams2, extr2, "left", targets)
        Gp[:, :, j] = (b - a) / h
    C = rig_covariance(result)[np.ix_(idx, idx)]
    C1 = _inv_spd(_inv_spd(C) + H)
    var0 = np.einsum("pij,jk,plk->p", Gp, C, Gp)
    var1 = np.einsum("pij,jk,plk->p", Gp, C1, Gp)
    return float(np.sum(var0 - var1))


def _distance(a, b):
    Ra, ta = (a.R, a.t) if hasattr(a, "R") else a
    Rb, tb = (b.R, b.t) if hasattr(b, "R") else b
    ang = np.degrees(np.arccos(np.clip(
        (np.trace(np.asarray(Ra) @ np.asarray(Rb).T) - 1) / 2, -1, 1)))
    return ang, abs(float(ta[2] - tb[2]))


def _diverse(cands, selected, min_deg=8.0, min_mm=40.0):
    out = []
    for c in cands:
        ok = True
        for s in selected:
            ang, dm = _distance((c.R, c.t), s)
            if ang < min_deg and dm < min_mm:
                ok = False; break
        if ok:
            out.append(c)
    return out


def select_online(strategy, result, pool, selected, rng, targets,
                  effort_weight=0.05, task_cache=None):
    cands = _diverse(pool, selected)
    if not cands:
        cands = pool
    if strategy in ("kalibr_param", "fisher_param"):
        scored = [(c, _param_gain(result, c, "A" if strategy == "fisher_param" else "D"))
                  for c in cands]
    elif strategy == "aprilcal":
        scored = [(c, _ere_gain(result, c, targets)) for c in cands]
    elif strategy in ("task_volume", "task_effort", "task_depth", "hybrid"):
        kind = "depth" if strategy == "task_depth" else "A"
        scored = []
        for c in cands:
            eff = (objective.pose_effort(selected[-1].R, selected[-1].t, c)
                   if strategy in ("task_effort", "hybrid") and selected else 0.0)
            s = objective.score_candidate(result, c, targets, kind, effort=eff,
                                          effort_weight=effort_weight, cache=task_cache)
            if s is not None:
                scored.append((c, s.utility))
    else:
        raise ValueError(strategy)
    scored.sort(key=lambda z: z[1], reverse=True)
    return scored[0][0] if scored else cands[0]


# ---------------------------------------------------------------------------
# one strategy run
# ---------------------------------------------------------------------------

def run_one(args):
    strategy, trial, seed, noise, outlier, missing, budgets, budget_max = args
    scene = make_scene(n_views=25, noise_px=noise, outlier_frac=outlier, seed=seed)
    rng = np.random.default_rng(seed * 1000 + 7)
    init = perturbed_init(scene, rng)
    pool = build_pool()
    targets = objective.measurement_volume(n_z=4, n_xy=3)
    start = ViewCandidate(400.0, 0.0, 0.0, 0.0, *board_pose(400.0, 0, 0, 0), "start")
    if strategy == "fixed_vendor":
        selected = vendor_poses(pool)
    elif strategy == "uniform":
        selected = uniform_poses()
    elif strategy == "random_diverse":
        selected = [start]
        while len(selected) < budget_max:
            c = pool[int(rng.integers(len(pool)))]
            if _diverse([c], selected):
                selected.append(c)
    else:
        selected = [start]
        result = None
        cache = {}
        for step in range(1, budget_max):
            if strategy == "hybrid" and len(selected) < 4:
                # coverage-first bootstrap: the easiest pose that is still
                # diverse from every accepted view (same rule as the session)
                cands = []
                for c in pool:
                    dmin = min(_distance(c, s)[0] / 8.0 + _distance(c, s)[1] / 40.0
                               for s in selected)
                    if dmin >= 1.0:
                        eff = objective.pose_effort(selected[-1].R, selected[-1].t, c)
                        cands.append((eff, c))
                if cands:
                    cands.sort(key=lambda z: z[0])
                    selected.append(cands[0][1])
                else:
                    selected.append(pool[int(rng.integers(len(pool)))])
                continue
            if result is None or step % 2 == 0:
                obs_so_far = []
                for i, c in enumerate(selected):
                    obs_so_far += make_obs(scene, (c.R, c.t), i, noise, outlier, missing,
                                           np.random.default_rng(seed * 10000 + i))
                result = _solve(obs_so_far, init, free_kc=len(selected) >= 10,
                                max_nfev=400)
                cache = {}
            c = select_online(strategy, result, pool, selected, rng, targets,
                              task_cache=cache)
            selected.append(c)
    # evaluate prefixes at the requested budgets
    out = {"strategy": strategy, "trial": trial, "seed": seed, "noise": noise,
           "outlier": outlier, "missing": missing, "budgets": {}}
    for k in budgets:
        k = min(k, len(selected))
        obs = []
        for i, c in enumerate(selected[:k]):
            obs += make_obs(scene, (c.R, c.t), i, noise, outlier, missing,
                            np.random.default_rng(seed * 10000 + i))
        t0 = time.time()
        free_kc = k >= 10 and strategy not in ("fixed_vendor",)  # vendor also frees at 25
        res = _solve(obs, init, free_kc=(k >= 10))
        solve_s = time.time() - t0
        res._obs = obs
        m = metrics(scene, res, [ (c.R, c.t) for c in selected[:k]])
        m["solve_s"] = solve_s
        m["n_views"] = k
        out["budgets"][str(k)] = m
    out["selected"] = [c.label for c in selected]
    out["n_selected"] = len(selected)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=6)
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 4) - 2))
    ap.add_argument("--budget", type=int, default=25)
    ap.add_argument("--noise", type=float, default=0.2)
    ap.add_argument("--outlier", type=float, default=0.02)
    ap.add_argument("--missing", type=float, default=0.05)
    ap.add_argument("--out", default="strategy_results.json")
    ap.add_argument("--strategies", default="fixed_vendor,uniform,random_diverse,kalibr_param,fisher_param,aprilcal,task_volume,task_effort,task_depth,hybrid")
    args = ap.parse_args(argv)
    strategies = [s for s in args.strategies.split(",") if s]
    jobs = []
    for trial in range(args.trials):
        for s in strategies:
            jobs.append((s, trial, 100 + trial, args.noise, args.outlier,
                         args.missing, BUDGETS, args.budget))
    t0 = time.time()
    with Pool(args.workers) as pool:
        results = pool.map(run_one, jobs)
    json.dump({"meta": {"trials": args.trials, "noise": args.noise,
                        "outlier": args.outlier, "missing": args.missing,
                        "budgets": list(BUDGETS), "seconds": time.time() - t0,
                        "strategies": strategies},
               "runs": results}, open(args.out, "w"), indent=1)
    print(f"wrote {args.out}: {len(results)} runs in {time.time() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
