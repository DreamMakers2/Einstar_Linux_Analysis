"""Tests for operator guidance, quality gating and acceptance."""
import numpy as np
import pytest

from calib import guidance
from calib.adaptive import ViewCandidate, board_pose
from calib.calibrate import CAMERAS, CameraIntrinsics, Observation


def _pose(d, tx=0.0, ty=0.0, lat=(0.0, 0.0)):
    R, t = board_pose(d, tx, ty, 0.0, lat)
    return R, t


def test_instruction_moves_closer_and_right():
    cur = _pose(500.0)
    tgt = _pose(300.0)
    ins = guidance.instruction(cur, tgt)
    # to make the board appear closer the scanner must move forward (+z)
    assert ins["move_mm"][2] > 0
    assert "forward" in ins["text"]
    # target board to the right -> scanner moves left (negative x)
    tgt2 = _pose(500.0, lat=(60.0, 0.0))
    ins2 = guidance.instruction(cur, tgt2)
    assert ins2["move_mm"][0] < 0
    assert "left" in ins2["text"]


def test_instruction_distance_and_effort():
    a = guidance.instruction(_pose(400.0), _pose(400.0))
    assert a["text"] == "hold still"
    b = guidance.instruction(_pose(400.0), _pose(250.0))
    assert b["effort"] > a["effort"] > 0


def test_reachable_limits_motion():
    cur = _pose(400.0)
    pool = [ViewCandidate(*(240.0, 0.0, 0.0, 0.0), *board_pose(240.0, 0, 0), "close"),
            ViewCandidate(*(420.0, 0.0, 0.0, 0.0), *board_pose(420.0, 0, 0), "near"),
            ViewCandidate(*(560.0, 0.0, 0.0, 0.0), *board_pose(560.0, 0, 0), "far")]
    r = guidance.reachable(cur, pool, max_depth_mm=100.0)
    labels = {c.label for c in r}
    assert "near" in labels and "far" not in labels


def _obs(cam, view, n=40):
    from calib import plate
    uv = {}
    for (u, v) in plate.lattice_uv()[:n]:
        uv[(float(u), float(v))] = np.array([640 + 10 * (u - 3.5), 512 + 10 * (v - 2)])
    return Observation(cam, view, uv)


def test_quality_accepts_clean_frame():
    imgs = {c: np.full((1024, 1280), 40, np.uint8) for c in CAMERAS}
    grids = {c: _obs(c, 0) for c in CAMERAS}
    # draw markers so the ROI has contrast/focus
    import cv2
    for c in CAMERAS:
        for k, uv in grids[c].uv.items():
            cv2.circle(imgs[c], (int(uv[0]), int(uv[1])), 5, 230, -1)
    q = guidance.evaluate_frame(imgs, grids, cameras={c: CameraIntrinsics(1150, 1150, 640, 512, np.zeros(5)) for c in CAMERAS})
    assert q.pose is not None
    assert q.n_markers["left"] == 40


def test_quality_rejects_low_markers_and_saturation():
    imgs = {c: np.full((1024, 1280), 40, np.uint8) for c in CAMERAS}
    grids = {c: _obs(c, 0, n=5) for c in CAMERAS}
    q = guidance.evaluate_frame(imgs, grids)
    assert not q.passed
    assert any("markers" in r for r in q.reasons)


def test_acceptance_reasons():
    class Q:
        passed = True
        reasons = []
    d = guidance.acceptance(Q(), predicted_rel_gain=0.0001, policy={"min_rel_gain": 0.01})
    assert not d["accept"] and "redundant" in d["reasons"][0]
    d2 = guidance.acceptance(Q(), predicted_rel_gain=0.05)
    assert d2["accept"]


def test_coverage_bins():
    poses = [_pose(240.0), _pose(400.0, 20.0), _pose(560.0)]
    c = guidance.coverage_bins(poses)
    assert c["occupied"] >= 2
    assert len(c["counts"]) == len(c["distance_edges"]) - 1


# ---------------------------------------------------------------------------
# motion gate: honest dt, exposure blur, dwell admission
# ---------------------------------------------------------------------------

def _clean_frame_inputs():
    """A synthetic frame set that passes every quality gate."""
    import cv2
    from calib import plate
    imgs = {c: np.full((1024, 1280), 40, np.uint8) for c in CAMERAS}
    grids = {}
    for c in CAMERAS:
        uv = {}
        for (u, v) in plate.lattice_uv():
            uv[(float(u), float(v))] = np.array(
                [640.0 + 100.0 * (u - 3.5), 512.0 + 80.0 * (v - 2.0)])
        grids[c] = Observation(c, 0, uv)
        for point in uv.values():
            cv2.circle(imgs[c], (int(point[0]), int(point[1])), 15, 230, -1)
    cams = {c: CameraIntrinsics(1150, 1150, 640, 512, np.zeros(5))
            for c in CAMERAS}
    return imgs, grids, cams


def test_motion_gate_depends_on_dt_not_absolute_displacement():
    imgs, grids, cams = _clean_frame_inputs()
    base = guidance.evaluate_frame(imgs, grids, cameras=cams, timestamp=5.0)
    assert base.passed is True and base.pose is not None
    R, t = base.pose
    previous = (R, t + np.array([30.0, 0.0, 0.0]))

    # 30 mm in 0.1 s = 300 mm/s: 1.2 ms exposure gives 0.36 mm / 0.41 px
    fast = guidance.evaluate_frame(
        imgs, grids, cameras=cams, timestamp=5.0,
        previous_pose=previous, previous_timestamp=4.9)
    assert fast.passed is False
    assert any("motion blur" in reason for reason in fast.reasons), fast.reasons
    assert fast.metrics["speed_mm_s"] == pytest.approx(300.0, rel=1e-6)

    # the same 30 mm over 5 s = 6 mm/s is ordinary hand motion, sharp exposure
    slow = guidance.evaluate_frame(
        imgs, grids, cameras=cams, timestamp=5.0,
        previous_pose=previous, previous_timestamp=0.0)
    assert slow.passed is True, slow.reasons
    assert slow.metrics["motion_blur_px"] < fast.metrics["motion_blur_px"]


def test_large_inter_decision_displacement_with_sharp_exposure_is_acceptable():
    imgs, grids, cams = _clean_frame_inputs()
    base = guidance.evaluate_frame(imgs, grids, cameras=cams, timestamp=5.0)
    R, t = base.pose
    previous = (R, t + np.array([200.0, 0.0, 0.0]))
    q = guidance.evaluate_frame(
        imgs, grids, cameras=cams, timestamp=5.0,
        previous_pose=previous, previous_timestamp=0.0)
    assert q.passed is True, q.reasons
    assert q.metrics["motion_mm"] > 100.0
    # 200 mm / 5 s = 40 mm/s; 1.2 ms exposure blurs only 0.048 mm
    assert q.metrics["motion_blur_px"] < 0.2
    assert q.metrics.get("motion_warning") is True
    assert q.metrics["stationary"] is False


def test_evaluate_frame_records_stationarity_fields():
    imgs, grids, cams = _clean_frame_inputs()
    base = guidance.evaluate_frame(imgs, grids, cameras=cams, timestamp=5.0)
    R, t = base.pose
    q = guidance.evaluate_frame(
        imgs, grids, cameras=cams, timestamp=5.0,
        previous_pose=(R, t + np.array([0.5, 0.0, 0.0])),
        previous_timestamp=4.9)
    assert q.metrics["stationarity_speed_mm_s"] == pytest.approx(5.0, rel=1e-6)
    assert q.metrics["stationarity_rate_deg_s"] is not None
    assert q.metrics["stationarity_window_s"] == pytest.approx(0.1)
    assert q.metrics["stationary"] is False      # 5 mm/s > 3 mm/s


def _stationary_quality(speed=0.5, motion_deg=0.1, dt=0.2, passed=True):
    return guidance.FrameQuality(bool(passed), [], {
        "speed_mm_s": speed, "motion_deg": motion_deg, "dt_s": dt})


def test_acceptance_admits_stationary_dwell_only_when_opted_in():
    q = _stationary_quality()
    off = guidance.acceptance(q, predicted_rel_gain=0.0)
    assert off["accept"] is False and off["dwell"] is False
    assert off["stationary"] is True
    on = guidance.acceptance(q, predicted_rel_gain=0.0, admit_dwell=True)
    assert on["accept"] is True and on["dwell"] is True
    assert "dwell view admitted" in on["reasons"][0]
    assert on["rejected_reasons"]
    # a moving duplicate is never admitted
    moving = _stationary_quality(speed=30.0, motion_deg=8.0)
    assert guidance.acceptance(moving, 0.0, admit_dwell=True)["accept"] is False
    # a quality failure is never admitted
    bad = _stationary_quality(passed=False)
    assert guidance.acceptance(bad, 0.0, admit_dwell=True)["accept"] is False
    # no dt -> no stationarity observation -> no dwell admission
    no_dt = guidance.FrameQuality(True, [], {"speed_mm_s": 0.2})
    assert guidance.quality_stationarity(no_dt) is None
    assert guidance.acceptance(no_dt, 0.0, admit_dwell=True)["accept"] is False


def test_stereo_rig_check_rejects_a_one_camera_label_slip():
    """A lattice slip in one IR camera must not reach the solver.

    Each camera's own reprojection can stay self-consistent while the two
    cameras disagree about which physical marker is which; the implied rig then
    moves by a plate pitch.  The check is inactive without a rig (unit tests,
    bootstrap) and active as soon as the factory rig is known.
    """
    from calib import guidance as _g
    imgs, grids, cams = _clean_frame_inputs()
    rig = {"right": (np.eye(3), np.zeros(3))}
    clean = _g.evaluate_frame(imgs, grids, cameras=cams, rig=rig)
    assert clean.passed is True, clean.reasons

    shifted = {}
    for (u, v), point in grids["right"].uv.items():
        shifted[(float(u) + 1.0, float(v))] = np.asarray(point, float)
    bad_grids = dict(grids)
    bad_grids["right"] = Observation("right", 0, shifted)
    bad = _g.evaluate_frame(imgs, bad_grids, cameras=cams, rig=rig)
    assert bad.passed is False, bad.metrics
    assert any("stereo pose inconsistency" in reason for reason in bad.reasons), \
        bad.reasons
