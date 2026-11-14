"""Stationary holds must be recognised from the live stream.

fresh-01 showed 0 dwell intervals from 2 stationary observations: the decision
path was starved by multi-second solves, and controller vetoes were evaluated
without a previous pose so they carried no motion metric at all.  These tests
pin the windowed estimator and the stream counting separately.
"""
from __future__ import annotations

import dataclasses
from pathlib import Path

import numpy as np

from tools.wizard import controller, coverage, sources, types


def _single_pose_groups(count: int = 12, dt: float = 0.1):
    spec = sources.SyntheticSpec(n_views=1, poses=[(400.0, 6.0, 0.0, 20.0)],
                                 seed=5)
    synthetic = sources.SyntheticSource(spec)
    base = next(iter(synthetic))
    groups = [dataclasses.replace(base, index=i, timestamp_s=dt * i,
                                  label=f"hold {i:03d}")
              for i in range(count)]
    return synthetic, groups


def test_window_estimator_calls_a_still_plate_stationary(tmp_path):
    synthetic, groups = _single_pose_groups(2)
    factory = tmp_path / "factory.json"
    types.atomic_write_json(factory, synthetic.profile_dict())
    ctl = controller.WizardController(
        factory_profile=factory, session_dir=tmp_path / "session",
        mode="quick", source=synthetic.info, ring_keep=2)
    ctl.begin()
    try:
        R = np.eye(3)
        t = np.array([0.0, 0.0, 400.0])
        for i in range(8):
            # 0.02 mm of PnP-like jitter, zero trend
            jitter = 0.02 * ((-1) ** i)
            ctl._track_pose(0.1 * i, (R, t + np.array([jitter, 0.0, 0.0])))
        still = ctl._window_motion(0.7)
        assert still is not None
        assert still["stationary"] is True
        assert still["stationarity_speed_mm_s"] < 3.0
        assert still["stationarity_rate_deg_s"] < 1.5
        assert still["stationarity_n"] >= 2

        # 8 mm per 0.1 s is 80 mm/s: not a hold.
        for i in range(8):
            ctl._track_pose(2.0 + 0.1 * i,
                            (R, t + np.array([0.0, 0.0, 8.0 * i])))
        moving = ctl._window_motion(2.7)
        assert moving["stationary"] is False
        assert moving["stationarity_speed_mm_s"] > 20.0
    finally:
        ctl.cancel()


def test_a_hold_supplies_a_qualified_dwell_interval(tmp_path):
    synthetic, groups = _single_pose_groups(12, dt=0.1)
    frames = sources.write_replay_frames(groups, tmp_path / "frames")
    factory = tmp_path / "factory.json"
    types.atomic_write_json(factory, synthetic.profile_dict())
    source = sources.ReplayDirSource(frames)
    assert source.continuous is True
    ctl = controller.WizardController(
        factory_profile=factory, session_dir=tmp_path / "session",
        mode="quick", source=source.info, ring_keep=2)
    ctl.begin()
    try:
        decisions = [ctl.ingest(group) for group in source]
        # The first frame may be accepted; the rest are duplicate dwell frames
        # that are deliberately not solver views.
        assert decisions[0].accepted
        assert any(not d.accepted for d in decisions[1:])
        measured = ctl._measurement or {}
        stationarity = (measured.get("checks") or {}).get("stationarity") or {}
        stats = stationarity.get("measured") or {}
        assert stats.get("stationarity_status") == "stream", stats
        assert int(stats.get("n_stationary_intervals") or 0) >= 1, stats
        assert int(stats.get("n_stationary_observations") or 0) >= 3, stats
    finally:
        ctl.cancel()
