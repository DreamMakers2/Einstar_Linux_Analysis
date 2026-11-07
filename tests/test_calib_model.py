#!/usr/bin/env python3
"""Tests for the three-camera calibration model and the rectification pipeline.

Fixture-free where possible: most tests build a synthetic rig from scratch and
check the mathematics (rectification geometry, Q matrix, DLT, PLY, JSON).
The fixture-backed tests read the CCF records in the repository and the private
device dump; they are *skipped* when those artefacts are absent.

pytest-style; pytest is optional::

    pytest tests/test_calib_model.py -q
    python3 tests/test_calib_model.py
"""

from __future__ import annotations

import json
import math
import struct
import tempfile
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT / "tools/calib"))
sys.path.insert(0, str(ROOT / "tools/ccf"))

import decode_ccf as C  # noqa: E402

from calib_model import (CCF_DIR, DUMP_PATH, Calibration, CameraModel,  # noqa: E402
                         StereoPair, distort_normalized, make_k,
                         undistort_normalized)
from rectify import (bouguet_rectification, remap_bilinear,  # noqa: E402
                     rectification_rotation)
from synth import run_round_trip, sample_scene  # noqa: E402
from triangulate import (read_ply_vertices, stats, triangulate_dlt,  # noqa: E402
                         xyz_to_ply)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _rot_y(deg: float) -> np.ndarray:
    r = math.radians(deg)
    return np.array([[math.cos(r), 0.0, math.sin(r)],
                     [0.0, 1.0, 0.0],
                     [-math.sin(r), 0.0, math.cos(r)]])


def _rot_x(deg: float) -> np.ndarray:
    r = math.radians(deg)
    return np.array([[1.0, 0.0, 0.0],
                     [0.0, math.cos(r), -math.sin(r)],
                     [0.0, math.sin(r), math.cos(r)]])


def synthetic_calibration(image_size=(640, 480)) -> Calibration:
    """A physically-shaped three-camera rig (no device data involved)."""
    dist = np.array([-0.12, 0.09, 2.0e-4, -1.5e-4, 0.012])
    cams = {}
    left_R = _rot_y(0.0)
    cams["left"] = CameraModel("left", make_k(500.0, 499.5, 321.0, 243.0), dist,
                               left_R, np.zeros(3), image_size=image_size,
                               source="synthetic")
    # right camera: 60 mm to the right, yawed 8 deg, small offsets
    R_right = _rot_x(1.5) @ _rot_y(-8.0)
    C_right = np.array([60.0, 3.0, 2.0])
    cams["right"] = CameraModel("right", make_k(502.0, 501.0, 318.0, 240.0),
                                dist, R_right, -R_right @ C_right,
                                image_size=image_size, source="synthetic")
    # colour camera: 45 mm to the left and 20 mm down, different focal length
    R_tex = _rot_x(-2.0) @ _rot_y(5.0)
    C_tex = np.array([-45.0, -20.0, 4.0])
    cams["tex"] = CameraModel("tex", make_k(600.0, 600.5, 315.0, 245.0),
                              np.array([-0.10, 0.07, 1e-4, 1e-4, 0.01]),
                              R_tex, -R_tex @ C_tex,
                              image_size=image_size, source="synthetic")
    return Calibration(cameras=cams, image_size=image_size, source="synthetic")


def _require(path: Path, what: str):
    if not Path(path).exists():
        raise unittest.SkipTest(f"{what} not present at {path}")


# ---------------------------------------------------------------------------
# fixture-free: model maths
# ---------------------------------------------------------------------------

def test_synthetic_model_invariants():
    calib = synthetic_calibration()
    for cam in calib.cameras.values():
        assert np.abs(cam.R @ cam.R.T - np.eye(3)).max() < 1e-12
        assert abs(np.linalg.det(cam.R) - 1) < 1e-12
        assert abs(cam.K[1, 0]) == 0 and abs(cam.K[2, 0]) == 0 and abs(cam.K[2, 1]) == 0
    assert 50 < calib.baseline("left", "right") < 70


def test_projection_and_distortion_roundtrip():
    calib = synthetic_calibration()
    cam = calib.left
    X = np.array([[10.0, -20.0, 400.0], [-60.0, 30.0, 700.0], [0.0, 0.0, 300.0]])
    uv = cam.project(X)
    rays = cam.pixel_rays(uv)
    Xn = rays * 400.0
    # the ray direction must reproduce the original 3-D direction
    Xn = Xn / np.linalg.norm(Xn, axis=1, keepdims=True)
    X0 = X / np.linalg.norm(X, axis=1, keepdims=True)
    assert np.abs(Xn - X0).max() < 1e-12
    # normalized distortion is invertible
    g = np.linspace(-0.5, 0.5, 21)
    gx, gy = np.meshgrid(g, g)
    xd, yd = distort_normalized(gx, gy, cam.dist)
    xu, yu = undistort_normalized(xd, yd, cam.dist)
    assert max(np.abs(xu - gx).max(), np.abs(yu - gy).max()) < 1e-12


def test_relative_pose_is_consistent():
    calib = synthetic_calibration()
    R, t = calib.relative_pose("left", "right")
    Cb = -R.T @ t
    assert np.linalg.norm(Cb - calib.right.centre()) < 1e-12
    assert abs(np.linalg.norm(t) - calib.baseline("left", "right")) < 1e-12
    # X_right == R X_left + t for an arbitrary world point
    Xw = np.array([12.0, -7.0, 333.0])
    assert np.abs((R @ Xw + t) - calib.right.R @ Xw - calib.right.T).max() < 1e-12


def test_rectification_rotation_maps_baseline_to_x():
    T = np.array([60.0, 3.0, 2.0])
    R = rectification_rotation(T)
    assert np.abs(R @ R.T - np.eye(3)).max() < 1e-15
    assert abs(np.linalg.det(R) - 1) < 1e-15
    e = R @ (T / np.linalg.norm(T))
    assert np.allclose(e, [1.0, 0.0, 0.0], atol=1e-15)


def test_rectification_image_size_handling():
    for size in ((640, 480), (1280, 1024), (321, 197)):
        calib = synthetic_calibration(size)
        pair = StereoPair.from_calibration(calib, "left", "right")
        rect = bouguet_rectification(pair, size)
        w, h = size
        assert rect.map_x_a.shape == (h, w)
        assert rect.map_y_b.shape == (h, w)
        assert rect.Q.shape == (4, 4) and rect.P1.shape == (3, 4)
        assert np.isfinite(rect.map_x_a).all()
        assert rect.valid_fraction()["a"] > 0.4


def test_epipolar_rows_become_horizontal():
    calib = synthetic_calibration()
    pair = StereoPair.from_calibration(calib, "left", "right")
    rect = bouguet_rectification(pair, calib.image_size)
    X = np.array([[20.0, 10.0, 400.0], [-30.0, -15.0, 350.0], [0.0, 0.0, 500.0]])
    Xa = X @ rect.R1
    Xb = Xa @ pair.R.T + pair.t
    ra = rect.rectify_pixels_a(pair.a.project(Xa))
    rb = rect.rectify_pixels_b(pair.b.project(Xb))
    assert np.abs(ra[:, 1] - rb[:, 1]).max() < 1e-12
    d = ra[:, 0] - rb[:, 0]
    assert (d > 0).all()


def test_q_and_projection_matrices_agree():
    calib = synthetic_calibration()
    rect = bouguet_rectification(StereoPair.from_calibration(calib, "left", "right"),
                                 calib.image_size)
    rng = np.random.default_rng(11)
    X = rng.uniform([-100, -60, 300], [100, 60, 600], size=(200, 3))
    ra = rect.project_rectified(X, "a")
    rb = rect.project_rectified(X, "b")
    d = ra[:, 0] - rb[:, 0]
    Xq = rect.disparity_to_xyz(d, ra)
    assert np.abs(Xq - X).max() < 1e-9
    Xd = triangulate_dlt(rect.P1, rect.P2, ra, rb)
    assert np.abs(Xd - X).max() < 1e-8
    # depth relation
    assert np.abs(rect.depth(d) - X[:, 2]).max() < 1e-9


def test_maps_are_the_inverse_map():
    calib = synthetic_calibration()
    rect = bouguet_rectification(StereoPair.from_calibration(calib, "left", "right"),
                                 calib.image_size)
    h, w = rect.image_size[1], rect.image_size[0]
    vv, uu = np.mgrid[0:h:31, 0:w:31]
    mx = rect.map_x_a[vv, uu]
    my = rect.map_y_a[vv, uu]
    ok = (mx >= 0) & (mx <= w - 1) & (my >= 0) & (my <= h - 1)
    src = np.stack([mx[ok], my[ok]], axis=1)
    back = rect.rectify_pixels_a(src)
    assert np.abs(back - np.stack([uu[ok], vv[ok]], axis=1)).max() < 1e-3


def test_remap_bilinear_matches_manual_interpolation():
    img = np.arange(12, dtype=float).reshape(3, 4)
    mx = np.array([[0.0, 1.5], [2.25, 3.0]])
    my = np.array([[0.0, 1.0], [2.0, 1.5]])
    out = remap_bilinear(img, mx, my)
    assert abs(out[0, 0] - img[0, 0]) < 1e-12
    assert abs(out[0, 1] - 0.5 * (img[1, 1] + img[1, 2])) < 1e-12
    assert abs(out[1, 0] - (0.75 * img[2, 2] + 0.25 * img[2, 3])) < 1e-12


def test_synthetic_roundtrip_is_exact():
    calib = synthetic_calibration()
    rect = bouguet_rectification(StereoPair.from_calibration(calib, "left", "right"),
                                 calib.image_size)
    rt = run_round_trip(rect=rect, n=500, seed=5, z_range=(300.0, 600.0))
    s = rt.summary()
    assert rt.n_used > 100
    assert s["err3d_mm"]["max"] < 1e-8
    assert s["reprojection_left_px"]["max"] < 1e-9
    assert s["reprojection_right_px"]["max"] < 1e-9
    assert s["dlt_vs_q_mm"]["max"] < 1e-7


def test_integer_disparity_error_is_at_the_quantisation_floor():
    calib = synthetic_calibration()
    rect = bouguet_rectification(StereoPair.from_calibration(calib, "left", "right"),
                                 calib.image_size)
    rt = run_round_trip(rect=rect, n=800, seed=6, z_range=(300.0, 600.0),
                        integer_disparity=True)
    z = rt.true_xyz[:, 2]
    f = rect.K_new[0, 0]
    B = float(np.linalg.norm(rect.pair.centre_b_in_a()))
    floor = z * z / (f * B) * 0.5
    # the 3-D error is the depth error times the ray magnification
    # sqrt(1 + x^2 + y^2), which is < 1.5 over the image; rounding can add
    # at most half a pixel of disparity.
    assert np.all(rt.err3d() <= floor * 1.5 + 1e-9)


def test_ply_roundtrip():
    rng = np.random.default_rng(3)
    xyz = rng.normal(size=(37, 3)) * 100
    cols = rng.integers(0, 256, size=(37, 3))
    with tempfile.TemporaryDirectory() as td:
        for binary in (True, False):
            p = Path(td) / f"cloud_{binary}.ply"
            xyz_to_ply(p, xyz, cols, binary=binary)
            back = read_ply_vertices(p)
            assert back.shape == (37, 3)
            assert np.abs(back - xyz).max() < 1e-3   # float32 storage


def test_json_roundtrip():
    calib = synthetic_calibration()
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "m.json"
        calib.save_json(p)
        back = Calibration.load_json(p)
        assert set(back.cameras) == set(calib.cameras)
        for n in calib.cameras:
            assert np.allclose(back.cameras[n].K, calib.cameras[n].K)
            assert np.allclose(back.cameras[n].dist, calib.cameras[n].dist)
            assert np.allclose(back.cameras[n].R, calib.cameras[n].R)
            assert np.allclose(back.cameras[n].T, calib.cameras[n].T)
        assert back.image_size == calib.image_size


# ---------------------------------------------------------------------------
# fixture-backed: the real records
# ---------------------------------------------------------------------------

def test_ccf_key_difference_is_proved():
    _require(CCF_DIR / "LeftCCF.txt", "CCF records")
    for name in ("Left", "Right"):
        rec = C.decode_record((CCF_DIR / f"{name}CCF.txt").read_bytes(), name, "best")
        diff = rec.stored[0] - rec.stored[1]
        assert abs(diff - 12.0) < 0.5, (name, diff)
        assert abs(diff - 11.0) > 1.0 and abs(diff - 13.0) > 0.5


def test_ccf_model_golden_values():
    _require(CCF_DIR / "LeftCCF.txt", "CCF records")
    calib = Calibration.from_ccf_dir()
    assert abs(calib.left.fx - 1159.115416028666) < 1e-9
    assert abs(calib.right.cx - 635.501437028911) < 1e-9
    assert abs(calib.tex.fx - 1141.077627098664) < 1e-9
    assert abs(calib.baseline("left", "right") - 158.557027) < 5e-3
    assert abs(calib.relative_rotation_deg("left", "right") - 22.138282) < 1e-4


def test_dump_model_is_unkeyed_and_orthonormal():
    _require(DUMP_PATH, "device dump")
    calib = Calibration.from_dump()
    assert calib.source == "dump"
    for cam in calib.cameras.values():
        assert np.abs(cam.R @ cam.R.T - np.eye(3)).max() < 1e-12
        assert abs(np.linalg.det(cam.R) - 1) < 1e-12
        assert cam.extra is not None


def test_dump_field_order_is_kc_first():
    """d[4:9] is the CCF kc exactly; d[9] is *not* the CCF alpha."""
    _require(DUMP_PATH, "device dump")
    _require(CCF_DIR / "TexCCF.txt", "CCF records")
    buf = DUMP_PATH.read_bytes()
    recs = [np.array(struct.unpack_from("<22d", buf, C.CAMERA_RECORD_OFF + i * C.CAMERA_RECORD_STRIDE))
            for i in range(3)]
    tex = C.decode_record((CCF_DIR / "TexCCF.txt").read_bytes(), "Tex")
    # kc-first: the five distortion coefficients are d[4:9] (bit-identical to Tex)
    assert np.array_equal(recs[2][4:9], np.array(tex.stored[10:15]))
    # alpha-first would require d[4] to be alpha (|alpha| < 1e-2) -- it is k1
    assert abs(recs[2][4] + 0.151009833578517) < 1e-15
    # and d[9] is not the CCF alpha under any integer offset
    assert abs(recs[2][9] - tex.stored[8]) > 1e-4
    # the IR records' keyed kc differ from the dump's unkeyed kc by exactly
    # the proved integer key
    for name, v in zip(("Left", "Right"), recs[:2]):
        stored = np.array(C.decode_record((CCF_DIR / f"{name}CCF.txt").read_bytes(),
                                          name).stored[10:15])
        assert np.allclose(stored - v[4:9], [0, 6, 2, 7, 13], atol=1e-12), (name, stored - v[4:9])


def test_alpha_key_and_rotation_key_are_proved():
    _require(CCF_DIR / "LeftCCF.txt", "CCF records")
    for name in ("Left", "Right"):
        rec = C.decode_record((CCF_DIR / f"{name}CCF.txt").read_bytes(), name)
        assert abs(rec.stored[8] - (-2.0)) < 1e-3     # alpha close to an integer
        assert abs(rec.alpha) < 1e-2
    # rotation key gives an exact rotation
    for name in ("Left", "Right"):
        calib = Calibration.from_ccf_dir()
        R = calib.cameras[name.lower()].R
        assert np.abs(R @ R.T - np.eye(3)).max() < 1e-14


def test_two_models_agree_on_distortion():
    _require(DUMP_PATH, "device dump")
    _require(CCF_DIR / "LeftCCF.txt", "CCF records")
    ccf = Calibration.from_ccf_dir()
    dmp = Calibration.from_dump()
    for n in ("left", "right", "tex"):
        assert np.allclose(ccf.cameras[n].dist, dmp.cameras[n].dist, atol=1e-15)
    # rig triangle from the two encodings of the same unit
    for k, v in ccf.rig_triangle().items():
        assert abs(v - dmp.rig_triangle()[k]) < 3.0


def test_real_rig_roundtrip_is_exact():
    _require(CCF_DIR / "LeftCCF.txt", "CCF records")
    calib = Calibration.from_ccf_dir()
    rect = bouguet_rectification(StereoPair.from_calibration(calib, "left", "right"),
                                 calib.image_size)
    rt = run_round_trip(rect=rect, n=600, seed=9, z_range=(250.0, 450.0))
    s = rt.summary()
    assert s["err3d_mm"]["max"] < 1e-8
    assert s["reprojection_right_px"]["max"] < 1e-9


# ---------------------------------------------------------------------------
# runner
# ---------------------------------------------------------------------------

def _main() -> int:
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = skipped = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS {name}")
        except unittest.SkipTest as exc:
            skipped += 1
            print(f"SKIP {name}: {exc}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {name}: {exc}")
        except Exception as exc:                       # noqa: BLE001
            failed += 1
            print(f"ERROR {name}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed - skipped}/{len(tests)} passed, "
          f"{skipped} skipped, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(_main())
