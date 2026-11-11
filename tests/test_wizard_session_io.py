"""Session directory tests: layout, atomic manifest, materialise, resume, verify."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from tools.wizard import session_io
from tools.wizard.session_io import (append_decision, create, disk_free_bytes, load,
                                    materialise_group, resume, save_manifest,
                                    storage_guard, verify)
from tools.wizard.types import FrameGroup, read_pgm, sha256_file

CONFIG = {"serial": "TESTUNIT", "mode": "high", "pitch_mm": 28.0,
          "factory_profile": "/nonexistent/factory.json"}


def group(index: int, *, value: int = 10, missing: tuple[str, ...] = ()) -> FrameGroup:
    imgs = {c: np.full((24, 32), value + i, np.uint8)
            for i, c in enumerate(("left", "right", "tex")) if c not in missing}
    return FrameGroup(index=index, device_group=1000 + index,
                      timestamp_s=0.1 * index, images=imgs,
                      slot_ids={"left": 1, "right": 1, "tex": 1},
                      complete=not missing, source="synthetic")


def view(index: int, *, accepted: bool = True, centre=(640.0, 512.0)) -> dict:
    pts = np.array([[centre[0] + 8 * (u - 3.5), centre[1] + 8 * (v - 2)]
                    for v in range(5) for u in range(8)])
    return {"index": index, "view_id": index, "device_group": 1000 + index,
            "pose": {"R": np.eye(3), "t": [0.0, 0.0, 400.0 + index]},
            "quality": {"mean": 30.0, "saturation_frac": 0.001, "focus": 800.0},
            "image_points": {"left": pts},
            "n_markers": {"left": 40, "right": 40, "tex": 40},
            "shared_markers_ir": 40, "assignment_ok": True,
            "timestamp": 0.1 * index, "accepted": accepted}


@pytest.fixture()
def session(tmp_path: Path):
    paths = create(tmp_path / "SESSION", CONFIG)
    files = {}
    for i in range(3):
        rec = materialise_group(paths, group(i))
        files[i] = rec["files"]
        append_decision(paths, {"view_id": i, "accepted": True,
                                "reasons": [], "quality": {"focus": 800.0}})
    centres = [(180.0, 170.0), (640.0, 512.0), (1100.0, 860.0)]   # 3 image regions
    save_manifest(paths, [{**view(i, centre=centres[i]), "files": files[i]}
                          for i in range(3)])
    return paths


# ---------------------------------------------------------------------------
# round trip
# ---------------------------------------------------------------------------

def test_round_trip_layout_and_load(session):
    root = session.root
    for rel in ("session.json", "manifest.json", "decisions.jsonl", "groups",
                "candidate"):
        assert (root / rel).exists(), rel
    for i in range(3):
        for cam in (0, 1, 2):
            p = root / f"groups/{i:06d}/cam{cam}.pgm"
            assert p.is_file(), p
            assert sha256_file(p) == json.loads((root / "manifest.json").read_text())[
                "accepted"][i]["files"][{"left": "left", "right": "right",
                                         "tex": "tex"}[
                {0: "right", 1: "left", 2: "tex"}[cam]]]["sha256"]
    d = load(root)
    assert d["config"] == CONFIG
    assert d["n_accepted"] == 3 and len(d["accepted"]) == 3
    assert d["counters"]["decisions"] == 3
    assert d["counters"]["accepted"] == 3
    assert d["consistent"] is True, d["notes"]
    assert d["hashes"]["manifest"] == d["hashes"]["manifest_recorded"]
    assert set(d["hashes"]["accepted_files"]) == {0, 1, 2}
    v = verify(root)
    assert v["ok"] is True and v["checked"] == 9
    assert v["unexpected"] == [] and v["mismatches"] == []


def test_load_round_trips_enough_for_a_coverage_re_measure(session):
    """A resumed session must be able to re-measure coverage without pixels."""
    from tools.wizard import coverage
    d = load(session.root)
    m = coverage.measure(d["accepted"], coverage.CoverageSpec(require_colour=False))
    assert m["n_usable"] == 3
    assert m["D4"]["measured"]["regions_visited"] == 3
    assert m["D5"]["satisfied"] is True
    entry = d["accepted"][0]
    assert "image_points" not in entry                  # pixels are not in the manifest
    assert entry["plate_centre_px"] == pytest.approx([180.0, 170.0])
    assert entry["pose"]["t"] == pytest.approx([0.0, 0.0, 400.0])
    assert d["manifest"]["n_accepted"] == 3


def test_materialise_applies_no_extra_transform_and_detects_missing_camera(tmp_path):
    paths = create(tmp_path / "S", CONFIG)
    imgs = {c: np.arange(24 * 32, dtype=np.uint8).reshape(24, 32) + i
            for i, c in enumerate(("left", "right", "tex"))}
    g = FrameGroup(index=7, device_group=5, timestamp_s=1.0, images=imgs,
                   complete=True, source="synthetic")
    rec = materialise_group(paths, g)
    assert rec["complete"] is True and rec["missing"] == []
    for cam, meta in rec["files"].items():
        out = read_pgm(paths.root / meta["path"])
        assert np.array_equal(out, imgs[cam]), cam       # orientation already applied
    # an incomplete group reports what it has, and nothing more
    rec2 = materialise_group(paths, group(8, value=40, missing=("tex",)))
    assert rec2["complete"] is False and rec2["missing"] == ["tex"]
    assert set(rec2["files"]) == {"left", "right"}
    assert not paths.group_file(8, "tex").exists()


def test_create_refuses_to_overwrite(tmp_path):
    create(tmp_path / "S", CONFIG)
    with pytest.raises(FileExistsError):
        create(tmp_path / "S", {"serial": "OTHER"})
    paths = create(tmp_path / "S", {"serial": "OTHER"}, exist_ok=True)
    assert load(paths)["config"] == {"serial": "OTHER"}


# ---------------------------------------------------------------------------
# resume
# ---------------------------------------------------------------------------

def test_resume_continues_indexes_without_rewriting_history(session):
    before_manifest = session.manifest_json.read_bytes()
    before_decisions = session.decisions_jsonl.read_bytes()
    before_sha = (sha256_file(session.manifest_json),
                  sha256_file(session.decisions_jsonl))
    out = resume(session.root)
    assert out["next_group_index"] == 3
    assert out["next_decision_seq"] == 3
    assert out["verify"]["ok"] is True
    assert session.manifest_json.read_bytes() == before_manifest
    assert session.decisions_jsonl.read_bytes() == before_decisions
    assert (sha256_file(session.manifest_json),
            sha256_file(session.decisions_jsonl)) == before_sha
    s = load(session.root)["session"]
    assert s["state"] == "resumed"
    assert s["history"][-1]["event"] == "resume"
    # continuing really continues: index 3 does not collide with 0..2
    rec = materialise_group(session, group(3))
    assert rec["index"] == 3
    append_decision(session, {"view_id": 3, "accepted": True})
    assert load(session.root)["counters"]["decisions"] == 4


def test_resume_refuses_a_damaged_session(session):
    p = session.group_file(1, "left")
    p.write_bytes(b"P5\n2 2\n255\n\x00\x00\x00\x00")
    with pytest.raises(session_io.SessionError) as exc:
        resume(session.root)
    assert exc.value.code == "verify_failed"
    # the state is untouched by the refusal
    assert load(session.root)["session"]["state"] == "created"


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------

def test_verify_detects_mismatch_missing_and_unexpected(session):
    session.group_file(0, "left").write_bytes(b"P5\n2 2\n255\n\x00\x00\x00\x00")
    session.group_file(1, "right").unlink()
    (session.groups_dir / "999999").mkdir()
    (session.groups_dir / "999999" / "cam1.pgm").write_bytes(b"P5\n2 2\n255\n\x00" * 4)
    v = verify(session.root)
    assert v["ok"] is False
    assert [m["camera"] for m in v["mismatches"]] == ["left"]
    assert v["mismatches"][0]["index"] == 0
    assert [m["camera"] for m in v["missing"]] == ["right"]
    assert "groups/999999/cam1.pgm" in v["unexpected"]
    assert v["manifest_digest_ok"] is True          # the manifest itself is intact


def test_verify_detects_an_edited_manifest_and_bad_counters(session):
    man = json.loads(session.manifest_json.read_text())
    man["accepted"][0]["quality"]["focus"] = 999.0
    session.manifest_json.write_text(json.dumps(man))
    v = verify(session.root)
    assert v["manifest_digest_ok"] is False and v["ok"] is False
    d = load(session.root)
    assert d["consistent"] is False
    assert any("manifest.json does not match" in n for n in d["notes"])


# ---------------------------------------------------------------------------
# atomicity
# ---------------------------------------------------------------------------

def test_manifest_write_is_atomic(session, monkeypatch):
    before = session.manifest_json.read_text()
    accepted = json.loads(before)["accepted"]

    def boom(src, dst):
        raise OSError("simulated crash mid-rename")

    monkeypatch.setattr("tools.wizard.types.os.replace", boom)
    with pytest.raises(OSError):
        save_manifest(session, accepted)
    # the old manifest is still there and still valid JSON
    after = json.loads(session.manifest_json.read_text())
    assert after["accepted_digest"] == json.loads(before)["accepted_digest"]
    assert after["n_accepted"] == 3
    # the temp file is the only trace; it is never read back
    assert (session.manifest_json.with_suffix(".json.tmp")).exists()
    monkeypatch.undo()
    save_manifest(session, accepted)
    assert not (session.manifest_json.with_suffix(".json.tmp")).exists()


def test_decisions_jsonl_is_append_only(session):
    first = session.decisions_jsonl.read_text()
    append_decision(session, {"view_id": 9, "accepted": False, "reasons": ["blur"]})
    second = session.decisions_jsonl.read_text()
    assert second.startswith(first)
    lines = [json.loads(x) for x in second.splitlines()]
    assert [x["seq"] for x in lines] == [0, 1, 2, 3]
    assert lines[-1]["accepted"] is False and lines[-1]["reasons"] == ["blur"]
    assert load(session.root)["counters"] == {
        "decisions": 4, "accepted": 3, "rejected": 1, "groups": 3,
        "next_group_index": 3, "next_decision_seq": 4}


# ---------------------------------------------------------------------------
# storage
# ---------------------------------------------------------------------------

def test_storage_helpers(session):
    free = disk_free_bytes(session.root)
    assert free > 0
    assert disk_free_bytes(session.root / "does-not-exist") > 0
    ok = storage_guard(session, 0.0)
    assert ok["ok"] is True and ok["action"] == "continue"
    assert ok["free_bytes"] == free
    assert ok["groups_left_estimate"] == int(free // session_io.GROUP_BYTES)
    stop = storage_guard(session, (free + 10 ** 9) / 1e9)
    assert stop["ok"] is False and stop["action"] == "stop"
    assert "stop the session" in stop["message"]
    assert stop["used_bytes"] > 0                    # the PGMs written above
