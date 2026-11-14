"""Isolation between physical attempts that share one session directory.

The fresh-01 evidence showed a reused directory mixing two (arguably three)
attempts: 70 of 162 accepted views came from a previous run, a single session
carried two clock domains and two device-group ranges, and the first minutes of
the new attempt were spent replaying the old frame ring.  These tests pin the
three isolation rules:

* the capture supervisor moves a previous frame ring / raw stream / status
  aside (never deletes) before starting;
* the controller moves a previous session's views, decisions and manifests
  aside before writing a fresh session;
* live group timestamps use one monotonic clock, never a manifest/wall-clock
  mixture.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from tools.wizard import capture, controller, session_io, sources, types


def _synthetic(tmp_path: Path, *, n_views: int = 4):
    spec = sources.SyntheticSpec(n_views=n_views, seed=13, pattern="distance")
    synthetic = sources.SyntheticSource(spec)
    groups = list(synthetic)
    frames = sources.write_replay_frames(groups, tmp_path / "frames")
    factory = tmp_path / "factory.json"
    types.atomic_write_json(factory, synthetic.profile_dict())
    return synthetic, groups, frames, factory


def _write_pgm(path: Path, value: int = 90) -> None:
    arr = np.full((64, 80), value, dtype=np.uint8)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as fh:
        fh.write(b"P5\n80 64\n255\n")
        fh.write(arr.tobytes())


def test_capture_moves_previous_output_aside(tmp_path):
    session = tmp_path / "session"
    frames = session / "frames"
    _write_pgm(frames / "frame_000000_cam0.pgm")
    _write_pgm(frames / "frame_000000_cam1.pgm")
    _write_pgm(frames / "frame_000000_cam2.pgm")
    (frames / "frames_manifest.json").write_text('{"schema": "old"}')
    (session / "raw.e1r").write_bytes(b"OLD-RECORDING")
    (session / "status.json").write_text('{"state": "stopped"}')
    (session / "exposure-control.txt").write_text("exposure 0:2325 1:2325 2:20000\n")
    (session / "control.txt").write_text("stop\n")

    config = capture.CaptureConfig(session_dir=session,
                                   binary=Path("/bin/sh"))
    supervisor = capture.LiveCaptureProcess(config)
    info = supervisor.archive_previous_output()

    assert info["reused"] is True
    assert frames.is_dir()
    assert not list(frames.iterdir()), "a fresh frame ring must start empty"
    assert not (session / "raw.e1r").exists()
    archived = [entry for entry in info["archived"]]
    kinds = {entry["kind"] for entry in archived}
    assert {"frames", "recording", "status", "exposure-control.txt",
            "control.txt"} <= kinds
    frames_dest = Path(next(e["moved_to"] for e in archived
                            if e["kind"] == "frames"))
    assert (frames_dest / "frames_manifest.json").read_text() == '{"schema": "old"}'
    recording = Path(next(e["moved_to"] for e in archived
                          if e["kind"] == "recording"))
    assert recording.read_bytes() == b"OLD-RECORDING"
    # the previous evidence is preserved, not deleted
    assert frames_dest.is_dir() and recording.is_file()


def test_capture_leaves_an_empty_directory_alone(tmp_path):
    session = tmp_path / "session"
    (session / "frames").mkdir(parents=True)
    config = capture.CaptureConfig(session_dir=session, binary=Path("/bin/sh"))
    supervisor = capture.LiveCaptureProcess(config)
    info = supervisor.archive_previous_output()
    assert info["reused"] is False
    assert info["archived"] == []
    assert (session / "frames").is_dir()


def test_begin_quarantines_a_previous_session(tmp_path):
    _, _, frames, factory = _synthetic(tmp_path, n_views=4)
    source = sources.ReplayDirSource(frames)
    first = controller.WizardController(
        factory_profile=factory, session_dir=tmp_path / "session",
        mode="quick", source=source.info, ring_keep=4)
    first.begin()
    for group in source:
        first.ingest(group)
    accepted = len(first.session.accepted)
    assert accepted
    first.cancel()

    # A second attempt in the same directory must start empty rather than
    # appending to the first attempt's views and coverage.
    second = controller.WizardController(
        factory_profile=factory, session_dir=tmp_path / "session",
        mode="quick", source=source.info, ring_keep=4)
    begun = second.begin()
    assert begun["previous_session"] is not None
    moved = Path(begun["previous_session"]["directory"])
    assert moved.is_dir()
    assert (moved / "session.json").is_file()
    assert (moved / "views").is_dir()
    assert len(second.session.accepted) == 0
    assert len(second._views) == 0
    assert second._view_store is not None
    assert second._view_store.summary()["views"] == 0
    # the new session.json is a new record, not the old one
    new = json.loads(second.paths.session_json.read_text())
    old = json.loads((moved / "session.json").read_text())
    assert new["started_at"] >= old["started_at"]
    second.cancel()


def test_begin_preserves_an_in_memory_session(tmp_path):
    """``begin()`` is still idempotent and never quarantines live state."""
    _, _, frames, factory = _synthetic(tmp_path, n_views=4)
    source = sources.ReplayDirSource(frames)
    ctl = controller.WizardController(
        factory_profile=factory, session_dir=tmp_path / "session",
        mode="quick", source=source.info, ring_keep=4)
    ctl.begin()
    for group in source:
        ctl.ingest(group)
    accepted = len(ctl.session.accepted)
    assert accepted
    again = ctl.begin()
    assert again["created"] is False
    assert len(ctl.session.accepted) == accepted
    ctl.cancel()


def test_live_timestamp_is_one_monotonic_clock():
    ticks = iter([100.0, 100.1, 100.2])
    stamps = [sources.live_group_timestamp(clock=lambda: next(ticks))
              for _ in range(3)]
    assert stamps == pytest.approx([100.0, 100.1, 100.2])
    # never the manifest/wall-clock value, whatever is supplied elsewhere
    assert sources.live_group_timestamp(clock=lambda: 7.5) == 7.5
