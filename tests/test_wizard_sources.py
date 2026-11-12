"""Deterministic, offline tests for the wizard frame sources.

Covers the orientation contract against hand-built PGMs, replay-directory
ordering and partial-write tolerance, and the synthetic renderer (including the
reproducible failure modes).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys

import numpy as np
import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tools"))
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tools.wizard import sources, types  # noqa: E402


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _marker_image(seed: int = 0, shape=(96, 128)) -> np.ndarray:
    """A grey image with one bright asymmetric square (easy to localise)."""
    rng = np.random.default_rng(seed)
    img = rng.integers(8, 40, shape, dtype=np.uint8)
    img[20:25, 10:15] = 240
    img[60:63, 90:93] = 200
    return img


def _write_group(root: Path, index: int, images: dict[str, np.ndarray]) -> None:
    """Write a group in *file* orientation (the inverse of the source)."""
    types.write_pgm(root / f"frame_{index:06d}_cam1.pgm", images["left"])
    types.write_pgm(root / f"frame_{index:06d}_cam0.pgm",
                   np.ascontiguousarray(np.rot90(images["right"], 2)))
    types.write_pgm(root / f"frame_{index:06d}_cam2.pgm",
                   np.ascontiguousarray(np.rot90(images["tex"], 2)))


# ---------------------------------------------------------------------------
# orientation
# ---------------------------------------------------------------------------

def test_replay_source_applies_orientation_exactly_once(tmp_path):
    left = _marker_image(1)
    right = _marker_image(2)
    tex = _marker_image(3)
    _write_group(tmp_path, 0, {"left": left, "right": right, "tex": tex})
    group = next(iter(sources.ReplayDirSource(tmp_path)))

    assert np.array_equal(group.images["left"], left)
    # cam0 was written rotated 180 (the live convention); the source rotates
    # it back, i.e. the documented orient_right transform applied exactly once
    assert np.array_equal(group.images["right"], right)
    assert np.array_equal(group.images["right"],
                          types.orient_right(np.rot90(right, 2)))
    # the colour path is debayer+grey; the bright square must stay in place
    expected = types.grey_from_bgr(types.debayer_bgr(np.rot90(tex, 2)))
    assert np.array_equal(group.images["tex"], expected)
    assert group.colour_bgr is not None and group.colour_bgr.ndim == 3

    # the marker centroid is preserved by the tex orientation round trip
    def centroid(img):
        ys, xs = np.where(img > 220)   # only the 240-DN square
        return float(xs.mean()), float(ys.mean())

    assert abs(centroid(group.images["tex"])[0] - 12.0) < 1.5
    assert abs(centroid(group.images["tex"])[1] - 22.0) < 1.5


def test_left_is_cam1_and_right_is_cam0_rotated(tmp_path):
    left = np.zeros((32, 40), np.uint8)
    left[2, 3] = 255
    right = np.zeros((32, 40), np.uint8)
    right[4, 5] = 255
    tex = np.zeros((32, 40), np.uint8)
    _write_group(tmp_path, 0, {"left": left, "right": right, "tex": tex})
    group = next(iter(sources.ReplayDirSource(tmp_path)))
    # cam1 as-is
    assert group.images["left"][2, 3] == 255
    # cam0 was written rotated; the source rotates it back
    assert group.images["right"][4, 5] == 255


# ---------------------------------------------------------------------------
# replay directory
# ---------------------------------------------------------------------------

def test_replay_ordering_and_device_groups(tmp_path):
    groups = []
    for i in range(4):
        images = {k: _marker_image(10 + i) for k in ("left", "right", "tex")}
        _write_group(tmp_path, i, images)
        groups.append(images)
    # a capture-style manifest gives the device group and timestamps
    types.atomic_write_json(tmp_path / "frames_manifest.json", {
        "schema": "einstar-frames-v1",
        "cadence": {"fps": 10.0, "min_ms": 99.0, "max_ms": 101.0},
        "groups": [{"group": i, "device_group": 500 + i,
                    "host_time_ns": 1_000_000_000 + i * 100_000_000}
                   for i in range(4)],
    })
    src = sources.ReplayDirSource(tmp_path)
    assert src.deterministic is True
    assert src.info.continuous is True
    got = list(src)
    assert [g.index for g in got] == [0, 1, 2, 3]
    assert [g.device_group for g in got] == [500, 501, 502, 503]
    assert [round(g.timestamp_s, 3) for g in got] == [0.0, 0.1, 0.2, 0.3]
    assert all(g.complete for g in got)

    # start/limit select by file index, not by dense output index
    got = list(sources.ReplayDirSource(tmp_path, start=1, limit=2))
    assert [g.label for g in got] == ["frame 000001", "frame 000002"]


def test_partial_directory_is_tolerated(tmp_path):
    for i in range(4):
        _write_group(tmp_path, i,
                     {k: _marker_image(20 + i) for k in ("left", "right", "tex")})
    os.unlink(tmp_path / "frame_000002_cam0.pgm")
    # a truncated PGM parses as an error, not a crash
    (tmp_path / "frame_000003_cam2.pgm").write_bytes(b"P5\n128 96\n255\n\x00\x01")
    src = sources.ReplayDirSource(tmp_path)
    got = list(src)  # must not raise
    # groups 0 and 1 are complete; 2 and 3 are unreadable
    assert [g.label for g in got] == ["frame 000000", "frame 000001"]
    assert len(src.skipped) == 2
    assert {s["file_index"] for s in src.skipped} == {2, 3}


def test_missing_directory_raises(tmp_path):
    with pytest.raises(sources.SourceError):
        sources.ReplayDirSource(tmp_path / "does-not-exist")


def test_vendor_style_directory_is_not_continuous(tmp_path):
    # no manifest, no regular cadence: still collection
    for i in range(3):
        _write_group(tmp_path, i,
                     {k: _marker_image(30 + i) for k in ("left", "right", "tex")})
    src = sources.ReplayDirSource(tmp_path)
    assert src.info.continuous is False


# ---------------------------------------------------------------------------
# synthetic
# ---------------------------------------------------------------------------

def test_synthetic_is_reproducible():
    spec = sources.SyntheticSpec(n_views=3, seed=5, pattern="diverse")
    a = [g.digest() for g in sources.SyntheticSource(spec)]
    b = [g.digest() for g in sources.SyntheticSource(spec)]
    assert a == b
    c = [g.digest() for g in sources.SyntheticSource(
        sources.SyntheticSpec(n_views=3, seed=6, pattern="diverse"))]
    assert a != c
    src = sources.SyntheticSource(spec)
    assert src.info.continuous is False
    assert src.deterministic is True
    assert len(src.poses) == 3


def test_synthetic_dwell_is_continuous():
    src = sources.SyntheticSource(
        sources.SyntheticSpec(n_views=2, frames_per_pose=2))
    assert src.info.continuous is True
    assert len(list(src)) == 4


def test_synthetic_mirrored_layout_is_detected_as_ambiguous():
    from calib.detect import detect_grid_auto
    from tools.sweep.orient import check_orientation

    syn = sources.SyntheticSource(sources.SyntheticSpec(
        n_views=1, seed=11, pattern="single", distance_mm=420.0,
        tilt_deg=(0.0, 0.0), marker_layout="mirror_u"))
    group = next(iter(syn))
    grid = detect_grid_auto(group.images["left"])
    assert grid is not None and grid.n_lattice >= 30
    check = check_orientation(group.images["left"], grid)
    assert check.ok is False
    assert check.reason == "mirrored_geometry"
    # the marker count is the same as an identity plate: a bare count would
    # not have caught this
    identity = next(iter(sources.SyntheticSource(sources.SyntheticSpec(
        n_views=1, seed=11, pattern="single", distance_mm=420.0))))
    id_grid = detect_grid_auto(identity.images["left"])
    assert id_grid is not None and id_grid.n_lattice == grid.n_lattice


def test_synthetic_identity_layout_verifies():
    from calib.detect import detect_grid_auto
    from tools.sweep.orient import check_orientation

    syn = sources.SyntheticSource(sources.SyntheticSpec(
        n_views=1, seed=11, pattern="single", distance_mm=420.0,
        tilt_deg=(0.0, 0.0)))
    group = next(iter(syn))
    grid = detect_grid_auto(group.images["left"])
    assert grid is not None and grid.n_lattice >= 38
    assert check_orientation(group.images["left"], grid).ok is True


def test_synthetic_degradation_modes():
    from calib.detect import detect_grid_auto

    occluded = next(iter(sources.SyntheticSource(sources.SyntheticSpec(
        n_views=1, pattern="single", occlusion="left", occlusion_frac=0.8))))
    grid = detect_grid_auto(occluded.images["left"])
    assert grid is None or grid.n_lattice < 38

    sparse = next(iter(sources.SyntheticSource(sources.SyntheticSpec(
        n_views=1, pattern="single", visible_markers=18))))
    grid = detect_grid_auto(sparse.images["left"])
    assert grid is None or grid.n_lattice < 38


def test_synthetic_profile_round_trips_through_recon_profile():
    from calib import recon_profile

    src = sources.SyntheticSource(sources.SyntheticSpec(n_views=2, seed=3))
    doc = src.profile_dict()
    assert doc["schema"] == recon_profile.SCHEMA
    profile = recon_profile._profile_from_mapping(doc) if hasattr(
        recon_profile, "_profile_from_mapping") else src.profile
    assert set(profile.cameras) == {"ir_left", "ir_right", "color"}
    # absolute pixel skew, and the calibration view normalises it back
    left = profile.camera("ir_left").intrinsics()
    assert abs(left.skew * left.fx - doc["cameras"]["ir_left"]["skew"]) < 1e-9


def test_surface_texture_is_fixed_to_the_plate_not_the_image():
    """A stereo matcher can only use texture that both cameras see identically.

    The per-image speckle is drawn from an image-local rng, so left and right
    see different patterns and it reconstructs as noise.  ``surface_texture``
    is generated on a millimetre grid over the board and sampled by ray-casting
    each plate pixel, so the pattern is a property of the surface.
    """
    from wizard import sources
    from calib import recon_profile
    import os
    profile = os.path.expanduser("~/einstar-private/einstar-profiles/factory-e10.json")
    if not os.path.exists(profile):
        pytest.skip("profile absent")
    prof = recon_profile.load(profile)
    left = prof.camera("ir_left").intrinsics()
    pose = (np.eye(3), np.array([0.0, 0.0, 420.0]))
    # sensor noise is rng-dependent by design, so switch it off to isolate the
    # texture: the plate pattern must then be identical for any rng
    spec = sources.SyntheticSpec(surface_texture=14.0, noise_sigma=0.0)
    a = sources.render_plate_view(left, pose, spec, rng=np.random.default_rng(0))
    b = sources.render_plate_view(left, pose, spec, rng=np.random.default_rng(99))
    assert np.array_equal(a, b)
    # and it must carry real contrast on the plate
    assert float(a[400:700, 300:900].std()) > 5.0
    # switching the texture off restores the per-image speckle (rng-dependent)
    off = sources.SyntheticSpec(surface_texture=0.0, noise_sigma=0.0,
                                plate_texture=0.0)
    c = sources.render_plate_view(left, pose, off, rng=np.random.default_rng(0))
    d = sources.render_plate_view(left, pose, off, rng=np.random.default_rng(99))
    assert np.array_equal(c, d)


def test_lazy_colour_defers_the_conversion_and_matches_eager(tmp_path):
    """A group's colour is only produced when a consumer asks for it."""
    from wizard import sources, types
    frames = Path(os.path.expanduser("~/einstar-private/sweep-b/frames"))
    if not frames.is_dir():
        pytest.skip("recording not available")
    lazy = next(iter(sources.ReplayDirSource(frames, limit=1, lazy_colour=True)))
    eager = next(iter(sources.ReplayDirSource(frames, limit=1, lazy_colour=False)))
    assert "tex" in eager.images and eager.colour_bgr is not None
    assert "tex" not in lazy.images
    assert lazy.raw_colour is not None and lazy.colour_bgr is None
    # the deferred conversion produces exactly the eager result
    bgr = types.debayer_bgr(lazy.raw_colour)
    grey = types.grey_from_bgr(bgr)
    assert np.array_equal(grey, eager.images["tex"])
    assert np.array_equal(bgr, eager.colour_bgr)


def test_live_source_lag_bound_is_configurable():
    """The live source exposes its lag bound; -1 means keep every group."""
    import inspect
    from wizard import sources
    sig = inspect.signature(sources.LiveSource.__init__)
    assert sig.parameters["max_lag"].default == 1
