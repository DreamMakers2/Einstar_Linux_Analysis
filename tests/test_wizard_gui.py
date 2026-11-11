"""End-to-end GUI tests, run in a subprocess.

Qt (PySide6) does not survive being initialised inside the same interpreter as
the rest of this suite, so every test here drives the real entry point
``tools/wizard/gui/app.py`` as a child process and checks its output and the
snapshot it writes.  That is also a more honest test: it exercises the exact
command a user runs.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

REPO = Path(__file__).resolve().parents[1]
APP = REPO / "tools" / "wizard" / "gui" / "app.py"
PYTHON = REPO / ".venv" / "bin" / "python"
if not PYTHON.is_file():  # pragma: no cover - fall back to the running one
    PYTHON = Path(sys.executable)


def _run(args, timeout=300):
    env = dict(os.environ)
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    env.pop("DISPLAY", None)
    env.pop("WAYLAND_DISPLAY", None)
    return subprocess.run([str(PYTHON), str(APP), *args],
                          cwd=str(REPO), env=env, timeout=timeout,
                          capture_output=True, text=True)


def _summary(stdout: str) -> dict:
    for line in reversed(stdout.splitlines()):
        if line.startswith("wizard: "):
            out = {}
            for part in line[len("wizard: "):].split():
                if "=" in part:
                    key, _, value = part.partition("=")
                    out[key] = value
            return out
    return {}


def test_app_help_is_available():
    proc = subprocess.run([str(PYTHON), str(APP), "--help"], cwd=str(REPO),
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0
    for token in ("--source", "--frames", "--session-dir", "--headless", "--demo"):
        assert token in proc.stdout


@pytest.mark.skipif(
    not (Path.home() / "einstar-private" / "sweep-b" / "frames").is_dir(),
    reason="replay capture not available")
def test_replay_session_snapshot(tmp_path):
    """Real controller + real frames: renders, decides, shuts down cleanly."""
    frames = Path.home() / "einstar-private" / "sweep-b" / "frames"
    out = tmp_path / "session"
    shot = tmp_path / "wizard.png"
    proc = _run(["--source", "replay", "--frames", str(frames), "--pace", "40",
                 "--mode", "quick", "--session-dir", str(out),
                 "--headless", "--duration", "12", "--snapshot-out", str(shot)])
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert shot.is_file() and shot.stat().st_size > 20_000
    summary = _summary(proc.stdout)
    assert summary.get("state") in {"recording", "cancelled", "done"}
    assert int(summary.get("accepted", "0")) >= 1
    assert int(summary.get("rejected", "0")) >= 1
    assert summary.get("ready") == "False", summary
    for name in ("session.json", "decisions.jsonl", "groups"):
        assert (out / name).exists(), name


def test_demo_controller_renders_without_data(tmp_path):
    """Snapshot smoke test on an empty frame directory (no groups, no crash)."""
    shot = tmp_path / "demo.png"
    proc = _run(["--source", "replay", "--frames", str(tmp_path), "--demo",
                 "--session-dir", str(tmp_path / "s"),
                 "--headless", "--duration", "2", "--snapshot-out", str(shot)])
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert shot.is_file() and shot.stat().st_size > 10_000


def test_vendor_corpus_session_if_available(tmp_path):
    corpus = Path.home() / "einstar-private" / "vendor-calib-rapid"
    if not corpus.is_dir():
        pytest.skip("vendor corpus not available")
    out = tmp_path / "vendor"
    shot = tmp_path / "vendor.png"
    proc = _run(["--source", "vendor", "--frames", str(corpus), "--pace", "5",
                 "--mode", "standard", "--session-dir", str(out), "--finish",
                 "--headless", "--duration", "20", "--snapshot-out", str(shot)],
                timeout=900)
    assert proc.returncode == 0, proc.stderr[-2000:]
    summary = _summary(proc.stdout)
    assert int(summary.get("accepted", "0")) >= 5, summary
    data = json.loads((out / "manifest.json").read_text())
    accepted = data.get("accepted") or data.get("views") or []
    assert len(accepted) >= 5
    # the finish path must produce a candidate and a full gate report
    candidate = out / "candidate" / "profile.json"
    validation = out / "candidate" / "validation.json"
    assert candidate.is_file() and validation.is_file()
    profile = json.loads(candidate.read_text())
    assert profile.get("validation") == "pending"
    report = json.loads(validation.read_text())
    lc = report.get("lifecycle_validate") or {}
    assert lc.get("gates"), lc
    assert (out / "report.md").is_file()
