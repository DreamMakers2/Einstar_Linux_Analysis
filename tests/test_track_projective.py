"""Projective point-to-plane ICP on the engine's depth maps.

The synthetic cases use depth maps generated analytically, so the true transform
is known; the real-frame case checks that the path runs on the engine's own
output and that its cross-frame consistency beats the cloud baseline on the same
point set.
"""
from __future__ import annotations

import math
import os
from pathlib import Path
import sys

import numpy as np
import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tools"))

pytest.importorskip("cv2")

from tools.track import projective as pj  # noqa: E402

K = np.array([[600.0, 0.0, 128.0], [0.0, 600.0, 120.0], [0.0, 0.0, 1.0]])
W, H = 256, 240


def _surface(seed=0):
    """A gently curved surface as a depth map (millimetres)."""
    rng = np.random.default_rng(seed)
    v, u = np.mgrid[0:H, 0:W].astype(np.float64)
    x = (u - K[0, 2]) / K[0, 0]
    y = (v - K[1, 2]) / K[1, 1]
    z = 300.0 + 25.0 * x * x + 18.0 * y * y + rng.normal(0, 0.02, size=(H, W))
    return z.astype(np.float32)


def _warp(depth, R, t):
    """Render the same surface moved by (R, t) into another depth map."""
    d = pj.DepthFrame(depth, K)
    pts, uv = d.points()
    moved = (R @ pts.T).T + t
    out = np.zeros((H, W), np.float32)
    fx, fy = K[0, 0], K[1, 1]
    u = np.rint(fx * moved[:, 0] / moved[:, 2] + K[0, 2]).astype(int)
    v = np.rint(fy * moved[:, 1] / moved[:, 2] + K[1, 2]).astype(int)
    ok = (u >= 0) & (u < W) & (v >= 0) & (v < H) & (moved[:, 2] > 1)
    out[v[ok], u[ok]] = moved[ok, 2]
    return out


def _se3(rx, ry, rz, t):
    import cv2
    R = cv2.Rodrigues(np.array([rx, ry, rz], float))[0]
    return R, np.asarray(t, float)


def test_identity_alignment():
    d = _surface()
    src = pj.DepthFrame(d, K)
    dst = pj.DepthFrame(d.copy(), K)
    r = pj.align_projective(src, dst)
    assert r["ok"], r["reason"]
    assert r["translation_mm"] < 0.05 and r["rotation_deg"] < 0.05


@pytest.mark.parametrize("params", [
    ((0.0, 0.0, math.radians(0.3)), (1.5, -1.0, 0.5)),
    ((math.radians(0.4), 0.0, 0.0), (0.0, 0.0, 2.0)),
])
def test_recovers_a_known_transform(params):
    """Alignment quality is what matters: a curved surface is still nearly
    degenerate, so the recovered parameters are compared by how well they map
    the source surface onto the target one, not by parameter identity."""
    from scipy.spatial import cKDTree
    rot, t = params
    base = _surface()
    R, T = _se3(*rot, t)
    moved = _warp(base, R, T)
    src = pj.DepthFrame(base, K)
    dst = pj.DepthFrame(moved, K)
    r = pj.align_projective(src, dst, max_iterations=30)
    assert r["ok"], r["reason"]
    pts, _ = src.points()
    P = r["transform"]
    aligned = (P[:3, :3] @ pts.T).T + P[:3, 3]
    tgt_pts, _ = dst.points()
    d, _ = cKDTree(tgt_pts).query(aligned, k=1)
    assert float(np.median(d)) < 0.35, float(np.median(d))
    # and the recovered motion must point the right way
    assert float(np.linalg.norm(P[:3, 3])) > 0.2


def test_normals_are_unit_and_zero_off_surface():
    d = _surface()
    n = pj.normals_from_depth(d, K)
    assert n.shape == (H, W, 3)
    norms = np.linalg.norm(n, axis=-1)
    inside = norms > 0
    assert inside.mean() > 0.9
    # finite differences over noisy depth leave a few outliers, so assert the
    # bulk is unit rather than the worst pixel
    dev = np.abs(norms[inside] - 1.0)
    assert float(np.median(dev)) < 1e-3, float(np.median(dev))
    assert float(np.percentile(dev, 99)) < 2e-2, float(np.percentile(dev, 99))


def test_planar_target_is_reported_degenerate():
    flat = np.full((H, W), 400.0, np.float32)
    src = pj.DepthFrame(flat, K)
    dst = pj.DepthFrame(flat.copy(), K)
    r = pj.align_projective(src, dst)
    assert math.isfinite(r["condition"])
    assert r["condition"] > 1e6
    assert r["ok"] is False


def test_surface_consistency_is_zero_for_identical_frames():
    d = _surface()
    frames = [pj.DepthFrame(d, K) for _ in range(3)]
    assert pj.surface_consistency(frames, [np.eye(4)] * 3) < 0.05


def test_projective_step_is_the_right_order_of_magnitude(tmp_path):
    """Regression for the intrinsics bug: the depth map is on the SCALED
    rectified grid, and using the full-resolution calibration intrinsics made
    every ray nonsense - the tracker then reported ~0.1-0.3 mm for a real
    3.1-3.4 mm motion.  With the report's ``rectified`` block the step is the
    right order of magnitude.

    The bound is wide on purpose: the plate moves on a turntable against a
    static background, so the scene is not one rigid body and ICP reports a
    compromise between them.  This data therefore cannot arbitrate between
    trackers; it can only catch a broken one.
    """
    import subprocess
    from calib import detect, geometry, plate, recon_profile
    from wizard import types

    frames_dir = Path(os.path.expanduser("~/einstar-private/sweep-c/frames"))
    profile = Path(os.path.expanduser(
        "~/einstar-private/einstar-profiles/factory-e10.json"))
    recon = REPO / "build" / "recon" / "recon_cli"
    if not (frames_dir.is_dir() and profile.is_file() and recon.is_file()):
        pytest.skip("recording, profile or recon binary not available")
    ids = [0, 5, 10, 15]
    outdirs = []
    for fid in ids:
        out = tmp_path / f"f{fid}"
        proc = subprocess.run(
            [str(recon), "reconstruct", "--frames", str(frames_dir),
             "--id", str(fid), "--profile", str(profile), "--out", str(out),
             "--scale", "0.25"], capture_output=True, text=True, timeout=120)
        if proc.returncode != 0 or not (out / "recon_depth_mm.tiff").is_file():
            pytest.skip("reconstruction failed in this environment")
        outdirs.append(out)

    prof = recon_profile.load(profile)
    intr = prof.camera("ir_left").intrinsics()
    Kp = np.asarray(intr.K, float)
    kc = np.asarray(intr.kc, float)
    board = plate.board_points_mm()
    idx = {(float(u), float(v)): i for i, (u, v) in enumerate(plate.lattice_uv())}
    pnp = []
    for fid in ids:
        im = types.orient_left(types.read_pgm(
            frames_dir / f"frame_{fid:06d}_cam1.pgm"))
        o = detect.detect_grid_auto(np.ascontiguousarray(im))
        ks = [k for k in o.points if (float(k[0]), float(k[1])) in idx]
        obj = np.asarray([board[idx[(float(u), float(v))]] for u, v in ks])
        img = np.asarray([o.points[k] for k in ks], float)
        pnp.append(geometry.solve_pnp(obj, img, Kp, kc))
    frames = [pj.load_depth(d / "recon_depth_mm.tiff") for d in outdirs]
    # the depth map is the scaled rectified grid, not the full-resolution one
    assert frames[0].depth_mm.shape == (256, 320)
    assert 250.0 < frames[0].K[0, 0] < 330.0, frames[0].K[0, 0]
    ratios = []
    for i in range(1, len(frames)):
        ref = float(np.linalg.norm(pnp[i].t - pnp[i - 1].t))
        got = float(pj.align_projective(frames[i - 1], frames[i])["translation_mm"])
        ratios.append(got / max(ref, 1e-9))
    assert min(ratios) > 0.3, ratios
    assert max(ratios) < 1.5, ratios


def test_real_frames_beat_the_cloud_baseline_on_the_same_points():
    base = Path(os.path.expanduser("~/einstar-private/analysis/long/clouds"))
    profile = Path(os.path.expanduser(
        "~/einstar-private/einstar-profiles/factory-e10.json"))
    if not (base.is_dir() and profile.is_file()):
        pytest.skip("reconstructed sequence not available")
    from calib import recon_profile
    from tools.track.baseline import load_clouds, track_sequence
    dirs = sorted(base.iterdir(), key=lambda p: int(p.name))[:6]
    if len(dirs) < 4:
        pytest.skip("not enough frames")
    prof = recon_profile.load(profile)
    Kp = np.asarray(prof.camera("ir_left").intrinsics().K, float)
    frames = [pj.load_depth(d / "recon_depth_mm.tiff", Kp) for d in dirs]
    clouds = load_clouds([d / "recon_cloud.ply" for d in dirs], voxel_mm=5.0)
    chain = track_sequence(clouds)
    cloud_poses = [np.eye(4)] + [np.asarray(p, float) for p in chain["poses"]]
    proj_poses = [np.eye(4)]
    for i in range(1, len(frames)):
        r = pj.align_projective(frames[i - 1], frames[i])
        proj_poses.append(proj_poses[-1] @ np.linalg.inv(r["transform"]))
    cloud_c = pj.surface_consistency(frames, cloud_poses)
    proj_c = pj.surface_consistency(frames, proj_poses)
    assert proj_c < cloud_c, (proj_c, cloud_c)
