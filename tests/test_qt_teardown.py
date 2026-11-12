"""Regression guard for the Qt shutdown use-after-free.

The original failure happened *after* pytest printed its summary, so a green
test count was not enough.  Run the dialog test in a fresh interpreter several
times and require a zero exit code every time; a QObject parent/child visit
ordering bug exits 135/139.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

REPO = Path(__file__).resolve().parents[1]


def test_profiles_dialog_process_exits_cleanly(tmp_path):
    env = dict(os.environ)
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    env["PYTHONPATH"] = os.pathsep.join(
        [str(REPO), str(REPO / "tools"), env.get("PYTHONPATH", "")])
    target = "tests/test_profile_view.py::test_profiles_dialog_lists_and_gates_the_buttons"
    for attempt in range(8):
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", target,
             "-p", "no:cacheprovider"],
            cwd=REPO, env=env, text=True, capture_output=True, timeout=120)
        combined = (proc.stdout or "") + (proc.stderr or "")
        assert proc.returncode == 0, (
            f"attempt {attempt}: exit {proc.returncode}\n{combined[-4000:]}")
        assert "Fatal Python error" not in combined
        assert "Bus error" not in combined and "Segmentation fault" not in combined
