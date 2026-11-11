"""Tests for the reconstruction-profile loader and its skew convention."""
from __future__ import annotations

import json
from pathlib import Path
import sys

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from calib import recon_profile  # noqa: E402


IDENTITY = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]


def _rotation(axis_angle) -> list[float]:
    """A genuinely orthonormal 3x3 for the fixtures (no rounded literals)."""
    return [float(v) for v in
            __import__("calib.geometry", fromlist=["rodrigues"])
            .rodrigues(np.asarray(axis_angle, float)).ravel()]


# ~22 deg about the vertical axis, as on the real rig, plus a 0.1 deg tilt.
IR_RIGHT_ROTATION = _rotation([0.001, 0.384, 0.002])


def make_profile(tmp_path: Path, skew_px: float = 0.0718) -> Path:
    doc = {
        "schema": recon_profile.SCHEMA,
        "kind": "factory",
        "image_width": 1280,
        "image_height": 1024,
        "units": "mm",
        "plate_pitch_mm": 28.0,
        "color_status": "stale",
        "cameras": {
            "ir_left": {
                "fx": 1159.1, "fy": 1159.0, "cx": 623.9, "cy": 534.5,
                "skew": skew_px, "dist": [-0.162, 0.173, 0.0, 0.0, 0.018],
                "R": IDENTITY, "T": [0.0, 0.0, 0.0],
            },
            "ir_right": {
                "fx": 1158.0, "fy": 1157.7, "cx": 635.5, "cy": 494.5,
                "skew": 0.212, "dist": [-0.162, 0.178, 0.0, 0.0, 0.012],
                "R": IR_RIGHT_ROTATION,
                "T": [-155.5, -0.23, 31.1],
            },
        },
    }
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(doc))
    return path


def test_profile_skew_is_absolute_pixels(tmp_path):
    """The JSON stores K[0,1] in pixels; the loader must not rescale it."""
    profile = recon_profile.load(make_profile(tmp_path, skew_px=0.0718))
    camera = profile.camera("ir_left")
    assert camera.K[0, 1] == pytest.approx(0.0718, abs=1e-12)
    # calib.CameraIntrinsics normalizes internally, so it must reproduce the
    # same absolute term exactly once.
    assert camera.intrinsics().K[0, 1] == pytest.approx(0.0718, abs=1e-9)


def test_scaled_skew_would_be_detectable(tmp_path):
    """A normalized-skew mistake changes K[0,1] by ~fx and must be visible."""
    profile = recon_profile.load(make_profile(tmp_path, skew_px=0.0718))
    camera = profile.camera("ir_left")
    wrong = camera.fx * camera.skew_px
    assert wrong > 50.0            # ~83 px shear
    assert camera.K[0, 1] != pytest.approx(wrong, rel=1e-3)


def test_extrinsics_and_identity_left_frame(tmp_path):
    profile = recon_profile.load(make_profile(tmp_path))
    left = profile.camera("ir_left")
    assert np.allclose(left.R, np.eye(3))
    assert np.allclose(left.T, 0.0)
    right = profile.camera("ir_right")
    assert np.linalg.norm(right.T) == pytest.approx(158.6, abs=0.5)
    # rotation must be orthonormal after the profile round trip
    assert np.abs(right.R @ right.R.T - np.eye(3)).max() < 1e-12


def test_rejects_non_orthonormal_rotation(tmp_path):
    path = make_profile(tmp_path)
    doc = json.loads(path.read_text())
    doc["cameras"]["ir_right"]["R"][0] = 1.5
    path.write_text(json.dumps(doc))
    with pytest.raises(ValueError):
        recon_profile.load(path)


def test_rejects_wrong_schema(tmp_path):
    path = make_profile(tmp_path)
    doc = json.loads(path.read_text())
    doc["schema"] = "something-else/1"
    path.write_text(json.dumps(doc))
    with pytest.raises(ValueError):
        recon_profile.load(path)


def test_project_round_trips_a_plane(tmp_path):
    """Project a planar grid through a known pose and recover it with PnP."""
    from calib import geometry, plate

    profile = recon_profile.load(make_profile(tmp_path))
    camera = profile.camera("ir_left")
    uv = plate.lattice_uv()
    board = plate.board_points_mm()
    R = geometry.rodrigues(np.array([0.12, -0.08, 0.01]))
    T = np.array([-10.0, -20.0, 385.0])
    pixels = recon_profile.project(camera.K, camera.dist, R, T, board)
    pose = geometry.solve_pnp(board, pixels, camera.K, camera.dist)
    assert pose is not None
    assert np.linalg.norm(pose.t - T) < 0.05
    # and the reprojection of the recovered pose matches the input pixels
    errors = geometry.reprojection_errors(camera.K, camera.dist, pose.R, pose.t,
                                          board, pixels)
    # cv2.solvePnP stops at its default tolerance, so allow sub-0.02 px.
    assert np.sqrt((errors ** 2).mean()) < 0.02
    assert uv.shape[0] == board.shape[0]
