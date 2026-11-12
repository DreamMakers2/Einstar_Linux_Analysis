"""Tests for the scanning application core (tools/scan/session.py).

The session is what both the CLI and the Qt window run, so these tests exercise
the real chain on real frames: reconstruct → track → fuse → mesh, plus the
failure paths a scanner must handle (a lost pose must never be integrated, a
missing frame must not crash the run).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys

import numpy as np
import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tools"))

from tools.scan import session as scan_session  # noqa: E402
from tools.track import baseline as track
from tools.track import projective as track_proj  # noqa: E402

SWEEP = Path(os.path.expanduser("~/einstar-private/sweep-b/frames"))
FACTORY = Path(os.path.expanduser("~/einstar-private/einstar-profiles/factory-e10.json"))
RECON = REPO / "build" / "recon" / "recon_cli"

pytestmark = pytest.mark.skipif(
    not (SWEEP.is_dir() and FACTORY.is_file() and RECON.is_file()),
    reason="recorded sweep, factory profile or recon binary not available")


def _config(tmp_path: Path, **kwargs) -> scan_session.ScanConfig:
    cfg = dict(frames_dir=SWEEP, profile=FACTORY, out_dir=tmp_path / "scan",
               recon_binary=RECON, voxel_mm=6.0, input_voxel_mm=10.0,
               scale=0.25)
    cfg.update(kwargs)
    return scan_session.ScanConfig(**cfg)


def test_session_produces_a_mesh_and_report(tmp_path):
    session = scan_session.ScanSession(_config(tmp_path))
    try:
        statuses = [session.process(i) for i in range(4)]
        assert all(s.ok for s in statuses), [s.as_dict() for s in statuses]
        assert all(s.integrated for s in statuses)
        assert session.n_frames == 4
        report = session.finish()
    finally:
        session.close()
    assert report["frames"] == 4
    assert report["vertices"] > 1000 and report["faces"] > 1000
    obj = Path(report["obj"])
    assert obj.is_file() and obj.stat().st_size > 10_000
    written = json.loads(Path(report["report"]).read_text())
    assert written["frames"] == 4
    assert len(written["frames_status"]) == 4
    assert len(written["poses"]) == 4
    # the first frame has no predecessor; the rest must have moved a little
    moves = [s.translation_mm for s in statuses[1:]]
    assert all(0.0 <= m < 5.0 for m in moves), moves


def test_snapshot_contract(tmp_path):
    session = scan_session.ScanSession(_config(tmp_path))
    try:
        session.process(0)
        session.process(1)
        snap = session.snapshot()
    finally:
        session.close()
    for key in ("frames_processed", "frames_integrated", "frames_rejected",
                "elapsed_s", "last", "depth", "cloud", "volume", "finished"):
        assert key in snap
    assert snap["frames_processed"] == 2
    assert snap["frames_integrated"] == 2
    assert isinstance(snap["cloud"], list) and len(snap["cloud"]) > 100
    assert snap["volume"]["voxel_mm"] == 6.0
    assert snap["depth"]["p50_mm"] is not None


def test_lost_tracking_is_not_integrated(tmp_path, monkeypatch):
    """A failed alignment must skip the frame, never absorb its pose."""
    # cloud path
    session = scan_session.ScanSession(_config(tmp_path, tracking="cloud"))
    real_align = track.align_point_to_plane

    def failing_align(source, target, *a, **k):
        result = real_align(source, target, *a, **k)
        result.ok = False
        result.reason = "simulated failure"
        return result

    try:
        first = session.process(0)
        assert first.ok
        monkeypatch.setattr(track, "align_point_to_plane", failing_align)
        bad = session.process(1)
        assert bad.ok is False
        assert "tracking lost" in bad.reason
        assert bad.integrated is False

        # projective path: the same guarantee
        proj = scan_session.ScanSession(_config(tmp_path / "proj", tracking="projective"))
        real_proj = track_proj.align_projective

        def failing_proj(source, target, *a, **k):
            out = dict(real_proj(source, target, *a, **k))
            out["ok"] = False
            out["reason"] = "simulated failure"
            return out

        assert proj.process(0).ok
        monkeypatch.setattr(track_proj, "align_projective", failing_proj)
        bad2 = proj.process(1)
        assert bad2.ok is False
        assert "tracking lost" in bad2.reason
        assert bad2.integrated is False
        proj.close()
        assert session.n_frames == 1          # only the good frame is fused
        assert session.n_rejected == 1
        snap = session.snapshot()
        assert snap["frames_rejected"] == 1
    finally:
        session.close()


def test_missing_frame_is_reported_not_fatal(tmp_path):
    session = scan_session.ScanSession(_config(tmp_path))
    try:
        status = session.process(99999)
        assert status.ok is False
        assert status.reason
        report = session.finish()
    finally:
        session.close()
    assert report["frames"] == 0


def test_finish_is_idempotent(tmp_path):
    session = scan_session.ScanSession(_config(tmp_path))
    try:
        session.process(0)
        first = session.finish()
        second = session.finish()
    finally:
        session.close()
    assert first["vertices"] == second["vertices"]
    assert first["report"] == second["report"]


def test_qt_scan_window_headless(tmp_path):
    """The Qt window must drive the same session and produce a snapshot."""
    import subprocess
    app = REPO / "tools" / "scan" / "app.py"
    python = REPO / ".venv" / "bin" / "python"
    if not python.is_file():  # pragma: no cover
        pytest.skip("venv python missing")
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["OMP_NUM_THREADS"] = "2"
    env.pop("DISPLAY", None)
    shot = tmp_path / "scan.png"
    proc = subprocess.run(
        [str(python), str(app), "--frames", str(SWEEP), "--profile", str(FACTORY),
         "--out", str(tmp_path / "scan-gui"), "--recon", str(RECON),
         "--count", "3", "--scale", "0.25", "--headless",
         "--snapshot-out", str(shot)],
        cwd=str(REPO), env=env, capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, proc.stderr[-1500:]
    assert "frames=3" in proc.stdout, proc.stdout[-400:]
    assert shot.is_file() and shot.stat().st_size > 10_000


def test_live_capture_source_plumbing(tmp_path):
    """Capture start/follow/stop on the simulated backend (no device needed)."""
    from tools.scan.live import CaptureSource
    binary = REPO / "build" / "driver" / "einstar-capture-frames"
    if not binary.is_file():
        pytest.skip("capture binary missing")
    source = CaptureSource(session_dir=tmp_path / "live", capture_binary=binary,
                           backend="simulated", ring=20,
                           extra_args=("--max-seconds", "30"))
    started = source.start()
    if not started.get("started"):
        pytest.skip(f"simulated capture did not start: {started.get('error')}")
    ids = []
    try:
        for frame_id in source.ids(max_frames=3, timeout_s=25, poll_s=0.1):
            ids.append(frame_id)
    finally:
        stopped = source.stop()
    assert ids, "no frames were seen"
    assert ids == sorted(ids)
    assert stopped.get("stopped") is True
    safe = stopped.get("safe") or {}
    assert safe.get("ok") is True, safe
    snap = source.snapshot()
    assert snap["frames_seen"] == len(ids)
    assert snap["stop_reason"] in {"control:stop", "max_seconds"}
    # frames really landed on disk with all three cameras
    for frame_id in ids[:1]:
        for cam in (0, 1, 2):
            assert (source.frames_dir / f"frame_{frame_id:06d}_cam{cam}.pgm").is_file()


def test_live_scan_app_reports_and_stops(tmp_path):
    """The scan app in --live mode must run, stop the capture and write a report."""
    import subprocess
    binary = REPO / "build" / "driver" / "einstar-capture-frames"
    if not binary.is_file():
        pytest.skip("capture binary missing")
    app = REPO / "tools" / "scan" / "app.py"
    python = REPO / ".venv" / "bin" / "python"
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["OMP_NUM_THREADS"] = "2"
    env.pop("DISPLAY", None)
    out = tmp_path / "live-scan"
    proc = subprocess.run(
        [str(python), str(app), "--live", "--backend", "simulated",
         "--capture-binary", str(binary), "--frames", str(tmp_path),
         "--profile", str(FACTORY), "--out", str(out), "--recon", str(RECON),
         "--count", "2", "--scale", "0.25", "--headless"],
        cwd=str(REPO), env=env, capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, proc.stderr[-1500:]
    report = json.loads((out / "scan_report.json").read_text())
    assert report["frames"] >= 1
    capture_status = json.loads((out / "capture" / "status.json").read_text())
    assert capture_status["state"] == "stopped"
    assert capture_status["stop_reason"] in {"control:stop", "max_seconds"}


def test_export_formats_and_project_round_trip(tmp_path):
    from tools.scan import export as scan_export
    session = scan_session.ScanSession(_config(tmp_path))
    try:
        for i in range(3):
            assert session.process(i).ok
        report = session.finish()
    finally:
        session.close()
    for key in ("obj", "ply", "stl", "project"):
        assert report.get(key) and Path(report[key]).is_file(), key
    # PLY header and counts must match the report
    ply = Path(report["ply"]).read_text().splitlines()
    assert ply[0] == "ply" and "format ascii 1.0" in ply
    n_vertex = int(next(ln for ln in ply if ln.startswith("element vertex")).split()[-1])
    n_face = int(next(ln for ln in ply if ln.startswith("element face")).split()[-1])
    assert n_vertex == report["vertices"]
    assert n_face == report["faces"]
    # STL: one facet normal + three vertices per face
    stl = Path(report["stl"]).read_text()
    assert stl.startswith("solid einstar")
    assert stl.count("facet normal") == report["faces"]
    assert stl.count("vertex ") == 3 * report["faces"]
    # project round trip
    doc = scan_export.load_project(report["project"])
    assert doc["schema"] == scan_export.PROJECT_SCHEMA
    assert doc["report"]["frames"] == 3
    assert len(doc["report"]["poses"]) == 3
    assert Path(doc["config"]["frames_dir"]).is_dir()
    assert scan_export.project_clouds_available(doc) is True


def test_project_opens_in_the_3d_view(tmp_path):
    """A saved project must reopen and render its mesh without scanning."""
    import subprocess
    app = REPO / "tools" / "scan" / "app.py"
    python = REPO / ".venv" / "bin" / "python"
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["OMP_NUM_THREADS"] = "2"
    env.pop("DISPLAY", None)
    # make a small scan to get a project
    out = tmp_path / "scan3d"
    made = subprocess.run(
        [str(python), "-m", "tools.scan.cli", "--frames", str(SWEEP),
         "--profile", str(FACTORY), "--out", str(out), "--recon", str(RECON),
         "--count", "3", "--scale", "0.25"],
        cwd=str(REPO), env=env, capture_output=True, text=True, timeout=300)
    assert made.returncode == 0, made.stderr[-800:]
    project = out / "scan_project.json"
    assert project.is_file()
    shot = tmp_path / "view.png"
    proc = subprocess.run(
        [str(python), str(app), "--frames", str(tmp_path),
         "--profile", str(FACTORY), "--out", str(tmp_path / "view-out"),
         "--project", str(project), "--headless", "--snapshot-out", str(shot)],
        cwd=str(REPO), env=env, capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, proc.stderr[-1200:]
    assert f"project={project}" in proc.stdout
    assert shot.is_file() and shot.stat().st_size > 10_000


def test_ply_mesh_reader_round_trip(tmp_path):
    from tools.scan import export as scan_export
    verts = np.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0],
                      [0.0, 0.0, 1.0]])
    faces = np.array([[0, 1, 2], [0, 1, 3], [0, 2, 3], [1, 2, 3]])
    path = scan_export.write_ply(tmp_path / "tetra.ply", verts, faces)
    back_v, back_f = scan_export.read_ply_mesh(path)
    assert back_v.shape == verts.shape and np.allclose(back_v, verts)
    assert back_f.shape == faces.shape and np.array_equal(back_f, faces)


def test_launcher_opens_scan_and_profiles(tmp_path):
    """One entry point: the launcher must open the scan window and show profiles."""
    import subprocess
    app = REPO / "tools" / "app.py"
    python = REPO / ".venv" / "bin" / "python"
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["OMP_NUM_THREADS"] = "2"
    env.pop("DISPLAY", None)
    script = (
        "import sys; sys.path.insert(0,'tools'); sys.path.insert(0,'.')\n"
        "from PySide6 import QtWidgets\n"
        "app = QtWidgets.QApplication([])\n"
        "import tools.app as L\n"
        "ap = L.build_parser().parse_args(['--source','replay','--frames',sys.argv[1],"
        "'--profile',sys.argv[2],'--session-dir',sys.argv[3]])\n"
        "w = L.Launcher(ap); w.show(); app.processEvents()\n"
        "w.open_scan(); app.processEvents()\n"
        "assert w._scan is not None, 'scan window did not open'\n"
        "w.show_profiles()\n"
        "lines = w.info.toPlainText().splitlines()\n"
        "assert any('factory profile' in l for l in lines), lines\n"
        "w.close(); app.processEvents()\n"
        "print('launcher-ok')\n")
    proc = subprocess.run([str(python), "-c", script, str(SWEEP), str(FACTORY),
                           str(tmp_path / "launch")],
                          cwd=str(REPO), env=env, capture_output=True, text=True,
                          timeout=300)
    assert proc.returncode == 0, proc.stderr[-1500:]
    assert "launcher-ok" in proc.stdout


def test_scan_is_byte_identical_across_runs(tmp_path):
    """The same recording and settings must produce the same mesh."""
    import hashlib
    digests = []
    for run in ("a", "b"):
        session = scan_session.ScanSession(_config(tmp_path / run))
        try:
            for i in range(3):
                assert session.process(i).ok
            report = session.finish()
        finally:
            session.close()
        data = Path(report["ply"]).read_bytes()
        digests.append(hashlib.sha256(data).hexdigest())
    assert digests[0] == digests[1], digests


def test_preview_mesh_is_decimated_and_final_mesh_is_not(tmp_path):
    session = scan_session.ScanSession(_config(tmp_path))
    try:
        for i in range(3):
            assert session.process(i).ok
        preview = session.preview_mesh(target_faces=2000)
        assert preview is not None
        verts, faces = preview
        assert len(faces) <= 2100, len(faces)
        assert len(verts) > 0
        final = session.finish()
    finally:
        session.close()
    assert final["faces"] > len(faces), (final["faces"], len(faces))


def test_reprocess_from_project_reuses_the_recorded_poses(tmp_path):
    """A saved scan can be re-fused at another voxel size without tracking."""
    base = tmp_path / "first"
    session = scan_session.ScanSession(_config(base, voxel_mm=8.0))
    try:
        for i in range(3):
            assert session.process(i).ok
        first = session.finish()
    finally:
        session.close()

    repro = scan_session.ScanSession.from_project(
        first["project"], out_dir=tmp_path / "repro", voxel_mm=2.0)
    try:
        assert repro.replay_poses and len(repro.replay_poses) == 3
        for idx in repro.replay_order:
            status = repro.process(idx)
            assert status.ok, status.reason
            assert status.tracking_ok is True
            assert status.tracking_reason == "replayed pose"
        second = repro.finish()
    finally:
        repro.close()

    # finer voxels must produce a larger mesh from the same data
    assert second["vertices"] > first["vertices"]
    # and the poses must be exactly the recorded ones
    a = np.asarray(json.loads(Path(first["report"]).read_text())["poses"], float)
    b = np.asarray(json.loads(Path(second["report"]).read_text())["poses"], float)
    assert a.shape == b.shape
    assert np.allclose(a, b, atol=1e-9)
    assert (Path(second["ply"]).is_file() and Path(second["obj"]).is_file())


def test_session_tracks_projectively_by_default_and_falls_back(tmp_path):
    """The projective path is the default; a missing depth map must not break it."""
    cfg = _config(tmp_path / "proj")
    assert cfg.tracking == "projective"
    session = scan_session.ScanSession(cfg)
    try:
        first = session.process(0)
        assert first.ok
        second = session.process(1)
        assert second.ok, second.reason
        # the projective path reports its own residual and a replayed/ICP reason
        assert second.tracking_reason in ("ok", "replayed pose"), second.tracking_reason
        assert second.rms_mm is not None
    finally:
        session.close()

    # a frames directory whose reconstructions carry no depth map falls back to
    # the cloud tracker instead of failing
    fallback = _config(tmp_path / "fb")
    session = scan_session.ScanSession(fallback)
    try:
        session.process(0)
        session._prev_depth = None                      # simulate no depth map
        status = session.process(1)
        assert status.ok, status.reason
        assert status.tracking_reason != "replayed pose"
    finally:
        session.close()
