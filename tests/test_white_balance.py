"""Photometric (white-balance) layer: estimator contract, refusals, profile safety.

Every test is synthetic unless it is explicitly marked ``needs_exstar``: the
EXStar white-balance frames are private data that never enters the repository,
so the real-data check skips when the private fixture is absent.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import numpy as np
import pytest

from calib.white_balance import (
    CHANNEL_ORDER, EXSTAR_SLOT0_PHASE, LAYER, PROFILE_SCHEMA,
    PROFILE_SCHEMA_VERSION, SLOT0_PHASE, EstimatorConfig, RefusedCalibration,
    apply_gains, bayer_phase_planes, build_profile, clipped_mask, correct,
    estimate, estimate_pair, from_exstar_matrix, load_profile, mask_means,
    neutrality, plate_mask, profile_digest, revert_gains, to_exstar_matrix,
    validate_profile, window_bounds, write_profile)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def mosaic_from_levels(p00: float, g: float, p11: float, *, h=96, w=128,
                       g_jitter=0.0, seed=7) -> np.ndarray:
    """Raw mosaic with constant phase levels (rounded to 8-bit)."""
    rng = np.random.default_rng(seed)
    m = np.zeros((h, w), dtype=np.float64)
    m[0::2, 0::2] = p00
    m[1::2, 1::2] = p11
    m[0::2, 1::2] = g
    m[1::2, 0::2] = g
    if g_jitter:
        m = m + rng.normal(0.0, g_jitter, m.shape)
    return np.clip(np.rint(m), 0, 255).astype(np.uint8)


def exstar_like_pair():
    """Two frames whose phase levels mimic the real EXStar wb pair."""
    return (mosaic_from_levels(44, 65, 47), mosaic_from_levels(59, 88, 62))


def _source():
    return {"frames": {"led_on": {"path": "/tmp/on.bmp", "sha256": "0" * 64},
                       "led_off": {"path": "/tmp/off.bmp", "sha256": "1" * 64}},
            "intervention": "synthetic"}


def _profile_with(est, cfg, holdout=None, status_holdout=None):
    return build_profile({"led_on": est}, estimator=cfg, source=_source(),
                         holdout=holdout, created_utc="2026-11-14T00:00:00Z")


CFG = EstimatorConfig(window_divisor=1, subtract_dark=False,
                      dark_current=(0.0, 0.0, 0.0))


# ---------------------------------------------------------------------------
# estimator contract
# ---------------------------------------------------------------------------
def test_recovers_known_grey_world_gains():
    m = mosaic_from_levels(40, 70, 50)
    est = estimate(m, CFG)
    assert not est.refused
    assert est.gains[0] == pytest.approx(70 / 40, rel=1e-3)   # slot0 = p00
    assert est.gains[1] == 1.0
    assert est.gains[2] == pytest.approx(70 / 50, rel=1e-3)    # slot2 = p11


def test_gains_are_normalised_to_green():
    for p00, g, p11 in [(30, 60, 40), (80, 60, 70), (50, 50, 50)]:
        est = estimate(mosaic_from_levels(p00, g, p11), CFG)
        assert est.gains[1] == 1.0


def test_slot_zero_is_the_even_even_phase():
    """A dimmer (0,0) phase must ask for a *larger* slot-0 gain."""
    bright_p00 = estimate(mosaic_from_levels(80, 60, 40), CFG)
    dim_p00 = estimate(mosaic_from_levels(40, 60, 40), CFG)
    assert dim_p00.gains[0] > bright_p00.gains[0]
    # and the two slots move independently
    assert dim_p00.gains[2] == pytest.approx(bright_p00.gains[2], rel=1e-6)


def test_exstar_matrix_is_reversed_relative_to_our_buffer():
    """EXStar's slot 0 is our p11, so the stored matrix swaps the two gains."""
    gains = (1.75, 1.0, 1.55)
    m = to_exstar_matrix(gains)
    assert len(m) == 9
    assert m[0] == pytest.approx(1.55)      # slot 2 of our buffer
    assert m[4] == 1.0
    assert m[8] == pytest.approx(1.75)
    assert from_exstar_matrix(m) == pytest.approx(gains)


def test_exstar_matrix_rejects_off_diagonal_terms():
    with pytest.raises(ValueError, match="not diagonal"):
        from_exstar_matrix([1.0, 0.1, 0, 0, 1.0, 0, 0, 0, 1.0])


def test_profile_records_the_slot_convention():
    cfg = EstimatorConfig(window_divisor=1)
    prof = _profile_with(estimate(mosaic_from_levels(40, 70, 50), cfg), cfg)
    assert prof["channel_order"] == CHANNEL_ORDER == "BGR"
    assert prof["slot0_phase"] == SLOT0_PHASE == "p00"
    assert prof["exstar_slot0_phase"] == EXSTAR_SLOT0_PHASE == "p11"


# ---------------------------------------------------------------------------
# window, clipping, dark
# ---------------------------------------------------------------------------
def test_window_is_centred_and_scales_with_the_divisor():
    assert window_bounds((512, 640), 1) == (0, 0, 512, 640)
    r0, c0, rows, cols = window_bounds((512, 640), 8)
    assert (rows, cols) == (64, 80)
    assert (r0, c0) == (224, 280)
    # centred: equal margins on both sides
    assert r0 == 512 - (r0 + rows)
    assert c0 == 640 - (c0 + cols)


def test_window_outside_scene_changes_do_not_move_the_estimate():
    base = mosaic_from_levels(40, 70, 50)
    tampered = base.copy()
    tampered[:40, :60] = 255                     # bright coloured corner
    a = estimate(base, CFG)
    b = estimate(tampered, CFG)
    assert b.gains == pytest.approx(a.gains, rel=1e-9)


def test_scene_change_inside_the_window_does_move_the_estimate():
    base = mosaic_from_levels(40, 70, 50)
    tampered = base.copy()
    r0, c0, rows, cols = window_bounds((base.shape[0] // 2, base.shape[1] // 2), 1)
    tampered[2 * r0:2 * (r0 + rows):2, 2 * c0:2 * (c0 + cols):2] = 30   # darken p00
    assert estimate(tampered, CFG).gains[0] > estimate(base, CFG).gains[0]


def test_clipped_pixels_are_excluded_from_the_sums():
    clean = mosaic_from_levels(40, 70, 50)
    clipped = clean.copy()
    h, w = clean.shape
    clipped[h // 2 - 4:h // 2 + 4, w // 2 - 4:w // 2 + 4] = 255      # saturated blob
    a = estimate(clean, CFG)
    b = estimate(clipped, CFG)
    assert b.summary.n_clipped > 0
    assert b.gains == pytest.approx(a.gains, rel=1e-3)


def test_clipped_mask_flags_any_saturated_slot():
    m = mosaic_from_levels(40, 250, 50)
    planes = bayer_phase_planes(m)
    assert clipped_mask(planes, 0.95).all()
    assert not clipped_mask(bayer_phase_planes(mosaic_from_levels(40, 60, 50)),
                            0.95).any()


def test_dark_current_shifts_the_estimate_the_way_the_model_says():
    m = mosaic_from_levels(40, 70, 50)
    off = estimate(m, CFG)
    dark = (0.04, 0.04, 0.04)
    on = estimate(m, EstimatorConfig(window_divisor=1, subtract_dark=True,
                                     dark_current=dark))
    # removing a pedestal raises the ratio of a dim channel more than a bright one
    assert on.gains[0] > off.gains[0]
    assert on.gains[2] > off.gains[2]


def test_gains_solved_on_dark_subtracted_data_need_the_same_dark_to_apply():
    dark = (0.04, 0.04, 0.04)
    m = mosaic_from_levels(40, 70, 50)
    cfg = EstimatorConfig(window_divisor=1, subtract_dark=True, dark_current=dark)
    est = estimate(m, cfg)
    planes = bayer_phase_planes(m)
    bgr = planes.as_bgr()
    whole = mask_means(bgr, np.ones(planes.shape, bool))
    raw_after = mask_means(apply_gains(bgr, est.gains), np.ones(planes.shape, bool))
    chain_after = mask_means(correct(bgr, est.gains, dark), np.ones(planes.shape, bool))
    assert whole["max_cast"] > 0.1                     # strong cast before
    assert raw_after["max_cast"] > 0.005               # gains alone over-correct
    assert chain_after["max_cast"] == pytest.approx(0.0, abs=1e-9)


# ---------------------------------------------------------------------------
# refusals — the "insufficient evidence" path
# ---------------------------------------------------------------------------
def test_black_frame_is_refused():
    est = estimate(np.zeros((96, 128), np.uint8), CFG)
    assert est.refused
    assert est.as_dict()["gains_bgr"] is None
    assert any("dark/noise floor" in r for r in est.reasons)
    with pytest.raises(RefusedCalibration):
        est.require()


def test_fully_saturated_frame_is_refused():
    est = estimate(np.full((96, 128), 255, np.uint8), CFG)
    assert est.refused
    assert any("clipped" in r for r in est.reasons)


def test_implausible_gain_is_refused():
    # slot0 at 10 DN against green at 200 DN -> gain 20, far above the band
    m = mosaic_from_levels(10, 200, 200)
    est = estimate(m, CFG)
    assert est.refused
    assert any("plausibility band" in r for r in est.reasons)


def test_too_few_valid_pixels_is_refused():
    cfg = EstimatorConfig(window_divisor=1, min_samples=10 ** 6)
    est = estimate(mosaic_from_levels(40, 70, 50), cfg)
    assert est.refused
    assert any("too few valid pixels" in r for r in est.reasons)


def test_refused_estimate_still_reports_its_measurement():
    est = estimate(np.zeros((96, 128), np.uint8), CFG)
    d = est.as_dict()
    assert d["measurement"]["n_valid"] == est.summary.n_valid
    assert d["measurement"]["mean_slots_dn"] == [0.0, 0.0, 0.0]


# ---------------------------------------------------------------------------
# reversibility and metrics
# ---------------------------------------------------------------------------
def test_apply_then_revert_is_exact():
    planes = bayer_phase_planes(mosaic_from_levels(40, 70, 50))
    bgr = planes.as_bgr()
    gains = (1.75, 1.0, 1.55)
    assert np.allclose(revert_gains(apply_gains(bgr, gains), gains), bgr)


def test_apply_gains_does_not_clip():
    planes = bayer_phase_planes(mosaic_from_levels(200, 220, 210))
    out = apply_gains(planes.as_bgr(), (4.0, 1.0, 4.0))
    assert out.max() > 255.0


def test_neutral_frame_has_zero_cast_and_a_cast_is_measured():
    neutral = np.full((16, 16, 3), 100.0)
    assert neutrality(neutral)["max_cast"] == pytest.approx(0.0, abs=1e-9)
    cast = neutral.copy()
    cast[..., 0] *= 1.2
    assert neutrality(cast)["max_cast"] == pytest.approx(0.2, abs=1e-6)


def test_plate_mask_excludes_the_window_and_saturation():
    m = mosaic_from_levels(40, 70, 50, h=256, w=256)
    m[10:20, 10:20] = 255                      # saturated corner
    planes = bayer_phase_planes(m)
    win = window_bounds(planes.shape, 4)
    mask = plate_mask(planes, 90.0, exclude_window=win)
    assert not mask[win[0]:win[0] + win[2], win[1]:win[1] + win[3]].any()
    assert not clipped_mask(planes, 0.95)[mask].any()


# ---------------------------------------------------------------------------
# profile: versioning, separation, never activated
# ---------------------------------------------------------------------------
def test_profile_shape_and_layer_separation():
    cfg = EstimatorConfig(window_divisor=1)
    prof = _profile_with(estimate(mosaic_from_levels(40, 70, 50), cfg), cfg)
    assert prof["schema"] == PROFILE_SCHEMA
    assert prof["schema_version"] == PROFILE_SCHEMA_VERSION
    assert prof["layer"] == LAYER
    assert prof["geometry_profile_digest"] is None
    assert prof["activation"]["activated"] is False
    assert prof["activation"]["auto_activate"] is False
    assert prof["status"] == "estimated"
    assert prof["digest"] == profile_digest(prof)


def test_digest_is_stable_and_content_addressed():
    cfg = EstimatorConfig(window_divisor=1)
    est = estimate(mosaic_from_levels(40, 70, 50), cfg)
    a = _profile_with(est, cfg)
    b = _profile_with(est, cfg)
    assert a["digest"] == b["digest"]
    b["notes"] = ["a different note"]
    assert profile_digest(b) != a["digest"]


def test_refusal_propagates_into_the_profile_status():
    cfg = EstimatorConfig(window_divisor=1)
    prof = _profile_with(estimate(np.zeros((96, 128), np.uint8), cfg), cfg)
    assert prof["status"] == "refused"
    assert prof["conditions"]["led_on"]["gains_bgr"] is None
    assert prof["refusal_reasons"]


def test_status_only_promotes_on_a_good_holdout():
    cfg = EstimatorConfig(window_divisor=1, max_holdout_cast=0.05)
    est = estimate(mosaic_from_levels(40, 70, 50), cfg)
    good = _profile_with(est, cfg, holdout={"led_on": {"max_cast": 0.01}})
    bad = _profile_with(est, cfg, holdout={"led_on": {"max_cast": 0.20}})
    assert good["status"] == "validated"
    assert bad["status"] == "estimated"


def test_profile_round_trip(tmp_path):
    cfg = EstimatorConfig(window_divisor=1)
    prof = _profile_with(estimate(mosaic_from_levels(40, 70, 50), cfg), cfg)
    p = write_profile(prof, tmp_path / "wb.json")
    back = load_profile(p)
    assert back["digest"] == prof["digest"]
    assert back["conditions"]["led_on"]["gains_bgr"] == pytest.approx(
        prof["conditions"]["led_on"]["gains_bgr"])


def test_write_refuses_factory_paths(tmp_path):
    cfg = EstimatorConfig(window_divisor=1)
    prof = _profile_with(estimate(mosaic_from_levels(40, 70, 50), cfg), cfg)
    with pytest.raises(RefusedCalibration, match="factory path"):
        write_profile(prof, tmp_path / "res" / "Einscan-E10" / "wb.json")
    with pytest.raises(RefusedCalibration, match="factory path"):
        write_profile(prof, tmp_path / "einstar-calibration-03" / "wb.json")


def test_write_refuses_an_overwrite_by_default_and_activation(tmp_path):
    cfg = EstimatorConfig(window_divisor=1)
    prof = _profile_with(estimate(mosaic_from_levels(40, 70, 50), cfg), cfg)
    p = write_profile(prof, tmp_path / "wb.json")
    with pytest.raises(FileExistsError):
        write_profile(prof, p)
    activated = json.loads(p.read_text())
    activated["activation"]["activated"] = True
    with pytest.raises(RefusedCalibration, match="activated"):
        write_profile(activated, tmp_path / "wb2.json")


def test_load_rejects_other_layers_and_a_swap(tmp_path):
    cfg = EstimatorConfig(window_divisor=1)
    prof = _profile_with(estimate(mosaic_from_levels(40, 70, 50), cfg), cfg)
    p = tmp_path / "wb.json"
    p.write_text(json.dumps({**prof, "layer": "geometric"}))
    with pytest.raises(ValueError, match="wrong layer"):
        load_profile(p)
    p.write_text(json.dumps({**prof, "channel_order": "RGB"}))
    with pytest.raises(ValueError, match="channel_order"):
        load_profile(p)
    p.write_text(json.dumps({**prof, "schema_version": 99}))
    with pytest.raises(ValueError, match="schema_version"):
        load_profile(p)


# ---------------------------------------------------------------------------
# validation report
# ---------------------------------------------------------------------------
def _plate_with_shading(h=256, w=320, *, shading=0.20):
    """A lit plate whose slot-2/green balance is *different outside the window*.

    This is the real colour-shading failure mode in its simplest form: a gain
    fitted on the centre cannot neutralise a field whose channel balance drifts.
    """
    m = np.zeros((h, w), np.float64)
    m[0::2, 0::2] = 40
    m[0::2, 1::2] = 70
    m[1::2, 0::2] = 70
    p11 = np.full((h // 2, w // 2), 50.0)
    r0, c0, rows, cols = window_bounds((h // 2, w // 2), 2)
    p11[r0:r0 + rows, c0:c0 + cols] = 50.0                     # reference window
    outside = np.ones_like(p11, bool)
    outside[r0:r0 + rows, c0:c0 + cols] = False
    p11[outside] = 50.0 * (1.0 + shading)
    m[1::2, 1::2] = p11
    return np.clip(np.rint(m), 0, 255).astype(np.uint8)


def test_validation_window_is_neutral_by_construction():
    cfg = EstimatorConfig(window_divisor=2, subtract_dark=False)
    m = _plate_with_shading()
    est = estimate(m, cfg)
    prof = _profile_with(est, cfg)
    rep = validate_profile(prof, {"led_on": m})
    rec = rep["conditions"]["led_on"]
    assert rec["window_after"]["max_cast"] == pytest.approx(0.0, abs=1e-6)
    assert rec["window_before"]["max_cast"] > 0.3


def test_validation_reports_the_residual_shading_outside_the_window():
    cfg = EstimatorConfig(window_divisor=2, subtract_dark=False)
    flat = mosaic_from_levels(40, 70, 50, h=256, w=320)
    shaded = _plate_with_shading(shading=0.20)
    e_flat = estimate(flat, cfg)
    e_sh = estimate(shaded, cfg)
    r_flat = validate_profile(_profile_with(e_flat, cfg), {"led_on": flat})
    r_sh = validate_profile(_profile_with(e_sh, cfg), {"led_on": shaded})
    assert r_flat["conditions"]["led_on"]["outside_after"]["max_cast"] < 0.01
    assert r_sh["conditions"]["led_on"]["outside_after"]["max_cast"] > 0.10
    assert r_flat["passes"] is True


def test_validation_skips_refused_conditions():
    cfg = EstimatorConfig(window_divisor=1)
    prof = _profile_with(estimate(np.zeros((96, 128), np.uint8), cfg), cfg)
    rep = validate_profile(prof, {"led_on": mosaic_from_levels(40, 70, 50)})
    assert rep["conditions"] == {}
    assert rep["passes"] is False


# ---------------------------------------------------------------------------
# real EXStar data (private fixture, never in the repo)
# ---------------------------------------------------------------------------
FIXTURE = Path(os.path.expanduser("~/einstar-private/exstar-20261114/wb"))
needs_exstar = pytest.mark.skipif(
    not (FIXTURE / "imageTex1.bmp").is_file(),
    reason="private EXStar wb fixture not present")

#: values EXStar wrote to wbdata.txt / wbdata_noled.txt on 2026-11-14
EXSTAR_WBDATA = (1.6034658, 1.0, 1.79404747)
EXSTAR_NOLED = (1.60523856, 1.0, 1.79638302)
EXSTAR_DARK = (0.04502069, 0.0455484, 0.04504853)


@needs_exstar
def test_exstar_fixture_reproduces_the_stored_matrix():
    cv2 = pytest.importorskip("cv2")
    on = cv2.imread(str(FIXTURE / "imageTex1.bmp"), cv2.IMREAD_UNCHANGED)
    off = cv2.imread(str(FIXTURE / "imageTex2.bmp"), cv2.IMREAD_UNCHANGED)
    cfg = EstimatorConfig(window_divisor=8,
                          dark_current=EXSTAR_DARK)
    est = estimate(on, cfg)
    m = to_exstar_matrix(est.gains)
    assert not est.refused
    # both stored gains to better than 2 % on the LED-on frame
    assert m[0] == pytest.approx(EXSTAR_WBDATA[0], rel=0.02)
    assert m[8] == pytest.approx(EXSTAR_WBDATA[2], rel=0.02)
    # and the estimate must be systematically slightly *low*, not high
    assert m[0] < EXSTAR_WBDATA[0] and m[8] < EXSTAR_WBDATA[2]
    # the LED-off frame is the closer of the two, as EXStar's own numbers are
    off_est = estimate(off, cfg)
    m_off = to_exstar_matrix(off_est.gains)
    assert m_off[0] == pytest.approx(EXSTAR_NOLED[0], rel=0.02)
    assert m_off[8] == pytest.approx(EXSTAR_NOLED[2], rel=0.02)


@needs_exstar
def test_exstar_fixture_validates_and_stays_below_two_percent():
    cv2 = pytest.importorskip("cv2")
    on = cv2.imread(str(FIXTURE / "imageTex1.bmp"), cv2.IMREAD_UNCHANGED)
    off = cv2.imread(str(FIXTURE / "imageTex2.bmp"), cv2.IMREAD_UNCHANGED)
    cfg = EstimatorConfig(window_divisor=8, dark_current=EXSTAR_DARK)
    ests = estimate_pair(on, off, cfg)
    prof = build_profile(ests, estimator=cfg, source=_source())
    rep = validate_profile(prof, {"led_on": on, "led_off": off})
    for name in ("led_on", "led_off"):
        rec = rep["conditions"][name]
        assert rec["window_before"]["max_cast"] > 0.30
        assert rec["window_after"]["max_cast"] == pytest.approx(0.0, abs=1e-6)
        assert rec["outside_after"]["max_cast"] < 0.02
    assert rep["passes"] is True
