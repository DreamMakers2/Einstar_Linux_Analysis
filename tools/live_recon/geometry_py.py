"""Camera profile parsing, rectification and depth-map -> 3-D inversion.

The C++ pipeline (``recon/src/rectify.cpp``, ``recon/src/depth.cpp``) is the
authority.  This module reproduces *only* the deterministic camera model so that
the harness can

* recover per-pixel 3-D points from ``recon_depth_mm.tiff`` (which stores the
  depth along the **left IR camera z axis**, not the rectified z), and
* project plate geometry into the rectified depth-map grid for image-space
  coverage/edge statistics.

The inversion was verified against the PLY that ``recon_cli`` writes for the same
group: ``max |delta| = 3.1e-5 mm`` (float32 round-off), see
``reconstruct_sweep.py --selftest``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import cv2
import numpy as np


CAMERA_NAMES = ("ir_left", "ir_right", "color")


@dataclass
class CameraModel:
    name: str
    fx: float
    fy: float
    cx: float
    cy: float
    skew: float
    dist: np.ndarray          # (5,)
    R: np.ndarray             # (3,3) world -> camera
    T: np.ndarray             # (3,)  millimetres

    @property
    def K(self) -> np.ndarray:
        return np.array([[self.fx, self.skew, self.cx],
                         [0.0, self.fy, self.cy],
                         [0.0, 0.0, 1.0]], dtype=np.float64)

    def dist_mat(self) -> np.ndarray:
        return self.dist.reshape(1, -1).astype(np.float64)


@dataclass
class Profile:
    path: str
    doc: dict
    cameras: dict = field(default_factory=dict)

    @classmethod
    def load(cls, path: str) -> "Profile":
        with open(path) as f:
            doc = json.load(f)
        cams = {}
        for name in CAMERA_NAMES:
            c = doc["cameras"][name]
            cams[name] = CameraModel(
                name=name,
                fx=float(c["fx"]), fy=float(c["fy"]),
                cx=float(c["cx"]), cy=float(c["cy"]),
                skew=float(c.get("skew", 0.0)),
                dist=np.asarray(c["dist"], dtype=np.float64).reshape(-1),
                R=np.asarray(c["R"], dtype=np.float64).reshape(3, 3),
                T=np.asarray(c["T"], dtype=np.float64).reshape(3),
            )
        return cls(path=path, doc=doc, cameras=cams)

    @property
    def width(self) -> int:
        return int(self.doc["image_width"])

    @property
    def height(self) -> int:
        return int(self.doc["image_height"])

    @property
    def color_status(self) -> str:
        return str(self.doc.get("color_status", "unknown"))

    @property
    def plate_pitch_mm(self) -> float:
        return float(self.doc.get("plate_pitch_mm", 28.0))

    def relative_pose(self, frm: str = "ir_left", to: str = "ir_right"):
        """X_to = R X_from + T, from the shared world frame."""
        a, b = self.cameras[frm], self.cameras[to]
        R = b.R @ a.R.T
        T = b.T - R @ a.T
        return R, T


@dataclass
class ScaledGeometry:
    """Scaled rectified-left pinhole used by the matcher (stereo.hpp contract)."""
    scale: float
    f: float
    cx: float
    cy: float
    baseline_mm: float
    disp_offset: float
    width: int
    height: int
    R1: np.ndarray            # full-resolution rectification rotation

    def disparity_at(self, depth_mm: float) -> float:
        return self.f * self.baseline_mm / float(depth_mm) + self.disp_offset

    def depth_at(self, disparity: float) -> float:
        return self.f * self.baseline_mm / (float(disparity) - self.disp_offset)

    def window(self, min_depth_mm: float = 150.0, max_depth_mm: float = 800.0,
               align: int = 16) -> tuple:
        """(min_disparity, num_disparities) exactly as stereo.cpp derives them."""
        lo = min(self.disparity_at(min_depth_mm), self.disparity_at(max_depth_mm))
        hi = max(self.disparity_at(min_depth_mm), self.disparity_at(max_depth_mm))
        min_disp = int(np.floor(lo / align)) * align
        num = int(np.ceil((hi - min_disp) / align)) * align
        return min_disp, max(num, align)


class RectifyModel:
    """Bouguet rectification of the IR pair exactly as ``recon/src/rectify.cpp``."""

    def __init__(self, profile: Profile, alpha: float = -1.0, border_erode: int = 5):
        self.profile = profile
        self.alpha = alpha
        self.border_erode = border_erode
        cl = profile.cameras["ir_left"]
        cr = profile.cameras["ir_right"]
        R, T = profile.relative_pose("ir_left", "ir_right")
        size = (profile.width, profile.height)
        (self.R1, self.R2, self.P1, self.P2, self.Q,
         self.roi1, self.roi2) = cv2.stereoRectify(
            cl.K, cl.dist_mat(), cr.K, cr.dist_mat(), size,
            R, T.reshape(3, 1),
            flags=cv2.CALIB_ZERO_DISPARITY, alpha=alpha, newImageSize=size)
        self.size = size
        self.f = float(self.P1[0, 0])
        self.cx = float(self.P1[0, 2])
        self.cy = float(self.P1[1, 2])
        self.baseline_mm = float(-self.P2[0, 3] / self.P2[0, 0])
        self.disp_offset_full = float(self.P1[0, 2] - self.P2[0, 2])
        # baseline between the two optical centres (independent of P2)
        self.baseline_from_centres_mm = float(np.linalg.norm(T))

    # -- geometry ---------------------------------------------------------
    def scaled(self, scale: float) -> ScaledGeometry:
        return ScaledGeometry(
            scale=float(scale),
            f=self.f * scale,
            cx=self.cx * scale,
            cy=self.cy * scale,
            baseline_mm=self.baseline_mm,
            disp_offset=self.disp_offset_full * scale,
            width=int(round(self.size[0] * scale)),
            height=int(round(self.size[1] * scale)),
            R1=self.R1,
        )

    # -- maps -------------------------------------------------------------
    def maps(self):
        """initUndistortRectifyMap for both cameras (CV_32FC1)."""
        cl = self.profile.cameras["ir_left"]
        cr = self.profile.cameras["ir_right"]
        m1x, m1y = cv2.initUndistortRectifyMap(
            cl.K, cl.dist_mat(), self.R1, self.P1, self.size, cv2.CV_32FC1)
        m2x, m2y = cv2.initUndistortRectifyMap(
            cr.K, cr.dist_mat(), self.R2, self.P2, self.size, cv2.CV_32FC1)
        return (m1x, m1y), (m2x, m2y)

    def rectified_pair(self, left_raw: np.ndarray, right_raw: np.ndarray):
        """Rectify raw (calibration-orientation) IR images and build valid masks."""
        (m1x, m1y), (m2x, m2y) = self.maps()
        left = cv2.remap(left_raw, m1x, m1y, cv2.INTER_LINEAR,
                         borderMode=cv2.BORDER_CONSTANT, borderValue=0)
        right = cv2.remap(right_raw, m2x, m2y, cv2.INTER_LINEAR,
                          borderMode=cv2.BORDER_CONSTANT, borderValue=0)
        white = np.full((self.size[1], self.size[0]), 255, np.uint8)
        vl = cv2.remap(white, m1x, m1y, cv2.INTER_NEAREST,
                       borderMode=cv2.BORDER_CONSTANT, borderValue=0)
        vr = cv2.remap(white, m2x, m2y, cv2.INTER_NEAREST,
                       borderMode=cv2.BORDER_CONSTANT, borderValue=0)
        if self.border_erode > 0:
            k = cv2.getStructuringElement(cv2.MORPH_RECT,
                                          (self.border_erode, self.border_erode))
            vl = cv2.erode(vl, k)
            vr = cv2.erode(vr, k)
        return left, right, vl, vr

    # -- points -----------------------------------------------------------
    def points_from_depth(self, z_left: np.ndarray, scale: float):
        """Invert ``recon_depth_mm.tiff`` -> per-pixel 3-D points (left IR frame).

        ``z_left`` is the map written by ``compute_depth()``: the depth along the
        **left camera** z axis.  For a rectified pixel the ray is
        ``[a, b, 1]`` with ``a=(u-cx)/f``, ``b=(v-cy)/f`` (scaled pinhole);
        rotating it back with ``R1^T`` gives ``q``, the point is ``X = z*q/q_z``.

        Returns ``(v, u, X)`` where ``X`` is (N,3) float64 in mm.
        """
        z = np.asarray(z_left)
        g = self.scaled(scale)
        if z.shape != (g.height, g.width):
            raise ValueError(f"depth map {z.shape} does not match scale {scale} "
                             f"({g.height}x{g.width})")
        v, u = np.nonzero(z > 0)
        zl = z[v, u].astype(np.float64)
        a = (u.astype(np.float64) - g.cx) / g.f
        b = (v.astype(np.float64) - g.cy) / g.f
        q = np.stack([a, b, np.ones_like(a)], axis=1) @ g.R1   # (R1^T r)^T = r^T R1
        X = zl[:, None] * q / q[:, 2:3]
        return v, u, X

    # -- projection -------------------------------------------------------
    def project_board(self, R_board: np.ndarray, T_board: np.ndarray,
                      points_board: np.ndarray, scale: float) -> tuple:
        """Board-frame points -> pixel coordinates of the scaled depth map.

        Board is in the left IR camera frame via ``X_cam = R_board X_b + T_board``;
        rectification is ``X_rect = R1 X_cam``; the rectified left camera is the
        pinhole ``(f, cx, cy)`` of P1.
        """
        g = self.scaled(scale)
        Xc = np.asarray(points_board, dtype=np.float64).reshape(-1, 3) @ R_board.T \
            + np.asarray(T_board, dtype=np.float64).reshape(1, 3)
        Xr = Xc @ self.R1.T
        z = Xr[:, 2]
        uv = np.stack([g.f * Xr[:, 0] / z + g.cx,
                       g.f * Xr[:, 1] / z + g.cy], axis=1)
        return uv, z

    def board_outline(self, half_x_mm: float, half_y_mm: float) -> np.ndarray:
        return np.array([[-half_x_mm, -half_y_mm, 0.0],
                         [half_x_mm, -half_y_mm, 0.0],
                         [half_x_mm, half_y_mm, 0.0],
                         [-half_x_mm, half_y_mm, 0.0]], dtype=np.float64)


def left_camera_project(profile: Profile, X_cam: np.ndarray,
                        R_extra: np.ndarray | None = None,
                        T_extra: np.ndarray | None = None) -> np.ndarray:
    """Project left-camera-frame points through the *raw* left IR camera model.

    Used to map a 3-D plate frame back onto the raw cam1 image (marker/edge
    annotations).  Distortion is applied, so this is the inverse of the model
    solvePnP uses.
    """
    cam = profile.cameras["ir_left"]
    X = np.asarray(X_cam, dtype=np.float64).reshape(-1, 3)
    if R_extra is not None:
        X = X @ np.asarray(R_extra).T + np.asarray(T_extra).reshape(1, 3)
    z = X[:, 2]
    x = X[:, 0] / z
    y = X[:, 1] / z
    k1, k2, p1, p2, k3 = cam.dist[:5]
    r2 = x * x + y * y
    radial = 1.0 + k1 * r2 + k2 * r2 * r2 + k3 * r2 * r2 * r2
    xd = x * radial + 2.0 * p1 * x * y + p2 * (r2 + 2.0 * x * x)
    yd = y * radial + p1 * (r2 + 2.0 * y * y) + 2.0 * p2 * x * y
    u = cam.fx * xd + cam.skew * yd + cam.cx
    v = cam.fy * yd + cam.cy
    return np.stack([u, v], axis=1)
