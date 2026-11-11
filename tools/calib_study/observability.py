#!/usr/bin/env python3
"""Phase 4 calibration-observability study on the real live sweeps.

    .venv/bin/python tools/calib_study/observability.py \
        --sweep ~/einstar-private/sweep-c/frames \
        --sweep2 ~/einstar-private/sweep-b/frames \
        --profile ~/einstar-private/einstar-profiles/factory-e10.json \
        --vendor-corpus ~/einstar-private/vendor-calib-rapid \
        --out ~/einstar-private/analysis/calib_study

Everything is measured on the real frames; nothing is simulated and nothing is
written outside ``--out``.  See ``README.md`` for the method and
``REPORT.md`` (written into ``--out``) for the findings.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from calib import plate                                    # noqa: E402
from calib.calibrate import CAMERAS, CameraIntrinsics, Observation   # noqa: E402
from calib import detect as cdetect                        # noqa: E402

from tools.calib_study import frames as F                  # noqa: E402
from tools.calib_study import models as M                  # noqa: E402
from tools.calib_study import motion as MOT                # noqa: E402
from tools.calib_study import identify as ID               # noqa: E402
from tools.calib_study import export as EX                 # noqa: E402


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def jsonable(o):
    if isinstance(o, dict):
        return {str(k): jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [jsonable(v) for v in o]
    if isinstance(o, (np.floating, np.integer)):
        return o.item()
    if isinstance(o, np.ndarray):
        return o.tolist()
    if isinstance(o, (np.bool_,)):
        return bool(o)
    if isinstance(o, float) and not np.isfinite(o):
        return None
    return o


def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(jsonable(payload), indent=1, sort_keys=False) + "\n")


def sha256_text(s: str) -> str:
    import hashlib
    return hashlib.sha256(s.encode()).hexdigest()


def git_commit() -> str:
    try:
        p = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"],
                           capture_output=True, text=True, timeout=10)
        dirty = subprocess.run(["git", "-C", str(ROOT), "status", "--porcelain"],
                               capture_output=True, text=True, timeout=10)
        return p.stdout.strip() + ("+dirty" if dirty.stdout.strip() else "")
    except Exception:
        return "unknown"


def env_info() -> dict:
    import cv2, scipy
    return {"python": platform.python_version(), "numpy": np.__version__,
            "opencv": cv2.__version__, "scipy": scipy.__version__,
            "platform": platform.platform(), "git": git_commit(),
            "host": platform.node(),
            "started_utc": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")}


def load_factory(path: str | Path) -> tuple[dict, dict, dict]:
    prof = EX.load_profile(path)
    cams, extr = EX.profile_to_cameras(prof)
    return prof, cams, extr


# --------------------------------------------------------------------------
# stage 1: data
# --------------------------------------------------------------------------

def stage_data(args, log) -> dict:
    prof, cams, extr = load_factory(args.profile)
    out = {"profile_path": str(Path(os.path.expanduser(args.profile)).resolve()),
           "profile_sha256": EX.sha256_file(os.path.expanduser(args.profile)),
           "profile_id_note": "profile 'id' is a C++-side hash of the numeric content",
           "factory": {"cameras": {c: {"fx": cams[c].fx, "fy": cams[c].fy,
                                       "cx": cams[c].cx, "cy": cams[c].cy,
                                       "skew_px": cams[c].skew * cams[c].fx,
                                       "dist": [float(x) for x in cams[c].kc]}
                                   for c in CAMERAS},
                       "extrinsics": {c: {"R": extr[c][0].tolist(), "T": extr[c][1].tolist()}
                                      for c in ("right", "tex")},
                       "baseline_right_mm": float(np.linalg.norm(extr["right"][1])),
                       "baseline_tex_mm": float(np.linalg.norm(extr["tex"][1])),
                       "plate_pitch_mm": float(prof.get("plate_pitch_mm", 28.0)),
                       "color_status": prof.get("color_status")}}

    cache = Path(args.out) / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    sweeps, dets = {}, {}
    for key, path in (("sweep_c", args.sweep), ("sweep_b", args.sweep2)):
        if not path:
            continue
        t = time.time()
        sw = F.load_sweep(path)
        det = F.detect_sweep(sw, cache=cache / f"{sw.root.parent.name}_detections.json")
        sweeps[key] = sw
        dets[key] = det
        out[key] = {
            "root": str(sw.root.resolve()),
            "n_groups": len(sw.groups),
            "manifest_schema": sw.schema,
            "manifest_device_groups": [int(sw.device_groups()[0]),
                                       int(sw.device_groups()[-1])],
            "frame_id_wrap_check": _frame_id_wrap(sw),
            "duration_s": float(sw.manifest.get("duration_s", 0.0)),
            "cadence_fps": float(sw.manifest.get("cadence", {}).get("fps", 0.0)),
            "cadence_mean_ms": float(sw.manifest.get("cadence", {}).get("mean_ms", 0.0)),
            "lossy": not bool(sw.manifest.get("complete", False)),
            "detection": MOT.detection_stats(det),
            "detect_wall_s": time.time() - t,
        }
        log(f"  {sw.name}: {len(sw.groups)} groups, detection "
            f"{ {c: out[key]['detection'][c]['n_detected'] for c in CAMERAS} }")
    return {"info": out, "sweeps": sweeps, "dets": dets,
            "cams": cams, "extr": extr, "profile": prof}


def _frame_id_wrap(sw) -> dict:
    """``frame_ids`` is an 8-bit slot counter; check it really wraps and that
    ``device_group`` is the monotonic counter."""
    out = {}
    dg = sw.device_groups()
    out["device_group_monotonic"] = bool(np.all(np.diff(dg) > 0))
    out["device_group_step_min"] = int(np.min(np.diff(dg))) if len(dg) > 1 else None
    out["device_group_step_max"] = int(np.max(np.diff(dg))) if len(dg) > 1 else None
    for cam in (0, 1, 2):
        f = sw.frame_ids(cam)
        d = np.diff(f)
        out[f"cam{cam}_wraps"] = int(np.sum(d < -100))
        out[f"cam{cam}_unique_ids"] = int(len(np.unique(f)))
        out[f"cam{cam}_max_id"] = int(f.max())
        out[f"cam{cam}_consistent_with_device_group_step"] = bool(
            np.all((np.diff(f) - np.diff(dg)) <= 0))
    out["note"] = ("frame_ids repeat with period 256 in units of device_group; "
                   "group/device_group are the only usable sequence keys")
    return out


# --------------------------------------------------------------------------
# stage 2: assignment audit + trajectory + splits
# --------------------------------------------------------------------------

def stage_geometry(args, data, log) -> dict:
    dets = data["dets"]
    cams = data["cams"]
    out = {}
    out["assignment_audit"] = {
        key: MOT.assignment_stats(det) for key, det in dets.items()}
    # explicit dihedral test on a fixed subset: residuals are invariant
    det_c = dets["sweep_c"]
    sample = list(range(0, len(det_c["left"]), max(1, len(det_c["left"]) // 40)))[:40]
    dihedral = {h: {"rms": [], "verdicts": {}} for h in F.DIHEDRAL}
    for v in sample:
        for c in CAMERAS:
            d = det_c[c][v]
            if d is None:
                continue
            a = F.audit_assignment(d)
            for h in F.DIHEDRAL:
                dihedral[h]["rms"].append(a["hypotheses"][h]["homography_rms"])
            dihedral[h]["verdicts"][a["verdict"]] = \
                dihedral[h]["verdicts"].get(a["verdict"], 0) + 1
    for h in F.DIHEDRAL:
        r = np.array(dihedral[h]["rms"], float)
        dihedral[h] = {"n": int(len(r)),
                       "homography_rms_median": float(np.median(r)) if len(r) else None,
                       "homography_rms_max": float(r.max()) if len(r) else None,
                       "verdicts": dihedral[h]["verdicts"]}
    out["dihedral_test"] = {
        "cameras": dihedral,
        "note": ("all four maps are rigid rotations of the planar board, so the "
                 "homography RMS is identical by construction; only the large-marker "
                 "area signature and the off-lattice marker break the tie"),
    }
    # trajectory
    traj = {}
    for key, det in dets.items():
        traj[key] = MOT.trajectory(det, "left", cams["left"],
                                   float(data["profile"].get("plate_pitch_mm", 28.0)))
        out.setdefault(key, {})["trajectory"] = traj[key].summary()
        out[key]["legs"] = [[a, b] for a, b in traj[key].legs()]
        out[key]["image_coverage_left"] = MOT.image_coverage(
            det, "left", list(traj[key].view[traj[key].ok]))
        out[key]["image_coverage_right"] = MOT.image_coverage(
            det, "right", list(traj[key].view[traj[key].ok]))
        out[key]["image_coverage_tex"] = MOT.image_coverage(
            det, "tex", list(traj[key].view[traj[key].ok]))
    splits = MOT.build_splits(traj["sweep_c"], traj["sweep_b"],
                              n_train=args.train_views,
                              n_val_leg=args.val_views,
                              n_val_session=args.val_views)
    out["splits"] = splits
    out["train_group_ids"] = splits["train"]
    out["heldout_legs_group_ids"] = splits["heldout_legs"]
    out["heldout_session_group_ids"] = splits["heldout_session"]
    log(f"  legs: {splits['legs']}  train {len(splits['train'])} "
        f"held-out-legs {len(splits['heldout_legs'])} "
        f"held-out-session {len(splits['heldout_session'])}")
    return {"report": out, "traj": traj, "splits": splits}


# --------------------------------------------------------------------------
# stage 3: factory-solution linearisation
# --------------------------------------------------------------------------

def stage_factory_information(args, data, geo, log) -> dict:
    dets, cams, extr = data["dets"], data["cams"], data["extr"]
    train = geo["splits"]["train"]
    obs = MOT.make_obs(dets["sweep_c"], train, ("left", "right"))
    pitch = float(data["profile"].get("plate_pitch_mm", 28.0))
    spec_all = M.ModelSpec(
        {c: set(M.INTR_NAMES) for c in CAMERAS},
        {"right": set(M.EXTR_NAMES), "tex": set(M.EXTR_NAMES)}, False,
        "factory-linearisation")
    t = time.time()
    lin = M.linearise(obs, spec_all, cams, extr, pitch_mm=pitch)
    free = np.asarray(lin.info["free_mask"], bool)[:M.N_RIG]
    names = ID.rig_names()
    spec = ID.scaled_spectrum(lin.marginal_information, names, free, top=8)
    corr = ID.correlations(lin.rig_covariance, names, free, min_abs=0.5, top=20)
    rows = ID.parameter_table(lin, obs, free)
    log(f"  factory linearisation: {lin.info.get('n_residuals')} residuals, "
        f"cond(scaled S) = {spec.get('condition_number_scaled'):.3e}, "
        f"{time.time() - t:.1f}s")
    return {"report": {
        "n_residuals": lin.info.get("n_residuals"),
        "n_free": lin.info.get("n_free"),
        "jacobian_rank": lin.info.get("rank"),
        "condition_number_jacobian": lin.info.get("condition"),
        "spectrum": spec, "correlations": corr,
        "parameters": rows,
        "verdicts": ID.summarise_verdicts(rows),
        "wall_s": time.time() - t,
        "note": ("poses are PnP at the factory intrinsics; no rig parameter is "
                 "fitted, so these sigmas are the information content of the data "
                 "at the factory solution"),
    }, "fit": lin, "obs": obs}


# --------------------------------------------------------------------------
# stage 4: model ladder
# --------------------------------------------------------------------------

def _metrics(fit, obs_train_ir, obs_leg_ir, obs_sess_ir, data, cams_factory,
             extr_factory, obs_train_all=None, obs_leg_all=None, obs_sess_all=None):
    out = {"rms_px": fit.rms, "rms_max_px": fit.rms_max,
           "n_observations": fit.n_obs, "n_free_parameters": fit.n_free,
           "n_views": fit.n_views, "wall_s": fit.wall_s,
           "nfev": fit.info.get("nfev"), "success": fit.info.get("success"),
           "optimality": fit.info.get("optimality"),
           "message": fit.info.get("message"),
           "sigma_px": fit.info.get("sigma_px"),
           "rank": fit.info.get("rank"),
           "condition_jacobian": fit.info.get("condition")}
    out["train"] = M.evaluate(fit, obs_train_ir)
    out["heldout_legs"] = M.evaluate(fit, obs_leg_ir, solve_poses=True)
    out["heldout_session"] = M.evaluate(fit, obs_sess_ir, solve_poses=True)
    if obs_train_all is not None:
        out["train_including_color"] = M.evaluate(fit, obs_train_all, solve_poses=True)
        out["heldout_legs_including_color"] = M.evaluate(fit, obs_leg_all, solve_poses=True)
        out["heldout_session_including_color"] = M.evaluate(fit, obs_sess_all,
                                                            solve_poses=True)
    out["per_camera_pnp"] = M.pnp_residuals(fit, obs_train_ir)
    out["heldout_rig_prediction"] = {
        "legs": M.rig_residuals(fit, obs_leg_ir),
        "session": M.rig_residuals(fit, obs_sess_ir)}
    # parameter deltas vs factory
    delta = {}
    for c in CAMERAS:
        g0, g = cams_factory[c], fit.cams[c]
        delta[c] = {"dfx": g.fx - g0.fx, "dfy": g.fy - g0.fy,
                    "dcx": g.cx - g0.cx, "dcy": g.cy - g0.cy,
                    "dskew_px": g.skew * g.fx - g0.skew * g0.fx,
                    "dkc": (np.asarray(g.kc, float) - np.asarray(g0.kc, float)).tolist()}
    for c in ("right", "tex"):
        R, T = fit.extr[c]
        R0, T0 = extr_factory[c]
        dR = R @ R0.T
        ang = float(np.degrees(np.arccos(np.clip((np.trace(dR) - 1) / 2, -1, 1))))
        delta[c + "_extr"] = {"d_rotation_deg": ang,
                              "d_translation_mm": float(np.linalg.norm(T - T0)),
                              "baseline_mm": float(np.linalg.norm(T)),
                              "baseline_factory_mm": float(np.linalg.norm(T0)),
                              "d_baseline_mm": float(np.linalg.norm(T) - np.linalg.norm(T0))}
    out["delta_vs_factory"] = delta
    out["rig_vs_factory"] = M.rig_pose_delta(fit, extr_factory)
    free = np.asarray(fit.info["free_mask"], bool)[:M.N_RIG]
    rows = ID.parameter_table(fit, obs_train_ir, free)
    out["parameters"] = rows
    out["verdicts"] = ID.summarise_verdicts(rows)
    out["spectrum"] = ID.scaled_spectrum(fit.marginal_information, ID.rig_names(),
                                         free, top=6)
    out["correlations"] = ID.correlations(fit.rig_covariance, ID.rig_names(), free,
                                          min_abs=0.7, top=15)
    out["f_vs_depth"] = ID.f_vs_depth_correlation(fit, obs_train_ir, free)
    return out


def stage_ladder(args, data, geo, log):
    dets, cams, extr = data["dets"], data["cams"], data["extr"]
    splits = geo["splits"]
    pitch = float(data["profile"].get("plate_pitch_mm", 28.0))
    ir = ("left", "right")
    obs = {
        "train_ir": MOT.make_obs(dets["sweep_c"], splits["train"], ir),
        "legs_ir": MOT.make_obs(dets["sweep_c"], splits["heldout_legs"], ir),
        "sess_ir": MOT.make_obs(dets["sweep_b"], splits["heldout_session"], ir),
        "train_all": MOT.make_obs(dets["sweep_c"], splits["train"], CAMERAS),
        "legs_all": MOT.make_obs(dets["sweep_c"], splits["heldout_legs"], CAMERAS),
        "sess_all": MOT.make_obs(dets["sweep_b"], splits["heldout_session"], CAMERAS),
    }
    specs = M.model_ladder()
    log(f"  observations: train {len(obs['train_ir'])} IR / {len(obs['train_all'])} with colour"
        f"  legs {len(obs['legs_ir'])}  session {len(obs['sess_ir'])}")
    results, fits = {}, {}
    for name, spec in specs.items():
        t = time.time()
        # colour observations only enter where the colour camera is being calibrated
        fit_obs = obs["train_all"] if name == "M6" else obs["train_ir"]
        fit = M.fit(fit_obs, spec, cams, init_extr=extr, pitch_mm=pitch,
                    max_nfev=args.max_nfev, compute_covariance=True)
        m = _metrics(fit, obs["train_ir"], obs["legs_ir"], obs["sess_ir"], data,
                     cams, extr, obs["train_all"], obs["legs_all"], obs["sess_all"])
        m["model"] = spec.summary()
        m["fit_observations"] = len(fit_obs)
        m["color_in_fit"] = bool(name == "M6")
        if name == "M6":
            m["color_participation"] = {
                "n_color_observations_in_fit":
                    sum(1 for o in fit_obs if o.camera == "tex"),
                "n_color_views": len({o.view for o in fit_obs if o.camera == "tex"}),
                "train_rms_color_px": M.evaluate(fit, [o for o in obs["train_all"]
                                                       if o.camera == "tex"],
                                                 solve_poses=True)["rms"],
                "note": "color_status is NOT promoted; the factory colour record is stale"}
        results[name] = m
        fits[name] = fit
        log(f"  {name}: train(IR) {m['train']['rms']:.4f}  legs {m['heldout_legs']['rms']:.4f}"
            f"  session {m['heldout_session']['rms']:.4f}  nfev {fit.info.get('nfev')}"
            f"  {time.time() - t:.1f}s")
    return {"report": results, "fits": fits, "obs": obs}


def stage_bootstrap(args, data, geo, ladder, log):
    dets, cams, extr = data["dets"], data["cams"], data["extr"]
    splits = geo["splits"]
    pitch = float(data["profile"].get("plate_pitch_mm", 28.0))
    train = list(splits["train"])
    pool = MOT.even_views(train, args.bootstrap_views)
    specs = M.model_ladder()
    rng = np.random.default_rng(args.seed)
    out = {}
    for name, spec in specs.items():
        samples, fmask = [], None
        t = time.time()
        for b in range(args.bootstrap):
            ids = sorted(rng.choice(pool, len(pool), replace=True).tolist())
            obs = MOT.make_obs(dets["sweep_c"], ids, CAMERAS)
            try:
                fit = M.fit(obs, spec, cams, init_extr=extr, pitch_mm=pitch,
                            max_nfev=args.bootstrap_nfev, compute_covariance=False)
            except Exception as exc:                     # pragma: no cover
                log(f"    bootstrap {name}#{b} failed: {exc}")
                continue
            samples.append(np.asarray(fit.x[:M.N_RIG], float))
            if fmask is None:
                fmask = np.asarray(fit.info["free_mask"], bool)[:M.N_RIG]
        if not samples or fmask is None:
            out[name] = {"n": 0}
            continue
        A = np.stack(samples)
        sd = A.std(axis=0, ddof=1)
        names = ID.rig_names()
        rows = []
        for i, n in enumerate(names):
            if not fmask[i]:
                continue
            rows.append({"param": n, "bootstrap_std": float(sd[i]),
                         "mean": float(A[:, i].mean())})
        out[name] = {"n": len(samples), "wall_s": time.time() - t,
                     "n_views": len(pool), "params": rows,
                     "note": "view-level bootstrap of the training views "
                             f"({len(pool)} draws per resample, {args.bootstrap} resamples)"}
        log(f"  bootstrap {name}: {len(samples)} resamples in {time.time() - t:.1f}s")
    return out


def stage_path_dependence(args, data, geo, log):
    """The same model started from OpenCV's per-camera initialisation."""
    from calib.calibrate import _initial_intrinsics
    dets, cams, extr = data["dets"], data["cams"], data["extr"]
    splits = geo["splits"]
    pitch = float(data["profile"].get("plate_pitch_mm", 28.0))
    obs = MOT.make_obs(dets["sweep_c"], splits["train"], CAMERAS)
    remapped = sorted(set(int(o.view) for o in obs))
    remap = {v: i for i, v in enumerate(remapped)}
    obs_r = [Observation(o.camera, remap[int(o.view)], o.uv) for o in obs]
    obj = plate.board_points_mm(pitch)
    cams_cv = _initial_intrinsics(obs_r, cams, obj, True)
    from calib.calibrate import _initial_poses
    views_cv, extr_cv = _initial_poses(obs_r, cams_cv, obj)
    specs = M.model_ladder()
    out = {"opencv_per_camera_init": {c: {"fx": cams_cv[c].fx, "fy": cams_cv[c].fy,
                                          "cx": cams_cv[c].cx, "cy": cams_cv[c].cy,
                                          "kc": [float(x) for x in cams_cv[c].kc]}
                                      for c in CAMERAS},
           "note": ("per-camera OpenCV calibration on the same views; the two IR "
                    "cameras of one rig landing far apart is direct evidence that "
                    "the sweep does not determine f")}
    cv_spread = abs(cams_cv["left"].fx - cams_cv["right"].fx) / np.mean(
        [cams_cv["left"].fx, cams_cv["right"].fx])
    out["ir_focal_spread_fraction"] = float(cv_spread)
    for name in ("M4", "M5"):
        fit = M.fit(obs, specs[name], cams_cv, init_extr=extr_cv, init_views=views_cv,
                    pitch_mm=pitch, max_nfev=args.max_nfev, compute_covariance=False)
        out[name] = {"rms_px": fit.rms, "wall_s": fit.wall_s,
                     "nfev": fit.info.get("nfev"),
                     "fx": {c: fit.cams[c].fx for c in CAMERAS},
                     "cx": {c: fit.cams[c].cx for c in CAMERAS},
                     "baseline_mm": float(np.linalg.norm(fit.extr["right"][1])),
                     "mean_depth_mm": float(np.mean([v[1][2] for v in fit.views]))}
        log(f"  path-dependence {name}: RMS {fit.rms:.4f} fx "
            f"{ {c: round(fit.cams[c].fx, 1) for c in CAMERAS} }")
    return out


def stage_profile_f(args, data, geo, log):
    """Fixed common focal scale -> profile likelihood and the depth response."""
    dets, cams, extr = data["dets"], data["cams"], data["extr"]
    splits = geo["splits"]
    pitch = float(data["profile"].get("plate_pitch_mm", 28.0))
    obs = MOT.make_obs(dets["sweep_c"], splits["train"], CAMERAS)
    base = {"left": {"cx", "cy", "skew_px", "k1", "k2", "p1", "p2", "k3"},
            "right": {"cx", "cy", "skew_px", "k1", "k2", "p1", "p2", "k3"}}
    spec = M.ModelSpec(dict(base), {"right": set(M.EXTR_NAMES)}, True, "fixed-f")
    scales = [0.90, 0.94, 0.97, 0.99, 1.00, 1.01, 1.03, 1.06, 1.10]
    rms, zmean, wall = [], [], []
    for s in scales:
        ini = {c: CameraIntrinsics(cams[c].fx * s, cams[c].fy * s, cams[c].cx,
                                  cams[c].cy, np.asarray(cams[c].kc, float).copy(),
                                  cams[c].skew) for c in CAMERAS}
        t = time.time()
        fit = M.fit(obs, spec, ini, init_extr=extr, pitch_mm=pitch,
                    max_nfev=args.max_nfev, compute_covariance=False)
        rms.append(fit.rms)
        zmean.append(float(np.mean([v[1][2] for v in fit.views])))
        wall.append(time.time() - t)
        log(f"    f scale {s:.2f}: RMS {fit.rms:.5f}  mean depth {zmean[-1]:.2f} mm")
    k = int(np.argmin(rms))
    # residual scale for the chi-square profile: the achieved RMS at the minimum
    sigma = float(np.asarray(rms)[k])
    tab = ID.profile_table(scales, rms, zmean, len(obs) * 40 * 2, max(sigma, 1e-3))
    tab["wall_s"] = wall
    tab["note"] = ("both IR focal lengths are scaled together; everything else "
                   "(kc, skew, rig, poses) is refitted at each scale")
    return tab


# --------------------------------------------------------------------------
# stage 5: vendor corpus
# --------------------------------------------------------------------------

def stage_vendor(args, data, log) -> dict:
    if not args.vendor_corpus:
        return {"skipped": "no --vendor-corpus"}
    root = Path(os.path.expanduser(args.vendor_corpus))
    files = sorted(root.glob("imageLeft*.bmp"))
    if not files:
        return {"skipped": f"no imageLeft*.bmp under {root}"}
    idx = sorted({int(p.stem.replace("imageLeft", "")) for p in files})
    det = {"left": [], "right": [], "tex": []}
    ids = []
    t = time.time()
    for i in idx:
        paths = {c: root / {"left": f"imageLeft{i}.bmp", "right": f"imageRight{i}.bmp",
                            "tex": f"imageTex{i}.bmp"}[c] for c in CAMERAS}
        if not all(p.exists() for p in paths.values()):
            continue
        ids.append(i)
        for c in CAMERAS:
            raw = __import__("cv2").imread(str(paths[c]), __import__("cv2").IMREAD_UNCHANGED)
            if raw is None:
                det[c].append(None); continue
            det[c].append(F.detect_view(raw, c))
    log(f"  vendor corpus: {len(ids)} view sets detected in {time.time() - t:.1f}s")
    obs = []
    for k, i in enumerate(ids):
        for c in CAMERAS:
            if det[c][k] is not None:
                obs.append(Observation(c, k, dict(det[c][k].points)))
    out = {"root": str(root.resolve()), "n_views": len(ids), "view_ids": ids,
           "detection": MOT.detection_stats(det),
           "assignment_audit": MOT.assignment_stats(det),
           "obs_count": len(obs)}
    traj = MOT.trajectory(det, "left", data["cams"]["left"],
                          float(data["profile"].get("plate_pitch_mm", 28.0)))
    out["trajectory"] = traj.summary()
    out["legs"] = [[int(a), int(b)] for a, b in traj.legs()]
    out["image_coverage_left"] = MOT.image_coverage(det, "left", list(range(len(ids))))
    spec_all = M.ModelSpec({c: set(M.INTR_NAMES) for c in CAMERAS},
                           {"right": set(M.EXTR_NAMES), "tex": set(M.EXTR_NAMES)},
                           False, "vendor-linearisation")
    pitch = float(data["profile"].get("plate_pitch_mm", 28.0))
    lin = M.linearise(obs, spec_all, data["cams"], data["extr"], pitch_mm=pitch)
    free = np.asarray(lin.info["free_mask"], bool)[:M.N_RIG]
    names = ID.rig_names()
    out["factory_linearisation"] = {
        "spectrum": ID.scaled_spectrum(lin.marginal_information, names, free, top=8),
        "correlations": ID.correlations(lin.rig_covariance, names, free, 0.5, 20),
        "parameters": ID.parameter_table(lin, obs, free),
        "verdicts": ID.summarise_verdicts(ID.parameter_table(lin, obs, free)),
        "n_residuals": lin.info.get("n_residuals"),
    }
    specs = M.model_ladder()
    fits = {}
    for name in ("M2", "M5"):
        fit = M.fit(obs, specs[name], data["cams"], init_extr=data["extr"],
                    pitch_mm=pitch, max_nfev=args.max_nfev,
                    compute_covariance=(name == "M5"))
        f2 = np.asarray(fit.info["free_mask"], bool)[:M.N_RIG]
        fits[name] = {"rms_px": fit.rms, "n_free": fit.n_free,
                      "n_observations": fit.n_obs, "wall_s": fit.wall_s,
                      "nfev": fit.info.get("nfev"), "success": fit.info.get("success"),
                      "fx": {c: fit.cams[c].fx for c in CAMERAS},
                      "baseline_mm": float(np.linalg.norm(fit.extr["right"][1])),
                      "spectrum": ID.scaled_spectrum(fit.marginal_information,
                                                     names, f2, top=8),
                      "parameters": ID.parameter_table(fit, obs, f2),
                      "f_vs_depth": ID.f_vs_depth_correlation(fit, obs, f2)}
        log(f"  vendor {name}: RMS {fit.rms:.4f} fx "
            f"{ {c: round(fit.cams[c].fx, 2) for c in CAMERAS} }")
    out["fits"] = fits
    out["wall_s"] = time.time() - t
    return out


# --------------------------------------------------------------------------
# stage 6: downstream reconstruction
# --------------------------------------------------------------------------

def stage_downstream(args, data, geo, ladder, log) -> dict:
    from calib.geometry import solve_pnp
    recon = Path(os.path.expanduser(args.recon_cli))
    if not recon.exists():
        return {"skipped": f"{recon} not found"}
    dets, cams, extr = data["dets"], data["cams"], data["extr"]
    pitch = float(data["profile"].get("plate_pitch_mm", 28.0))
    outdir = Path(args.out) / "candidates"
    outdir.mkdir(parents=True, exist_ok=True)
    model_hashes = {p.name: EX.sha256_file(p) for p in
                    sorted((Path(args.out) / "cache").glob("*_detections.json"))}
    sweep_c = data["sweeps"]["sweep_c"]
    # frames used for provenance: the training views of the fitted model
    used_files = []
    for v in geo["splits"]["train"]:
        used_files.extend(sweep_c.streams(int(v)).values())
    frames_digest = EX.sha256_files(used_files)

    candidates = {}
    plan = [("factory", None, "M0")]
    for name in args.candidate_models:
        if name in ladder["fits"]:
            plan.append((name, ladder["fits"][name], name))
    for label, fit, model in plan:
        if fit is None:
            prof = dict(data["profile"])
            prof["kind"] = "factory"
            prof["validation"] = "reference-not-for-writing"
            prof["provenance"] = list(prof.get("provenance", [])) + [
                f"source_profile_sha256={EX.sha256_file(os.path.expanduser(args.profile))}",
                "role=M0 reference (unmodified factory numbers)"]
            cams_p, extr_p = cams, extr
        else:
            cams_p, extr_p = fit.cams, fit.extr
            prof = EX.build_profile(
                cams_p, extr_p, pitch, kind="replacement",
                color_status="unknown",
                provenance=[
                    f"study=tools/calib_study (phase 4 observability)",
                    f"model={model}",
                    f"fit_rms_px={fit.rms:.6f}",
                    f"n_views={fit.n_views}",
                    f"n_observations={fit.n_obs}",
                    f"pitch_mm={pitch}",
                    f"frames_sha256={frames_digest}",
                    f"detections_sha256={model_hashes}",
                    f"factory_profile_sha256={EX.sha256_file(os.path.expanduser(args.profile))}",
                    "validation=pending",
                    "warning=not validated against any metric reference",
                ],
                extra={"validation": "pending",
                       "study": {"model": model, "train_views": len(geo["splits"]["train"]),
                                 "heldout_legs": len(geo["splits"]["heldout_legs"]),
                                 "heldout_session": len(geo["splits"]["heldout_session"])}})
        path = outdir / f"{label}.json"
        digest = EX.write_profile(prof, path)
        candidates[label] = {"path": str(path), "sha256": digest,
                             "model": model,
                             "validation": "pending" if fit is not None else "reference",
                             "baseline_mm": float(np.linalg.norm(extr_p["right"][1])),
                             "f_left": float(cams_p["left"].fx),
                             "f_right": float(cams_p["right"].fx)}
    # reconstruct a few held-out groups
    groups = list(geo["splits"]["heldout_legs"])[::max(1, len(geo["splits"]["heldout_legs"]) // 4)][:4]
    out = {"candidates": candidates, "groups": [int(g) for g in groups],
           "recon_cli": str(recon), "runs": {}}
    obj = plate.board_points_mm(pitch)
    uv_index = {(float(u), float(v)): i for i, (u, v) in enumerate(plate.lattice_uv())}
    clouds, poses = {}, {}
    for label, cand in candidates.items():
        runs, xyz = {}, {}
        cams_p, extr_p = EX.profile_to_cameras(EX.load_profile(cand["path"]))
        for g in groups:
            r = EX.run_recon(recon, sweep_c.root, int(g), cand["path"],
                             Path(args.out) / "recon" / label / f"g{int(g)}",
                             scale=args.recon_scale, timeout=args.recon_timeout)
            pl = r["report"].get("depth", {})
            entry = {"returncode": r["returncode"], "wall_s": r["wall_s"],
                     "valid_fraction": pl.get("valid_fraction"),
                     "depth_p05_mm": pl.get("p05_mm"),
                     "depth_p50_mm": pl.get("p50_mm"),
                     "depth_p95_mm": pl.get("p95_mm"),
                     "points": r["report"].get("cloud", {}).get("points"),
                     "profile_id": r["report"].get("calibration", {}).get("id")}
            try:
                P, _ = EX.read_ply(r["cloud"])
                entry["plane"] = EX.plane_fit(P, seed=int(g))
                xyz[int(g)] = P
            except Exception as exc:
                entry["plane"] = {"ok": False, "reason": str(exc)}
            # pose of the plate for this group, from the *candidate's own* left K
            d = dets["sweep_c"]["left"][int(g)]
            if d is not None:
                keys = [k for k in d.points if (float(k[0]), float(k[1])) in uv_index]
                idx = [uv_index[(float(k[0]), float(k[1]))] for k in keys]
                p = solve_pnp(obj[idx], np.array([d.points[k] for k in keys]),
                              cams_p["left"].K, np.asarray(cams_p["left"].kc, float))
                if p is not None:
                    poses[(label, int(g))] = (p.R, p.t)
                    entry["plate_pose_t_mm"] = [float(x) for x in p.t]
            runs[str(int(g))] = entry
            log(f"    recon {label} g{g}: valid {entry['valid_fraction']} "
                f"plane RMS {entry.get('plane', {}).get('rms_mm')}")
        clouds[label] = xyz
        out["runs"][label] = runs
    # pose-normalised repeatability between similar-pose held-out groups
    rep = {}
    for label, xyz in clouds.items():
        pairs = []
        gs = sorted(xyz)
        for i in range(len(gs)):
            for j in range(i + 1, len(gs)):
                a, b = gs[i], gs[j]
                pa, pb = poses.get((label, a)), poses.get((label, b))
                if pa is None or pb is None:
                    continue
                dR = pa[0] @ pb[0].T
                ang = float(np.degrees(np.arccos(np.clip((np.trace(dR) - 1) / 2, -1, 1))))
                dt = float(np.linalg.norm(pa[1] - pb[1]))
                B = EX.transform_cloud_to(xyz[b], pa[0], pa[1], pb[0], pb[1])
                r = EX.nn_repeatability(xyz[a], B)
                pairs.append({"a": int(a), "b": int(b), "d_rot_deg": ang,
                              "d_t_mm": dt, **r})
        pairs.sort(key=lambda z: z.get("d_rot_deg", 1e9) + z.get("d_t_mm", 1e9))
        rep[label] = {"pairs": pairs[:6],
                      "note": "cloud b is mapped into a's camera frame through the "
                              "plate pose of each group (pose-normalised)"}
    out["repeatability"] = rep
    return out


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sweep", default=os.path.expanduser("~/einstar-private/sweep-c/frames"))
    ap.add_argument("--sweep2", default=os.path.expanduser("~/einstar-private/sweep-b/frames"))
    ap.add_argument("--profile",
                    default=os.path.expanduser("~/einstar-private/einstar-profiles/factory-e10.json"))
    ap.add_argument("--vendor-corpus", default=None)
    ap.add_argument("--out", default=os.path.expanduser("~/einstar-private/analysis/calib_study"))
    ap.add_argument("--recon-cli", default=str(ROOT / "build/recon/recon_cli"))
    ap.add_argument("--candidate-models", default="M2,M3")
    ap.add_argument("--train-views", type=int, default=72)
    ap.add_argument("--val-views", type=int, default=36)
    ap.add_argument("--max-nfev", type=int, default=800)
    ap.add_argument("--bootstrap", type=int, default=10)
    ap.add_argument("--bootstrap-views", type=int, default=36)
    ap.add_argument("--bootstrap-nfev", type=int, default=300)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--recon-scale", type=float, default=0.5)
    ap.add_argument("--recon-timeout", type=float, default=300.0)
    ap.add_argument("--only", default="all",
                    help="comma list of data,geometry,factory,ladder,bootstrap,path,"
                         "profile,design,verify,vendor,downstream,report")
    ap.add_argument("--resume", action="store_true",
                    help="reuse an existing observability_report.json")
    args = ap.parse_args(argv)
    out = Path(os.path.expanduser(args.out))
    out.mkdir(parents=True, exist_ok=True)
    only = set(x.strip() for x in args.only.split(",") if x.strip())
    allst = "all" in only

    t_start = time.time()
    def log(msg):
        print(msg, flush=True)

    report_path = out / "observability_report.json"
    report = {}
    if args.resume and report_path.exists():
        report = json.loads(report_path.read_text())
        log(f"resumed {report_path}")
    report.setdefault("study", {})
    report["study"].update({
        "title": "Phase 4 - calibration observability on real live sweeps",
        "env": env_info(),
        "args": vars(args),
        "outputs": str(out.resolve()),
    })

    data = stage_data(args, log) if (allst or "data" in only or "geometry" in only
                                     or "factory" in only or "ladder" in only
                                     or "bootstrap" in only or "path" in only
                                     or "profile" in only or "downstream" in only) else None
    if data is not None:
        report["data"] = data["info"]
    geo = None
    if data is not None and (allst or "geometry" in only or "ladder" in only
                             or "factory" in only or "bootstrap" in only
                             or "path" in only or "profile" in only
                             or "downstream" in only):
        log("stage: geometry")
        g = stage_geometry(args, data, log)
        report["geometry"] = g["report"]
        geo = g
    if data is not None and geo is not None and (allst or "factory" in only):
        log("stage: factory information")
        f = stage_factory_information(args, data, geo, log)
        report["factory_information"] = f["report"]
    ladder = None
    if data is not None and geo is not None and (allst or "ladder" in only
                                                 or "bootstrap" in only
                                                 or "downstream" in only):
        log("stage: model ladder")
        ladder = stage_ladder(args, data, geo, log)
        report["model_ladder"] = ladder["report"]
    if data is not None and geo is not None and ladder is not None and (allst or "bootstrap" in only):
        log("stage: bootstrap")
        report["bootstrap"] = stage_bootstrap(args, data, geo, ladder, log)
    if data is not None and geo is not None and (allst or "path" in only):
        log("stage: path dependence")
        report["path_dependence"] = stage_path_dependence(args, data, geo, log)
    if data is not None and geo is not None and (allst or "profile" in only):
        log("stage: fixed-focal profile")
        report["profile_likelihood"] = stage_profile_f(args, data, geo, log)
    if data is not None and geo is not None and (allst or "design" in only):
        log("stage: design sensitivity")
        report["design_sensitivity"] = stage_design(args, data, geo, log)
    if data is not None and geo is not None and (allst or "verify" in only):
        log("stage: wrapper verification")
        report["wrapper_verification"] = stage_wrapper_verification(args, data, geo, log)
    if data is not None and (allst or "vendor" in only):
        log("stage: vendor corpus")
        report["vendor_corpus"] = stage_vendor(args, data, log)
    if data is not None and geo is not None and ladder is not None and (allst or "downstream" in only):
        log("stage: downstream")
        report["downstream"] = stage_downstream(args, data, geo, ladder, log)
    report["study"]["wall_s_total"] = time.time() - t_start
    write_json(report_path, report)
    log(f"wrote {report_path}")

    if allst or "report" in only:
        from tools.calib_study import report as REP
        REP.write_all(report, out, log)
        log(f"wrote {out / 'REPORT.md'} and figures")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


# --------------------------------------------------------------------------
# stage 7: design sensitivity (which excitation constrains what)
# --------------------------------------------------------------------------

def stage_design(args, data, geo, log) -> dict:
    """Recompute the factory-solution information on view subsets.

    Every subset uses the *same* rig linearisation as §factory, only fewer views,
    so a change in sigma is caused by the excitation the subset removes (a
    ``sqrt(N)`` component from the smaller sample is reported separately).
    """
    dets, cams, extr = data["dets"], data["cams"], data["extr"]
    pitch = float(data["profile"].get("plate_pitch_mm", 28.0))
    train = list(geo["splits"]["train"])
    traj = geo["traj"]["sweep_c"]
    idx = {int(v): i for i, v in enumerate(traj.view)}
    tilt = np.array([traj.tilt_deg[idx[v]] if v in idx else np.nan for v in train])
    z = np.array([traj.t[idx[v], 2] if v in idx else np.nan for v in train])
    ucen = []
    for v in train:
        d = dets["sweep_c"]["left"][int(v)]
        ucen.append(float(np.mean([p[0] for p in d.points.values()])) if d else np.nan)
    ucen = np.array(ucen)
    good = np.isfinite(tilt) & np.isfinite(z) & np.isfinite(ucen)
    tilt, z, ucen = tilt[good], z[good], ucen[good]
    tv = [v for v, g in zip(train, good) if g]
    med = {k: float(np.median(a)) for k, a in (("tilt", tilt), ("z", z), ("u", ucen))}
    sel = {
        "all_train_views": list(range(len(tv))),
        "low_tilt": list(np.where(tilt <= med["tilt"])[0]),
        "high_tilt": list(np.where(tilt > med["tilt"])[0]),
        "near": list(np.where(z <= med["z"])[0]),
        "far": list(np.where(z > med["z"])[0]),
        "image_left": list(np.where(ucen <= med["u"])[0]),
        "image_right": list(np.where(ucen > med["u"])[0]),
        "central_50pct": list(np.where((np.abs(ucen - med["u"]) < 0.25 * (ucen.max() - ucen.min())))[0]),
    }
    spec_all = M.ModelSpec({c: set(M.INTR_NAMES) for c in CAMERAS},
                           {"right": set(M.EXTR_NAMES), "tex": set(M.EXTR_NAMES)},
                           False, "design-sensitivity")
    n_full = len(sel["all_train_views"])
    out = {"definitions": {
        "median_tilt_deg": med["tilt"], "median_depth_mm": med["z"],
        "median_u_px": med["u"],
        "note": ("subsets of the training views; 'sigma_norm' rescales the "
                 "sigma to the full view count by sqrt(N_full/N_subset) so only "
                 "the change in excitation remains")},
        "subsets": {}}
    for name, sidx in sel.items():
        if len(sidx) < 8:
            continue
        ids = [tv[int(i)] for i in sidx]
        obs = MOT.make_obs(dets["sweep_c"], ids, ("left", "right"))
        lin = M.linearise(obs, spec_all, cams, extr, pitch_mm=pitch)
        free = np.asarray(lin.info["free_mask"], bool)[:M.N_RIG]
        names = ID.rig_names()
        sig = dict(zip(names, np.sqrt(np.maximum(np.diag(lin.rig_covariance), 0.0))))
        spec = ID.scaled_spectrum(lin.marginal_information, names, free, top=3)
        scale_N = float(np.sqrt(n_full / len(ids)))
        out["subsets"][name] = {
            "n_views": len(ids),
            "n_residuals": lin.info.get("n_residuals"),
            "condition_scaled": spec.get("condition_number_scaled"),
            "sigma_fx_left": float(sig.get("left.fx", np.nan)),
            "sigma_cx_left": float(sig.get("left.cx", np.nan)),
            "sigma_k1_left": float(sig.get("left.k1", np.nan)),
            "sigma_skew_left": float(sig.get("left.skew_px", np.nan)),
            "sigma_baseline_mm": _baseline_sigma(lin, "right"),
            "sigma_rot_deg": float(np.degrees(np.sqrt(max(
                np.trace(lin.rig_covariance[30:33, 30:33]), 0.0)))),
            "sigma_norm": {"fx_left": float(sig.get("left.fx", np.nan) * scale_N),
                           "cx_left": float(sig.get("left.cx", np.nan) * scale_N),
                           "k1_left": float(sig.get("left.k1", np.nan) * scale_N),
                           "baseline_mm": _baseline_sigma(lin, "right") * scale_N},
            "weakest_direction": [c["param"] for c in spec["directions"][-1]["components"][:3]]
            if spec.get("directions") else [],
        }
        log(f"    design {name:16s} n={len(ids):3d} σfx {sig.get('left.fx'):.4f} "
            f"σcx {sig.get('left.cx'):.4f} σbl {_baseline_sigma(lin,'right'):.4f}")
    return out


def _baseline_sigma(lin, camera: str = "right") -> float:
    """First-order sigma of |t| for a rig camera (mm)."""
    i = 30 if camera == "right" else 36
    C = lin.rig_covariance[i:i + 3, i:i + 3]
    t = lin.extr[camera][1]
    n = float(np.linalg.norm(t))
    if n <= 0:
        return float("nan")
    u = t / n
    return float(np.sqrt(max(u @ C @ u, 0.0)))


# --------------------------------------------------------------------------
# stage 8: wrapper equivalence against calib.calibrate
# --------------------------------------------------------------------------

def stage_wrapper_verification(args, data, geo, log) -> dict:
    """Prove the wrapper is the same model as ``calib.calibrate``."""
    from scipy.optimize import least_squares
    from calib.calibrate import (_Problem, _Packer, _pack_initial,
                                 _initial_intrinsics, _initial_poses, calibrate)
    dets, cams, extr = data["dets"], data["cams"], data["extr"]
    pitch = float(data["profile"].get("plate_pitch_mm", 28.0))
    ids = MOT.even_views(geo["splits"]["train"], min(20, args.train_views))
    obs = MOT.make_obs(dets["sweep_c"], ids, ("left", "right"))
    used = sorted(set(int(o.view) for o in obs))
    remap = {v: i for i, v in enumerate(used)}
    obs_r = [Observation(o.camera, remap[int(o.view)], o.uv) for o in obs]
    obj = plate.board_points_mm(pitch)
    cams0 = _initial_intrinsics(obs_r, {c: CameraIntrinsics(cams[c].fx, cams[c].fy,
                                                           cams[c].cx, cams[c].cy,
                                                           np.asarray(cams[c].kc, float),
                                                           0.0) for c in CAMERAS},
                                obj, True)
    views0, extr0 = _initial_poses(obs_r, cams0, obj)
    pk = _Packer(len(used), True)
    x0 = _pack_initial(cams0, extr0, views0, pk)
    prob = _Problem(obs_r, obj, len(used), True, "huber", 1.0, True)
    t = time.time()
    res = least_squares(lambda xf: prob.residuals_free(xf, x0), x0[prob.free_idx],
                        method="trf", loss="huber", f_scale=1.0,
                        max_nfev=args.max_nfev, xtol=1e-10, ftol=1e-10, gtol=1e-10,
                        jac_sparsity=prob.jac_sparsity_free(prob.jac_sparsity()),
                        x_scale="jac")
    x = prob.pack(res.x, x0)
    r = prob.residuals(x)
    cc, ee, _ = prob.unpack(x)
    lin = {"fx", "fy", "cx", "cy"}
    kc5 = {"k1", "k2", "p1", "p2", "k3"}
    spec = M.ModelSpec({c: lin | kc5 for c in ("left", "right")},
                       {"right": set(M.EXTR_NAMES)}, True, "verify")
    # the same starting point and the same skew convention (zero) for both
    init0 = {c: CameraIntrinsics(cams0[c].fx if c != "tex" else cams[c].fx,
                                 cams0[c].fy if c != "tex" else cams[c].fy,
                                 cams0[c].cx if c != "tex" else cams[c].cx,
                                 cams0[c].cy if c != "tex" else cams[c].cy,
                                 np.asarray(cams0[c].kc if c != "tex" else cams[c].kc, float),
                                 0.0) for c in CAMERAS}
    mine = M.fit(obs, spec, init0, init_extr=extr0, init_views=views0, pitch_mm=pitch,
                 max_nfev=args.max_nfev, compute_covariance=False)
    out = {
        "n_views": len(ids),
        "calib_problem": {"rms_px": float(np.sqrt(np.mean(r ** 2))),
                          "nfev": int(res.nfev), "wall_s": time.time() - t,
                          "fx": {c: cc[c].fx for c in ("left", "right")},
                          "baseline_mm": float(np.linalg.norm(ee["right"][1]))},
        "wrapper": {"rms_px": mine.rms, "nfev": mine.info.get("nfev"),
                    "wall_s": mine.wall_s,
                    "fx": {c: mine.cams[c].fx for c in ("left", "right")},
                    "baseline_mm": float(np.linalg.norm(mine.extr["right"][1]))},
        "solver": {"x_scale": "jac", "max_nfev": args.max_nfev,
                   "note": "both sides identical; the wrapper is not a different model"},
    }
    out["delta"] = {
        "rms_px": abs(out["calib_problem"]["rms_px"] - out["wrapper"]["rms_px"]),
        "max_fx_px": max(abs(out["calib_problem"]["fx"][c] - out["wrapper"]["fx"][c])
                         for c in ("left", "right")),
        "baseline_mm": abs(out["calib_problem"]["baseline_mm"]
                           - out["wrapper"]["baseline_mm"]),
    }
    # and what calib.calibrate() itself reports with its default solver settings
    try:
        ref = calibrate(obs, board_pitch_mm=pitch, init_cameras=data["cams"],
                        free_kc=True)
        out["calib_calibrate_default"] = {
            "rms_px": ref.residual_rms, "nfev": ref.info.get("nfev"),
            "success": ref.info.get("success"), "message": ref.info.get("message"),
            "fx": {c: ref.cameras[c].fx for c in CAMERAS},
            "note": "x_scale=1.0 in calib.calibrate; compare with the rows above"}
    except Exception as exc:
        out["calib_calibrate_default"] = {"error": str(exc)}
    log(f"  wrapper verification: ΔRMS {out['delta']['rms_px']:.2e} px, "
        f"Δfx {out['delta']['max_fx_px']:.2e} px")
    return out
