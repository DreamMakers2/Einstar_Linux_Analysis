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
    stationary_speed_mm_s: float = 3.0
    stationary_rate_deg_s: float = 1.5
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


def _timestamp(view: Mapping[str, Any]) -> float | None:
    ts = view.get("timestamp")
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
            spec: CoverageSpec | None = None) -> dict:
    """Measure D1-D8 over the accepted views.

    Parameters
    ----------
    accepted_views:
        Sequence of dicts with ``view_id``, ``pose`` (R 3x3, t in mm),
        ``n_markers``, ``quality``, ``image_points``, ``shared_markers_ir``,
        ``assignment_ok``, ``timestamp``.  Missing optional keys degrade the
        relevant dimension instead of raising.
    spec:
        :class:`CoverageSpec`; the defaults are the architecture §6 values.

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
    metrics: list[dict] = []
    view_of: list[Mapping[str, Any]] = []
    unusable: list[dict] = []
    for v in views:
        m = pose_metrics(v)
        if m is None:
            unusable.append({"view_id": v.get("view_id"), "reason": "no usable pose"})
            continue
        metrics.append(m)
        view_of.append(v)

    n = len(metrics)
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
    q_rows = []
    for v in view_of:
        q = _quality(v)
        sat = q.get("saturation_frac", q.get("sat_frac"))
        foc = q.get("focus", q.get("blur"))
        ok = True
        if sat is not None:
            ok = ok and float(sat) <= float(spec.max_saturation_frac)
        if foc is not None:
            ok = ok and float(foc) >= float(spec.min_focus)
        q_rows.append({"view_id": v.get("view_id"), "saturation_frac": sat,
                       "focus": foc, "ok": bool(ok)})
    n_q_ok = int(sum(1 for r in q_rows if r["ok"]))
    q_frac = (n_q_ok / n) if n else 0.0
    quality_ok = n > 0 and q_frac >= float(spec.min_quality_fraction)

    speeds, rates = [], []
    order = sorted(range(n), key=lambda i: (_timestamp(view_of[i]) is None,
                                            _timestamp(view_of[i]) or 0.0))
    for a, b in zip(order, order[1:]):
        ta, tb = _timestamp(view_of[a]), _timestamp(view_of[b])
        if ta is None or tb is None or tb <= ta:
            continue
        dt = tb - ta
        dtrans = float(np.linalg.norm(metrics[b]["t"] - metrics[a]["t"]))
        drot = _rotation_angle_deg(metrics[a]["R"], metrics[b]["R"])
        speeds.append(dtrans / dt)
        rates.append(drot / dt)
    sp = np.array(speeds, float) if speeds else np.zeros(0, float)
    rt = np.array(rates, float) if rates else np.zeros(0, float)
    stationary_flags = []
    for i, v in enumerate(view_of):
        explicit = v.get("stationary")
        if explicit is not None:
            stationary_flags.append(bool(explicit))
        elif len(sp):
            k = max(0, i - 1)
            stationary_flags.append(bool(sp[min(k, len(sp) - 1)] <= spec.stationary_speed_mm_s
                                         and rt[min(k, len(rt) - 1)] <= spec.stationary_rate_deg_s))
        else:
            stationary_flags.append(False)
    n_stationary = int(sum(stationary_flags))
    stationary_ok = n_stationary >= int(spec.min_stationary_views)
    d7_sat = quality_ok and stationary_ok
    d7_deficit = {"quality": max(0, int(math.ceil(spec.min_quality_fraction * n)) - n_q_ok),
                  "stationary_views": max(0, int(spec.min_stationary_views) - n_stationary)}
    d7_hint = ("Quality and dwell are within limits." if d7_sat else
               (f"Hold each pose still for ~0.3 s: only {n_stationary} of {n} views are "
                f"below {spec.stationary_speed_mm_s:.0f} mm/s and "
                f"{spec.stationary_rate_deg_s:.1f} deg/s (need "
                f"{spec.min_stationary_views})."
                if not stationary_ok else
                f"Quality: {n - n_q_ok} of {n} accepted views exceed "
                f"{spec.max_saturation_frac:.1%} saturation or fall below "
                f"focus {spec.min_focus:.0f}."))
    checks["stationarity"] = _check(
        "stationarity / quality",
        {"min_stationary_views": int(spec.min_stationary_views),
         "stationary_speed_mm_s": spec.stationary_speed_mm_s,
         "stationary_rate_deg_s": spec.stationary_rate_deg_s,
         "min_quality_fraction": spec.min_quality_fraction,
         "max_saturation_frac": spec.max_saturation_frac,
         "min_focus": spec.min_focus},
        {"n_stationary": n_stationary, "n_quality_ok": n_q_ok,
         "quality_fraction": q_frac,
         "speed_p50_mm_s": float(np.median(sp)) if len(sp) else None,
         "speed_p95_mm_s": float(np.percentile(sp, 95)) if len(sp) else None,
         "rate_p50_deg_s": float(np.median(rt)) if len(rt) else None,
         "rate_p95_deg_s": float(np.percentile(rt, 95)) if len(rt) else None},
        d7_sat, d7_deficit, d7_hint)
    dims["D7"] = {"id": "D7", "label": "stationarity / quality",
                  "required": checks["stationarity"]["required"],
                  "measured": checks["stationarity"]["measured"],
                  "satisfied": d7_sat, "deficit": d7_deficit, "hint": d7_hint,
                  "detail": {"views": q_rows, "stationary": stationary_flags,
                             "speed_mm_s": sp.tolist(), "rate_deg_s": rt.tolist()},
                  "checks": ["stationarity"]}

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
    stable, stability = _stability(metrics, view_of, spec)

    out = {
        "n_views": len(views),
        "n_usable": n,
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
    verdict = readiness(out)
    out["ready"] = bool(verdict["ready"])
    out["summary"] = verdict["summary"]
    if n > 0 and n == len(views):
        # Compatibility block for ``WizardController._external_coverage``
        # (agent C): eight dimension keys plus ``ready``.  It is only emitted
        # when every entry was usable — i.e. when the numbers describe the real
        # geometry.  If the caller supplied entries without a pose the
        # controller must fall back to its own bookkeeping rather than display
        # a measurement that was never taken.
        out["dimensions"] = {
            "distance_bands": dims["D1"],
            "tilt_two_axes": dims["D2"],
            "roll_sectors": dims["D3"],
            "image_regions": dims["D4"],
            "stereo_overlap": dims["D5"],
            "colour_participation": dims["D6"],
            "quality": _sub_dimension(dims["D7"], "quality", n_q_ok, n,
                                      float(spec.min_quality_fraction) * n),
            "stationarity": _sub_dimension(dims["D7"], "stationarity",
                                           n_stationary,
                                           int(spec.min_stationary_views),
                                           int(spec.min_stationary_views)),
            "scale_artefact": dims["D8"],
        }
    return out


def _sub_dimension(parent: Mapping[str, Any], name: str, value: float,
                   measured_of: float, required: float) -> dict:
    """One sub-check of a composite dimension, shaped like a dimension."""
    node = dict(parent)
    node["id"] = f"{parent['id']}.{name}"
    node["label"] = name
    if name == "quality":
        node["satisfied"] = bool(measured_of > 0 and value >= required)
        node["measured"] = {"n_quality_ok": value, "n_views": measured_of,
                            "required_fraction": (required / measured_of
                                                  if measured_of else None)}
        node["required"] = {"min_quality_fraction": (required / measured_of
                                                     if measured_of else None)}
    else:
        node["satisfied"] = bool(value >= required)
        node["measured"] = {"n_stationary": value}
        node["required"] = {"min_stationary_views": required}
    node["hint"] = parent.get("hint", "")
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
                    "last_speed_mm_s": speed, "last_rate_deg_s": rate}


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


def _families(measurement: Mapping[str, Any], spec: CoverageSpec,
              current: dict | None) -> list[dict]:
    """Rank candidate pose families for the unsatisfied checks."""
    checks = measurement.get("checks") or {}
    nx = len(spec.distance_bands_mm)
    families: list[dict] = []

    band_missing = [i for i in range(nx)
                    if not (checks.get("distance_bands", {}).get("measured", {})
                            .get("band_counts", [0] * nx)[i] >= spec.min_views_per_distance_band)]
    band_counts = (checks.get("distance_bands", {}).get("measured", {})
                   .get("band_counts", [0] * nx))
    if not band_missing:
        # distance is done; still offer a mid band as the carrier of other moves
        band_missing = [min(range(nx), key=lambda i: (abs(_band_centre(spec, i)
                                                          - (current or {}).get("distance_mm", 0.0))))]
    tilt_x_missing = not checks.get("tilt_axis_x", {}).get("satisfied", True)
    tilt_y_missing = not checks.get("tilt_axis_y", {}).get("satisfied", True)
    quad_missing = not checks.get("tilt_quadrants", {}).get("satisfied", True)
    roll_missing = not checks.get("roll_sectors", {}).get("satisfied", True)
    region_missing = not checks.get("image_regions", {}).get("satisfied", True)
    corner_missing = not checks.get("image_corners", {}).get("satisfied", True)

    tx_choices = [0.0]
    ty_choices = [0.0]
    stats = measurement.get("pose_stats") or {}
    med_x = float(stats.get("tilt_x_centre_deg") or 0.0)
    med_y = float(stats.get("tilt_y_centre_deg") or 0.0)
    exc = float(spec.min_tilt_deg) + 5.0
    if tilt_x_missing or quad_missing:
        tx_choices = [med_x - exc, med_x + exc]
    if tilt_y_missing or quad_missing:
        ty_choices = [med_y - exc, med_y + exc]
    if not (tilt_x_missing or tilt_y_missing or quad_missing):
        tx_choices = [float((current or {}).get("tilt_x_deg", med_x))]
        ty_choices = [float((current or {}).get("tilt_y_deg", med_y))]

    roll_choices = [0.0]
    if roll_missing:
        counts = checks.get("roll_sectors", {}).get("measured", {}).get("bin_counts", [])
        size = spec.roll_bin_size_deg()
        roll_choices = [((i + 0.5) * size) % 360.0 for i, c in enumerate(counts)
                        if c < spec.min_views_per_roll_bin] or [0.0]

    region_choices = [(None, None)]
    if corner_missing and not region_missing:
        region_choices = [(0, 0), (0, 2), (2, 0), (2, 2)]
    elif region_missing:
        region_choices = [(row, col) for row in range(3) for col in range(3)
                          if (row, col) != (1, 1)]   # centre is where the plate is
        if not region_choices:
            region_choices = [(None, None)]

    for b in band_missing:
        for tx in tx_choices:
            for ty in ty_choices:
                for roll in roll_choices:
                    for (row, col) in region_choices:
                        # lateral offset that puts the plate centre in a region
                        lat = (0.0, 0.0)
                        if row is not None:
                            lat = (((col - 1) * 0.30) * _band_centre(spec, b),
                                   ((row - 1) * 0.24) * _band_centre(spec, b))
                        R, t = family_pose(_band_centre(spec, b), tx, ty, roll, lat)
                        fam = {"distance_band": b,
                               "distance_mm": _band_centre(spec, b),
                               "tilt_x_deg": float(tx), "tilt_y_deg": float(ty),
                               "roll_deg": float(roll),
                               "image_region": None if row is None else [int(row), int(col)],
                               "lateral_mm": [float(lat[0]), float(lat[1])],
                               "R": R, "t": t,
                               "unlocks": _unlocks(band_counts[b] if b < len(band_counts) else 0,
                                                   b, tilt_x_missing, tilt_y_missing,
                                                   quad_missing, tx, ty,
                                                   roll_missing, roll,
                                                   region_missing, corner_missing,
                                                   row, col)}
                        if current is not None:
                            eff = pose_effort((current["R"], current["t"]), (R, t))
                        else:
                            eff = {"score": 0.0, "lateral_mm": 0.0, "depth_mm": 0.0,
                                   "rotation_deg": 0.0, "move_mm": [0.0, 0.0, 0.0]}
                        fam["effort"] = eff
                        families.append(fam)
    # lexicographic: nearest family first, then a deterministic family order
    families.sort(key=lambda f: (round(float(f["effort"]["score"]), 6),
                                 f["distance_band"], f["tilt_x_deg"],
                                 f["tilt_y_deg"], f["roll_deg"],
                                 -1 if f["image_region"] is None else f["image_region"][0],
                                 -1 if f["image_region"] is None else f["image_region"][1]))
    return families


def _unlocks(band_count: int, b: int, tx_missing: bool,
             ty_missing: bool, quad_missing: bool, tx: float, ty: float,
             roll_missing: bool, roll: float, region_missing: bool,
             corner_missing: bool, row: int | None, col: int | None) -> list[str]:
    out = [f"distance band {b}"]
    if quad_missing or tx_missing or ty_missing:
        out.append(f"tilt ({'+' if tx >= 0 else '-'}x,{'+' if ty >= 0 else '-'}y)")
    if roll_missing:
        out.append(f"roll sector {int((roll % 360) // 90)}")
    if (region_missing or corner_missing) and row is not None:
        out.append(f"image region {row},{col}")
    return out


def _instruction(spec: CoverageSpec, fam: Mapping[str, Any], current: dict | None) -> str:
    """Short operator sentence for one pose family (deltas when known)."""
    lo, hi = spec.distance_bands_mm[fam["distance_band"]]
    parts = [f"Move to {lo:.0f}-{hi:.0f} mm"]
    if current is not None:
        d = fam["distance_mm"] - current["distance_mm"]
        if abs(d) >= 10.0:
            parts[0] += f" ({'closer' if d < 0 else 'further'}, {abs(d):.0f} mm)"
    dtx = fam["tilt_x_deg"] - (current["tilt_x_deg"] if current else 0.0)
    dty = fam["tilt_y_deg"] - (current["tilt_y_deg"] if current else 0.0)
    tilts = []
    if abs(dtx) >= 2.0:
        tilts.append(f"{abs(dtx):.0f} deg {'left' if dtx < 0 else 'right'}")
    if abs(dty) >= 2.0:
        tilts.append(f"{abs(dty):.0f} deg {'down' if dty < 0 else 'up'}")
    if tilts:
        parts.append("tilt " + " and ".join(tilts))
    droll = fam["roll_deg"] - (current["roll_deg"] if current else 0.0)
    droll = (droll + 180.0) % 360.0 - 180.0
    if abs(droll) >= 5.0:
        parts.append(f"roll the plate by {abs(droll):.0f} deg "
                     f"{'clockwise' if droll < 0 else 'anticlockwise'}")
    reg = fam["image_region"]
    if reg is not None:
        rname = ["top", "middle", "bottom"][reg[0]]
        cname = ["left", "centre", "right"][reg[1]]
        parts.append(f"put the plate centre in the {rname}-{cname} of the image")
    return ", ".join(parts) + "."


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

    families = _families(measurement, spec, cur)
    if ready["ready"] and not families:
        return {"mode": "done",
                "instruction": "Coverage complete — stop when the uncertainty "
                               "gate is also satisfied.",
                "target_pose": None, "target_R": None, "target_t": None,
                "family": None, "families": [], "unlocks": [],
                "effort": None, "precise": False,
                "reason": "all dimensions satisfied",
                "readiness": ready["summary"]}
    if ready["ready"]:
        return {"mode": "done",
                "instruction": "Coverage complete — the remaining work is the "
                               "uncertainty gate, not more motion.",
                "target_pose": None, "target_R": None, "target_t": None,
                "family": families[0], "families": families[:8], "unlocks": [],
                "effort": families[0].get("effort"), "precise": False,
                "reason": "all dimensions satisfied",
                "readiness": ready["summary"]}
    if not families:
        return {"mode": "hold", "instruction": "No admissible next family — "
                "check the plate visibility.", "target_pose": None,
                "target_R": None, "target_t": None, "family": None,
                "families": [], "unlocks": [], "effort": None, "precise": False,
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
