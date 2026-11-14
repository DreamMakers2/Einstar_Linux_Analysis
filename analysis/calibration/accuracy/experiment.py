#!/usr/bin/env python
"""Does enabling partial-board views change calibration accuracy or conditioning?

Experiment for objective section 9.  Three observation policies are compared
under one independent validation protocol:

  (a) FULL    -- only views in which all three cameras detected the whole
                 8x5 marker field (40/40/40),
  (b) ALL     -- every accepted observation, partial boards included,
  (c) HYBRID  -- the full-board core plus a bounded number K of partial views.

Data
----
``data_fresh01.json`` from :mod:`prepare_data` (keyed ``(u, v)`` observations
recovered from the stored PNGs with a verified identity witness).  Only
observations that pass an arm-independent screen are used:

* the detection's identity witness verified (chiral large-marker evidence),
* at least ``--min-markers`` markers,
* a PnP residual below ``--screen-px`` under the **factory** model (the same
  3 px prune the wizard's ``_prune_outliers`` applies, but evaluated with the
  prior model, so no arm can influence the screen).

Model / policy
--------------
The production call exactly:

    calibrate(obs, 28.0, factory_cameras, free_kc=(n_views >= kc_views),
              robust_loss='huber', f_scale=0.8, backend='fast')

with the ``high`` mode policy from :mod:`calib.session` (``kc_views`` = 8,
``f_scale`` = 0.8, Huber).  ``free_kc`` is therefore True for every arm here.

Validation
----------
Repeated k-fold: R repeats of k-fold assignment over the whole view pool.  A
held-out view is scored *without* refitting: its board pose is PnP-solved from
its **left** observation with the fitted left intrinsics, and every camera's
pixels are then predicted through the fitted rig
(``X_c = R_c X_left + t_c``) and the fitted intrinsics of that camera.  The
test views, and therefore the paired comparison, are identical for every arm
inside a (repeat, fold).

Reported held-out quantities per view:
  ``rms_left``   -- left only (intrinsics check; the pose is fitted to it)
  ``rms_right``  -- right predicted through the fitted stereo extrinsic
  ``rms_tex``    -- colour predicted through the fitted colour extrinsic
  ``rms_cross``  -- pooled right+tex (primary: tests extrinsics + intrinsics)
  ``rms_all``    -- pooled over all three cameras

Usage::

    OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 \\
      .venv/bin/python analysis/calibration/accuracy/experiment.py --repeats 3
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO))

from calib import geometry, plate                        # noqa: E402
from calib import calibrate as calibrate_mod            # noqa: E402
from calib.calibrate import Observation                 # noqa: E402
from calib import quality                               # noqa: E402
from tools.calib_study import export as EX              # noqa: E402

FACTORY = Path.home() / "einstar-private/einstar-profiles/factory-e10.json"
CAMS = ("left", "right", "tex")
#: ``high`` mode policy, the strictest production policy (calib.session.MODES)
POLICY = {"free_kc": True, "robust_loss": "huber", "f_scale": 0.8, "kc_views": 8}


# ---------------------------------------------------------------------------
# data
# ---------------------------------------------------------------------------

def load_dataset(path: Path, session: str, min_markers: int, screen_px: float):
    """Return the screened observation table and a description of it."""
    raw = json.loads(Path(path).read_text())
    sess = raw["sessions"][session]
    views = sess["views"]
    cams, _ = EX.profile_to_cameras(EX.load_profile(FACTORY))
    obj = plate.board_points_mm(28.0)

    table: dict[int, dict] = {}
    screen = {"n_views": len(views), "dropped_identity": 0, "dropped_few": 0,
              "dropped_residual": 0, "dropped_items": [], "kept_items": 0}
    for key, rec in views.items():
        vid = int(key)
        cams_ok = {}
        for cam in CAMS:
            det = rec["cams"].get(cam) or {}
            n = int(det.get("n") or 0)
            if not det.get("identity_ok") or n == 0:
                screen["dropped_identity"] += 1
                continue
            if n < int(min_markers):
                screen["dropped_few"] += 1
                continue
            uv = {(float(u), float(v)): np.array([x, y], float)
                  for u, v, x, y in det["uv"]}
            keys = sorted(uv)
            idx = np.array([int(v) * plate.GRID_COLS + int(u) for u, v in keys])
            img = np.array([uv[k] for k in keys], float)
            pose = geometry.solve_pnp(obj[idx], img, cams[cam].K, cams[cam].kc)
            if pose is None:
                screen["dropped_residual"] += 1
                screen["dropped_items"].append(
                    {"view": vid, "camera": cam, "n": n, "reason": "pnp failed"})
                continue
            X = (pose.R @ obj[idx].T).T + pose.t
            pred = geometry.project(cams[cam].K, cams[cam].kc, X)
            rms = float(np.sqrt(np.mean(np.sum((pred - img) ** 2, axis=1))))
            if rms > float(screen_px):
                screen["dropped_residual"] += 1
                screen["dropped_items"].append(
                    {"view": vid, "camera": cam, "n": n,
                     "factory_pnp_rms_px": round(rms, 3), "reason": "residual"})
                continue
            screen["kept_items"] += 1
            cams_ok[cam] = {"uv": uv, "n": n, "factory_pnp_rms": rms,
                            "pose": pose}
        table[vid] = {"cams": cams_ok, "meta": rec.get("meta") or {},
                      "stored_n": rec.get("stored_n")}

    # a view can train only if the stereo pair is present; tex is optional
    pool = {v: r for v, r in table.items()
            if "left" in r["cams"] and "right" in r["cams"]}
    for v, r in pool.items():
        ns = [r["cams"][c]["n"] for c in CAMS if c in r["cams"]]
        r["full"] = all(r["cams"].get(c, {}).get("n") == 40 for c in CAMS)
        r["n_markers"] = {c: (r["cams"][c]["n"] if c in r["cams"] else 0)
                          for c in CAMS}
        r["min_markers"] = int(min(ns)) if ns else 0
        r["max_markers"] = int(max(ns)) if ns else 0
    desc = {"session": session, "root": sess.get("root"),
            "n_pool_views": len(pool), "n_full": sum(r["full"] for r in pool.values()),
            "n_partial": sum(not r["full"] for r in pool.values()),
            "screen": screen}
    return pool, desc


def view_geometry(pool: dict) -> dict:
    """Per-view coverage descriptors from the factory-model left pose."""
    out = {}
    for vid, rec in pool.items():
        pose = rec["cams"]["left"]["pose"]
        R = pose.R
        n_cam = R[:, 2]                     # board normal in the left camera
        tx = math.degrees(math.atan2(float(n_cam[0]), float(n_cam[2])))
        ty = math.degrees(math.atan2(float(n_cam[1]), float(n_cam[2])))
        roll = math.degrees(math.atan2(float(R[1, 0]), float(R[0, 0])))
        uv = np.array([list(k) for k in rec["cams"]["left"]["uv"]], float)
        img = np.array([rec["cams"]["left"]["uv"][tuple(k)] for k in uv], float)
        out[vid] = {"tilt_x_deg": tx, "tilt_y_deg": ty, "roll_deg": roll,
                    "distance_mm": float(np.linalg.norm(pose.t)),
                    "centroid_x": float(img[:, 0].mean()),
                    "centroid_y": float(img[:, 1].mean()),
                    "spread_x": float(np.ptp(img[:, 0])),
                    "spread_y": float(np.ptp(img[:, 1])),
                    "quadrant": int((img[:, 0].mean() > 640.0)) * 2
                                + int((img[:, 1].mean() > 512.0))}
    return out


def coverage_summary(view_ids, pool, geom) -> dict:
    ids = sorted(int(v) for v in view_ids)
    if not ids:
        return {}
    tx = np.array([geom[v]["tilt_x_deg"] for v in ids])
    ty = np.array([geom[v]["tilt_y_deg"] for v in ids])
    roll = np.array([geom[v]["roll_deg"] for v in ids])
    dist = np.array([geom[v]["distance_mm"] for v in ids])
    roll_sector = ((roll + 360.0) % 360.0) // 90.0
    quad = np.array([geom[v]["quadrant"] for v in ids])
    mk = np.array([pool[v]["min_markers"] for v in ids])
    n_partial = sum(1 for v in ids if not pool[v]["full"])

    def frac(x):
        return float(np.mean(x)) if len(x) else None

    return {
        "n_views": len(ids),
        "n_partial_views": int(n_partial),
        "n_full_views": int(len(ids) - n_partial),
        "n_marker_obs": int(sum(sum(1 for c in pool[v]["cams"]) for v in ids)),
        "markers_min": int(mk.min()), "markers_median": float(np.median(mk)),
        "markers_total": int(sum(pool[v]["cams"][c]["n"] for v in ids
                                 for c in pool[v]["cams"])),
        "distance_mm": {"min": float(dist.min()), "max": float(dist.max()),
                        "median": float(np.median(dist)),
                        "n_100mm_bins": int(len(np.unique(np.round(dist / 100.0))))},
        "tilt_x_deg": {"min": float(tx.min()), "max": float(tx.max()),
                       "median": float(np.median(tx)),
                       "frac_pos": frac(tx > 1.0), "frac_neg": frac(tx < -1.0),
                       "frac_flat": frac(np.abs(tx) <= 1.0)},
        "tilt_y_deg": {"min": float(ty.min()), "max": float(ty.max()),
                       "median": float(np.median(ty)),
                       "frac_pos": frac(ty > 1.0), "frac_neg": frac(ty < -1.0),
                       "frac_flat": frac(np.abs(ty) <= 1.0)},
        "roll_sectors": {str(int(s)): int((roll_sector == s).sum())
                         for s in range(4)},
        "image_quadrants": {str(q): int((quad == q).sum()) for q in range(4)},
        "n_quadrants": int(len(np.unique(quad))),
    }


# ---------------------------------------------------------------------------
# fit / evaluation
# ---------------------------------------------------------------------------

def observations_for(pool, view_ids) -> list[Observation]:
    obs = []
    for v in sorted(int(x) for x in view_ids):
        rec = pool[v]
        for cam in CAMS:
            c = rec["cams"].get(cam)
            if c is None:
                continue
            obs.append(Observation(cam, v, c["uv"]))
    return obs


def _observation_residuals(res, pool, view_ids) -> dict[tuple[int, str], float]:
    """In-sample per-camera-view RMS under the fitted model."""
    obj = plate.board_points_mm(28.0)
    out = {}
    seen_views = set(int(o.view) for o in observations_for(pool, view_ids))
    for v in sorted(seen_views):
        rec = pool[v]
        try:
            R, t = res.view_pose(v)
        except Exception:                                # pragma: no cover
            continue
        for cam in CAMS:
            c = rec["cams"].get(cam)
            if c is None:
                continue
            cam_r = res.cameras[cam]
            Rc, tc = res.extrinsics[cam]
            keys = sorted(c["uv"])
            idx = np.array([int(vv) * plate.GRID_COLS + int(uu) for uu, vv in keys])
            img = np.array([c["uv"][k] for k in keys], float)
            Xl = (R @ obj[idx].T).T + t
            Xc = (Rc @ Xl.T).T + tc
            pred = geometry.project(cam_r.K, cam_r.kc, Xc)
            out[(int(v), cam)] = float(
                np.sqrt(np.mean(np.sum((pred - img) ** 2, axis=1))))
    return out


def fit(pool, view_ids, cams, *, prune_px: float = 3.0):
    """Production solve + the wizard's finish-time outlier prune and refit.

    ``wizard._prune_outliers`` drops per-camera observations whose in-sample
    residual exceeds 3 px before the final solve, because one bad view-camera
    pair otherwise poisons the model.  The same rule is applied here with the
    fitted model (training data only, so no held-out leakage), identically for
    every arm, and the number of dropped observations is reported.
    """
    def _solve(obs):
        n_views = len(set(int(o.view) for o in obs))
        free_kc = bool(POLICY["free_kc"] and n_views >= POLICY["kc_views"])
        res = calibrate_mod.calibrate(obs, 28.0, cams, free_kc=free_kc,
                                      robust_loss=POLICY["robust_loss"],
                                      f_scale=POLICY["f_scale"], backend="fast")
        return res

    t0 = time.time()
    obs = observations_for(pool, view_ids)
    res = _solve(obs)
    pruned = []
    if prune_px and prune_px > 0:
        resid = _observation_residuals(res, pool, view_ids)
        bad = {k for k, r in resid.items() if r > prune_px}
        if bad:
            keep = [o for o in obs if (int(o.view), o.camera) not in bad]
            pruned = sorted(({"view": int(v), "camera": c,
                              "rms_px": round(resid[(v, c)], 3)}
                             for v, c in bad),
                            key=lambda d: (d["view"], d["camera"]))
            if keep:
                obs = keep
                res = _solve(obs)
    dt = time.time() - t0
    cov = res.info.get("covariance") or {}
    ps = res.param_std()
    rig = {k: float(v) for k, v in ps.items()}
    views_used = sorted(set(int(o.view) for o in obs))
    metrics = {
        "n_views": int(len(views_used)),
        "n_observations": int(res.n_observations),
        "n_parameters": int(res.n_parameters),
        "train_rms_px": float(res.residual_rms),
        "train_max_px": float(res.residual_max),
        "free_kc": bool(res.info.get("free_kc")),
        "seconds": dt,
        "success": bool(res.info.get("success")),
        "nfev": int(res.info.get("nfev") or 0),
        "sigma_px": cov.get("sigma_px"),
        "condition": cov.get("condition"),
        "condition_scaled": cov.get("condition_scaled"),
        "eig_min": cov.get("eig_min"),
        "rank": cov.get("rank"), "n_free": cov.get("n_free"),
        "estimator": cov.get("estimator"),
        "sigma_fx": {c: rig.get(f"{c}.fx") for c in CAMS},
        "sigma_cx": {c: rig.get(f"{c}.cx") for c in CAMS},
        "sigma_cy": {c: rig.get(f"{c}.cy") for c in CAMS},
        "sigma_k1": {c: rig.get(f"{c}.k1") for c in CAMS},
        "baseline_sigma_mm": float(quality.baseline_sigma(res)),
        "rot_ir_sigma_deg": float(quality.relative_pose_sigma(res, "left", "right")["rotation_deg"]),
        "tex_rotation_sigma_deg": float(quality.relative_pose_sigma(res, "left", "tex")["rotation_deg"]),
        "tex_translation_sigma_mm": float(quality.relative_pose_sigma(res, "left", "tex")["translation_mm"]),
        "rgb_pixel_sigma_at_400mm": float(quality.rgb_alignment(res)["pixel_at_distance"]),
        "baseline_mm": float(np.linalg.norm(res.baseline())),
        "tex_baseline_mm": float(np.linalg.norm(res.baseline("left", "tex"))),
        "n_pruned": int(len(pruned)),
        "pruned": pruned,
    }
    return res, metrics, views_used


def evaluate_holdout(res, pool, view_ids) -> list[dict]:
    """Score held-out views without refitting them."""
    obj = plate.board_points_mm(28.0)
    out = []
    for v in sorted(int(x) for x in view_ids):
        rec = pool[v]
        left = rec["cams"].get("left")
        if left is None or left["n"] < 12:
            out.append({"view": v, "valid": False, "reason": "left markers"})
            continue
        cam_l = res.cameras["left"]
        keys = sorted(left["uv"])
        idx = np.array([int(vv) * plate.GRID_COLS + int(uu) for uu, vv in keys])
        img = np.array([left["uv"][k] for k in keys], float)
        pose = geometry.solve_pnp(obj[idx], img, cam_l.K, cam_l.kc)
        if pose is None:
            out.append({"view": v, "valid": False, "reason": "pnp failed"})
            continue
        X_left = (pose.R @ obj[idx].T).T + pose.t
        row = {"view": v, "valid": True, "n_left": int(left["n"]),
               "partial": bool(not rec["full"])}
        sq = {}
        for cam in CAMS:
            c = rec["cams"].get(cam)
            if c is None:
                continue
            cam_r = res.cameras[cam]
            Rc, tc = res.extrinsics[cam]
            Xc = (Rc @ X_left.T).T + tc
            pred = geometry.project(cam_r.K, cam_r.kc, Xc)
            # only markers this camera actually saw
            seen = [i for i, k in enumerate(keys) if k in c["uv"]]
            if len(seen) < 4:
                continue
            obs_px = np.array([c["uv"][keys[i]] for i in seen], float)
            d2 = np.sum((pred[seen] - obs_px) ** 2, axis=1)
            sq[cam] = {"n": int(len(seen)), "mse": float(d2.mean())}
            row[f"rms_{cam}"] = float(np.sqrt(d2.mean()))
        cross = [sq[c]["mse"] for c in ("right", "tex") if c in sq]
        allc = [sq[c]["mse"] for c in CAMS if c in sq]
        if cross:
            row["rms_cross"] = float(np.sqrt(np.mean(cross)))
        if allc:
            row["rms_all"] = float(np.sqrt(np.mean(allc)))
        out.append(row)
    return out


# ---------------------------------------------------------------------------
# arms
# ---------------------------------------------------------------------------

def pick(rng, ids, k):
    ids = list(ids)
    if k >= len(ids):
        return ids
    return sorted(int(x) for x in rng.choice(np.asarray(ids), size=int(k),
                                            replace=False))


def build_arms(rng, full_train, partial_train, min_markers, *, hybrid_ks=(10, 20),
               clipped_max=36):
    """Return [(arm_name, view_ids, note), ...] for one fold."""
    all_train = sorted(full_train) + sorted(partial_train)
    n = len(full_train)
    arms = [("A_full", list(full_train), "full-board only")]
    arms.append(("B_all", list(all_train), "every accepted view"))
    for d in range(3):
        arms.append((f"C_mix{d}", pick(rng, all_train, n),
                     "matched count, random from all views"))
    for d in range(3):
        n_full = int(round(2 * n / 3))
        ids = pick(rng, full_train, n_full) + pick(rng, partial_train, n - n_full)
        arms.append((f"D_2f1p{d}", sorted(ids), "matched count, 2/3 full + 1/3 partial"))
    for d in range(3):
        n_full = int(round(n / 3))
        ids = pick(rng, full_train, n_full) + pick(rng, partial_train, n - n_full)
        arms.append((f"E_1f2p{d}", sorted(ids), "matched count, 1/3 full + 2/3 partial"))
    q = sorted(partial_train, key=lambda v: (-min_markers[v], v))
    for k in hybrid_ks:
        arms.append((f"F_h{k}B", sorted(list(full_train) + q[:k]),
                     f"full core + {k} best partial"))
    for d in range(3):
        arms.append((f"G_h10R{d}", sorted(list(full_train) + pick(rng, partial_train, 10)),
                     "full core + 10 random partial"))
    clipped = [v for v in q if min_markers[v] <= clipped_max]
    if len(clipped) >= 10:
        arms.append(("H_h10C", sorted(list(full_train) + clipped[:10]),
                     "full core + 10 most-clipped partial"))
    return arms


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default=str(HERE / "data_fresh01.json"))
    ap.add_argument("--session", default="fresh01")
    ap.add_argument("--out", default=str(HERE / "results_fresh01.json"))
    ap.add_argument("--folds", type=int, default=4)
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--seed", type=int, default=20261114)
    ap.add_argument("--min-markers", type=int, default=12)
    ap.add_argument("--screen-px", type=float, default=3.0)
    ap.add_argument("--limit-arms", default="", help="debug: comma-separated prefixes")
    args = ap.parse_args(argv)

    pool, desc = load_dataset(Path(args.data), args.session,
                              args.min_markers, args.screen_px)
    geom = view_geometry(pool)
    cams, _ = EX.profile_to_cameras(EX.load_profile(FACTORY))

    full = sorted(v for v, r in pool.items() if r["full"])
    partial = sorted(v for v, r in pool.items() if not r["full"])
    print(f"pool {len(pool)} views: {len(full)} full, {len(partial)} partial; "
          f"screen dropped {desc['screen']['dropped_items']}", flush=True)

    profile_sha = hashlib.sha256(FACTORY.read_bytes()).hexdigest()
    try:
        commit = subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"],
                                capture_output=True, text=True).stdout.strip()
    except Exception:                                    # pragma: no cover
        commit = ""
    result = {
        "schema": "einstar.partial-vs-full/1",
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "provenance": {"git_commit": commit, "host": platform.node(),
                       "factory_profile": str(FACTORY),
                       "factory_profile_sha256": profile_sha,
                       "python": sys.version.split()[0],
                       "numpy": np.__version__,
                       "blas_threads": {k: os.environ.get(k) for k in
                                        ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS",
                                         "MKL_NUM_THREADS")}},
        "protocol": {
            "folds": args.folds, "repeats": args.repeats, "seed": args.seed,
            "policy": dict(POLICY), "min_markers": args.min_markers,
            "screen_px": args.screen_px,
            "model": "calib.calibrate.calibrate(..., backend='fast')",
            "holdout": ("left-anchored PnP with the fitted left model, other "
                        "cameras predicted through the fitted rig"),
            "primary_metric": "held-out rms_cross (right+tex, px)",
        },
        "dataset": desc,
        "coverage": {
            "full": coverage_summary(full, pool, geom),
            "partial": coverage_summary(partial, pool, geom),
            "all": coverage_summary(full + partial, pool, geom),
        },
        "fits": [],
    }

    for rep in range(args.repeats):
        perm = np.random.default_rng(args.seed + rep).permutation(len(pool))
        folds = np.array_split(perm, args.folds)
        order = list(pool)
        for f, test_idx in enumerate(folds):
            test_views = sorted(order[int(i)] for i in test_idx)
            test_set = set(test_views)
            full_train = [v for v in full if v not in test_set]
            partial_train = [v for v in partial if v not in test_set]
            rng = np.random.default_rng([args.seed, rep, f])
            arms = build_arms(rng, full_train, partial_train,
                              {v: r['min_markers'] for v, r in pool.items()})
            if args.limit_arms:
                keep = tuple(args.limit_arms.split(","))
                arms = [a for a in arms if a[0].startswith(keep)]
            for name, ids, note in arms:
                ids = sorted(set(int(x) for x in ids))
                if not ids:
                    continue
                t0 = time.time()
                try:
                    res, met, used = fit(pool, ids, cams)
                except Exception as exc:                # pragma: no cover
                    print(f"  FAIL {name} rep{rep} fold{f}: {exc}", flush=True)
                    result["fits"].append({"arm": name, "repeat": rep, "fold": f,
                                           "error": f"{type(exc).__name__}: {exc}"})
                    continue
                hold = evaluate_holdout(res, pool, test_views)
                result["fits"].append({
                    "arm": name, "note": note, "repeat": rep, "fold": f,
                    "train_views": ids,
                    "n_full_train": sum(1 for v in ids if pool[v]["full"]),
                    "n_partial_train": sum(1 for v in ids if not pool[v]["full"]),
                    "views_used": used,
                    "metrics": met,
                    "holdout": hold,
                })
                rms = [h["rms_cross"] for h in hold if h.get("rms_cross")]
                print(f"  rep{rep} fold{f} {name:9s} n={met['n_views']:3d} "
                      f"train {met['train_rms_px']:.3f}px hold "
                      f"med {np.median(rms):.3f} p95 {np.percentile(rms, 95):.3f} "
                      f"baseline_sig {met['baseline_sigma_mm']:.3f} "
                      f"({time.time() - t0:.1f}s)", flush=True)
            Path(args.out).write_text(json.dumps(result))
    Path(args.out).write_text(json.dumps(result))
    print("wrote", args.out, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
