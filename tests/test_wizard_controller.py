"""Offline tests for the wizard controller (the ``CalibrationSession`` wrapper).

Every test renders a small synthetic scene and drives it through a replay
directory, so the same code path as the GUI is exercised without a scanner.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys

import numpy as np
import pytest

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tools.wizard import controller, sources, types  # noqa: E402


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _make_fixture(tmp_path: Path, *, n_views: int = 6, seed: int = 13,
                  pattern: str = "distance", **spec_kw):
    spec = sources.SyntheticSpec(n_views=n_views, seed=seed, pattern=pattern,
                                 **spec_kw)
    synthetic = sources.SyntheticSource(spec)
    groups = list(synthetic)
    frames = sources.write_replay_frames(groups, tmp_path / "frames")
    factory = tmp_path / "factory.json"
    types.atomic_write_json(factory, synthetic.profile_dict())
    return synthetic, groups, frames, factory


def _controller(tmp_path: Path, source, factory, *, mode: str = "quick",
                name: str = "session", **kwargs):
    ctl = controller.WizardController(
        factory_profile=factory, session_dir=tmp_path / name, mode=mode,
        source=source.info, ring_keep=8, **kwargs)
    ctl.begin()
    return ctl


def _ingest_all(ctl, source):
    groups, decisions = [], []
    for group in source:
        groups.append(group)
        decisions.append(ctl.ingest(group))
    return groups, decisions


# ---------------------------------------------------------------------------
# the decision path
# ---------------------------------------------------------------------------

def test_accept_materialises_and_manifest_round_trip(tmp_path):
    synthetic, groups, frames, factory = _make_fixture(tmp_path, n_views=5)
    source = sources.ReplayDirSource(frames)
    ctl = _controller(tmp_path, source, factory)
    replayed, decisions = _ingest_all(ctl, source)

    assert len(ctl.session.accepted) >= 3
    assert sum(1 for d in decisions if d.accepted) == len(ctl.session.accepted)

    # the first decision is a bootstrap seed and is recorded as accepted
    assert decisions[0].accepted
    assert "bootstrap seed" in decisions[0].reasons

    # every accepted triplet is on disk, byte-identical to the oriented input
    view = ctl.session.accepted[0]
    view_id = int(view.view_id)
    materialised_group = ctl._views[0]["group"]
    source_group = replayed[materialised_group]
    for key, cam in (("left", 1), ("right", 0)):
        path = ctl.paths.group_dir(view_id) / f"cam{cam}.pgm"
        assert path.is_file()
        assert np.array_equal(types.read_pgm(path), source_group.images[key])
    assert (ctl.paths.group_dir(view_id) / "cam2.pgm").is_file()

    manifest = json.loads(ctl.manifest_path.read_text())
    assert manifest["schema"] == "einstar-wizard-manifest/1"
    assert manifest["n_accepted"] == len(ctl.session.accepted)
    assert len(manifest["accepted"]) == len(ctl.session.accepted)
    entry = manifest["accepted"][0]
    assert entry["index"] == view_id
    assert entry["pose"]["R"] and entry["pose"]["t"]
    assert entry["files"]["left"]["sha256"]
    assert manifest["accepted_digest"]

    # decisions.jsonl has one deterministic line per ingest
    records = [json.loads(line) for line in
               ctl.paths.decisions_jsonl.read_text().splitlines()]
    assert len(records) == len(decisions)
    assert all("timestamp_s" not in json.dumps(r["status"]) for r in records)

    # session.json counters are consistent with the data on disk
    session = json.loads(ctl.paths.session_json.read_text())
    assert session["counters"]["accepted"] == len(ctl.session.accepted)
    assert session["counters"]["rejected"] == len(ctl.session.rejected)
    assert session["counters"]["decisions"] == len(records)


def test_duplicate_is_rejected_as_redundant(tmp_path):
    _, groups, frames, factory = _make_fixture(tmp_path, n_views=5)
    source = sources.ReplayDirSource(frames)
    ctl = _controller(tmp_path, source, factory)
    stream = list(source)
    for group in stream[:3]:
        ctl.ingest(group)
    assert ctl.session.result is not None, "the first solve should have happened"

    duplicate = ctl.ingest(stream[2])
    assert duplicate.accepted is False
    text = " ".join(duplicate.reasons).lower()
    assert "similar" in text or "redundant" in text
    assert controller.classify_reason(duplicate.reasons[0]) == "redundant"
    assert ctl.snapshot()["counts"]["redundant"] >= 1


def test_incomplete_group_is_rejected_with_a_specific_reason(tmp_path):
    _, groups, frames, factory = _make_fixture(tmp_path, n_views=3)
    source = sources.ReplayDirSource(frames)
    ctl = _controller(tmp_path, source, factory)
    complete = next(iter(source))
    incomplete = types.FrameGroup(
        index=77, device_group=None, timestamp_s=0.0,
        images={k: v for k, v in complete.images.items() if k != "tex"},
        complete=False, source=types.SOURCE_REPLAY, label="incomplete")
    decision = ctl.ingest(incomplete)
    assert decision.accepted is False
    assert decision.reasons and decision.reasons[0].startswith("incomplete group")
    assert "tex" in decision.reasons[0]
    assert controller.classify_reason(decision.reasons[0]) == "incomplete"
    assert not ctl.paths.group_dir(0).exists()


def test_mirrored_assignment_is_rejected(tmp_path):
    # a physically mirrored plate: same marker count, wrong chirality
    synthetic = sources.SyntheticSource(sources.SyntheticSpec(
        n_views=1, seed=11, pattern="single", distance_mm=420.0,
        marker_layout="mirror_u"))
    groups = list(synthetic)
    frames = sources.write_replay_frames(groups, tmp_path / "frames")
    factory = tmp_path / "factory.json"
    types.atomic_write_json(factory, synthetic.profile_dict())
    source = sources.ReplayDirSource(frames)
    ctl = _controller(tmp_path, source, factory, continuous=False)
    decision = ctl.ingest(next(iter(source)))
    assert decision.accepted is False
    text = " ".join(decision.reasons).lower()
    assert "mirrored" in text or "ambiguous" in text
    assert controller.classify_reason(decision.reasons[0]) == "assignment"
    assert ctl.snapshot()["assignment_ok"] is False


def test_begin_is_idempotent(tmp_path):
    _, groups, frames, factory = _make_fixture(tmp_path, n_views=3)
    source = sources.ReplayDirSource(frames)
    ctl = _controller(tmp_path, source, factory)
    first = ctl.begin()
    second = ctl.begin()
    assert second["created"] is False
    assert second["session_dir"] == first["session_dir"]
    assert second["factory_sha256"] == first["factory_sha256"]


# ---------------------------------------------------------------------------
# determinism
# ---------------------------------------------------------------------------

def test_same_twelve_group_replay_gives_identical_manifest(tmp_path):
    _, groups, frames, factory = _make_fixture(tmp_path, n_views=12, seed=21)
    runs = []
    for name in ("run-a", "run-b"):
        source = sources.ReplayDirSource(frames)
        ctl = _controller(tmp_path, source, factory, name=name,
                          continuous=False)
        _ingest_all(ctl, source)
        runs.append({
            "manifest": ctl.manifest_digest(),
            "decisions": json.dumps(ctl.decisions(), sort_keys=True),
            "counts": ctl.snapshot()["counts"],
            "views": [(v["view_id"], v["group"]) for v in ctl._views],
        })
    assert runs[0]["manifest"] == runs[1]["manifest"]
    assert runs[0]["decisions"] == runs[1]["decisions"]
    assert runs[0]["counts"] == runs[1]["counts"]
    assert runs[0]["views"] == runs[1]["views"]
    assert runs[0]["manifest"] is not None


# ---------------------------------------------------------------------------
# snapshot / finish / cancel
# ---------------------------------------------------------------------------

def test_snapshot_matches_the_gui_contract(tmp_path):
    _, groups, frames, factory = _make_fixture(tmp_path, n_views=2)
    source = sources.ReplayDirSource(frames)
    ctl = _controller(tmp_path, source, factory)
    _ingest_all(ctl, source)
    snapshot = ctl.snapshot()
    for key in ("state", "error", "source", "session", "counts",
                "last_decision", "last_pose", "detections", "markers",
                "shared_markers_ir", "assignment_ok", "quality", "capture",
                "coverage", "observability", "guidance", "readiness"):
        assert key in snapshot, key
    for key in ("accepted", "rejected", "redundant", "by_reason"):
        assert key in snapshot["counts"]
    for key in ("distance_bands", "tilt_two_axes", "roll_sectors",
                "image_regions", "stereo_overlap", "colour_participation",
                "quality", "stationarity"):
        assert key in snapshot["coverage"]["dimensions"], key
    assert snapshot["session"]["factory_sha256"]
    json.dumps(snapshot)  # must be JSON-serialisable
    # and it must not block on the session lock while ingest could run
    assert ctl.snapshot()["state"] in ("recording", "paused", "done")


def test_finish_writes_a_pending_candidate_and_report(tmp_path):
    _, groups, frames, factory = _make_fixture(tmp_path, n_views=5)
    source = sources.ReplayDirSource(frames)
    ctl = _controller(tmp_path, source, factory)
    _ingest_all(ctl, source)
    result = ctl.finish()

    assert result["candidate"]["produced"] is True
    assert ctl.candidate_path.is_file()
    document = json.loads(ctl.candidate_path.read_text())
    assert document["schema"] == "einstar.recon.profile/1"
    assert document["validation"] == "pending"
    assert document["kind"] == "candidate"
    assert set(document["cameras"]) == {"ir_left", "ir_right", "color"}
    # the profile stores absolute pixel skew
    assert abs(document["cameras"]["ir_left"]["skew"]) < 5.0

    assert ctl.validation_path.is_file()
    validation = json.loads(ctl.validation_path.read_text())
    assert validation["validation"] == "pending"
    assert "verify" in validation and "quality" in validation
    assert Path(result["report_path"]).is_file()
    assert "NOT READY" in Path(result["report_path"]).read_text()
    assert result["factory_unchanged"] is True
    assert ctl.state == "done"


def test_cancel_keeps_data_and_writes_no_profile(tmp_path):
    _, groups, frames, factory = _make_fixture(tmp_path, n_views=3)
    source = sources.ReplayDirSource(frames)
    ctl = _controller(tmp_path, source, factory)
    _ingest_all(ctl, source)
    result = ctl.cancel()
    assert result["state"] == "cancelled"
    assert not ctl.candidate_path.exists()
    assert ctl.paths.manifest_json.is_file()
    assert ctl.paths.decisions_jsonl.is_file()
    assert ctl.snapshot()["state"] == "cancelled"


# ---------------------------------------------------------------------------
# factory protection
# ---------------------------------------------------------------------------

def test_factory_profile_is_never_modified(tmp_path):
    _, groups, frames, factory = _make_fixture(tmp_path, n_views=3)
    before = factory.read_bytes()
    digest = __import__("hashlib").sha256(before).hexdigest()
    source = sources.ReplayDirSource(frames)
    ctl = _controller(tmp_path, source, factory)
    _ingest_all(ctl, source)
    ctl.finish()
    assert factory.read_bytes() == before
    assert ctl.factory_sha256 == digest
