"""Per-group processing of the live sweep and aggregation.

One group = one synchronised three-camera observation.  For each group the
harness runs ``recon_cli`` (the production pipeline), reads its maps and report,
detects the plate, and reduces the reconstruction to plate-frame statistics plus
a compact plate-surface point sample used by the repeatability analysis.
"""

from __future__ import annotations

import os
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import numpy as np

from . import common, plate_eval, recon_runner
from .geometry_py import Profile, RectifyModel

VOXEL_MM = 1.0


@dataclass
class SweepContext:
    cli: recon_runner.ReconCli
    profile: Profile
    profile_path: str
    model: RectifyModel
    frames: str
    workdir: str
    scale: float = 0.5
    extra_args: list = field(default_factory=list)
    prefix: str = "recon"
    keep_groups: set = field(default_factory=set)
    out_groups_dir: str = ""
    ring_mm: float = 2.5


def voxel_mean(points_xyz: np.ndarray, voxel_mm: float = VOXEL_MM) -> np.ndarray:
    """Voxel-grid mean of a point set (uniform density for repeatability)."""
    p = np.asarray(points_xyz, dtype=np.float64).reshape(-1, 3)
    if len(p) == 0:
        return p.astype(np.float32)
    key = np.round(p / voxel_mm).astype(np.int64)
    _, inv = np.unique(key, axis=0, return_inverse=True)
    inv = inv.ravel()
    sums = np.zeros((inv.max() + 1, 3), dtype=np.float64)
    counts = np.zeros(inv.max() + 1, dtype=np.int64)
    np.add.at(sums, inv, p)
    np.add.at(counts, inv, 1)
    return (sums / counts[:, None]).astype(np.float32)


def process_group(ctx: SweepContext, group: int) -> dict:
    """Run the CLI and evaluate one group.  Never raises."""
    t0 = time.time()
    rec: dict = {"group": int(group), "ok": False}
    outdir = os.path.join(ctx.workdir, f"g{group:06d}")
    plate_points = None
    try:
        run = ctx.cli.run(ctx.frames, group, ctx.profile_path, outdir,
                          scale=ctx.scale, prefix=ctx.prefix,
                          extra_args=ctx.extra_args)
        rec["cli"] = {
            "wall_seconds": run["wall_seconds"],
            "returncode": run["returncode"],
            "error": (run.get("stderr") or "").strip()[:400] if run["returncode"] else "",
        }
        if not run["ok"]:
            rec["error"] = "recon_cli failed"
            return rec, plate_points
        rep = run["report"]
        rec["frame_sha256"] = rep.get("input", {}).get("sha256")
        rec["timings"] = {
            "rectify_s": rep["timings"]["rectify"],
            "stereo_s": rep["timings"]["stereo"],
            "depth_s": rep["timings"]["depth"],
            "total_s": rep["timings"]["total"],
        }
        rec["stereo"] = {
            "scale": rep["stereo"]["scale"],
            "min_disparity": int(rep["stereo"]["min_disparity"]),
            "num_disparities": int(rep["stereo"]["num_disparities"]),
            "valid_fraction": float(rep["stereo"]["valid_fraction"]),
            "occluded_fraction": float(rep["stereo"]["occluded_fraction"]),
            "median_disparity": float(rep["stereo"]["median_disparity"]),
        }
        rec["depth"] = {
            "valid_fraction": float(rep["depth"]["valid_fraction"]),
            "p05_mm": float(rep["depth"]["p05_mm"]),
            "p50_mm": float(rep["depth"]["p50_mm"]),
            "p95_mm": float(rep["depth"]["p95_mm"]),
            "sigma_p50_mm": float(rep["depth"]["sigma_p50_mm"]),
            "points": int(rep["cloud"]["points"]),
            "color_status": rep["depth"]["color_status"],
        }
        maps = recon_runner.ReconCli.read_maps(outdir, ctx.prefix)
        if maps["depth"] is None:
            rec["error"] = "missing depth map"
            return rec, plate_points

        left = recon_runner.calibration_view(ctx.frames, group, 1)      # left IR
        right = recon_runner.calibration_view(ctx.frames, group, 0)     # right IR
        rec["left_image"] = recon_runner.image_stats(left)
        rec["right_image"] = recon_runner.image_stats(right)
        if rec["right_image"].get("present") and rec["left_image"].get("present"):
            lm = rec["left_image"]["mean_dn"]
            rm = rec["right_image"]["mean_dn"]
            rec["ir_exposure_ratio_right_over_left"] = float(rm / lm) if lm > 0 else None

        ev = plate_eval.evaluate_group(ctx.model, ctx.profile, maps["depth"],
                                       maps["confidence"], ctx.scale, left,
                                       right_gray=right, detect_mode="fast",
                                       ring_mm=ctx.ring_mm)
        if ev.get("detected"):
            R_pose = np.asarray(ev["pose_left"]["R"], dtype=np.float64)
            n_plate = R_pose[:, 2]
            rec["pose"] = {
                "R": R_pose.ravel().tolist(),
                "T": np.asarray(ev["pose_left"]["T"], dtype=np.float64).ravel().tolist(),
                "range_mm": float(np.linalg.norm(ev["pose_left"]["T"])),
                # angle between the plate normal and the left optical axis
                "plate_angle_deg": float(np.degrees(np.arccos(
                    np.clip(abs(float(n_plate[2])), -1.0, 1.0)))),
            }
        rec["plate"] = _strip_arrays(ev)
        rec["plate_detected"] = bool(ev.get("detected"))
        if ev.get("detected") and ev.get("surface", {}).get("ok"):
            R = np.asarray(ev["pose_left"]["R"], dtype=np.float64)
            T = np.asarray(ev["pose_left"]["T"], dtype=np.float64)
            v, u, X = ctx.model.points_from_depth(maps["depth"], ctx.scale)
            Xb = (X - T) @ R
            hx, hy = plate_eval.plate_half_extents_mm()
            inside = (np.abs(Xb[:, 0]) <= hx) & (np.abs(Xb[:, 1]) <= hy) & \
                     (np.abs(Xb[:, 2]) < 6.0)
            if inside.sum() >= 50:
                plate_points = voxel_mean(Xb[inside])
                rec["plate_points_count"] = int(len(plate_points))
        rec["ok"] = True
        return rec, plate_points
    except Exception as e:  # keep the sweep going: a failed group is data
        rec["error"] = f"{type(e).__name__}: {e}"
        return rec, plate_points
    finally:
        rec["harness_seconds"] = time.time() - t0
        keep = group in ctx.keep_groups
        if keep and rec.get("ok") and ctx.out_groups_dir:
            dst = os.path.join(ctx.out_groups_dir, f"g{group:06d}")
            try:
                os.makedirs(dst, exist_ok=True)
                for name in os.listdir(outdir):
                    shutil.copy2(os.path.join(outdir, name), os.path.join(dst, name))
            except Exception as e:
                rec["keep_error"] = str(e)
        if os.path.isdir(outdir):
            shutil.rmtree(outdir, ignore_errors=True)
        _ = rec.setdefault("error", "")


def _strip_arrays(obj):
    """Recursively drop numpy arrays that are too large for the JSON report."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(v, np.ndarray):
                out[k] = {"array_shape": list(v.shape)}
            elif isinstance(v, dict):
                out[k] = _strip_arrays(v)
            else:
                out[k] = v
        return out
    if isinstance(obj, np.ndarray):
        return {"array_shape": list(obj.shape)}
    return obj


def run_sweep(ctx: SweepContext, groups, jobs: int = 4, log=print) -> tuple:
    """Run the sweep over ``groups``; returns (records, plate_points_by_group)."""
    groups = list(groups)
    results = [None] * len(groups)
    plate_points: dict = {}
    t0 = time.time()
    done = 0
    if jobs <= 1:
        for i, g in enumerate(groups):
            r = process_group(ctx, g)
            rec, pp = r if isinstance(r, tuple) else (r, None)
            results[i] = rec
            if pp is not None:
                plate_points[int(g)] = pp
            done += 1
            if log and (done % 25 == 0 or done == len(groups)):
                log(f"  sweep {done}/{len(groups)} groups, {time.time()-t0:.1f}s")
    else:
        with ThreadPoolExecutor(max_workers=jobs) as ex:
            futures = {ex.submit(process_group, ctx, g): (i, g)
                       for i, g in enumerate(groups)}
            for fut in futures:
                i, g = futures[fut]
                try:
                    r = fut.result()
                except Exception as e:  # defensive: process_group catches
                    r = ({"group": int(g), "ok": False, "error": str(e)}, None)
                rec, pp = r if isinstance(r, tuple) else (r, None)
                results[i] = rec
                if pp is not None:
                    plate_points[int(g)] = pp
                done += 1
                if log and (done % 50 == 0 or done == len(groups)):
                    log(f"  sweep {done}/{len(groups)} groups, {time.time()-t0:.1f}s")
    return results, plate_points, time.time() - t0


# ---------------------------------------------------------------------------
# aggregation
# ---------------------------------------------------------------------------

def _get_angle(r):
    p = r.get("pose") or {}
    return p.get("plate_angle_deg", np.nan)


def aggregate(records) -> dict:
    ok = [r for r in records if r.get("ok")]
    det = [r for r in ok if r.get("plate_detected")]
    surf = [r["plate"]["surface"] for r in det
            if r["plate"].get("surface", {}).get("ok")]
    img = [r["plate"]["image"] for r in det
           if r["plate"].get("image", {}).get("ok")]
    out = {
        "n_groups_requested": len(records),
        "n_groups_ok": len(ok),
        "n_plate_detected": len(det),
        "n_surface_ok": len(surf),
        "n_image_ok": len(img),
    }
    if not ok:
        return out

    def stats(vals, label, unit):
        v = common.finite(vals)
        if v.size == 0:
            return {label: None}
        p = common.pct(v, (5, 50, 95))
        return {
            f"{label}_p05{unit}": p[0], f"{label}_p50{unit}": p[1],
            f"{label}_p95{unit}": p[2], f"{label}_n": int(v.size),
        }

    out.update(stats([r["depth"]["valid_fraction"] * 100 for r in ok],
                     "valid_fraction", "_pct"))
    out.update(stats([r["depth"]["points"] for r in ok], "points", ""))
    out.update(stats([s.get("n_points_in_outline", np.nan) for s in surf],
                     "points_on_plate", ""))
    out.update(stats([r["timings"]["total_s"] for r in ok], "cli_total", "_s"))
    out.update(stats([r["cli"]["wall_seconds"] for r in ok], "cli_wall", "_s"))
    out.update(stats([r["depth"]["p50_mm"] for r in ok], "depth_p50", "_mm"))
    out.update(stats([r["left_image"].get("saturated_fraction", np.nan) * 100 for r in ok],
                     "left_saturated", "_pct"))
    out.update(stats([r["left_image"].get("mean_dn", np.nan) for r in ok],
                     "left_mean", "_dn"))
    out.update(stats([_get_angle(r) for r in ok], "plate_angle", "_deg"))
    if surf:
        out.update(stats([s["plane_rms_mm"] for s in surf], "plane_rms", "_mm"))
        out.update(stats([s["plane_p95_abs_mm"] for s in surf], "plane_p95", "_mm"))
        out.update(stats([s["plane_tilt_deg"] for s in surf], "plane_tilt", "_deg"))
        out.update(stats([s["bias_median_mm"] for s in surf], "bias", "_mm"))
        out.update(stats([s["bias_iqr_mm"] for s in surf], "bias_iqr", "_mm"))
        out.update(stats([s["depth_bias_ratio_pct"] for s in surf], "range_bias", "_pct"))
        out.update(stats([s["implied_pitch_mm"] for s in surf], "implied_pitch", "_mm"))
        out.update(stats([s["range_mm"] for s in surf], "range", "_mm"))
        out.update(stats([s["coverage_1mm_frac"] * 100 for s in surf], "coverage", "_pct"))
        out.update(stats([s["density_pts_per_cm2"] for s in surf], "density", "_per_cm2"))
        # Repeatability of the plate pose estimate itself across the sweep is not
        # meaningful (the plate moves); what matters is the spread at fixed pose,
        # which repeatability.py measures.
    if img:
        out.update(stats([s["plate_valid_fraction"] * 100 for s in img],
                         "plate_valid", "_pct"))
        out.update(stats([s.get("edge_band_invalid_fraction", np.nan) * 100 for s in img],
                         "edge_band_invalid", "_pct"))
        out.update(stats([s.get("interior_invalid_fraction", np.nan) * 100 for s in img],
                         "interior_invalid", "_pct"))
        out.update(stats([s.get("marker_ring_invalid_fraction", np.nan) * 100 for s in img],
                         "marker_ring_invalid", "_pct"))
        out.update(stats([s.get("marker_ref_invalid_fraction", np.nan) * 100 for s in img],
                         "marker_ref_invalid", "_pct"))
    sc = [r["plate"]["pose_selfcheck"] for r in det
          if r["plate"].get("pose_selfcheck")]
    if sc:
        out.update(stats([s["translation_diff_norm_mm"] for s in sc],
                         "pose_selfcheck_dT", "_mm"))
        out.update(stats([s["rotation_diff_deg"] for s in sc],
                         "pose_selfcheck_dR", "_deg"))
        out.update(stats([s["range_diff_mm"] for s in sc],
                         "pose_selfcheck_drange", "_mm"))
    return out
