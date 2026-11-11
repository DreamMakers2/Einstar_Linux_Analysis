#!/usr/bin/env python3
"""Analyse a captured Einstar sweep: plate detection, pose, stereo check,
quality, trajectory, repeatability and informative-view selection.

    .venv/bin/python tools/sweep/analyze_sweep.py \\
        --frames ~/einstar-private/sweep-b/frames \\
        --profile ~/einstar-private/einstar-profiles/factory-e10.json \\
        --out ~/einstar-private/analysis/sweep-b [--json] [--cams 0,1]

The capture directory is read-only; the tool never writes into it.

Per complete three-camera group the tool

1. detects the 40-marker plate in both IR cameras (stream 0 is rotated 180 deg
   into the calibration orientation first) and **verifies the UV label
   assignment** with the four large markers (``orient.py``); assignments whose
   labels cannot be proven are rejected and counted;
2. solves the plate pose in each camera independently with
   ``calib.geometry.solve_pnp`` (factory intrinsics + distortion), reports the
   reprojection RMS/max and the Gauss-Newton pose covariance, and derives the
   right-to-left relative pose from the two plate poses - an independent check
   of the factory stereo extrinsics;
3. measures image quality (level, saturation, clipping, contrast, normalised
   blur, marker-edge sharpness);
4. builds the pose trajectory, the frame-to-frame motion, the turnaround points
   of the dominant translation axis and the resulting sweeps;
5. measures revisit repeatability (cross-leg nearest neighbours) and the pose
   noise floor (local quadratic detrend);
6. selects an informative subset by deterministic greedy farthest-point
   coverage with a duplicate threshold, and reports how many distinguishable
   poses the sweep actually contains.

Everything is deterministic: no RNG is used anywhere.

Outputs (in ``--out``): ``sweep_report.json``, ``selection.json``, six PNG
figures and ``REPORT.md``.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import multiprocessing as mp
import os
from pathlib import Path
import sys
import time
import traceback

import numpy as np

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
_REPO = HERE.parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from calib import detect, plate  # noqa: E402

import sweepio  # noqa: E402
import orient  # noqa: E402
import pose as pose_mod  # noqa: E402
import qual  # noqa: E402
import trajectory as traj_mod  # noqa: E402
import repeat  # noqa: E402
import stereo as stereo_mod  # noqa: E402
import observability  # noqa: E402
import viewselect as select_mod  # noqa: E402
import figures  # noqa: E402
import report as report_mod  # noqa: E402

SCHEMA = "einstar-sweep-analysis/1"
SELECTION_SCHEMA = "einstar-sweep-selection/1"

#: separation thresholds (in tolerance units) reported as "distinct poses"
TAU_GRID = (0.25, 0.5, 1.0, 2.0, 3.0, 4.0)
#: primary selection tolerance
TAU_MIN = 1.0
#: duplicate threshold (a frame this close to another is a near-duplicate)
TAU_DUP = 0.5
#: revisit threshold used for the repeatability pairs
REVISIT_MM = 5.0
REVISIT_DEG = 1.0


# ---------------------------------------------------------------------------
# worker (per group)
# ---------------------------------------------------------------------------

_W: dict = {}


def _init_worker(cfg: dict):
    _W.clear()
    _W.update(cfg)


def _detect_one(stream: int, path: str, K, kc, pitch_mm: float) -> dict:
    """Detect + verify + pose + quality for one IR camera of one group."""
    raw = sweepio.read_pgm(path)
    img = sweepio.calibration_image(stream, raw)
    del raw
    obs = detect.detect_grid_auto(img)
    rec = {"stream": int(stream), "role": sweepio.STREAM_ROLE[int(stream)],
           "rotated_180": bool(sweepio.STREAM_ROT180[int(stream)]),
           "detected": obs is not None}
    if obs is None:
        return {"det": rec, "quality": qual.image_quality(img)}
    rec.update({"n_lattice": int(obs.n_lattice),
                "homography_rms": _r(obs.rms), "homography_max": _r(obs.max_residual),
                "detector": getattr(obs, "detector", None)})
    ocheck = orient.check_orientation(img, obs)
    rec["orientation"] = ocheck.as_dict()
    pix = np.asarray(obs.pixels, float)
    pitch_px = ocheck.pitch_px
    q = qual.image_quality(img, pix, pitch_px if pitch_px else 81.0)
    psol = None
    if ocheck.ok:
        psol = pose_mod.solve_pose(obs, K, kc)
    rec["pixels"] = pix.round(3).tolist()
    rec["uv"] = [[float(u), float(v)] for (u, v) in
                 sorted(obs.points.keys(), key=lambda k: (k[1], k[0]))]
    # keep image points in the same order as uv
    order = sorted(obs.points.keys(), key=lambda k: (k[1], k[0]))
    rec["pixels"] = np.asarray([obs.points[k] for k in order], float).round(3).tolist()
    return {"det": rec,
            "pose": None if psol is None else psol.as_dict(),
            "quality": q,
            "_R": None if psol is None else psol.R.tolist(),
            "_t": None if psol is None else psol.t.tolist()}


def _r(v, nd=4):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return round(f, nd) if np.isfinite(f) else None


def _worker(job: dict) -> dict:
    """Analyse one group in a worker process."""
    cfg = _W
    out = {"group": job["group"], "device_group": job.get("device_group"),
           "frame_ids": job.get("frame_ids") or {}, "host_time_ns": job.get("host_time_ns"),
           "detect": {}, "pose": {}, "quality": {}, "R": {}, "t": {}}
    try:
        res = {}
        for stream in cfg["all_streams"]:
            K = cfg["K"][stream]
            kc = cfg["kc"][stream]
            path = job["paths"][str(stream)]
            if stream in cfg["ir_streams"]:
                one = _detect_one(stream, path, K, kc, cfg["pitch_mm"])
                out["detect"][str(stream)] = one["det"]
                out["pose"][str(stream)] = one["pose"]
                out["quality"][str(stream)] = one["quality"]
                out["R"][str(stream)] = one["_R"]
                out["t"][str(stream)] = one["_t"]
            else:
                raw = sweepio.read_pgm(path)
                img = sweepio.calibration_image(stream, raw)
                out["quality"][str(stream)] = qual.image_quality(img, bayer_raw=True)
        out["stereo"] = (_stereo_block(out, cfg)
                         if len(cfg["ir_streams"]) == 2 else None)
    except Exception as exc:  # keep the run alive, report the failure
        out["error"] = f"{type(exc).__name__}: {exc}"
        out["traceback"] = traceback.format_exc()[-1500:]
    return out


def _stereo_block(out: dict, cfg: dict) -> dict | None:
    s0, s1 = cfg["ir_streams"]
    R0, t0 = out["R"].get(str(s0)), out["t"].get(str(s0))
    R1, t1 = out["R"].get(str(s1)), out["t"].get(str(s1))
    d0, d1 = out["detect"].get(str(s0)) or {}, out["detect"].get(str(s1)) or {}
    block: dict = {}
    # image-space agreement of the two cameras after the 180 deg mapping
    if d0.get("detected") and d1.get("detected"):
        uv0 = {(u, v): p for (u, v), p in zip(map(tuple, d0["uv"]), d0["pixels"])}
        uv1 = {(u, v): p for (u, v), p in zip(map(tuple, d1["uv"]), d1["pixels"])}
        common = sorted(set(uv0) & set(uv1))
        if common:
            a = np.asarray([uv0[k] for k in common])
            b = np.asarray([uv1[k] for k in common])
            d = np.linalg.norm(a - b, axis=1)
            block["pix_agreement"] = {"n_common": len(common),
                                      "median_px": _r(np.median(d), 2),
                                      "p95_px": _r(np.percentile(d, 95), 2),
                                      "max_px": _r(d.max(), 2)}
        else:
            block["pix_agreement"] = {"n_common": 0}
        # epipolar residual with the FACTORY essential matrix (no PnP involved)
        if common:
            prof = cfg["factory"]
            x1 = pose_mod.undistort_normalised(np.asarray([uv0[k] for k in common]),
                                               cfg["K"][s0], cfg["kc"][s0])
            x2 = pose_mod.undistort_normalised(np.asarray([uv1[k] for k in common]),
                                               cfg["K"][s1], cfg["kc"][s1])
            E = pose_mod.essential(prof["R"], prof["T"])
            # E maps left -> right: the constraint is x_right^T E x_left
            resid = pose_mod.epipolar_sampson_px(
                E, x2, x1, 0.5 * (cfg["K"][s1][0, 0] + cfg["K"][s0][0, 0]))
            block["epipolar_factory_px"] = {
                "p50": _r(np.median(resid), 4),
                "p95": _r(np.percentile(resid, 95), 4),
                "max": _r(resid.max(), 4),
                "n": int(len(resid)),
            }
    if R0 and R1:
        R_rel, t_rel = pose_mod.stereo_from_poses(
            pose_mod.PoseSolution(np.asarray(R0, float), np.asarray(t0, float), 0,
                                  np.nan, np.nan, np.nan, np.nan, (np.nan,) * 3,
                                  np.nan, np.zeros(3)),
            pose_mod.PoseSolution(np.asarray(R1, float), np.asarray(t1, float), 0,
                                  np.nan, np.nan, np.nan, np.nan, (np.nan,) * 3,
                                  np.nan, np.zeros(3)))
        cmp = pose_mod.compare_stereo(R_rel, t_rel, cfg["factory"]["R"],
                                      cfg["factory"]["T"])
        block.update({
            "R_rel": [float(v) for v in R_rel.ravel()],
            "t_rel": [float(v) for v in t_rel.ravel()],
            "rot_deg": _r(pose_mod.rot_angle_deg(R_rel)),
            "baseline_mm": _r(cmp["baseline_mm"]),
            "rot_err_deg": _r(cmp["rot_err_deg"]),
            "rot_err_rvec_deg": [_r(v) for v in cmp["rot_err_rvec_deg"]],
            "t_err_mm": _r(cmp["t_err_mm"]),
            "baseline_ratio": _r(cmp["baseline_ratio"], 6),
            "pitch_implied_mm": _r(cmp["pitch_implied_mm"]),
            "t_direction_deg": _r(cmp["t_direction_deg"]),
            "baseline_factory_mm": _r(float(np.linalg.norm(cfg["factory"]["T"]))),
        })
        # held-out cross-camera prediction with the FACTORY extrinsic: the
        # destination camera's own pose is never used
        try:
            obj = np.asarray([cfg["obj"][cfg["uv_index"][k]] for k in common], float)
            px0 = np.asarray([uv0[k] for k in common], float)
            px1 = np.asarray([uv1[k] for k in common], float)
            Rf, Tf = cfg["factory"]["R"], cfg["factory"]["T"]
            block["heldout"] = {
                "left_to_right": stereo_mod.heldout_residual(
                    np.asarray(R1, float), np.asarray(t1, float), Rf, Tf,
                    cfg["K"][s0], cfg["kc"][s0], obj, px0),
                "right_to_left": stereo_mod.heldout_residual(
                    np.asarray(R0, float), np.asarray(t0, float),
                    np.asarray(Rf, float).T,
                    -np.asarray(Rf, float).T @ np.asarray(Tf, float),
                    cfg["K"][s1], cfg["kc"][s1], obj, px1),
            }
        except Exception:
            pass
        # self-consistency: epipolar residual with the pose-derived essential
        # matrix.  Small values prove the projection/undistortion chain used by
        # this tool is consistent; the comparison with the factory value is then
        # a statement about the factory extrinsics, not about this code.
        if block.get("epipolar_factory_px") and common:
            E_m = pose_mod.essential(R_rel, t_rel)
            x1 = pose_mod.undistort_normalised(np.asarray([uv0[k] for k in common]),
                                               cfg["K"][s0], cfg["kc"][s0])
            x2 = pose_mod.undistort_normalised(np.asarray([uv1[k] for k in common]),
                                               cfg["K"][s1], cfg["kc"][s1])
            r_m = pose_mod.epipolar_sampson_px(
                E_m, x2, x1, 0.5 * (cfg["K"][s1][0, 0] + cfg["K"][s0][0, 0]))
            block["epipolar_measured_px"] = {
                "p50": _r(np.median(r_m), 4),
                "p95": _r(np.percentile(r_m, 95), 4),
                "max": _r(r_m.max(), 4),
                "n": int(len(r_m)),
            }
    return block or None


# ---------------------------------------------------------------------------
# analysis stage
# ---------------------------------------------------------------------------

def _flat_records(per_frame: list[dict], cfg: dict, K1, kc1) -> list[dict]:
    """Per-frame analysis records (reference camera pose + quality)."""
    recs = []
    ref = str(cfg.get("ref_stream", 1))
    for i, fr in enumerate(per_frame):
        p = fr["pose"].get(ref)
        if not p:
            continue
        dt = fr.get("_time_s")
        centre = pose_mod.project_board(K1, kc1, np.asarray(p["R"]).reshape(3, 3),
                                        np.asarray(p["t"]), np.zeros((1, 3)))[0]
        roll = p["roll_deg"]
        recs.append({
            "index": i,
            "group": int(fr["group"]),
            "time_s": dt,
            "t": list(p["t"]),
            "R": list(p["R"]),
            "tilt_deg": p["tilt_deg"],
            "pitch_deg": p["pitch_deg"],
            "yaw_deg": p["yaw_deg"],
            "roll_deg": roll,
            "u_c": float(centre[0]),
            "v_c": float(centre[1]),
            "rms_px_ref": p["rms_px"],
            "rms_px_other": (fr["pose"].get("0" if ref == "1" else "1") or {}).get("rms_px"),
            "sigma_rot_deg": p["sigma_rot_deg"],
            "sigma_t_mm": p["sigma_t_mm"],
        })
    med = float(np.median([r["roll_deg"] for r in recs])) if recs else 0.0
    for r in recs:
        rel = r["roll_deg"] - med
        r["roll_rel_deg"] = float((rel + 180.0) % 360.0 - 180.0)
    return recs, med


def _stereo_stats(per_frame: list[dict]) -> dict:
    bl = [fr["stereo"] for fr in per_frame if fr.get("stereo")]
    out: dict = {"n_frames": len(bl)}
    if not bl:
        return out
    for key, name in (("rot_err_deg", "rot_err_deg"), ("t_err_mm", "t_err_mm"),
                      ("baseline_mm", "baseline_mm"), ("rot_deg", "rot_deg"),
                      ("baseline_ratio", "baseline_ratio"),
                      ("baseline_factory_mm", "baseline_factory_mm"),
                      ("pitch_implied_mm", "pitch_implied_mm"),
                      ("t_direction_deg", "t_direction_deg")):
        vals = [b[key] for b in bl if b.get(key) is not None]
        out[name] = repeat.summarise(vals)
    rvecs = [b["rot_err_rvec_deg"] for b in bl if b.get("rot_err_rvec_deg")]
    if rvecs:
        A = np.asarray(rvecs, float)
        out["rot_err_rvec_deg"] = {
            "mean": [round(float(v), 4) for v in A.mean(0)],
            "std": [round(float(v), 4) for v in A.std(0, ddof=1)] if len(A) > 1 else [0, 0, 0],
            "rms": [round(float(v), 4) for v in np.sqrt(np.mean(A ** 2, 0))],
        }
    trel = np.asarray([b["t_rel"] for b in bl if b.get("t_rel")], float)
    if len(trel):
        out["t_rel_mean"] = [round(float(v), 4) for v in trel.mean(0)]
        out["t_rel_std"] = ([round(float(v), 4) for v in trel.std(0, ddof=1)]
                            if len(trel) > 1 else [0, 0, 0])
    Rrel = np.asarray([b["R_rel"] for b in bl if b.get("R_rel")], float)
    if len(Rrel):
        out["R_rel_mean"] = [round(float(v), 6) for v in Rrel.mean(0)]
        out["R_rel_std"] = ([round(float(v), 6) for v in Rrel.std(0, ddof=1)]
                            if len(Rrel) > 1 else [0.0] * 9)
    for key in ("epipolar_factory_px", "epipolar_measured_px", "pix_agreement"):
        vals = [b[key] for b in bl if b.get(key)]
        if vals:
            out[key] = {k: repeat.summarise([v.get(k) for v in vals]) for k in vals[0]
                        if k != "n"}
    for side in ("left_to_right", "right_to_left"):
        vals = [(b.get("heldout") or {}).get(side) for b in bl]
        vals = [v for v in vals if v]
        if vals:
            out[f"heldout_{side}_px"] = repeat.summarise([v["rms_px"] for v in vals])
    return out


def _quality_stats(per_frame: list[dict], cams: list[str]) -> dict:
    out = {}
    for cam in cams:
        qs = [fr["quality"].get(cam) for fr in per_frame if fr["quality"].get(cam)]
        if not qs:
            continue
        keys = [k for k in qs[0] if k not in ("bayer_raw",)]
        out[cam] = {k: repeat.summarise([q.get(k) for q in qs]) for k in keys}
        out[cam]["bayer_raw"] = bool(qs[0].get("bayer_raw"))
    return out


def _detection_stats(per_frame: list[dict], cams: list[str]) -> dict:
    out = {}
    for cam in cams:
        dets = [fr["detect"].get(cam) for fr in per_frame if fr["detect"].get(cam)]
        n = [d.get("n_lattice", 0) for d in dets]
        rms = [d.get("homography_rms") for d in dets if d.get("homography_rms") is not None]
        mx = [d.get("homography_max") for d in dets if d.get("homography_max") is not None]
        reasons: dict[str, int] = {}
        for d in dets:
            o = d.get("orientation") or {}
            reasons[o.get("reason", "none")] = reasons.get(o.get("reason", "none"), 0) + 1
        scores = {}
        alt: dict[str, dict[str, int]] = {}
        for d in dets:
            o = d.get("orientation") or {}
            s = o.get("score")
            if s is not None:
                scores[str(int(s))] = scores.get(str(int(s)), 0) + 1
            for hyp, val in (o.get("scores") or {}).items():
                h = alt.setdefault(hyp, {})
                h[str(int(val))] = h.get(str(int(val)), 0) + 1
        hough = [d for d in dets if d.get("orientation")]
        area_ratio = [d["orientation"].get("min_area_ratio") for d in hough
                      if d["orientation"].get("min_area_ratio") is not None]
        out[cam] = {
            "n_frames": len(dets),
            "n_detected": int(sum(1 for d in dets if d.get("detected"))),
            "n_lattice": repeat.summarise(n),
            "homography_rms_px": repeat.summarise(rms),
            "homography_max_px": repeat.summarise(mx),
            "orientation_reasons": reasons,
            "orientation_score_hist": scores,
            "orientation_hypothesis_hist": alt,
            "witness_area_ratio": repeat.summarise(area_ratio),
            "detect_failures": len(dets) - int(sum(1 for d in dets if d.get("detected"))),
        }
    # pose residuals
    p1 = [fr["pose"].get("1") for fr in per_frame if fr["pose"].get("1")]
    p0 = [fr["pose"].get("0") for fr in per_frame if fr["pose"].get("0")]
    if p1:
        out["pnp_rms_px_stream1"] = repeat.summarise([p["rms_px"] for p in p1])
        out["pnp_max_px_stream1"] = repeat.summarise([p["max_px"] for p in p1])
        out["sigma_rot_deg_stream1"] = repeat.summarise([p["sigma_rot_deg"] for p in p1])
        out["sigma_t_mm_stream1"] = repeat.summarise([p["sigma_t_mm"] for p in p1])
        out["n_points_stream1"] = repeat.summarise([p["n_points"] for p in p1])
    if p0:
        out["pnp_rms_px_stream0"] = repeat.summarise([p["rms_px"] for p in p0])
        out["pnp_max_px_stream0"] = repeat.summarise([p["max_px"] for p in p0])
        out["sigma_rot_deg_stream0"] = repeat.summarise([p["sigma_rot_deg"] for p in p0])
        out["sigma_t_mm_stream0"] = repeat.summarise([p["sigma_t_mm"] for p in p0])
        out["n_points_stream0"] = repeat.summarise([p["n_points"] for p in p0])
    return out


def _sweep_table(recs: list[dict], legs: list[tuple]) -> list[dict]:
    rows = []
    for k, (a, b, d) in enumerate(legs):
        sub = recs[a:b + 1]
        if not sub:
            continue
        t = np.asarray([r["t"] for r in sub], float)
        times = [r["time_s"] for r in sub if r["time_s"] is not None]
        rows.append({
            "leg": k,
            "direction": d,
            "index_start": a, "index_end": b,
            "group_start": int(sub[0]["group"]), "group_end": int(sub[-1]["group"]),
            "n_frames": len(sub),
            "duration_s": round(float(times[-1] - times[0]), 3) if len(times) > 1 else None,
            "x_mm": [round(float(t[:, 0].min()), 2), round(float(t[:, 0].max()), 2)],
            "y_mm": [round(float(t[:, 1].min()), 2), round(float(t[:, 1].max()), 2)],
            "z_mm": [round(float(t[:, 2].min()), 2), round(float(t[:, 2].max()), 2)],
            "tilt_deg": [round(float(min(r["tilt_deg"] for r in sub)), 3),
                         round(float(max(r["tilt_deg"] for r in sub)), 3)],
            "x_span_mm": round(float(np.ptp(t[:, 0])), 2),
            "z_span_mm": round(float(np.ptp(t[:, 2])), 2),
            "mean_speed_mm_s": round(float(np.mean([r["motion"]["speed_mm_s"] for r in sub
                                                    if r.get("motion")])), 2)
            if any(r.get("motion") for r in sub) else None,
        })
    return rows


def analyse(frames: list[dict], cfg: dict) -> dict:
    ref = int(cfg.get("ref_stream", 1))
    K1 = cfg["K"][ref]
    kc1 = cfg["kc"][ref]
    recs, roll_median = _flat_records(frames, cfg, K1, kc1)
    cams = [str(c) for c in cfg["ir_streams"]]
    out: dict = {"cams": cams, "ref_stream": ref,
                 "cam_roles": {c: sweepio.STREAM_ROLE[int(c)] for c in cams}}
    out["n_frames_analysed"] = len(recs)
    out["roll_median_deg"] = round(float(roll_median), 4)
    out["frames"] = recs            # flat analysis records (left IR pose)
    out["records"] = frames         # raw per-frame detection/pose/quality

    # ---- trajectory -----------------------------------------------------
    t = np.asarray([r["t"] for r in recs], float)
    k_dom, dom_ptp = traj_mod.dominant_axis(t)
    turn, smoothed = traj_mod.find_turnarounds([r["group"] for r in recs], t[:, k_dom])
    turn_d = [x.as_dict() for x in turn]
    legs = traj_mod.split_sweeps(len(recs), turn, smoothed)
    # motion
    for i, r in enumerate(recs):
        m = None
        if i > 0:
            m = traj_mod.motion_between(recs[i - 1], r)
        r["motion"] = None if m is None else m.as_dict()
    traj = {
        "dominant_axis": traj_mod.AXIS_NAME[k_dom],
        "dominant_axis_ptp_mm": round(float(dom_ptp), 3),
        "turnarounds": turn_d,
        "n_turnarounds": len(turn_d),
        "n_legs": len(legs),
        "legs": [{"leg": k, "index_start": a, "index_end": b, "direction": d,
                  "group_start": int(recs[a]["group"]),
                  "group_end": int(recs[b]["group"])}
                 for k, (a, b, d) in enumerate(legs)],
        "sweeps": _sweep_table(recs, legs),
        "range": {
            "x_mm": traj_mod.range_stats(t[:, 0]),
            "y_mm": traj_mod.range_stats(t[:, 1]),
            "z_mm": traj_mod.range_stats(t[:, 2]),
            "distance_mm": traj_mod.range_stats(np.linalg.norm(t, axis=1)),
            "tilt_deg": traj_mod.range_stats([r["tilt_deg"] for r in recs]),
            "pitch_deg": traj_mod.range_stats([r["pitch_deg"] for r in recs]),
            "yaw_deg": traj_mod.range_stats([r["yaw_deg"] for r in recs]),
            "roll_deg": traj_mod.range_stats([r["roll_deg"] for r in recs]),
            "roll_rel_deg": traj_mod.range_stats([r["roll_rel_deg"] for r in recs]),
            "u_c_px": traj_mod.range_stats([r["u_c"] for r in recs]),
            "v_c_px": traj_mod.range_stats([r["v_c"] for r in recs]),
            "speed_mm_s": traj_mod.range_stats([r["motion"]["speed_mm_s"] for r in recs
                                                if r.get("motion")]),
            "ang_speed_deg_s": traj_mod.range_stats([r["motion"]["ang_speed_deg_s"]
                                                     for r in recs if r.get("motion")]),
        },
    }
    out["trajectory"] = traj
    out["legs"] = legs
    out["turnarounds"] = turn_d

    # ---- repeatability --------------------------------------------------
    cross_all = repeat.cross_leg_nearest(recs, legs)   # nearest, no threshold
    revisit = [p for p in cross_all
               if p["dt_mm"] <= REVISIT_MM and p["drot_deg"] <= REVISIT_DEG]
    rep_table = []
    for mm, deg in ((2.0, 0.5), (5.0, 1.0), (10.0, 2.0), (20.0, 4.0)):
        sub = [p for p in cross_all if p["dt_mm"] <= mm and p["drot_deg"] <= deg]
        rep_table.append({"translation_mm": mm, "rotation_deg": deg,
                          "n_pairs": len(sub),
                          "dt_mm": repeat.summarise([p["dt_mm"] for p in sub]),
                          "drot_deg": repeat.summarise([p["drot_deg"] for p in sub])})
    adj = repeat.adjacent_pairs(recs)
    nf = repeat.quadratic_noise_floor(recs)
    quiet = [a for a in adj if a["dt_mm"] < 0.5 and a["drot_deg"] < 0.05]
    rep = {
        "revisit_threshold": {"translation_mm": REVISIT_MM, "rotation_deg": REVISIT_DEG},
        "n_revisit_pairs": len(revisit),
        "revisit_threshold_table": rep_table,
        "cross_leg_nearest_all": repeat.summarise([p["dt_mm"] for p in cross_all]),
        "cross_leg_nearest_all_rot": repeat.summarise([p["drot_deg"] for p in cross_all]),
        "revisit_pairs": [{k: (round(float(v), 5) if isinstance(v, float) else v)
                           for k, v in p.items()} for p in revisit[:400]],
        "revisit_dt_mm": [p["dt_mm"] for p in revisit],
        "revisit_drot_deg": [p["drot_deg"] for p in revisit],
        "dt_mm": repeat.summarise([p["dt_mm"] for p in revisit]),
        "drot_deg": repeat.summarise([p["drot_deg"] for p in revisit]),
        "adjacent_dt_mm": [p["dt_mm"] for p in adj],
        "adjacent_drot_deg": [p["drot_deg"] for p in adj],
        "adjacent": repeat.summarise([p["dt_mm"] for p in adj], "dt_mm_"),
        "adjacent_rot": repeat.summarise([p["drot_deg"] for p in adj], "drot_deg_"),
        "noise_floor": nf,
        "quiet_adjacent_pairs": {
            "n": len(quiet),
            "dt_mm": repeat.summarise([p["dt_mm"] for p in quiet]),
            "drot_deg": repeat.summarise([p["drot_deg"] for p in quiet]),
        },
    }
    out["repeatability"] = rep

    # ---- global stereo-extrinsic correction -----------------------------
    try:
        corr = stereo_mod.fit_correction(
            stereo_mod.frames_for_correction(frames, cfg["factory"]["R"],
                                             cfg["factory"]["T"], cfg["obj"],
                                             cfg["uv_index"]),
            cfg["factory"]["R"], cfg["factory"]["T"], cfg["K"][0], cfg["kc"][0])
    except Exception as exc:  # never lose the rest of the analysis for this
        corr = {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}
    out["stereo_correction"] = corr

    # ---- observability probes -------------------------------------------
    prof_like = cfg.get("cam_profiles")
    probes: dict = {}
    try:
        probes["focal_scale_stream1"] = observability.focal_scale_probe(
            frames, cfg["K"][1], cfg["kc"][1], cfg["obj"], cfg["uv_index"], "1")
        if "0" in [str(c) for c in cfg["ir_streams"]]:
            probes["focal_scale_stream0"] = observability.focal_scale_probe(
                frames, cfg["K"][0], cfg["kc"][0], cfg["obj"], cfg["uv_index"], "0")
        if prof_like:
            probes["skew_convention_stream1"] = observability.skew_convention_probe(
                frames, prof_like["ir_left"], cfg["kc"][1], cfg["obj"],
                cfg["uv_index"], "1")
    except Exception as exc:
        probes["error"] = f"{type(exc).__name__}: {exc}"
    out["observability"] = probes

    # ---- coverage / selection -------------------------------------------
    desc = select_mod.descriptor_matrix(recs)
    taus = {}
    for tau in TAU_GRID:
        sel = select_mod.greedy_farthest(desc, tau_min=tau)
        taus[str(tau)] = {
            "n_distinct": len(sel),
            "separation": {
                "translation_mm": round(tau * select_mod.DEFAULT_SCALES["t_mm"], 3),
                "rotation_deg": round(tau * select_mod.DEFAULT_SCALES["rot_deg"], 3),
                "image_px": round(tau * select_mod.DEFAULT_SCALES["px"], 3),
            },
        }
    dup = {str(tau): select_mod.duplicate_fraction(desc, tau) for tau in TAU_GRID}
    selected = select_mod.greedy_farthest(desc, tau_min=TAU_MIN)
    cov = select_mod.coverage_report(desc, selected)
    # nearest-neighbour distance for every frame (to ANY other frame)
    dmat = np.linalg.norm(desc[:, None, :] - desc[None, :, :], axis=2)
    np.fill_diagonal(dmat, np.inf)
    nn = dmat.min(1)
    cov.update({
        "descriptor": {"dims": select_mod.DIM_NAMES,
                       "scales": select_mod.DEFAULT_SCALES},
        "tau_min": TAU_MIN, "tau_dup": TAU_DUP,
        "distinct_poses_at_tau": taus,
        "duplicates_at_tau": dup,
        "nn_distance_all_frames": repeat.summarise(nn.tolist()),
        "selected_indices": [int(i) for i in selected],
        "selected_groups": [int(recs[i]["group"]) for i in selected],
        "statement": ("the sweep is a 1-D trajectory through pose space; repeated "
                      "passes over the same trajectory are NOT independent "
                      "geometric viewpoints, and the coverage above measures "
                      "subsampling of that curve, not 3-D viewpoint diversity"),
    })
    out["coverage"] = cov

    # selection.json payload
    sel_rows = []
    for rank, i in enumerate(selected):
        d = float(desc[i].min()) if False else None
        sel_rows.append({
            "rank": rank, "index": int(i), "group": int(recs[i]["group"]),
            "time_s": recs[i]["time_s"],
            "x_mm": round(float(recs[i]["t"][0]), 3),
            "y_mm": round(float(recs[i]["t"][1]), 3),
            "z_mm": round(float(recs[i]["t"][2]), 3),
            "tilt_deg": recs[i]["tilt_deg"],
            "pitch_deg": recs[i]["pitch_deg"], "yaw_deg": recs[i]["yaw_deg"],
            "roll_rel_deg": round(float(recs[i]["roll_rel_deg"]), 4),
            "u_c_px": round(float(recs[i]["u_c"]), 2),
            "v_c_px": round(float(recs[i]["v_c"]), 2),
            "reason": ("seed: farthest descriptor from the mean pose"
                       if rank == 0 else
                       "farthest remaining frame from the selected set"),
            "min_dist_to_selected": round(float(
                np.min([np.linalg.norm(desc[i] - desc[j]) for j in selected if j != i]))
                if len(selected) > 1 else 0.0, 4),
        })
    out["selection"] = {
        "schema": SELECTION_SCHEMA,
        "descriptor": {"dims": select_mod.DIM_NAMES,
                       "scales": select_mod.DEFAULT_SCALES,
                       "note": ("each dimension divided by its scale; a distance "
                                "of 1.0 means 'just distinguishable'")},
        "tau_min": TAU_MIN, "tau_dup": TAU_DUP,
        "selected": sel_rows,
        "coverage": {k: v for k, v in cov.items()
                     if k in ("n_selected", "n_frames", "reduction",
                              "min_pairwise", "mean_nn_selected", "cover_radius",
                              "cover_p95", "cover_median")},
        "distinct_poses_at_tau": taus,
        "duplicates_at_tau": dup,
        "statement": cov["statement"],
    }
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_cfg(profile, cams) -> dict:
    K = {}
    kc = {}
    for s in (0, 1, 2):
        cp = profile.camera(s)
        K[s] = cp.K
        kc[s] = cp.kc
    R_fac, t_fac = sweepio.factory_stereo(profile)
    uvs = [(float(u), float(v)) for u, v in plate.lattice_uv()]
    cams = tuple(cams)
    return {
        "ir_streams": cams,
        "ref_stream": 1 if 1 in cams else cams[0],
        "all_streams": tuple(sorted(set(cams) | {2})),
        "K": K, "kc": kc,
        "pitch_mm": float(profile.pitch_mm),
        "factory": {"R": R_fac, "T": t_fac},
        "cam_profiles": {k: v for k, v in profile.cameras.items()},
        "obj": plate.board_points_mm(pitch=float(profile.pitch_mm)),
        "uvs": uvs,
        "uv_index": {uv: i for i, uv in enumerate(uvs)},
    }


def _check_outdir(out: str, frames_dir: str) -> Path:
    frames_abs = Path(frames_dir).resolve()
    out_p = Path(out).resolve()
    if out_p == frames_abs or frames_abs in out_p.parents:
        raise SystemExit(f"refusing to write inside the capture directory: {out_p}")
    out_p.mkdir(parents=True, exist_ok=True)
    return out_p


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--frames", required=True, help="capture frame directory")
    ap.add_argument("--profile", required=True, help="factory profile JSON")
    ap.add_argument("--out", required=True, help="output directory (created)")
    ap.add_argument("--json", action="store_true", help="print the summary JSON to stdout")
    ap.add_argument("--cams", default="0,1", help="USB streams to analyse (default 0,1)")
    ap.add_argument("--workers", type=int, default=0,
                    help="worker processes (0 = min(16, cpu count))")
    ap.add_argument("--limit", type=int, default=0, help="analyse only the first N groups")
    ap.add_argument("--no-figures", action="store_true")
    args = ap.parse_args(argv)

    t_start = time.perf_counter()
    cams = tuple(int(c) for c in args.cams.split(",") if c.strip() != "")
    for c in cams:
        if c not in (0, 1):
            raise SystemExit("--cams must be a subset of {0,1} (IR cameras)")
    if not cams:
        raise SystemExit("--cams is empty")
    profile = sweepio.load_profile(args.profile)
    src = sweepio.load_frames(args.frames, cams)
    if not src.groups:
        raise SystemExit(f"no complete groups in {args.frames}")
    if args.limit:
        src.groups = src.groups[: args.limit]
    outdir = _check_outdir(args.out, args.frames)
    cfg = build_cfg(profile, cams)
    man = sweepio.manifest_summary(src, cams)

    jobs = [{"group": g.group, "device_group": g.device_group,
             "frame_ids": {str(k): v for k, v in g.frame_ids.items()},
             "host_time_ns": g.host_time_ns,
             "paths": {str(k): v for k, v in g.paths.items()}}
            for g in src.groups]
    t0 = time.perf_counter()
    workers = args.workers or min(16, os.cpu_count() or 1)
    results: list[dict] = []
    if workers <= 1:
        _init_worker(cfg)
        for k, job in enumerate(jobs):
            results.append(_worker(job))
            if (k + 1) % 25 == 0:
                print(f"  detected {k + 1}/{len(jobs)} groups", file=sys.stderr)
    else:
        ctx = mp.get_context("fork")
        with ctx.Pool(processes=workers, initializer=_init_worker,
                      initargs=(cfg,)) as pool:
            for k, res in enumerate(pool.imap_unordered(_worker, jobs, chunksize=4)):
                results.append(res)
                if (k + 1) % 50 == 0:
                    print(f"  detected {k + 1}/{len(jobs)} groups", file=sys.stderr)
    results.sort(key=lambda r: r["group"])
    t_detect = time.perf_counter() - t0
    errors = [r for r in results if r.get("error")]
    if errors:
        print(f"WARNING: {len(errors)} groups failed: {errors[0]['error']}", file=sys.stderr)

    # time base: host_time_ns when present, else nominal cadence
    base = next((r["host_time_ns"] for r in results if r.get("host_time_ns")), None)
    for r in results:
        if base is not None and r.get("host_time_ns") is not None:
            r["_time_s"] = (r["host_time_ns"] - base) / 1e9
        else:
            r["_time_s"] = None
    if any(r["_time_s"] is None for r in results) and base is None:
        for k, r in enumerate(results):
            r["_time_s"] = k * 0.1
    # fill missing times forward
    last = 0.0
    for r in results:
        if r["_time_s"] is None:
            r["_time_s"] = last
        last = r["_time_s"]

    t1 = time.perf_counter()
    ana = analyse(results, cfg)
    t_analyse = time.perf_counter() - t1

    # ---- summary --------------------------------------------------------
    records = ana["records"]           # raw per-frame detection/pose/quality
    frames = ana["frames"]             # flat analysis records (left IR pose)
    tr = ana["trajectory"]
    det_stats = _detection_stats(records, ana["cams"])
    qual_stats = _quality_stats(records, [str(c) for c in cfg["all_streams"]])
    stereo_stats = _stereo_stats(records)
    n_detect1 = sum(1 for fr in records if (fr["detect"].get("1") or {}).get("detected"))
    n_detect0 = sum(1 for fr in records if (fr["detect"].get("0") or {}).get("detected"))
    ref = str(ana["ref_stream"])
    rejected_by_stream = {
        c: sum(1 for fr in records if (fr["detect"].get(c) or {}).get("detected")
               and not fr["pose"].get(c)) for c in ana["cams"]}
    reasons: dict[str, int] = {}
    for fr in records:
        for c in ana["cams"]:
            o = (fr["detect"].get(c) or {}).get("orientation") or {}
            reasons[o.get("reason", "none")] = reasons.get(o.get("reason", "none"), 0) + 1
    summary = {
        "n_groups_used": len(records),
        "n_analysis_frames": len(frames),
        "n_groups_with_pose_stream1": sum(1 for fr in records if fr["pose"].get("1")),
        "n_groups_with_pose_both": sum(1 for fr in records
                                       if fr["pose"].get("1") and fr["pose"].get("0")),
        "n_groups_detected_stream1": n_detect1,
        "n_groups_detected_stream0": n_detect0,
        "reference_stream": int(ana["ref_stream"]),
        "n_pose_rejected_ref": len(records) - sum(1 for fr in records
                                                 if fr["pose"].get(ref)),
        "n_pose_rejected_by_stream": rejected_by_stream,
        "orientation_reasons": reasons,
        "trajectory": {
            "dominant_axis": tr["dominant_axis"],
            "dominant_axis_ptp_mm": tr["dominant_axis_ptp_mm"],
            "x_mm": tr["range"]["x_mm"], "y_mm": tr["range"]["y_mm"],
            "z_mm": tr["range"]["z_mm"],
            "distance_mm": tr["range"]["distance_mm"],
            "tilt_deg": tr["range"]["tilt_deg"],
            "pitch_deg": tr["range"]["pitch_deg"],
            "yaw_deg": tr["range"]["yaw_deg"],
            "roll_deg": tr["range"]["roll_deg"],
            "roll_rel_deg": tr["range"]["roll_rel_deg"],
            "u_c_px": tr["range"]["u_c_px"], "v_c_px": tr["range"]["v_c_px"],
            "n_turnarounds": tr["n_turnarounds"],
            "n_legs": tr["n_legs"],
            "turnarounds": tr["turnarounds"],
            "speed_mm_s": tr["range"]["speed_mm_s"],
            "ang_speed_deg_s": tr["range"]["ang_speed_deg_s"],
        },
        "repeatability": {
            "revisit_threshold": ana["repeatability"]["revisit_threshold"],
            "revisit_threshold_table": ana["repeatability"]["revisit_threshold_table"],
            "cross_leg_nearest_all": ana["repeatability"]["cross_leg_nearest_all"],
            "cross_leg_nearest_all_rot": ana["repeatability"]["cross_leg_nearest_all_rot"],
            "n_revisit_pairs": ana["repeatability"]["n_revisit_pairs"],
            "dt_mm": ana["repeatability"]["dt_mm"],
            "drot_deg": ana["repeatability"]["drot_deg"],
            "adjacent_dt_mm": ana["repeatability"]["adjacent"],
            "adjacent_drot_deg": ana["repeatability"]["adjacent_rot"],
            "noise_floor": ana["repeatability"]["noise_floor"],
            "quiet_adjacent_pairs": ana["repeatability"]["quiet_adjacent_pairs"]["n"],
        },
        "coverage": {
            "n_frames": ana["coverage"]["n_frames"],
            "n_selected": ana["coverage"]["n_selected"],
            "selected_indices": ana["coverage"]["selected_indices"],
            "distinct_poses_at_tau": ana["coverage"]["distinct_poses_at_tau"],
            "duplicates_at_tau": ana["coverage"]["duplicates_at_tau"],
            "tau_min": ana["coverage"]["tau_min"],
            "tau_dup": ana["coverage"]["tau_dup"],
            "cover_radius": ana["coverage"]["cover_radius"],
            "cover_p95": ana["coverage"]["cover_p95"],
            "cover_median": ana["coverage"]["cover_median"],
            "reduction": ana["coverage"]["reduction"],
            "min_pairwise": ana["coverage"]["min_pairwise"],
            "mean_nn_selected": ana["coverage"]["mean_nn_selected"],
            "selected_groups": ana["coverage"]["selected_groups"],
            "nn_distance_all_frames": ana["coverage"]["nn_distance_all_frames"],
        },
        "detection": det_stats,
        "stereo": stereo_stats,
        "stereo_correction": ana.get("stereo_correction"),
        "observability": ana.get("observability"),
        "quality": qual_stats,
        "manifest": man,
        "runtime": {"detect_s": round(t_detect, 2), "analyse_s": round(t_analyse, 2),
                    "total_s": round(time.perf_counter() - t_start, 2),
                    "workers": workers},
    }
    doc = {
        "schema": SCHEMA,
        "source": {"frames_dir_name": Path(args.frames).name,
                   "profile_name": Path(args.profile).name,
                   "pitch_mm": profile.pitch_mm,
                   "profile_schema": profile.schema,
                   "cameras_analysed": list(ana["cams"]),
        "reference_stream": ana["ref_stream"],
                   "camera_roles": ana["cam_roles"],
                   "roll_median_deg": ana["roll_median_deg"]},
        "summary": summary,
        "trajectory": ana["trajectory"],
        "repeatability": {k: v for k, v in ana["repeatability"].items()
                          if k not in ("revisit_dt_mm", "revisit_drot_deg",
                                       "adjacent_dt_mm", "adjacent_drot_deg")},
        "coverage": {k: v for k, v in ana["coverage"].items()},
        "stereo_correction": ana.get("stereo_correction"),
        "observability": ana.get("observability"),
        "errors": [{"group": r["group"], "error": r["error"]} for r in errors],
        "frames": [_strip_frame(fr) for fr in records],
    }

    with open(outdir / "sweep_report.json", "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1)
    with open(outdir / "selection.json", "w", encoding="utf-8") as fh:
        json.dump(ana["selection"], fh, indent=1)

    if not args.no_figures:
        ctx = {"frames": frames, "records": records,
               "legs": ana["legs"], "turnarounds": ana["turnarounds"],
               "repeatability": ana["repeatability"], "cams": ana["cams"],
               "cam_roles": ana["cam_roles"], "image_size": src.frame_size,
               "coverage": ana["coverage"]}
        figures.trajectory(ctx, str(outdir))
        figures.pose_vs_time(ctx, str(outdir))
        figures.quality_vs_time(ctx, str(outdir))
        figures.repeatability(ctx, str(outdir))
        figures.image_coverage(ctx, str(outdir))
        figures.residuals(ctx, str(outdir))

    report_mod.write_report(outdir, doc, ana, records, profile, src)

    print(f"analysed {len(records)} groups in {time.perf_counter() - t_start:.1f}s "
          f"(detect {t_detect:.1f}s, analysis {t_analyse:.1f}s, {workers} workers)",
          file=sys.stderr)
    print(f"outputs in {outdir}", file=sys.stderr)
    if args.json:
        print(json.dumps(summary, indent=1, sort_keys=False))
    return 0


def _strip_frame(fr: dict) -> dict:
    """JSON-ready copy of one per-frame record (drops internal keys)."""
    def clean(o):
        if isinstance(o, dict):
            return {k: clean(v) for k, v in o.items() if not k.startswith("_")}
        if isinstance(o, (list, tuple)):
            return [clean(v) for v in o]
        if isinstance(o, (np.floating, np.integer)):
            return o.item()
        return o

    out = clean(fr)
    out["time_s"] = round(float(fr.get("_time_s") or 0.0), 6)
    return out


if __name__ == "__main__":
    sys.exit(main())
