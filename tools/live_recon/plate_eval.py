"""Plate detection, PnP pose and plate-surface statistics.

What is independent and what is not
------------------------------------
The evaluation compares two *different* computations:

``PnP reference``
    The 40 lattice-marker centroids are located in the raw left IR image
    (``calib.detect``) and ``calib.geometry.solve_pnp`` fits the known board
    geometry (``calib.plate.board_points_mm``) with the **left-camera
    intrinsics only**.  It never looks at the right camera, at disparity, or at
    the reconstruction.  Its depth information comes from the *apparent size and
    perspective* of the 196 x 112 mm marker field.

``stereo reconstruction``
    ``recon_cli`` triangu-lates the same pixels from the disparity map, i.e. from
    the product ``f * B`` and the rectification/extrinsics.

Therefore the difference between the reconstructed plate surface and the PnP
plane is a genuine cross-check: a bias in ``f*B`` (or in the disparity) shows up
as a range ratio, and an error in the *relative orientation* shows up as a tilt,
neither of which PnP can see.  Two caveats are reported with the numbers:

1. **Shared inputs.**  Both use the same left-camera ``fx, fy, cx, cy, dist``.
   A focal-length error moves both the same way, so this check is insensitive to
   it (a common ``fx`` error scales both the PnP range and the stereo range).
2. **The pitch gauge.**  PnP range is proportional to the *assumed* board pitch
   (28.0 mm from ``calib/plate.py``).  The measured range ratio therefore also
   absorbs ``pitch_true / 28.0``.  The report quotes the ratio and the implied
   pitch and keeps the degeneracy explicit.

A second, weakly independent orientation check is available: detecting the plate
on ``cam0`` (right IR, rotated 180 deg) and mapping that pose into the left
camera through the factory extrinsics.  The residual of that comparison is a
floor on the PnP systematic error, and is reported as ``pose_selfcheck``.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from calib import detect as calib_detect
from calib import geometry as calib_geometry
from calib import plate as calib_plate

from . import common
from .geometry_py import Profile, RectifyModel, left_camera_project


# ---------------------------------------------------------------------------
# geometry constants (from the recovered plate specification)
# ---------------------------------------------------------------------------

def plate_half_extents_mm(pitch_mm: float | None = None) -> tuple:
    """Half width/height of the **plate outline** (8 x 6 pitches)."""
    spec = calib_plate.current_spec()
    pitch = spec.pitch_mm if pitch_mm is None else float(pitch_mm)
    return (0.5 * spec.plate_pitches[0] * pitch,
            0.5 * spec.plate_pitches[1] * pitch)


def board_points_mm(pitch_mm: float | None = None) -> np.ndarray:
    return calib_plate.board_points_mm(pitch_mm)


def all_marker_points_mm(pitch_mm: float | None = None) -> np.ndarray:
    """40 lattice markers + the off-lattice large marker (41 points, z=0)."""
    return calib_plate.all_points_mm(pitch_mm)


def _uv_lookup() -> dict:
    return {(float(u), float(v)): i
            for i, (u, v) in enumerate(calib_plate.lattice_uv())}


# ---------------------------------------------------------------------------
# detection + PnP
# ---------------------------------------------------------------------------

def detect_pose(gray: np.ndarray, profile: Profile, camera: str = "ir_left",
                mode: str = "fast") -> dict:
    """Detect the plate and solve its pose in the given camera's frame.

    ``mode='fast'`` uses the bright-marker polarity that the live IR images
    actually show (one detector pass); ``mode='auto'`` uses
    ``detect_grid_auto`` (both polarities x three thresholds, ~6x slower) and is
    used for the independent right-camera cross-check and as a robustness probe.
    """
    if gray is None or gray.size == 0:
        return {"detected": False, "reason": "missing-image"}
    if gray.ndim == 3:
        gray = cv2.cvtColor(gray, cv2.COLOR_BGR2GRAY)
    if mode == "auto":
        obs = calib_detect.detect_grid_auto(gray)
    else:
        obs = calib_detect.detect_grid(gray, invert=False, percentile=99.0,
                                       refine_mode="centroid")
    if obs is None:
        return {"detected": False, "reason": "no-grid"}
    lut = _uv_lookup()
    obj = board_points_mm()
    try:
        idx = [lut[(float(u), float(v))] for (u, v) in obs.uv]
    except KeyError:
        return {"detected": False, "reason": "non-canonical-uv"}
    if len(idx) < 12:
        return {"detected": False, "reason": "too-few-markers",
                "n_markers": int(len(idx))}
    cam = profile.cameras[camera]
    pose = calib_geometry.solve_pnp(obj[idx], obs.pixels, cam.K, cam.dist)
    if pose is None:
        return {"detected": False, "reason": "pnp-failed",
                "n_markers": int(len(idx))}
    return {
        "detected": True,
        "camera": camera,
        "n_markers": int(len(idx)),
        "grid_rms_px": float(obs.rms),
        "grid_max_residual_px": float(obs.max_residual),
        "R": np.asarray(pose.R, dtype=np.float64),
        "T": np.asarray(pose.t, dtype=np.float64),
        "markers_uv": np.asarray(obs.uv, dtype=np.float64),
        "markers_px": np.asarray(obs.pixels, dtype=np.float64),
        "homography": np.asarray(obs.homography, dtype=np.float64),
        "detector": str(obs.detector),
    }


def right_pose_to_left(profile: Profile, R_right: np.ndarray,
                       T_right: np.ndarray) -> tuple:
    """Map a board pose measured in the right IR camera into the left frame."""
    R_rl, t_rl = profile.relative_pose("ir_left", "ir_right")
    R = R_rl.T @ np.asarray(R_right, dtype=np.float64)
    T = R_rl.T @ (np.asarray(T_right, dtype=np.float64) - t_rl)
    return R, T


def pose_selfcheck(profile: Profile, pose_left: dict, pose_right: dict) -> dict | None:
    """Agreement of the left-camera plate pose with the cam0-derived pose."""
    if not pose_left.get("detected") or not pose_right.get("detected"):
        return None
    R, T = right_pose_to_left(profile, pose_right["R"], pose_right["T"])
    dR = common.rotation_angle_deg(pose_left["R"], R)
    dT = T - pose_left["T"]
    return {
        "rotation_diff_deg": float(dR),
        "translation_diff_mm": [float(x) for x in dT],
        "translation_diff_norm_mm": float(np.linalg.norm(dT)),
        "range_diff_mm": float(np.linalg.norm(T) - np.linalg.norm(pose_left["T"])),
        "n_markers_left": int(pose_left["n_markers"]),
        "n_markers_right": int(pose_right["n_markers"]),
    }


# ---------------------------------------------------------------------------
# plate-frame surface statistics
# ---------------------------------------------------------------------------

def _robust_plane(points_xyz: np.ndarray, iterations: int = 2):
    """Plane fit with a MAD-based 3-sigma clip (thickness, not offset)."""
    p = np.asarray(points_xyz, dtype=np.float64).reshape(-1, 3)
    keep = np.ones(len(p), dtype=bool)
    fit = None
    for _ in range(iterations):
        if keep.sum() < 3:
            break
        fit = common.fit_plane(p[keep])
        if fit is None:
            break
        centroid, normal, _ = fit
        resid = (p - centroid) @ normal
        sigma = common.robust_sigma(resid[keep])
        if not np.isfinite(sigma) or sigma <= 0:
            break
        new_keep = np.abs(resid) <= 3.0 * sigma
        if new_keep.sum() < 3 or np.array_equal(new_keep, keep):
            keep = new_keep if new_keep.sum() >= 3 else keep
            break
        keep = new_keep
    if fit is None:
        return None
    centroid, normal, _ = fit
    resid = (p[keep] - centroid) @ normal
    return {
        "centroid": centroid,
        "normal": normal,
        "rms_mm": float(np.sqrt(np.mean(resid ** 2))),
        "p95_abs_mm": float(np.percentile(np.abs(resid), 95)),
        "n_inliers": int(keep.sum()),
    }


def board_stats(X: np.ndarray, pose: dict, pitch_mm: float | None = None,
                clip_mm: float = 6.0, ring_mm: float = 2.5) -> dict:
    """Statistics of the reconstructed points in the plate (board) frame.

    Parameters
    ----------
    X : (N,3) points in the **left IR camera** frame (mm).
    pose : output of :func:`detect_pose` (must be ``detected``).
    clip_mm : points further than this from the plate plane are excluded from
        the plane fit (they are real background seen through/around the plate).
    ring_mm : radius around each marker centre used for the marker-hole
        artefact statistic.
    """
    out: dict = {}
    X = np.asarray(X, dtype=np.float64).reshape(-1, 3)
    out["n_points_total"] = int(len(X))
    if not pose.get("detected") or len(X) == 0:
        out["ok"] = False
        return out
    R = np.asarray(pose["R"], dtype=np.float64)
    T = np.asarray(pose["T"], dtype=np.float64)
    half_x, half_y = plate_half_extents_mm(pitch_mm)
    Xb = (X - T) @ R                       # board frame coordinates
    in_outline = (np.abs(Xb[:, 0]) <= half_x) & (np.abs(Xb[:, 1]) <= half_y)
    n_plate = int(in_outline.sum())
    out["n_points_in_outline"] = n_plate
    out["frac_points_in_outline"] = float(n_plate) / max(len(X), 1)
    out["plate_half_extents_mm"] = [float(half_x), float(half_y)]
    out["range_mm"] = float(np.median(np.linalg.norm(X[in_outline], axis=1))) \
        if n_plate else float("nan")
    if n_plate < 50:
        out["ok"] = False
        return out

    zb = Xb[in_outline, 2]
    near = in_outline.copy()
    near[in_outline] = np.abs(zb) < clip_mm
    if near.sum() < 50:
        out["ok"] = False
        return out

    fit = _robust_plane(X[near])
    if fit is None:
        out["ok"] = False
        return out
    n_fit = np.asarray(fit["normal"], dtype=np.float64)
    n_pnp = R[:, 2]
    centroid = np.asarray(fit["centroid"], dtype=np.float64)

    out["ok"] = True
    out["plane_rms_mm"] = fit["rms_mm"]
    out["plane_p95_abs_mm"] = fit["p95_abs_mm"]
    out["plane_n_inliers"] = fit["n_inliers"]
    out["plane_tilt_deg"] = common.angle_between(n_fit, n_pnp)
    # Signed distance along the *PnP* normal: >0 means "farther than the plate".
    signed_pnp = (X[in_outline] - T) @ n_pnp
    out["bias_median_mm"] = float(np.median(signed_pnp))
    out["bias_mean_mm"] = float(np.mean(signed_pnp))
    out["bias_p05_mm"], out["bias_p95_mm"] = [float(v) for v in np.percentile(signed_pnp, [5, 95])]
    out["bias_iqr_mm"] = float(np.percentile(signed_pnp, 75) - np.percentile(signed_pnp, 25))
    # Offset of the fitted plane measured along its *own* normal (tilt-free).
    out["fit_plane_offset_mm"] = float((centroid - T) @ n_fit)
    out["pnp_plane_offset_mm"] = float((centroid - T) @ n_pnp)
    # Residual gradient of the surface relative to the PnP plane (mm / 100 mm).
    coef = common.fit_affine_xy(Xb[in_outline][:, :2], signed_pnp)
    if coef is not None:
        out["bias_gradient_mm_per_100mm"] = [float(coef[1] * 100.0), float(coef[2] * 100.0)]

    # Range ratio along each point's own viewing ray vs the PnP plane.
    Pn = X[in_outline] / np.linalg.norm(X[in_outline], axis=1, keepdims=True)
    denom = Pn @ n_pnp
    valid = np.abs(denom) > 1e-6
    t_expected = np.full(len(Pn), np.nan)
    t_expected[valid] = (n_pnp @ T) / denom[valid]
    rng = np.linalg.norm(X[in_outline], axis=1)
    ratio = rng[valid] / t_expected[valid]
    out["range_ratio"] = float(np.median(ratio))
    out["range_ratio_p05"], out["range_ratio_p95"] = [float(v) for v in np.percentile(ratio, [5, 95])]
    out["depth_bias_median_mm"] = float(np.median(rng[valid] - t_expected[valid]))
    out["depth_bias_ratio_pct"] = float(100.0 * (out["range_ratio"] - 1.0))
    pitch = calib_plate.current_spec().pitch_mm if pitch_mm is None else float(pitch_mm)
    out["implied_pitch_mm"] = float(pitch * out["range_ratio"])

    # Spatial coverage of the plate surface: 1 mm cells in the board frame.
    gx = np.floor(Xb[in_outline, 0] + half_x).astype(np.int64)
    gy = np.floor(Xb[in_outline, 1] + half_y).astype(np.int64)
    nx, ny = int(2 * half_x) + 1, int(2 * half_y) + 1
    ok = (gx >= 0) & (gx < nx) & (gy >= 0) & (gy < ny)
    occ = np.zeros((ny, nx), dtype=bool)
    occ[gy[ok], gx[ok]] = True
    out["coverage_1mm_frac"] = float(occ.mean())
    out["density_pts_per_cm2"] = float(n_plate / ((2 * half_x * 2 * half_y) / 100.0))

    # Marker-hole artefacts: distance in the board plane to the nearest of 41
    # marker centres, and the surface residual there versus the plate interior.
    markers = all_marker_points_mm(pitch_mm)[:, :2]
    dmin = np.full(len(Xb), np.inf)
    for m in markers:
        dmin = np.minimum(dmin, np.linalg.norm(Xb[:, :2] - m[None, :], axis=1))
    resid_fit = (X - centroid) @ n_fit
    ring = in_outline & (dmin < ring_mm)
    interior = in_outline & (dmin > 3.0 * ring_mm) & (np.abs(Xb[:, 0]) < half_x - 5) \
        & (np.abs(Xb[:, 1]) < half_y - 5)
    out["marker_ring_frac_of_plate"] = float(ring.sum()) / max(n_plate, 1)
    out["marker_ring_mean_abs_resid_mm"] = common.p50(np.abs(resid_fit[ring])) if ring.any() else None
    out["marker_ring_p95_abs_resid_mm"] = (
        float(np.percentile(np.abs(resid_fit[ring]), 95)) if ring.any() else None)
    out["interior_mean_abs_resid_mm"] = common.p50(np.abs(resid_fit[interior])) if interior.any() else None
    out["interior_p95_abs_resid_mm"] = (
        float(np.percentile(np.abs(resid_fit[interior]), 95)) if interior.any() else None)
    out["_marker_distance_mm"] = dmin   # reused by the image-space statistics
    return out


def plate_image_stats(model: RectifyModel, pose: dict, z_map: np.ndarray,
                      scale: float, ring_mm: float = 2.5,
                      edge_mm: float = 3.0, pitch_mm: float | None = None) -> dict:
    """Image-space coverage and edge statistics on the scaled depth map.

    The plate outline is projected into the rectified depth-map grid, so the
    denominator is the *area the plate should occupy*, independent of how many
    points survived.  This is what separates "more pixels" from "better pixels".
    """
    out: dict = {"ok": False}
    if pose is None or not pose.get("detected") or z_map is None:
        return out
    R = np.asarray(pose["R"], dtype=np.float64)
    T = np.asarray(pose["T"], dtype=np.float64)
    half_x, half_y = plate_half_extents_mm(pitch_mm)
    outline = model.board_outline(half_x, half_y)
    uv, zc = model.project_board(R, T, outline, scale)
    g = model.scaled(scale)
    if np.any(zc <= 0):
        return out
    poly = np.round(uv).astype(np.int32).reshape(-1, 1, 2)
    h, w = z_map.shape
    poly_mask = np.zeros((h, w), dtype=np.uint8)
    cv2.fillPoly(poly_mask, [poly], 255)
    area = int((poly_mask > 0).sum())
    if area <= 0:
        return out
    valid = (z_map > 0).astype(np.uint8) * 255
    n_valid = int(((valid > 0) & (poly_mask > 0)).sum())
    out["ok"] = True
    out["plate_area_px"] = area
    out["plate_valid_fraction"] = float(n_valid) / float(area)
    out["frame_valid_fraction"] = float((z_map > 0).mean())

    # Edge band: inside the outline but within `edge_mm` of it.
    range_mm = float(np.median(np.linalg.norm(np.asarray([T]), axis=1)))
    px_per_mm = g.f / max(range_mm, 1e-6)
    k = max(1, int(round(edge_mm * px_per_mm)))
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * k + 1, 2 * k + 1))
    inner = cv2.erode(poly_mask, kernel)
    band = (poly_mask > 0) & (inner == 0)
    interior = inner > 0
    out["edge_band_px"] = int(k)
    out["edge_band_area_px"] = int(band.sum())
    out["edge_band_invalid_fraction"] = (
        float(((valid == 0) & band).sum()) / float(band.sum()) if band.any() else None)
    out["interior_invalid_fraction"] = (
        float(((valid == 0) & interior).sum()) / float(interior.sum()) if interior.any() else None)

    # Marker rings in the image: the markers are specular/bright features whose
    # edges are the hardest thing to match on this plate.
    markers = all_marker_points_mm(pitch_mm)
    muv, mz = model.project_board(R, T, markers, scale)
    ring_mask = np.zeros((h, w), dtype=np.uint8)
    far = np.zeros((h, w), dtype=np.uint8)
    for (u, v), zz in zip(muv, mz):
        if zz <= 0:
            continue
        r = max(1, int(round(g.f * ring_mm / zz)))          # physical radius -> px
        cv2.circle(ring_mask, (int(round(u)), int(round(v))), r, 255, -1)
        cv2.circle(far, (int(round(u)), int(round(v))), 3 * r, 255, -1)
    rings = (ring_mask > 0) & (poly_mask > 0)
    out["marker_ring_area_px"] = int(rings.sum())
    out["marker_ring_radius_px_p50"] = float(np.median(
        [max(1, int(round(g.f * ring_mm / zz))) for zz in mz if zz > 0])) if len(mz) else None
    out["marker_ring_invalid_fraction"] = (
        float(((valid == 0) & rings).sum()) / float(rings.sum()) if rings.any() else None)
    rest = (poly_mask > 0) & (far == 0)
    out["marker_ref_invalid_fraction"] = (
        float(((valid == 0) & rest).sum()) / float(rest.sum()) if rest.any() else None)
    return out


def evaluate_group(model: RectifyModel, profile: Profile,
                   z_map: np.ndarray, conf_map: np.ndarray | None,
                   scale: float, left_gray: np.ndarray,
                   right_gray: np.ndarray | None = None,
                   detect_mode: str = "fast",
                   ring_mm: float = 2.5) -> dict:
    """Full plate evaluation for one group (pose + surface + image statistics)."""
    result: dict = {"detected": False}
    pose = detect_pose(left_gray, profile, "ir_left", mode=detect_mode)
    result["pose_left"] = pose
    if not pose.get("detected"):
        result["reason"] = pose.get("reason", "no-pose")
        return result
    v, u, X = model.points_from_depth(z_map, scale)
    stats = board_stats(X, pose, ring_mm=ring_mm)
    stats.pop("_marker_distance_mm", None)
    result["surface"] = stats
    result["detected"] = True
    result["pose_T_mm"] = [float(x) for x in pose["T"]]
    result["pose_range_mm"] = float(np.linalg.norm(pose["T"]))
    result["pose_rvec_deg"] = [float(x) for x in
                               np.degrees(cv2.Rodrigues(pose["R"])[0].ravel())]
    result["image"] = plate_image_stats(model, pose, z_map, scale, ring_mm=ring_mm)
    if conf_map is not None:
        conf = np.asarray(conf_map, dtype=np.float64)
        cvalid = conf[v, u]
        positive = conf[conf > 0]
        result["confidence"] = {
            "valid_p05": float(np.percentile(cvalid, 5)) if cvalid.size else None,
            "valid_p50": float(np.percentile(cvalid, 50)) if cvalid.size else None,
            "all_p50": float(np.percentile(positive, 50)) if positive.size else None,
        }
    if right_gray is not None:
        pose_r = detect_pose(right_gray, profile, "ir_right", mode="auto")
        result["pose_right"] = pose_r
        result["pose_selfcheck"] = pose_selfcheck(profile, pose, pose_r)
    return result
