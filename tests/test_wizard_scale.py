"""35–60 accepted-view end-to-end controller acceptance.

The GUI tests prove the operator path at 33 accepted views; this one exercises
the larger 60-rendered-view fixture and the final validation, so the claimed
35–60 range is represented in the suite rather than only in a manual run.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tools"))

from tools.wizard import controller, sources, types  # noqa: E402


def test_sixty_view_workflow_reaches_ready_and_validates(tmp_path):
    spec = sources.SyntheticSpec(pattern="coverage", n_views=60, seed=7,
                                 factory_perturb_px=1.5)
    src = sources.SyntheticSource(spec)
    factory = tmp_path / "factory.json"
    types.atomic_write_json(factory, src.profile_dict())
    before = hashlib.sha256(factory.read_bytes()).hexdigest()
    ctl = controller.WizardController(factory_profile=str(factory),
                                      session_dir=str(tmp_path / "session"),
                                      mode="standard", source=src.info)
    ctl.begin()
    for group in src:
        ctl.ingest(group)
    assert 35 <= len(ctl.session.accepted) <= 60
    snap = ctl.snapshot()
    assert (snap.get("coverage") or {}).get("satisfied") == 8
    assert (snap.get("readiness") or {}).get("ready") is True
    result = ctl.finish()
    assert result.get("ok") is True
    validation = json.loads(ctl.validation_path.read_text())
    assert validation.get("passed") is True
    verdict = validation.get("verdict") or {}
    assert verdict.get("would_activate") is True, verdict
    assert (verdict.get("gates") or {}).get("lifecycle_passed") is True
    assert hashlib.sha256(factory.read_bytes()).hexdigest() == before
    ctl.close()
