"""Auto-exposure safety, hysteresis, metering and readback behaviour."""

from __future__ import annotations

import numpy as np

from tools.wizard.exposure import (AutoExposure, ExposureSettings, FrameStats,
                                   IR_POLICY, RGB_POLICY, RoiHold, plate_bbox)


def _settings(us=1200, gain=105, rgb_us=5000, rgb_gain=180, laser=100):
    return ExposureSettings({0: us, 1: us, 2: rgb_us},
                            {0: gain, 1: gain, 2: rgb_gain}, laser)


def _dark(mean=30.0, contrast=6.0):
    return FrameStats(mean=mean, p99=mean * 1.4, saturation=0.0,
                      contrast=contrast, p95=mean * 1.2)


def _good(mean=100.0, contrast=40.0):
    return FrameStats(mean=mean, p99=180.0, saturation=0.0,
                      contrast=contrast, p95=140.0)


def _clipped(mean=240.0, saturation=0.30):
    return FrameStats(mean=mean, p99=255.0, saturation=saturation,
                      contrast=20.0, p95=255.0)


# -- hard / soft response ------------------------------------------------

def test_hard_clipping_is_immediate_fast_and_pairs_both_ir_cameras():
    a = AutoExposure(min_interval_s=0.0)
    cur = _settings(us=2000)
    a.freeze(5.0)                       # accepted view freeze is active
    advice = a.observe(ir=_clipped(), rgb=None, current=cur, now=0.0)
    assert advice.changed and advice.settings is not None
    assert "clipping" in advice.reason
    assert advice.settings.exposure_us[0] == advice.settings.exposure_us[1]
    assert advice.settings.exposure_us[0] == int(2000 * IR_POLICY.fast_down)
    assert advice.settings.exposure_us[0] < cur.exposure_us[0]


def test_soft_saturation_needs_two_consecutive_observations():
    a = AutoExposure(min_interval_s=0.0, settle_frames=2)
    cur = _settings(us=2000)
    soft = FrameStats(mean=120.0, p99=200.0, saturation=0.08,
                      contrast=40.0, p95=180.0)
    first = a.observe(ir=soft, rgb=None, current=cur, now=0.0)
    assert not first.changed
    second = a.observe(ir=soft, rgb=None, current=cur, now=1.0)
    assert second.changed
    assert "saturation" in second.reason
    assert second.settings.exposure_us[0] == int(2000 * IR_POLICY.step_down)


def test_dark_frame_raises_exposure_then_gain_then_laser():
    a = AutoExposure(min_interval_s=0.0, settle_frames=1)
    cur = _settings(us=8000, gain=200, laser=60)
    advice = a.observe(ir=_dark(), rgb=None, current=cur, now=0.0)
    assert advice.changed
    assert advice.settings.exposure_us[0] == 8000       # already at max
    assert advice.settings.gain[0] > 200                 # gain is second choice
    # gain cap reached -> illumination rises
    a2 = AutoExposure(min_interval_s=0.0, settle_frames=1)
    cur2 = _settings(us=8000, gain=IR_POLICY.max_gain, laser=60)
    advice2 = a2.observe(ir=_dark(), rgb=None, current=cur2, now=0.0)
    assert advice2.settings.laser_percent == 70


def test_rgb_is_independent_and_at_its_own_limits():
    a = AutoExposure(min_interval_s=0.0, settle_frames=1)
    cur = _settings(us=1200, gain=105, rgb_us=5000, rgb_gain=180)
    advice = a.observe(ir=None, rgb=_dark(), current=cur, now=0.0)
    assert advice.changed
    assert advice.settings.exposure_us[2] > 5000
    assert advice.settings.exposure_us[0] == 1200        # IR untouched
    assert advice.settings.gain[0] == 105


# -- no-op suppression and limits ---------------------------------------

def test_no_op_changes_are_never_emitted_at_a_limit():
    a = AutoExposure(min_interval_s=0.0, settle_frames=1)
    cur = _settings(us=20000 - 1000, gain=IR_POLICY.max_gain,
                    rgb_us=20000, rgb_gain=300)
    for i in range(6):
        advice = a.observe(ir=None, rgb=_dark(mean=20), current=cur,
                           now=float(i))
        assert not advice.changed
        assert advice.settings is None
        assert advice.at_limits
        assert a.history == []
    assert "maximum" in advice.at_limits[0]


def test_no_op_suppression_at_ir_maximum_reports_limit_once_per_eval():
    a = AutoExposure(min_interval_s=0.0, settle_frames=1)
    cur = _settings(us=8000, gain=240, laser=100)
    advice = a.observe(ir=_dark(), rgb=None, current=cur, now=0.0)
    assert not advice.changed
    assert advice.at_limits and "maximum" in advice.at_limits[0]
    assert a.status()["changes"] == 0


def test_reason_matches_the_measured_statistic():
    a = AutoExposure(min_interval_s=0.0, settle_frames=1)
    cur = _settings(us=4000)
    bright = FrameStats(mean=190.0, p99=210.0, saturation=0.001,
                        contrast=60.0, p95=200.0)
    advice = a.observe(ir=bright, rgb=None, current=cur, now=0.0)
    assert advice.changed
    assert "bright" in advice.reason and "saturation" not in advice.reason
    assert "p50 190" in advice.reason


def test_rgb_change_has_a_rgb_reason_not_within_target():
    a = AutoExposure(min_interval_s=0.0, settle_frames=1)
    cur = _settings()
    advice = a.observe(ir=None, rgb=_dark(mean=30), current=cur, now=0.0)
    assert advice.changed
    assert advice.reason.startswith("RGB")
    assert "within target" not in advice.reason


# -- metering / ROI ------------------------------------------------------

def test_frame_stats_use_the_plate_roi_not_the_dark_background():
    image = np.full((200, 300), 20, np.uint8)
    image[60:140, 90:210] = 110
    full = FrameStats.from_image(image)
    roi = FrameStats.from_image(image, bbox=(90, 60, 210, 140))
    assert full.mean < 40 and full.metering == "frame"
    assert roi.mean == 110 and roi.metering == "roi"
    assert roi.contrast == 0


def test_frame_stats_ignore_colour_channels_and_are_bounded():
    grey = np.full((64, 64), 100, np.uint8)
    stats = FrameStats.from_image(grey)
    assert stats.mean == 100 and stats.saturation == 0.0
    colour = np.zeros((64, 64, 3), np.uint8)
    colour[..., 1] = 200
    stats = FrameStats.from_image(colour)
    assert stats.mean == 200


def test_saturated_marker_cores_are_excluded_from_plate_clipping():
    image = np.full((200, 300), 110, np.uint8)
    points = [(120, 100), (160, 100), (120, 140), (160, 140)]
    for x, y in points:
        image[y - 8:y + 8, x - 8:x + 8] = 255
    with_markers = FrameStats.from_image(image, bbox=(90, 70, 200, 170))
    excluded = FrameStats.from_image(image, bbox=(90, 70, 200, 170),
                                     exclude=points)
    assert with_markers.saturation > 0.01
    assert excluded.saturation == 0.0
    assert excluded.mean == 110


def test_roi_hold_reuses_a_recent_box_and_then_expires():
    hold = RoiHold(hold_s=2.0)
    points = [(100, 100), (200, 100), (200, 200), (100, 200)]
    box, metering, used = hold.box(points, now=0.0)
    assert box is not None and metering == "roi" and len(used) == 4
    box2, metering2, used2 = hold.box([], now=1.5)
    assert box2 == box and metering2 == "stale" and len(used2) == 4
    box3, metering3, used3 = hold.box([], now=2.5)
    assert box3 is None and metering3 == "none" and used3 == []


def test_stale_roi_does_not_chase_the_dark_background_but_still_protects():
    a = AutoExposure(min_interval_s=0.0, settle_frames=1)
    cur = _settings()
    stale_dark = FrameStats(mean=20.0, p99=40.0, saturation=0.0,
                            contrast=8.0, p95=30.0, metering="stale")
    advice = a.observe(ir=stale_dark, rgb=None, current=cur, now=0.0)
    assert not advice.changed
    assert advice.at_limits and "stale" in advice.at_limits[0]
    # the same stale box still reports gross clipping, which may not wait
    advice2 = a.observe(ir=_clipped(), rgb=None, current=cur, now=0.5)
    assert advice2.changed


def test_frame_metering_holds_on_dark_but_reacts_to_gross_clipping():
    a = AutoExposure(min_interval_s=0.0, settle_frames=1)
    cur = _settings()
    background = FrameStats(mean=15.0, p99=255.0, saturation=0.02,
                            contrast=10.0, p95=255.0, metering="frame")
    advice = a.observe(ir=background, rgb=None, current=cur, now=0.0)
    assert not advice.changed
    gross = FrameStats(mean=180.0, p99=255.0, saturation=0.35,
                       contrast=10.0, p95=255.0, metering="frame")
    advice2 = a.observe(ir=gross, rgb=None, current=cur, now=0.5)
    assert advice2.changed


def test_plate_bbox_pads_and_requires_four_points():
    assert plate_bbox([(0, 0), (10, 0), (10, 10), (0, 10)]) == (
        -3.0, -3.0, 13.0, 13.0)
    assert plate_bbox([(0, 0), (10, 0), (10, 10)]) is None
    box = plate_bbox([(100, 100), (200, 100), (200, 200), (100, 200)])
    assert box[0] < 100 and box[3] > 200


# -- pairing and readback -------------------------------------------------

def test_ir_pair_uses_the_darker_camera_as_the_step_base():
    a = AutoExposure(min_interval_s=0.0, settle_frames=1)
    cur = ExposureSettings({0: 1000, 1: 2000}, {0: 105, 1: 105, 2: 180}, 100)
    advice = a.observe(ir=_dark(), rgb=None, current=cur, now=0.0)
    assert advice.changed
    assert advice.settings.exposure_us[0] == advice.settings.exposure_us[1]
    assert advice.settings.exposure_us[0] == int(1000 * IR_POLICY.step_up)


def test_readback_lag_is_not_reported_but_a_real_mismatch_is():
    a = AutoExposure(min_interval_s=0.0, settle_frames=1,
                     readback_grace_s=2.0)
    cur = _settings(us=2000)
    advice = a.observe(ir=_dark(), rgb=None, current=cur, now=0.0)
    assert advice.changed
    requested = advice.settings
    # The child polls the control file: the old value is expected briefly.
    a.observe(ir=None, rgb=None, current=cur, now=1.0)
    assert a.status()["mismatch"] is None
    # After the grace window the device still reads the old value.
    later = a.observe(ir=None, rgb=None, current=cur, now=3.5)
    assert later.mismatch is not None
    assert a.status()["mismatch_count"] == 1
    assert a.status()["readback_ok"] is False
    # Applying it clears the mismatch and records a match.
    applied = a.observe(ir=None, rgb=None, current=requested, now=4.5)
    assert applied.mismatch is None
    assert a.status()["readback_ok"] is True


# -- freeze / stationary --------------------------------------------------

def test_accepted_view_freezes_soft_changes_but_not_clipping():
    a = AutoExposure(min_interval_s=0.0, settle_frames=1)
    cur = _settings()
    a.freeze(5.0)
    assert not a.observe(ir=_good(), rgb=None, current=cur, now=0.0).changed
    soft_dark = _dark(mean=55.0)        # outside the band, not urgent enough
    assert not a.observe(ir=soft_dark, rgb=None, current=cur, now=0.1).changed
    advice = a.observe(ir=_clipped(), rgb=None, current=cur, now=0.2)
    assert advice.changed and advice.settings.exposure_us[0] < 1200


def test_stationary_hold_suppresses_soft_correction_but_not_urgent_dark():
    a = AutoExposure(min_interval_s=0.0, settle_frames=1)
    cur = _settings()
    near_band = _dark(mean=60.0)        # below 65 but not urgent
    hold = a.observe(ir=near_band, rgb=None, current=cur, now=0.0,
                     stationary=True)
    assert not hold.changed and "stationary" in hold.reason
    urgent = _dark(mean=30.0)           # < 0.8 * target_low
    advice = a.observe(ir=urgent, rgb=None, current=cur, now=0.1,
                       stationary=True)
    assert advice.changed


def test_changes_respect_the_minimum_evaluation_interval():
    a = AutoExposure(min_interval_s=0.75, settle_frames=1)
    cur = _settings()
    first = a.observe(ir=_dark(), rgb=None, current=cur, now=0.0)
    assert first.changed
    applied = first.settings
    second = a.observe(ir=_dark(), rgb=None, current=applied, now=0.2)
    assert not second.changed and second.reason == "settling"
    third = a.observe(ir=_dark(), rgb=None, current=applied, now=0.8)
    assert third.changed


# -- closed-loop convergence ---------------------------------------------

class _Plant:
    """Linear exposure response with a 1/d^2 distance law (no clipping)."""

    def __init__(self, distance_mm=400.0, mean_at_1200=70.0):
        self.distance_mm = distance_mm
        self.mean_at_1200 = mean_at_1200

    def stats(self, us, *, target_mean=0.0):
        signal = (self.mean_at_1200 * (us / 1200.0)
                  * (400.0 / self.distance_mm) ** 2)
        mean = float(np.clip(signal, 0.0, 250.0))
        return FrameStats(mean=mean, p99=min(255.0, mean * 1.8),
                          saturation=0.0, contrast=min(120.0, mean * 0.6),
                          p95=min(245.0, mean * 1.5))


def _run_plant(plant, *, seconds=40.0, dt=0.75, policy=IR_POLICY,
               group="ir", start_us=1200, start_gain=105, start_rgb=5000,
               start_rgb_gain=180):
    a = AutoExposure(min_interval_s=0.0, settle_frames=2)
    cur = _settings(us=start_us, gain=start_gain, rgb_us=start_rgb,
                    rgb_gain=start_rgb_gain)
    changes = 0
    means: list[float] = []
    history: list[dict] = []
    t = 0.0
    while t <= seconds:
        if group == "ir":
            stats = plant.stats(cur.exposure_us[0])
            advice = a.observe(ir=stats, rgb=None, current=cur, now=t)
        else:
            stats = plant.stats(cur.exposure_us[2])
            advice = a.observe(ir=None, rgb=stats, current=cur, now=t)
        means.append(stats.mean)
        if advice.changed:
            changes += 1
            cur = advice.settings
            history.append({"t": t, "changes": dict(advice.changes),
                            "mean": stats.mean})
        t += dt
    return a, cur, changes, means, history


def test_far_distance_converges_without_oscillation_or_pinning():
    plant = _Plant(distance_mm=550.0, mean_at_1200=37.0)
    a, cur, changes, means, history = _run_plant(plant, seconds=45.0)
    assert 1 <= changes <= 6
    assert IR_POLICY.target_low <= means[-1] <= IR_POLICY.target_high
    assert cur.exposure_us[0] < IR_POLICY.max_us
    assert cur.gain[0] <= 105          # no gain ramp needed at this distance
    # No change in the last 15 s: the loop settled instead of hunting.
    assert all(h["t"] <= 30.0 for h in history)
    up = [h for h in history if "ir_exposure_us" in h["changes"]]
    assert len(up) == changes


def test_distance_sweep_recovers_with_headroom_in_both_directions():
    plant = _Plant(distance_mm=350.0)
    a = AutoExposure(min_interval_s=0.0, settle_frames=2)
    cur = _settings()
    # converge at 350 mm
    for i in range(40):
        stats = plant.stats(cur.exposure_us[2])
        advice = a.observe(ir=None, rgb=stats, current=cur, now=i * 0.75)
        if advice.changed:
            cur = advice.settings
    settled = cur.exposure_us[2]
    assert RGB_POLICY.target_low <= plant.stats(settled).mean <= 155
    # move far, then near: brightness stays in band and nothing pins at max
    changed = 0
    t = 40 * 0.75
    for distance in (450, 550, 500, 400, 320, 350):
        plant.distance_mm = float(distance)
        for _ in range(24):
            stats = plant.stats(cur.exposure_us[2])
            advice = a.observe(ir=None, rgb=stats, current=cur, now=t)
            t += 0.75
            if advice.changed:
                cur = advice.settings
                changed += 1
            assert cur.exposure_us[2] < RGB_POLICY.max_us
            assert cur.gain[2] <= 300
        assert 45.0 <= plant.stats(cur.exposure_us[2]).mean <= 170.0
    assert changed <= 20
    assert cur.exposure_us[2] < RGB_POLICY.max_us
    assert 55.0 <= plant.stats(cur.exposure_us[2]).mean <= 155.0


def test_history_and_status_expose_applied_changes():
    a = AutoExposure(min_interval_s=0.0, settle_frames=1)
    cur = _settings()
    advice = a.observe(ir=_dark(), rgb=None, current=cur, now=0.0)
    assert advice.changed
    history = a.history
    assert len(history) == 1
    assert history[0]["settings"] == advice.settings.as_capture_args()
    assert history[0]["stats"]["ir"]["mean"] == _dark().mean
    status = a.status()
    assert status["changes"] == 1
    assert status["last_reason"] == advice.reason
    assert status["requested"] == advice.settings.as_capture_args()


def test_status_reports_hardware_maxima_and_ir_pair_mismatch():
    a = AutoExposure(min_interval_s=0.0)
    cur = ExposureSettings({0: 2000, 1: 1800}, {0: 105, 1: 105, 2: 300}, 100)
    a.observe(ir=_good(), rgb=None, current=cur, now=0.0)
    status = a.status()
    assert status["hardware_max"]["rgb_gain"] is True
    assert status["hardware_max"]["ir_exposure"] is False
    assert status["ir_pair_mismatch"] is True
    assert status["readback"]["exposure_us"][0] == 2000


def test_reset_forgets_history_and_confirmations():
    a = AutoExposure(min_interval_s=0.0, settle_frames=2)
    cur = _dark()
    a.observe(ir=cur, rgb=None, current=_settings(), now=0.0)
    a.reset()
    assert a.history == [] and a.status()["changes"] == 0
    assert a.status()["mismatch_count"] == 0


def test_highlight_headroom_holds_instead_of_stepping_into_clipping():
    a = AutoExposure(min_interval_s=0.0, settle_frames=1)
    cur = _settings(us=2325)
    near_clip = FrameStats(mean=58.0, p99=255.0, saturation=0.01,
                           contrast=60.0, p95=242.0)
    advice = a.observe(ir=near_clip, rgb=None, current=cur, now=0.0)
    assert not advice.changed
    assert advice.at_limits and "headroom" in advice.at_limits[0]
    # with headroom the same dark frame may raise
    with_headroom = FrameStats(mean=58.0, p99=200.0, saturation=0.0,
                               contrast=60.0, p95=190.0)
    advice2 = a.observe(ir=with_headroom, rgb=None, current=cur, now=1.0)
    assert advice2.changed


def test_roi_hold_ages_from_the_detection_not_the_poll():
    """A cached detection list must not stay "fresh" forever.

    The GUI polls the controller snapshot at 10 Hz while detections only change
    on a decision.  Ageing the box from the poll clock made a retained list
    permanently fresh, so a stale plate box could still raise exposure from the
    dark background after detection was lost.
    """
    from tools.wizard.exposure import RoiHold

    hold = RoiHold(hold_s=2.0, fresh_s=0.5)
    points = [[100.0, 100.0], [200.0, 100.0], [200.0, 180.0], [100.0, 180.0]]
    box, metering, pts = hold.box(points, 0.0, observed_at=0.0)
    assert metering == "roi" and box is not None

    # same detection re-presented by the poll loop 1 s later: stale, not fresh
    box2, metering2, _ = hold.box(points, 1.0, observed_at=0.0)
    assert metering2 == "stale" and box2 == box

    # ...and after the hold window it is unusable, not fresh
    box3, metering3, _ = hold.box(points, 3.0, observed_at=0.0)
    assert metering3 == "none" and box3 is None
