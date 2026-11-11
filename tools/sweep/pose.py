"""Per-camera plate pose, pose covariance and stereo consistency.

Everything here is expressed in the OpenCV camera convention of
``docs/calibration/model.md`` and ``calib.geometry``::

    X_cam = R @ X_board + t
    x = X/Z, y = Y/Z,  (xd,yd) = distort(x,y,kc)
    u = fx*xd + skew*yd + cx,  v = fy*yd + cy

Board frame.  ``calib.plate.board_points_mm()`` is used unchanged, i.e.

    x = pitch*(u - 3.5)     (along +u, image-right for an upright plate)
    y = pitch*(v - 2)       (along +v, image-down for an upright plate)
    z = x cross y           (into the plate, i.e. away from the camera)

so a plate held upright and fronto-parallel facing the camera has
``R ~= I``, ``t ~= (0, 0, distance)`` and the board normal ``R[:,2]`` points
away from the camera.  This differs from ``PlateSpec.vendor_points_mm()``
(y = pitch*(2-v)) by a 180 deg rotation about x, which flips the sign of the
reported y, pitch and roll; the module reports Euler angles so the convention
is visible in the output.

The sweep is a *single rigid plate* observed by both IR cameras, so the two
independent poses give an independent estimate of the right-to-left extrinsic
``X_right = R_rel @ X_left + t_rel`` that can be compared with the factory
profile.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import cv2
import numpy as np

from calib import geometry, plate

UVS = [(float(u), float(v)) for u, v in plate.lattice_uv()]
UV_INDEX = {uv: i for i, uv in enumerate(UVS)}
BOARD_MM = plate.board_points_mm()


def object_points(uv_keys) -> np.ndarray:
    return np.asarray([BOARD_MM[UV_INDEX[k]] for k in uv_keys], float)


# ---------------------------------------------------------------------------
# pose
# ---------------------------------------------------------------------------

@dataclass
class PoseSolution:
    R: np.ndarray
    t: np.ndarray
    n_points: int
    rms_px: float
    max_px: float
    sigma_rot_deg: float
    sigma_t_mm: float
    euler: tuple           # (pitch, yaw, roll) deg, ZYX decomposition
    tilt_deg: float        # angle between board normal and camera z axis
    normal_cam: np.ndarray

    def as_dict(self) -> dict:
        return {
            "R": [float(v) for v in self.R.ravel()],
            "t": [float(v) for v in self.t.ravel()],
            "n_points": int(self.n_points),
            "rms_px": _r(self.rms_px),
            "max_px": _r(self.max_px),
            "sigma_rot_deg": _r(self.sigma_rot_deg, 6),
            "sigma_t_mm": _r(self.sigma_t_mm, 6),
            "pitch_deg": _r(self.euler[0]),
            "yaw_deg": _r(self.euler[1]),
            "roll_deg": _r(self.euler[2]),
            "tilt_deg": _r(self.tilt_deg),
            "normal_cam": [_r(v, 6) for v in self.normal_cam],
        }


def _r(v, nd=4):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return round(f, nd) if np.isfinite(f) else None


def euler_zyx(R: np.ndarray) -> tuple[float, float, float]:
    """Decompose R = Rz(roll) Ry(yaw) Rx(pitch) -> (pitch, yaw, roll) in degrees.

    Well conditioned here: the plate stays within 20 deg of fronto-parallel, so
    ``|R[2,0]|`` is far from 1.
    """
    R = np.asarray(R, float)
    sy = -float(np.clip(R[2, 0], -1.0, 1.0))
    yaw = np.degrees(np.arcsin(sy))
    pitch = np.degrees(np.arctan2(R[2, 1], R[2, 2]))
    roll = np.degrees(np.arctan2(R[1, 0], R[0, 0]))
    return float(pitch), float(yaw), float(roll)


def _project(K, kc, R, t, obj):
    return geometry.project_world(K, kc, R, t, obj)


def pose_jacobian(obj, uv, K, kc, R, t, eps: float = 1e-6) -> np.ndarray:
    """d(u,v)/d(rvec, t) with a left (camera-frame) rotation perturbation."""
    obj = np.asarray(obj, float)
    n = len(obj)
    J = np.zeros((2 * n, 6))
    for k in range(6):
        d = np.zeros(6)
        d[k] = eps * 0.5
        Rp = geometry.rodrigues(d[:3]) @ R if k < 3 else R
        tp = t + d[3:] if k >= 3 else t
        Rm = geometry.rodrigues(-d[:3]) @ R if k < 3 else R
        tm = t - d[3:] if k >= 3 else t
        pp = _project(K, kc, Rp, tp, obj)
        pm = _project(K, kc, Rm, tm, obj)
        J[:, k] = ((pp - pm) / eps).ravel()
    return J


def solve_pose(obs, K: np.ndarray, kc: np.ndarray,
               min_points: int = 8) -> Optional[PoseSolution]:
    """``calib.geometry.solve_pnp`` on the detected lattice, plus diagnostics.

    Returns ``None`` when fewer than ``min_points`` lattice markers were
    detected or the solver fails.  ``rms_px``/``max_px`` are measured with the
    documented distortion model (:func:`calib.geometry.project_world`), i.e. in
    distorted pixel space, and ``sigma_*`` come from the Gauss-Newton covariance
    at that solution with the residual scatter as the noise estimate.
    """
    keys = [k for k in UVS if k in obs.points]
    if len(keys) < min_points:
        return None
    obj = object_points(keys)
    uv = np.asarray([obs.points[k] for k in keys], float)
    p = geometry.solve_pnp(obj, uv, K, kc)
    if p is None:
        return None
    err = geometry.reprojection_errors(K, kc, p.R, p.t, obj, uv)
    rms = float(np.sqrt(np.mean(err ** 2)))
    mx = float(err.max())
    # covariance from the linearised model, noise = residual scatter
    try:
        J = pose_jacobian(obj, uv, K, kc, p.R, p.t)
        JTJ = J.T @ J
        dof = max(2 * len(obj) - 6, 1)
        var = float(np.sum(err ** 2) / dof)
        C = var * np.linalg.inv(JTJ)
        sigma_rot = float(np.degrees(np.sqrt(max(np.trace(C[:3, :3]), 0.0))))
        sigma_t = float(np.sqrt(max(np.trace(C[3:, 3:]), 0.0)))
    except np.linalg.LinAlgError:
        sigma_rot = sigma_t = float("nan")
    eul = euler_zyx(p.R)
    n = np.asarray(p.R, float)[:, 2]
    tilt = float(np.degrees(np.arccos(np.clip(n[2], -1.0, 1.0))))
    return PoseSolution(R=np.asarray(p.R, float), t=np.asarray(p.t, float),
                        n_points=len(keys), rms_px=rms, max_px=mx,
                        sigma_rot_deg=sigma_rot, sigma_t_mm=sigma_t,
                        euler=eul, tilt_deg=tilt, normal_cam=n)


# ---------------------------------------------------------------------------
# stereo
# ---------------------------------------------------------------------------

def stereo_from_poses(pose_a: PoseSolution, pose_b: PoseSolution):
    """Relative pose of camera ``a`` expressed in camera ``b``: X_a = R X_b + t.

    Camera ``b`` is the gauge (``ir_left`` by convention, i.e. stream 1).
    """
    R, t = geometry.relative_pose(pose_b.R, pose_b.t, pose_a.R, pose_a.t)
    return R, t


def rot_angle_deg(R: np.ndarray) -> float:
    c = (np.trace(np.asarray(R, float)) - 1.0) / 2.0
    return float(np.degrees(np.arccos(np.clip(c, -1.0, 1.0))))


def compare_stereo(R_meas, t_meas, R_fac, t_fac) -> dict:
    """Compare a measured relative pose with the factory extrinsic."""
    R_meas = np.asarray(R_meas, float)
    t_meas = np.asarray(t_meas, float)
    dR = R_meas @ np.asarray(R_fac, float).T
    ang = rot_angle_deg(dR)
    rvec_err = geometry.rvec_from_R(dR)
    b_meas = float(np.linalg.norm(t_meas))
    b_fac = float(np.linalg.norm(t_fac))
    # the relative translation scales with the assumed plate pitch, so the
    # length ratio is an independent estimate of the effective pitch
    pitch_implied = 28.0 * b_fac / b_meas if b_meas > 1e-9 else float("nan")
    cosang = float(np.dot(t_meas, t_fac) / max(b_meas * b_fac, 1e-12))
    return {
        "rot_err_deg": ang,
        "rot_err_rvec_deg": [float(v) for v in np.degrees(rvec_err)],
        "t_err_mm": float(np.linalg.norm(t_meas - np.asarray(t_fac, float))),
        "baseline_mm": b_meas,
        "baseline_factory_mm": b_fac,
        "baseline_ratio": b_meas / b_fac if b_fac > 0 else float("nan"),
        "pitch_implied_mm": float(pitch_implied),
        "t_direction_deg": float(np.degrees(np.arccos(np.clip(cosang, -1.0, 1.0)))),
    }


def undistort_normalised(uv: np.ndarray, K: np.ndarray, kc: np.ndarray) -> np.ndarray:
    pts = np.asarray(uv, float).reshape(-1, 1, 2)
    out = cv2.undistortPoints(pts, np.asarray(K, float), np.asarray(kc, float).ravel())
    return out.reshape(-1, 2)


def epipolar_sampson_px(E_or_F: np.ndarray, x1: np.ndarray, x2: np.ndarray,
                        focal_px: float) -> np.ndarray:
    """Sampson distance in pixel-equivalent units for *normalised* points."""
    return geometry.epipolar_residual(E_or_F, x1, x2) * float(focal_px)


def essential(R: np.ndarray, t: np.ndarray) -> np.ndarray:
    tx = np.array([[0, -t[2], t[1]], [t[2], 0, -t[0]], [-t[1], t[0], 0]], float)
    return tx @ np.asarray(R, float)


def project_board(K: np.ndarray, kc: np.ndarray, R, t, board_mm: np.ndarray) -> np.ndarray:
    return geometry.project_world(K, kc, R, t, board_mm)
