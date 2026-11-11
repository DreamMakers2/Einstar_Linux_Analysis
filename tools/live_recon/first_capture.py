"""Comparison with the older genuine capture and with the published numbers.

The Nov-2026 ``first-capture`` directory holds three synchronised triplets of a
*different* scene (a close-up of a textured object on a desk, 22 % of the left
IR stream saturated).  The plate is not in those frames, so no plate-referenced
statistics can be computed for it: the comparison is limited to the quantities
that do not need a known target - valid-depth fraction, saturation, depth
distribution, dominant-plane planarity (RANSAC) and runtime.

The previously published numbers are quoted from
``docs/reconstruction/real_data_report.md`` and ``status.md`` and are labelled as
quoted, not re-measured here (they are re-produced on the same three frames for
the fields that are comparable).
"""

from __future__ import annotations

import os
import shutil

import numpy as np

from . import common, plate_eval, recon_runner


def ransac_plane(points: np.ndarray, thresh_mm: float = 2.0, iterations: int = 600,
                 sample: int = 4000, seed: int = 7) -> dict:
    """Dominant-plane RANSAC (no ground truth needed; same idea as the old report)."""
    P = np.asarray(points, dtype=np.float64).reshape(-1, 3)
    if len(P) < 100:
        return {"ok": False}
    rng = np.random.default_rng(seed)
    sub = P if len(P) <= sample else P[rng.choice(len(P), sample, replace=False)]
    best = None
    for _ in range(iterations):
        idx = rng.choice(len(sub), 3, replace=False)
        p0, p1, p2 = sub[idx]
        n = np.cross(p1 - p0, p2 - p0)
        norm = np.linalg.norm(n)
        if norm < 1e-9:
            continue
        n = n / norm
        d = float(n @ p0)
        inl = np.abs(sub @ n - d) < thresh_mm
        cnt = int(inl.sum())
        if best is None or cnt > best[0]:
            best = (cnt, n, d)
    if best is None:
        return {"ok": False}
    _, n, d = best
    inl = np.abs(P @ n - d) < thresh_mm
    fit = common.fit_plane(P[inl]) if inl.sum() >= 3 else None
    if fit is None:
        return {"ok": False}
    centroid, normal, rms = fit
    resid = (P[inl] - centroid) @ normal
    return {
        "ok": True,
        "normal": [float(x) for x in normal],
        "offset_mm": float(normal @ centroid),
        "rms_mm": float(np.sqrt(np.mean(resid ** 2))),
        "p95_abs_mm": float(np.percentile(np.abs(resid), 95)),
        "inlier_fraction": float(inl.mean()),
        "n_inliers": int(inl.sum()),
        "n_points": int(len(P)),
        "thresh_mm": thresh_mm,
    }


def evaluate_first_capture(cli, model, profile, frames: str, profile_path: str,
                           workdir: str, groups=(0, 1, 2), scale: float = 0.5,
                           log=print) -> dict:
    out = {"frames": frames, "groups": list(groups), "per_group": []}
    if not os.path.isdir(frames):
        out["available"] = False
        return out
    out["available"] = True
    for g in groups:
        outdir = os.path.join(workdir, f"g{g:06d}")
        run = cli.run(frames, g, profile_path, outdir, scale=scale)
        rec = {"group": int(g), "ok": False}
        try:
            if not run["ok"]:
                rec["error"] = (run.get("stderr") or "cli failed")[:300]
                out["per_group"].append(rec)
                continue
            rep = run["report"]
            maps = recon_runner.ReconCli.read_maps(outdir)
            z = maps["depth"]
            v, u, X = model.points_from_depth(z, scale)
            left = recon_runner.calibration_view(frames, g, 1)
            right = recon_runner.calibration_view(frames, g, 0)
            rec.update({
                "ok": True,
                "valid_fraction": float(rep["depth"]["valid_fraction"]),
                "points": int(rep["cloud"]["points"]),
                "depth_p05_mm": float(rep["depth"]["p05_mm"]),
                "depth_p50_mm": float(rep["depth"]["p50_mm"]),
                "depth_p95_mm": float(rep["depth"]["p95_mm"]),
                "sigma_p50_mm": float(rep["depth"]["sigma_p50_mm"]),
                "timings": rep["timings"],
                "cli_wall_seconds": run["wall_seconds"],
                "left_image": recon_runner.image_stats(left),
                "right_image": recon_runner.image_stats(right),
                "plane": ransac_plane(X),
                "nn_spacing_mm": _nn_spacing(X),
            })
        finally:
            shutil.rmtree(outdir, ignore_errors=True)
            out["per_group"].append(rec)
        log(f"  first-capture g{g}: valid {100*rec.get('valid_fraction', float('nan')):.1f} %")
    ok = [r for r in out["per_group"] if r.get("ok")]
    if ok:
        out["aggregate"] = {
            "valid_fraction_p50": common.p50([r["valid_fraction"] for r in ok]),
            "depth_p50_mm": common.p50([r["depth_p50_mm"] for r in ok]),
            "plane_rms_mm_p50": common.p50([r["plane"]["rms_mm"] for r in ok if r["plane"].get("ok")]),
            "plane_p95_mm_p50": common.p50([r["plane"]["p95_abs_mm"] for r in ok if r["plane"].get("ok")]),
            "plane_inlier_frac_p50": common.p50([r["plane"]["inlier_fraction"] for r in ok if r["plane"].get("ok")]),
            "points_p50": common.p50([r["points"] for r in ok]),
            "left_saturated_pct_p50": common.p50([100 * r["left_image"]["saturated_fraction"] for r in ok]),
            "left_mean_dn_p50": common.p50([r["left_image"]["mean_dn"] for r in ok]),
            "nn_spacing_mm_p50": common.p50([r["nn_spacing_mm"] for r in ok if r.get("nn_spacing_mm")]),
            "ir_ratio_right_over_left_p50": common.p50(
                [r["right_image"]["mean_dn"] / r["left_image"]["mean_dn"]
                 for r in ok if r["left_image"]["mean_dn"] > 0]),
            "right_saturated_pct_p50": common.p50(
                [100 * r["right_image"]["saturated_fraction"] for r in ok]),
            "cli_wall_s_p50": common.p50([r["cli_wall_seconds"] for r in ok]),
        }
    return out


def _nn_spacing(X: np.ndarray, sample: int = 4000, seed: int = 3):
    """Median nearest-neighbour spacing of a point set (mm)."""
    P = np.asarray(X, dtype=np.float64).reshape(-1, 3)
    if len(P) < 100:
        return None
    rng = np.random.default_rng(seed)
    sub = P[rng.choice(len(P), min(sample, len(P)), replace=False)]
    try:
        from scipy.spatial import cKDTree
        d, _ = cKDTree(P).query(sub, k=2)
        return float(np.median(d[:, 1]))
    except Exception:
        return None


PUBLISHED = {
    "source": "docs/reconstruction/real_data_report.md (2026-11-11), "
              "docs/reconstruction/status.md",
    "first_capture_valid_fraction_pct": 17.3,
    "first_capture_points": 56600,
    "first_capture_depth_p05_p50_p95_mm": [331.6, 349.5, 368.6],
    "first_capture_plane_rms_mm": 0.336,
    "first_capture_cross_frame_delta_z_p50_p95_mm": [0.071, 0.236],
    "first_capture_nn_spacing_mm_p50": 0.582,
    "first_capture_cli_wall_s": 0.263,
    "first_capture_left_saturated_pct": 22.2,
    "first_capture_runtime_s_full_harness": 13.0,
}
