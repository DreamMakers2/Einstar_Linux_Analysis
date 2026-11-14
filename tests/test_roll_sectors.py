"""All four in-plane roll sectors through the normal acceptance path.

The real fresh-01 session reached only 44-172 deg on its accepted views, yet
the trailing real frames contain genuine 304/317 deg views that detection and
PnP resolve.  These tests pin two separate contracts:

* the controller accepts a view in each of the four roll sectors and coverage
  counts all four (the acceptance path, not just the math);
* the roll the UI reports is the roll coverage counts (one convention).
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from tools.wizard import controller, coverage, sources, types


def _run(tmp_path: Path, poses, *, name="session"):
    spec = sources.SyntheticSpec(n_views=len(poses), poses=poses, seed=11)
    synthetic = sources.SyntheticSource(spec)
    groups = list(synthetic)
    factory = tmp_path / "factory.json"
    types.atomic_write_json(factory, synthetic.profile_dict())
    ctl = controller.WizardController(
        factory_profile=factory, session_dir=tmp_path / name, mode="quick",
        source=synthetic.info, ring_keep=4)
    ctl.begin()
    for group in groups:
        ctl.ingest(group)
    return ctl


def test_four_roll_sectors_are_accepted_and_counted(tmp_path):
    # Guidance targets the *midpoint* of a missing sector (45/135/225/315).
    # An exact 90 deg multiple is the boundary between two sectors and a
    # 0.01 deg measurement error decides which; no operator is asked to hold
    # there.
    rolls = (45.0, 135.0, 225.0, 315.0)
    poses = [(400.0, 8.0, 0.0, r) for r in rolls]
    ctl = _run(tmp_path, poses)
    try:
        ctl.measure_coverage()
        measurement = ctl._measurement
        check = (measurement.get("checks") or {}).get("roll_sectors") or {}
        counts = (check.get("measured") or {}).get("bin_counts") or []
        assert check.get("satisfied") is True, counts
        assert len(counts) == 4
        assert all(int(c) >= 1 for c in counts), counts
        assert measurement["D3"]["satisfied"] is True
    finally:
        ctl.cancel()


def test_ui_roll_matches_the_coverage_roll(tmp_path):
    # An explicit synthetic pose uses adaptive.board_pose, whose measured roll
    # can differ from the nominal arguments once tilt is composed; the contract
    # under test is that whatever pose is measured, the UI reports the same
    # number coverage counts.
    poses = [(400.0, 12.0, -8.0, 315.0)]
    ctl = _run(tmp_path, poses)
    try:
        assert ctl._views, "the 270 deg view must be accepted"
        view = ctl._views[0]
        coverage_roll = coverage.roll_deg(
            np.asarray(view["pose"]["R"], float).reshape(3, 3))
        ui_roll = float(view["roll_deg"])
        assert abs(((ui_roll - coverage_roll + 180.0) % 360.0) - 180.0) < 1e-6, (
            ui_roll, coverage_roll)
        assert 270.0 <= ui_roll < 360.0
        assert int(ui_roll // 90) % 4 == 3
    finally:
        ctl.cancel()


def test_a_partially_visible_270_view_is_accepted(tmp_path):
    """Clipping must not silently collapse a high-roll view onto a lower bin."""
    spec = sources.SyntheticSpec(
        n_views=1, poses=[(400.0, 12.0, -8.0, 315.0)], seed=11,
        occlusion="left", occlusion_frac=0.30)
    synthetic = sources.SyntheticSource(spec)
    groups = list(synthetic)
    factory = tmp_path / "factory.json"
    types.atomic_write_json(factory, synthetic.profile_dict())
    ctl = controller.WizardController(
        factory_profile=factory, session_dir=tmp_path / "session",
        mode="quick", source=synthetic.info, ring_keep=4)
    ctl.begin()
    try:
        decision = ctl.ingest(groups[0])
        if decision.accepted:
            view = ctl._views[0]
            assert 270.0 <= float(view["roll_deg"]) < 360.0
            covered = {int(float(view["roll_deg"]) // 90) % 4
                       for view in ctl._views}
            assert 3 in covered
        else:
            # A 30 % occlusion can genuinely remove identity evidence; then the
            # frame must be rejected with a reason, never accepted into a wrong
            # sector.
            assert decision.reasons
    finally:
        ctl.cancel()


def test_guidance_target_pose_lands_in_the_missing_sector():
    """The pose the operator is asked to reach is the sector being counted.

    ``family_pose`` is the constructor guidance uses for its target.  If its
    round-trip were inverted, the operator could follow every instruction and
    still never fill the missing sector -- the exact failure mode reported.
    """
    spec = coverage.CoverageSpec()
    for target in (0.0, 45.0, 90.0, 135.0, 180.0, 225.0, 270.0, 315.0):
        R, t = coverage.family_pose(400.0, 0.0, 0.0, target)
        measured = coverage.roll_deg(R)
        assert abs(((measured - target + 180.0) % 360.0) - 180.0) < 1e-6, (
            target, measured)
        if target % 90.0:
            assert spec.roll_bin(measured) == int(target // 90), (
                target, measured)
    # The sector midpoints guidance actually asks for are interior points.
    for target in (45.0, 135.0, 225.0, 315.0):
        R, _ = coverage.family_pose(400.0, 0.0, 0.0, target)
        assert spec.roll_bin(coverage.roll_deg(R)) == int(target // 90)


def test_roll_sector_boundary_is_deterministic():
    """A boundary angle is counted in exactly one sector, never both.

    At 269.9/270.1 deg a 0.2 deg difference flips the bin.  That is a genuine
    ambiguity of the parameter, not a bug: coverage must credit one sector, and
    guidance then asks for the still-missing midpoint.
    """
    spec = coverage.CoverageSpec()
    below = coverage.roll_deg(coverage.family_pose(400.0, 0.0, 0.0, 269.9)[0])
    above = coverage.roll_deg(coverage.family_pose(400.0, 0.0, 0.0, 270.1)[0])
    assert spec.roll_bin(below) == 2
    assert spec.roll_bin(above) == 3
    assert spec.roll_bin(269.9) != spec.roll_bin(270.1)
