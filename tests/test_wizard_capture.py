"""Offline tests for the live-capture supervisor (no scanner, no USB).

The command line, the one-process-at-a-time lock and the safe-final-state
verdict are exercised against fabricated files; the real binary is never
started here (the simulated-backend integration check is run manually).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

import pytest

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tools.wizard import capture, types  # noqa: E402


# ---------------------------------------------------------------------------
# command line
# ---------------------------------------------------------------------------

def test_command_uses_verified_safe_settings_and_session_dir(tmp_path):
    # the verified device settings apply to the real (libusb) acquisition path
    config = capture.CaptureConfig(session_dir=tmp_path / "session",
                                   backend="libusb")
    cmd = config.command()
    assert cmd[0].endswith("einstar-capture-frames")
    assert "--session-dir" in cmd
    assert "--control" in cmd
    assert "--ring" in cmd
    # --session-dir already supplies the status file; combining them is
    # rejected by the C++ tool (exit 2)
    assert "--status-file" not in cmd
    joined = " ".join(cmd)
    for fragment in ("--trigger 100000", "--laser 100",
                     "--strobe 0:1000", "--strobe 1:300",
                     "--exposure-us 0:1200", "--exposure-us 1:1200",
                     "--exposure-us 2:5000",
                     "--gain 0:105", "--gain 1:105", "--gain 2:180"):
        assert fragment in joined, fragment
    # ring/max-seconds are passed through
    config2 = capture.CaptureConfig(session_dir=tmp_path / "s2", ring=12,
                                    max_seconds=30, groups=0,
                                    backend="libusb")
    cmd2 = config2.command()
    assert cmd2[cmd2.index("--ring") + 1] == "12"
    assert cmd2[cmd2.index("--max-seconds") + 1] == "30"


def test_simulated_backend_does_not_send_device_commands(tmp_path):
    # the simulator has no command channel: the tool rejects laser/strobe/
    # exposure/gain on that backend
    config = capture.CaptureConfig(session_dir=tmp_path / "session",
                                   backend="simulated")
    cmd = config.command()
    for flag in ("--laser", "--strobe", "--exposure-us", "--gain"):
        assert flag not in cmd, flag
    assert "--status-file" not in cmd


def test_explicit_status_path_is_passed(tmp_path):
    status = tmp_path / "elsewhere" / "status.json"
    config = capture.CaptureConfig(session_dir=tmp_path / "session",
                                   status=status)
    cmd = config.command()
    assert cmd[cmd.index("--status-file") + 1] == str(status)
    assert config.status == status


def test_config_from_mapping_accepts_cli_style_keys(tmp_path):
    config = capture.CaptureConfig.from_mapping({
        "--session-dir": str(tmp_path),
        "--ring": 5,
        "--max-seconds": 20,
        "--trigger": 100000,
        "--laser": 40,
        "--strobe": ["0:900", "1:200"],
        "--exposure-us": {"0": 1000, "1": 1100, "2": 4000},
        "--gain": {"0": 100, "1": 101, "2": 170},
        "--backend": "simulated",
    })
    assert config.ring == 5
    assert config.max_seconds == 20.0
    assert config.laser_percent == 40
    assert config.strobe == ((0, 900), (1, 200))
    assert config.exposure_us[2] == 4000


def test_config_rejects_unsafe_values(tmp_path):
    with pytest.raises(ValueError):
        capture.CaptureConfig(session_dir=tmp_path, laser_percent=101)
    with pytest.raises(ValueError):
        capture.CaptureConfig(session_dir=tmp_path, exposure_us={0: 0})
    with pytest.raises(ValueError):
        capture.CaptureConfig(session_dir=tmp_path, ring=-1)


# ---------------------------------------------------------------------------
# lock / one process at a time
# ---------------------------------------------------------------------------

def _fake_capture_process(tmp_path: Path) -> subprocess.Popen:
    """A live process whose argv[0] basename is the capture binary name.

    A symlink to ``sleep`` (not a shell script: the kernel would report the
    interpreter in ``/proc/<pid>/cmdline`` and the scanner would miss it).
    """
    target = tmp_path / "einstar-capture-frames"
    if not target.exists():
        target.symlink_to("/bin/sleep")
    return subprocess.Popen([str(target), "30"], stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)


def test_start_refuses_while_another_capture_process_is_alive(tmp_path):
    proc = _fake_capture_process(tmp_path)
    try:
        time.sleep(0.2)
        config = capture.CaptureConfig(session_dir=tmp_path / "session",
                                       backend="simulated")
        supervisor = capture.LiveCaptureProcess(config)
        with pytest.raises(capture.CaptureError) as excinfo:
            supervisor.start()
        assert "already running" in str(excinfo.value)
        assert supervisor.process is None
        assert not supervisor.running
        assert not supervisor.lock_path.exists()
    finally:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=5)


def test_live_lock_is_detected_and_stale_lock_is_cleared(tmp_path):
    proc = _fake_capture_process(tmp_path)
    try:
        time.sleep(0.2)
        supervisor = capture.LiveCaptureProcess(
            capture.CaptureConfig(session_dir=tmp_path, backend="simulated"))
        types.atomic_write_json(supervisor.lock_path, {
            "pid": proc.pid, "binary": "einstar-capture-frames",
            "started_at": time.time(), "session_dir": str(tmp_path)})
        assert supervisor.lock_is_live() is True

        # a lock whose pid is gone is stale, not live
        types.atomic_write_json(supervisor.lock_path, {
            "pid": 999_999_999, "binary": "einstar-capture-frames",
            "started_at": time.time(), "session_dir": str(tmp_path)})
        assert supervisor.lock_is_live() is False
    finally:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=5)


def test_stale_lock_is_removed_and_replaced(tmp_path):
    session = tmp_path / "session"
    session.mkdir()
    types.atomic_write_json(session / "capture.lock", {
        "pid": 999_999_999, "binary": "einstar-capture-frames",
        "started_at": time.time(), "session_dir": str(session)})
    config = capture.CaptureConfig(session_dir=session, backend="simulated",
                                   binary=tmp_path / "missing-binary")
    supervisor = capture.LiveCaptureProcess(config)
    # a missing binary must be refused without touching the lock
    with pytest.raises(capture.CaptureError):
        supervisor.start()
    assert supervisor.lock_path.exists()

    # a stale lock is cleared and replaced by ours (not treated as live)
    supervisor._acquire_lock()
    assert supervisor.stale_lock_removed is not None
    lock = supervisor.read_lock()
    assert lock is not None and lock["token"] == supervisor._token
    supervisor._cleanup_lock()
    assert not supervisor.lock_path.exists()


# ---------------------------------------------------------------------------
# status parsing / safe final state (fabricated JSON, no device)
# ---------------------------------------------------------------------------

def _write_status(tmp_path: Path, **overrides) -> Path:
    status = {
        "schema": "einstar-live-status-v1",
        "state": "stopped",
        "stop_reason": "control:stop",
        "groups_written": 42,
        "groups_on_disk": 4,
        "ring_size": 4,
        "frames_dropped": 1,
        "throughput_mbps": 39.4,
        "trigger_counts": {"mono": 0, "rgb": 0},
        "trigger_period_us": 0,
        "laser_percent": 0,
        "simulated": False,
        "live": True,
    }
    status.update(overrides)
    path = tmp_path / "status.json"
    types.atomic_write_json(path, status)
    return path


def test_safe_final_state_passes_for_a_clean_stop(tmp_path):
    _write_status(tmp_path)
    supervisor = capture.LiveCaptureProcess(
        capture.CaptureConfig(session_dir=tmp_path))
    verdict = supervisor.check_safe_final_state()
    assert verdict["ok"] is True, verdict["reasons"]
    assert verdict["state"] == "stopped"
    assert verdict["trigger_counts"] == {"mono": 0, "rgb": 0}
    assert verdict["laser_percent"] == 0
    assert verdict["stop_reason"] == "control:stop"


@pytest.mark.parametrize("overrides,fragment", [
    ({"state": "running"}, "expected 'stopped'"),
    ({"laser_percent": 100}, "laser"),
    ({"trigger_counts": {"mono": 1, "rgb": 0}}, "trigger"),
    ({"laser_percent": None}, "laser state is unavailable"),
    ({"trigger_counts": None, "trigger_on": None, "trigger_period_us": None},
     "trigger state is unavailable"),
])
def test_safe_final_state_fails_closed(tmp_path, overrides, fragment):
    _write_status(tmp_path, **overrides)
    supervisor = capture.LiveCaptureProcess(
        capture.CaptureConfig(session_dir=tmp_path))
    verdict = supervisor.check_safe_final_state()
    assert verdict["ok"] is False
    assert any(fragment in reason for reason in verdict["reasons"]), verdict


def test_simulated_backend_without_trigger_fields_still_verifies(tmp_path):
    _write_status(tmp_path, simulated=True, laser_percent=None,
                  trigger_counts=None, trigger_period_us=None,
                  state="stopped", stop_reason="max_seconds")
    supervisor = capture.LiveCaptureProcess(
        capture.CaptureConfig(session_dir=tmp_path, backend="simulated"))
    verdict = supervisor.check_safe_final_state()
    assert verdict["ok"] is True
    assert verdict["simulated"] is True
    assert "simulated backend" in verdict["note"]


def test_status_parser_normalises_the_live_schema(tmp_path):
    _write_status(tmp_path, state="running", groups_written=120,
                  elapsed_s=12.0, frames_dropped=3, laser_percent=100,
                  trigger_counts={"mono": 1, "rgb": 1})
    supervisor = capture.LiveCaptureProcess(
        capture.CaptureConfig(session_dir=tmp_path, ring=60))
    status = supervisor.status()
    assert status["state"] == "running"
    assert status["groups_written"] == 120
    assert status["drops"] == 3
    assert abs(status["fps"] - 10.0) < 1e-9
    assert status["ring_size"] == 60
    assert status["available"] is True


def test_poll_states_records_each_transition(tmp_path):
    supervisor = capture.LiveCaptureProcess(
        capture.CaptureConfig(session_dir=tmp_path))
    _write_status(tmp_path, state="running")
    supervisor.poll_states()
    _write_status(tmp_path, state="paused")
    supervisor.poll_states()
    _write_status(tmp_path, state="stopped")
    history = supervisor.poll_states()
    assert [entry["state"] for entry in history] == ["running", "paused",
                                                     "stopped"]
    # a repeated read must not append a duplicate transition
    supervisor.poll_states()
    assert len(supervisor.state_history) == 3


def test_control_file_is_written_atomically(tmp_path):
    supervisor = capture.LiveCaptureProcess(
        capture.CaptureConfig(session_dir=tmp_path))
    supervisor._write_control("pause")
    assert (tmp_path / "control.txt").read_text() == "pause\n"
    supervisor._write_control("resume")
    assert (tmp_path / "control.txt").read_text() == "resume\n"
    assert [c["command"] for c in supervisor.commands] == ["pause", "resume"]
    assert not list(tmp_path.glob("*.tmp"))
