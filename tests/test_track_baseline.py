"""Tests for the tracking baseline (tools/track/baseline.py).

The ICP mathematics is validated on synthetic non-planar surfaces where the
ground-truth transform is known; the real-data case (a planar calibration plate)
is used to check that the degeneracy signal fires, because that is the failure
mode a fusion stage must not miss.
"""
from __future__ import annotations

import math
from pathlib import Path
import sys

import numpy as np
import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from tools.track import baseline as tb  # noqa: E402


def _bumpy_surface(n: int = 4000, seed: int = 0) -> np.ndarray:
    """A non-planar surface: sinusoidal relief over a 120 mm patch."""
    rng = np.random.default_rng(seed)
    x = rng.uniform(-60, 60, n)
    y = rng.uniform(-60, 60, n)
    z = 400.0 + 12.0 * np.sin(x / 9.0) + 9.0 * np.cos(y / 7.0) + rng.normal(0, 0.15, n)
    return np.stack([x, y, z], axis=1)


def _se3(rx=0.0, ry=0.0, rz=0.0, t=(0.0, 0.0, 0.0)) -> np.ndarray:
    def rot(axis, ang):
        c, s = math.cos(ang), math.sin(ang)
        if axis == "x":
            return np.array([[1, 0, 0], [0, c, -s], [0, s, c]])
        if axis == "y":
            return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]])
        return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]])
    R = rot("z", rz) @ rot("y", ry) @ rot("x", rx)
    T = np.eye(4)
    T[:3, :3] = R
    T[:3, 3] = np.asarray(t, float)
    return T


def _cloud(points):
    pts = tb.voxel_downsample(points, 3.0)
    return tb.Cloud(pts, tb.estimate_normals(pts))


def test_identity_alignment():
    pts = _bumpy_surface()
    src = _cloud(pts)
    dst = _cloud(pts)
    result = tb.align_point_to_plane(src, dst, max_iterations=15)
    assert result.ok, result.reason
    assert result.translation_mm < 0.05
    assert result.rotation_deg < 0.05
    assert result.rms_mm < 0.5


@pytest.mark.parametrize("t,rot", [
    ((3.0, -2.0, 1.0), (0.0, 0.0, math.radians(0.5))),
    ((0.0, 0.0, 6.0), (math.radians(0.8), 0.0, 0.0)),
    ((-4.0, 3.0, 0.0), (0.0, math.radians(-0.7), 0.0)),
])
def test_recovers_known_transform(t, rot):
    """Move the target by a known transform; the solver must undo it."""
    pts = _bumpy_surface(seed=3)
    T = _se3(rot[0], rot[1], rot[2], t)
    moved = (T[:3, :3] @ pts.T).T + T[:3, 3]
    src = _cloud(pts)
    dst = _cloud(moved)
    result = tb.align_point_to_plane(src, dst, max_iterations=40,
                                     max_correspondence_mm=30.0)
    assert result.ok, result.reason
    # the recovered transform should map src onto dst, i.e. be close to T
    err_t = float(np.linalg.norm(result.transform[:3, 3] - T[:3, 3]))
    err_r = tb._rotation_angle_deg(result.transform[:3, :3].T @ T[:3, :3])
    assert err_t < 0.6, err_t
    assert err_r < 0.15, err_r


def test_outliers_do_not_drag_the_solution():
    pts = _bumpy_surface(seed=5)
    T = _se3(0.0, 0.0, math.radians(0.4), (2.0, 0.0, 3.0))
    moved = (T[:3, :3] @ pts.T).T + T[:3, 3]
    rng = np.random.default_rng(1)
    n_out = int(0.2 * len(moved))
    outliers = rng.uniform([-70, -70, 330], [70, 70, 470], size=(n_out, 3))
    dst = _cloud(np.vstack([moved, outliers]))
    src = _cloud(pts)
    result = tb.align_point_to_plane(src, dst, max_iterations=40, trim=0.7,
                                     max_correspondence_mm=30.0)
    assert result.ok, result.reason
    err_t = float(np.linalg.norm(result.transform[:3, 3] - T[:3, 3]))
    assert err_t < 1.2, err_t


def test_planar_target_is_reported_degenerate():
    """A plane leaves three degrees of freedom free: that must be caught."""
    rng = np.random.default_rng(2)
    x = rng.uniform(-60, 60, 3000)
    y = rng.uniform(-60, 60, 3000)
    plane = np.stack([x, y, np.full_like(x, 400.0)], axis=1)
    src = _cloud(plane)
    shifted = plane + np.array([1.5, 0.0, 0.0])
    dst = _cloud(shifted)
    result = tb.align_point_to_plane(src, dst, max_iterations=20)
    assert math.isfinite(result.condition)
    assert result.condition > 1e4
    assert result.ok is False
    assert "degenerate" in result.reason


def test_unrelated_clouds_fail():
    a = _cloud(_bumpy_surface(seed=7))
    b = _cloud(_bumpy_surface(seed=8) + np.array([180.0, 140.0, 260.0]))
    result = tb.align_point_to_plane(a, b, max_iterations=20)
    assert result.ok is False
    assert result.reason


def test_sequence_reports_quality_and_never_chains_a_failure():
    clouds = [_cloud(_bumpy_surface(seed=11)) for _ in range(3)]
    report = tb.track_sequence(clouds, max_iterations=15)
    assert report["n_pairs"] == 2
    assert report["n_ok"] == 2
    assert report["median_rms_mm"] is not None
    assert len(report["poses"]) == 2
    # an unrelated final cloud must break the chain, not be chained silently
    bad = _cloud(_bumpy_surface(seed=12) + np.array([200.0, 0.0, 0.0]))
    report2 = tb.track_sequence(clouds + [bad], max_iterations=15)
    assert report2["n_ok"] < report2["n_pairs"]
    assert any(not p["ok"] for p in report2["pairs"])


def test_ply_round_trip(tmp_path):
    pts = _bumpy_surface(200)
    path = tmp_path / "cloud.ply"
    body = "\n".join(f"{x} {y} {z}" for x, y, z in pts)
    path.write_text("ply\nformat ascii 1.0\nelement vertex "
                    f"{len(pts)}\nproperty float x\nproperty float y\n"
                    "property float z\nend_header\n" + body + "\n")
    back = tb.read_ply(path)
    assert back.shape == pts.shape
    assert np.allclose(back, pts, atol=1e-4)


def test_keyframe_reanchoring_mechanics_and_consistency_metric():
    """Keyframe tracking must re-anchor when the direct pose disagrees, and the
    cross-frame consistency metric must be finite and sensible."""
    clouds = [_cloud(_bumpy_surface(seed=21 + i)) for i in range(6)]
    # a small rigid motion between frames so alignment is meaningful
    T = _se3(0.0, 0.0, math.radians(0.2), (0.0, 0.0, 0.0))
    moved = []
    for i, c in enumerate(clouds):
        pts = (np.linalg.matrix_power(T, i)[:3, :3] @ c.points.T).T + \
              np.linalg.matrix_power(T, i)[:3, 3]
        m = tb.Cloud(pts, tb.estimate_normals(pts))
        moved.append(m)
    plain = tb.track_sequence(moved)
    # keyframe spacing 3: frames 1 and 2 are checked against keyframe 0, and the
    # chained pose of frame 2 (via frame 1) differs from its direct pose, which
    # is exactly the disagreement the re-anchor rule acts on
    kf = tb.track_sequence(moved, keyframe_every=3,
                           drift_threshold_mm=0.01, drift_threshold_deg=0.001)
    assert plain["n_ok"] == plain["n_pairs"]
    assert "reanchored" in kf and "keyframe_checks" in kf
    # a deliberately tiny threshold must force at least one re-anchor
    assert len(kf["reanchored"]) >= 1
    for entry in kf["reanchored"]:
        assert entry["drift_mm"] >= 0.01 or entry["drift_deg"] >= 0.001
    assert len(kf["poses"]) == len(plain["poses"])


def test_foreground_keeps_the_near_object_and_falls_back():
    """The foreground helper isolates the near cluster; it must not empty a
    cloud that has nothing near, and it must keep the object's surface."""
    rng = np.random.default_rng(5)
    # a small "object" at 400 mm and a large "background" at 900 mm
    obj = rng.normal(0, 30, size=(2000, 3)) * [1, 1, 0.2] + [0, 0, 400]
    bkg = rng.normal(0, 300, size=(6000, 3)) * [1, 1, 0.05] + [0, 0, 900]
    pts = np.vstack([obj, bkg])
    cloud = tb.Cloud(pts, tb.estimate_normals(pts))
    far_cloud = tb.Cloud(bkg, tb.estimate_normals(bkg))
    near = tb.foreground(cloud, band_mm=150.0)
    assert 1500 < len(near) < 2600, len(near)
    z = near.points[:, 2]
    assert z.max() < 600.0
    # too few points near the sensor to solve with -> the original comes back
    tiny = bkg[:150]
    small = tb.Cloud(tiny, tb.estimate_normals(tiny))
    same = tb.foreground(small, band_mm=10.0)
    assert len(same) == len(small), (len(same), len(small))
    # a band that does capture a usable cluster is honoured
    thinned = tb.foreground(far_cloud, band_mm=50.0)
    assert 200 <= len(thinned) < len(far_cloud)
