"""Tests for the missing-view completion workflow (``calib.missing_views``).

Three layers:

* pure unit tests of :class:`HoldTracker`, :class:`ExtensionStore`, ``plan``
  and the read-only base-evidence loader;
* a real-data test over ``~/einstar-calibration-03`` (read-only, skipped when
  absent) plus the committed recovered-session evidence that adds the four
  right-corner views decoded from the same untouched recording;
* a controlled synthetic extension that drives 4 real hold state machines
  through the append-only store and ``combine`` and shows the assessment going
  from 7/8 to 8/8 READY while the activation flag stays false.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pytest

from calib import missing_views as mv
from tools.wizard import coverage

REPO = Path(__file__).resolve().parents[1]
REAL_SESSION = Path.home() / "einstar-calibration-03"
RECOVERED = REPO / "analysis/calibration/recovery/session_recovered"


# ---------------------------------------------------------------------------
# synthetic session evidence helpers
# ---------------------------------------------------------------------------

def nested_quality(*, focus: float = 800.0, saturation: float = 0.001,
                   speed: float | None = 0.2, motion_deg: float | None = 0.02,
                   dt: float | None = 0.1, passed: bool = True) -> dict:
    metrics = {
        "left.active": 1.0, "left.focus": focus, "left.saturation": saturation,
        "left.contrast": 60.0,
        "right.active": 1.0, "right.focus": focus, "right.saturation": saturation,
        "right.contrast": 60.0,
        "tex.active": 1.0, "tex.focus": 150.0, "tex.saturation": saturation,
        "tex.contrast": 17.0,
        "stereo_markers": 40.0, "big_markers": 3.0, "pose_ok": 1.0,
    }
    if speed is not None:
        metrics["speed_mm_s"] = speed
        metrics["stationarity_speed_mm_s"] = speed
    if motion_deg is not None:
        metrics["motion_deg"] = motion_deg
    if dt is not None:
        metrics["dt_s"] = dt
        metrics["stationarity_window_s"] = dt
    if speed is not None and motion_deg is not None and dt:
        metrics["stationary"] = bool(speed <= 3.0 and motion_deg / dt <= 1.5)
    return {"passed": passed, "reasons": [], "metrics": metrics,
            "n_markers": {"left": 40, "right": 40, "tex": 40}}


def _view(view_id: int, distance: float, tilt_x: float, tilt_y: float,
          roll: float, centre: tuple[float, float], timestamp: float) -> dict:
    R, t = coverage.family_pose(distance, tilt_x, tilt_y, roll)
    pts = np.array([[centre[0] + 8 * (u - 3.5), centre[1] + 8 * (v - 2)]
                    for v in range(5) for u in range(8)], float)
    return {
        "view_id": int(view_id),
        "pose": {"R": np.asarray(R, float).tolist(),
                 "t": [float(x) for x in t]},
        "image_points": {"left": pts.tolist(),
                         "right": (pts - [150.0, 0.0]).tolist(),
                         "tex": pts.tolist()},
        "timestamp": float(timestamp),
        "distance_mm": float(distance),
        "n_markers": {"left": 40, "right": 40, "tex": 40},
        "quality": nested_quality(),
        "quality_passed": True,
        "shared_markers_ir": 40,
        "assignment_ok": True,
    }


def covering_views(n_base: int = 57) -> list[dict]:
    """A synthetic base that satisfies D1-D6/D8 and has no D7 stream yet."""
    views: list[dict] = []
    stamp = 0.0
    for distance in (340.0, 420.0, 510.0):
        for tx, ty in ((20.0, 20.0), (20.0, -20.0),
                       (-20.0, 20.0), (-20.0, -20.0)):
            for roll in (0.0, 90.0, 180.0, 270.0):
                views.append(_view(len(views), distance, tx, ty, roll,
                                   (640.0, 512.0), stamp))
                stamp += 0.5
    for row in range(3):
        for col in range(3):
            centre = ((col + 0.5) * coverage.IMAGE_WIDTH / 3.0,
                      (row + 0.5) * coverage.IMAGE_HEIGHT / 3.0)
            views.append(_view(len(views), 420.0, 0.0, 0.0, 0.0, centre, stamp))
            stamp += 0.5
    assert len(views) == n_base
    return views


def _stationary_rows(start: float, count: int = 5,
                     speed: float = 0.3, rate: float = 0.05) -> list[dict]:
    return [{"timestamp": start + 0.1 * k, "group": int(start * 10) + k,
             "stationary": True,
             "stationarity_speed_mm_s": speed,
             "stationarity_rate_deg_s": rate,
             "stationarity_window_s": 0.1} for k in range(count)]


def write_session(root: Path, views: list[dict], *,
                  stream: list[dict] | None = None,
                  decisions: list[dict] | None = None,
                  factory_sha256: str | None = "f" * 64,
                  spec: dict | None = None) -> Path:
    """Write a minimal but faithful session directory (like ``session.json``)."""
    root.mkdir(parents=True, exist_ok=True)
    accepted, raw_views = [], []
    for view in views:
        accepted.append({
            "view_id": int(view["view_id"]),
            "R": view["pose"]["R"], "t": view["pose"]["t"],
            "timestamp": float(view["timestamp"]),
            "predicted_gain": 0.0, "holdout_rms_px": None,
            "n_markers": dict(view["n_markers"]),
            "label": "", "quality": view["quality"],
        })
        for camera in ("left", "right", "tex"):
            rows = []
            for index, (x, y) in enumerate(view["image_points"][camera]):
                rows.append([float(index % 10), float(index // 10),
                             float(x), float(y)])
            raw_views.append({"camera": camera,
                              "view": int(view["view_id"]), "uv": rows})
    block = {
        "schema": "einstar.wizard.resume/1", "state": "test",
        "factory_sha256": factory_sha256, "pitch_mm": 28.0,
        "next_view": len(views), "views": raw_views, "accepted": accepted,
        "counters": {"decisions": len(views), "rejected": 0,
                     "n_materialised": len(views)},
    }
    if stream is not None:
        block["stationarity_observations"] = stream
    payload = {
        "schema": "einstar-wizard-session/1", "state": "test",
        "hashes": {"factory_profile": factory_sha256},
        "config": {"mode": "high", "goal": "full", "pitch_mm": 28.0},
        "spec": spec or {},
        "resume": block,
    }
    (root / "session.json").write_text(json.dumps(payload))
    if decisions is not None:
        with (root / "decisions.jsonl").open("w", encoding="utf-8") as handle:
            for row in decisions:
                handle.write(json.dumps(row) + "\n")
    return root


def make_base(tmp_path: Path, *, name: str = "base") -> tuple[Path, list[dict]]:
    views = covering_views()
    stream = _stationary_rows(0.0) + _stationary_rows(10.0)
    root = write_session(tmp_path / name, views, stream=stream)
    return root, views


# ---------------------------------------------------------------------------
# HoldTracker
# ---------------------------------------------------------------------------

def _pose(distance: float = 420.0, tx: float = 0.0, ty: float = 0.0,
          roll: float = 0.0) -> tuple[np.ndarray, np.ndarray]:
    return coverage.family_pose(distance, tx, ty, roll)


def _jitter(pose, k: int) -> tuple[np.ndarray, np.ndarray]:
    R, t = pose
    wobble = np.array([0.05 * math.sin(0.7 * k),
                       0.04 * math.cos(0.9 * k), 0.0])
    return R, t + wobble


def test_hold_tracker_movement_resets_and_never_accepts():
    tracker = mv.HoldTracker(mv.HoldConfig(hold_s=1.0))
    pose = _pose()
    # 5 still samples build progress
    for k in range(6):
        status = tracker.update(k * 0.1, *_jitter(pose, k))
    assert status.state == "HOLDING"
    assert 0.3 < status.progress < 1.0
    # an intentional 40 mm move resets the attempt
    moved = (pose[0], pose[1] + np.array([40.0, 0.0, 0.0]))
    status = tracker.update(0.6, *moved)
    assert status.state == "ALIGNING"
    assert status.progress == 0.0
    assert tracker.accepted_holds == []
    # and the attempt that ended is reported as too short
    assert status.rejected is not None
    assert status.rejected["reason"] in ("hold_too_short", "hold_unstable")


def test_hold_tracker_tolerates_micro_jitter_and_reports_duration():
    tracker = mv.HoldTracker(mv.HoldConfig(hold_s=1.0))
    pose = _pose(distance=420.0, tx=20.0, ty=-20.0, roll=90.0)
    status = None
    for k in range(11):                      # 0 .. 1.0 s at 10 Hz
        status = tracker.update(k * 0.1, *_jitter(pose, k))
    assert status.state == "ACCEPTED"
    assert status.hold is not None
    hold = status.hold
    assert hold["duration_s"] == pytest.approx(1.0, abs=1e-6)
    assert hold["n_samples"] == 11
    assert hold["mean_speed_mm_s"] is not None
    assert hold["mean_speed_mm_s"] < 3.0
    assert hold["max_speed_mm_s"] < 3.0
    # averaged pose is the held pose
    assert np.asarray(hold["pose"]["t"], float) == pytest.approx(
        np.asarray(pose[1], float), abs=0.2)
    assert len(tracker.accepted_holds) == 1


def test_hold_tracker_rejects_too_short_hold_with_a_reason():
    tracker = mv.HoldTracker(mv.HoldConfig(hold_s=1.0))
    pose = _pose()
    for k in range(6):                       # 0 .. 0.5 s, then move away
        tracker.update(k * 0.1, *_jitter(pose, k))
    moved = (pose[0], pose[1] + np.array([0.0, 30.0, 0.0]))
    status = tracker.update(0.6, *moved)
    assert status.state == "ALIGNING"
    assert status.rejected is not None
    assert status.rejected["reason"] == "hold_too_short"
    assert status.rejected["duration_s"] == pytest.approx(0.5, abs=1e-6)
    assert "0.5" in status.rejected["detail"] or "too short" in \
        status.rejected["detail"]
    assert tracker.accepted_holds == []


def test_hold_tracker_never_accepts_a_drifting_pose():
    # 2.8 mm/s stays below the D7 speed threshold and each step (0.28 mm) is
    # far below the move threshold, so only the jitter tolerance can stop it:
    # after ~0.9 s the pose has drifted 2.5 mm from the anchor.
    tracker = mv.HoldTracker(mv.HoldConfig(hold_s=1.0, jitter_mm=2.5))
    R, t = _pose()
    status = None
    rejected = []
    for k in range(40):                      # 0 .. 3.9 s
        drift = t + np.array([0.28 * k, 0.0, 0.0])
        status = tracker.update(k * 0.1, R, drift)
        if status.rejected:
            rejected.append(status.rejected)
    assert tracker.accepted_holds == []
    assert rejected and rejected[0]["reason"] == "hold_unstable"
    assert status.hold is None
    # a deliberate move ends the attempt and returns the machine to ALIGNING
    status = tracker.update(4.0, R, t + np.array([60.0, 0.0, 0.0]))
    assert status.state == "ALIGNING"
    assert status.progress == 0.0


def test_hold_tracker_after_accept_requires_a_real_move():
    tracker = mv.HoldTracker(mv.HoldConfig(hold_s=0.5))
    pose = _pose()
    for k in range(6):
        tracker.update(k * 0.1, *_jitter(pose, k))
    # continuing to hold the same pose must not accept a second hold
    for k in range(10, 20):
        status = tracker.update(k * 0.1, *_jitter(pose, k))
    assert status.state == "ALIGNING"
    assert len(tracker.accepted_holds) == 1
    # a genuine move re-arms the tracker
    moved = (pose[0], pose[1] + np.array([25.0, 0.0, 0.0]))
    tracker.update(2.1, *moved)
    for k in range(1, 7):
        status = tracker.update(2.1 + 0.1 * k, *(
            (pose[0], moved[1] + np.array([0.05 * math.sin(k), 0.0, 0.0]))))
    assert status.state == "ACCEPTED"
    assert len(tracker.accepted_holds) == 2


# ---------------------------------------------------------------------------
# ExtensionStore
# ---------------------------------------------------------------------------

def _accepted_hold(pose, *, duration: float = 2.0, samples: int = 21,
                   start: float = 1000.0) -> dict:
    tracker = mv.HoldTracker(mv.HoldConfig(hold_s=duration))
    status = None
    for k in range(samples):
        status = tracker.update(start + 0.1 * k, *(
            (pose[0], pose[1] + np.array([0.05 * math.sin(0.7 * k), 0.0, 0.0]))))
    assert status.state == "ACCEPTED", status
    hold = dict(status.hold)
    hold["quality"] = nested_quality()
    hold["n_markers"] = {"left": 40, "right": 40, "tex": 40}
    hold["shared_markers_ir"] = 40
    hold["pose"] = {"R": hold["pose"]["R"], "t": hold["pose"]["t"]}
    hold["target"] = {"distance_mm": float(np.linalg.norm(pose[1]))}
    return hold


def test_extension_store_append_load_and_manifest(tmp_path):
    base, _ = make_base(tmp_path)
    evidence = mv.load_base_evidence(base)
    store = mv.ExtensionStore.create(
        tmp_path / "ext", base_session_dir=base, base_digests=evidence,
        factory_digest=evidence.factory_sha256, pitch_mm=28.0)
    manifest = store.manifest()
    assert manifest["schema"] == mv.EXTENSION_SCHEMA
    assert manifest["base"]["session_dir"] == str(base.resolve())
    assert manifest["base"]["digests"] == evidence.digests
    assert manifest["activation"]["activated"] is False

    first = store.append_hold(_accepted_hold(_pose(420.0, 20.0, 20.0, 90.0)))
    second = store.append_hold(_accepted_hold(_pose(340.0, -20.0, 20.0, 0.0)))
    assert (first["hold_id"], second["hold_id"]) == (1, 2)
    holds = store.load_holds()
    assert [hold["hold_id"] for hold in holds] == [1, 2]
    assert holds[0]["duration_s"] == pytest.approx(2.0, abs=1e-6)
    reopened = mv.ExtensionStore.open(tmp_path / "ext")
    assert [hold["hold_id"] for hold in reopened.load_holds()] == [1, 2]


def test_extension_store_never_rewrites_existing_lines(tmp_path):
    base, _ = make_base(tmp_path)
    evidence = mv.load_base_evidence(base)
    store = mv.ExtensionStore.create(
        tmp_path / "ext", base_session_dir=base, base_digests=evidence)
    store.append_hold(_accepted_hold(_pose(420.0, 20.0, 20.0, 0.0)))
    prefix = store.holds_path.read_bytes()
    store.append_hold(_accepted_hold(_pose(340.0, -20.0, 20.0, 90.0)))
    after = store.holds_path.read_bytes()
    assert after.startswith(prefix)
    assert after.count(b"\n") == 2


def test_extension_store_refuses_a_changed_base(tmp_path):
    base, _ = make_base(tmp_path)
    evidence = mv.load_base_evidence(base)
    store = mv.ExtensionStore.create(
        tmp_path / "ext", base_session_dir=base, base_digests=evidence)
    store.append_hold(_accepted_hold(_pose(420.0, 20.0, 20.0, 0.0)))
    payload = json.loads((base / "session.json").read_text())
    payload["state"] = "tampered"
    (base / "session.json").write_text(json.dumps(payload))
    with pytest.raises(mv.BaseSessionChanged):
        store.verify_base()
    with pytest.raises(mv.BaseSessionChanged):
        store.append_hold(_accepted_hold(_pose(340.0, -20.0, 20.0, 0.0)))
    with pytest.raises(mv.BaseSessionChanged):
        mv.ExtensionStore.open(tmp_path / "ext")


def test_extension_store_tolerates_a_crash_truncated_last_line(tmp_path):
    base, _ = make_base(tmp_path)
    evidence = mv.load_base_evidence(base)
    store = mv.ExtensionStore.create(
        tmp_path / "ext", base_session_dir=base, base_digests=evidence)
    store.append_hold(_accepted_hold(_pose(420.0, 20.0, 20.0, 0.0)))
    # simulate a crash in the middle of the second append
    with store.holds_path.open("ab") as handle:
        handle.write(b'{"schema": "einstar.missing-views.hold/1", "hold_')
    holds = store.load_holds()
    assert [hold["hold_id"] for hold in holds] == [1]
    assert store.warnings and "truncated" in store.warnings[0]
    # appending after the crash still produces a loadable record
    second = store.append_hold(_accepted_hold(_pose(340.0, -20.0, 20.0, 90.0)))
    holds = store.load_holds()
    assert [hold["hold_id"] for hold in holds] == [1, second["hold_id"]]


def test_load_base_evidence_is_read_only_and_reconstructs_decisions(tmp_path):
    views = covering_views()
    decisions = _stationary_rows(0.0) + _stationary_rows(10.0)
    root = write_session(tmp_path / "base", views, stream=None,
                         decisions=decisions)
    before = {name: (root / name).stat().st_mtime_ns for name in
              ("session.json", "decisions.jsonl")}
    listing = sorted(path.name for path in root.iterdir())
    evidence = mv.load_base_evidence(root)
    after = {name: (root / name).stat().st_mtime_ns for name in
             ("session.json", "decisions.jsonl")}
    assert before == after
    assert sorted(path.name for path in root.iterdir()) == listing
    assert evidence.source == "decisions_jsonl"
    assert len(evidence.stationarity_observations) == len(decisions)
    assert mv.verify_base_digests(evidence)["ok"] is True
    assert len(evidence.views) == len(views)
    # the reconstructed views carry the image points D4 needs
    assert evidence.views[0]["image_points"]["left"]


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------

def test_plan_d7_deficit_arithmetic(tmp_path):
    base, _ = make_base(tmp_path)
    evidence = mv.load_base_evidence(base)
    assessment = mv.assess(evidence.views,
                           evidence.stationarity_observations, evidence.spec)
    assert (assessment.satisfied, assessment.total) == (7, 8)
    assert assessment.readiness["unsatisfied_dimensions"] == ["D7"]
    stationarity = assessment.measurement["checks"]["stationarity"]
    assert stationarity["measured"]["n_stationary_intervals"] == 2
    plan = mv.plan(assessment, evidence, evidence.spec)
    assert plan["n_planned"] == 4
    assert [entry["dimension"] for entry in plan["poses"]] == ["D7"] * 4
    for entry in plan["poses"]:
        assert "needs 4 more distinct dwell interval(s)" in entry["reason"]
        assert "2 recorded, 6 required" in entry["reason"]
        assert entry["hold_s"] == pytest.approx(1.0)
        assert entry["target"]["distance_range_mm"]
        assert entry["target"]["tilt_quadrant"] in ("+x+y", "+x-y",
                                                    "-x+y", "-x-y")
        assert 0 <= entry["target"]["roll_sector"] < 4
        assert len(entry["target"]["image_cell"]) == 2


def test_plan_targets_are_distinct_and_not_already_recorded(tmp_path):
    base, _ = make_base(tmp_path)
    evidence = mv.load_base_evidence(base)
    assessment = mv.assess(evidence.views,
                           evidence.stationarity_observations, evidence.spec)
    plan = mv.plan(assessment, evidence, evidence.spec)
    spec = mv._spec_object(evidence.spec)
    base_keys = {mv._signature_key(mv.pose_signature(view["pose"], spec, view))
                 for view in evidence.views}
    keys = []
    for entry in plan["poses"]:
        target = entry["target"]
        quadrant = target["tilt_quadrant"]
        keys.append((target["band_index"],
                     (1 if quadrant[0] == "+" else -1,
                      1 if quadrant[2] == "+" else -1),
                     target["roll_sector"],
                     tuple(target["image_cell"])))
        assert mv._pose_rt(entry["pose"]) is not None
    assert len(set(keys)) == len(keys)
    assert all(key not in base_keys for key in keys)
    # the plan's own target keys are what the controller/tracker accept
    for entry, key in zip(plan["poses"], keys):
        signature = mv.pose_signature(
            (np.asarray(entry["pose"]["R"], float),
             np.asarray(entry["pose"]["t"], float)), spec)
        assert (signature["band_index"], signature["quadrant"],
                signature["roll_bin"]) == key[:3]


def test_plan_cap_is_respected():
    measurement = coverage.measure([])
    plan = mv.plan(measurement, None, coverage.CoverageSpec(), cap=8)
    assert plan["n_planned"] == 8
    assert len(plan["poses"]) == 8
    assert plan["deferred"]
    assert sum(plan["deferred_counts"].values()) == len(plan["deferred"])
    assert [entry["index"] for entry in plan["poses"]] == list(range(1, 9))


# ---------------------------------------------------------------------------
# combine / assess
# ---------------------------------------------------------------------------

def test_assess_never_sets_the_activation_flag(tmp_path):
    base, _ = make_base(tmp_path)
    evidence = mv.load_base_evidence(base)
    assessment = mv.assess(evidence.views,
                           evidence.stationarity_observations, evidence.spec)
    assert assessment.activation["activated"] is False
    compact = mv.assessment_json(assessment)
    assert compact["activation"]["activated"] is False
    json.dumps(compact)                     # JSON-safe
    assert compact["satisfied"] == 7
    # the readiness helper also accepts a raw measurement dict
    verdict = mv.readiness_verdict(assessment.measurement)
    assert verdict["satisfied"] == 7
    assert verdict["activation"]["activated"] is False
    assert mv.activation_gate()["activated"] is False


def _extension_holds(tmp_path: Path, evidence, count: int) -> tuple[Path, list]:
    assessment = mv.assess(evidence.views,
                           evidence.stationarity_observations, evidence.spec)
    plan = mv.plan(assessment, evidence, evidence.spec)
    store = mv.ExtensionStore.create(
        tmp_path / "extension", base_session_dir=evidence.session_dir,
        base_digests=evidence, factory_digest=evidence.factory_sha256,
        pitch_mm=evidence.pitch_mm)
    start = max(float(row.get("timestamp") or 0.0)
                for row in evidence.stationarity_observations) + 100.0
    for entry in plan["poses"][:count]:
        pose = (np.asarray(entry["pose"]["R"], float),
                np.asarray(entry["pose"]["t"], float))
        hold = _accepted_hold(pose, duration=2.0, start=start)
        hold["target"] = entry["target"]
        store.append_hold(hold)
        start += 10.0
    return store.root, store.load_holds()


def test_four_synthetic_holds_reach_ready_without_activating(tmp_path):
    base, _ = make_base(tmp_path)
    evidence = mv.load_base_evidence(base)
    before = mv.assess(evidence.views,
                       evidence.stationarity_observations, evidence.spec)
    assert before.satisfied == 7 and before.ready is False

    extension_dir, holds = _extension_holds(tmp_path, evidence, 4)
    combined = mv.combine(evidence, holds)
    after = mv.assess(combined.views, combined.stationarity_observations,
                      evidence.spec)

    assert len(holds) == 4
    assert combined.n_dwell_views == 4
    assert after.ready is True
    assert after.satisfied == 8
    assert after.readiness["unsatisfied_dimensions"] == []
    stationarity = after.measurement["checks"]["stationarity"]
    assert stationarity["measured"]["n_stationary_intervals"] == 6
    assert stationarity["measured"]["n_dwell_views"] == 4
    # quality counts dwell views too, and they pass
    assert after.measurement["checks"]["quality"]["satisfied"] is True
    # explicit activation gate: READY is not activation
    assert after.activation["activated"] is False
    assert after.measurement["checks"]["stationarity"]["satisfied"] is True
    # the extension store itself never claims activation either
    manifest = mv.ExtensionStore.open(extension_dir).manifest()
    assert manifest["activation"]["activated"] is False


def test_two_synthetic_holds_are_not_ready(tmp_path):
    base, _ = make_base(tmp_path)
    evidence = mv.load_base_evidence(base)
    _, holds = _extension_holds(tmp_path, evidence, 2)
    combined = mv.combine(evidence, holds)
    after = mv.assess(combined.views, combined.stationarity_observations,
                      evidence.spec)
    assert after.ready is False
    assert after.satisfied == 7
    assert after.readiness["unsatisfied_dimensions"] == ["D7"]
    measured = after.measurement["checks"]["stationarity"]["measured"]
    assert measured["n_stationary_intervals"] == 4


# ---------------------------------------------------------------------------
# real data
# ---------------------------------------------------------------------------

requires_real = pytest.mark.skipif(
    not (REAL_SESSION / "session.json").is_file(),
    reason="~/einstar-calibration-03 is not present on this machine")
requires_recovered = pytest.mark.skipif(
    not (RECOVERED / "session.json").is_file(),
    reason="the committed recovered-session evidence is not present")


@requires_real
def test_real_session03_original_is_6_of_8():
    evidence = mv.load_base_evidence(REAL_SESSION)
    assert len(evidence.accepted) == 297
    assert len(evidence.views) == 297
    assert evidence.source == "decisions_jsonl"
    assert len(evidence.stationarity_observations) == 2004
    assert evidence.factory_sha256 == (
        "8259f97cc513eea6c25e23f0423be6c6067fabb2fc52bcbe3c01afa70ecda1c4")
    assert mv.verify_base_digests(evidence)["ok"] is True

    assessment = mv.assess(evidence.views,
                           evidence.stationarity_observations, evidence.spec)
    assert (assessment.satisfied, assessment.total) == (6, 8)
    assert assessment.readiness["unsatisfied_dimensions"] == ["D4", "D7"]
    corners = assessment.measurement["checks"]["image_corners"]["measured"]
    assert corners["corners"]["top_right"] == 0
    assert corners["corners"]["bottom_right"] == 0
    intervals = (assessment.measurement["checks"]["stationarity"]["measured"]
                 ["n_stationary_intervals"])
    # the decision-rate stream qualifies 4 intervals; the raw 10 Hz stream of
    # the same recording only has 2 (see the recovered test below), which is
    # why the recovered evidence is the completion base.
    assert intervals == 4


@requires_recovered
def test_real_recovered_session_is_7_of_8_with_d7_only():
    base = mv.load_base_evidence(REAL_SESSION) if (
        REAL_SESSION / "session.json").is_file() else None
    evidence = mv.load_base_evidence(RECOVERED)
    assert len(evidence.accepted) == 301
    assert evidence.source == "stored_stream"
    assert len(evidence.stationarity_observations) == 11620
    if base is not None:
        assert evidence.factory_sha256 == base.factory_sha256

    # the four right-corner views were decoded from the *same untouched* 52 GB
    # recording; including them is what makes D4 satisfied without a new
    # corner capture (docs/calibration/session03_recovery.md §2.1)
    recovered_groups = {entry.get("recovered_from_raw_group")
                        for entry in evidence.accepted}
    assert {89, 5682, 4019, 6241} <= recovered_groups

    assessment = mv.assess(evidence.views,
                           evidence.stationarity_observations, evidence.spec)
    assert (assessment.satisfied, assessment.total) == (7, 8)
    assert assessment.readiness["unsatisfied_dimensions"] == ["D7"]
    assert assessment.measurement["D4"]["satisfied"] is True
    corners = assessment.measurement["checks"]["image_corners"]["measured"]
    assert corners["corners"] == {"top_left": 9, "top_right": 2,
                                  "bottom_left": 2, "bottom_right": 2}
    stationarity = assessment.measurement["checks"]["stationarity"]
    measured = stationarity["measured"]
    assert measured["n_stationary_intervals"] == 2
    assert measured["dwell_durations_s"] == pytest.approx([4.643, 225.402],
                                                          abs=1e-3)
    assert assessment.measurement["D7"]["deficit"]["stationary_views"] == 4

    plan = mv.plan(assessment, evidence, evidence.spec)
    assert plan["n_planned"] == 4
    assert all(entry["dimension"] == "D7" for entry in plan["poses"])
    keys = set()
    for entry in plan["poses"]:
        target = entry["target"]
        keys.add((target["band_index"], target["tilt_quadrant"],
                  target["roll_sector"], tuple(target["image_cell"])))
        assert entry["hold_s"] == pytest.approx(1.0)
        assert entry["instruction"]
        assert entry["reason"]
    assert len(keys) == 4
    # 4 one-second holds is the whole remaining physical action
    assert plan["estimated_minutes"] < 3.0


if __name__ == "__main__":              # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))


# ---------------------------------------------------------------------------
# controller integration (additive extension mode)
# ---------------------------------------------------------------------------

def test_controller_extension_accepts_a_hold_and_never_touches_the_base(tmp_path):
    import hashlib

    from tools.wizard import controller, sources, types

    spec = sources.SyntheticSpec(n_views=1, seed=3)
    synthetic = sources.SyntheticSource(spec)
    factory = tmp_path / "factory.json"
    types.atomic_write_json(factory, synthetic.profile_dict())
    factory_sha = hashlib.sha256(factory.read_bytes()).hexdigest()

    base_views = covering_views()
    base = write_session(tmp_path / "base", base_views,
                         stream=_stationary_rows(1000.0) + _stationary_rows(1010.0),
                         factory_sha256=factory_sha)
    base_session_before = (base / "session.json").read_bytes()
    evidence = mv.load_base_evidence(base)
    assessment = mv.assess(evidence.views,
                           evidence.stationarity_observations, evidence.spec)
    assert assessment.readiness["unsatisfied_dimensions"] == ["D7"]
    plan = mv.plan(assessment, evidence, evidence.spec)

    ctl = controller.WizardController(factory_profile=factory,
                                      session_dir=tmp_path / "ext",
                                      mode="quick", source=None, ring_keep=4)
    result = ctl.begin_extension(base)
    assert result["ok"] is True, result.get("reason")
    assert ctl.state == "recording"
    status = ctl.extension_status()
    assert status["active"] is True
    assert status["plan"]["n_planned"] == 4
    assert status["remaining_dwell"] == 4
    assert status["n_accepted_holds"] == 0

    # feed the plan's first pose (held for ~1.2 s at 10 Hz), then a hold at a
    # pose the base session already covers: the second must be refused as
    # redundant and must not create a dwell view.
    entry = plan["poses"][0]
    target = entry["target"]
    pose6 = (target["distance_mm"], target["tilt_x_deg"],
             target["tilt_y_deg"], target["roll_deg"],
             entry["pose"]["t"][0], entry["pose"]["t"][1])
    duplicate_pose = covering_views()[0]
    duplicate6 = (duplicate_pose["distance_mm"],
                  *coverage.tilt_deg(np.asarray(duplicate_pose["pose"]["R"],
                                                float)),
                  coverage.roll_deg(np.asarray(duplicate_pose["pose"]["R"],
                                               float)),
                  duplicate_pose["pose"]["t"][0],
                  duplicate_pose["pose"]["t"][1])
    source = sources.SyntheticSource(sources.SyntheticSpec(
        n_views=2, seed=5, poses=[pose6, duplicate6], frames_per_pose=12))
    decisions = [ctl.ingest(group) for group in source]
    accepted = [decision for decision in decisions if decision.accepted]
    assert len(accepted) == 1
    assert accepted[0].view_id is not None
    assert any(not decision.accepted for decision in decisions)
    assert ctl._reason_counts.get("motion", 0) >= 1
    status = ctl.extension_status()
    assert status["n_accepted_holds"] == 1
    assert status["accepted_holds"][0]["duration_s"] == pytest.approx(
        1.0, abs=1e-6)
    assert status["remaining_dwell"] == 3
    assert status["ready"] is False
    assert status["activation"]["activated"] is False
    assert status["hold"]["state"] in ("ALIGNING", "ACCEPTED")
    assert sum(1 for view in ctl._views if view.get("dwell")) == 1
    assert (tmp_path / "ext" / "extension" / "holds.jsonl").is_file()
    assert len(ctl.extension["store"].load_holds()) == 1
    assert ctl.snapshot()["extension"]["n_accepted_holds"] == 1
    assert status["n_duplicate_holds"] >= 1
    assert not any(decision.accepted for decision in decisions[12:])
    assert status["n_accepted_holds"] == 1
    assert status["n_duplicate_holds"] >= 1
    assert len(ctl.extension["store"].load_holds()) == 1
    assert sum(1 for view in ctl._views if view.get("dwell")) == 1

    # an unsatisfied plan cannot produce an activatable candidate: the verdict
    # gate is the existing coverage_ready check
    verdict = ctl._candidate_verdict({"ok": True}, {"status": "pass"},
                                     ctl.readiness(), {})
    assert verdict["would_activate"] is False
    assert verdict["gates"]["coverage_ready"] is False
    assert any("coverage not ready" in str(reason)
               for reason in verdict["reasons"])

    # the base session is byte-identical and its digests still verify
    assert (base / "session.json").read_bytes() == base_session_before
    assert mv.verify_base_digests(evidence)["ok"] is True


def test_cli_re_assembles_an_extension_and_reports_ready(tmp_path, capsys):
    from tools.wizard import missing_views as cli

    base, _ = make_base(tmp_path)
    evidence = mv.load_base_evidence(base)
    extension_dir, _ = _extension_holds(tmp_path, evidence, 4)
    out = tmp_path / "assessment.json"

    code = cli.main(["--session-dir", str(base), "--extension",
                     str(extension_dir), "--json", str(out)])
    assert code == 0
    text = capsys.readouterr().out
    assert "8/8" in text
    assert "READY" in text
    assert "accepted holds" in text
    payload = json.loads(out.read_text())
    assert payload["schema"].endswith("/cli")
    assert payload["assessments"]["final"]["ready"] is True
    assert payload["assessments"]["final"]["activation"]["activated"] is False
    assert payload["extension"]["n_holds"] == 4
    assert payload["plan"]["n_planned"] == 0

    # the base session was not written to (the extension path is the only
    # write destination and the JSON is outside both evidence directories)
    assert sorted(path.name for path in base.iterdir()) == ["session.json"]
    assert out.is_file()


def test_cli_refuses_a_json_output_inside_the_base_session(tmp_path):
    from tools.wizard import missing_views as cli

    base, _ = make_base(tmp_path)
    code = cli.main(["--session-dir", str(base),
                     "--json", str(base / "assessment.json")])
    assert code != 0
    assert not (base / "assessment.json").exists()


def test_hold_tracker_does_not_pollute_an_accepted_hold_with_the_entry_move():
    tracker = mv.HoldTracker(mv.HoldConfig(hold_s=1.0))
    pose = _pose()
    # a fast 50 mm move into the pose (500 mm/s at 10 Hz)
    tracker.update(0.0, *pose)
    moved = (pose[0], pose[1] + np.array([50.0, 0.0, 0.0]))
    tracker.update(0.1, *moved)
    status = None
    for k in range(2, 13):                   # still from 0.2 s to 1.2 s
        status = tracker.update(0.1 * k, *(moved[0],
                                           moved[1] + np.array(
                                               [0.05 * math.sin(k), 0.0, 0.0])))
    assert status.state == "ACCEPTED"
    hold = status.hold
    assert hold["duration_s"] == pytest.approx(1.0, abs=1e-6)
    # the fast entry sample is not part of the hold window
    assert hold["max_speed_mm_s"] < 3.0
    assert hold["samples"][0]["t"] == pytest.approx(0.2, abs=1e-6)


def test_begin_after_begin_extension_does_not_reset_the_extension(tmp_path):
    """The launcher calls ``begin_extension`` and then the window's Start path
    calls ``begin()``.  ``begin`` must be a no-op for an already-recording
    extension, or the plan, the tracker and the store would be replaced by a
    fresh empty session (the operator would silently get a full capture).
    """
    import hashlib

    from tools.wizard import controller, sources, types

    spec = sources.SyntheticSpec(n_views=1, seed=3)
    synthetic = sources.SyntheticSource(spec)
    factory = tmp_path / "factory.json"
    types.atomic_write_json(factory, synthetic.profile_dict())
    factory_sha = hashlib.sha256(factory.read_bytes()).hexdigest()
    base = write_session(tmp_path / "base", covering_views(),
                         stream=_stationary_rows(1000.0) + _stationary_rows(1010.0),
                         factory_sha256=factory_sha)

    ctl = controller.WizardController(factory_profile=factory,
                                      session_dir=tmp_path / "ext",
                                      mode="quick", source=None, ring_keep=4)
    assert ctl.begin_extension(base)["ok"] is True
    tracker_before = ctl.extension["tracker"]
    plan_before = dict(ctl.extension["plan"])
    session_json_before = (tmp_path / "ext" / "session.json").read_bytes()

    result = ctl.begin()

    assert result["created"] is False
    assert ctl.state == "recording"
    assert ctl.extension is not None
    assert ctl.extension["tracker"] is tracker_before
    assert ctl.extension["plan"] == plan_before
    assert (tmp_path / "ext" / "session.json").read_bytes() == session_json_before


def _still_for_target(ctl, entry, spec):
    """A one-frame synthetic still whose *fitted* pose matches the plan target.

    The synthetic renderer ignores ``lateral_mm`` when explicit poses are
    supplied, so the image position comes from the board translation in the
    pose tuple.  Candidates are evaluated exactly as the controller evaluates
    them (``_detect`` + ``_extension_pose`` + the entry matcher), because
    validating against a hand-made pose made an earlier version of this test
    pass its own check and fail ingest.
    """
    from tools.wizard import sources

    machine = entry["target"]
    cell = tuple(int(v) for v in machine["image_cell"])   # (row, column)
    base_distance = float(machine["distance_mm"])
    dx_sign = 0.0 if cell[1] == 1 else (1.0 if cell[1] == 2 else -1.0)
    dy_sign = 0.0 if cell[0] == 1 else (1.0 if cell[0] == 2 else -1.0)
    tilts = [(machine["tilt_x_deg"], machine["tilt_y_deg"])]
    tilts += [(sx * 16.0, sy * 16.0) for sx in (1.0, -1.0)
              for sy in (1.0, -1.0)]
    sizes = [(60.0, 50.0), (95.0, 72.0), (130.0, 95.0), (160.0, 110.0)]
    for distance in (base_distance, base_distance - 40.0, base_distance + 40.0):
        scale = distance / base_distance
        for tilt_x, tilt_y in tilts:
            for size_x, size_y in sizes:
                for flip in ((1.0, 1.0), (-1.0, -1.0)):
                    dx = dx_sign * size_x * scale * flip[0]
                    dy = dy_sign * size_y * scale * flip[1]
                    pose = (distance, tilt_x, tilt_y, machine["roll_deg"],
                            dx, dy)
                    source = sources.SyntheticSource(sources.SyntheticSpec(
                        n_views=1, seed=11, poses=[pose]))
                    group = next(iter(source))
                    images = {key: np.asarray(value)
                              for key, value in group.images.items()}
                    grids, raw = ctl._detect(images)
                    fitted = ctl._extension_pose(grids)
                    if fitted is None:
                        continue
                    ctl._last_detections = ctl._detections(raw)
                    signature = mv.pose_signature(
                        fitted, spec, {"image_points": ctl._last_detections})
                    if ctl._extension_view_matches(entry, signature):
                        return group, pose
    return None, None


def controller_key(target):
    """The plan target as the controller compares it (same key arithmetic)."""
    from tools.wizard import controller as _controller
    return _controller.WizardController._extension_target_key(target)


def test_extension_fills_the_missing_image_corners_with_stills(tmp_path):
    """The operator command must be able to fill a D4 deficit.

    The real session needs image corners that a dwell view can never supply:
    dwell views are excluded from D1-D6 by contract.  The plan therefore mixes
    ``hold`` entries (D7) with ``view`` entries (D1-D6), and the extension has
    to accept a sharp still for the latter.  Before this was implemented the
    extension accepted only holds, so the plan could never be completed and the
    operator dead-ended at 7/8 -- exactly what the audit reproduced.
    """
    import hashlib

    from tools.wizard import controller, sources, types

    base_spec = sources.SyntheticSpec(n_views=1, seed=3)
    synthetic = sources.SyntheticSource(base_spec)
    factory = tmp_path / "factory.json"
    types.atomic_write_json(factory, synthetic.profile_dict())
    factory_sha = hashlib.sha256(factory.read_bytes()).hexdigest()

    # a base that satisfies everything except the two right-hand corners and D7
    kept = []
    for view in covering_views():
        centre = np.asarray(view["image_points"]["left"], float).mean(axis=0)
        col = int(centre[0] // (coverage.IMAGE_WIDTH / 3.0))
        row = int(centre[1] // (coverage.IMAGE_HEIGHT / 3.0))
        if col == 2 and row in (0, 2):
            continue
        kept.append(view)
    base = write_session(tmp_path / "base", kept,
                         stream=_stationary_rows(0.0) + _stationary_rows(10.0),
                         factory_sha256=factory_sha)
    before = (base / "session.json").read_bytes()

    ctl = controller.WizardController(factory_profile=factory,
                                      session_dir=tmp_path / "ext",
                                      mode="quick", source=None, ring_keep=4)
    assert ctl.begin_extension(base)["ok"] is True
    plan = ctl.extension_status()["plan"]["poses"]
    view_entries = [entry for entry in plan if not entry.get("hold_s")]
    assert len(view_entries) == 2, plan          # the two missing corners
    assert all(entry["dimension"] == "D4" for entry in view_entries)

    for entry in view_entries:
        group, _pose = _still_for_target(ctl, entry, ctl.coverage_spec)
        assert group is not None, f"could not render a still for {entry['target']}"
        decision = ctl.ingest(group)
        assert decision.accepted, decision.reasons
        assert ctl.extension_status()["accepted_views"][-1]["dimension"] == "D4"

    status = ctl.extension_status()
    assert status["n_accepted_views"] == 2
    assert status["n_accepted_holds"] == 0
    # D4 is now satisfied; only stationarity remains, and it still blocks.
    assert status["satisfied"] == 7, (status["satisfied"], status["missing"])
    assert status["ready"] is False
    assert status["activation"]["activated"] is False
    # the base session is evidence, not scratch space
    assert (base / "session.json").read_bytes() == before
