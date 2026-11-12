"""Full-window acceptance: READY -> finish -> validated candidate, and refusal.

These are deliberately end-to-end: the application, ``ModernMainWindow``,
``SessionWorker``, ``WizardController`` and the real synthetic renderer all run
in a child interpreter.  The positive fixture is the same 36-view
``coverage-mid`` scene used by the rehearsal, with a 1.5 px factory-intrinsic
perturbation so the candidate has a real improvement to demonstrate.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

REPO = Path(__file__).resolve().parents[1]
PYTHON = REPO / ".venv" / "bin" / "python"
if not PYTHON.is_file():  # public clean clone runs from the caller's venv
    PYTHON = Path(sys.executable)
APP = REPO / "tools" / "wizard" / "gui" / "app.py"


def _run(args, timeout=420):
    env = dict(os.environ)
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    env["PYTHONPATH"] = os.pathsep.join(
        [str(REPO), str(REPO / "tools"), env.get("PYTHONPATH", "")])
    return subprocess.run([str(PYTHON), str(APP), *args], cwd=str(REPO),
                          env=env, text=True, capture_output=True,
                          timeout=timeout)


def _summary(stdout: str) -> dict:
    for line in reversed(stdout.splitlines()):
        if line.startswith("wizard: "):
            body = line[len("wizard: "):]
            return dict(part.split("=", 1) for part in body.split()
                        if "=" in part)
    raise AssertionError(f"no wizard summary in stdout:\n{stdout[-4000:]}")


def _factory_profile(path: Path, perturb_px: float = 1.5) -> None:
    sys.path.insert(0, str(REPO))
    sys.path.insert(0, str(REPO / "tools"))
    from tools.wizard import sources, types
    spec = sources.SyntheticSpec(pattern="coverage", n_views=36, seed=7,
                                 factory_perturb_px=perturb_px)
    src = sources.SyntheticSource(spec)
    types.atomic_write_json(path, src.profile_dict())


def test_full_window_reaches_ready_and_writes_validated_candidate(tmp_path):
    profile = tmp_path / "factory.json"
    _factory_profile(profile)
    before = hashlib.sha256(profile.read_bytes()).hexdigest()
    out = tmp_path / "session"
    proc = _run(["--source", "synthetic", "--scene", "coverage",
                 "--views", "36", "--seed", "7", "--profile", str(profile),
                 "--session-dir", str(out), "--headless", "--autostart",
                 "--duration", "180", "--finish",
                 "--snapshot-out", str(tmp_path / "ready.png")])
    assert proc.returncode == 0, proc.stderr[-4000:]
    summary = _summary(proc.stdout)
    assert summary.get("ready") == "True", summary
    assert int(summary.get("coverage", "0/8").split("/")[0]) == 8, summary
    assert int(summary.get("accepted", "0")) >= 30, summary
    assert hashlib.sha256(profile.read_bytes()).hexdigest() == before

    validation_path = out / "candidate" / "validation.json"
    assert (out / "candidate" / "profile.json").is_file()
    assert validation_path.is_file()
    validation = json.loads(validation_path.read_text())
    assert validation.get("passed") is True
    verdict = validation.get("verdict") or {}
    assert verdict.get("would_activate") is True, verdict
    assert (verdict.get("gates") or {}).get("lifecycle_passed") is True
    assert (verdict.get("gates") or {}).get("coverage_ready") is True
    assert all(validation.get("observability", {}).get("gates", {}).values())
    assert (out / "report.md").is_file()
    metrics = json.loads((out / "ui_metrics.json").read_text())
    assert metrics["finish"]["validated"] is True, metrics.get("finish")


def test_full_window_reports_unvalidated_candidate_truthfully(tmp_path):
    # A READY session can still fail independent validation when the factory
    # profile is already the ground truth (no improvement to demonstrate).
    # The window must say "candidate not validated", never "validated".
    profile = tmp_path / "factory-true.json"
    _factory_profile(profile, perturb_px=0.0)
    out = tmp_path / "session"
    proc = _run(["--source", "synthetic", "--scene", "coverage",
                 "--views", "36", "--seed", "7", "--profile", str(profile),
                 "--session-dir", str(out), "--headless", "--autostart",
                 "--duration", "180", "--finish",
                 "--snapshot-out", str(tmp_path / "unvalidated.png")])
    assert proc.returncode == 0, proc.stderr[-4000:]
    summary = _summary(proc.stdout)
    assert summary.get("ready") == "True", summary
    validation = json.loads((out / "candidate" / "validation.json").read_text())
    assert validation.get("passed") is False
    assert (validation.get("verdict") or {}).get("would_activate") is False
    metrics = json.loads((out / "ui_metrics.json").read_text())
    assert metrics["finish"]["validated"] is False, metrics.get("finish")
    assert metrics["finish"]["reasons"], metrics.get("finish")


def test_full_window_refuses_finish_when_coverage_is_incomplete(tmp_path):
    profile = tmp_path / "factory.json"
    _factory_profile(profile)
    out = tmp_path / "session"
    proc = _run(["--source", "synthetic", "--scene", "diverse",
                 "--views", "20", "--seed", "7", "--profile", str(profile),
                 "--session-dir", str(out), "--headless", "--autostart",
                 "--duration", "45", "--finish",
                 "--snapshot-out", str(tmp_path / "refused.png")])
    assert proc.returncode == 0, proc.stderr[-4000:]
    summary = _summary(proc.stdout)
    assert summary.get("ready") == "False", summary
    assert not (out / "candidate" / "profile.json").exists()
    assert not (out / "candidate" / "validation.json").exists()
