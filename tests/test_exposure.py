"""Auto-exposure safety and hysteresis."""

from __future__ import annotations

import numpy as np

from tools.wizard.exposure import (AutoExposure, ExposureSettings, FrameStats,
                                   IR_POLICY, RGB_POLICY)


def _settings(us=1200, gain=105, rgb_us=5000, rgb_gain=180, laser=100):
    return ExposureSettings({0: us, 1: us, 2: rgb_us},
                            {0: gain, 1: gain, 2: rgb_gain}, laser)


def test_saturation_reduces_ir_exposure_after_confirmation():
    a = AutoExposure(min_interval_s=0.0)
    cur = _settings()
    bright = FrameStats(mean=220, p99=255, saturation=0.2, contrast=20)
    first = a.observe(ir=bright, rgb=None, current=cur, now=0.0)
    assert not first.changed
    second = a.observe(ir=bright, rgb=None, current=cur, now=1.0)
    assert second.changed and second.settings is not None
    assert second.settings.exposure_us[0] == second.settings.exposure_us[1]
    assert second.settings.exposure_us[0] < cur.exposure_us[0]


def test_dark_frame_raises_exposure_then_gain_then_laser():
    a = AutoExposure(min_interval_s=0.0, settle_frames=1)
    cur = _settings(us=8000, gain=200, laser=60)
    dark = FrameStats(mean=20, p99=40, saturation=0.0, contrast=5)
    advice = a.observe(ir=dark, rgb=None, current=cur, now=0.0)
    assert advice.changed
    assert advice.settings.exposure_us[0] == 8000       # already at max
    assert advice.settings.gain[0] > 200                 # gain is second choice
    # gain cap reached -> illumination rises
    a2 = AutoExposure(min_interval_s=0.0, settle_frames=1)
    cur2 = _settings(us=8000, gain=IR_POLICY.max_gain, laser=60)
    advice2 = a2.observe(ir=dark, rgb=None, current=cur2, now=0.0)
    assert advice2.settings.laser_percent == 70


def test_rgb_is_independent_and_at_its_own_limits():
    a = AutoExposure(min_interval_s=0.0, settle_frames=1)
    cur = _settings(us=1200, gain=105, rgb_us=5000, rgb_gain=180)
    dark_rgb = FrameStats(mean=20, p99=40, saturation=0.0, contrast=5)
    advice = a.observe(ir=None, rgb=dark_rgb, current=cur, now=0.0)
    assert advice.changed
    assert advice.settings.exposure_us[2] > 5000
    assert advice.settings.exposure_us[0] == 1200        # IR untouched
    assert advice.settings.gain[0] == 105


def test_accepted_view_freezes_settings_but_saturation_overrides():
    a = AutoExposure(min_interval_s=0.0, settle_frames=1)
    cur = _settings()
    a.freeze(5.0)
    normal = FrameStats(mean=100, p99=150, saturation=0.0, contrast=30)
    assert not a.observe(ir=normal, rgb=None, current=cur, now=0.0).changed
    saturated = FrameStats(mean=240, p99=255, saturation=0.2, contrast=10)
    advice = a.observe(ir=saturated, rgb=None, current=cur, now=0.1)
    assert advice.changed and advice.settings.exposure_us[0] < 1200


def test_frame_stats_use_the_plate_roi_not_the_dark_background():
    image = np.full((200, 300), 20, np.uint8)
    image[60:140, 90:210] = 110
    full = FrameStats.from_image(image)
    roi = FrameStats.from_image(image, bbox=(90, 60, 210, 140))
    assert full.mean < 40
    assert roi.mean == 110
    assert roi.contrast == 0


def test_frame_stats_ignore_colour_channels_and_are_bounded():
    grey = np.full((64, 64), 100, np.uint8)
    stats = FrameStats.from_image(grey)
    assert stats.mean == 100 and stats.saturation == 0.0
    colour = np.zeros((64, 64, 3), np.uint8)
    colour[..., 1] = 200
    stats = FrameStats.from_image(colour)
    assert stats.mean == 200
