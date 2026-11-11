"""Load an ``einstar.recon.profile/1`` JSON profile with the right conventions.

Why this module exists
----------------------
Two skew conventions coexist in this repository and mixing them silently
corrupts every projection:

``calib.CameraIntrinsics`` (and the CCF record field 8, and
``tools/calib/export_profile.py``'s ``skew`` input) store the **normalized**
skew ``alpha_c``: the camera matrix is ``K[0,1] = alpha_c * fx``.

The reconstruction profile JSON (``einstar.recon.profile/1``, consumed by the
C++ backend as ``cv::Matx33d(fx, skew, cx, 0, fy, cy, 0, 0, 1)``) stores the
**absolute pixel** skew ``K[0,1]``.

Measured on live Einstar IR frames (2026-11-11, group 300, cam1, plate PnP):

* ``K[0,1] = 0.0718`` px  -> 0.885 px RMS
* ``K[0,1] = 0.0718 * fx = 83.2`` px -> 10.41 px RMS

so the distinction is worth ~10 px of systematic error.  Always load profiles
through this module instead of indexing the JSON by hand.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

import numpy as np

from .calibrate import CameraIntrinsics

SCHEMA = "einstar.recon.profile/1"


@dataclass(frozen=True)
class ProfileCamera:
    """One camera of a reconstruction profile, in the C++ (absolute) convention."""

    name: str
    fx: float
    fy: float
    cx: float
    cy: float
    skew_px: float          # absolute K[0,1]
    dist: np.ndarray        # 5 OpenCV coefficients
    R: np.ndarray           # 3x3 world->camera rotation (ir_left is identity)
    T: np.ndarray           # 3 translation, millimetres

    @property
    def K(self) -> np.ndarray:
        return np.array([[self.fx, self.skew_px, self.cx],
                         [0.0, self.fy, self.cy],
                         [0.0, 0.0, 1.0]], float)

    def intrinsics(self) -> CameraIntrinsics:
        """Return the ``calib`` view, normalizing the skew exactly once."""
        return CameraIntrinsics(fx=self.fx, fy=self.fy, cx=self.cx, cy=self.cy,
                                kc=np.asarray(self.dist, float).copy(),
                                skew=self.skew_px / self.fx if self.fx else 0.0)


@dataclass(frozen=True)
class Profile:
    path: str
    schema: str
    kind: str
    color_status: str
    plate_pitch_mm: float
    units: str
    width: int
    height: int
    cameras: dict[str, ProfileCamera]

    def camera(self, name: str) -> ProfileCamera:
        return self.cameras[name]


def _camera(name: str, node: dict) -> ProfileCamera:
    R = np.asarray(node["R"], float).reshape(3, 3)
    T = np.asarray(node["T"], float).reshape(3)
    ortho = float(np.abs(R @ R.T - np.eye(3)).max())
    if ortho > 1e-5:
        raise ValueError(f"{name}: profile rotation is not orthonormal "
                         f"(max |R R^T - I| = {ortho:.2e})")
    return ProfileCamera(name=name, fx=float(node["fx"]), fy=float(node["fy"]),
                         cx=float(node["cx"]), cy=float(node["cy"]),
                         skew_px=float(node.get("skew", 0.0)),
                         dist=np.asarray(node["dist"], float).reshape(-1)[:5],
                         R=R, T=T)


def load(path: str | Path) -> Profile:
    """Parse a reconstruction profile.  Raises on wrong schema or bad rotation."""
    path = Path(path)
    doc = json.loads(path.read_text())
    schema = doc.get("schema")
    if schema != SCHEMA:
        raise ValueError(f"{path}: unsupported profile schema {schema!r} "
                         f"(expected {SCHEMA!r})")
    cams = {name: _camera(name, node)
            for name, node in doc["cameras"].items()}
    if "ir_left" not in cams:
        raise ValueError(f"{path}: profile has no ir_left camera")
    left = cams["ir_left"]
    if np.abs(left.R - np.eye(3)).max() > 1e-6 or np.abs(left.T).max() > 1e-6:
        raise ValueError(f"{path}: ir_left must be the identity world frame")
    return Profile(path=str(path), schema=schema,
                   kind=str(doc.get("kind", "unknown")),
                   color_status=str(doc.get("color_status", "unknown")),
                   plate_pitch_mm=float(doc.get("plate_pitch_mm", 28.0)),
                   units=str(doc.get("units", "mm")),
                   width=int(doc.get("image_width", 1280)),
                   height=int(doc.get("image_height", 1024)),
                   cameras=cams)


def project(K: np.ndarray, kc: np.ndarray, R: np.ndarray,
            T: np.ndarray, X_world: np.ndarray) -> np.ndarray:
    """Project world points through (R, T) with the OpenCV pinhole+distortion
    model (``X_cam = R X_world + T``).  Self-contained so callers cannot pick up
    a different convention by accident."""
    from . import geometry

    X = np.asarray(X_world, float).reshape(-1, 3)
    Xc = X @ np.asarray(R, float).T + np.asarray(T, float)
    return geometry.project(np.asarray(K, float), np.asarray(kc, float), Xc)
