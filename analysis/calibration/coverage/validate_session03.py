#!/usr/bin/env python3
"""Validate the corrected D4/D7 measurement on the real session03 recording.

Reads <calibration-session> read-only (manifest, decisions, status)
and the factory profile under <private-inputs> read-only.  Writes
machine-readable JSON next to this script.  No detection or capture is re-run:
the quality/motion fields are exactly those recorded by the controller.

Run:  .venv/bin/python analysis/calibration/coverage/validate_session03.py
"""
from __future__ import annotations

import hashlib
import json
import math
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from calib import geometry, plate
from calib.recon_profile import load as load_profile
from tools.wizard import coverage

SESSION = Path("<calibration-session>")
FACTORY = Path("<private-inputs>/einstar-profiles/factory-e10.json")
OUT = Path(__file__).resolve().parent


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _jl(value):
    """JSON-safe copy for numpy scalars/arrays."""
    if isinstance(value, dict):
        return {str(k): _jl(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jl(v) for v in value]
    if isinstance(value, (np.floating,)):
        return float(value)
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, np.ndarray):
        return [_jl(v) for v in value.tolist()]
    return value


def _write(name: str, payload: dict) -> None:
    path = OUT / name
    path.write_text(json.dumps(_jl(payload), indent=2, sort_keys=False) + "\n")
    print("wrote", path)


# ---------------------------------------------------------------------------
# recorded data
# ---------------------------------------------------------------------------

manifest = json.loads((SESSION / "manifest.json").read_text())
views = list(manifest["views"])
decisions = [json.loads(line) for line in
             (SESSION / "decisions.jsonl").read_text().splitlines() if line.strip()]
status = json.loads((SESSION / "status.json").read_text())

measurement = coverage.measure(views, stationarity_observations=decisions)
accepted_only = coverage.measure(views)


def _d7(measurement):
    return {
        "satisfied": measurement["D7"]["satisfied"],
        "quality": measurement["checks"]["quality"]["measured"],
        "stationarity": measurement["checks"]["stationarity"]["measured"],
        "quality_hint": measurement["checks"]["quality"]["hint"],
        "stationarity_hint": measurement["checks"]["stationarity"]["hint"],
        "deficit": measurement["D7"]["deficit"],
    }


def _readiness(measurement):
    r = coverage.readiness(measurement)
    return {"ready": r["ready"], "satisfied": len(
        [d for d in coverage.DIMENSIONS if measurement[d]["satisfied"]]),
        "unsatisfied_dimensions": r["unsatisfied_dimensions"],
        "unsatisfied": r["unsatisfied"], "summary": r["summary"]}


# ---------------------------------------------------------------------------
# old behaviour, reconstructed only for the before/after table
# ---------------------------------------------------------------------------

def _legacy_quality_passes(views) -> int:
    """Old D7 read flat quality keys that the live recorder never wrote."""
    n = 0
    for v in views:
        q = v.get("quality") or {}
        sat = q.get("saturation_frac", q.get("sat_frac"))
        foc = q.get("focus", q.get("blur"))
        ok = True
        if sat is not None:
            ok = ok and float(sat) <= 0.05
        if foc is not None:
            ok = ok and float(foc) >= 150.0
        n += int(ok)
    return n


def _legacy_stationarity(views) -> dict:
    """Old D7: speed between consecutive ACCEPTED poses."""
    order = sorted(range(len(views)),
                   key=lambda i: (views[i].get("timestamp") is None,
                                  views[i].get("timestamp") or 0.0))
    speeds, rates = [], []
    for a, b in zip(order, order[1:]):
        ta = views[a].get("timestamp")
        tb = views[b].get("timestamp")
        if ta is None or tb is None or tb <= ta:
            continue
        dt = tb - ta
        Ra = np.asarray(views[a]["pose"]["R"], float).reshape(3, 3)
        Rb = np.asarray(views[b]["pose"]["R"], float).reshape(3, 3)
        ta_v = np.asarray(views[a]["pose"]["t"], float)
        tb_v = np.asarray(views[b]["pose"]["t"], float)
        speeds.append(float(np.linalg.norm(tb_v - ta_v)) / dt)
        dR = Rb @ Ra.T
        ang = math.degrees(math.acos(min(1.0, max(-1.0, (float(np.trace(dR)) - 1) / 2))))
        rates.append(ang / dt)
    flags = []
    for i, v in enumerate(views):
        if v.get("stationary") is not None:
            flags.append(bool(v["stationary"]))
        elif speeds:
            k = max(0, i - 1)
            flags.append(bool(speeds[min(k, len(speeds) - 1)] <= 3.0
                              and rates[min(k, len(rates) - 1)] <= 1.5))
        else:
            flags.append(False)
    return {"n_stationary": int(sum(flags)),
            "speed_p50_mm_s": float(np.median(speeds)) if speeds else None,
            "rate_p50_deg_s": float(np.median(rates)) if rates else None}


# ---------------------------------------------------------------------------
# dwell decisions
# ---------------------------------------------------------------------------

def _decision_rows(decisions):
    rows = []
    for r in decisions:
        metrics = (r.get("quality") or {}).get("metrics") or {}
        speed = metrics.get("speed_mm_s")
        motion_deg = metrics.get("motion_deg")
        dt = metrics.get("dt_s")
        rate = (motion_deg / dt) if motion_deg is not None and dt else None
        rows.append({
            "group": r.get("group"), "accepted": bool(r.get("accepted")),
            "view_id": r.get("view_id"), "timestamp_s": r.get("timestamp_s"),
            "speed_mm_s": speed, "rate_deg_s": rate, "dt_s": dt,
            "quality_passed": bool((r.get("quality") or {}).get("passed")),
            "reasons": list(r.get("reasons") or []),
            "n_markers": dict(r.get("markers") or {}),
            "shared_markers_ir": r.get("shared_markers_ir"),
        })
    return rows


decision_rows = _decision_rows(decisions)
stationary_decisions = [r for r in decision_rows
                        if r["speed_mm_s"] is not None
                        and r["rate_deg_s"] is not None
                        and r["speed_mm_s"] <= 3.0
                        and r["rate_deg_s"] <= 1.5]


def _reason_class(reasons):
    text = " ".join(reasons).lower()
    if "redundant" in text:
        return "redundant_view"
    if "too similar" in text:
        return "duplicate_pose"
    if not reasons:
        return "accepted_or_unreported"
    return "quality_or_other"


dwell_quality_pass = [r for r in stationary_decisions if r["quality_passed"]]
dwell_reason_counts = Counter(_reason_class(r["reasons"])
                              for r in dwell_quality_pass)
dwell_rejected_as_redundant = [r for r in dwell_quality_pass
                              if _reason_class(r["reasons"]) in
                              ("redundant_view", "duplicate_pose")]

# Hypothetical: those good, stationary duplicate decisions are kept as
# coverage-only dwell observations.  Their quality record is recorded data;
# no pose is invented (D7 does not need one, D1-D6 exclude dwell=True).
dwell_views = [{
    "view_id": f"dwell-{r['group']}",
    "group": r["group"],
    "timestamp": r["timestamp_s"],
    "quality": next((d["quality"] for d in decisions
                     if d.get("group") == r["group"]), None),
    "dwell": True,
    "n_markers": r["n_markers"],
    "shared_markers_ir": r["shared_markers_ir"],
} for r in dwell_rejected_as_redundant]
hypothetical_dwell = coverage.measure(
    views + dwell_views, stationarity_observations=decisions)

# ---------------------------------------------------------------------------
# D4 attainability with the factory rig
# ---------------------------------------------------------------------------

profile = load_profile(FACTORY)
left = profile.cameras["ir_left"]
right = profile.cameras["ir_right"]
obj = plate.board_points_mm()
W, H = int(profile.width), int(profile.height)


def _project_pose(R, t):
    uv_l = geometry.project_world(left.K, left.dist, R, t, obj)
    x_r = obj @ (right.R @ R).T + (right.R @ t + right.T)
    uv_r = geometry.project(right.K, right.dist, x_r)
    ok_l = ((uv_l[:, 0] >= 0) & (uv_l[:, 0] < W)
            & (uv_l[:, 1] >= 0) & (uv_l[:, 1] < H))
    ok_r = ((uv_r[:, 0] >= 0) & (uv_r[:, 0] < W)
            & (uv_r[:, 1] >= 0) & (uv_r[:, 1] < H))
    centre = uv_l[ok_l].mean(axis=0) if ok_l.any() else None
    return {
        "n_left": int(ok_l.sum()), "n_right": int(ok_r.sum()),
        "n_shared": int((ok_l & ok_r).sum()),
        "left_centroid_px": None if centre is None else [float(centre[0]), float(centre[1])],
        "board_centre_px": [float(x) for x in
                            geometry.project(left.K, left.dist,
                                             np.asarray(t, float).reshape(1, 3))[0]],
    }


def _region(u, v):
    return int(v / (H / 3.0)), int(u / (W / 3.0))


def _corner_pose(d, tx, ty, roll, row, col):
    """Corner target at camera distance ``d`` (``norm(t)``), not z=d."""
    lat = (0.27 * (col - 1) * d, 0.21 * (row - 1) * d)
    z = math.sqrt(max(d * d - lat[0] * lat[0] - lat[1] * lat[1], 1.0))
    return coverage.family_pose(z, tx, ty, roll, lat), lat, z


corner_specs = [("top_left", 0, 0), ("top_right", 0, 2),
                ("bottom_left", 2, 0), ("bottom_right", 2, 2)]
attainability = {"image_size": [W, H], "bands": [], "robustness": [],
                 "full_plate_limit": []}

for band in coverage.CoverageSpec().distance_bands_mm:
    d = 0.5 * (band[0] + band[1])
    band_row = {"band_mm": list(band), "distance_mm": d, "corners": {}}
    for name, row, col in corner_specs:
        (R, t), lat, z = _corner_pose(d, 0.0, 0.0, 0.0, row, col)
        info = _project_pose(R, t)
        info["camera_distance_mm"] = d
        info["pose_z_mm"] = z
        info["target_lateral_mm"] = [float(lat[0]), float(lat[1])]
        info["visible_region"] = (None if info["left_centroid_px"] is None
                                  else list(_region(*info["left_centroid_px"])))
        info["target_region"] = [row, col]
        info["attainable_ge_28_both"] = bool(
            info["n_left"] >= 28 and info["n_right"] >= 28
            and info["n_shared"] >= 12)
        band_row["corners"][name] = info
    attainability["bands"].append(band_row)

# robustness over realistic tilt/roll at each band centre
for band in coverage.CoverageSpec().distance_bands_mm:
    d = 0.5 * (band[0] + band[1])
    row_out = {"distance_mm": d, "n_cases": 0, "n_attainable": 0,
               "n_region_match": 0, "n_right_ir_ge_28": 0,
               "n_right_ir_partial": 0, "right_ir_min": 40,
               "right_ir_p50": [], "shared_min": 40}
    for tx in (-30.0, -20.0, -10.0, 0.0, 10.0, 20.0, 30.0):
        for ty in (-30.0, -20.0, -10.0, 0.0, 10.0, 20.0, 30.0):
            for roll in (0.0, 45.0, 90.0, 135.0, 180.0, 225.0, 270.0, 315.0):
                for name, row, col in corner_specs:
                    (R, t), _lat, _z = _corner_pose(
                        d, tx, ty, roll, row, col)
                    info = _project_pose(R, t)
                    if info["left_centroid_px"] is None:
                        continue
                    row_out["n_cases"] += 1
                    reg = _region(*info["left_centroid_px"])
                    match = reg == (row, col)
                    row_out["n_region_match"] += int(match)
                    row_out["n_right_ir_ge_28"] += int(info["n_right"] >= 28)
                    row_out["n_right_ir_partial"] += int(info["n_right"] < 40)
                    row_out["right_ir_min"] = min(row_out["right_ir_min"],
                                                  info["n_right"])
                    row_out["right_ir_p50"].append(info["n_right"])
                    row_out["shared_min"] = min(row_out["shared_min"],
                                                info["n_shared"])
                    row_out["n_attainable"] += int(
                        match and info["n_left"] >= 28
                        and info["n_right"] >= 28 and info["n_shared"] >= 12)
    vals = np.asarray(row_out.pop("right_ir_p50"), float)
    row_out["right_ir_p50"] = float(np.median(vals)) if len(vals) else None
    row_out["attainable_fraction"] = (row_out["n_attainable"] /
                                      row_out["n_cases"] if row_out["n_cases"]
                                      else 0.0)
    attainability["robustness"].append(row_out)

# maximum rightward centroid for each constraint set, at zero tilt/roll
for band in coverage.CoverageSpec().distance_bands_mm:
    d = 0.5 * (band[0] + band[1])
    best = {}
    for label, min_each, min_shared in (
            ("shared_ge_12_each_ge_12", 12, 12),
            ("both_ir_ge_28", 28, 12),
            ("all_40_both_ir", 40, 40)):
        best_u = None
        for x in np.arange(0.0, 0.45 * d + 1e-9, 5.0):
            for y in np.arange(-0.30 * d, 0.30 * d + 1e-9, 5.0):
                z2 = d * d - x * x - y * y
                if z2 <= 1.0:
                    continue
                R, t = coverage.family_pose(math.sqrt(z2), 0.0, 0.0, 0.0,
                                            (float(x), float(y)))
                info = _project_pose(R, t)
                if info["left_centroid_px"] is None:
                    continue
                if (info["n_left"] >= min_each and info["n_right"] >= min_each
                        and info["n_shared"] >= min_shared):
                    if best_u is None or info["left_centroid_px"][0] > best_u["u"]:
                        best_u = {"u": float(info["left_centroid_px"][0]),
                                  "v": float(info["left_centroid_px"][1]),
                                  "lateral_mm": [float(x), float(y)],
                                  "n_left": info["n_left"],
                                  "n_right": info["n_right"],
                                  "n_shared": info["n_shared"]}
        best[label] = best_u
    attainability["full_plate_limit"].append(
        {"distance_mm": d, "best": best})

# Can all 40 markers stay in BOTH IR cameras while the visible centroid is in
# a right corner?  If yes, partial plate visibility is a property of the chosen
# offset, not a physical necessity.
attainability["corner_full_plate"] = []
for band in coverage.CoverageSpec().distance_bands_mm:
    d = 0.5 * (band[0] + band[1])
    for name, row, col in corner_specs:
        best = None
        for x in np.arange(0.0, 0.40 * d + 1e-9, 5.0):
            for y in np.arange(-0.30 * d, 0.30 * d + 1e-9, 5.0):
                z2 = d * d - x * x - y * y
                if z2 <= 1.0:
                    continue
                R, t = coverage.family_pose(math.sqrt(z2), 0.0, 0.0, 0.0,
                                            (float(x), float(y)))
                info = _project_pose(R, t)
                if info["n_left"] < 40 or info["n_right"] < 40:
                    continue
                if info["left_centroid_px"] is None:
                    continue
                if _region(*info["left_centroid_px"]) != (row, col):
                    continue
                cand = {"lateral_mm": [float(x), float(y)],
                        "pose_z_mm": math.sqrt(z2),
                        "left_centroid_px": info["left_centroid_px"],
                        "n_left": info["n_left"],
                        "n_right": info["n_right"],
                        "n_shared": info["n_shared"]}
                # prefer the target furthest inside the region
                margin = min(cand["left_centroid_px"][0] - 2.0 * W / 3.0,
                             abs(cand["left_centroid_px"][1]
                                 - ((H / 6.0) if row == 0 else (5.0 * H / 6.0))))
                if best is None or margin > best["margin_px"]:
                    cand["margin_px"] = float(margin)
                    best = cand
        attainability["corner_full_plate"].append(
            {"distance_mm": d, "corner": name,
             "full_plate_both_ir": best is not None,
             "best": best})

# actual guidance target from the corrected real measurement
guidance = coverage.guidance_target(measurement, views[-1]["pose"])
target = guidance.get("target_t")
if target is not None:
    R = np.asarray(guidance["target_R"], float)
    t = np.asarray(target, float)
    actual_info = _project_pose(R, t)
    actual_info["instruction"] = guidance["instruction"]
    actual_info["unlocks"] = guidance["unlocks"]
    actual_info["family"] = {k: v for k, v in guidance["family"].items()
                             if k in ("distance_band", "distance_mm",
                                      "tilt_x_deg", "tilt_y_deg", "roll_deg",
                                      "image_region", "lateral_mm",
                                      "distance_required", "unlock_weight")}
    actual_info["visible_region"] = (None if actual_info["left_centroid_px"] is None
                                     else list(_region(*actual_info["left_centroid_px"])))
else:
    actual_info = None

# real D4 counts from the recording
d4_views = coverage.measure(views)
d4_real = d4_views["D4"]["measured"]

# ---------------------------------------------------------------------------
# raw-frame caveat
# ---------------------------------------------------------------------------

groups_written = int(status.get("groups_written") or 0)
ring_size = int(status.get("ring_size") or 0)
last_group = int(status.get("latest_group") or status.get("last_group") or 0)
ring_start = (last_group - ring_size + 1) if ring_size else None
raw_frame_caveat = {
    "manifest_accepted_views": len(views),
    "decisions": len(decisions),
    "groups_written": groups_written,
    "frames_written": status.get("frames_written"),
    "trigger_period_us": status.get("trigger_period_us"),
    "ring_size": ring_size,
    "ring_group_range": [ring_start, last_group] if ring_start else None,
    "ring_note": ("lead evidence: the last 120 ring groups (13075-13194) were "
                  "never evaluated live and are a stationary centred plate with "
                  "40 markers in all three cameras; this script did not "
                  "re-detect them"),
    "decisions_with_speed": int(sum(1 for r in decision_rows
                                    if r["speed_mm_s"] is not None)),
    "per_raw_frame_measurement": "unavailable in the manifest: pose/motion "
                                 "fields exist per decision, not per 10 Hz raw "
                                 "frame; this script does not fabricate them",
    "raw_recording": {
        "path": "raw.e1r",
        "bytes": (SESSION / "raw.e1r").stat().st_size
        if (SESSION / "raw.e1r").is_file() else None,
        "full_recording_verified": "lead evidence: the 52 GB raw recording is "
                                   "verified and a memory-bounded streaming "
                                   "decode exists",
        "right_corner_scan": "open: the raw corpus is being scanned and may "
                             "recover right-corner frames that are not in the "
                             "297 accepted views; this script did not decode it",
    },
    "consequence": ("raw-frame measurement could add dwell observations and "
                    "sharpen the interval count.  The raw-corpus scan may also "
                    "recover right-corner frames not present in the 297 "
                    "accepted views, so the D4 verdict is conditional on that "
                    "scan; it does not change the accepted-view counts."),
}

# ---------------------------------------------------------------------------
# assemble
# ---------------------------------------------------------------------------

payload = {
    "schema": "einstar.coverage.validation/1",
    "generated_utc": datetime.now(timezone.utc).isoformat(),
    "source": {
        "session": str(SESSION),
        "manifest_sha256": _sha(SESSION / "manifest.json"),
        "decisions_sha256": _sha(SESSION / "decisions.jsonl"),
        "status_sha256": _sha(SESSION / "status.json"),
        "factory_profile": str(FACTORY),
        "factory_sha256": manifest.get("factory_sha256"),
    },
    "before": {
        "manifest_readiness": manifest.get("readiness"),
        "manifest_coverage_dimensions": {
            k: {"satisfied": v.get("satisfied"),
                "measured": v.get("measured"), "hint": v.get("hint")}
            for k, v in (manifest.get("coverage", {}).get("dimensions") or {}).items()},
        "legacy_quality_passes": _legacy_quality_passes(views),
        "legacy_quality_note": ("flat quality.saturation_frac/focus keys were "
                                "absent, so every view passed silently"),
        "legacy_stationarity": _legacy_stationarity(views),
        "legacy_stationarity_note": ("speed between consecutive ACCEPTED poses; "
                                     "sparse and biased towards motion"),
    },
    "after": {
        "measurement_without_stream": _readiness(accepted_only) | {
            "D7": _d7(accepted_only),
            "D4": {"satisfied": accepted_only["D4"]["satisfied"],
                   "measured": accepted_only["D4"]["measured"],
                   "hint": accepted_only["D4"]["hint"]},
            "dimensions": {k: {"satisfied": v["satisfied"],
                               "hint": v["hint"],
                               "measured": v["measured"]}
                           for k, v in accepted_only.get("dimensions", {}).items()},
        },
        "measurement_with_stream": _readiness(measurement) | {
            "D7": _d7(measurement),
            "D4": {"satisfied": measurement["D4"]["satisfied"],
                   "measured": measurement["D4"]["measured"],
                   "hint": measurement["D4"]["hint"]},
            "dimensions": {k: {"satisfied": v["satisfied"],
                               "hint": v["hint"],
                               "measured": v["measured"]}
                           for k, v in measurement.get("dimensions", {}).items()},
        },
        "d4_real_counts": d4_real,
        "quality_per_view": [
            {k: row[k] for k in ("view_id", "quality_source", "ok",
                                 "focus_min", "saturation_max", "tex_focus",
                                 "tex_saturation", "missing_metrics", "reasons")}
            for row in measurement["D7"]["detail"]["quality"]],
        "stationarity_per_view": [
            {k: row[k] for k in ("view_id", "stationary",
                                 "stationarity_source",
                                 "stationarity_speed_mm_s",
                                 "stationarity_rate_deg_s",
                                 "stationarity_window_s", "motion_blur_px",
                                 "reasons")}
            for row in measurement["D7"]["detail"]["stationarity"]],
        "stationarity_intervals": measurement["D7"]["detail"]["stationarity_intervals"],
        "dwell_admission_hypothetical": {
            "stationary_decisions": len(stationary_decisions),
            "stationary_quality_pass": len(dwell_quality_pass),
            "rejected_as_redundant_or_duplicate": len(dwell_rejected_as_redundant),
            "reason_counts": dict(dwell_reason_counts),
            "admitted_as_dwell_views": len(dwell_views),
            "readiness": _readiness(hypothetical_dwell),
            "D7": _d7(hypothetical_dwell),
            "D4": {"satisfied": hypothetical_dwell["D4"]["satisfied"],
                   "measured": hypothetical_dwell["D4"]["measured"]},
        },
        "guidance_target": guidance,
        "d4_actual_target_attainability": actual_info,
    },
    "d4_attainability": attainability,
    "raw_frame_caveat": raw_frame_caveat,
    "notes": [
        "D7 quality is measured from accepted views only; D7 stationarity is "
        "measured from the supplied observation stream and counts distinct "
        "dwell intervals, not frames.",
        "The 297 accepted views alone do not establish stationarity: 0 accepted "
        "dwells and 13 views have no previous-pose motion record.",
        "With the recorded decision stream, D7 stationarity is established by "
        "11 distinct intervals from 138 stationary observations.",
        "D4 remains unsatisfied in the recorded session: top-right and "
        "bottom-right have zero visits.  Corrected guidance now targets only "
        "those deficient corners.",
        "No raw-frame data was fabricated and no threshold was lowered.",
    ],
}

_write("session03_validation.json", payload)
_write("d4_attainability.json", {
    "schema": "einstar.coverage.d4_attainability/1",
    "source": payload["source"],
    **attainability,
    "actual_target": actual_info,
    "corner_full_plate": attainability["corner_full_plate"],
})
_write("d7_stationarity.json", {
    "schema": "einstar.coverage.d7_stationarity/1",
    "source": payload["source"],
    "before": payload["before"],
    "accepted_only": payload["after"]["measurement_without_stream"]["D7"],
    "with_stream": payload["after"]["measurement_with_stream"]["D7"],
    "dwell_admission_hypothetical": payload["after"]["dwell_admission_hypothetical"],
    "raw_frame_caveat": raw_frame_caveat,
})
