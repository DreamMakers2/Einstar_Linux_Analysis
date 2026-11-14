"""Production capture data handling: bounded RAM, compact views, crash safety.

Every test here is offline.  The capture integration tests use the *real*
``einstar-capture-frames`` binary with the simulated backend (they skip when it
is not built); no scanner, USB device or private recording is touched.

The measured numbers these tests pin are documented in
``docs/calibration/production_capture.md``.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import time

import numpy as np
import pytest

from tools.wizard import capture, session_io
from tools.wizard.ring_buffer import (DEFAULT_CADENCE_HZ, ENTRY_OVERHEAD_BYTES,
                                      GROUP_BYTES, BoundedRingBuffer,
                                      capacity_for_seconds)
from tools.wizard.types import FrameGroup

REAL_BINARY = (Path(__file__).resolve().parents[1] / "build" / "driver"
               / "einstar-capture-frames")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _image(height: int = 48, width: int = 64, *, seed: int = 0,
           noise: int = 3) -> np.ndarray:
    """A plate-like frame: smooth shading plus a little sensor noise."""
    yy, xx = np.mgrid[0:height, 0:width]
    base = (40 + 90 * yy / max(height - 1, 1) + 30 * xx / max(width - 1, 1))
    rng = np.random.default_rng(seed)
    return np.clip(base + rng.integers(-noise, noise + 1, base.shape),
                   0, 255).astype(np.uint8)


def _images(index: int = 0, *, seed: int = 0,
            shape: tuple[int, int] = (48, 64)) -> dict[str, np.ndarray]:
    return {cam: _image(*shape, seed=seed + i + index)
            for i, cam in enumerate(("left", "right", "tex"))}


def _group(index: int, *, seed: int = 0, shape=(48, 64)) -> FrameGroup:
    return FrameGroup(index=index, device_group=1000 + index,
                      timestamp_s=0.102 * index,
                      images=_images(index, seed=seed, shape=shape),
                      slot_ids={"left": 1, "right": 1, "tex": 1},
                      complete=True, source="synthetic")


def _observation(index: int, *, centre=(320.0, 240.0),
                 distance=420.0, roll=0.0) -> dict:
    pts = [[float(centre[0] + 6 * (u - 3.5)), float(centre[1] + 6 * (v - 2))]
           for v in range(5) for u in range(8)]
    angle = np.deg2rad(roll)
    R = np.array([[np.cos(angle), -np.sin(angle), 0.0],
                  [np.sin(angle), np.cos(angle), 0.0],
                  [0.0, 0.0, 1.0]])
    return {"index": index, "view_id": index, "device_group": 1000 + index,
            "pose": {"R": R.tolist(), "t": [0.0, 0.0, float(distance)]},
            "quality": {"passed": True, "focus": 820.0, "saturation_frac": 0.001,
                        "metrics": {"left.focus": 820.0}},
            "image_points": {"left": pts, "right": pts, "tex": pts},
            "n_markers": {"left": 40, "right": 40, "tex": 40},
            "shared_markers_ir": 40, "assignment_ok": True,
            "timestamp": 0.102 * index, "accepted": True}


def _store(tmp_path: Path, *, mode="production", **kwargs):
    paths = session_io.create(tmp_path / "SESSION", {"mode": "high"},
                              storage=mode, storage_options=kwargs)
    return paths, session_io.open_store(paths)


def _capture_binary() -> Path:
    if not (REAL_BINARY.is_file() and os.access(REAL_BINARY, os.X_OK)):
        pytest.skip("capture executable not built")
    return REAL_BINARY


# ---------------------------------------------------------------------------
# 1. bounded RAM ring buffer under adversarial sizes
# ---------------------------------------------------------------------------

def test_ring_buffer_never_exceeds_its_byte_budget():
    ring = BoundedRingBuffer(1024 * 1024)
    frame = np.zeros((200, 200), np.uint8)          # 40,000 B
    for i in range(60):
        ring.push({"left": frame, "right": frame, "tex": frame}, key=i,
                  timestamp_s=0.1 * i)
        assert ring.nbytes <= ring.capacity_bytes
        assert ring.nbytes == sum(entry.nbytes for entry in ring.snapshot())
        assert ring.nbytes == ring.payload_bytes + ring.n_groups * ENTRY_OVERHEAD_BYTES
    stats = ring.stats()
    assert stats["consistent"] is True
    assert ring.n_groups == ring.capacity_bytes // (120_000 + ENTRY_OVERHEAD_BYTES)
    assert stats["evicted_groups"] == 60 - ring.n_groups
    # eviction is oldest-first and no frame data is copied to do it
    assert ring.keys() == tuple(range(60 - ring.n_groups, 60))
    assert all(entry.images["left"] is frame for entry in ring.snapshot())


def test_ring_buffer_refuses_a_frame_larger_than_the_whole_budget():
    ring = BoundedRingBuffer(64 * 1024)
    small = np.zeros((32, 32), np.uint8)
    ring.push({"left": small, "right": small, "tex": small}, key=1)
    before = (ring.nbytes, ring.n_groups)
    huge = np.zeros((400, 400), np.uint8)           # 160,000 B > budget
    evicted = ring.push({"left": huge, "right": huge, "tex": huge}, key=2)
    assert evicted == ()
    assert (ring.nbytes, ring.n_groups) == before   # previous content intact
    assert ring.stats()["oversize_dropped"] == 1
    assert ring.find(2) is None


def test_ring_buffer_accounting_is_exact_for_random_frame_sizes():
    rng = np.random.default_rng(7)
    ring = BoundedRingBuffer(2 * 1024 * 1024)
    pushed = 0
    for i in range(200):
        height = int(rng.integers(8, 400))
        width = int(rng.integers(8, 400))
        image = np.zeros((height, width), np.uint8)
        ring.push({"left": image}, key=i, timestamp_s=i * 0.102)
        pushed += 1
        expected_payload = sum(entry.images["left"].nbytes
                               for entry in ring.snapshot())
        assert ring.payload_bytes == expected_payload
        assert ring.nbytes == expected_payload + len(ring) * ENTRY_OVERHEAD_BYTES
        assert ring.nbytes <= ring.capacity_bytes
    assert pushed == 200
    assert ring.stats()["pushed_groups"] == 200
    assert ring.stats()["evicted_groups"] + ring.n_groups == 200
    assert ring.seconds() == pytest.approx(ring.n_groups / DEFAULT_CADENCE_HZ)
    assert ring.capacity_seconds() == 0.0        # budget < one full 3.93 MB group
    full = BoundedRingBuffer(256 * 1024 * 1024)
    assert full.capacity_seconds() == pytest.approx(
        (256 * 1024 * 1024 // (GROUP_BYTES + ENTRY_OVERHEAD_BYTES))
        / DEFAULT_CADENCE_HZ)


def test_ring_buffer_eviction_keeps_the_same_frame_objects():
    ring = BoundedRingBuffer(3 * (64 * 64) + 3 * ENTRY_OVERHEAD_BYTES)
    frames = []
    for i in range(5):
        image = np.full((64, 64), i, np.uint8)
        frames.append(image)
        ring.push({"left": image}, key=i)
    # entries 0 and 1 were evicted; their arrays are the originals (no copy)
    assert ring.keys() == (2, 3, 4)
    assert ring.find(2) is not None
    assert ring.find(2).images["left"] is frames[2]
    assert ring.newest(1)[0].key == 4 and ring.oldest(1)[0].key == 2
    # copy_on_push means the buffer owns its pixels instead of borrowing them
    copying = BoundedRingBuffer(1 << 20, copy_on_push=True)
    original = np.zeros((16, 16), np.uint8)
    copying.push({"left": original})
    assert copying.snapshot()[0].images["left"] is not original


def test_ring_capacity_helpers_are_consistent():
    assert GROUP_BYTES == 3 * 1280 * 1024
    budget = capacity_for_seconds(2.0)
    assert budget == int(np.ceil(2.0 * DEFAULT_CADENCE_HZ)) * (
        GROUP_BYTES + ENTRY_OVERHEAD_BYTES)
    ring = BoundedRingBuffer(budget)
    assert ring.capacity_seconds() == pytest.approx(2.0, abs=0.15)
    with pytest.raises(ValueError):
        BoundedRingBuffer(0)
    with pytest.raises(ValueError):
        BoundedRingBuffer(1024, cadence_hz=0)


# ---------------------------------------------------------------------------
# 2. retention: accepted views are kept, duplicate frames are not
# ---------------------------------------------------------------------------

def test_retention_keeps_every_accepted_view_and_no_duplicate_frames(tmp_path):
    """Selection reuses coverage bins; the store keeps exactly the accepted set.

    The frame stream below contains exact duplicates of poses that are already
    covered.  The existing coverage measurement shows they add no new region /
    corner evidence, so they are not accepted views; the storage layer stores
    every accepted view and never grows with the rejected duplicates.
    """
    from tools.wizard import coverage

    spec = coverage.CoverageSpec(require_colour=False)
    candidates = [
        _observation(0, centre=(180.0, 170.0), distance=340.0),
        _observation(1, centre=(640.0, 512.0), distance=420.0),
        _observation(2, centre=(1100.0, 860.0), distance=520.0),
        _observation(3, centre=(640.0, 170.0), distance=420.0, roll=45.0),
        # duplicates of already-covered poses (same frame content too)
        _observation(4, centre=(640.0, 512.0), distance=420.0),
        _observation(5, centre=(640.0, 512.0), distance=420.0),
    ]
    accepted: list[dict] = []
    cells = set()
    for entry in candidates:
        measured = coverage.measure(accepted + [entry], spec)["D4"]["measured"]
        new_regions = (measured["regions_visited"],
                       tuple(sorted(measured.get("corners_visited") or [])))
        if not accepted or new_regions not in cells:
            accepted.append(entry)
            cells.add(new_regions)
    assert len(accepted) == 4                       # the two duplicates dropped

    paths, store = _store(tmp_path)
    for entry in accepted:
        store.add_view(_group(entry["view_id"]), entry)
    # a repeated delivery of the same frame/observation is idempotent
    store.add_view(_group(accepted[1]["view_id"]), accepted[1])

    loaded = session_io.load_views(paths)
    assert loaded["n_views"] == 4
    assert [view["index"] for view in loaded["views"]] == [0, 1, 2, 3]
    records, info = session_io.read_journal(paths.root / "views.jsonl")
    assert info["records"] == 4                    # one record per accepted view
    assert info["truncated"] is False and info["corrupt"] is False
    assert session_io.verify_views(paths)["ok"] is True
    # the frame stream itself is not retained: total stored bytes are four
    # views, not six, and each view is ~ the compressed triplet
    summary = session_io.storage_summary(paths)
    assert summary["views"] == 4
    assert summary["bytes"]["view_files"] < 6 * GROUP_BYTES


# ---------------------------------------------------------------------------
# 3. compact production storage: round trip, estimate, crash recovery
# ---------------------------------------------------------------------------

def test_full_synthetic_session_round_trip_replays_the_same_observations(tmp_path):
    from tools.wizard import coverage

    paths = session_io.create(tmp_path / "SESSION", {"mode": "high"},
                              storage="production")
    store = session_io.open_store(paths)
    spec = coverage.CoverageSpec(require_colour=False)
    entries = []
    originals: dict[int, dict[str, np.ndarray]] = {}
    for i in range(6):
        group = _group(i, seed=11 * i)
        entry = _observation(i, centre=(180.0 + 150 * i, 170.0 + 90 * i),
                             distance=340.0 + 30 * i, roll=15.0 * i)
        record = store.add_view(group, entry)
        # the controller passes the store record's files into the manifest, so
        # a resumed session re-hashes the compressed views (not PGMs)
        entry = {**entry, "files": {cam: {"path": meta["path"],
                                          "sha256": meta["sha256"],
                                          "bytes": meta["bytes"]}
                                    for cam, meta in record["files"].items()}}
        session_io.append_decision(paths, {"view_id": i, "accepted": True,
                                           "reasons": [], "quality": entry["quality"]})
        originals[i] = dict(group.images)
        entries.append(entry)
    session_io.save_manifest(paths, entries)
    store.rebuild_manifest()
    before = coverage.measure(entries, spec)

    loaded = session_io.load(paths)
    assert loaded["storage"]["mode"] == "production"
    assert loaded["storage"]["lossless_recording"] is False
    assert loaded["n_accepted"] == 6 and len(loaded["views"]) == 6
    assert loaded["consistent"] is True, loaded["notes"]

    # replay: exact pixels + the same refined observations
    replayed = list(store.iter_views(verify=True))
    assert len(replayed) == 6
    after_entries = []
    for item in replayed:
        record = item["record"]
        for cam, image in item["images"].items():
            assert np.array_equal(image, originals[int(record["index"])][cam])
        after_entries.append(item["observation"])
    assert after_entries == [{k: v for k, v in entry.items() if k != "files"}
                             for entry in entries]   # same refined observations

    after = coverage.measure(after_entries, spec)
    assert after == before                     # the replay reproduces coverage

    verified = session_io.verify(paths)
    assert verified["ok"] is True
    assert verified["storage"]["ok"] is True
    # manifest-listed compressed files (18) + production re-hash/decode (18)
    assert verified["checked"] == 6 * 3 + 6 * 3
    assert verified["storage"]["checked"] == 6 * 3
    summary = session_io.storage_summary(paths)
    assert summary["bytes_per_view"]["mean"] > 0
    assert summary["compression"]["ratio"] > 1.0
    assert paths.root.joinpath("views_manifest.json").is_file()
    assert session_io.load_views(paths)["complete"] is True


def test_production_storage_is_far_smaller_than_pgm_and_estimate_holds(tmp_path):
    paths, store = _store(tmp_path)
    entries = []
    pgm_total = 0
    for i in range(10):
        group = _group(i, seed=3 * i)
        pgm_total += sum(int(np.asarray(img).nbytes) for img in group.images.values())
        store.add_view(group, _observation(i))
        entries.append(_observation(i))
    session_io.save_manifest(paths, entries)
    summary = session_io.storage_summary(paths)
    actual = summary["bytes"]["total"]
    assert summary["bytes_per_view"]["min"] <= summary["bytes_per_view"]["mean"]
    assert summary["bytes_per_view"]["mean"] <= summary["bytes_per_view"]["max"]
    assert summary["bytes_per_view"]["mean"] < GROUP_BYTES
    # plate-like frames compress; the measurement is per session, not a claim
    # about arbitrary noise (a noise frame is incompressible).
    assert summary["compression"]["ratio"] > 1.5
    assert paths.root.joinpath("storage.json").is_file()

    estimate = store.estimate(10)
    assert estimate["views"] == 10
    assert estimate["bound_bytes"] >= actual          # documented upper bound
    assert estimate["total_bytes"] >= 0
    assert estimate["within_bound"] is True
    # the a-priori estimate for a realistic 60-view calibration is < 1 GB and
    # the lossless reference is an order of magnitude larger
    sixty = session_io.estimate_session_bytes(60)
    assert sixty["total_bytes"] < sixty["documented_bound_bytes"]
    assert sixty["lossless_reference_bytes"] > 50 * sixty["total_bytes"]


def test_truncated_journal_tail_recovers_the_intact_prefix(tmp_path):
    paths, store = _store(tmp_path)
    for i in range(5):
        img = np.full((32, 40), i * 7, np.uint8)
        store.add_images(i, {"left": img, "right": img, "tex": img},
                         _observation(i))
    journal = paths.root / "views.jsonl"
    data = journal.read_bytes()
    journal.write_bytes(data[:-7])                    # mid-record truncation
    # crash during the view write: the last view directory never appeared
    for path in (paths.root / "views/000004").glob("*"):
        path.unlink()
    (paths.root / "views/000004").rmdir()

    loaded = session_io.load_views(paths)
    assert loaded["n_views"] == 4
    assert loaded["journal"]["journal_truncated"] is True
    assert loaded["journal"]["tail_bytes"] > 0
    assert (loaded["journal"]["intact_bytes"]
            + loaded["journal"]["tail_bytes"]) == journal.stat().st_size
    assert loaded["journal"]["journal_records"] == 4
    assert loaded["recovered_orphans"] == []
    assert [view["index"] for view in loaded["views"]] == [0, 1, 2, 3]
    assert session_io.verify_views(paths)["ok"] is True
    # the ordinary loader reports the recovery instead of pretending
    report = session_io.load(paths)
    assert any("truncated tail" in note for note in report["notes"])

    # A crash between the view rename and the journal append is the common
    # case: the directory is complete, the record was lost.  It is recovered
    # from views/ without touching the journal.
    session_io.open_store(paths).add_images(
        5, {"left": np.zeros((32, 40), np.uint8)}, _observation(5))
    data = journal.read_bytes()
    journal.write_bytes(data[:-5])
    recovered = session_io.load_views(paths)
    assert recovered["journal"]["journal_truncated"] is True
    assert 5 in recovered["recovered_orphans"]
    assert recovered["n_views"] == 5          # view 4 never completed
    assert session_io.verify_views(paths)["ok"] is True

    # A writable re-open repairs the torn tail before appending: otherwise the
    # torn bytes would sit in the middle and every later record would be lost.
    repaired_store = session_io.open_store(paths)
    assert repaired_store._recovery.get("repaired") is True
    assert repaired_store._recovery.get("repaired_bytes", 0) > 0
    repaired_store.add_images(6, {"left": np.zeros((32, 40), np.uint8)},
                              _observation(6))
    after = session_io.load_views(paths)
    assert after["journal"]["journal_truncated"] is False
    assert after["journal"]["journal_corrupt"] is False
    assert after["n_views"] == 6
    assert session_io.verify_views(paths)["ok"] is True


def test_corrupt_middle_record_stops_at_the_damage(tmp_path):
    paths, store = _store(tmp_path)
    for i in range(5):
        store.add_view(_group(i), _observation(i))
    journal = paths.root / "views.jsonl"
    data = bytearray(journal.read_bytes())
    # flip a payload byte of the third record: header scans must stop there
    third = 2
    offset = 0
    for _ in range(third + 1):
        length = int.from_bytes(data[offset + 4:offset + 8], "little")
        offset += 12 + length
    data[offset - 3] ^= 0xFF
    journal.write_bytes(bytes(data))
    # the reader itself never guesses past the damage: only the intact prefix
    records, info = session_io.read_journal(journal)
    assert info["corrupt"] is True and len(records) == 2
    assert info["intact_bytes"] < len(data)
    # the loader recovers every complete view directory, so no view is lost
    loaded = session_io.load_views(paths)
    assert loaded["journal"]["journal_corrupt"] is True
    assert loaded["n_views"] == 5
    assert loaded["journal"]["recovered_orphans"] == 3
    assert loaded["complete"] is False                 # index needs rebuilding
    verified = session_io.verify_views(paths)
    assert verified["journal_ok"] is False
    assert verified["ok"] is True                      # the data itself is intact
    rebuilt = session_io.open_store(paths).rebuild_manifest()
    assert rebuilt["n_views"] == 5


def test_readding_a_view_supersedes_the_earlier_journal_record(tmp_path):
    paths, store = _store(tmp_path)
    store.add_view(_group(4), _observation(4, distance=420.0))
    corrected = _observation(4, distance=455.0)
    store.add_view(_group(4), corrected)
    loaded = session_io.load_views(paths)
    assert loaded["n_views"] == 1
    assert loaded["views"][0]["index"] == 4
    assert loaded["journal"]["superseded"] == [4]
    assert store.observation(4)["pose"]["t"][2] == 455.0
    assert session_io.verify_views(paths)["ok"] is True


def test_views_manifest_is_rebuildable_and_validated(tmp_path):
    paths, store = _store(tmp_path)
    for i in range(3):
        store.add_view(_group(i), _observation(i))
    assert session_io.verify_views(paths)["views_manifest"]["present"] is False
    built = store.rebuild_manifest()
    assert built["n_views"] == 3 and built["digest"]
    assert session_io.verify_views(paths)["views_manifest_ok"] is True
    # a stale manifest is reported (and can be rebuilt), not silently trusted
    doc = json.loads(paths.root.joinpath("views_manifest.json").read_text())
    doc["views"][0]["stored_bytes"] += 1
    paths.root.joinpath("views_manifest.json").write_text(json.dumps(doc))
    report = session_io.verify_views(paths)
    assert report["views_manifest_ok"] is False
    assert report["ok"] is True            # files are intact; the index is stale
    assert store.rebuild_manifest()["digest"] == built["digest"]
    assert session_io.verify_views(paths)["views_manifest_ok"] is True


def test_truncated_frame_file_is_detected_by_verification(tmp_path):
    paths, store = _store(tmp_path)
    store.add_view(_group(0), _observation(0))
    target = next((paths.root / "views/000000").glob("cam*.png"))
    target.write_bytes(target.read_bytes()[:32])
    report = session_io.verify_views(paths)
    assert report["ok"] is False
    assert any(item["kind"] in ("file", "decode", "pixels")
               for item in report["mismatches"])


# ---------------------------------------------------------------------------
# 4. modes: production vs diagnostic
# ---------------------------------------------------------------------------

def test_diagnostic_store_keeps_bounded_diagnostic_frames_only_in_diagnostic_mode(
        tmp_path):
    paths, store = _store(tmp_path)
    assert store.add_diagnostic(_group(0), "blur") is None      # production
    assert not (paths.root / "diagnostics").exists()

    diag_paths, diag = _store(tmp_path / "diag", mode="diagnostic",
                              diagnostic_max_groups=2)
    assert diag.config.lossless_recording is True
    first = diag.add_diagnostic(_group(0), "motion blur",
                                meta={"motion_blur_px": 4.2})
    assert first is not None and first["diagnostic"] is True
    assert first["reason"] == "motion blur"
    assert diag.add_diagnostic(_group(1), "saturation") is not None
    # the explicit budget stops retention; it never grows without a bound
    assert diag.add_diagnostic(_group(2), "blur") is None
    summary = session_io.storage_summary(diag_paths)
    assert summary["mode"] == "diagnostic"
    assert summary["diagnostics"]["groups"] == 2
    assert summary["diagnostics"]["frames"] == 2 * 3
    assert summary["diagnostics"]["bytes"] > 0
    assert (diag_paths.root / "diagnostics/000000/view.json").is_file()
    assert diag_paths.root.joinpath("diagnostics.jsonl").is_file()
    # production views and diagnostics are separate budgets
    assert summary["views"] == 0


def test_create_defaults_to_the_legacy_layout_for_compatibility(tmp_path):
    paths = session_io.create(tmp_path / "legacy", {"mode": "quick"})
    assert (paths.root / "groups").is_dir()
    assert not (paths.root / "views").exists()
    assert not (paths.root / "storage.json").exists()
    assert session_io.load(paths)["storage"] is None


# ---------------------------------------------------------------------------
# 5. capture: production is the default and records no .e1r
# ---------------------------------------------------------------------------

def test_default_capture_command_has_no_raw_recording(tmp_path):
    config = capture.CaptureConfig(session_dir=tmp_path / "s")
    cmd = config.command()
    assert config.storage_mode == "production"
    assert config.record is False
    assert "--out" in cmd and cmd[cmd.index("--out") + 1] == str(
        tmp_path / "s" / "frames")
    assert "--status-file" in cmd
    assert "--session-dir" not in cmd        # implies raw.e1r in the C++ tool
    assert "--record" not in cmd
    # the acquisition flags themselves are unchanged
    assert "--control" in cmd and "--ring" in cmd and "--trigger" in cmd


def test_diagnostic_capture_command_keeps_the_lossless_session_dir(tmp_path):
    config = capture.CaptureConfig(session_dir=tmp_path / "s", record=True)
    cmd = config.command()
    assert config.storage_mode == "diagnostic"
    assert "--session-dir" in cmd
    assert "--out" not in cmd
    mapped = capture.CaptureConfig.from_mapping(
        {"storage": "diagnostic"}, session_dir=tmp_path / "s")
    assert mapped.record is True and mapped.storage_mode == "diagnostic"
    assert capture.CaptureConfig.from_mapping(
        {"ring": 8}, session_dir=tmp_path / "s").record is False


def test_storage_environment_variable_is_an_explicit_operator_opt_in(
        tmp_path, monkeypatch):
    monkeypatch.setenv(capture.STORAGE_ENV_VAR, "diagnostic")
    mapped = capture.CaptureConfig.from_mapping({"ring": 5},
                                                session_dir=tmp_path / "s")
    assert mapped.record is True and mapped.storage_mode == "diagnostic"
    # an explicit mapping key wins over the environment
    explicit = capture.CaptureConfig.from_mapping(
        {"storage": "production"}, session_dir=tmp_path / "s")
    assert explicit.record is False and explicit.storage_mode == "production"
    # the low-level constructor always states what it wants and is not
    # overridden by the environment
    assert capture.CaptureConfig(session_dir=tmp_path / "s").record is False
    assert capture.CaptureConfig(session_dir=tmp_path / "s",
                                 record=True).storage_mode == "diagnostic"
    monkeypatch.delenv(capture.STORAGE_ENV_VAR)
    assert capture.CaptureConfig.from_mapping(
        {"ring": 5}, session_dir=tmp_path / "s").storage_mode == "production"


def test_live_source_path_selects_production_storage(tmp_path):
    """The wizard's live-source mapping gets production storage by default."""
    from tools.wizard import sources

    source = sources.LiveSource(tmp_path / "live",
                                capture_args={"backend": "simulated", "ring": 2})
    config = source.capture.config
    assert config.storage_mode == "production"
    assert config.as_dict()["storage_mode"] == "production"
    command = config.command()
    assert "--out" in command and "--session-dir" not in command
    assert "--record" not in command


def test_capture_refuses_smuggling_the_recording_through_extra_args(tmp_path):
    with pytest.raises(ValueError):
        capture.CaptureConfig(session_dir=tmp_path / "s",
                              extra_args=("--record", str(tmp_path / "raw.e1r")))
    with pytest.raises(ValueError):
        capture.CaptureConfig(session_dir=tmp_path / "s",
                              extra_args=("--session-dir", "/tmp/elsewhere"))
    # diagnostic mode may not contradict itself with --out either
    with pytest.raises(ValueError):
        capture.CaptureConfig(session_dir=tmp_path / "s", record=True,
                              extra_args=("--out", "/tmp/frames"))


@pytest.mark.skipif(not (REAL_BINARY.is_file() and os.access(REAL_BINARY, os.X_OK)),
                    reason="capture executable not built")
def test_default_capture_run_creates_no_e1r_and_verifies(tmp_path):
    """The guard: a normal (default) capture session must never write raw.e1r."""
    session = tmp_path / "session"
    supervisor = capture.LiveCaptureProcess(capture.CaptureConfig(
        binary=REAL_BINARY, session_dir=session, backend="simulated",
        ring=2, groups=3))
    assert supervisor.config.storage_mode == "production"
    try:
        supervisor.start()
        deadline = time.time() + 10
        while (time.time() < deadline
               and supervisor.status().get("state") not in ("running", "stopped")):
            time.sleep(0.02)
        result = supervisor.stop_and_verify(reason="production guard")
        assert result["stopped"] is True
        assert result["verified"] is True, result.get("reasons")
        acquisition = result["acquisition"]
        assert acquisition["state"] == "stopped_verified"
        assert acquisition["recording"]["requested"] is False
        assert acquisition["recording"]["finalized"] is True
        assert not (session / "raw.e1r").exists()
        assert (session / "frames" / "frames_manifest.json").is_file()
        assert not any("raw" in p.name for p in session.iterdir())
    finally:
        supervisor.close()


@pytest.mark.skipif(not (REAL_BINARY.is_file() and os.access(REAL_BINARY, os.X_OK)),
                    reason="capture executable not built")
def test_diagnostic_capture_run_keeps_the_lossless_recording(tmp_path):
    session = tmp_path / "session"
    supervisor = capture.LiveCaptureProcess(capture.CaptureConfig(
        binary=REAL_BINARY, session_dir=session, backend="simulated",
        ring=2, groups=3, record=True))
    try:
        supervisor.start()
        deadline = time.time() + 10
        while (time.time() < deadline
               and supervisor.status().get("state") not in ("running", "stopped")):
            time.sleep(0.02)
        result = supervisor.stop_and_verify(reason="diagnostic run")
        assert result["stopped"] is True
        assert result["verified"] is True, result.get("reasons")
        recording = result["acquisition"]["recording"]
        assert recording["requested"] is True
        assert recording["finalized"] is True
        assert (session / "raw.e1r").is_file()
        assert capture.read_recording_trailer(session / "raw.e1r")["present"] is True
    finally:
        supervisor.close()


# ---------------------------------------------------------------------------
# 6. migration and resume
# ---------------------------------------------------------------------------

def test_compact_session_migrates_legacy_pgm_views_and_resume_still_works(tmp_path):
    paths = session_io.create(tmp_path / "legacy", {"mode": "high"})
    files = {}
    entries = []
    for i in range(3):
        record = session_io.materialise_group(paths, _group(i))
        files[i] = record["files"]
        entries.append({**_observation(i), "files": files[i]})
        session_io.append_decision(paths, {"view_id": i, "accepted": True})
    session_io.save_manifest(paths, entries)
    before = session_io.verify(paths)
    assert before["ok"] is True and before["storage"] is None

    result = session_io.compact_session(paths, delete_pgm=True)
    assert result["n_views"] == 3 and result["deleted_pgm"] is True
    assert result["stored_bytes"] < result["pgm_bytes_before"]
    assert list(paths.groups_dir.rglob("*.pgm")) == []

    after = session_io.verify(paths)
    assert after["ok"] is True
    assert after["storage"]["ok"] is True
    loaded = session_io.load(paths)
    assert loaded["storage"]["mode"] == "production"
    for entry in loaded["accepted"]:
        for meta in entry["files"].values():
            assert meta["path"].startswith("views/")
    resumed = session_io.resume(paths)
    assert resumed["next_group_index"] == 3
    assert resumed["verify"]["ok"] is True


def test_resume_advances_past_production_views(tmp_path):
    paths, store = _store(tmp_path)
    entries = []
    for i in (0, 2, 5):
        store.add_view(_group(i), _observation(i), view_id=i)
        entries.append(_observation(i))
    session_io.save_manifest(paths, entries)
    manifest_before = paths.manifest_json.read_bytes()
    decisions_before = paths.decisions_jsonl.read_bytes()
    resumed = session_io.resume(paths)
    assert resumed["next_group_index"] == 6
    assert paths.manifest_json.read_bytes() == manifest_before
    assert paths.decisions_jsonl.read_bytes() == decisions_before
    assert resumed["verify"]["ok"] is True
