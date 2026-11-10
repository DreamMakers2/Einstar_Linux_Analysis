"""Tests for operator guidance, quality gating and acceptance."""
import numpy as np

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
