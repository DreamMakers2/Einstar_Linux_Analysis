#!/usr/bin/env python3
"""Phase 5: reconstruct a real live-captured sweep with ``recon_cli`` and measure it.

Example
-------
    .venv/bin/python tools/live_recon/reconstruct_sweep.py \
        --frames ~/einstar-private/sweep-b/frames \
        --profile ~/einstar-private/einstar-profiles/factory-e10.json \
        --out ~/einstar-private/analysis/recon/sweep-b \
        --groups 0-349 --scale 0.5

The command runs the production C++ pipeline on every requested group, evaluates
each reconstructed plate surface against the marker-PnP geometry, measures
pose-normalised repeatability, runs the controlled experiments and writes
``recon_report.json``, ``SUMMARY.md``, figures and a representative subset of
point clouds.  See ``tools/live_recon/README.md`` for the method.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time

import numpy as np

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from tools.live_recon import (common, experiments, figures, first_capture,  # noqa: E402
                              matcher_py, plate_eval, recon_runner, repeatability,
                              report as report_mod, sweep)
from tools.live_recon.geometry_py import Profile, RectifyModel  # noqa: E402


def parse_groups(text: str, available) -> list:
    """'0-349', '0,10,20', '0-349:5' (every 5th) or 'all'."""
    text = (text or "all").strip()
    if text in ("all", "*"):
        return list(available)
    out = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        m = re.fullmatch(r"(\d+)-(\d+)(?::(\d+))?", part)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            step = int(m.group(3)) if m.group(3) else 1
            out.extend(range(a, b + 1, step))
        else:
            out.append(int(part))
    avail = set(available)
    return sorted({g for g in out if g in avail})


def build_argparser():
    p = argparse.ArgumentParser(
        description="Reconstruct and measure a live Einstar sweep (Phase 5).")
    p.add_argument("--frames", required=True, help="directory with frame_%%06d_cam{0,1,2}.pgm")
    p.add_argument("--profile", required=True, help="factory/replacement calibration profile JSON")
    p.add_argument("--out", required=True, help="output directory (created)")
    p.add_argument("--groups", default="all", help="'0-349', '0,10,20', '0-349:5' or 'all'")
    p.add_argument("--scale", type=float, default=0.5, help="matching scale (default 0.5)")
    p.add_argument("--recon-cli", default=common.repo_path("build", "recon", "recon_cli"))
    p.add_argument("--jobs", type=int, default=4, help="parallel recon_cli workers")
    p.add_argument("--subset-pairs", type=int, default=6,
                   help="similar-pose pairs used for the per-experiment repeatability")
    p.add_argument("--subset-extra", type=int, default=6,
                   help="additional spread groups in the experiment subset")
    p.add_argument("--clip-levels", default="",
                   help="comma list of clip levels; default derived from the data")
    p.add_argument("--skip-experiments", action="store_true")
    p.add_argument("--skip-first-capture", action="store_true")
    p.add_argument("--skip-replica", action="store_true",
                   help="skip the Python replica validation (LR experiment then omitted)")
    p.add_argument("--selftest-only", action="store_true")
    p.add_argument("--quiet", action="store_true")
    return p


def log_factory(quiet):
    t0 = time.time()

    def log(msg):
        if not quiet:
            print(f"[{time.time() - t0:7.1f}s] {msg}", flush=True)
    return log


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def plane_fit_report(points_plate: np.ndarray) -> dict:
    """Plane fit of a plate-frame cloud (should be z = 0)."""
    fit = common.fit_plane(points_plate)
    if fit is None:
        return {"ok": False}
    centroid, normal, rms = fit
    resid = np.asarray(points_plate, dtype=np.float64) @ normal - float(normal @ centroid)
    return {
        "ok": True,
        "normal": [float(x) for x in normal],
        "offset_mm": float(normal @ centroid),
        "rms_mm": float(rms),
        "p95_abs_mm": float(np.percentile(np.abs(resid), 95)),
    }


def validate_replica(cli, model, frames, profile_path, groups, workdir, log) -> dict:
    """Compare the Python replica against the real CLI at default settings."""
    import shutil
    rows = []
    for g in groups:
        outdir = os.path.join(workdir, f"g{g:06d}")
        run = cli.run(frames, g, profile_path, outdir, scale=0.5)
        if not run["ok"]:
            shutil.rmtree(outdir, ignore_errors=True)
            continue
        maps = recon_runner.ReconCli.read_maps(outdir)
        shutil.rmtree(outdir, ignore_errors=True)
        img_l = recon_runner.calibration_view(frames, g, 1)
        img_r = recon_runner.calibration_view(frames, g, 0)
        left, right, vl, vr = model.rectified_pair(img_l, img_r)
        geom = model.scaled(0.5)
        st = matcher_py.compute_stereo_py(left, right, vl, vr, geom)
        z_py = matcher_py.depth_map_py(st, geom, model.R1)
        d_cli, d_py = maps["disparity"], st["disparity"]
        v_cli, v_py = maps["depth"] > 0, z_py > 0
        both = v_cli & v_py
        either = v_cli | v_py
        n_both = int(both.sum())
        n_either = int(either.sum())
        row = {
            "group": int(g),
            "valid_fraction_cli": float(v_cli.mean()),
            "valid_fraction_replica": float(v_py.mean()),
            "mask_iou": float(n_both / n_either) if n_either else None,
        }
        if n_both:
            dd = np.abs(d_cli[both].astype(np.float64) - d_py[both].astype(np.float64))
            row["disparity_delta_p50_px"] = float(np.median(dd))
            row["disparity_delta_p95_px"] = float(np.percentile(dd, 95))
            row["disparity_delta_max_px"] = float(dd.max())
            zz = np.abs(maps["depth"][both].astype(np.float64) - z_py[both].astype(np.float64))
            row["depth_delta_p50_mm"] = float(np.median(zz))
            row["depth_delta_p95_mm"] = float(np.percentile(zz, 95))
        rows.append(row)
    out = {"groups": rows, "n": len(rows)}
    if rows:
        def nan_p50(vals):
            a = np.asarray([v for v in vals if v is not None], dtype=np.float64)
            a = a[np.isfinite(a)]
            return float(np.median(a)) if a.size else float("nan")
        out["mask_iou_p50"] = nan_p50([r["mask_iou"] for r in rows])
        out["disparity_delta_p50_px_p50"] = nan_p50(
            [r.get("disparity_delta_p50_px") for r in rows])
        out["depth_delta_p50_mm_p50"] = nan_p50(
            [r.get("depth_delta_p50_mm") for r in rows])
    iou_val = out.get("mask_iou_p50")
    dz_val = out.get("depth_delta_p50_mm_p50")
    iou_ok = iou_val is not None and float(iou_val) > 0.98
    depth_ok = dz_val is not None and abs(float(dz_val)) < 0.05
    out["ok"] = bool(rows and iou_ok and depth_ok)
    out["verdict"] = (
        "replica reproduces the CLI (mask IoU %.4f, |dd| p50 %.4f px, |dz| p50 %.4f mm); "
        "used only for the lr_tolerance knob, which has no CLI flag"
        % (out.get("mask_iou_p50") or 0.0, out.get("disparity_delta_p50_px_p50") or 0.0,
           out.get("depth_delta_p50_mm_p50") or 0.0)
        if out["ok"] else
        "replica does NOT reproduce the CLI closely enough (IoU %.4f, |dz| p50 %.4f mm); "
        "its numbers are not used as measurements"
        % (out.get("mask_iou_p50") or 0, out.get("depth_delta_p50_mm_p50") or 0))
    log(f"  replica validation: IoU p50 {out.get('mask_iou_p50')}, "
        f"|dd| p50 {out.get('disparity_delta_p50_px_p50')} px, "
        f"|dz| p50 {out.get('depth_delta_p50_mm_p50')} mm "
        f"-> {'OK' if out['ok'] else 'FAIL'}")
    return out


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    args = build_argparser().parse_args(argv)
    log = log_factory(args.quiet)
    t_start = time.time()
    out = os.path.abspath(args.out)
    os.makedirs(out, exist_ok=True)
    workdir = os.path.join(out, "work")
    os.makedirs(workdir, exist_ok=True)

    frames = os.path.abspath(os.path.expanduser(args.frames))
    profile_path = os.path.abspath(os.path.expanduser(args.profile))
    if not os.path.isdir(frames):
        print(f"error: --frames not a directory: {frames}", file=sys.stderr)
        return 2
    available = sorted(int(m.group(1)) for m in
                       (re.match(r"frame_(\d{6})_cam\d\.pgm$", f)
                        for f in os.listdir(frames)) if m)
    groups = parse_groups(args.groups, available)
    if not groups:
        print("error: no matching groups", file=sys.stderr)
        return 2

    profile = Profile.load(profile_path)
    model = RectifyModel(profile)
    cli = recon_runner.ReconCli(args.recon_cli)
    log(f"profile {os.path.basename(profile_path)}  baseline {model.baseline_mm:.3f} mm  "
        f"f {model.f:.2f} px  groups {len(groups)}  cli {os.path.basename(cli.path)}")

    # representative output subset: every ~Nth group, pruned after the sweep
    stride = max(1, len(groups) // 18)
    keep_candidates = set(groups[::stride])
    keep_dir = os.path.join(out, "groups")

    report: dict = {
        "schema": "einstar.live_recon.report/1",
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "command": " ".join([sys.executable] + sys.argv),
        "environment": common.environment_record(),
        "inputs": {
            "frames": frames,
            "n_groups_available": len(available),
            "groups": groups,
            "profile": profile_path,
            "profile_fingerprint": common.file_fingerprint(profile_path),
            "profile_id_doc": {k: profile.doc.get(k) for k in
                               ("schema", "kind", "color_status", "plate_pitch_mm",
                                "image_width", "image_height")},
            "rectified_model": {
                "f_px": model.f, "cx": model.cx, "cy": model.cy,
                "baseline_mm": model.baseline_mm,
                "baseline_from_centres_mm": model.baseline_from_centres_mm,
                "disp_offset_full": model.disp_offset_full,
                "alpha": model.alpha,
                "size": list(model.size),
            },
            "recon_cli": cli.fingerprint,
            "matching_scale": args.scale,
            "data_written_readonly": True,
        },
        "timing": {},
    }
    log(f"output {out}")

    # ---- self-test: depth map -> point cloud inversion -------------------
    log("self-test: depth-map -> point-cloud inversion vs the CLI PLY")
    selftest = recon_runner.selftest_inversion(
        cli, model, profile_path, frames, groups[0],
        os.path.join(workdir, "selftest"), scale=args.scale)
    report["selftest"] = selftest
    if selftest.get("ok"):
        log(f"  inversion max |delta| {selftest['max_abs_coord_diff_mm']:.2e} mm "
            f"over {selftest['n_points']} points")
    if args.selftest_only:
        common.write_json(os.path.join(out, "selftest.json"), selftest)
        return 0 if selftest.get("ok") else 1

    # ---- main sweep ------------------------------------------------------
    ctx = sweep.SweepContext(
        cli=cli, profile=profile, profile_path=profile_path, model=model,
        frames=frames, workdir=workdir, scale=args.scale,
        keep_groups=keep_candidates, out_groups_dir=keep_dir, prefix="recon")
    log(f"sweep: {len(groups)} groups through recon_cli (scale {args.scale}, {args.jobs} jobs)")
    t0 = time.time()
    records, plate_points, sweep_wall = sweep.run_sweep(ctx, groups, jobs=args.jobs, log=log)
    report["timing"]["sweep_wall_s"] = sweep_wall
    agg = sweep.aggregate(records)
    report["sweep"] = {"aggregate": agg, "records": [_clean_record(r) for r in records]}
    log(f"  sweep done: {agg['n_groups_ok']}/{len(groups)} ok, "
        f"{agg['n_plate_detected']} plate poses, {sweep_wall:.1f}s")

    # keep only representative group directories where the plate was found
    kept = []
    for r in records:
        if r.get("group") in keep_candidates and r.get("plate_detected"):
            kept.append(int(r["group"]))
    kept = kept[:12]
    for g in keep_candidates:
        d = os.path.join(keep_dir, f"g{int(g):06d}")
        if int(g) not in kept and os.path.isdir(d):
            import shutil
            shutil.rmtree(d, ignore_errors=True)
    report["outputs_kept_groups"] = kept
    log(f"  kept full outputs for groups {kept}")

    # ---- repeatability ---------------------------------------------------
    log("repeatability: pose-space pairing + plate-frame statistics")
    legs = repeatability.assign_legs(records)
    pairs = repeatability.select_pairs(
        records, legs,
        max_similar_pairs=max(20, args.subset_pairs * 12),
        max_diff_pairs=120)
    rep = repeatability.run_repeatability(records, plate_points, pairs)
    rep["pairs_selected"] = {"similar": pairs["similar"], "different_angle": pairs["different_angle"]}
    report["bias_trend"] = repeatability.bias_trend(records)
    log(f"  legs {rep['n_legs']}, similar pairs {len(rep['similar'].get('pairs', []))}, "
        f"different-angle pairs {len(rep['different_angle'].get('pairs', []))}")

    bulk = bulk_on_plane(plate_points)
    rep["bulk_on_plane_frac_p50"] = bulk.get("p50")
    rep["bulk_on_plane_frac_p05"] = bulk.get("p05")
    rep["bulk_on_plane_frac_p95"] = bulk.get("p95")
    rep["bulk_on_plane_tol_mm"] = 1.0
    log(f"  baseline bulk on-plane fraction (|z-med| < 1 mm): "
        f"{bulk.get('p50'):.3f}" if bulk.get("p50") is not None else "  bulk on-plane: n/a")
    report["repeatability"] = _jsonable(rep)

    # save the compact plate-frame samples (reproducible repeatability)
    try:
        arrays = {f"g{int(g):06d}": plate_points[g] for g in sorted(plate_points)}
        np.savez_compressed(os.path.join(out, "plate_points.npz"), **arrays)
    except Exception as e:  # not fatal
        report["plate_points_error"] = str(e)

    # ---- experiments -----------------------------------------------------
    report["experiments"] = None
    if not args.skip_experiments:
        subset = _experiment_subset(records, plate_points, pairs,
                                    args.subset_pairs, args.subset_extra)
        log(f"experiment subset: {len(subset)} groups {subset}")
        clip_levels = _clip_levels(records, frame_dir=frames,
                                   groups=subset, spec=args.clip_levels)
        log(f"  saturation clip levels for the experiment: {clip_levels}")
        specs = experiments.build_experiments(subset, clip_levels)
        records_by_group = {int(r["group"]): r for r in records if r.get("ok")}
        runner = experiments.ExperimentRunner(
            ctx, records_by_group,
            os.path.join(workdir, "experiments"), log=log)
        t0 = time.time()
        exp = runner.run(specs, pairs=[(p['a'], p['b'])
                                        for p in pairs['similar'][:args.subset_pairs]])
        exp["wall_seconds"] = time.time() - t0
        exp["clip_levels"] = clip_levels
        exp["baseline_bulk_on_plane_frac"] = rep.get("bulk_on_plane_frac_p50")
        sub_recs = [records_by_group.get(int(g), {}) for g in subset]
        angles = [r.get("pose", {}).get("plate_angle_deg") for r in sub_recs
                  if r.get("pose")]
        plate_valid = [_get_path(r, "plate", "image", "plate_valid_fraction")
                       for r in sub_recs if r.get("plate", {}).get("image", {}).get("ok")]
        exp["subset_plate_angle_deg_p50"] = common.p50(angles) if angles else None
        exp["subset_plate_valid_frac_p50"] = common.p50(plate_valid) if plate_valid else None
        # replicate the fixed similar-pose pairs for the per-experiment repeatability
        report["experiments"] = _jsonable(exp)
        log(f"  experiments done in {exp['wall_seconds']:.1f}s")

    # ---- replica validation ---------------------------------------------
    report["replica_validation"] = None
    if not args.skip_experiments and not args.skip_replica:
        log("validating the Python replica against recon_cli")
        val_groups = groups[:: max(1, len(groups) // 3)][:3]
        report["replica_validation"] = validate_replica(
            cli, model, frames, profile_path, val_groups,
            os.path.join(workdir, "replica"), log)

    # ---- first capture comparison ---------------------------------------
    report["first_capture"] = None
    if not args.skip_first_capture:
        fc_dir = os.path.expanduser("~/einstar-private/first-capture")
        if os.path.isdir(fc_dir):
            log("first-capture comparison (Nov 2026 genuine triplets)")
            fc = first_capture.evaluate_first_capture(
                cli, model, profile, fc_dir, profile_path,
                os.path.join(workdir, "first_capture"), groups=(0, 1, 2), scale=args.scale,
                log=log)
            fc["published"] = first_capture.PUBLISHED
            report["first_capture"] = _jsonable(fc)

    # ---- figures ---------------------------------------------------------
    log("figures")
    figdir = os.path.join(out, "figures")
    figs = {}
    okrecords = [r for r in records if r.get("ok")]
    old_vf = first_capture.PUBLISHED["first_capture_valid_fraction_pct"]
    old_p50 = first_capture.PUBLISHED["first_capture_depth_p05_p50_p95_mm"][1]
    figs["valid_fraction"] = figures.fig_valid_fraction(
        okrecords, os.path.join(figdir, "valid_fraction.png"), old_vf)
    figs["depth_quality"] = figures.fig_depth_quality(
        okrecords, os.path.join(figdir, "depth_quality.png"), old_p50)
    figs["plane_rms"] = figures.fig_plane_rms(okrecords, os.path.join(figdir, "plane_rms.png"))
    figs["repeatability_pose"] = figures.fig_repeatability(
        rep, os.path.join(figdir, "repeatability_pose.png"))
    if kept:
        # representative group for the cloud/map figures: the kept group whose
        # plate planarity is closest to the median over the whole sweep (an
        # oblique outlier would misrepresent the typical surface)
        rms_by_group = {int(r["group"]): _get_path(r, "plate", "surface", "plane_rms_mm")
                        for r in records if r.get("ok")}
        vals = [rms_by_group[g] for g in kept if rms_by_group.get(g)]
        if vals:
            target = float(np.median(vals))
            g = min(kept, key=lambda gg: abs((rms_by_group.get(gg) or 1e9) - target))
        else:
            g = kept[len(kept) // 2]
        report["outputs_representative_group"] = int(g)
        log(f"  representative group for figures: {g} "
            f"(plane RMS {rms_by_group.get(g)})")
        pts = plate_points.get(g)
        if pts is not None:
            figs["cloud_plate"] = figures.fig_cloud_plate(
                pts, os.path.join(figdir, "cloud_plate.png"),
                f"group {g}: {len(pts)} plate points (1 mm voxel means), "
                f"plate frame")
        figs["example_maps"] = figures.fig_example_maps(
            os.path.join(keep_dir, f"g{g:06d}"), "recon",
            os.path.join(figdir, "example_maps.png"))
    if report["experiments"]:
        figs["experiments"] = figures.fig_experiments(
            report["experiments"], os.path.join(figdir, "experiments_coverage_vs_rms.png"))
    report["figures"] = {k: v for k, v in figs.items() if v}

    # ---- report ----------------------------------------------------------
    report["timing"]["total_wall_s"] = time.time() - t_start
    report["limitations"] = report_mod.build_limitations(report)
    report["cxx_change_requests"] = report_mod.build_cxx_requests(report)
    report["scoreboard"] = report_mod.build_scoreboard(report, plate_points)
    report["outputs"] = _output_inventory(out)
    common.write_json(os.path.join(out, "recon_report.json"), report)
    report_mod.write_summary(report, os.path.join(out, "SUMMARY.md"))
    log(f"report: {os.path.join(out, 'recon_report.json')}")
    log(f"summary: {os.path.join(out, 'SUMMARY.md')}")
    log(f"total wall {report['timing']['total_wall_s']:.1f}s, "
        f"output {report['outputs']['total_bytes'] / 1e6:.1f} MB")
    return 0


# ---------------------------------------------------------------------------
# helpers used by main
# ---------------------------------------------------------------------------

def _get_path(d, *keys, default=None):
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
        if cur is None:
            return default
    return cur


def bulk_on_plane(plate_points: dict, tol_mm: float = 1.0) -> dict:
    """Fraction of each group's plate points within +-tol of its own median plane.

    This is the reference against which an experiment's *newly accepted* points
    are judged: a config whose new points score far below this bulk fraction has
    added pixels that the pose-derived plane does not confirm.
    """
    vals = []
    for _g, p in plate_points.items():
        if p is None or len(p) < 100:
            continue
        z = np.asarray(p, dtype=np.float64)[:, 2]
        vals.append(float(np.mean(np.abs(z - np.median(z)) < tol_mm)))
    if not vals:
        return {"p50": None}
    p = common.pct(vals, (5, 50, 95))
    return {"p05": p[0], "p50": p[1], "p95": p[2], "tol_mm": tol_mm, "n": len(vals)}


def _clean_record(r: dict) -> dict:
    """Drop the large per-image arrays and keep the JSON record compact."""
    out = dict(r)
    for key in ("plate",):
        if key in out and isinstance(out[key], dict):
            out[key] = out[key]
    return out


def _jsonable(obj):
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items() if not k.startswith("_")}
    if isinstance(obj, list):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, (np.floating, np.integer, np.bool_)):
        return obj.item()
    return obj


def _experiment_subset(records, plate_points, pairs, n_pairs, n_extra) -> list:
    """Similar-pose pair groups (for per-experiment repeatability) + spread."""
    chosen = []
    for p in pairs.get("similar", [])[:n_pairs]:
        for g in (p["a"], p["b"]):
            if g in plate_points and g not in chosen:
                chosen.append(int(g))
    det = [int(r["group"]) for r in records
           if r.get("plate_detected") and r.get("group") in plate_points]
    if det and n_extra > 0:
        step = max(1, len(det) // n_extra)
        for g in det[::step][:n_extra]:
            if g not in chosen:
                chosen.append(int(g))
    return sorted(chosen)


def _clip_levels(records, frame_dir, groups, spec: str) -> list:
    """Saturation clip levels; the default is derived from the old capture."""
    if spec:
        return [int(x) for x in spec.split(",") if x.strip()]
    import cv2
    levels = []
    if groups:
        img = recon_runner.load_frame(frame_dir, groups[0], 1)
        if img is not None:
            # the Nov capture had 22.2 % of the left IR stream at 255; the same
            # saturated fraction here needs clipping at this percentile
            q = float(np.percentile(img, 100.0 - 22.2))
            levels.append(int(round(q)))
    levels.extend([128, 64])
    seen, outl = set(), []
    for v in levels:
        v = max(1, min(254, int(v)))
        if v not in seen:
            seen.add(v)
            outl.append(v)
    return outl


def _output_inventory(out: str) -> dict:
    total = 0
    files = []
    for root, _dirs, names in os.walk(out):
        for n in names:
            p = os.path.join(root, n)
            try:
                total += os.path.getsize(p)
            except OSError:
                continue
            files.append(os.path.relpath(p, out))
    return {"total_bytes": total, "total_mb": total / 1e6, "n_files": len(files),
            "files": sorted(files)[:400]}


if __name__ == "__main__":
    raise SystemExit(main())
