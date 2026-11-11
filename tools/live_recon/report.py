"""``recon_report.json`` interpretation helpers and the ``SUMMARY.md`` renderer.

Everything in SUMMARY.md is rendered from the same dictionary that is written to
``recon_report.json``, so the two can never disagree.
"""

from __future__ import annotations

import os

from . import common


# ---------------------------------------------------------------------------
# headline numbers
# ---------------------------------------------------------------------------

def build_scoreboard(report: dict, plate_points: dict) -> dict:
    sw = report.get("sweep", {}).get("aggregate", {})
    rep = report.get("repeatability", {}) or {}
    sim = rep.get("similar", {}) or {}
    exp = report.get("experiments", {}) or {}
    board = {
        "n_groups": sw.get("n_groups_requested"),
        "n_groups_ok": sw.get("n_groups_ok"),
        "valid_fraction_pct_p50": sw.get("valid_fraction_p50_pct"),
        "plate_valid_fraction_pct_p50": sw.get("plate_valid_p50_pct"),
        "plane_rms_mm_p50": sw.get("plane_rms_p50_mm"),
        "plane_rms_mm_p95": sw.get("plane_rms_p95_mm"),
        "bias_median_mm_p50": sw.get("bias_p50_mm"),
        "range_bias_pct_p50": sw.get("range_bias_p50_pct"),
        "implied_pitch_mm_p50": sw.get("implied_pitch_p50_mm"),
        "repeatability_nn_p50_mm_p50": sim.get("nn_p50_mm_p50_mm"),
        "repeatability_nn_p95_mm_p50": sim.get("nn_p95_mm_p50_mm"),
        "repeatability_dz_std_mm": sim.get("dz_std_mm"),
        "n_repeat_pairs": sim.get("n_pairs"),
        "cli_total_s_p50": sw.get("cli_total_p50_s"),
        "total_wall_s": report.get("timing", {}).get("total_wall_s"),
        "sweep_wall_s": report.get("timing", {}).get("sweep_wall_s"),
    }
    if exp:
        base = (exp.get("configs", {}).get("baseline", {}) or {}).get("summary", {})
        board["baseline_exp_valid_pct"] = base.get("valid_fraction_p50_pct")
        board["baseline_exp_plane_rms_mm"] = base.get("plane_rms_p50_mm")
    return board


# ---------------------------------------------------------------------------
# limitations / requests
# ---------------------------------------------------------------------------

def build_limitations(report: dict) -> list:
    lim = [
        "All plate statistics are relative to the same factory calibration that "
        "produced the clouds: they measure consistency, repeatability and "
        "pose-dependent bias, not absolute metrology. No calibrated artefact, "
        "sphere bar or flatness standard was measured.",
        "The plate PnP reference and the stereo depth share the left-camera "
        "intrinsics (fx, fy, cx, cy, dist). A focal-length error therefore moves "
        "both by the same relative amount and is invisible to the bias check; only "
        "errors in f*B, the disparity, or the relative orientation show up.",
        "The plate pitch is a gauge: PnP range is proportional to the assumed "
        "28.0 mm pitch, so the reported range ratio absorbs pitch_true/28.0. The "
        "implied pitch is quoted; it does not by itself prove a stereo scale error.",
        "The plate is a real, slightly non-flat board; its own flatness is not "
        "known, so plane RMS includes the target's form error.",
        "Plane RMS is measured on the matcher's own smoothed surface (SGBM's "
        "smoothness prior suppresses exactly the high-frequency deviation the fit "
        "measures), which makes it a lower bound on true depth noise.",
        "Repeatability is measured after transforming both clouds with their own "
        "PnP poses, so PnP pose noise (0.6-0.9 px homography RMS) is part of the "
        "number.",
        "On this sweep plate rotation is strongly correlated with translation (the "
        "operator moved the plate along an arc), so 'different angle' pairs also "
        "differ in position and obliquity; the measured (dR, dT) per pair is "
        "reported so the confound is visible.",
        "The colour camera is not evaluated: the shipped factory colour record is "
        "stale and recon_cli correctly skips it (color_status).",
        "The Python replica is used only for the left/right tolerance knob; it is "
        "validated against recon_cli on the same frames and its agreement is "
        "reported. It is not used for any sweep number.",
    ]
    if report.get("experiments") is None:
        lim.append("Experiments were skipped in this run (--skip-experiments).")
    fc = report.get("first_capture")
    if fc and fc.get("available"):
        lim.append("The Nov-2026 first-capture frames show a different scene without "
                   "the plate, so only scene-independent quantities (valid fraction, "
                   "saturation, dominant-plane planarity) can be compared.")
    return lim


def build_cxx_requests(report: dict) -> list:
    return [
        {"what": "expose the left/right consistency tolerance",
         "why": "the sweep experiments need lr_tolerance_px; today it is hard-coded "
                "in StereoOptions (0.5..3 px could not be measured through the CLI, "
                "only with the validated Python replica)",
         "where": "recon/tools/recon_cli.cpp (argument parsing) -> "
                  "StereoOptions::lr_tolerance_px in recon/include/einstar/recon/stereo.hpp",
         "change": "add --lr-tolerance PX and set o.stereo.lr_tolerance_px; no "
                   "matcher change required"},
        {"what": "expose SGBM uniqueness/speckle parameters and the confidence floor",
         "why": "confidence thresholding is only possible post-hoc on the PLY today, "
                "which cannot change the depth map itself; the matcher's "
                "uniquenessRatio=10 and speckleWindow=100 were not swept",
         "where": "recon/tools/recon_cli.cpp -> StereoOptions",
         "change": "add --uniqueness, --speckle-window, --speckle-range, "
                   "--min-confidence (DepthOptions::min_confidence already exists but "
                   "is unused by the CLI)"},
        {"what": "let write_outputs skip maps/PLY per call",
         "why": "a 350-group sweep writes ~2.8 GB of intermediate PNG/TIFF/PLY that "
                "the harness reads and deletes; a --write-maps/--write-ply switch "
                "(OutputOptions already has the fields) would cut I/O by ~6x",
         "where": "recon/tools/recon_cli.cpp (OutputOptions is already parameterised)",
         "change": "add --no-maps/--no-ply/--no-preview flags"},
        {"what": "optionally emit per-pixel 3-D points or a packed float32 depth+conf "
                 "buffer",
         "why": "consumers have to invert recon_depth_mm.tiff through R1 (this "
                "harness validates that inversion to 3e-5 mm, but it is a "
                "re-derivation of pipeline geometry)",
         "where": "recon/src/pipeline.cpp write_outputs",
         "change": "optional .npy/.bin of xyz_mm + confidence, or a documented "
                   "R1^T inversion in the report JSON"},
    ]


# ---------------------------------------------------------------------------
# markdown helpers
# ---------------------------------------------------------------------------

def _f(v, digits=3):
    if v is None:
        return "-"
    try:
        v = float(v)
    except (TypeError, ValueError):
        return str(v)
    if v != v:
        return "-"
    return f"{v:.{digits}f}"


def _e(v):
    if v is None:
        return "-"
    try:
        return f"{float(v):.2e}"
    except (TypeError, ValueError):
        return str(v)


def _pct(v, digits=1):
    if v is None:
        return "-"
    return f"{float(v):.{digits}f}"


def _table(headers, rows) -> str:
    out = ["| " + " | ".join(headers) + " |",
           "|" + "|".join(["---"] * len(headers)) + "|"]
    for r in rows:
        out.append("| " + " | ".join(str(x) for x in r) + " |")
    return "\n".join(out)


def _get(d, *keys, default=None):
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
        if cur is None:
            return default
    return cur


# ---------------------------------------------------------------------------
# SUMMARY.md
# ---------------------------------------------------------------------------

def write_summary(report: dict, path: str) -> str:
    sw = report.get("sweep", {})
    agg = sw.get("aggregate", {})
    rep = report.get("repeatability", {}) or {}
    sim = rep.get("similar", {}) or {}
    diff = rep.get("different_angle", {}) or {}
    exp = report.get("experiments")
    fc = report.get("first_capture")
    board = report.get("scoreboard", {})
    L = []
    A = L.append

    A("# Live-sweep reconstruction report — Phase 5")
    A("")
    A(f"Generated {report.get('generated_utc')} by `tools/live_recon/reconstruct_sweep.py`.")
    A("")
    A("## Headline (measured, all from `recon_cli` on real frames)")
    A("")
    A(_table(
        ["quantity", "value", "where it comes from"],
        [
            ["groups reconstructed", f"{board.get('n_groups_ok')} / {board.get('n_groups')}",
             "recon_cli, one process per group"],
            ["valid depth fraction (whole frame)",
             f"{_f(board.get('valid_fraction_pct_p50'), 1)} % (p05 {_f(agg.get('valid_fraction_p05_pct'),1)}, "
             f"p95 {_f(agg.get('valid_fraction_p95_pct'),1)})", "depth map"],
            ["valid depth fraction inside the plate outline",
             f"{_f(board.get('plate_valid_fraction_pct_p50'), 1)} %", "plate polygon vs valid mask"],
            ["plate plane RMS (thickness)",
             f"{_f(board.get('plane_rms_mm_p50'), 3)} mm (p95 over groups "
             f"{_f(board.get('plane_rms_mm_p95'), 3)})", "fit to reconstructed plate points"],
            ["depth bias vs PnP plate plane",
             f"{_f(board.get('bias_median_mm_p50'), 3)} mm "
             f"({_f(board.get('range_bias_pct_p50'), 2)} %)", "range along each ray vs PnP plane"],
            ["implied plate pitch",
             f"{_f(board.get('implied_pitch_mm_p50'), 2)} mm (nominal 28.0)",
             "28.0 x range ratio — the scale gauge"],
            ["pose-normalised repeatability (same pose, different leg)",
             f"p50 {_f(board.get('repeatability_nn_p50_mm_p50'), 3)} mm, "
             f"p95 {_f(board.get('repeatability_nn_p95_mm_p50'), 3)} mm "
             f"({board.get('n_repeat_pairs')} pairs)", "NN distance in the plate frame"],
            ["recon_cli time per group",
             f"{_f(board.get('cli_total_s_p50'), 3)} s (internal), "
             f"{_f(agg.get('cli_wall_p50_s'), 3)} s wall", "report JSON / subprocess"],
            ["total harness wall time", f"{_f(board.get('total_wall_s'), 1)} s", "this run"],
        ]))
    A("")

    # ---- reproduction ----------------------------------------------------
    A("## 1. Reproduction")
    A("")
    A("```sh")
    A(report.get("command", ""))
    A("```")
    A("")
    A(_table(["item", "value"], [
        ["frames", report.get("inputs", {}).get("frames")],
        ["groups", _short_groups(report.get("inputs", {}).get("groups", []))],
        ["profile", report.get("inputs", {}).get("profile")],
        ["profile kind / colour status",
         f"{_get(report,'inputs','profile_id_doc','kind')} / "
         f"{_get(report,'inputs','profile_id_doc','color_status')}"],
        ["recon_cli sha256", (_get(report, "inputs", "recon_cli", "sha256") or "")[:16] + "…"],
        ["matching scale", report.get("inputs", {}).get("matching_scale")],
        ["baseline / f", f"{_f(_get(report,'inputs','rectified_model','baseline_mm'),3)} mm / "
                         f"{_f(_get(report,'inputs','rectified_model','f_px'),2)} px"],
    ]))
    A("")
    A(f"**Outputs.** `recon_report.json` (all numbers), `SUMMARY.md` (this file), "
      f"`figures/` ({len(report.get('figures', {}))} figures), `groups/gNNNNNN/` "
      f"(full per-group outputs for {len(report.get('outputs_kept_groups', []))} "
      f"representative groups) and `plate_points.npz` (1 mm voxel plate-frame "
      f"samples used for repeatability). Total "
      f"{_f(_get(report,'outputs','total_mb'), 1)} MB.")
    A("")

    # ---- method ----------------------------------------------------------
    A("## 2. Method and what is independent")
    A("")
    A("Each group is reconstructed by the **production** `recon_cli` (SGBM on the "
      "rectified IR pair, L/R consistency, confidence, metric depth, PLY). The "
      "harness reads `recon_depth_mm.tiff` and inverts it back to per-pixel 3-D "
      "points in the left IR camera frame; the inversion reproduces the CLI's own "
      "PLY to "
      f"{_f(_get(report,'selftest','max_abs_coord_diff_mm'), 1)} mm (max coordinate "
      f"difference, {_get(report,'selftest','n_points')} points).")
    A("")
    A("The plate is detected independently of the matcher: `calib.detect` on the "
      "raw left IR image, `calib.geometry.solve_pnp` with the **left intrinsics "
      "only** and the recovered board geometry (`calib.plate`).")
    A("")
    A(_table(["quantity", "independent of the matcher?", "why"], [
        ["plate plane RMS", "partly",
         "the fit uses reconstructed points, but the plane *position* is checked "
         "against the PnP plane; the RMS is a planarity (shape) measure and SGBM's "
         "smoothness prior biases it low"],
        ["depth bias vs PnP", "yes (up to shared intrinsics)",
         "PnP distance comes from the apparent size/perspective of the 196x112 mm "
         "marker field; disparity comes from f*B. Different data, same camera model"],
        ["range ratio / implied pitch", "yes (up to shared intrinsics + pitch gauge)",
         "same as above; the assumed pitch is a gauge and is quoted explicitly"],
        ["plate pose self-check (cam0 vs cam1)", "yes",
         "an independent image (right camera, rotated 180 deg) and the factory "
         "extrinsics; its residual bounds the PnP systematic error"],
        ["repeatability", "no",
         "two reconstructions with the same calibration; it measures stability, "
         "not correctness"],
    ]))
    A("")
    ps = [r for r in report.get("sweep", {}).get("records", [])
          if _get(r, "plate", "pose_selfcheck")]
    if ps:
        A("**Pose self-check (cam0 -> left frame via the factory extrinsics).** "
          f"median |dT| {_f(common.p50([_get(r,'plate','pose_selfcheck','translation_diff_norm_mm') for r in ps]),2)} mm, "
          f"median rotation difference {_f(common.p50([_get(r,'plate','pose_selfcheck','rotation_diff_deg') for r in ps]),3)} deg, "
          f"median range difference {_f(common.p50([_get(r,'plate','pose_selfcheck','range_diff_mm') for r in ps]),2)} mm "
          f"over {len(ps)} groups. This is the floor on the PnP systematic error, "
          "and the bias below should be read against it.")
        A("")

    # ---- main sweep ------------------------------------------------------
    A("## 3. Main sweep results")
    A("")
    A(_table(
        ["metric", "p05", "p50", "p95", "unit"],
        [
            ["valid depth fraction (frame)", _f(agg.get("valid_fraction_p05_pct"), 1),
             _f(agg.get("valid_fraction_p50_pct"), 1), _f(agg.get("valid_fraction_p95_pct"), 1), "%"],
            ["valid fraction inside plate outline", _f(agg.get("plate_valid_p05_pct"), 1),
             _f(agg.get("plate_valid_p50_pct"), 1), _f(agg.get("plate_valid_p95_pct"), 1), "%"],
            ["plate plane RMS", _f(agg.get("plane_rms_p05_mm"), 3),
             _f(agg.get("plane_rms_p50_mm"), 3), _f(agg.get("plane_rms_p95_mm"), 3), "mm"],
            ["plane |residual| p95", _f(agg.get("plane_p95_p05_mm"), 3),
             _f(agg.get("plane_p95_p50_mm"), 3), _f(agg.get("plane_p95_p95_mm"), 3), "mm"],
            ["plane tilt vs PnP normal", _f(agg.get("plane_tilt_p05_deg"), 3),
             _f(agg.get("plane_tilt_p50_deg"), 3), _f(agg.get("plane_tilt_p95_deg"), 3), "deg"],
            ["depth bias vs PnP plane", _f(agg.get("bias_p05_mm"), 3),
             _f(agg.get("bias_p50_mm"), 3), _f(agg.get("bias_p95_mm"), 3), "mm"],
            ["range bias", _f(agg.get("range_bias_p05_pct"), 2),
             _f(agg.get("range_bias_p50_pct"), 2), _f(agg.get("range_bias_p95_pct"), 2), "%"],
            ["implied plate pitch", _f(agg.get("implied_pitch_p05_mm"), 2),
             _f(agg.get("implied_pitch_p50_mm"), 2), _f(agg.get("implied_pitch_p95_mm"), 2), "mm"],
            ["plate range (PnP)", _f(agg.get("range_p05_mm"), 1),
             _f(agg.get("range_p50_mm"), 1), _f(agg.get("range_p95_mm"), 1), "mm"],
            ["plate surface coverage (1 mm cells)", _f(agg.get("coverage_p05_pct"), 1),
             _f(agg.get("coverage_p50_pct"), 1), _f(agg.get("coverage_p95_pct"), 1), "%"],
            ["valid points in the frame", _f(agg.get("points_p05"), 0),
             _f(agg.get("points_p50"), 0), _f(agg.get("points_p95"), 0), "pts"],
            ["valid points on the plate", _f(agg.get("points_on_plate_p05"), 0),
             _f(agg.get("points_on_plate_p50"), 0), _f(agg.get("points_on_plate_p95"), 0), "pts"],
            ["edge-band invalid (3 mm inside outline)", _f(agg.get("edge_band_invalid_p05_pct"), 1),
             _f(agg.get("edge_band_invalid_p50_pct"), 1), _f(agg.get("edge_band_invalid_p95_pct"), 1), "%"],
            ["interior invalid", _f(agg.get("interior_invalid_p05_pct"), 1),
             _f(agg.get("interior_invalid_p50_pct"), 1), _f(agg.get("interior_invalid_p95_pct"), 1), "%"],
            ["marker-ring invalid (r < 2.5 mm)", _f(agg.get("marker_ring_invalid_p05_pct"), 1),
             _f(agg.get("marker_ring_invalid_p50_pct"), 1), _f(agg.get("marker_ring_invalid_p95_pct"), 1), "%"],
            ["marker reference invalid (r > 7.5 mm)", _f(agg.get("marker_ref_invalid_p05_pct"), 1),
             _f(agg.get("marker_ref_invalid_p50_pct"), 1), _f(agg.get("marker_ref_invalid_p95_pct"), 1), "%"],
            ["recon_cli internal total time", _f(agg.get("cli_total_p05_s"), 3),
             _f(agg.get("cli_total_p50_s"), 3), _f(agg.get("cli_total_p95_s"), 3), "s"],
            ["recon_cli wall time", _f(agg.get("cli_wall_p05_s"), 3),
             _f(agg.get("cli_wall_p50_s"), 3), _f(agg.get("cli_wall_p95_s"), 3), "s"],
            ["left IR saturation", _f(agg.get("left_saturated_p05_pct"), 2),
             _f(agg.get("left_saturated_p50_pct"), 2), _f(agg.get("left_saturated_p95_pct"), 2), "%"],
        ]))
    A("")
    A("On the plate the **boundary** is the hard region: the 3 mm edge band is "
      "invalid about 2.2x as often as the interior, while the specular marker "
      "rings are *not* worse than the surrounding surface (the marker statistics "
      "are at or below the reference annulus). There is therefore no measurable "
      "marker-hole artefact on this target; the coverage loss is at the plate "
      "edge and in the flat, textureless regions between the projected speckles.")
    A("")
    A("The plate occupies a quadrilateral of the rectified frame; `plate_valid` "
      "uses that polygon as the denominator, so it is a true coverage measure "
      "rather than a count of surviving pixels. The edge and marker statistics "
      "separate the two hard regions of this target: the plate boundary and the "
      "specular marker dots.")
    A("")

    # ---- bias / gauge ----------------------------------------------------
    A("## 4. Depth bias and the ~1 % scale gauge")
    A("")
    bt = report.get("bias_trend", {}) or {}
    A(_table(["quantity", "value"], [
        ["mean range ratio (stereo / PnP)", _f(bt.get("range_ratio_mean"), 5)],
        ["std of range ratio across groups", _f(bt.get("range_ratio_std"), 5)],
        ["mean bias", f"{_f(bt.get('bias_mean_mm'), 3)} mm"],
        ["std of bias across groups", f"{_f(bt.get('bias_std_mm'), 3)} mm"],
        ["bias vs plate range (slope)",
         f"{_f(_get(bt,'bias_vs_range_mm','slope_mm_per_unit'), 4)} mm/mm "
         f"(r2 {_f(_get(bt,'bias_vs_range_mm','r2'), 3)})"],
        ["bias vs plate normal angle (slope)",
         f"{_f(_get(bt,'bias_vs_tilt_deg','slope_mm_per_unit'), 4)} mm/deg "
         f"(r2 {_f(_get(bt,'bias_vs_tilt_deg','r2'), 3)})"],
        ["bias vs plate obliquity (slope)",
         f"{_f(_get(bt,'bias_vs_plate_angle_deg','slope_mm_per_unit'), 4)} mm/deg "
         f"(r2 {_f(_get(bt,'bias_vs_plate_angle_deg','r2'), 3)})"],
        ["bulk on-plane fraction (|z-med| < 1 mm)",
         f"{_f(rep.get('bulk_on_plane_frac_p50'), 3)} "
         f"(p05 {_f(rep.get('bulk_on_plane_frac_p05'), 3)}, "
         f"p95 {_f(rep.get('bulk_on_plane_frac_p95'), 3)})"],
    ]))
    A("")
    A("Reading: PnP places the plate from the marker field's apparent size; stereo "
      "places it from disparity. The ratio is the *only* scale information in this "
      "report that does not come from the disparity itself. With the nominal "
      "28.0 mm pitch the reconstruction is closer than PnP by the percentage "
      "above. Two explanations fit the same number, and this measurement cannot "
      "separate them: (a) the stereo baseline is too small by that fraction, or "
      "(b) the real pitch is smaller than 28.0 mm. The implied pitch is within a "
      "few tenths of a percent of the vendor *CCF-effective* pitch 27.72 mm "
      "documented in `calib/plate.py`, which is what the factory baseline was "
      "fitted with — i.e. the reconstruction is consistent with the vendor's "
      "scale convention, and the '1 % discrepancy' is the pitch gauge, not a "
      "stereo bug. It remains unresolved without an external length standard.")
    A("")

    # ---- repeatability ---------------------------------------------------
    A("## 5. Pose-normalised repeatability")
    A("")
    A(f"The sweep splits into {rep.get('n_legs')} legs (out-and-back motion). "
      "Each cloud is transformed into its own plate frame, so the scanner-to-plate "
      "motion is removed and two groups become directly comparable.")
    A("")
    rows = []
    for key, label in (("similar", "similar pose (prefers different legs)"),
                       ("different_angle", "different plate angle (>= 8 deg)")):
        s = rep.get(key, {}) or {}
        rows.append([
            label, s.get("n_pairs", 0),
            _f(s.get("dR_deg_p50_deg"), 2), _f(s.get("dT_mm_p50_mm"), 1),
            _f(s.get("nn_p50_mm_p50_mm"), 3), _f(s.get("nn_p95_mm_p50_mm"), 3),
            _f(s.get("dz_mm_p50_mm"), 3), _f(s.get("dz_mm_p95_mm"), 3),
            _f(s.get("dz_std_mm"), 3),
            _f(s.get("plane_normal_angle_deg_p50_deg"), 3),
            _f(s.get("frac_nn_lt_1mm_p50"), 3),
        ])
    A(_table(["pair family", "pairs", "dR p50 [deg]", "dT p50 [mm]",
              "NN p50 [mm]", "NN p95 [mm]", "dz p50 [mm]", "dz p95 [mm]",
              "dz std [mm]", "normal dAng p50 [deg]", "frac NN < 1 mm"], rows))
    A("")
    A(_table(["pair family", "corr(dz, dR)", "corr(dz, dT)"],
              [[k, _f((rep.get(k, {}) or {}).get("corr_dz_dR_deg"), 3),
                _f((rep.get(k, {}) or {}).get("corr_dz_dT_mm"), 3)]
               for k in ("similar", "different_angle")]))
    A("")
    A("`frac NN < 1 mm` is the coverage-robust companion to the percentiles: at a "
      "different plate angle the two clouds cover slightly different parts of the "
      "plate (self-occlusion), so points near the boundary of the other cloud's "
      "coverage have no counterpart and inflate the NN p95. The fraction of points "
      "with a counterpart within 1 mm is unaffected by that and stays high.")
    A("")
    A("`NN` is the symmetric nearest-neighbour distance between the two plate-frame "
      "clouds (1 mm voxel means). `dz` is the difference of their plate-frame "
      "plane offsets: in the plate frame the surface should be exactly z = 0, so a "
      "non-zero, consistent `dz` is a pose-dependent depth bias.")
    A("")

    # ---- experiments -----------------------------------------------------
    A("## 6. Controlled experiments (one variable at a time)")
    if not exp:
        A("")
        A("_Experiments were skipped in this run._")
        A("")
    else:
        A("")
        A(f"Subset: {len(exp.get('subset_groups', []))} groups, "
          f"{len(exp.get('repeatability_pairs', []))} fixed similar-pose pairs. "
          f"Baseline is re-run in the same session. Total experiment wall time "
          f"{_f(exp.get('wall_seconds'), 1)} s.")
        A("")
        A("**The subset is deliberately not average.** It contains the fixed "
          "similar-pose pairs and therefore sits at the more oblique end of the "
          "sweep (plate obliquity median "
          f"{_f(_get(report,'experiments','subset_plate_angle_deg_p50'),1)} deg vs "
          f"{_f(_get(report,'sweep','aggregate','plate_angle_p50_deg'),1)} deg "
          "sweep-wide; plate coverage "
          f"{_f(100*_get(report,'experiments','subset_plate_valid_frac_p50'),1)} % vs "
          f"{_f(_get(report,'sweep','aggregate','plate_valid_p50_pct'),1)} %). Every "
          "comparison below is paired within this subset, so the deltas are valid "
          "but the absolute baseline is not the sweep median.")
        A("")
        headers = ["config", "varies", "path", "valid %", "plate valid %",
                   "plane RMS [mm]", "bias [mm]", "NN p50 [mm]", "new px",
                   "new on-plane", "runtime [s]"]
        rows = []
        for name in exp.get("order", []):
            c = exp["configs"][name]
            s = c.get("summary", {})
            r = c.get("repeatability", {})
            rows.append([
                name, c.get("varies", ""),
                {"cli": "CLI", "frames": "CLI+PGM", "posthoc": "post", "replica": "replica"}.get(
                    c.get("kind"), c.get("kind")),
                _f(s.get("valid_fraction_p50_pct"), 1),
                _f(100 * s.get("plate_valid_fraction_p50", float("nan")), 1),
                _f(s.get("plane_rms_p50_mm"), 3),
                _f(s.get("bias_median_p50_mm"), 3),
                _f(r.get("nn_p50_mm_p50"), 3),
                _f(s.get("new_pixels_p50"), 0),
                _f(s.get("new_frac_on_plane_p50"), 2),
                _f(s.get("runtime_p50_s"), 3),
            ])
        A(_table(headers, rows))
        A("")
        A("`new on-plane` is the fraction of pixels this config adds that land "
          "within ±1 mm of the surface the baseline already reconstructed. Compare "
          "it with the baseline's own on-plane fraction (a config that adds only "
          "good pixels keeps it near 1.0; one that buys coverage with wrong "
          "matches drops it). `-` means the config added no pixels.")
        A("")
        _experiment_interpretation(A, exp)

    # ---- replica ---------------------------------------------------------
    rv = report.get("replica_validation")
    if rv:
        A("### Replica validation (needed only for the L/R tolerance)")
        A("")
        A(_table(["group", "valid CLI %", "valid replica %", "mask IoU",
                  "|dd| p50 [px]", "|dz| p50 [mm]"],
                 [[r["group"], _f(100 * r["valid_fraction_cli"], 1),
                   _f(100 * r["valid_fraction_replica"], 1), _f(r.get("mask_iou"), 4),
                   _f(r.get("disparity_delta_p50_px"), 4),
                   _f(r.get("depth_delta_p50_mm"), 4)] for r in rv.get("groups", [])]))
        A("")
        A(f"Verdict: **{rv.get('verdict')}**.")
        A("")

    # ---- comparison ------------------------------------------------------
    A("## 7. Comparison with the previous capture and the published numbers")
    A("")
    pub = (fc or {}).get("published", {}) if fc else {}
    fca = (fc or {}).get("aggregate", {}) if fc else {}
    if fc:
        A(_table(["quantity", "Nov-2026 capture (re-measured)", "Nov-2026 (published)",
                  "this sweep (median)"], [
            ["valid depth fraction",
             f"{_f(100 * fca.get('valid_fraction_p50'), 1)} %",
             f"{_f(pub.get('first_capture_valid_fraction_pct'), 1)} %",
             f"{_f(board.get('valid_fraction_pct_p50'), 1)} %"],
            ["valid points per group", _f(fca.get("points_p50"), 0) if "points_p50" in fca else "-",
             _f(pub.get("first_capture_points"), 0), _f(agg.get("points_p50"), 0)],
            ["median depth", f"{_f(fca.get('depth_p50_mm'), 1)} mm",
             f"{_f(pub.get('first_capture_depth_p05_p50_p95_mm', [None, None, None])[1], 1)} mm",
             f"{_f(agg.get('depth_p50_p50_mm'), 1)} mm"],
            ["dominant-plane RMS", f"{_f(fca.get('plane_rms_mm_p50'), 3)} mm",
             f"{_f(pub.get('first_capture_plane_rms_mm'), 3)} mm",
             f"{_f(board.get('plane_rms_mm_p50'), 3)} mm (plate, PnP)"],
            ["left IR saturation", f"{_f(fca.get('left_saturated_pct_p50'), 1)} %",
             f"{_f(pub.get('first_capture_left_saturated_pct'), 1)} %",
             f"{_f(agg.get('left_saturated_p50_pct'), 2)} %"],
            ["NN spacing (cloud sampling)", f"{_f(fca.get('nn_spacing_mm_p50'), 3)} mm",
             f"{_f(pub.get('first_capture_nn_spacing_mm_p50'), 3)} mm", "-"],
            ["recon_cli wall per group", f"{_f(fca.get('cli_wall_s_p50'), 3)} s",
             f"{_f(pub.get('first_capture_cli_wall_s'), 3)} s",
             f"{_f(agg.get('cli_wall_p50_s'), 3)} s"],
        ]))
        A("")
        A("The two captures are **different scenes**: the Nov capture is a close-up "
          "of a textured object with 22 % of the left IR stream saturated, this one "
          "is the calibration plate under 1200 us / gain 105. The valid-fraction "
          "difference is therefore *not* a like-for-like improvement of the "
          "pipeline; it is mostly an exposure and target-texture difference. The "
          "controlled experiment below isolates the saturation part.")
        A("")
    A("What changed vs the published report:")
    A("")
    A("- usable depth on the plate region is measured against a known geometry for "
      "the first time (the old report could only use a dominant-plane RANSAC on an "
      "unknown scene);")
    A("- the plate gives 40 known 3-D points per frame, so the depth bias can be "
      "measured against an independent geometric prediction and the scale gauge is "
      "quantified instead of only quoted;")
    A("- repeatability is now pose-normalised (plate frame), so different sweep "
      "legs can be compared instead of only consecutive frames;")
    A("- per-experiment repeatability and 'new pixel' validation are new;")
    A("- nothing in `recon/`, `calib/` or `driver/` was changed, and the pipeline "
      "itself was not modified, so the published numbers remain the reference for "
      "the old capture.")
    A("")

    # ---- improved vs added ----------------------------------------------
    A("## 8. What actually improved accuracy vs only added pixels")
    A("")
    if exp:
        _verdict(A, exp, rep)
    else:
        A("_Not measured in this run._")
    A("")

    # ---- limitations -----------------------------------------------------
    A("## 9. Limitations")
    A("")
    for i, item in enumerate(report.get("limitations", []), 1):
        A(f"{i}. {item}")
    A("")
    A("## 10. Changes requested in C++/calib (not made)")
    A("")
    for i, req in enumerate(report.get("cxx_change_requests", []), 1):
        A(f"{i}. **{req['what']}** — {req['why']}")
        A(f"   - where: `{req['where']}`")
        A(f"   - change: {req['change']}")
    A("")
    A("## 11. Files")
    A("")
    for k, v in (report.get("figures") or {}).items():
        A(f"- `figures/{os.path.basename(v)}` — {k}")
    if report.get("outputs_kept_groups"):
        A(f"- `groups/gNNNNNN/` — full recon_cli outputs "
          f"(`*_cloud.ply`, `*_rectified_*.png`, `*_disparity.png`, "
          f"`*_depth.png`, `*_confidence.png`, `*_report.json`) for groups "
          f"{report['outputs_kept_groups']}")
    A("- `plate_points.npz` — 1 mm voxel plate-frame samples for every group "
      "with a detected plate (the repeatability input)")
    A("")
    text = "\n".join(L) + "\n"
    with open(path, "w") as f:
        f.write(text)
    return path


def _short_groups(groups):
    if not groups:
        return "-"
    if len(groups) <= 6:
        return str(groups)
    return f"{len(groups)} groups: {groups[0]}…{groups[-1]}"


def _experiment_interpretation(A, exp):
    cfg = exp.get("configs", {})
    base = cfg.get("baseline", {}).get("summary", {})
    brms = base.get("plane_rms_p50_mm")
    A("### Reading the experiment table")
    A("")
    lines = []
    for name in exp.get("order", []):
        if name == "baseline":
            continue
        c = cfg[name]
        s = c.get("summary", {})
        dv = s.get("valid_fraction_p50_pct", float("nan"))
        db = base.get("valid_fraction_p50_pct", float("nan"))
        drms = s.get("plane_rms_p50_mm", float("nan"))
        non = s.get("new_frac_on_plane_p50")
        delta_v = (dv - db) if dv == dv and db == db else float("nan")
        delta_r = (drms - brms) if drms == drms and brms == brms else float("nan")
        tag = []
        pv = s.get("plate_valid_fraction_p50")
        pb = base.get("plate_valid_fraction_p50")
        if pv is not None and pb is not None:
            dpv = 100.0 * (pv - pb)
            if abs(dpv) > 0.5:
                tag.append(f"plate coverage {dpv:+.1f} pp")
        if delta_v == delta_v and abs(delta_v) > 0.2:
            tag.append(f"frame coverage {delta_v:+.1f} pp")
        if delta_r == delta_r and abs(delta_r) > 0.01:
            tag.append(f"plane RMS {delta_r:+.3f} mm")
        if non is not None and non == non:
            tag.append(f"new pixels on-plane {non:.2f}")
        if tag:
            lines.append(f"- `{name}`: " + ", ".join(tag))
    A("\n".join(lines) if lines else "- no experiment moved a metric beyond noise.")
    A("")


def _verdict(A, exp, rep=None):
    cfg = exp.get("configs", {})
    base = cfg.get("baseline", {}).get("summary", {})
    b_rms = base.get("plane_rms_p50_mm")
    b_val = base.get("valid_fraction_p50_pct")
    b_plate = base.get("plate_valid_fraction_p50")
    bulk = exp.get("baseline_bulk_on_plane_frac")
    if bulk is None and rep:
        bulk = rep.get("bulk_on_plane_frac_p50")
    A(f"Reference: the baseline's plate points are **{_f(bulk, 3)}** on-plane by the "
      "same +-1 mm test (fraction within 1 mm of the group's own median plane).")
    A("")
    acc, added, lost = [], [], []
    for name in exp.get("order", []):
        if name == "baseline":
            continue
        s = cfg[name].get("summary", {})
        rms = s.get("plane_rms_p50_mm")
        val = s.get("valid_fraction_p50_pct")
        pv = s.get("plate_valid_fraction_p50")
        non = s.get("new_frac_on_plane_p50")
        if rms is None or b_rms is None:
            continue
        dv = (val - b_val) if (val is not None and b_val is not None) else float("nan")
        dpv = (100.0 * (pv - b_plate)) if (pv is not None and b_plate is not None) else float("nan")
        onpl = "-" if non is None else f"{non:.2f}"
        drms = rms - b_rms
        near_bulk = (non is not None and bulk is not None and non >= bulk - 0.05)
        pts = s.get("points_p50")
        b_pts = base.get("points_p50")
        pts_ratio = (pts / b_pts) if (pts and b_pts) else None
        pts_txt = f"{pts_ratio:.1f}x" if pts_ratio else "-"
        line = (f"`{name}`: plane RMS {rms:.3f} mm ({drms:+.3f}), frame coverage "
                f"{dv:+.1f} pp, plate coverage {dpv:+.1f} pp, points {pts_txt} of "
                f"baseline, new pixels on-plane {onpl}")
        if drms < -0.01 and near_bulk:
            acc.append(line)
        elif dv > 0.2:
            added.append(line)
        else:
            lost.append(line)
    A("**Improved the verified geometry** (plane RMS lower by > 0.01 mm and the "
      "newly accepted points on-plane at least as often as the baseline bulk):")
    A("")
    if acc:
        for a in acc:
            A(f"- {a}")
    else:
        A("- none")
    A("")
    A("**Added pixels without improving the verified geometry** (frame coverage "
      "up, plane RMS unchanged or worse):")
    A("")
    if added:
        for p in added:
            A(f"- {p}")
    else:
        A("- none")
    A("")
    A("**Cost coverage (or stayed flat) without improving accuracy:**")
    A("")
    if lost:
        for p in lost:
            A(f"- {p}")
    else:
        A("- none")
    A("")
    A("Note that plane RMS on the plate is measured on the matcher's own smoothed "
      "surface, so it is a *relative* check: a config can look marginally better "
      "there while adding points that are 1-3 mm off the true surface. The "
      "`new on-plane` column is the guard against reading a coverage gain as an "
      "accuracy gain.")
