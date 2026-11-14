"""Coverage measurement and next-pose guidance for the calibration wizard.

This module answers two questions, both from the *accepted* views only (never
from the operator's intent, never from a frame count):

1. **Is the session's pose excitation sufficient?**  Eight dimensions D1-D8
   from ``docs/calibration/one_session_plan.md`` are measured and each returns
   ``required`` / ``measured`` / ``satisfied`` / ``deficit`` / ``hint`` /
   ``detail`` (``docs/calibration/wizard_architecture.md`` §6).
2. **What should the operator do next?**  :func:`guidance_target` names the
   single most valuable next *pose family* (distance band x tilt quadrant x
   roll sector x image region) and a coarse instruction.  It never derives a
   hand trajectory: the target is a family representative, and when the
   current pose is not stable (missing markers, too few shared markers or
   still moving) it returns a ``hold`` instruction with no target pose at all.

Geometry conventions
--------------------
``view["pose"]`` is ``(R, t)`` with ``X_cam = R X_board + t``, board in the
left-IR frame, millimetres.  Therefore

* plate normal in the camera frame      ``n = R[:, 2]``  (unit, pointing at
  the camera, i.e. ``n_z < 0`` for a fronto-parallel plate);
* tilt about the camera x axis          ``atan2(n_x, |n_z|)``;
* tilt about the camera y axis          ``atan2(n_y, |n_z|)``;
* in-plane roll                        the angle of the board x axis in the
  plate plane, against the projected camera x axis, ``mod 360`` — the
  geometric rotation about the plate normal, independent of the tilt.

The last two are the quantities the plan's D2/D3 talk about, so they are what
this module measures — not an Euler decomposition that changes meaning with
the order of the rotations.

Measured against the live one-axis sweep (350 accepted poses, ``sweep-b``)
this gate is **not ready**: see ``tests/test_wizard_coverage.py`` and the
report in ``docs/calibration/wizard_architecture.md`` §6.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from typing import Any, Mapping, Sequence

import numpy as np

from .types import CAMERA_KEYS, IMAGE_HEIGHT, IMAGE_WIDTH

#: Dimension identifiers in the order of ``one_session_plan.md`` §2.
DIMENSIONS: tuple[str, ...] = ("D1", "D2", "D3", "D4", "D5", "D6", "D7", "D8")

#: Default image of the generated camera (the wizard's ``types`` constants).
_DEFAULT_IMAGE = (IMAGE_WIDTH, IMAGE_HEIGHT)


# ---------------------------------------------------------------------------
# specification
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CoverageSpec:
    """Per-dimension requirements.

    The first block is *exactly* ``wizard_architecture.md`` §6 — do not change
    a default there without telling the integrator.  The second block is
    additive: D7 (stationarity/quality) and D8 (scale artefact) need
    thresholds that §6 does not name, and the image-region/corner split of D4
    needs the "central 70 %" rule from ``one_session_plan.md`` §2 to be made
    numeric.  Nothing in the second block changes a §6 default.
    """

    # ---- §6, verbatim ---------------------------------------------------
    distance_bands_mm: tuple[tuple[float, float], ...] = (
        (300.0, 380.0), (380.0, 460.0), (460.0, 560.0))
    min_views_per_distance_band: int = 3
    tilt_axes: str = "two"           # positive+negative out-of-plane, both axes
    min_tilt_deg: float = 15.0
    min_views_per_tilt_quadrant: int = 2
    roll_bins: int = 4               # in-plane orientation sectors
    min_views_per_roll_bin: int = 1
    image_regions: int = 9           # 3x3; centre visits >= 6, corners >= 1 each
    min_shared_markers: int = 12     # both IR cameras, unambiguous assignment
    min_colour_markers: int = 12
    #: colour registration is constrained by the views where the colour sensor
    #: actually sees the plate.  A corner pose can put the board outside the
    #: colour field of view while both IR cameras see it well; demanding colour
    #: in *every* view makes edge/corner coverage physically impossible.
    min_colour_views: int = 12
    min_colour_participation: float = 0.6
    require_colour: bool = True
    max_saturation_frac: float = 0.05
    min_focus: float = 150.0

    # ---- additive (D4 split, D7, D8); documented, not from §6 -----------
    min_image_region_views: int = 6          # §6: "plate centre must visit >= 6"
    min_corner_views: int = 1                # §6: "corners >= 1 each"
    min_quality_fraction: float = 0.95       # D7: accepted views inside limits
    min_stationary_views: int = 6            # D7: ~0.3 s dwell at >= 6 poses
    #: A dwell episode must be long enough to be a real hold, not one sample.
    #: One decision observation with a long dt only bounds *net* displacement;
    #: it is not evidence of a stationary interval (the raw 10 Hz trace of the
    #: real session showed 5-1800 mm/s inside such "stationary" decision gaps).
    min_dwell_s: float = 0.3
    min_dwell_observations: int = 2
    stationary_speed_mm_s: float = 3.0
    stationary_rate_deg_s: float = 1.5
    #: consecutive stationary observations closer than this are one dwell
    #: interval.  The 10 Hz raw stream is 0.1 s apart and the live decisions
    #: were 0.24-1.5 s apart; 1.0 s keeps a hold together without merging
    #: separate pauses.  This is a grouping rule, not a loosened threshold.
    stationarity_interval_gap_s: float = 1.0
    require_scale_artefact: bool = False     # D8: plan §2 calls it optional
    min_views_per_scale_artefact: int = 5    # plan §2: "imaged in >= 5 poses"
    min_scale_distance_bands: int = 2
    scale_tolerance_rel: float = 0.01        # plan §4: ~1 % scale question

    # -- helpers ----------------------------------------------------------
    def axis_tilt_required(self) -> bool:
        return self.tilt_axes.strip().lower() in ("two", "2", "both")

    def as_dict(self) -> dict:
        d = asdict(self)
        d["distance_bands_mm"] = [list(b) for b in self.distance_bands_mm]
        return d

    def band_index(self, distance_mm: float) -> int | None:
        for i, (lo, hi) in enumerate(self.distance_bands_mm):
            if lo <= distance_mm < hi:
                return i
        return None

    def roll_bin_size_deg(self) -> float:
        return 360.0 / max(int(self.roll_bins), 1)

    def roll_bin(self, roll_deg: float) -> int:
        return int((roll_deg % 360.0) // self.roll_bin_size_deg()) % max(int(self.roll_bins), 1)


# ---------------------------------------------------------------------------
# small geometry / view helpers
# ---------------------------------------------------------------------------

def _as_pose(pose: Any) -> tuple[np.ndarray, np.ndarray] | None:
    """Accept ``{"R":..,"t":..}``, ``(R, t)`` or ``None``."""
    if pose is None:
        return None
    if isinstance(pose, Mapping):
        R, t = pose.get("R"), pose.get("t")
    elif len(pose) == 2:
        R, t = pose
    else:
        return None
    if R is None or t is None:
        return None
    R = np.asarray(R, float).reshape(3, 3)
    t = np.asarray(t, float).reshape(3)
    if not np.all(np.isfinite(R)) or not np.all(np.isfinite(t)):
        return None
    return R, t


def plate_normal(R: np.ndarray) -> np.ndarray:
    n = np.asarray(R, float)[:, 2]
    return n / max(float(np.linalg.norm(n)), 1e-12)


def tilt_deg(R: np.ndarray) -> tuple[float, float]:
    """Out-of-plane tilt about the camera x and y axes (degrees)."""
    n = plate_normal(R)
    nz = max(abs(float(n[2])), 1e-12)
    return math.degrees(math.atan2(float(n[0]), nz)), math.degrees(math.atan2(float(n[1]), nz))


def roll_deg(R: np.ndarray) -> float:
    """In-plane roll in ``[0, 360)`` — rotation of the board about its normal.

    The board x axis is projected into the plate plane and its angle is
    measured against the projection of the camera x axis onto that plane.  This
    is the geometric in-plane rotation, so it is independent of the
    out-of-plane tilt (a bare ``atan2(R[1,0], R[0,0])`` is foreshortened by the
    tilt: 1.0 deg at 15 deg of tilt, 4.3 deg at 30 deg).  The sweep report's
    ``roll_deg`` uses the unprojected angle; on ``sweep-b`` the two agree to
    2.9 deg (180.9 vs 178.0) and always fall in the same 90 deg sector.
    """
    R = np.asarray(R, float)
    n = plate_normal(R)
    x = np.asarray(R[:, 0], float)
    for axis in (np.array([1.0, 0.0, 0.0]), np.array([0.0, 1.0, 0.0])):
        ref = axis - float(axis @ n) * n
        if float(np.linalg.norm(ref)) > 1e-6:
            ref = ref / np.linalg.norm(ref)
            break
    else:                                            # pragma: no cover - degenerate
        return 0.0
    e2 = np.cross(n, ref)
    return math.degrees(math.atan2(float(x @ e2), float(x @ ref))) % 360.0


def pose_metrics(view: Mapping[str, Any]) -> dict | None:
    """Distance, tilt (both axes), roll and normal of one accepted view."""
    pose = _as_pose(view.get("pose"))
    if pose is None:
        return None
    R, t = pose
    tx, ty = tilt_deg(R)
    return {"R": R, "t": t, "distance_mm": float(np.linalg.norm(t)),
            "depth_mm": float(t[2]), "tilt_x_deg": tx, "tilt_y_deg": ty,
            "tilt_deg": float(math.degrees(math.acos(
                min(1.0, max(-1.0, abs(float(plate_normal(R)[2]))))))),
            "roll_deg": roll_deg(R), "normal": plate_normal(R).tolist()}


def _image_point(view: Mapping[str, Any]) -> np.ndarray | None:
    """Plate centre proxy: centroid of the left-IR marker pixels.

    The centroid of the *visible* markers is biased towards the visible
    subset when the plate is clipped — which is exactly the situation D4 is
    about, so the bias is reported in ``detail`` rather than hidden.  The
    pose-free alternative (projecting the board origin) would need the
    calibration under test and is therefore not used here.
    """
    pts = view.get("image_points") or {}
    for cam in CAMERA_KEYS:
        arr = pts.get(cam)
        if arr is None:
            continue
        arr = np.asarray(arr, float).reshape(-1, 2)
        if len(arr):
            return arr.mean(axis=0)
    # a resumed session stores only the derived centre (see session_io)
    centre = view.get("plate_centre_px")
    if centre is not None:
        try:
            return np.asarray(centre, float).reshape(2)
        except (TypeError, ValueError):
            return None
    return None


def _n_markers(view: Mapping[str, Any], cam: str) -> int:
    nm = view.get("n_markers") or {}
    try:
        return int(nm.get(cam, 0) or 0)
    except (TypeError, ValueError):
        return 0


def _quality(view: Mapping[str, Any]) -> Mapping[str, Any]:
    q = view.get("quality") or {}
    return q if isinstance(q, Mapping) else {}


def _quality_metrics(view: Mapping[str, Any]) -> Mapping[str, Any]:
    metrics = _quality(view).get("metrics")
    return metrics if isinstance(metrics, Mapping) else {}


def _finite(value: Any) -> float | None:
    """A recorded number, or ``None``.  Booleans are not measurements."""
    if value is None or isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


#: The IR pair is what the D7 quality gate measures.  The colour camera has a
#: different exposure/focus scale (``DEFAULT_QUALITY`` uses focus 55 and
#: contrast 5 for ``tex`` against 150/14 for each IR camera) and its
#: participation is already D6.  It is reported, not silently folded into the
#: IR minimum: a tex failure must not be presented as an IR quality failure.
_IR_CAMERAS = ("left", "right")
_ALL_CAMERAS = ("left", "right", "tex")


def quality_observation(view: Mapping[str, Any],
                         spec: "CoverageSpec") -> dict:
    """Per-view quality from the metrics the detector actually recorded.

    Two layouts are accepted:

    * the live wizard's nested ``{"metrics": {"left.focus": ..., ...}}``
      (``guidance.evaluate_frame().as_dict()``), and
    * a legacy flat ``{"focus": ..., "saturation_frac": ...}`` recording.

    Missing measurements are *never* a pass.  ``quality.passed`` is reported
    as ``recorded_passed`` but is not itself used as evidence: the session in
    ``analysis/session03`` has ``passed=True`` on every view because the old
    D7 read keys that do not exist.
    """
    q = _quality(view)
    metrics = _quality_metrics(view)
    row: dict[str, Any] = {
        "view_id": view.get("view_id"),
        "quality_source": "missing",
        "active_cameras": [],
        "inactive_cameras": [],
        "focus_values": {}, "saturation_values": {}, "contrast_values": {},
        "focus_min": None, "focus_min_camera": None,
        "saturation_max": None, "saturation_max_camera": None,
        "contrast_min": None,
        "tex_active": None, "tex_focus": None, "tex_saturation": None,
        "tex_contrast": None, "tex_missing_metrics": [],
        "missing_metrics": [], "reasons": [], "ok": False,
        "recorded_passed": (bool(q.get("passed")) if q.get("passed") is not None
                            else None),
    }

    nested = any(key in metrics for cam in _ALL_CAMERAS
                 for key in (f"{cam}.focus", f"{cam}.saturation", f"{cam}.active"))
    if nested:
        active: dict[str, bool] = {}
        for cam in _ALL_CAMERAS:
            flag = metrics.get(f"{cam}.active")
            if flag is None:
                active[cam] = any(f"{cam}.{k}" in metrics
                                  for k in ("focus", "saturation", "contrast"))
            else:
                active[cam] = bool(flag)
        ir_active = [c for c in _IR_CAMERAS if active.get(c)]
        row["active_cameras"] = list(ir_active)
        row["inactive_cameras"] = [c for c in _IR_CAMERAS if not active.get(c)]
        row["quality_source"] = "nested_metrics"
        missing: list[str] = []
        if not ir_active:
            missing.append("active IR camera")
        for cam in ir_active:
            focus = _finite(metrics.get(f"{cam}.focus"))
            sat = _finite(metrics.get(f"{cam}.saturation"))
            contrast = _finite(metrics.get(f"{cam}.contrast"))
            if focus is None:
                missing.append(f"{cam}.focus")
            else:
                row["focus_values"][cam] = focus
            if sat is None:
                missing.append(f"{cam}.saturation")
            else:
                row["saturation_values"][cam] = sat
            if contrast is not None:
                row["contrast_values"][cam] = contrast
        if row["focus_values"]:
            cam = min(row["focus_values"], key=row["focus_values"].get)
            row["focus_min"] = row["focus_values"][cam]
            row["focus_min_camera"] = cam
        if row["saturation_values"]:
            cam = max(row["saturation_values"], key=row["saturation_values"].get)
            row["saturation_max"] = row["saturation_values"][cam]
            row["saturation_max_camera"] = cam
        if row["contrast_values"]:
            row["contrast_min"] = min(row["contrast_values"].values())
        # Colour metrics: reported for the UI but not part of the IR gate.
        row["tex_active"] = bool(active.get("tex"))
        if row["tex_active"]:
            row["tex_focus"] = _finite(metrics.get("tex.focus"))
            row["tex_saturation"] = _finite(metrics.get("tex.saturation"))
            row["tex_contrast"] = _finite(metrics.get("tex.contrast"))
            for key in ("tex.focus", "tex.saturation"):
                cam_key = key.split(".", 1)[1]
                if _finite(metrics.get(key)) is None:
                    row["tex_missing_metrics"].append(key)
        row["missing_metrics"] = missing
    else:
        # Legacy aggregate recording.  It describes the IR pair only; there is
        # no colour number in this layout.
        focus = _finite(q.get("focus"))
        sat = _finite(q.get("saturation_frac", q.get("sat_frac")))
        if focus is None and sat is None:
            row["quality_source"] = "missing"
            row["missing_metrics"] = ["focus", "saturation_frac"]
        else:
            row["quality_source"] = "legacy_flat"
            row["active_cameras"] = list(_IR_CAMERAS)   # aggregate IR pair
            if focus is not None:
                row["focus_values"]["ir"] = focus
                row["focus_min"] = focus
                row["focus_min_camera"] = "ir"
            else:
                row["missing_metrics"].append("focus")
            if sat is not None:
                row["saturation_values"]["ir"] = sat
                row["saturation_max"] = sat
                row["saturation_max_camera"] = "ir"
            else:
                row["missing_metrics"].append("saturation_frac")
        row["tex_active"] = None

    if row["focus_min"] is not None and row["focus_min"] < float(spec.min_focus):
        row["reasons"].append(
            f"focus {row['focus_min']:.0f} < {float(spec.min_focus):.0f} "
            f"({row['focus_min_camera']})")
    if row["saturation_max"] is not None and \
            row["saturation_max"] > float(spec.max_saturation_frac):
        row["reasons"].append(
            f"saturation {row['saturation_max']:.4f} > "
            f"{float(spec.max_saturation_frac):.4f} "
            f"({row['saturation_max_camera']})")
    if row["missing_metrics"]:
        row["reasons"].append("missing metrics: " + ", ".join(row["missing_metrics"]))
    if row["quality_source"] == "missing" and row["recorded_passed"] is True:
        row["reasons"].append("recorded quality.passed=true but no metrics recorded")
    row["ok"] = bool(not row["missing_metrics"]
                     and row["focus_min"] is not None
                     and row["saturation_max"] is not None
                     and row["focus_min"] >= float(spec.min_focus)
                     and row["saturation_max"] <= float(spec.max_saturation_frac))
    return row


def stationarity_observation(view: Mapping[str, Any],
                             spec: "CoverageSpec") -> dict:
    """Per-view short-term stationarity from recorded motion, never from poses.

    Precedence:

    1. an explicit ``stationary`` boolean (the controller's own per-view
       observation), optionally cross-checked against recorded speed/rate;
    2. recorded short-term motion fields, either at the top level
       (``stationarity_speed_mm_s`` / ``stationarity_rate_deg_s`` /
       ``stationarity_window_s``) or inside ``quality.metrics``
       (``speed_mm_s``, ``motion_deg``, ``dt_s``, ``motion_mm``);
    3. ``stationarity_source="missing"`` -- which is a *failure* to establish
       stationarity, never a pass.

    Pose deltas between accepted views are deliberately not consulted: accepted
    views are sparse and biased towards motion, and duplicate dwell frames are
    exactly what the live controller currently rejects as redundant.
    """
    metrics = _quality_metrics(view)
    spp = float(spec.stationary_speed_mm_s)
    rpp = float(spec.stationary_rate_deg_s)
    window = _finite(view.get("stationarity_window_s"))
    if window is None:
        window = _finite(metrics.get("dt_s"))
    speed = _finite(view.get("stationarity_speed_mm_s"))
    if speed is None:
        speed = _finite(metrics.get("speed_mm_s"))
    if speed is None:
        motion_mm = _finite(metrics.get("motion_mm"))
        if motion_mm is not None and window and window > 0:
            speed = motion_mm / window
    rate = _finite(view.get("stationarity_rate_deg_s"))
    if rate is None:
        rate = _finite(metrics.get("rate_deg_s"))
    if rate is None:
        motion_deg = _finite(metrics.get("motion_deg"))
        if motion_deg is not None and window and window > 0:
            rate = motion_deg / window
    blur = _finite(view.get("motion_blur_px"))
    if blur is None:
        blur = _finite(metrics.get("motion_blur_px"))

    explicit_raw = view.get("stationary")
    if explicit_raw is None:
        explicit_raw = metrics.get("stationary")   # guidance.evaluate_frame
    explicit = (bool(explicit_raw) if explicit_raw is not None else None)
    reasons: list[str] = []
    missing: list[str] = []
    if explicit is not None:
        source = "explicit"
        ok = explicit
        if not ok:
            reasons.append("explicit stationary=false")
        else:
            if speed is not None and speed > spp:
                ok = False
                reasons.append(f"explicit stationary contradicted by speed "
                               f"{speed:.2f} > {spp:.1f} mm/s")
            if rate is not None and rate > rpp:
                ok = False
                reasons.append(f"explicit stationary contradicted by rotation "
                               f"{rate:.2f} > {rpp:.1f} deg/s")
    elif speed is not None or rate is not None:
        source = "recorded_motion"
        if speed is None:
            missing.append("stationarity_speed_mm_s")
        if rate is None:
            missing.append("stationarity_rate_deg_s")
        ok = not missing and speed <= spp and rate <= rpp
        if missing:
            reasons.append("missing motion field(s): " + ", ".join(missing))
        if speed is not None and speed > spp:
            reasons.append(f"moving at {speed:.2f} mm/s > {spp:.1f}")
        if rate is not None and rate > rpp:
            reasons.append(f"rotating at {rate:.2f} deg/s > {rpp:.1f}")
    else:
        source = "missing"
        ok = False
        missing = ["stationarity_speed_mm_s", "stationarity_rate_deg_s"]
        reasons.append("no per-view stationarity observation recorded")

    return {
        "view_id": view.get("view_id"),
        "group": view.get("group"),
        "timestamp": _timestamp(view),
        "stationary": bool(ok),
        "stationarity_still": bool(view.get("stationarity_still")),
        "stationarity_source": source,
        "reported_stationarity_source": view.get("stationarity_source"),
        "stationarity_speed_mm_s": speed,
        "stationarity_rate_deg_s": rate,
        "stationarity_window_s": window,
        "motion_blur_px": blur,
        "missing_metrics": missing,
        "reasons": reasons,
    }


def stationary_intervals(rows: Sequence[Mapping[str, Any]],
                         gap_s: float = 1.0,
                         min_dwell_s: float = 0.3,
                         min_observations: int = 2) -> list[dict]:
    """Group consecutive stationary observations into distinct dwell episodes.

    A dwell episode is a run of observations whose recorded short-term motion
    is below the D7 thresholds.  It ends at a moving observation or at a time
    gap larger than ``gap_s``.  Counting *episodes*, not frames, is what keeps
    98 consecutive 10 Hz frames of one pause from pretending to be 98 poses.
    Rows without timestamps are treated as consecutive.
    """
    ordered = sorted(rows,
                     key=lambda r: (_timestamp(r) is None,
                                    _timestamp(r) or 0.0))
    intervals: list[dict] = []
    current: list[Mapping[str, Any]] = []
    prev_ts: float | None = None

    def flush() -> None:
        nonlocal current, prev_ts
        if not current:
            return
        speeds = [_finite(r.get("stationarity_speed_mm_s")) for r in current]
        rates = [_finite(r.get("stationarity_rate_deg_s")) for r in current]
        speeds = [x for x in speeds if x is not None]
        rates = [x for x in rates if x is not None]
        stamps = [_timestamp(r) for r in current]
        stamps = [x for x in stamps if x is not None]
        span = (max(stamps) - min(stamps)) if len(stamps) >= 2 else 0.0
        windows = [_finite(r.get("stationarity_window_s")) for r in current]
        windows = [x for x in windows if x is not None and x > 0]
        if not windows and len(stamps) >= 2:
            windows = list(np.diff(np.asarray(stamps, float)))
            windows = [float(x) for x in windows if x > 0]
        step = float(np.median(windows)) if windows else 0.0
        duration = (span + step) if len(stamps) >= 2 else 0.0
        qualified = bool(len(current) >= int(min_observations)
                         and duration + 1e-9 >= float(min_dwell_s))
        intervals.append({
            "n_observations": len(current),
            "start_timestamp": _timestamp(current[0]),
            "end_timestamp": _timestamp(current[-1]),
            "start_group": current[0].get("group"),
            "end_group": current[-1].get("group"),
            "duration_s": duration,
            "qualified": qualified,
            "speed_p50_mm_s": (float(np.median(speeds)) if speeds else None),
            "rate_p50_deg_s": (float(np.median(rates)) if rates else None),
        })
        current = []
        prev_ts = None

    for row in ordered:
        ts = _timestamp(row)
        if row.get("stationarity_still") and row.get("stationary"):
            # A still-collection pose is a deliberate stationary capture on
            # its own: one image per pose, no consecutive cadence.  Count each
            # as one qualified episode of the nominal dwell length.
            flush()
            speeds = [_finite(row.get("stationarity_speed_mm_s"))]
            rates = [_finite(row.get("stationarity_rate_deg_s"))]
            intervals.append({
                "n_observations": 1,
                "start_timestamp": ts, "end_timestamp": ts,
                "start_group": row.get("group"), "end_group": row.get("group"),
                "duration_s": float(min_dwell_s),
                "qualified": True,
                "speed_p50_mm_s": speeds[0], "rate_p50_deg_s": rates[0],
            })
            prev_ts = None
            continue
        if not row.get("stationary"):
            flush()
            prev_ts = None
            continue
        if current and prev_ts is not None and ts is not None \
                and ts - prev_ts > float(gap_s):
            flush()
        current.append(row)
        prev_ts = ts
    flush()
    return intervals


def _is_dwell(view: Mapping[str, Any]) -> bool:
    """A stationary duplicate kept as coverage evidence, not as a solver view.

    The live controller currently rejects these as redundant, which is why a
    real 297-view session has zero dwell views.  The contract is explicit:
    ``dwell=True`` views count for D7 quality/stationarity and are excluded
    from the pose-coverage dimensions (D1-D6) so duplicate poses cannot
    manufacture distance/tilt/region counts.
    """
    flag = view.get("dwell")
    return bool(flag) if isinstance(flag, (bool, int, float)) else False


def _timestamp(view: Mapping[str, Any]) -> float | None:
    ts = view.get("timestamp")
    if ts is None:
        ts = view.get("timestamp_s")       # decisions.jsonl spelling
    try:
        return float(ts) if ts is not None else None
    except (TypeError, ValueError):
        return None


#: number of views whose translation/rotation differ from the plate origin
#: (used by the guidance effort model)
def _rotation_angle_deg(Ra: np.ndarray, Rb: np.ndarray) -> float:
    dR = np.asarray(Rb, float) @ np.asarray(Ra, float).T
    c = (float(np.trace(dR)) - 1.0) / 2.0
    return math.degrees(math.acos(min(1.0, max(-1.0, c))))


def pose_effort(current: tuple[np.ndarray, np.ndarray],
                target: tuple[np.ndarray, np.ndarray]) -> dict:
    """Dimensionless operator effort to move between two board poses.

    Same weights as :func:`calib.objective.pose_effort` (150 mm and 25° are
    one effort unit, base cost 0.15) so the wizard's guidance and the
    task-space view scorer rank motion consistently.
    """
    Rc, tc = current
    Rt, tt = target
    Cc = -np.asarray(Rc, float).T @ np.asarray(tc, float)
    Ct = -np.asarray(Rt, float).T @ np.asarray(tt, float)
    dcam = np.asarray(Rc, float) @ (Ct - Cc)
    lateral = float(np.linalg.norm(dcam[:2]))
    depth = abs(float(dcam[2]))
    ang = _rotation_angle_deg(Rc, Rt)
    return {"score": 0.15 + (lateral + depth) / 150.0 + ang / 25.0,
            "lateral_mm": lateral, "depth_mm": depth, "rotation_deg": ang,
            "move_mm": (Ct - Cc).tolist()}


def family_pose(distance_mm: float, tilt_x_deg: float, tilt_y_deg: float,
                roll_deg: float = 0.0,
                lateral_mm: tuple[float, float] = (0.0, 0.0)
                ) -> tuple[np.ndarray, np.ndarray]:
    """Board pose whose :func:`tilt_deg` / :func:`roll_deg` are the arguments.

    The frame is built from the normal itself, so the round-trip is exact:

    ``n = normalise([tan(tilt_x), tan(tilt_y), -1])``  (plate faces the camera)
    ``x_board = cos(roll) * u + sin(roll) * (n x u)`` with ``u`` the projection
    of the camera x axis onto the plate plane.

    At ``(0, 0, 0)`` this is exactly ``diag(1, -1, -1)``, the same board frame
    ``calib.adaptive.board_pose`` uses.  Verified in
    ``tests/test_wizard_coverage.py::test_family_pose_roundtrip``.
    """
    n = np.array([math.tan(math.radians(tilt_x_deg)),
                  math.tan(math.radians(tilt_y_deg)), -1.0], float)
    nn = float(np.linalg.norm(n))
    if nn < 1e-12:                                 # pragma: no cover - degenerate
        n = np.array([0.0, 0.0, -1.0])
    else:
        n = n / nn
    for axis in (np.array([1.0, 0.0, 0.0]), np.array([0.0, 1.0, 0.0])):
        ref = axis - float(axis @ n) * n
        if float(np.linalg.norm(ref)) > 1e-9:
            ref = ref / np.linalg.norm(ref)
            break
    e2 = np.cross(n, ref)
    c = math.cos(math.radians(roll_deg))
    s = math.sin(math.radians(roll_deg))
    xa = c * ref + s * e2
    xa = xa / np.linalg.norm(xa)
    ya = np.cross(n, xa)
    R = np.column_stack([xa, ya, n])
    t = np.array([lateral_mm[0], lateral_mm[1], distance_mm], float)
    return R, t


# ---------------------------------------------------------------------------
# the measurement
# ---------------------------------------------------------------------------

def _check(label: str, required: Any, measured: Any, satisfied: bool,
           deficit: Any, hint: str) -> dict:
    return {"label": label, "required": required, "measured": measured,
            "satisfied": bool(satisfied), "deficit": deficit, "hint": hint}


def _fmt_bands(bands: Sequence[Sequence[float]]) -> str:
    return ", ".join(f"{lo:.0f}-{hi:.0f}" for lo, hi in bands)


def measure(accepted_views: Sequence[Mapping[str, Any]],
            spec: CoverageSpec | None = None,
            *, stationarity_observations: Sequence[Mapping[str, Any]] | None = None,
            tilt_center_deg: tuple[float, float] | None = None,
            latched_checks: set[str] | None = None
            ) -> dict:
    """Measure D1-D8 over the accepted views.

    Parameters
    ----------
    accepted_views:
        Sequence of dicts with ``view_id``, ``pose`` (R 3x3, t in mm),
        ``n_markers``, ``quality``, ``image_points``, ``shared_markers_ir``,
        ``assignment_ok``, ``timestamp``.  Missing optional keys degrade the
        relevant dimension instead of raising.  Views flagged ``dwell=True``
        count only for D7 (they are duplicate stationary evidence) and are
        excluded from the D1-D6 pose counts.
    spec:
        :class:`CoverageSpec`; the defaults are the architecture §6 values.
    tilt_center_deg:
        Optional fixed tilt centre (degrees).  By default the centre is the
        midrange of the *current* accepted tilt excursion, which can move as
        views are added.  Callers that require a frozen convention can pass the
        centre explicitly; the live wizard currently uses the dynamic centre
        (freezing it after eight mixed views regressed a validated real-image
        rehearsal from 8/8 to 7/8, because those first views were not neutral).
    latched_checks:
        Tilt checks that were satisfied earlier in this session.  The gate
        centres the tilt excursion on a statistic that moves as views are
        added, so a check can flip satisfied -> unsatisfied without any evidence
        being removed.  A live session latches them (accepted evidence is
        additive); the final validation does not, so a genuinely unsatisfied
        final state is still refused.
    stationarity_observations:
        The session-level observation stream: every ingested group/raw frame,
        accepted or rejected, with its short-term motion fields.  D7
        stationarity is measured here (distinct dwell intervals), because the
        accepted set is sparse and dwell duplicates are rejected as redundant.
        If omitted, stationarity is reported ``unestablished`` -- never as a
        silent pass or a default fail; the accepted-view stationarity count is
        still reported separately.

    Returns
    -------
    dict
        ``{"n_views", "n_usable", "n_unusable", "spec", "D1".."D8",
        "checks", "pose_stats", "stability"}``.  Every ``D*`` entry is
        ``{"id", "label", "required", "measured", "satisfied", "deficit",
        "hint", "detail", "checks"}``; ``checks`` is the flat map used by
        :func:`readiness` and :func:`guidance_target`.
    """
    spec = spec or CoverageSpec()
    views = list(accepted_views or [])
    all_metrics: list[dict] = []
    all_view_of: list[Mapping[str, Any]] = []
    unusable: list[dict] = []
    dwell_flags: list[bool] = []
    for v in views:
        dwell_flags.append(_is_dwell(v))
        m = pose_metrics(v)
        if m is None:
            unusable.append({"view_id": v.get("view_id"), "reason": "no usable pose"})
            continue
        all_metrics.append(m)
        all_view_of.append(v)

    # D1-D6 are pose-coverage dimensions: a duplicate dwell view must not be
    # able to manufacture a distance band / region / tilt count.  D7 is a
    # per-observation quality dimension and does count those views.
    pose_idx = [i for i, v in enumerate(all_view_of) if not _is_dwell(v)]
    metrics = [all_metrics[i] for i in pose_idx]
    view_of = [all_view_of[i] for i in pose_idx]
    n = len(metrics)
    n_all = len(views)
    n_dwell = int(sum(dwell_flags))
    image_size = _DEFAULT_IMAGE
    checks: dict[str, dict] = {}
    dims: dict[str, dict] = {}

    # ---- D1 distance bands ---------------------------------------------
    band_counts = [0] * len(spec.distance_bands_mm)
    band_index: list[int | None] = []
    for m in metrics:
        b = spec.band_index(m["distance_mm"])
        band_index.append(b)
        if b is not None:
            band_counts[b] += 1
    missing = [i for i, c in enumerate(band_counts) if c < spec.min_views_per_distance_band]
    d1_sat = n > 0 and not missing
    d1_deficit = {f"band_{i}": spec.min_views_per_distance_band - band_counts[i]
                  for i in missing}
    d1_hint = ("Distance coverage complete: " + _fmt_bands(spec.distance_bands_mm) + " mm."
               if d1_sat else _distance_hint(spec, band_counts, metrics))

    checks["distance_bands"] = _check(
        "distance bands",
        {"bands_mm": [list(b) for b in spec.distance_bands_mm],
         "min_views_per_band": spec.min_views_per_distance_band},
        {"band_counts": band_counts, "bands_mm": [list(b) for b in spec.distance_bands_mm],
         "distance_mm": _range(metrics, "distance_mm")},
        d1_sat, d1_deficit, d1_hint)
    dims["D1"] = {"id": "D1", "label": "distance bands",
                  "required": checks["distance_bands"]["required"],
                  "measured": checks["distance_bands"]["measured"],
                  "satisfied": d1_sat, "deficit": d1_deficit, "hint": d1_hint,
                  "detail": {"band_counts": band_counts,
                             "band_index_of_view": band_index},
                  "checks": ["distance_bands"]}

    # ---- D2 two-axis tilt ----------------------------------------------
    # Tilt signs are counted *relative to the centre of the observed excursion*
    # (the midrange), because the scanner may be mounted at an angle to the
    # plate and that fixed offset is absorbed by the board pose.  The midrange
    # rather than the median: a symmetric ±18° design has a median of +18° when
    # one sign has one view more, which would make the excursion look
    # one-sided.  Absolute ranges are still reported.
    tx_raw = np.array([m["tilt_x_deg"] for m in metrics], float)
    ty_raw = np.array([m["tilt_y_deg"] for m in metrics], float)
    if tilt_center_deg is not None:
        try:
            med_x = float(tilt_center_deg[0])
            med_y = float(tilt_center_deg[1])
            if not (np.isfinite(med_x) and np.isfinite(med_y)):
                med_x = _midrange(tx_raw)
                med_y = _midrange(ty_raw)
        except (TypeError, ValueError, IndexError):
            med_x = _midrange(tx_raw)
            med_y = _midrange(ty_raw)
    else:
        med_x = _midrange(tx_raw)
        med_y = _midrange(ty_raw)
    tx = tx_raw - med_x
    ty = ty_raw - med_y
    thr = float(spec.min_tilt_deg)
    ax_pos = int(np.sum(tx >= thr)) if n else 0
    ax_neg = int(np.sum(tx <= -thr)) if n else 0
    ay_pos = int(np.sum(ty >= thr)) if n else 0
    ay_neg = int(np.sum(ty <= -thr)) if n else 0
    need = int(spec.min_views_per_tilt_quadrant)
    two_axes = spec.axis_tilt_required()

    x_sat = ax_pos >= need and ax_neg >= need
    y_sat = ay_pos >= need and ay_neg >= need
    if not two_axes:
        x_sat = x_sat or (ax_pos + ax_neg) >= need
        y_sat = y_sat or (ay_pos + ay_neg) >= need
    quad_counts = {"+x+y": int(np.sum((tx >= thr) & (ty >= thr))),
                   "+x-y": int(np.sum((tx >= thr) & (ty <= -thr))),
                   "-x+y": int(np.sum((tx <= -thr) & (ty >= thr))),
                   "-x-y": int(np.sum((tx <= -thr) & (ty <= -thr)))}
    quad_ok = all(c >= need for c in quad_counts.values())
    d2_sat = x_sat and y_sat and (quad_ok or not two_axes)
    d2_deficit = {"axis_x": {"positive": max(0, need - ax_pos),
                             "negative": max(0, need - ax_neg)},
                  "axis_y": {"positive": max(0, need - ay_pos),
                             "negative": max(0, need - ay_neg)},
                  "quadrants": {k: max(0, need - c) for k, c in quad_counts.items()}}
    d2_hint = _tilt_hint(spec, ax_pos, ax_neg, ay_pos, ay_neg, tx, ty,
                         med_x, med_y, tx_raw, ty_raw)

    checks["tilt_axis_x"] = _check(
        "tilt about the x axis (positive and negative)",
        {"min_tilt_deg": thr, "min_views_per_sign": need,
         "relative_to": "centre of the session tilt excursion"},
        {"positive": ax_pos, "negative": ax_neg,
         "tilt_centre_deg": med_x,
         "centred_range_deg": _range_of(tx),
         "absolute_range_deg": _range_of(tx_raw)},
        x_sat,
        {"positive": max(0, need - ax_pos), "negative": max(0, need - ax_neg)},
        _axis_hint("the camera x axis", ax_pos, ax_neg, need, thr, med_x))
    checks["tilt_axis_y"] = _check(
        "tilt about the second axis (y, positive and negative)",
        {"min_tilt_deg": thr, "min_views_per_sign": need,
         "relative_to": "centre of the session tilt excursion"},
        {"positive": ay_pos, "negative": ay_neg,
         "tilt_centre_deg": med_y,
         "centred_range_deg": _range_of(ty),
         "absolute_range_deg": _range_of(ty_raw)},
        y_sat,
        {"positive": max(0, need - ay_pos), "negative": max(0, need - ay_neg)},
        _axis_hint("the second axis (camera y)", ay_pos, ay_neg, need, thr, med_y))
    checks["tilt_quadrants"] = _check(
        "tilt quadrants (four sign combinations)",
        {"min_views_per_quadrant": need,
         "quadrants": ["+x+y", "+x-y", "-x+y", "-x-y"]},
        quad_counts, quad_ok,
        {k: max(0, need - c) for k, c in quad_counts.items()},
        ("All four tilt quadrants visited." if quad_ok else
         "Tilt the plate the other way as well: "
         + ", ".join(f"{k} needs {max(0, need - c)} more"
                     for k, c in quad_counts.items() if c < need)
         + f" (>= {thr:.0f} deg from the centre of the tilt excursion)."))

    dims["D2"] = {"id": "D2", "label": "out-of-plane tilt about two axes",
                  "required": {"min_tilt_deg": thr, "min_views_per_sign": need,
                               "axes": spec.tilt_axes},
                  "measured": {"tilt_x": {"positive": ax_pos, "negative": ax_neg,
                                          "centre_deg": med_x},
                               "tilt_y": {"positive": ay_pos, "negative": ay_neg,
                                          "centre_deg": med_y},
                               "quadrants": quad_counts,
                               "tilt_x_deg_range": _range(metrics, "tilt_x_deg"),
                               "tilt_y_deg_range": _range(metrics, "tilt_y_deg")},
                  "satisfied": d2_sat, "deficit": d2_deficit, "hint": d2_hint,
                  "detail": {"tilt_x_deg": tx_raw.tolist(),
                             "tilt_y_deg": ty_raw.tolist(),
                             "tilt_x_centred_deg": tx.tolist(),
                             "tilt_y_centred_deg": ty.tolist(),
                             "normalised_axis_gap": _axis_gap(tx, ty)},
                  "checks": ["tilt_axis_x", "tilt_axis_y", "tilt_quadrants"]}

    # ---- D3 in-plane roll sectors --------------------------------------
    nb = max(int(spec.roll_bins), 1)
    roll_bins = np.array([spec.roll_bin(m["roll_deg"]) for m in metrics], int) if n \
        else np.zeros(0, int)
    rcounts = [int(np.sum(roll_bins == i)) for i in range(nb)]
    roll_missing = [i for i, c in enumerate(rcounts) if c < spec.min_views_per_roll_bin]
    d3_sat = n > 0 and not roll_missing
    d3_deficit = {f"bin_{i}": spec.min_views_per_roll_bin - rcounts[i] for i in roll_missing}
    d3_hint = ("All four in-plane roll sectors visited." if d3_sat else
               f"Rotate the plate in its own plane: {len(roll_missing)} of {nb} "
               f"sectors unvisited ({rcounts}); roll currently "
               f"{np.min([m['roll_deg'] for m in metrics]) if n else float('nan'):.1f}-"
               f"{np.max([m['roll_deg'] for m in metrics]) if n else float('nan'):.1f} deg, "
               f"need {nb} sectors of {spec.roll_bin_size_deg():.0f} deg.")
    checks["roll_sectors"] = _check(
        "in-plane roll sectors",
        {"bins": nb, "min_views_per_bin": spec.min_views_per_roll_bin,
         "bin_size_deg": spec.roll_bin_size_deg()},
        {"bin_counts": rcounts,
         "roll_deg_range": _range(metrics, "roll_deg"),
         "n_bins_covered": int(sum(1 for c in rcounts if c > 0))},
        d3_sat, d3_deficit, d3_hint)
    dims["D3"] = {"id": "D3", "label": "in-plane roll sectors",
                  "required": checks["roll_sectors"]["required"],
                  "measured": checks["roll_sectors"]["measured"],
                  "satisfied": d3_sat, "deficit": d3_deficit, "hint": d3_hint,
                  "detail": {"roll_deg": [m["roll_deg"] for m in metrics],
                             "bin_counts": rcounts},
                  "checks": ["roll_sectors"]}

    # ---- D4 image-plane regions + corners ------------------------------
    centres = [_image_point(v) for v in view_of]
    W, H = image_size
    ncol = nrow = 3
    region_counts = [0] * (nrow * ncol)
    used = 0
    for c in centres:
        if c is None:
            continue
        col = min(ncol - 1, max(0, int(c[0] / (W / ncol))))
        row = min(nrow - 1, max(0, int(c[1] / (H / nrow))))
        region_counts[row * ncol + col] += 1
        used += 1
    visited = int(sum(1 for c in region_counts if c > 0))
    corners = {"top_left": region_counts[0], "top_right": region_counts[2],
               "bottom_left": region_counts[6], "bottom_right": region_counts[8]}
    corners_ok = all(c >= spec.min_corner_views for c in corners.values())
    regions_ok = visited >= min(int(spec.min_image_region_views), nrow * ncol)
    d4_sat = used > 0 and regions_ok and corners_ok
    d4_deficit = {"regions": max(0, int(spec.min_image_region_views) - visited),
                  "corners": {k: max(0, spec.min_corner_views - c)
                              for k, c in corners.items() if c < spec.min_corner_views}}
    d4_hint = ("Image-plane coverage complete (all corners visited)." if d4_sat else
               _region_hint(region_counts, corners, spec, W, H))
    checks["image_regions"] = _check(
        "image-plane 3x3 regions",
        {"regions": int(spec.min_image_region_views), "grid": [nrow, ncol]},
        {"regions_visited": visited, "region_counts": region_counts,
         "centres_px": [None if c is None else c.tolist() for c in centres]},
        regions_ok, {"regions": max(0, int(spec.min_image_region_views) - visited)},
        d4_hint)
    checks["image_corners"] = _check(
        "image corners",
        {"min_views_per_corner": spec.min_corner_views,
         "corners": list(corners)},
        {"corners": corners},
        corners_ok,
        {k: max(0, spec.min_corner_views - c)
         for k, c in corners.items() if c < spec.min_corner_views},
        ("All four image corners visited." if corners_ok else
         "Aim the plate at the image corners: "
         + ", ".join(f"{k} (needs {max(0, spec.min_corner_views - c)} more)"
                     for k, c in corners.items() if c < spec.min_corner_views)
         + "."))
    dims["D4"] = {"id": "D4", "label": "image-plane regions and corners",
                  "required": {"regions": int(spec.min_image_region_views),
                               "corners": spec.min_corner_views, "grid": [nrow, ncol]},
                  "measured": {"regions_visited": visited,
                               "region_counts": region_counts, "corners": corners},
                  "satisfied": d4_sat, "deficit": d4_deficit, "hint": d4_hint,
                  "detail": {"plate_centre_px": [None if c is None else c.tolist()
                                                 for c in centres],
                             "image_size": [W, H],
                             "centre_source": "centroid of detected left-IR markers",
                             "centre_bias_note": ("clipped views bias the centroid "
                                                  "towards the visible markers")},
                  "checks": ["image_regions", "image_corners"]}

    # ---- D5 stereo overlap ---------------------------------------------
    shared = []
    assign_bad = []
    for v in view_of:
        try:
            s = int(v.get("shared_markers_ir", 0) or 0)
        except (TypeError, ValueError):
            s = 0
        shared.append(s)
        if v.get("assignment_ok", True) is False:
            assign_bad.append(v.get("view_id"))
    shared_arr = np.array(shared, int) if shared else np.zeros(0, int)
    n_shared_ok = int(np.sum(shared_arr >= int(spec.min_shared_markers)))
    d5_sat = n > 0 and n_shared_ok == n and not assign_bad
    d5_deficit = {"views_below_min_shared": max(0, (n - n_shared_ok)),
                  "ambiguous_assignment": len(assign_bad)}
    d5_hint = ("Stereo overlap complete: every view shares >= "
               f"{spec.min_shared_markers} IR markers with an unambiguous "
               "orientation witness." if d5_sat else
               f"Stereo overlap: {n - n_shared_ok} of {n} views have fewer than "
               f"{spec.min_shared_markers} shared IR markers"
               + (f", {len(assign_bad)} have an ambiguous label assignment" if assign_bad else "")
               + "; move the plate fully into both IR fields of view.")
    checks["stereo_overlap"] = _check(
        "stereo overlap (shared IR markers)",
        {"min_shared_markers": int(spec.min_shared_markers),
         "min_ir_markers_per_camera": int(spec.min_shared_markers),
         "assignment_ok_required": True},
        {"min_shared": int(shared_arr.min()) if len(shared_arr) else 0,
         "median_shared": float(np.median(shared_arr)) if len(shared_arr) else 0.0,
         "n_shared_ok": n_shared_ok, "n_views": n,
         "ambiguous_assignment": len(assign_bad),
         "n_markers_left_min": min([_n_markers(v, "left") for v in view_of], default=0),
         "n_markers_right_min": min([_n_markers(v, "right") for v in view_of], default=0)},
        d5_sat, d5_deficit, d5_hint)
    dims["D5"] = {"id": "D5", "label": "stereo overlap",
                  "required": checks["stereo_overlap"]["required"],
                  "measured": checks["stereo_overlap"]["measured"],
                  "satisfied": d5_sat, "deficit": d5_deficit, "hint": d5_hint,
                  "detail": {"shared_markers_ir": shared,
                             "assignment_bad_view_ids": assign_bad},
                  "checks": ["stereo_overlap"]}

    # ---- D6 colour participation ---------------------------------------
    colour_counts = [_n_markers(v, "tex") for v in view_of]
    colour_ok_views = [i for i, c in enumerate(colour_counts)
                       if c >= int(spec.min_colour_markers)]
    colour_fraction = (len(colour_ok_views) / n) if n else 0.0
    d6_required = bool(spec.require_colour)
    need_views = int(spec.min_colour_views)
    need_frac = float(spec.min_colour_participation)
    d6_sat = (True if not d6_required else
              (len(colour_ok_views) >= need_views and colour_fraction >= need_frac))
    d6_deficit = ({} if not d6_required else
                  {"views_with_colour": max(0, need_views - len(colour_ok_views)),
                   "participation_fraction": max(0.0, need_frac - colour_fraction)})
    if not d6_required:
        d6_hint = "Colour participation is not required in this spec."
    elif d6_sat:
        d6_hint = (f"Colour participates in {len(colour_ok_views)} of {n} views "
                   f"({colour_fraction * 100:.0f} %).")
    else:
        d6_hint = (f"Colour: only {len(colour_ok_views)} of {n} views have "
                   f">= {spec.min_colour_markers} colour markers "
                   f"(need {need_views} views and {need_frac * 100:.0f} %); keep "
                   "the plate inside the colour field of view for some poses.")
    checks["colour"] = _check(
        "colour participation",
        {"min_colour_markers": int(spec.min_colour_markers),
         "min_colour_views": need_views,
         "min_colour_participation": need_frac,
         "require_colour": d6_required},
        {"n_views_with_colour": len(colour_ok_views), "n_views": n,
         "participation_fraction": colour_fraction,
         "min_colour_markers_seen": int(min(colour_counts)) if colour_counts else 0,
         "max_colour_markers_seen": int(max(colour_counts)) if colour_counts else 0},
        d6_sat, d6_deficit, d6_hint)
    dims["D6"] = {"id": "D6", "label": "colour participation",
                  "required": checks["colour"]["required"],
                  "measured": checks["colour"]["measured"],
                  "satisfied": d6_sat, "deficit": d6_deficit, "hint": d6_hint,
                  "detail": {"colour_markers": colour_counts},
                  "checks": ["colour"]}

    # ---- D7 stationarity / quality -------------------------------------
    q_rows = [quality_observation(v, spec) for v in views]
    s_rows = [stationarity_observation(v, spec) for v in views]
    # D7 stationarity is an acquisition-quality property of the *stream*, not
    # of the accepted pose set.  Accepted views are sparse and biased towards
    # motion (duplicate dwell frames are rejected as redundant), so a caller
    # must supply every ingested observation's motion fields explicitly.
    stream_supplied = stationarity_observations is not None
    stream_rows = ([stationarity_observation(o, spec)
                    for o in (stationarity_observations or [])]
                   if stream_supplied else [])
    accepted_intervals = stationary_intervals(
        s_rows, float(spec.stationarity_interval_gap_s),
        float(spec.min_dwell_s), int(spec.min_dwell_observations))
    stream_intervals = (stationary_intervals(
        stream_rows, float(spec.stationarity_interval_gap_s),
        float(spec.min_dwell_s), int(spec.min_dwell_observations))
        if stream_supplied else [])
    accepted_stationary = int(sum(1 for r in s_rows if r["stationary"]))
    stream_stationary = int(sum(1 for r in stream_rows if r["stationary"]))
    if not stream_supplied:
        stationarity_status = "unestablished"
    elif any(r["stationarity_source"] != "missing" for r in stream_rows):
        stationarity_status = "stream"
    else:
        stationarity_status = "unestablished"
    stationarity_established = stationarity_status == "stream"
    n_q_ok = int(sum(1 for r in q_rows if r["ok"]))
    q_frac = (n_q_ok / n_all) if n_all else 0.0
    quality_ok = n_all > 0 and q_frac >= float(spec.min_quality_fraction)
    quality_sources: dict[str, int] = {}
    for r in q_rows:
        quality_sources[r["quality_source"]] = quality_sources.get(r["quality_source"], 0) + 1
    missing_quality_metrics: list[str] = []
    for r in q_rows:
        for key in r["missing_metrics"]:
            if key not in missing_quality_metrics:
                missing_quality_metrics.append(key)
    focus_vals = [r["focus_min"] for r in q_rows if r["focus_min"] is not None]
    sat_vals = [r["saturation_max"] for r in q_rows if r["saturation_max"] is not None]
    focus_min = float(min(focus_vals)) if focus_vals else None
    sat_max = float(max(sat_vals)) if sat_vals else None
    quality_measured = {
        "n_quality_ok": n_q_ok, "quality_fraction": q_frac, "n_views": n_all,
        "focus_min": focus_min, "saturation_max": sat_max,
        "quality_source_counts": quality_sources,
        "missing_metrics": missing_quality_metrics,
        "n_views_missing_metrics": int(sum(1 for r in q_rows if r["missing_metrics"])),
        "tex_note": ("tex focus/saturation are reported per view but not gated by "
                     "D7: its scale differs (tex focus >= 55, contrast >= 5) and "
                     "colour participation is D6"),
    }
    quality_deficit = {
        "quality_views": max(0, int(math.ceil(float(spec.min_quality_fraction) * n_all))
                             - n_q_ok),
        "missing_quality_metrics": len(missing_quality_metrics),
    }
    if quality_ok:
        q_hint = (f"Quality: {n_q_ok}/{n_all} views within focus >= "
                  f"{float(spec.min_focus):.0f} and saturation <= "
                  f"{float(spec.max_saturation_frac):.3f}"
                  + (f" (focus min {focus_min:.0f}, saturation max {sat_max:.4f})"
                     if focus_min is not None and sat_max is not None else "")
                  + ".")
    else:
        q_hint = (f"Quality: {n_all - n_q_ok}/{n_all} views outside focus >= "
                  f"{float(spec.min_focus):.0f} / saturation <= "
                  f"{float(spec.max_saturation_frac):.3f}"
                  + (f" (focus min {focus_min:.0f})" if focus_min is not None else "")
                  + (f" (saturation max {sat_max:.4f})" if sat_max is not None else "")
                  + (f"; missing metrics: {', '.join(missing_quality_metrics[:6])}"
                     if missing_quality_metrics else "")
                  + "; recorded quality.passed is not used without metrics.")

    # The stationarity statistics describe the supplied stream when there is
    # one; otherwise they describe the accepted observations but the verdict
    # stays "unestablished" rather than silently passing or failing.
    selected_rows = stream_rows if stream_supplied else s_rows
    speeds = np.array([r["stationarity_speed_mm_s"] for r in selected_rows
                       if r["stationarity_speed_mm_s"] is not None], float)
    rates = np.array([r["stationarity_rate_deg_s"] for r in selected_rows
                      if r["stationarity_rate_deg_s"] is not None], float)
    windows = np.array([r["stationarity_window_s"] for r in selected_rows
                        if r["stationarity_window_s"] is not None], float)
    blurs = np.array([r["motion_blur_px"] for r in selected_rows
                      if r["motion_blur_px"] is not None], float)
    stationary_flags = [bool(r["stationary"]) for r in s_rows]
    counted_intervals = ([i for i in stream_intervals if i.get("qualified")]
                         if stream_supplied
                         else [i for i in accepted_intervals if i.get("qualified")])
    n_intervals = len(counted_intervals)
    n_stationary = stream_stationary if stream_supplied else accepted_stationary
    stationary_ok = bool(stationarity_established
                         and n_intervals >= int(spec.min_stationary_views))
    stationarity_sources: dict[str, int] = {}
    for r in selected_rows:
        stationarity_sources[r["stationarity_source"]] = \
            stationarity_sources.get(r["stationarity_source"], 0) + 1
    missing_stationarity = int(sum(1 for r in selected_rows
                                   if r["stationarity_source"] == "missing"))
    stationarity_measured = {
        "n_stationary": n_stationary,
        "n_stationary_intervals": n_intervals,
        "n_stationary_observations": (stream_stationary if stream_supplied
                                      else accepted_stationary),
        "n_views": n_all,
        "n_stream_observations": len(stream_rows),
        "accepted_stationary_views": accepted_stationary,
        "accepted_stationary_fraction": (accepted_stationary / n_all) if n_all else 0.0,
        "accepted_stationary_intervals": len(
            [i for i in accepted_intervals if i.get("qualified")]),
        "n_unqualified_intervals": (
            len(stream_intervals) - n_intervals if stream_supplied
            else len(accepted_intervals) - n_intervals),
        "min_dwell_s": float(spec.min_dwell_s),
        "min_dwell_observations": int(spec.min_dwell_observations),
        "dwell_durations_s": [round(float(i.get("duration_s") or 0.0), 3)
                              for i in counted_intervals],
        "stationary_fraction": (n_stationary / len(stream_rows))
                               if (stream_supplied and stream_rows) else 0.0,
        "n_dwell_views": n_dwell,
        "stationarity_status": stationarity_status,
        "stationarity_source": ("stream" if stationarity_established else
                                ("accepted_only" if accepted_stationary
                                 else "unestablished")),
        "stationarity_source_counts": stationarity_sources,
        "n_missing_stationarity": missing_stationarity,
        "stationarity_interval_gap_s": float(spec.stationarity_interval_gap_s),
        "speed_p50_mm_s": float(np.median(speeds)) if len(speeds) else None,
        "speed_p95_mm_s": float(np.percentile(speeds, 95)) if len(speeds) else None,
        "rate_p50_deg_s": float(np.median(rates)) if len(rates) else None,
        "rate_p95_deg_s": float(np.percentile(rates, 95)) if len(rates) else None,
        "window_s_p50": float(np.median(windows)) if len(windows) else None,
        "motion_blur_px_max": float(np.max(blurs)) if len(blurs) else None,
    }
    stationarity_deficit = {
        "stationary_views": max(0, int(spec.min_stationary_views) - n_intervals),
        "stationary_intervals": n_intervals,
        "unestablished": not stationarity_established,
        "missing_stationarity_observations": missing_stationarity,
    }
    if stationary_ok:
        s_hint = (f"Stationarity: {n_intervals} distinct dwell interval(s) from "
                  f"{n_stationary} stationary stream observation(s) "
                  f"(need {int(spec.min_stationary_views)}; accepted views "
                  f"{accepted_stationary}/{n_all}).")
    elif not stationarity_established:
        s_hint = (f"Stationarity unestablished: no session-level stationarity "
                  f"observation sequence was supplied (accepted views: "
                  f"{accepted_stationary}/{n_all} stationary).  The controller "
                  f"must pass every ingested observation's consecutive motion "
                  f"fields, not only accepted views; need "
                  f"{int(spec.min_stationary_views)} distinct dwell intervals.")
    else:
        s_hint = (f"Stationarity: only {n_intervals} distinct dwell interval(s) "
                  f"from {n_stationary} stationary stream observation(s) "
                  f"(need {int(spec.min_stationary_views)}); median speed "
                  f"{float(np.median(speeds)):.1f} mm/s, median rotation "
                  f"{float(np.median(rates)):.2f} deg/s.  Hold each pose still "
                  f"for ~0.3 s and keep the dwell frames.")
    d7_sat = quality_ok and stationary_ok
    d7_deficit = {"quality": quality_deficit["quality_views"],
                  "stationary_views": stationarity_deficit["stationary_views"],
                  "stationarity_unestablished": not stationarity_established,
                  "missing_stationarity_observations": missing_stationarity}
    d7_hint = ("Quality and dwell are within limits." if d7_sat else
               (q_hint if not quality_ok else "") +
               (" " if not quality_ok and not stationary_ok else "") +
               (s_hint if not stationary_ok else ""))
    checks["quality"] = _check(
        "image quality (focus/saturation)",
        {"min_focus": float(spec.min_focus),
         "max_saturation_frac": float(spec.max_saturation_frac),
         "min_quality_fraction": float(spec.min_quality_fraction),
         "camera_scope": list(_IR_CAMERAS),
         "tex_policy": "reported, not gated by D7"},
        quality_measured, quality_ok, quality_deficit, q_hint)
    checks["stationarity"] = _check(
        "stationarity (dwell intervals from the observation stream)",
        {"min_stationary_views": int(spec.min_stationary_views),
         "stationary_speed_mm_s": float(spec.stationary_speed_mm_s),
         "stationary_rate_deg_s": float(spec.stationary_rate_deg_s),
         "stationarity_interval_gap_s": float(spec.stationarity_interval_gap_s),
         "observation_scope": "every ingested raw/decision observation; not "
                              "only accepted views",
         "stream_observation_required": True},
        stationarity_measured, stationary_ok, stationarity_deficit, s_hint)
    dims["D7"] = {"id": "D7", "label": "stationarity / quality",
                  "required": {"min_quality_fraction": float(spec.min_quality_fraction),
                               "min_focus": float(spec.min_focus),
                               "max_saturation_frac": float(spec.max_saturation_frac),
                               "min_stationary_views": int(spec.min_stationary_views),
                               "stationary_speed_mm_s": float(spec.stationary_speed_mm_s),
                               "stationary_rate_deg_s": float(spec.stationary_rate_deg_s),
                               "stationarity_interval_gap_s": float(
                                   spec.stationarity_interval_gap_s)},
                  "measured": {**quality_measured, **stationarity_measured},
                  "satisfied": d7_sat, "deficit": d7_deficit, "hint": d7_hint,
                  "detail": {"quality": q_rows, "stationarity": s_rows,
                             "stream_stationarity": stream_rows,
                             "stationarity_intervals": (stream_intervals
                                                        if stream_supplied
                                                        else accepted_intervals),
                             "stationarity_status": stationarity_status,
                             "stationary": stationary_flags,
                             "speed_mm_s": speeds.tolist(),
                             "rate_deg_s": rates.tolist()},
                  "checks": ["quality", "stationarity"]}

    # ---- D8 scale artefact ---------------------------------------------
    artefacts = []
    for v in view_of:
        a = v.get("scale_artefact")
        if isinstance(a, Mapping):
            artefacts.append({"view_id": v.get("view_id"),
                              "length_mm": _maybe_float(a.get("length_mm")),
                              "measured_mm": _maybe_float(a.get("measured_mm")),
                              "distance_mm": float(np.linalg.norm(
                                  _as_pose(v.get("pose"))[1]))})
        elif a:
            artefacts.append({"view_id": v.get("view_id"), "length_mm": None,
                              "measured_mm": None,
                              "distance_mm": float(np.linalg.norm(
                                  _as_pose(v.get("pose"))[1]))})
    scale_bands = sorted({spec.band_index(a["distance_mm"]) for a in artefacts
                          if spec.band_index(a["distance_mm"]) is not None})
    errs = [abs(a["measured_mm"] - a["length_mm"]) / a["length_mm"]
            for a in artefacts
            if a["length_mm"] and a["measured_mm"] is not None and a["length_mm"] > 0]
    d8_required = bool(spec.require_scale_artefact)
    d8_sat = True
    if d8_required:
        d8_sat = (len(artefacts) >= int(spec.min_views_per_scale_artefact)
                  and len(scale_bands) >= int(spec.min_scale_distance_bands)
                  and (not errs or max(errs) <= float(spec.scale_tolerance_rel)))
    d8_deficit = ({} if d8_sat else
                  {"views": max(0, int(spec.min_views_per_scale_artefact) - len(artefacts)),
                   "distance_bands": max(0, int(spec.min_scale_distance_bands) - len(scale_bands)),
                   "worst_rel_error": max(0.0, (max(errs) if errs else 0.0)
                                          - float(spec.scale_tolerance_rel))})
    d8_hint = ("Scale artefact measured in "
               f"{len(artefacts)} poses across {len(scale_bands)} distance bands"
               + (f", worst relative error {max(errs):.3%}." if errs else ".")
               if d8_required and d8_sat else
               ("No scale artefact: absolute metric scale stays labelled "
                "unvalidated (the plate pitch is a gauge; see plan §4.6)."
                if not d8_required else
                f"Image a known-length artefact in at least "
                f"{spec.min_views_per_scale_artefact} poses across "
                f"{spec.min_scale_distance_bands} distance bands "
                f"(have {len(artefacts)} poses / {len(scale_bands)} bands)."))
    checks["scale_artefact"] = _check(
        "scale artefact (absolute scale)",
        {"required": d8_required,
         "min_views": int(spec.min_views_per_scale_artefact),
         "min_distance_bands": int(spec.min_scale_distance_bands),
         "tolerance_rel": float(spec.scale_tolerance_rel)},
        {"n_views": len(artefacts), "distance_bands": scale_bands,
         "worst_rel_error": float(max(errs)) if errs else None,
         "label": "validated" if (errs and max(errs) <= spec.scale_tolerance_rel)
                  else "unvalidated"},
        d8_sat, d8_deficit, d8_hint)
    dims["D8"] = {"id": "D8", "label": "scale artefact",
                  "required": checks["scale_artefact"]["required"],
                  "measured": checks["scale_artefact"]["measured"],
                  "satisfied": d8_sat, "deficit": d8_deficit, "hint": d8_hint,
                  "detail": {"artefacts": artefacts,
                             "note": ("a session without an artefact cannot close "
                                      "the ~1 % scale question")},
                  "checks": ["scale_artefact"]}

    # ---- stability of the *current* pose (guidance input) --------------
    stable, stability = _stability(all_metrics, all_view_of, spec)

    out = {
        "n_views": len(views),
        "n_usable": len(all_metrics),
        "n_pose_views": n,
        "n_dwell_views": n_dwell,
        "n_unusable": len(unusable),
        "unusable": unusable,
        "spec": spec.as_dict(),
        "checks": checks,
        "pose_stats": {
            "distance_mm": _range(metrics, "distance_mm"),
            "depth_mm": _range(metrics, "depth_mm"),
            "tilt_x_deg": _range(metrics, "tilt_x_deg"),
            "tilt_y_deg": _range(metrics, "tilt_y_deg"),
            "tilt_x_centre_deg": med_x,
            "tilt_y_centre_deg": med_y,
            "roll_deg": _range(metrics, "roll_deg"),
            "tilt_deg": _range(metrics, "tilt_deg"),
        },
        "stability": stability,
        "stable": stable,
    }
    out.update(dims)
    if latched_checks:
        tilt_names = set(dims["D2"].get("checks") or [])
        forced = []
        for name in sorted(set(latched_checks) & tilt_names):
            node = out["checks"].get(name)
            if node is not None and not node.get("satisfied"):
                node["satisfied"] = True
                node["latched"] = True
                node["deficit"] = {}
                forced.append(name)
        if forced and all(out["checks"][name].get("satisfied")
                          for name in tilt_names):
            dims["D2"]["satisfied"] = True
            dims["D2"]["latched"] = True
            dims["D2"]["deficit"] = {}
            dims["D2"]["hint"] = (
                "Satisfied earlier in this session; accepted evidence is only "
                "additive, so the moving tilt centre does not revoke it.")
    verdict = readiness(out)
    out["ready"] = bool(verdict["ready"])
    out["summary"] = verdict["summary"]
    if n > 0 and n == n_all - n_dwell:
        # Compatibility block for ``WizardController._external_coverage``:
        # eight dimension keys plus ``ready``.  It is only emitted when every
        # non-dwell entry was usable — i.e. when the pose numbers describe the
        # real geometry.  If the caller supplied entries without a pose the
        # controller must fall back to its own bookkeeping rather than display
        # a measurement that was never taken.
        #
        # ``quality`` and ``stationarity`` are the two *separately honest*
        # halves of D7.  They must not both inherit D7's combined hint: the
        # GUI shows them as two sub-indicators.
        out["dimensions"] = {
            "distance_bands": dims["D1"],
            "tilt_two_axes": dims["D2"],
            "roll_sectors": dims["D3"],
            "image_regions": dims["D4"],
            "stereo_overlap": dims["D5"],
            "colour_participation": dims["D6"],
            "quality": _sub_dimension(checks, "quality", "D7"),
            "stationarity": _sub_dimension(checks, "stationarity", "D7"),
            "scale_artefact": dims["D8"],
        }
    return out


def _sub_dimension(checks: Mapping[str, Any], name: str,
                   parent_id: str) -> dict:
    """One sub-check of a composite dimension, shaped like a dimension.

    The node is a copy of its own :func:`_check` record, so ``satisfied``,
    ``measured``, ``deficit`` and ``hint`` all describe that half only.  The
    old implementation copied the parent D7 hint into both halves, which is
    precisely the "both sub-indicators show the combined failure" bug.
    """
    source = checks.get(name) or {}
    node = dict(source)
    node["id"] = f"{parent_id}.{name}"
    node["label"] = source.get("label", name)
    node["parent_id"] = parent_id
    node["satisfied"] = bool(source.get("satisfied"))
    node["required"] = source.get("required")
    node["measured"] = source.get("measured")
    node["deficit"] = source.get("deficit")
    node["hint"] = source.get("hint", "")
    return node


def _maybe_float(x: Any) -> float | None:
    try:
        return float(x) if x is not None else None
    except (TypeError, ValueError):
        return None


def _range(metrics: Sequence[Mapping[str, Any]], key: str) -> dict:
    if not metrics:
        return {"min": None, "max": None, "span": None}
    return _range_of(np.array([float(m[key]) for m in metrics], float))


def _midrange(vals: np.ndarray) -> float:
    if len(vals) == 0:
        return 0.0
    return 0.5 * (float(np.min(vals)) + float(np.max(vals)))


def _span(vals: np.ndarray) -> float:
    return float(np.ptp(vals)) if len(vals) else 0.0


def _range_of(vals: np.ndarray) -> dict:
    if len(vals) == 0:
        return {"min": None, "max": None, "span": 0.0}
    lo, hi = float(np.min(vals)), float(np.max(vals))
    return {"min": lo, "max": hi, "span": hi - lo}


def _axis_gap(tx: np.ndarray, ty: np.ndarray) -> float:
    """Ratio of the two tilt-axis spreads (1.0 = isotropic excitation)."""
    if len(tx) < 2:
        return 0.0
    sx = _span(tx)
    sy = _span(ty)
    return float(min(sx, sy) / max(sx, sy, 1e-9))


def _distance_hint(spec: CoverageSpec, counts: Sequence[int],
                   metrics: Sequence[Mapping[str, Any]]) -> str:
    near = [i for i, c in enumerate(counts) if c < spec.min_views_per_distance_band]
    lo, hi = (spec.distance_bands_mm[near[0]] if near
              else spec.distance_bands_mm[0])
    now = _range(metrics, "distance_mm")
    need = int(spec.min_views_per_distance_band)
    closer = [i for i in near
              if (now["min"] is None or spec.distance_bands_mm[i][0] < (now["min"] or 0))]
    word = "closer" if closer else "further away"
    return (f"Move {word} — {need - counts[near[0]]} more views between "
            f"{lo:.0f} and {hi:.0f} mm are needed"
            + (f"; distance so far {now['min']:.0f}-{now['max']:.0f} mm"
               if now["min"] is not None else "")
            + f" and all of {_fmt_bands(spec.distance_bands_mm)} mm must be visited "
              f"({need} views each).")


def _axis_hint(label: str, pos: int, neg: int, need: int, thr: float,
               median_deg: float) -> str:
    if pos >= need and neg >= need:
        return f"{label.capitalize()}: both signs covered."
    parts = []
    if pos < need:
        parts.append(f"{need - pos} more at >= +{thr:.0f} deg")
    if neg < need:
        parts.append(f"{need - neg} more at <= -{thr:.0f} deg")
    return (f"Tilt the plate about {label}: "
            + "; ".join(parts)
            + f" around the centre of the excursion ({median_deg:+.1f} deg)"
            + " — this axis is currently one-sided.")


def _tilt_hint(spec: CoverageSpec, xp: int, xn: int, yp: int, yn: int,
               tx: np.ndarray, ty: np.ndarray, med_x: float, med_y: float,
               tx_raw: np.ndarray, ty_raw: np.ndarray) -> str:
    thr = spec.min_tilt_deg
    need = spec.min_views_per_tilt_quadrant
    if xp >= need and xn >= need and yp >= need and yn >= need:
        return f"Two-axis tilt covered (>= {thr:.0f} deg in all four quadrants)."
    weak_axis = "second axis (y)" if min(yp, yn) <= min(xp, xn) else "x axis"
    return (f"Tilt the plate about the {weak_axis} as well: "
            f"x {xp:+d}/{xn:+d} and y {yp:+d}/{yn:+d} views beyond "
            f"+/-{thr:.0f} deg of the excursion centre "
            f"(median x {med_x:+.1f} deg, y {med_y:+.1f} deg; need {need} per sign); "
            f"absolute tilt range x {_span(tx_raw):.1f} deg, "
            f"y {_span(ty_raw):.1f} deg, "
            f"centred x {_span(tx):.1f} deg, y {_span(ty):.1f} deg.")


def _region_hint(counts: Sequence[int], corners: Mapping[str, int],
                 spec: CoverageSpec, W: int, H: int) -> str:
    names = ["left", "centre", "right"]
    rows = ["top", "middle", "bottom"]
    empty = [f"{rows[i]}-{names[j]}" for i in range(3) for j in range(3)
             if counts[i * 3 + j] == 0]
    miss_corners = [k for k, c in corners.items() if c < spec.min_corner_views]
    tx = ""
    if empty:
        tx = "Point the plate at the " + ", ".join(empty[:4]) + " of the image"
        if len(empty) > 4:
            tx += f" and {len(empty) - 4} more empty regions"
        tx += ". "
    if miss_corners:
        tx += ("Corners never visited: "
               + ", ".join(k.replace("_", " ") for k in miss_corners) + ".")
    return tx.strip() or "Image-plane coverage incomplete."


def _stability(metrics: Sequence[Mapping[str, Any]],
               views: Sequence[Mapping[str, Any]],
               spec: CoverageSpec) -> tuple[bool, dict]:
    """Is the current (last) pose good enough to guide from?

    A pose is *stable* when the last view is usable, has an unambiguous
    assignment, enough shared IR markers, and is not moving faster than the
    D7 dwell thresholds.  Guidance refuses to extrapolate a trajectory from an
    unstable pose.
    """
    if not metrics:
        return False, {"reason": "no accepted views yet", "last_view": None}
    v = views[-1]
    reasons = []
    if v.get("assignment_ok", True) is False:
        reasons.append("ambiguous marker-label assignment")
    shared = int(v.get("shared_markers_ir", 0) or 0)
    if shared < int(spec.min_shared_markers):
        reasons.append(f"only {shared} shared IR markers (< {spec.min_shared_markers})")

    obs = stationarity_observation(v, spec)
    speed = obs["stationarity_speed_mm_s"]
    rate = obs["stationarity_rate_deg_s"]
    if obs["stationarity_source"] != "missing":
        source = obs["stationarity_source"]
        if speed is not None and speed > spec.stationary_speed_mm_s:
            reasons.append(f"moving at {speed:.1f} mm/s")
        if rate is not None and rate > spec.stationary_rate_deg_s:
            reasons.append(f"rotating at {rate:.2f} deg/s")
    else:
        # Diagnostic only: the sparse accepted-pose delta can inform the
        # guidance guard, but it is not stationarity evidence for D7.
        source = "accepted_pose_delta"
        ts = [_timestamp(x) for x in views]
        if len(metrics) >= 2 and ts[-1] is not None and ts[-2] is not None \
                and ts[-1] > ts[-2]:
            dt = ts[-1] - ts[-2]
            speed = float(np.linalg.norm(metrics[-1]["t"] - metrics[-2]["t"])) / dt
            rate = _rotation_angle_deg(metrics[-2]["R"], metrics[-1]["R"]) / dt
            if speed > spec.stationary_speed_mm_s:
                reasons.append(f"moving at {speed:.1f} mm/s")
            if rate > spec.stationary_rate_deg_s:
                reasons.append(f"rotating at {rate:.2f} deg/s")
        else:
            speed = rate = None
    stable = not reasons
    return stable, {"stable": stable, "reasons": reasons,
                    "last_view": v.get("view_id"),
                    "last_speed_mm_s": speed, "last_rate_deg_s": rate,
                    "motion_source": source,
                    "last_dwell": _is_dwell(v)}


# ---------------------------------------------------------------------------
# readiness
# ---------------------------------------------------------------------------

def readiness(measurement: Mapping[str, Any],
              accepted_views: Sequence[Mapping[str, Any]] | None = None) -> dict:
    """Decide whether the measured coverage is sufficient.

    ``accepted_views`` is accepted and ignored: ``WizardController`` calls
    ``readiness(views, measured)`` (agent C's convention) and the measurement
    already contains everything the verdict needs.

    Returns ``{"ready", "unsatisfied", "unsatisfied_dimensions", "satisfied",
    "reasons", "n_views", "summary"}``.  ``unsatisfied`` is the list of
    operator-facing check labels (``"image corners"``, ``"tilt about the
    second axis (y, positive and negative)"``, ...) so a caller can log the
    exact missing excitation; ``reasons`` carries the same with numbers.

    The two arguments are swapped automatically when the first is not a
    measurement (``WizardController`` calls ``readiness(views, measured)``).
    """
    if not isinstance(measurement, Mapping) or (
            "checks" not in measurement and isinstance(accepted_views, Mapping)
            and "checks" in accepted_views):
        measurement, accepted_views = accepted_views, measurement  # type: ignore[assignment]
    checks = measurement.get("checks") or {}
    unsatisfied: list[str] = []          # operator-facing labels
    unsatisfied_checks: list[str] = []   # machine keys
    unsatisfied_dims: list[str] = []
    reasons: list[dict] = []
    satisfied_all: list[str] = []
    for dim_id in DIMENSIONS:
        dim = measurement.get(dim_id)
        if not dim:
            continue
        dim_ok = bool(dim.get("satisfied"))
        if not dim_ok:
            unsatisfied_dims.append(dim_id)
        for name in dim.get("checks", []):
            c = checks.get(name) or {}
            if c.get("satisfied"):
                lab = c.get("label", name)
                if lab not in satisfied_all:
                    satisfied_all.append(lab)
                continue
            label = c.get("label", name)
            if label not in unsatisfied:
                unsatisfied.append(label)
            if name not in unsatisfied_checks:
                unsatisfied_checks.append(name)
            reasons.append({
                "dimension": dim_id,
                "check": name,
                "label": c.get("label", name),
                "deficit": c.get("deficit"),
                "measured": c.get("measured"),
                "required": c.get("required"),
                "hint": c.get("hint", ""),
            })
    ready = not unsatisfied_dims and bool(measurement.get("n_views"))
    if ready:
        summary = (f"ready: all of D1-D8 satisfied over "
                   f"{measurement.get('n_views')} accepted views")
    elif not measurement.get("n_views"):
        summary = "not ready: no accepted views yet"
    else:
        summary = (f"not ready: {len(unsatisfied_dims)} dimension(s) unsatisfied over "
                   f"{measurement.get('n_views')} accepted views -> "
                   + ", ".join(unsatisfied_dims))
    return {"ready": bool(ready), "unsatisfied": unsatisfied,
            "unsatisfied_checks": unsatisfied_checks,
            "unsatisfied_dimensions": unsatisfied_dims,
            "satisfied": satisfied_all, "reasons": reasons,
            "n_views": int(measurement.get("n_views") or 0),
            "summary": summary}


# ---------------------------------------------------------------------------
# guidance
# ---------------------------------------------------------------------------

def _band_centre(spec: CoverageSpec, i: int) -> float:
    lo, hi = spec.distance_bands_mm[i]
    return 0.5 * (lo + hi)


_ROW_WORDS = ("top", "middle", "bottom")
_COL_WORDS = ("left", "centre", "right")
_CORNER_CELLS = {"top_left": (0, 0), "top_right": (0, 2),
                 "bottom_left": (2, 0), "bottom_right": (2, 2)}


def _cell_words(row: int, col: int) -> str:
    return f"{_ROW_WORDS[row]}-{_COL_WORDS[col]}"


def _image_targets(measurement: Mapping[str, Any],
                   spec: CoverageSpec) -> list[dict]:
    """Only the image cells whose measured count is genuinely deficient.

    The old guidance generated all four corners whenever any corner was
    missing, then effort-sorted them.  On the real session that kept proposing
    top-left (9 visits) because it was nearest, while top-right and bottom-right
    stayed at zero.  A satisfied corner is never a guidance target.
    """
    checks = measurement.get("checks") or {}
    targets: list[dict] = []
    seen: set[tuple[int, int]] = set()
    corner_check = checks.get("image_corners") or {}
    if not corner_check.get("satisfied", True):
        corners = ((corner_check.get("measured") or {}).get("corners") or {})
        for name, (row, col) in _CORNER_CELLS.items():
            count = int(corners.get(name, 0) or 0)
            if count < int(spec.min_corner_views) and (row, col) not in seen:
                targets.append({"row": row, "col": col, "kind": "corner",
                                "name": name, "count": count,
                                "label": f"{_cell_words(row, col)} corner",
                                "need": int(spec.min_corner_views)})
                seen.add((row, col))
    region_check = checks.get("image_regions") or {}
    if not region_check.get("satisfied", True):
        counts = ((region_check.get("measured") or {}).get("region_counts")
                  or [])
        cells: list[tuple[int, int]] = []
        if len(counts) >= 9:
            cells = [(r, c) for r in range(3) for c in range(3)
                     if counts[r * 3 + c] == 0]
            if not cells:
                # Visited-region requirement is ">= 6 visited"; if it is still
                # unsatisfied there is a zero somewhere, but a partial caller
                # may only report the grid.  Target the least visited cells.
                cells = sorted(
                    ((r, c) for r in range(3) for c in range(3)
                     if (r, c) != (1, 1)),
                    key=lambda rc: counts[rc[0] * 3 + rc[1]])[:1]
        else:
            cells = [(r, c) for r in range(3) for c in range(3)
                     if (r, c) != (1, 1)]
        for (row, col) in cells:
            if (row, col) in seen:
                continue
            count = int(counts[row * 3 + col]) if len(counts) >= 9 else 0
            targets.append({"row": row, "col": col, "kind": "region",
                            "name": f"{_cell_words(row, col)}_region",
                            "count": count,
                            "label": f"{_cell_words(row, col)} region",
                            "need": 0})
            seen.add((row, col))
    return targets


def _families(measurement: Mapping[str, Any], spec: CoverageSpec,
              current: dict | None) -> list[dict]:
    """Rank candidate pose families for the genuinely unsatisfied checks.

    A family is generated only when it unlocks at least one unsatisfied
    check.  Satisfied dimensions may still supply a *carrier* (the current
    distance band or roll), but they never appear in ``unlocks`` and never lead
    the operator instruction.
    """
    checks = measurement.get("checks") or {}
    nx = len(spec.distance_bands_mm)
    families: list[dict] = []

    band_counts = ((checks.get("distance_bands") or {}).get("measured") or {}) \
        .get("band_counts", [0] * nx)
    band_missing = [i for i in range(nx)
                    if (band_counts[i] if i < len(band_counts) else 0)
                    < int(spec.min_views_per_distance_band)]
    distance_required = bool(band_missing)
    if distance_required:
        band_choices = [(b, _band_centre(spec, b), True) for b in band_missing]
    else:
        cd = None if current is None else current.get("distance_mm")
        cd = float(cd) if cd is not None else None
        if cd is not None:
            b = spec.band_index(cd)
            if b is None:
                b = min(range(nx), key=lambda i: abs(_band_centre(spec, i) - cd))
                target_d = _band_centre(spec, b)
            else:
                target_d = min(max(cd, spec.distance_bands_mm[b][0]),
                               spec.distance_bands_mm[b][1] - 1e-6)
        else:
            b = min(range(nx), key=lambda i: abs(_band_centre(spec, i)))
            target_d = _band_centre(spec, b)
        band_choices = [(b, target_d, False)]

    tilt_x_missing = not (checks.get("tilt_axis_x") or {}).get("satisfied", True)
    tilt_y_missing = not (checks.get("tilt_axis_y") or {}).get("satisfied", True)
    quad_missing = not (checks.get("tilt_quadrants") or {}).get("satisfied", True)
    roll_missing = not (checks.get("roll_sectors") or {}).get("satisfied", True)
    tilt_required = bool(tilt_x_missing or tilt_y_missing or quad_missing)
    image_targets = _image_targets(measurement, spec)
    image_choices: list[dict | None] = list(image_targets) or [None]
    if not (distance_required or tilt_required or roll_missing or image_targets):
        return []

    stats = measurement.get("pose_stats") or {}
    med_x = float(stats.get("tilt_x_centre_deg") or 0.0)
    med_y = float(stats.get("tilt_y_centre_deg") or 0.0)
    exc = float(spec.min_tilt_deg) + 5.0
    if tilt_x_missing or quad_missing:
        tx_choices = [med_x - exc, med_x + exc]
    else:
        tx_choices = [float((current or {}).get("tilt_x_deg", med_x))]
    if tilt_y_missing or quad_missing:
        ty_choices = [med_y - exc, med_y + exc]
    else:
        ty_choices = [float((current or {}).get("tilt_y_deg", med_y))]

    if roll_missing:
        counts = ((checks.get("roll_sectors") or {}).get("measured") or {}) \
            .get("bin_counts", [])
        size = spec.roll_bin_size_deg()
        roll_choices = [((i + 0.5) * size) % 360.0 for i, c in enumerate(counts)
                        if c < int(spec.min_views_per_roll_bin)] or [0.0]
    else:
        roll_choices = [float((current or {}).get("roll_deg", 0.0))]

    for b, target_d, dist_required in band_choices:
        for tx in tx_choices:
            for ty in ty_choices:
                for roll in roll_choices:
                    for target in image_choices:
                        row = None if target is None else int(target["row"])
                        col = None if target is None else int(target["col"])
                        lat = (0.0, 0.0)
                        if row is not None:
                            lat = (((col - 1) * 0.27) * target_d,
                                   ((row - 1) * 0.21) * target_d)
                        # ``target_d`` is the board's distance from the camera
                        # (norm(t)), not its z translation.  A lateral offset
                        # therefore reduces z so the corner target stays inside
                        # the requested 300-560 mm physical band.
                        z = math.sqrt(max(target_d * target_d
                                          - lat[0] * lat[0]
                                          - lat[1] * lat[1], 1.0))
                        R, t = family_pose(z, tx, ty, roll, lat)
                        unlocks: list[str] = []
                        weight = 0
                        if dist_required:
                            unlocks.append(f"distance band {b}")
                            weight += 1
                        if tilt_required:
                            unlocks.append(
                                f"tilt ({'+' if tx >= 0 else '-'}x,"
                                f"{'+' if ty >= 0 else '-'}y)")
                            weight += 2
                        if roll_missing:
                            unlocks.append(
                                f"roll sector {int((roll % 360) // 90)}")
                            weight += 1
                        if target is not None:
                            if target["kind"] == "corner":
                                unlocks.append(
                                    f"image corner {target['label']} "
                                    f"(needs {max(0, target['need'] - target['count'])} "
                                    f"more)")
                                weight += 3
                            else:
                                unlocks.append(
                                    f"image region {target['label']} (unvisited)")
                                weight += 2
                        if not unlocks:
                            continue
                        fam = {"distance_band": b,
                               "distance_mm": float(target_d),
                               "pose_z_mm": float(z),
                               "tilt_x_deg": float(tx),
                               "tilt_y_deg": float(ty),
                               "roll_deg": float(roll),
                               "distance_required": dist_required,
                               "tilt_required": tilt_required,
                               "roll_required": roll_missing,
                               "image_target": target,
                               "image_region": (None if row is None else
                                                [int(row), int(col)]),
                               "lateral_mm": [float(lat[0]), float(lat[1])],
                               "unlock_weight": int(weight),
                               "unlocks": unlocks,
                               "R": R, "t": t}
                        if current is not None:
                            eff = pose_effort((current["R"], current["t"]),
                                              (R, t))
                        else:
                            eff = {"score": 0.0, "lateral_mm": 0.0,
                                   "depth_mm": 0.0, "rotation_deg": 0.0,
                                   "move_mm": [0.0, 0.0, 0.0]}
                        fam["effort"] = eff
                        families.append(fam)
    # First unlock *what is genuinely missing* (a corner outweighs a carrier
    # distance), then nearest family; the rest of the key is deterministic.
    families.sort(key=lambda f: (-int(f["unlock_weight"]),
                                 round(float(f["effort"]["score"]), 6),
                                 f["distance_band"], f["tilt_x_deg"],
                                 f["tilt_y_deg"], f["roll_deg"],
                                 -1 if f["image_region"] is None
                                 else f["image_region"][0],
                                 -1 if f["image_region"] is None
                                 else f["image_region"][1]))
    return families


def _instruction(spec: CoverageSpec, fam: Mapping[str, Any],
                 current: dict | None) -> str:
    """Short operator sentence, missing information first.

    Distance is stated only when a distance band is genuinely unsatisfied.
    When D1 is complete the band is an internal carrier: the operator must see
    the corner/region that is actually missing, not "move to 380-460 mm".
    """
    parts: list[str] = []
    target = fam.get("image_target")
    if target is not None:
        if target.get("kind") == "corner":
            parts.append(f"put the plate centre in the {target['label']} "
                         f"of the image")
        else:
            parts.append(f"put the plate centre in the {target['label']} "
                         f"of the image")
    if fam.get("tilt_required"):
        dtx = fam["tilt_x_deg"] - (current["tilt_x_deg"] if current else 0.0)
        dty = fam["tilt_y_deg"] - (current["tilt_y_deg"] if current else 0.0)
        tilts = []
        if abs(dtx) >= 2.0:
            tilts.append(f"{abs(dtx):.0f} deg {'left' if dtx < 0 else 'right'}")
        if abs(dty) >= 2.0:
            tilts.append(f"{abs(dty):.0f} deg {'down' if dty < 0 else 'up'}")
        if tilts:
            parts.append("tilt " + " and ".join(tilts))
    if fam.get("roll_required"):
        droll = fam["roll_deg"] - (current["roll_deg"] if current else 0.0)
        droll = (droll + 180.0) % 360.0 - 180.0
        if abs(droll) >= 5.0:
            parts.append(f"roll the plate by {abs(droll):.0f} deg "
                         f"{'clockwise' if droll < 0 else 'anticlockwise'}")
    if fam.get("distance_required"):
        lo, hi = spec.distance_bands_mm[fam["distance_band"]]
        text = f"move to {lo:.0f}-{hi:.0f} mm"
        if current is not None:
            d = fam["distance_mm"] - current["distance_mm"]
            if abs(d) >= 10.0:
                text += f" ({'closer' if d < 0 else 'further'}, {abs(d):.0f} mm)"
        parts.append(text)
    if not parts:
        parts.append("hold still")
    text = ", ".join(parts) + "."
    return text[:1].upper() + text[1:]


def _dwell_hint(measurement: Mapping[str, Any]) -> str:
    """The D7 sub-hint, independent of the pose guidance."""
    checks = measurement.get("checks") or {}
    hints = []
    for name in ("quality", "stationarity"):
        check = checks.get(name) or {}
        if check and not check.get("satisfied", True):
            hints.append(str(check.get("hint") or ""))
    return " ".join(h for h in hints if h)


def _pose_affecting_unsatisfied(measurement: Mapping[str, Any]) -> list[str]:
    """Dimensions D1-D6 that still need a new pose (not D7/D8)."""
    return [d for d in DIMENSIONS if d not in ("D7", "D8")
            and not (measurement.get(d) or {}).get("satisfied", False)]


def _dwell_hold_result(measurement: Mapping[str, Any],
                       ready: Mapping[str, Any],
                       spec: CoverageSpec) -> dict | None:
    """A hold/dwell instruction when only acquisition quality remains.

    Once every pose dimension is satisfied, generating another pose family
    would send the operator on an endless walk while D7 stays unsatisfied.
    Quality/stationarity are acquisition properties: the actionable request is
    to hold still and keep the dwell frames.
    """
    if _pose_affecting_unsatisfied(measurement):
        return None
    checks = measurement.get("checks") or {}
    stat = checks.get("stationarity") or {}
    qual = checks.get("quality") or {}
    hints = _dwell_hint(measurement)
    d8 = measurement.get("D8") or {}
    if not d8.get("satisfied", True) and d8.get("hint"):
        hints = (hints + " " + str(d8["hint"])).strip()
    if not hints:
        return None
    if stat and not stat.get("satisfied", True):
        deficit = stat.get("deficit") or {}
        need = int(deficit.get("stationary_views") or 0)
        instruction = (
            f"Hold the plate still at this pose for ~0.3 s — stationarity "
            f"needs {need} more distinct dwell interval(s).  Keep the duplicate "
            f"dwell frames; they are evidence, not redundant solver views.")
        if qual and not qual.get("satisfied", True):
            instruction += " " + str(qual.get("hint") or "")
    else:
        instruction = hints or "Hold the plate still for a moment."
    return {"mode": "hold", "instruction": instruction.strip(),
            "target_pose": None, "target_R": None, "target_t": None,
            "family": None, "families": [], "unlocks": [], "effort": None,
            "precise": False, "dwell_hint": hints,
            "reason": "only acquisition-quality dimensions remain",
            "readiness": ready.get("summary", "")}


def guidance_target(measurement: Mapping[str, Any],
                    current_pose: Any = None,
                    spec: CoverageSpec | None = None) -> dict:
    """The single most valuable next pose *family*.

    Returns ``{"mode", "instruction", "target_pose", "target_R", "target_t",
    "family", "families", "unlocks", "effort", "precise", "reason"}``.

    * ``mode="move"`` with a coarse target pose when the current pose is
      stable and coverage is still incomplete;
    * ``mode="hold"`` with ``target_pose=None`` when the last pose is unstable
      (missing/ambiguous markers, still moving) — guidance must never
      extrapolate a precise hand trajectory from an unstable pose;
    * ``mode="done"`` when every dimension is satisfied.
    """
    spec = spec or _spec_from_measurement(measurement)
    ready = readiness(measurement)
    cur = None
    if current_pose is not None:
        p = _as_pose(current_pose)
        if p is not None:
            m = pose_metrics({"pose": p})
            cur = m

    stable = bool(measurement.get("stable", True))
    if not ready["ready"] and not stable:
        stab = measurement.get("stability") or {}
        raw_reasons = [str(r) for r in (stab.get("reasons") or ["pose not stable"])]
        # Inter-frame speed is not a reason to stop the session: a 1.2 ms
        # exposure does not blur at ordinary hand speed, and a single sharp
        # frame carries a valid pose.  Ask for a pause only when the *pose*
        # itself is unreliable (missing/ambiguous markers, no pose).  Dwell
        # views are collected when the operator naturally pauses; the coverage
        # strip asks for them later instead of interrupting every movement.
        blocking = [r for r in raw_reasons
                    if not (r.startswith("moving at") or r.startswith("rotating at"))]
        if blocking:
            why = ", ".join(blocking)
            return {"mode": "hold", "instruction": "Hold still for a moment — "
                    f"{why}.", "target_pose": None, "target_R": None,
                    "target_t": None, "family": None, "families": [],
                    "unlocks": [], "effort": None, "precise": False,
                    "reason": why, "readiness": ready["summary"]}

    if ready["ready"]:
        return {"mode": "done",
                "instruction": "Coverage complete — the remaining work is the "
                               "uncertainty gate, not more motion.",
                "target_pose": None, "target_R": None, "target_t": None,
                "family": None, "families": [], "unlocks": [],
                "effort": None, "precise": False, "dwell_hint": "",
                "reason": "all dimensions satisfied",
                "readiness": ready["summary"]}

    dwell = _dwell_hold_result(measurement, ready, spec)
    if dwell is not None:
        return dwell

    families = _families(measurement, spec, cur)
    if not families:
        hold = _dwell_hold_result(measurement, ready, spec)
        if hold is not None:
            return hold
        return {"mode": "hold", "instruction": "No admissible next family — "
                "check the plate visibility.", "target_pose": None,
                "target_R": None, "target_t": None, "family": None,
                "families": [], "unlocks": [], "effort": None, "precise": False,
                "dwell_hint": _dwell_hint(measurement),
                "reason": "no family generated", "readiness": ready["summary"]}

    best = families[0]
    instruction = _instruction(spec, best, cur)
    return {
        "mode": "move",
        "instruction": instruction,
        # a *coarse* family representative, never a hand trajectory
        "target_pose": {"R": np.asarray(best["R"], float).tolist(),
                        "t": np.asarray(best["t"], float).tolist()},
        "target_R": np.asarray(best["R"], float).tolist(),
        "target_t": np.asarray(best["t"], float).tolist(),
        "family": {k: v for k, v in best.items() if k not in ("R", "t")},
        "families": [{k: v for k, v in f.items() if k not in ("R", "t")}
                     for f in families[:8]],
        "unlocks": best["unlocks"],
        "effort": best["effort"],
        "precise": False,
        "dwell_hint": _dwell_hint(measurement),
        "reason": "nearest unsatisfied pose family",
        "readiness": ready["summary"],
    }


def _spec_from_measurement(measurement: Mapping[str, Any]) -> CoverageSpec:
    """Rebuild the spec a measurement was taken with (for guidance)."""
    raw = measurement.get("spec")
    if not isinstance(raw, Mapping):
        return CoverageSpec()
    allowed = {f.name for f in CoverageSpec.__dataclass_fields__.values()}
    kw = {}
    for k, v in raw.items():
        if k not in allowed:
            continue
        if k == "distance_bands_mm":
            v = tuple(tuple(b) for b in v)
        kw[k] = v
    try:
        return CoverageSpec(**kw)
    except TypeError:
        return CoverageSpec()


# ---------------------------------------------------------------------------
# convenience
# ---------------------------------------------------------------------------

def unsatisfied(measurement: Mapping[str, Any]) -> list[str]:
    """Shortcut for :func:`readiness`'s check-label list."""
    return readiness(measurement)["unsatisfied"]
