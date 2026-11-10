"""Regression tests for the recovered Einstar calibration-target geometry.

The specification is *recovered from EXStar 1.2.2.0* (see
``docs/calibration/plate_spec_recovered.md``): 8 x 5 markers on a 17 x 14
lattice at 14 mm, i.e. a 28.0 mm marker pitch, plate 8 x 6 pitches
(224 x 168 mm), four large markers at (2,1), (4,1), (5,1), (3.5,3).
"""

from __future__ import annotations

import numpy as np

from calib import plate


def test_topology_is_8x5_with_41_markers():
    s = plate.PlateSpec()
    assert (s.cols, s.rows) == (8, 5)
    assert s.n_lattice == 40
    assert s.n_large_on_lattice == 3
    assert s.n_small == 37
    assert s.n_holes == 41


def test_large_markers_match_the_vendor_corpus():
    s = plate.PlateSpec()
    assert np.allclose(s.big_markers, [[2, 1], [4, 1], [5, 1], [3.5, 3]])


def test_vendor_pitch_and_plate_size():
    s = plate.PlateSpec()
    assert s.pitch_mm == 28.0
    assert s.pitch_source == "vendor_model"
    assert s.lattice_pitch_mm == 14.0
    assert s.plate_size_mm == (224.0, 168.0)
    # 8 x 5 markers at 28 mm span 7 x 4 pitches
    pts = s.vendor_points_mm()
    assert np.isclose(np.ptp(pts[:, 0]), 7 * 28.0)
    assert np.isclose(np.ptp(pts[:, 1]), 4 * 28.0)


def test_vendor_frame_large_marker_coordinates():
    s = plate.PlateSpec()
    big = {tuple(uv): tuple(xy) for uv, xy in zip(s.big_markers, s.vendor_big_mm()[:, :2])}
    assert np.allclose(big[(2, 1)], (-42, 28))
    assert np.allclose(big[(4, 1)], (14, 28))
    assert np.allclose(big[(5, 1)], (42, 28))
    assert np.allclose(big[(3.5, 3)], (0, -28))


def test_recognition_lattice_indices_match_the_binary():
    s = plate.PlateSpec()
    idx = [s.recognition_index(u, v) for u, v in s.big_markers]
    assert tuple(idx) == (73, 77, 79, 144)
    # The vendor reads those flat indices from a row-major 17x14 array.
    for i in (71, 73, 77, 79, 139, 144, 149):
        assert 0 <= i < 17 * 14


def test_distance_refs_reproduce_ple_aa_bb():
    refs = plate.current_spec().distance_refs_mm()
    assert refs["AA"]["indices"] == (71, 149)
    assert refs["BB"]["indices"] == (79, 139)
    # Nominal model distances are 14*sqrt(116) and 14*sqrt(80) mm.
    assert np.isclose(refs["AA"]["model_distance_mm"], 14 * np.sqrt(116))
    assert np.isclose(refs["BB"]["model_distance_mm"], 14 * np.sqrt(80))
    # This unit's per-board PLE values agree to <0.2 %.
    for key in ("AA", "BB"):
        d = refs[key]["model_distance_mm"]
        assert abs(d - refs[key]["unit_ple_value_mm"]) / d < 0.002
        # ... and both imply the same 28.0 mm marker pitch.
        assert abs(refs[key]["implied_pitch_mm"] - 28.0) < 0.05


def test_engine_frame_is_the_vendor_frame_with_y_flipped():
    s = plate.PlateSpec()
    assert np.allclose(s.points_mm[:, :2], s.vendor_points_mm()[:, :2] * [1.0, -1.0])


def test_8x7_manual_variant_is_still_representable_but_not_default():
    s = plate.PlateSpec(cols=8, rows=7, pitch_mm=28.0)
    assert s.n_holes == 57
    assert s.n_small == 53
    assert np.allclose(s.big_markers, [[2, 2], [4, 2], [5, 2], [3.5, 4]])
    assert plate.PlateSpec().n_holes == 41


def test_rejected_25mm_hypothesis_is_not_the_default():
    assert plate.DEFAULT_PITCH_MM == 28.0
    assert plate.PITCH_SOURCES_MM["vendor_model"] == 28.0
    assert plate.PITCH_SOURCES_MM["profile_name_hypothesis_rejected"] == 25.0
    assert not np.isclose(plate.DEFAULT_PITCH_MM, 25.0)


def test_use_spec_switches_the_active_geometry_and_restores_it():
    original = plate.current_spec()
    try:
        alt = plate.use_spec(plate.PlateSpec(cols=8, rows=7, pitch_mm=28.0))
        assert alt.n_holes == 57
        assert len(plate.lattice_uv()) == 56
        assert len(plate.board_points_mm()) == 56
    finally:
        plate.use_spec(original)
    assert len(plate.lattice_uv()) == 40
