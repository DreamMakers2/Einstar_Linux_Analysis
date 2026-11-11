"""Controlled one-variable-at-a-time experiments on the real IR data.

Rules followed here
-------------------
* **One thing changes per experiment.**  Everything else (frames, profile,
  scale, depth window, block size) is held at the baseline value, and the
  baseline is re-run on the *same* subset in the same session.
* **More pixels is not accuracy.**  Every experiment is scored on the
  pose-derived plate plane, not on coverage.  For every experiment the *newly
  accepted* pixels are separated from the pixels that were already accepted:
  ``new_frac_on_plane`` is the fraction of the new pixels landing within
  +-1 mm of the surface the **baseline** reconstructed (reference = the
  baseline's median plane residual for that group, so the global scale bias
  does not contaminate the comparison).  A coverage gain with
  ``new_frac_on_plane`` well below the baseline's on-plane fraction is not an
  accuracy gain, and is labelled that way in the report.
* **Where the knob lives.**  ``--scale``, ``--min-depth/--max-depth``,
  ``--block`` and ``--equalize`` are real ``recon_cli`` flags; exposure,
  clipping and photometric preprocessing are applied to the PGM bytes and then
  run through the *same* binary; the left/right tolerance is the only knob with
  no CLI flag and is measured with the validated Python replica
  (:mod:`live_recon.matcher_py`).  Every config records which path it used.
"""

from __future__ import annotations

import os
import shutil
import time
from dataclasses import dataclass, field

import numpy as np

from . import common, matcher_py, plate_eval, recon_runner
from .matcher_py import PyStereoOptions
from .repeatability import compare_pair
from .sweep import voxel_mean


# ---------------------------------------------------------------------------
# frame transforms (applied to the PGM bytes, then run through recon_cli)
# ---------------------------------------------------------------------------

def make_exposure_transform(factor_right: float):
    def fn(cam, img, group):
        if cam == 0:  # right IR
            out = np.clip(img.astype(np.float32) * factor_right, 0, 255)
            return np.round(out).astype(np.uint8)
        return img
    return fn


def make_clip_transform(level: int):
    def fn(cam, img, group):
        if cam in (0, 1):
            return np.minimum(img, level).astype(np.uint8)
        return img
    return fn


def make_preprocess_transform(mode: str):
    def fn(cam, img, group):
        if cam not in (0, 1):
            return img
        return matcher_py._preprocess(img, mode)
    return fn


# ---------------------------------------------------------------------------
# experiment specification
# ---------------------------------------------------------------------------

@dataclass
class ExpSpec:
    name: str
    kind: str                    # 'cli' | 'frames' | 'posthoc' | 'replica'
    varies: str
    description: str
    cli_args: list = field(default_factory=list)
    transform: object = None
    posthoc: dict = field(default_factory=dict)
    replica: dict = field(default_factory=dict)
    groups: list = field(default_factory=list)


def build_experiments(subset, clip_levels) -> list:
    specs = [
        ExpSpec("baseline", "cli", "none",
                "recon_cli defaults: scale 0.5, 150-800 mm, block 5, no CLAHE"),
    ]
    # (a) exposure / gain mismatch between the two IR cameras
    for f in (0.5, 0.7, 1.4, 2.0):
        specs.append(ExpSpec(
            f"exposure_right_x{f:g}", "frames", "right IR exposure",
            f"right IR (cam0) multiplied by {f:g} before the CLI; left untouched",
            transform=make_exposure_transform(f)))
    # (b) saturation clipping
    for level in clip_levels:
        specs.append(ExpSpec(
            f"clip_{level}", "frames", "sensor saturation",
            f"both IR streams clipped at {level} DN (emulates a lower full well)",
            transform=make_clip_transform(level)))
    # (c) disparity search window / min-disparity (through the metric depth range)
    for lo, hi in ((250, 600), (300, 500), (150, 500), (150, 1200)):
        specs.append(ExpSpec(
            f"window_{lo}_{hi}", "cli", "disparity search window",
            f"--min-depth {lo} --max-depth {hi} (changes min_disparity and num)",
            cli_args=["--min-depth", str(lo), "--max-depth", str(hi)]))
    # (d) confidence threshold: post-hoc on the baseline maps
    for t in (0.2, 0.4, 0.6, 0.8, 0.9, 0.95):
        specs.append(ExpSpec(
            f"confidence_ge_{t:g}", "posthoc", "confidence threshold",
            f"keep reconstructed points with confidence >= {t:g}",
            posthoc={"min_confidence": t}))
    # (d) left/right consistency tolerance: replica only (no CLI flag)
    for lr in (0.5, 1.5, 2.0, 3.0):
        specs.append(ExpSpec(
            f"lr_tolerance_{lr:g}", "replica", "left/right consistency tolerance",
            f"lr_tolerance_px = {lr:g} (default 1.0), Python replica of stereo.cpp",
            replica={"lr_tolerance_px": lr}))
    # (e) matching resolution
    for s in (0.25, 1.0):
        specs.append(ExpSpec(f"scale_{s:g}", "cli", "matching resolution",
                             f"--scale {s:g} (default 0.5)",
                             cli_args=["--scale", str(s)]))
    # (f) preprocessing / matcher parameters
    specs.append(ExpSpec("equalize_clahe", "cli", "CLAHE preprocessing",
                         "--equalize (CLAHE 3.0, 16x16 on both rectified IR images)",
                         cli_args=["--equalize"]))
    specs.append(ExpSpec("block_7", "cli", "SGBM block size",
                         "--block 7", cli_args=["--block", "7"]))
    for mode, label in (("log", "log1p response"), ("gamma05", "gamma 0.5"),
                        ("inverse", "inverse response (gamma 1/0.45)")):
        specs.append(ExpSpec(
            f"preproc_{mode}", "frames", "photometric preprocessing",
            f"{label} on both IR streams before the CLI",
            transform=make_preprocess_transform(mode)))
    for s in specs:
        s.groups = list(subset)
    return specs


# ---------------------------------------------------------------------------
# evaluation helpers
# ---------------------------------------------------------------------------

def resid_map(z_map, model, scale, pose) -> tuple:
    """Dense signed distance to the PnP plate plane (0 where invalid)."""
    R = np.asarray(pose["R"], dtype=np.float64).reshape(3, 3)
    T = np.asarray(pose["T"], dtype=np.float64).reshape(3)
    v, u, X = model.points_from_depth(z_map, scale)
    out = np.zeros(z_map.shape, dtype=np.float64)
    if len(v):
        out[v, u] = (X - T) @ R[:, 2]
    return out, (v, u, X)


def evaluate_maps(z_map, conf_map, model, scale, pose, pitch_mm=None) -> dict:
    v, u, X = model.points_from_depth(z_map, scale)
    stats = plate_eval.board_stats(X, pose, pitch_mm=pitch_mm)
    stats.pop("_marker_distance_mm", None)
    img = plate_eval.plate_image_stats(model, pose, z_map, scale, pitch_mm=pitch_mm)
    return {"surface": stats, "image": img}


def plate_polygon_mask(model, pose, shape, scale, pitch_mm=None) -> np.ndarray:
    """Boolean mask of the scaled depth-map pixels covered by the plate outline."""
    import cv2
    R = np.asarray(pose["R"], dtype=np.float64).reshape(3, 3)
    T = np.asarray(pose["T"], dtype=np.float64).reshape(3)
    hx, hy = plate_eval.plate_half_extents_mm(pitch_mm)
    outline = model.board_outline(hx, hy)
    uv, zc = model.project_board(R, T, outline, scale)
    mask = np.zeros(shape, dtype=np.uint8)
    if np.all(zc > 0):
        cv2.fillPoly(mask, [np.round(uv).astype(np.int32).reshape(-1, 1, 2)], 1)
    return mask > 0


def _new_pixel_analysis(z_new, z_base, model, scale, pose, ref_mm,
                        on_plane_mm: float = 1.0, base_scale: float | None = None,
                        pitch_mm: float | None = None) -> dict:
    """Separate the pixels an experiment adds/removes and validate them.

    Only pixels **inside the plate outline** are scored against the plane: a
    newly accepted background pixel has no geometric reference, so counting it
    as "off-plane" would be dishonest.  The frame-wide counts are reported
    separately.
    """
    import cv2
    if base_scale is None:
        base_scale = scale
    if z_new.shape != z_base.shape:
        # scale experiments live on a different pixel grid: resample the
        # experiment's validity onto the baseline grid (nearest: 0 stays 0) so
        # the pixel sets are comparable, and use the baseline geometry.
        z_new = cv2.resize(z_new, (z_base.shape[1], z_base.shape[0]),
                           interpolation=cv2.INTER_NEAREST)
        scale = base_scale
    valid_new = z_new > 0
    valid_base = z_base > 0
    plate = plate_polygon_mask(model, pose, valid_base.shape, base_scale, pitch_mm)
    new_all = valid_new & ~valid_base
    lost_all = valid_base & ~valid_new
    new_mask = new_all & plate
    lost_mask = lost_all & plate
    out = {"new_pixels": int(new_mask.sum()), "lost_pixels": int(lost_mask.sum()),
           "new_pixels_frame": int(new_all.sum()), "lost_pixels_frame": int(lost_all.sum()),
           "plate_polygon_px": int(plate.sum())}
    n_valid_new = int(valid_new.sum())
    out["new_frac_of_valid"] = (float(new_mask.sum()) / n_valid_new) if n_valid_new else None
    s_new, _ = resid_map(z_new, model, scale, pose)
    if new_mask.any():
        r = s_new[new_mask] - ref_mm
        out["new_signed_resid_p50_mm"] = float(np.median(r))
        out["new_abs_resid_p50_mm"] = float(np.median(np.abs(r)))
        out["new_abs_resid_p95_mm"] = float(np.percentile(np.abs(r), 95))
        out["new_frac_on_plane"] = float(np.mean(np.abs(r) < on_plane_mm))
    if lost_mask.any():
        s_base, _ = resid_map(z_base, model, base_scale, pose)
        r = s_base[lost_mask] - ref_mm
        out["lost_signed_resid_p50_mm"] = float(np.median(r))
        out["lost_abs_resid_p50_mm"] = float(np.median(np.abs(r)))
        out["lost_frac_on_plane"] = float(np.mean(np.abs(r) < on_plane_mm))
    return out


# ---------------------------------------------------------------------------
# runner
# ---------------------------------------------------------------------------

class ExperimentRunner:
    """Runs every experiment on a fixed subset and reduces to comparable rows."""

    def __init__(self, ctx, records_by_group: dict, workdir: str, log=print):
        self.ctx = ctx
        self.records = records_by_group
        self.workdir = workdir
        self.log = log
        os.makedirs(workdir, exist_ok=True)

    # -- one config x one group -------------------------------------------
    def _execute(self, spec, group, frames_dir, baseline_maps):
        """Returns (z_map, confidence, scale, wall_seconds, error)."""
        if spec.kind == "posthoc":
            base = baseline_maps.get(group)
            if base is None:
                return None, None, None, 0.0, "no baseline maps"
            t0 = time.time()
            z = base["depth"].copy()
            z[base["confidence"] < float(spec.posthoc.get("min_confidence", 0.0))] = 0.0
            return z, base["confidence"], base["scale"], time.time() - t0, ""
        if spec.kind == "replica":
            t0 = time.time()
            img_l = recon_runner.calibration_view(self.ctx.frames, group, 1)
            img_r = recon_runner.calibration_view(self.ctx.frames, group, 0)
            left, right, vl, vr = self.ctx.model.rectified_pair(img_l, img_r)
            geom = self.ctx.model.scaled(self.ctx.scale)
            o = PyStereoOptions(
                lr_tolerance_px=float(spec.replica.get("lr_tolerance_px", 1.0)))
            st = matcher_py.compute_stereo_py(left, right, vl, vr, geom, o)
            z = matcher_py.depth_map_py(st, geom, self.ctx.model.R1)
            return z, st["confidence"], self.ctx.scale, time.time() - t0, ""
        # real CLI
        args = list(spec.cli_args)
        scale = self.ctx.scale
        if "--scale" in args:
            i = args.index("--scale")
            scale = float(args[i + 1])
            del args[i:i + 2]
        outdir = os.path.join(self.workdir, "out", spec.name, f"g{group:06d}")
        run = self.ctx.cli.run(frames_dir or self.ctx.frames, group,
                               self.ctx.profile_path, outdir, scale=scale,
                               extra_args=args)
        if not run["ok"]:
            shutil.rmtree(outdir, ignore_errors=True)
            return None, None, None, run["wall_seconds"], (run.get("stderr") or "")[:300]
        maps = recon_runner.ReconCli.read_maps(outdir)
        shutil.rmtree(outdir, ignore_errors=True)
        return maps["depth"], maps["confidence"], scale, run["wall_seconds"], ""

    # -- main -------------------------------------------------------------
    def run(self, specs, pairs=None) -> dict:
        groups = list(specs[0].groups)
        pairs = list(pairs or [])
        out = {"subset_groups": groups, "configs": {}, "order": [],
               "repeatability_pairs": [[int(a), int(b)] for a, b in pairs]}
        baseline_maps: dict = {}
        baseline_ref: dict = {}
        for spec in specs:
            self.log(f"  experiment {spec.name}: {len(groups)} groups ({spec.kind})")
            frames_dir = None
            if spec.kind == "frames":
                frames_dir = os.path.join(self.workdir, "frames", spec.name)
                written = recon_runner.write_modified_frames(
                    self.ctx.frames, frames_dir, groups, transform=spec.transform)
                if written["groups"] != len(groups):
                    self.log(f"    warning: wrote {written['groups']}/{len(groups)} groups")
            rows = []
            pts_by_group = {}
            for g in groups:
                g = int(g)
                rec = self.records.get(g, {})
                pose = rec.get("pose")
                if not pose:
                    continue
                pose = {"detected": True,
                        "R": np.asarray(pose["R"], dtype=np.float64).reshape(3, 3),
                        "T": np.asarray(pose["T"], dtype=np.float64).reshape(3)}
                z, conf, scale, wall, err = self._execute(spec, g, frames_dir, baseline_maps)
                if z is None:
                    rows.append({"group": g, "ok": False, "error": err,
                                 "runtime_s": wall})
                    continue
                if spec.name == "baseline":
                    baseline_maps[g] = {"depth": z.copy(), "confidence": conf,
                                        "scale": scale}
                ev = evaluate_maps(z, conf, self.ctx.model, scale, pose,
                                   pitch_mm=self.ctx.profile.plate_pitch_mm)
                row = {
                    "group": g,
                    "ok": bool(ev["surface"].get("ok")),
                    "valid_fraction": float((z > 0).mean()),
                    "points": int((z > 0).sum()),
                    "runtime_s": float(wall),
                    "scale": float(scale),
                    "surface": ev["surface"],
                    "image": ev["image"],
                }
                if spec.name == "baseline" and row["ok"]:
                    baseline_ref[g] = row["surface"]["bias_median_mm"]
                if row["ok"]:
                    try:
                        v, u, X = self.ctx.model.points_from_depth(z, scale)
                        Xb = (X - pose["T"]) @ pose["R"]
                        hx, hy = plate_eval.plate_half_extents_mm(
                            self.ctx.profile.plate_pitch_mm)
                        inside = (np.abs(Xb[:, 0]) <= hx) & (np.abs(Xb[:, 1]) <= hy) & \
                                 (np.abs(Xb[:, 2]) < 6.0)
                        if inside.sum() >= 50:
                            pts_by_group[g] = voxel_mean(Xb[inside])
                    except Exception:
                        pass
                if spec.name != "baseline" and g in baseline_maps:
                    ref = baseline_ref.get(g)
                    if ref is not None:
                        row.update(_new_pixel_analysis(
                            z, baseline_maps[g]["depth"], self.ctx.model,
                            scale, pose, ref,
                            base_scale=baseline_maps[g]["scale"],
                            pitch_mm=self.ctx.profile.plate_pitch_mm))
                        row["baseline_ref_mm"] = float(ref)
                rows.append(row)
            rep_pairs = []
            for ga, gb in pairs:
                if ga in pts_by_group and gb in pts_by_group:
                    c = compare_pair(pts_by_group[ga], pts_by_group[gb])
                    if c.get("ok"):
                        c.update({"a": int(ga), "b": int(gb)})
                        rep_pairs.append(c)
            rep_summary = {"n_pairs": len(rep_pairs)}
            if rep_pairs:
                for key in ("nn_p50_mm", "nn_p95_mm", "dz_mm", "plane_normal_angle_deg"):
                    vals = common.finite([r.get(key, np.nan) for r in rep_pairs])
                    if vals.size:
                        rep_summary[f"{key}_p50"] = common.p50(vals)
                        rep_summary[f"{key}_p95"] = float(np.percentile(vals, 95))
                rep_summary["dz_std_mm"] = float(np.std(
                    common.finite([r.get("dz_mm", np.nan) for r in rep_pairs])))
                rep_summary["pairs"] = rep_pairs
            out["configs"][spec.name] = {
                "kind": spec.kind,
                "varies": spec.varies,
                "description": spec.description,
                "cli_args": spec.cli_args,
                "replica": spec.replica,
                "posthoc": spec.posthoc,
                "path": {"cli": "build/recon/recon_cli",
                         "frames": "modified PGMs -> build/recon/recon_cli",
                         "posthoc": "post-process of the CLI maps",
                         "replica": "tools/live_recon/matcher_py (validated against the CLI)"}[spec.kind],
                "rows": rows,
                "summary": summarise_rows(rows),
                "repeatability": rep_summary,
            }
            out["order"].append(spec.name)
            if frames_dir is not None:
                shutil.rmtree(frames_dir, ignore_errors=True)
        return out


def summarise_rows(rows) -> dict:
    ok = [r for r in rows if r.get("ok")]
    out = {"n_groups": len(rows), "n_ok": len(ok)}
    if not ok:
        return out

    def add(name, unit="", src="row", rowkey=None, scale=1.0):
        key = rowkey or name
        if src == "surface":
            vals = [r.get("surface", {}).get(key, np.nan) for r in ok]
        elif src == "image":
            vals = [r.get("image", {}).get(key, np.nan) for r in ok]
        else:
            vals = [r.get(key, np.nan) for r in ok]
        vals = common.finite([np.nan if v is None else v * scale for v in vals])
        if vals.size == 0:
            return
        p = common.pct(vals, (5, 50, 95))
        out[f"{name}_p05{unit}"] = p[0]
        out[f"{name}_p50{unit}"] = p[1]
        out[f"{name}_p95{unit}"] = p[2]

    add("valid_fraction", "_pct", scale=100.0)
    add("points")
    add("points_on_plate", "", "surface", rowkey="n_points_in_outline")
    add("runtime", "_s", rowkey="runtime_s")
    add("plane_rms", "_mm", "surface", rowkey="plane_rms_mm")
    add("plane_p95_abs", "_mm", "surface", rowkey="plane_p95_abs_mm")
    add("bias_median", "_mm", "surface", rowkey="bias_median_mm")
    add("plane_tilt", "_deg", "surface", rowkey="plane_tilt_deg")
    add("range_ratio", "", "surface", rowkey="range_ratio")
    add("implied_pitch", "_mm", "surface", rowkey="implied_pitch_mm")
    add("coverage_1mm_frac", "", "surface", rowkey="coverage_1mm_frac")
    add("plate_valid_fraction", "", "image")
    add("edge_band_invalid_fraction", "", "image")
    add("marker_ring_invalid_fraction", "", "image")
    add("interior_invalid_fraction", "", "image")
    for key in ("new_pixels", "lost_pixels", "new_pixels_frame", "lost_pixels_frame",
                "new_frac_on_plane", "lost_frac_on_plane", "new_abs_resid_p50_mm",
                "new_frac_of_valid", "new_signed_resid_p50_mm"):
        add(key)
    out["runtime_total_s"] = float(np.sum(common.finite([r.get("runtime_s", 0) for r in ok])))
    return out
