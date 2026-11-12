"""Moving real-plate replay: split preview must stay at the source cadence.

This is the sustained moving-viewpoint responsiveness check.  It deliberately
does not claim READY: the real sweep does not contain the full pose diversity.
It asserts that an operator playback at 10 Hz retains a responsive preview and
a live decision rate while the plate moves.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

REPO = Path(__file__).resolve().parents[1]
PYTHON = REPO / ".venv" / "bin" / "python"
if not PYTHON.is_file():
    PYTHON = Path(sys.executable)
APP = REPO / "tools" / "wizard" / "gui" / "app.py"


def test_moving_replay_keeps_a_responsive_split_preview(tmp_path):
    frames = Path(os.path.expanduser("~/einstar-private/sweep-b/frames"))
    if not frames.is_dir():
        pytest.skip("real moving-plate recording not available")
    out = tmp_path / "moving"
    env = dict(os.environ)
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    env["PYTHONPATH"] = os.pathsep.join(
        [str(REPO), str(REPO / "tools"), env.get("PYTHONPATH", "")])
    proc = subprocess.run(
        [str(PYTHON), str(APP), "--source", "replay", "--frames", str(frames),
         "--pace", "10", "--async-decisions", "--mode", "standard",
         "--session-dir", str(out), "--headless", "--duration", "12",
         "--snapshot-out", str(tmp_path / "moving.png")],
        cwd=str(REPO), env=env, text=True, capture_output=True, timeout=120)
    assert proc.returncode == 0, proc.stderr[-3000:]
    metrics = json.loads((out / "ui_metrics.json").read_text())
    worker = metrics["worker"]
    assert worker["async_decisions"] is True
    assert worker["preview"]["hz"] >= 8.0, worker
    assert worker["preview"]["p95_ms"] < 250.0, worker
    assert worker["groups_seen"] >= 90, worker
    assert worker["decision"]["count"] >= 20, worker
    assert not metrics.get("error"), metrics.get("error")
