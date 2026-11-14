"""Operator-facing guidance: frame quality, instructions, and acceptance.

The calibration session must decide *whether a captured frame set is worth
keeping* and *what the operator should do next*.  This module contains the
pure, testable parts of that decision:

* :class:`FrameQuality` / :func:`evaluate_frame` -- focus, exposure, motion
  blur, marker visibility, field coverage and camera synchronisation;
* :func:`instruction` -- turn a target board pose into a short, intuitive
  movement instruction (move left/right, closer/farther, tilt up/down, rotate
  the plate);
* :func:`reachable` -- the candidate viewpoints an operator can reach from the
  current pose with one natural movement;
* :func:`acceptance` -- the accept/reject rule with explicit reasons.

The session state machine that ties these together is :mod:`calib.session`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence

import cv2
import numpy as np

from . import plate
from .adaptive import ViewCandidate, board_pose
from .calibrate import CAMERAS, CameraIntrinsics, Observation
from .geometry import solve_pnp

# ---------------------------------------------------------------------------
# quality
# ---------------------------------------------------------------------------

# Thresholds measured on the 75-image vendor corpus (analysis/calibration/
# engine/corpus_quality.json).  They sit below the corpus 1st percentile with
# margin: IR focus >= 261 (median 307), IR contrast >= 19 (median 37), tex
# focus >= 95, tex contrast >= 7.  Marker counts go down to 19 in the left
# camera at the closest distance (the plate leaves the left field), so the
# stereo *overlap* is the meaningful gate, not the per-camera count.
DEFAULT_QUALITY = {
    "min_markers": 16,              # per camera (corpus minimum 19)
    "min_stereo_markers": 16,       # common left/right markers
    "min_big_markers": 3,           # of the 3 on-lattice large markers
    "min_plate_area_frac": 0.03,    # corpus minimum 0.04
    "max_plate_area_frac": 0.92,
    "max_saturation_frac": 0.02,    # corpus maximum 0.01
    "min_contrast": {"left": 14.0, "right": 14.0, "tex": 5.0},
    "min_focus": {"left": 150.0, "right": 150.0, "tex": 55.0},
    "max_sync_ms": 3.0,
    # Independent left/right PnP must imply the *same* rig.  A rigid relabel
    # of one camera's lattice (a sub-lattice shift, an orientation slip) fits
    # each camera's own reprojection but breaks the left-right correspondence;
    # the gross disagreement is caught here before it can enter the solver.
    "max_stereo_pose_deg": 4.0,
    "max_stereo_pose_mm": 20.0,
    "min_dwell_s": 0.25,
    "max_motion_mm": 3.0,           # dwell threshold: reported, not a rejection
    "max_motion_deg": 1.5,
    # A single frame is sharp or it is not; motion *between* frames does not
    # blur a 1.2 ms exposure.  Rejection is therefore driven by speed and
    # exposure blur with an honest dt, not by an absolute inter-decision
    # displacement.  The old absolute gate rejected 287 decisions on the live
    # backlog because dt was 0.5-15 s: 223 mm/17 deg between two seconds is
    # ordinary walking speed, not a pose failure.
    #
    # ``max_motion_reject_*`` survives only as a *pose-discontinuity sanity
    # check for genuinely adjacent samples* (the 10 Hz raw stream), where a
    # 4 cm / 8 deg jump in one 100 ms frame indicates a tracking/association
    # problem rather than hand motion.  It is never applied across a sparse
    # decision gap.
    "max_motion_reject_mm": 40.0,
    "max_motion_reject_deg": 8.0,
    "max_adjacent_pair_dt_s": 0.15,
    # Physical blur during the exposure, not motion between frames:
    # blur_mm = board speed (mm/s) * exposure time (s).  A 1.2 ms IR exposure
    # tolerates ordinary hand motion; a 5 ms colour exposure needs a slower
    # hand or a shorter exposure.  Both thresholds are conservative.
    "exposure_s": 0.0012,
    "max_motion_blur_mm": 0.30,
    "max_motion_blur_px": 0.45,
}


@dataclass
class FrameQuality:
    passed: bool
    reasons: list
    metrics: dict
    pose: Optional[tuple] = None      # (R, t) board -> left camera
    n_markers: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"passed": self.passed, "reasons": list(self.reasons),
                "metrics": {k: (float(v) if isinstance(v, (int, float, np.floating))
                                else v) for k, v in self.metrics.items()},
                "n_markers": self.n_markers}


def _roi(gray: np.ndarray, uv: np.ndarray, pad: int = 24):
    h, w = gray.shape[:2]
    if uv is None or len(uv) == 0:
        return gray
    x0 = max(0, int(np.floor(uv[:, 0].min())) - pad)
    x1 = min(w, int(np.ceil(uv[:, 0].max())) + pad)
    y0 = max(0, int(np.floor(uv[:, 1].min())) - pad)
    y1 = min(h, int(np.ceil(uv[:, 1].max())) + pad)
    if x1 - x0 < 8 or y1 - y0 < 8:
        return gray
    return gray[y0:y1, x0:x1]


def focus_metric(gray: np.ndarray, uv: Optional[np.ndarray] = None) -> float:
    roi = _roi(gray, uv)
    return float(cv2.Laplacian(roi.astype(np.float32), cv2.CV_32F).var())


def evaluate_frame(images: dict[str, np.ndarray],
                   grids: dict[str, Observation],
                   timestamp: float = 0.0,
                   previous_pose: Optional[tuple] = None,
                   previous_timestamp: Optional[float] = None,
                   camera_timestamps: Optional[dict[str, float]] = None,
                   quality: Optional[dict] = None,
                   cameras: Optional[dict[str, CameraIntrinsics]] = None,
                   rig: Optional[dict[str, tuple]] = None,
                   require_images: bool = True,
                   optional_cameras: Optional[set] = None) -> FrameQuality:
    """Assess one three-camera frame set.

    ``grids`` maps camera -> Observation with the ordered (u,v) markers.
    A frame is accepted only if *every* camera that will contribute has enough
    visible markers, the plate is inside the field, the exposure is sane and
    the set is temporally coherent.  The board pose is estimated from the left
    camera (fallback: right), which is what guidance and dwell gating need.
    """
    q = dict(DEFAULT_QUALITY)
    if quality:
        q.update(quality)
    reasons: list[str] = []
    metrics: dict = {}
    n_markers: dict = {}
    uv_by_cam: dict[str, np.ndarray] = {}

    optional = set(optional_cameras or ())
    for c in CAMERAS:
        o = grids.get(c)
        if o is None or not o.uv:
            n_markers[c] = 0
            if c in optional:
                # this camera simply does not see the board from this pose
                # (e.g. the colour sensor at an image corner).  The view still
                # constrains the cameras that do see it; overall colour
                # participation is enforced separately by the coverage gate.
                metrics[f"{c}.active"] = 0.0
                continue
            reasons.append(f"{c}: no markers detected")
            continue
        metrics[f"{c}.active"] = 1.0
        uv = np.asarray(list(o.uv.values()), float)
        uv_by_cam[c] = uv
        n_markers[c] = int(len(uv))
        if len(uv) < q["min_markers"]:
            reasons.append(f"{c}: only {len(uv)} markers (need {q['min_markers']})")
        img = images.get(c)
        if img is None:
            if require_images:
                reasons.append(f"{c}: no image")
            continue
        area_frac = float(cv2.contourArea(cv2.convexHull(uv.astype(np.float32)))) / img.size
        metrics[f"{c}.area_frac"] = area_frac
        if area_frac < q["min_plate_area_frac"]:
            reasons.append(f"{c}: plate too small ({area_frac:.3f})")
        if area_frac > q["max_plate_area_frac"]:
            reasons.append(f"{c}: plate clipped ({area_frac:.3f})")
        sat = float(np.mean(img >= 250))
        metrics[f"{c}.saturation"] = sat
        if sat > q["max_saturation_frac"]:
            reasons.append(f"{c}: saturated ({sat:.3f})")
        roi = _roi(img, uv)
        metrics[f"{c}.contrast"] = float(np.percentile(roi, 95) - np.percentile(roi, 5))
        cmin = q["min_contrast"]
        cmin = cmin.get(c, 10.0) if isinstance(cmin, dict) else float(cmin)
        if metrics[f"{c}.contrast"] < cmin:
            reasons.append(f"{c}: low contrast ({metrics[f'{c}.contrast']:.1f} < {cmin})")
        f = focus_metric(img, uv)
        metrics[f"{c}.focus"] = f
        fmin = q["min_focus"]
        fmin = fmin.get(c, 50.0) if isinstance(fmin, dict) else float(fmin)
        if img is not None and f < fmin:
            reasons.append(f"{c}: defocused ({f:.1f} < {fmin})")

    # stereo overlap: markers seen by both IR cameras (the pair that triangulates)
    if "left" in uv_by_cam and "right" in uv_by_cam:
        gl = grids["left"]; gr = grids["right"]
        common = len(set(gl.uv) & set(gr.uv)) if isinstance(gl.uv, dict) else 0
        metrics["stereo_markers"] = common
        if common < q["min_stereo_markers"]:
            reasons.append(f"stereo overlap {common} < {q['min_stereo_markers']}")

    # large markers: the three on-lattice orientation markers must be present
    big_uv = {(2.0, 1.0), (4.0, 1.0), (5.0, 1.0)}
    left = grids.get("left")
    n_big = 0
    if left is not None:
        n_big = sum(1 for k in left.uv if (float(k[0]), float(k[1])) in big_uv)
    metrics["big_markers"] = n_big
    if n_big < q["min_big_markers"]:
        reasons.append(f"large markers {n_big}/3")

    # synchronisation: the three cameras must belong to the same instant
    if camera_timestamps:
        ts = [camera_timestamps.get(c) for c in CAMERAS if camera_timestamps.get(c) is not None]
        if len(ts) >= 2:
            skew = 1000.0 * (max(ts) - min(ts))
            metrics["sync_skew_ms"] = skew
            if skew > q["max_sync_ms"]:
                reasons.append(f"cameras not synchronised ({skew:.1f} ms)")

    # board pose from the left camera (or right if left is missing)
    pose = None
    pnp: dict[str, object] = {}
    obj = plate.board_points_mm()
    uv_idx = [(float(u), float(v)) for (u, v) in plate.lattice_uv()]
    for c in ("left", "right"):
        o = grids.get(c)
        if o is None or len(o.uv) < 8:
            continue
        cam = (cameras or {}).get(c)
        if cam is None:
            cam = CameraIntrinsics(1150.0, 1150.0, 640.0, 512.0, np.zeros(5))
        keys = [k for k in uv_idx if k in o.uv]
        if len(keys) < 6:
            continue
        obj_pts = np.asarray([obj[uv_idx.index(k)] for k in keys])
        img_pts = np.asarray([o.uv[k] for k in keys], float)
        p = solve_pnp(obj_pts, img_pts, cam.K, cam.kc)
        if p is None:
            continue
        pnp[c] = p
        if pose is not None:
            continue
        # Board-pose convention normalisation.  The detector's lattice
        # assignment is mirrored in v relative to the vendor lattice frame
        # (the calibration is invariant to it: it is a global 180 deg rotation
        # of every board pose).  Pose-space guidance must compare like with
        # like, so flip the pose into the convention used by
        # adaptive.board_pose (board +z towards the camera).
        R_pose = p.R @ np.diag([1.0, -1.0, -1.0]) if p.R[2, 2] > 0 else p.R
        pose = (R_pose, p.t)
        if c == "right" and rig and "right" in rig:
            # right-camera pose -> left camera frame: X_r = R_rl X_l + t_rl
            R_rl, t_rl = rig["right"]
            pose = (R_rl.T @ p.R, R_rl.T @ (p.t - t_rl))
    if pose is None:
        reasons.append("no board pose")
    metrics["pose_ok"] = pose is not None

    # Stereo rig consistency: both IR cameras must imply the same left->right
    # transform.  A per-camera lattice slip is invisible to each camera's own
    # reprojection but shows up here as a gross translation/rotation error.
    if "left" in pnp and "right" in pnp and rig and "right" in rig:
        try:
            Rl = np.asarray(pnp["left"].R, float)
            tl = np.asarray(pnp["left"].t, float).reshape(3)
            Rr = np.asarray(pnp["right"].R, float)
            tr = np.asarray(pnp["right"].t, float).reshape(3)
            R_est = Rr @ Rl.T
            t_est = tr - R_est @ tl
            R_rig, t_rig = rig["right"]
            R_rig = np.asarray(R_rig, float)
            t_rig = np.asarray(t_rig, float).reshape(3)
            cos = (np.trace(R_est @ R_rig.T) - 1.0) / 2.0
            ang = float(np.degrees(np.arccos(np.clip(cos, -1.0, 1.0))))
            trans = float(np.linalg.norm(t_est - t_rig))
            metrics["stereo_pose_angle_deg"] = ang
            metrics["stereo_pose_translation_mm"] = trans
            if (ang > float(q["max_stereo_pose_deg"])
                    or trans > float(q["max_stereo_pose_mm"])):
                reasons.append(
                    f"stereo pose inconsistency ({ang:.2f} deg / "
                    f"{trans:.2f} mm vs the known rig); one camera's marker "
                    "identity is inconsistent with the other")
        except Exception:
            pass

    # dwell / motion: the board must be nearly stationary between frames
    if pose is not None and previous_pose is not None:
        R0, t0 = previous_pose
        C0 = -np.asarray(R0).T @ np.asarray(t0)
        C1 = -np.asarray(pose[0]).T @ np.asarray(pose[1])
        d = float(np.linalg.norm(C1 - C0))
        ang = float(np.degrees(np.arccos(np.clip(
            (np.trace(np.asarray(pose[0]) @ np.asarray(R0).T) - 1) / 2, -1, 1))))
        # mm of board-centre motion per frame; at 400 mm, 1.5 px ~ 0.5 mm
        metrics["motion_mm"] = d
        metrics["motion_deg"] = ang
        dt = None
        if previous_timestamp is not None and timestamp > previous_timestamp:
            dt = float(timestamp - previous_timestamp)
        speed = rate_deg_s = None
        if dt is not None and dt > 1e-6:
            speed = d / dt
            rate_deg_s = ang / dt
            exposure_s = float(q.get("exposure_s", 0.0012) or 0.0012)
            metrics["speed_mm_s"] = speed
            metrics["stationarity_speed_mm_s"] = speed
            metrics["stationarity_rate_deg_s"] = rate_deg_s
            metrics["stationarity_window_s"] = dt
            metrics["exposure_s"] = exposure_s
            # This is a *measured* acquisition-quality observation, not an
            # acceptance verdict: D7 counts these over every ingested frame.
            metrics["stationary"] = bool(speed <= float(q["max_motion_mm"])
                                         and rate_deg_s <= float(q["max_motion_deg"]))
            blur_mm = speed * exposure_s
            # Projected blur: f * blur / depth (small-angle approximation).
            f_px = 1150.0
            depth_mm = max(50.0, float(abs(pose[1][2])))
            blur_px = f_px * blur_mm / depth_mm
            metrics["motion_blur_mm"] = blur_mm
            metrics["motion_blur_px"] = blur_px
            # The metric is always reported, but it *rejects* only for samples
            # that are genuinely adjacent to the stream cadence.  Across a
            # sparse decision gap (0.3-1.5 s), a fast inter-frame displacement
            # is not evidence that the 1.2 ms exposure was blurred: a
            # deliberate still taken after a quick reposition was rejected 28
            # times on the real curated replay, exactly when the operator
            # needed a new coverage sector.  Sharpness itself is still gated by
            # focus and contrast on the image.
            if (dt <= float(q["max_adjacent_pair_dt_s"])
                    and (blur_mm > float(q["max_motion_blur_mm"])
                         or blur_px > float(q["max_motion_blur_px"]))):
                reasons.append(
                    f"motion blur ({blur_mm:.2f} mm / {blur_px:.2f} px at "
                    f"{speed:.0f} mm/s and {exposure_s * 1e3:.1f} ms exposure)")
            # Absolute displacement is a sanity guard only for genuinely
            # adjacent samples.  A large legal hand movement over seconds is
            # not a reliability problem when each frame has its own pose.
            if dt <= float(q["max_adjacent_pair_dt_s"]) and (
                    d > float(q["max_motion_reject_mm"])
                    or ang > float(q["max_motion_reject_deg"])):
                reasons.append(
                    f"pose discontinuity between adjacent frames "
                    f"({d:.1f} mm, {ang:.2f} deg in {dt:.3f} s)")
            elif d > float(q["max_motion_mm"]) or ang > float(q["max_motion_deg"]):
                # Informational and, importantly, still sharp: the frame is
                # kept.  Stationarity is D7's business; this is not a reject.
                metrics["motion_warning"] = True
        elif d > float(q["max_motion_mm"]) or ang > float(q["max_motion_deg"]):
            # dt is unknown: report motion but never turn it into a rejection.
            metrics["motion_warning"] = True
            metrics["motion_dt_unknown"] = True
    if previous_timestamp is not None:
        metrics["dt_s"] = float(timestamp - previous_timestamp)

    passed = len(reasons) == 0
    return FrameQuality(passed, reasons, metrics, pose, n_markers)


# ---------------------------------------------------------------------------
# operator instruction
# ---------------------------------------------------------------------------

def _axis_text(v: float, pos: str, neg: str, unit: str = "cm", scale: float = 0.1,
               dead: float = 0.8) -> Optional[str]:
    if abs(v) < dead:
        return None
    return f"{pos if v > 0 else neg} {abs(v) * scale:.1f} {unit}".replace(".0 ", " ")


def instruction(current: tuple, target: tuple) -> dict:
    """Human-readable move from the current to the target board pose.

    Both poses are ``(R, t)`` board -> left camera.  The required *camera*
    motion is expressed in the current camera frame (x right, y down,
    z forward), which is what the operator does when holding the scanner:
    move left/right/up/down, closer/farther, and orbit around the plate.
    """
    Rc, tc = current
    Rt, tt = target
    Oc = -np.asarray(Rc).T @ np.asarray(tc)
    Ot = -np.asarray(Rt).T @ np.asarray(tt)
    d_board = Ot - Oc
    d_cam = np.asarray(Rc) @ d_board            # camera-frame translation
    # relative camera rotation, expressed in the current camera frame
    R_rel_board = np.asarray(Rt).T @ np.asarray(Rc)
    R_rel_cam = np.asarray(Rc) @ R_rel_board @ np.asarray(Rc).T
    # small-angle instruction: Rodrigues components are only intuitive below
    # ~60 deg; above that describe the turn by its total angle
    angle_total = float(np.degrees(np.arccos(np.clip(
        (np.trace(R_rel_cam) - 1) / 2, -1, 1))))
    rvec = cv2.Rodrigues(R_rel_cam)[0].ravel()
    rdeg = np.degrees(rvec)
    if angle_total > 60.0:
        rdeg = rdeg * (angle_total / max(np.linalg.norm(rdeg), 1e-9)) * 0.0
        rdeg[2] = angle_total if rdeg[2] >= 0 else -angle_total
    parts = []
    for i, (pos, neg) in enumerate((("right", "left"), ("down", "up"), ("forward", "backward"))):
        txt = _axis_text(float(d_cam[i]), pos, neg)
        if txt:
            parts.append(txt)
    if abs(rdeg[0]) > 3.0:
        parts.append(f"tilt {'down' if rdeg[0] > 0 else 'up'} {abs(rdeg[0]):.0f} deg")
    if abs(rdeg[1]) > 3.0:
        parts.append(f"yaw {'right' if rdeg[1] > 0 else 'left'} {abs(rdeg[1]):.0f} deg")
    if abs(rdeg[2]) > 12.0:
        what = "rotate the plate" if angle_total > 60.0 else "roll"
        parts.append(f"{what} {abs(rdeg[2]):.0f} deg")
    if not parts:
        parts.append("hold still")
    return {"text": ", ".join(parts), "move_mm": [float(x) for x in d_cam],
            "rotate_deg": [float(x) for x in rdeg],
            "distance_change_mm": float(tt[2] - tc[2]),
            "effort": float(np.linalg.norm(d_cam[:2]) / 150.0
                            + abs(d_cam[2]) / 150.0
                            + np.linalg.norm(rdeg) / 25.0 + 0.15)}


def reachable(current: Optional[tuple], pool: Sequence[ViewCandidate],
              max_lateral_mm: float = 120.0, max_depth_mm: float = 140.0,
              max_angle_deg: float = 28.0) -> list[ViewCandidate]:
    """Candidate poses the operator can reach with one natural movement."""
    if current is None:
        return list(pool)
    Rc, tc = current
    Oc = -np.asarray(Rc).T @ np.asarray(tc)
    out = []
    for c in pool:
        Ot = -np.asarray(c.R).T @ np.asarray(c.t)
        dcam = np.asarray(Rc) @ (Ot - Oc)
        ang = np.degrees(np.arccos(np.clip(
            (np.trace(np.asarray(c.R) @ np.asarray(Rc).T) - 1) / 2, -1, 1)))
        if (np.linalg.norm(dcam[:2]) <= max_lateral_mm
                and abs(dcam[2]) <= max_depth_mm and ang <= max_angle_deg):
            out.append(c)
    return out


# ---------------------------------------------------------------------------
# acceptance rule
# ---------------------------------------------------------------------------

DEFAULT_ACCEPT = {
    "min_rel_gain": 0.004,        # predicted relative task-variance reduction
    "min_rel_gain_new_bin": 0.001,  # a new distance/tilt bin is worth more
    "min_pose_separation_deg": 1.5,
    "min_pose_separation_mm": 8.0,
    "max_duplicate_angle_deg": 1.2,
    "max_duplicate_distance_mm": 6.0,
}


def quality_stationarity(quality: FrameQuality,
                        speed_mm_s: float = 3.0,
                        rate_deg_s: float = 1.5) -> Optional[dict]:
    """Short-term stationarity from one ``FrameQuality`` record.

    This is the same observation :mod:`tools.wizard.coverage` consumes: a
    per-frame measured speed/rate over an honest ``dt``.  It is ``None`` when
    the frame has no time base, so a missing measurement can never be turned
    into a dwell verdict.
    """
    metrics = getattr(quality, "metrics", {}) or {}
    speed = metrics.get("stationarity_speed_mm_s", metrics.get("speed_mm_s"))
    rate = metrics.get("stationarity_rate_deg_s")
    window = metrics.get("stationarity_window_s", metrics.get("dt_s"))
    if rate is None:
        motion_deg = metrics.get("motion_deg")
        if motion_deg is not None and window:
            rate = float(motion_deg) / float(window)
    explicit = metrics.get("stationary")
    if (speed is None or rate is None) and explicit is not None:
        return {"stationary": bool(explicit), "speed_mm_s": speed,
                "rate_deg_s": rate, "window_s": window, "source": "explicit"}
    if speed is None or rate is None:
        return None
    return {"stationary": bool(float(speed) <= float(speed_mm_s)
                              and float(rate) <= float(rate_deg_s)),
            "speed_mm_s": float(speed), "rate_deg_s": float(rate),
            "window_s": (float(window) if window is not None else None),
            "source": "recorded_motion"}


def acceptance(quality: FrameQuality,
               predicted_rel_gain: Optional[float],
               nearest_pose: Optional[tuple] = None,
               pose: Optional[tuple] = None,
               new_coverage_bin: bool = False,
               policy: Optional[dict] = None,
               admit_dwell: bool = False) -> dict:
    """Accept/reject one frame set, with every reason made explicit.

    ``admit_dwell`` is the controller opt-in for D7: when the frame is a good
    quality stationary duplicate, it may be admitted as ``dwell=True`` evidence
    instead of being discarded as redundant.  The caller must keep such views
    out of the solver (``tools.wizard.coverage.measure`` excludes ``dwell=True``
    from D1-D6) and pass them to D7.  The default remains ``False`` so existing
    callers are unchanged.
    """
    p = dict(DEFAULT_ACCEPT)
    if policy:
        p.update(policy)
    reasons: list[str] = []
    if not quality.passed:
        reasons.extend(quality.reasons)
    gain = 0.0 if predicted_rel_gain is None else float(predicted_rel_gain)
    need = p["min_rel_gain_new_bin"] if new_coverage_bin else p["min_rel_gain"]
    if predicted_rel_gain is not None and gain < need:
        reasons.append(f"redundant view (predicted gain {gain * 100:.2f}% < {need * 100:.2f}%)")
    if pose is not None and nearest_pose is not None:
        R0, t0 = nearest_pose
        ang = np.degrees(np.arccos(np.clip(
            (np.trace(np.asarray(pose[0]) @ np.asarray(R0).T) - 1) / 2, -1, 1)))
        dist = abs(float(pose[1][2] - t0[2]))
        if ang < p["min_pose_separation_deg"] and dist < p["min_pose_separation_mm"]:
            reasons.append(f"too similar to an accepted view ({ang:.1f} deg, {dist:.1f} mm)")
    stationarity = quality_stationarity(quality)
    ok = len(reasons) == 0
    dwell = False
    rejected_reasons: list[str] = []
    if (admit_dwell and not ok and quality.passed and stationarity is not None
            and stationarity["stationary"]):
        redundancy = [r for r in reasons
                      if r.startswith("redundant") or r.startswith("too similar")]
        if redundancy and len(redundancy) == len(reasons):
            dwell = True
            ok = True
            rejected_reasons = reasons
            reasons = ["dwell view admitted for stationarity evidence "
                       "(not used for solver geometry)"]
    return {"accept": bool(ok), "reasons": reasons, "predicted_rel_gain": gain,
            "threshold": need, "dwell": bool(dwell),
            "stationary": (None if stationarity is None
                           else bool(stationarity["stationary"])),
            "stationarity": stationarity,
            "rejected_reasons": rejected_reasons}


# ---------------------------------------------------------------------------
# coverage
# ---------------------------------------------------------------------------

def coverage(observations: Iterable[Observation],
             image_size: tuple[int, int] = (1280, 1024),
             grid: int = 4) -> dict:
    """Image-plane occupancy and pose coverage of the accepted observations."""
    w, h = image_size
    occ: dict[str, np.ndarray] = {}
    seen: dict[str, int] = {}
    for o in observations:
        a = occ.setdefault(o.camera, np.zeros((grid, grid), int))
        seen[o.camera] = seen.get(o.camera, 0) + 1
        for uv in o.uv.values():
            u, v = float(np.asarray(uv).ravel()[0]), float(np.asarray(uv).ravel()[1])
            if 0 <= u < w and 0 <= v < h:
                a[min(grid - 1, int(v / h * grid)),
                  min(grid - 1, int(u / w * grid))] += 1
    out = {}
    for c, a in occ.items():
        tot = max(int(a.sum()), 1)
        p = a / tot
        nz = p[p > 0]
        out[c] = {"occupancy": a.tolist(), "fraction": float((a > 0).mean()),
                  "entropy": float(-(nz * np.log(nz)).sum()),
                  "n_observations": seen.get(c, 0)}
    return out


def coverage_bins(poses: Sequence[tuple], distance_edges=(200, 280, 360, 440, 520, 620),
                  tilt_edges=(0, 8, 16, 24, 40)) -> dict:
    """Distance x tilt occupancy for accepted board poses."""
    d = np.zeros((len(distance_edges) - 1, len(tilt_edges) - 1), int)
    for R, t in poses:
        dist = float(np.linalg.norm(t))
        # tilt of the board normal relative to the camera axis
        n = np.asarray(R)[:, 2]
        tilt = float(np.degrees(np.arccos(np.clip(abs(n[2]), -1, 1))))
        di = np.searchsorted(distance_edges, dist) - 1
        ti = np.searchsorted(tilt_edges, tilt) - 1
        di = int(np.clip(di, 0, d.shape[0] - 1)); ti = int(np.clip(ti, 0, d.shape[1] - 1))
        d[di, ti] += 1
    return {"distance_edges": list(distance_edges), "tilt_edges": list(tilt_edges),
            "counts": d.tolist(), "occupied": int((d > 0).sum()), "n_bins": int(d.size)}
