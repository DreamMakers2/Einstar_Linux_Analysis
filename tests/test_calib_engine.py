"""Regression tests for the Linux-native calibration engine."""

from __future__ import annotations

import json
import math
from pathlib import Path

import cv2
import numpy as np
import pytest

from calib import plate
from calib.ccf import decode_record, encode_record
from calib.calibrate import calibrate
from calib.detect import detect_grid_auto, detect_blobs, assign_grid
from calib.geometry import project_world, triangulate, projection_matrix
from calib.profiles import ProfileStore
from calib.synth import make_scene, perturbed_init
from calib.workflow import parse_rapid_config, exstar_sequence
from calib.adaptive import candidate_pool, candidate_jacobian, rig_vector, rig_covariance


REPO = Path(__file__).resolve().parents[1]
XML = REPO / "sources/vendor/EXStar_1.2.2.0/res/calibrateConfig/Einscan-EA/rapidCameraCalibrate.xml"
CCF_DIR = REPO / "sources/calibration/res_Einscan-E10_200x150"


def test_plate_topology():
    uv = plate.lattice_uv()
    assert uv.shape == (40, 2)
    assert uv[:, 0].max() == 7 and uv[:, 1].max() == 4
    assert np.allclose(plate.BIG_MARKERS, [[2, 1], [4, 1], [5, 1], [3.5, 3]])
    s = plate.current_spec()
    assert (s.n_lattice, s.n_small, s.n_holes) == (40, 37, 41)
    s7 = plate.PlateSpec(8, 7)
    assert (s7.n_lattice, s7.n_small, s7.n_holes) == (56, 53, 57)
    assert np.allclose(s7.big_markers, [[2, 2], [4, 2], [5, 2], [3.5, 4]])


def test_ccf_roundtrip_exact():
    if not CCF_DIR.exists():
        rng = np.random.default_rng(1)
        for ir, n in ((True, 520), (False, 264)):
            vals = rng.normal(0, 10, 33)
            trailer = rng.integers(0, 10, 64).astype("<i4").tobytes() if ir else None
            raw = encode_record(vals, ir=ir, trailer=trailer)
            rec = decode_record(raw, "synthetic")
            out = encode_record(rec.values, ir=rec.ir,
                                trailer=raw[264:] if rec.ir else None, key=rec.key)
            assert out == raw and len(raw) == n
        return
    for name in ("LeftCCF.txt", "RightCCF.txt", "TexCCF.txt"):
        raw = (CCF_DIR / name).read_bytes()
        rec = decode_record(raw, name)
        out = encode_record(rec.values, ir=rec.ir,
                            trailer=raw[264:] if rec.ir else None, key=rec.key)
        assert out == raw


def test_workflow_xml(tmp_path):
    if XML.exists():
        w = parse_rapid_config(XML)
    else:
        xml = ["<calibration><groups count='5'>"]
        for g in range(5):
            xml.append(
                f"<group{g} name='{g}' count='5' distanceMid='200' distanceMax='600'>"
                f"<positionInfo><angle valid='{'true' if g == 0 else 'false'}' "
                f"x='{0 if g == 0 else (-18 if g == 1 else 18)}' y='0' z='180' "
                f"thresX='8' thresY='8' thresZ='8'/>"
                f"<pt x='0' y='0' z='40' thresDim='40'/></positionInfo></group{g}>")
        xml.append("</groups></calibration>")
        p = tmp_path / "calib.xml"
        p.write_text("\n".join(xml))
        w = parse_rapid_config(p)
    assert len(w.groups) == 5
    assert w.n_steps == 25 and w.n_images == 75
    assert [g.count for g in w.groups] == [5] * 5
    assert w.groups[0].angle_valid and not w.groups[1].angle_valid
    seq = exstar_sequence()
    assert [s.target_distance_mm for s in seq[:5]] == [240, 320, 400, 480, 560]


def _render_plate(scene, view, size=(1280, 1024), invert=False):
    img = np.full((size[1], size[0]), 30, np.uint8)
    R, t = scene.views[view]
    Rl, tl = scene.extrinsics["left"]
    # (the hole centres are projected with the full board->camera pose below)
    # plate quad
    corners = np.array([[-110, -75, 0], [110, -75, 0], [110, 75, 0], [-110, 75, 0]], float)
    cc = project_world(scene.cameras["left"].K, scene.cameras["left"].kc,
                       Rl @ R, Rl @ t + tl, corners).astype(np.int32)
    cv2.fillConvexPoly(img, cc, 60 if not invert else 200)
    for (u, v) in plate.lattice_uv():
        p = project_world(scene.cameras["left"].K, scene.cameras["left"].kc,
                          Rl @ R, Rl @ t + tl,
                          np.array([[(u - 3.5) * scene.pitch_mm,
                                     (v - 2.0) * scene.pitch_mm, 0]]))[0]
        big = any(abs(u - bu) < 1e-6 and abs(v - bv) < 1e-6
                  for bu, bv in plate.BIG_MARKERS)
        cv2.circle(img, (int(round(p[0])), int(round(p[1]))), 8 if big else 4,
                   250 if not invert else 20, -1)
    # the fourth large marker is off-lattice at (3.5, 3)
    p = project_world(scene.cameras["left"].K, scene.cameras["left"].kc,
                      Rl @ R, Rl @ t + tl,
                      np.array([[0.0, 1.0 * scene.pitch_mm, 0]]))[0]
    cv2.circle(img, (int(round(p[0])), int(round(p[1]))), 8,
               250 if not invert else 20, -1)
    return img


def test_detector_on_synthetic_render():
    scene = make_scene(3, noise_px=0.0, outlier_frac=0.0, seed=11)
    for v in range(3):
        img = _render_plate(scene, v)
        obs = detect_grid_auto(img)
        assert obs is not None, f"view {v} not detected"
        assert obs.n_lattice >= 38


def test_geometry_projection_and_triangulation():
    K = np.array([[1100.0, 0, 640], [0, 1100, 512], [0, 0, 1]])
    R = np.eye(3); t = np.zeros(3)
    X = np.array([[10.0, 20.0, 500.0]])
    uv = project_world(K, np.zeros(5), R, t, X)
    P = projection_matrix(K, R, t)
    proj = P @ np.append(X[0], 1.0)
    assert np.allclose(proj[:2] / proj[2], uv[0], atol=1e-9)
    R2 = np.eye(3); t2 = np.array([-160.0, 0, 0])
    uv2 = project_world(K, np.zeros(5), R2, t2, X)
    Xr = triangulate(P, projection_matrix(K, R2, t2),
                     uv[0] / [1, 1], uv2[0] / [1, 1])
    assert np.allclose(Xr[0], X[0], atol=1e-6)


def test_profile_store(tmp_path):
    store = ProfileStore(tmp_path)
    from calib.ccf import load
    rng = np.random.default_rng(3)
    files = {}
    for c, ir in (("left", True), ("right", True), ("tex", False)):
        if CCF_DIR.exists():
            src = CCF_DIR / {"left": "LeftCCF.txt", "right": "RightCCF.txt",
                             "tex": "TexCCF.txt"}[c]
        else:
            src = tmp_path / f"{c}.CCF"
            raw = encode_record(rng.normal(0, 5, 33), ir=ir,
                                trailer=bytes(256) if ir else None)
            src.write_bytes(raw)
        files[c] = src
    store.snapshot_factory("SN1", files)
    recs = {c: load(p) for c, p in files.items()}
    rev = store.save_revision("SN1", recs, {"note": "test"})
    assert rev.verify()
    assert store.active("SN1").index == rev.index
    plan = store.export_plan(rev, "SN1")
    assert plan["offset"] == 0x39B and plan["size"] == 0xF21
    # factory snapshot is immutable
    with pytest.raises(FileExistsError):
        store.snapshot_factory("SN1", files)


def test_calibration_recovers_synthetic_ground_truth():
    rng = np.random.default_rng(42)
    scene = make_scene(12, noise_px=0.1, outlier_frac=0.0, seed=42)
    init = perturbed_init(scene, rng, fx_sigma=4, cx_sigma=4)
    res = calibrate(scene.observations, scene.pitch_mm, init,
                    free_kc=False, free_intrinsics=True, max_nfev=600)
    assert res.residual_rms < 0.4
    for c in ("left", "right", "tex"):
        assert abs(res.cameras[c].fx - scene.cameras[c].fx) < 1.5
    assert abs(np.linalg.norm(res.baseline()) - scene.truth["baseline_mm"]) < 0.15


def test_adaptive_jacobian_shape():
    scene = make_scene(6, noise_px=0.05, outlier_frac=0.0, seed=7)
    rng = np.random.default_rng(7)
    init = perturbed_init(scene, rng, fx_sigma=2, cx_sigma=2)
    res = calibrate(scene.observations, scene.pitch_mm, init, free_kc=False,
                    free_intrinsics=True, max_nfev=400)
    cand = candidate_pool()[0]
    J = candidate_jacobian(rig_vector(res), cand.R, cand.t, scene.pitch_mm)
    assert J is not None and J.shape[1] == 45
    assert np.isfinite(J).all()
    assert rig_covariance(res).shape == (39, 39)
