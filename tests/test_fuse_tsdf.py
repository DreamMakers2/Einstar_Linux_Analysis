"""Tests for confidence-weighted TSDF fusion and meshing (tools/fuse/tsdf.py).

Accuracy is measured against analytic surfaces (a sphere) where the answer is
known, and pose sensitivity is measured by deliberately corrupting a pose: a
fusion that is insensitive to pose would be useless, and one that hides a bad
pose would corrupt the model.
"""
from __future__ import annotations

import math
from pathlib import Path
import sys

import numpy as np
import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

pytest.importorskip("skimage")

from tools.fuse import tsdf  # noqa: E402
from tools.track.baseline import Cloud  # noqa: E402


def _sphere(radius=40.0, centre=(0.0, 0.0, 300.0), n=6000, seed=0):
    rng = np.random.default_rng(seed)
    u = rng.normal(size=(n, 3))
    u /= np.linalg.norm(u, axis=1, keepdims=True)
    pts = np.asarray(centre, float) + u * radius
    return Cloud(pts, u.copy())


def _plane(seed=1, n=4000):
    rng = np.random.default_rng(seed)
    x = rng.uniform(-60, 60, n)
    y = rng.uniform(-50, 50, n)
    pts = np.stack([x, y, np.full_like(x, 400.0)], axis=1)
    normals = np.tile(np.array([0.0, 0.0, -1.0]), (n, 1))
    return Cloud(pts, normals)


def _rot_y(deg):
    a = math.radians(deg)
    return np.array([[math.cos(a), 0, math.sin(a)],
                     [0, 1, 0],
                     [-math.sin(a), 0, math.cos(a)]])


def test_sphere_accuracy_at_default_voxel():
    cloud = _sphere()
    out = tsdf.fuse_clouds([cloud], [np.eye(4)], voxel_mm=2.0, trunc_mm=6.0)
    verts, faces = out.pop("_mesh")
    assert len(verts) > 2000 and len(faces) > 4000
    radius = np.linalg.norm(verts - np.array([0.0, 0.0, 300.0]), axis=1)
    assert abs(float(radius.mean()) - 40.0) < 0.2, radius.mean()
    assert float(np.percentile(np.abs(radius - 40.0), 95)) < 0.3


def test_plane_is_flat():
    cloud = _plane()
    out = tsdf.fuse_clouds([cloud], [np.eye(4)], voxel_mm=2.0, trunc_mm=6.0)
    verts, _ = out.pop("_mesh")
    assert len(verts) > 500
    z = verts[:, 2]
    assert abs(float(np.median(z)) - 400.0) < 0.3
    # the measured surface must be flat; the rim is trimmed, not invented
    assert float(np.percentile(np.abs(z - 400.0), 95)) < 0.5


def test_two_views_agree_and_a_wrong_pose_thickens_the_surface():
    cloud = _sphere()
    # second view: the object rotated about its own centre, so a correct pose
    # puts it back exactly where the first view had it
    T = np.eye(4)
    T[:3, :3] = _rot_y(12.0)
    centre = np.array([0.0, 0.0, 300.0])
    T[:3, 3] = centre - T[:3, :3] @ centre
    good = tsdf.fuse_clouds([cloud, cloud], [np.eye(4), T], voxel_mm=2.0,
                            trunc_mm=6.0)
    gv, _ = good.pop("_mesh")
    g_rad = np.linalg.norm(gv - centre, axis=1)
    assert float(np.percentile(np.abs(g_rad - 40.0), 95)) < 0.4

    bad_T = T.copy()
    bad_T[:3, 3] += np.array([6.0, 0.0, 0.0])       # a deliberately wrong pose
    bad = tsdf.fuse_clouds([cloud, cloud], [np.eye(4), bad_T], voxel_mm=2.0,
                           trunc_mm=6.0)
    bv, _ = bad.pop("_mesh")
    b_rad = np.linalg.norm(bv - centre, axis=1)
    assert float(np.percentile(np.abs(b_rad - 40.0), 95)) > \
        float(np.percentile(np.abs(g_rad - 40.0), 95)) * 2.0


def test_failed_tracking_is_never_integrated():
    cloud = _sphere()
    out = tsdf.fuse_clouds([cloud, cloud, cloud], [np.eye(4)] * 3,
                           ok=[True, False, False], voxel_mm=3.0, trunc_mm=9.0,
                           mesh=False)
    assert out["frames_integrated"] == 1


def test_volume_cap_is_enforced():
    cloud = _sphere()
    with pytest.raises(ValueError):
        tsdf.fuse_clouds([cloud], [np.eye(4)], voxel_mm=0.5, mesh=False,
                         trunc_mm=1.5, max_voxels=100_000)


def test_obj_round_trip(tmp_path):
    cloud = _sphere(n=1500)
    out = tsdf.fuse_clouds([cloud], [np.eye(4)], voxel_mm=4.0, trunc_mm=12.0)
    verts, faces = out.pop("_mesh")
    path = tsdf.write_obj(tmp_path / "sphere.obj", verts, faces, comment="test")
    text = path.read_text().splitlines()
    v = [ln for ln in text if ln.startswith("v ")]
    f = [ln for ln in text if ln.startswith("f ")]
    assert len(v) == len(verts)
    assert len(f) == len(faces)
    assert "units=mm" in text[1]


def test_weighted_average_reduces_noise():
    """Repeated noisy observations of one surface must average, not jitter."""
    rng = np.random.default_rng(3)
    base = _plane()
    noisy = []
    for i in range(5):
        pts = base.points + rng.normal(0, 0.5, base.points.shape)
        noisy.append(Cloud(pts, base.normals))
    single = tsdf.fuse_clouds(noisy[:1], [np.eye(4)], voxel_mm=2.0,
                              trunc_mm=6.0)
    many = tsdf.fuse_clouds(noisy, [np.eye(4)] * 5, voxel_mm=2.0, trunc_mm=6.0)
    sv, _ = single.pop("_mesh")
    mv, _ = many.pop("_mesh")
    s_spread = float(np.std(sv[:, 2]))
    m_spread = float(np.std(mv[:, 2]))
    assert m_spread <= s_spread + 0.15, (s_spread, m_spread)


def test_coloured_export_round_trip(tmp_path):
    """Per-vertex colour must survive into PLY and OBJ."""
    from tools.scan import export as scan_export

    verts = np.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]])
    faces = np.array([[0, 1, 2]])
    colours = np.array([[255, 0, 0], [0, 255, 0], [0, 0, 255]], np.uint8)
    written = scan_export.export_mesh(tmp_path, verts, faces,
                                      formats=("obj", "ply"), colors=colours)
    ply = Path(written["ply"]).read_text()
    assert "property uchar red" in ply and "property uchar green" in ply
    body = [ln for ln in ply.splitlines() if ln and ln[0].isdigit()]
    assert body and body[0].split()[3:] == ["255", "0", "0"]
    obj = Path(written["obj"]).read_text().splitlines()
    v = [ln for ln in obj if ln.startswith("v ")]
    assert len(v) == 3
    parts = v[0].split()
    assert len(parts) == 7, parts            # x y z r g b
    assert abs(float(parts[4]) - 1.0) < 1e-6  # red channel = 1.0
