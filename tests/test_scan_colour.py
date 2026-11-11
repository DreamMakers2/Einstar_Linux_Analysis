"""White balance for scanned colour (tools/scan/color.py).

The correction is a grey-world gain on the highlight set.  These tests check
that it removes a known cast, leaves a neutral input alone, and stays bounded -
and that the session records what it did (gains, neutrality before/after,
clipping) so the operation is auditable rather than silent.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys

import numpy as np
import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tools"))

from tools.scan import color as scan_color  # noqa: E402


def _neutral(n=4000, level=180.0, seed=0):
    rng = np.random.default_rng(seed)
    base = level + rng.normal(0.0, 6.0, size=(n, 3))
    return np.clip(base, 1.0, 255.0)


def test_removes_a_known_cast():
    cast = np.array([0.72, 1.0, 0.65])
    cast = cast / cast[1]
    rgb = _neutral() * cast[None, :]
    before = scan_color.neutrality(rgb)
    assert before["max_cast"] > 0.2
    wb = scan_color.estimate_white_balance(rgb)
    after = scan_color.neutrality(scan_color.apply_white_balance(rgb, wb))
    assert after["max_cast"] < 0.03, after
    assert len(wb.gains) == 3


def test_neutral_input_is_left_alone():
    rgb = _neutral(seed=3)
    wb = scan_color.estimate_white_balance(rgb)
    assert all(abs(g - 1.0) < 0.12 for g in wb.gains), wb.gains
    out = scan_color.apply_white_balance(rgb, wb)
    assert scan_color.neutrality(out)["max_cast"] < 0.05


def test_gains_are_bounded_and_output_does_not_wrap():
    rgb = _neutral() * np.array([0.2, 1.0, 3.0])
    wb = scan_color.estimate_white_balance(rgb)
    assert all(scan_color.MIN_GAIN <= g <= scan_color.MAX_GAIN for g in wb.gains)
    out = scan_color.apply_white_balance(rgb, wb)
    assert out.min() >= 0.0 and out.max() <= 255.0
    assert np.all(np.isfinite(out))


def test_normalised_input_stays_normalised():
    rgb = _neutral() / 255.0
    wb = scan_color.estimate_white_balance(rgb)
    out = scan_color.apply_white_balance(rgb, wb)
    assert out.max() <= 1.0 + 1e-9


def test_too_few_samples_is_refused():
    with pytest.raises(ValueError):
        scan_color.estimate_white_balance(np.zeros((10, 3)))


def test_real_scan_records_the_white_balance(tmp_path):
    """A colour-capable profile scan must record gains and neutrality."""
    import os
    import subprocess
    sweep = Path(os.path.expanduser("~/einstar-private/sweep-b/frames"))
    profile = Path(os.path.expanduser(
        "~/einstar-private/einstar-profiles/colour-fitted-e10.json"))
    if not (sweep.is_dir() and profile.is_file()):
        pytest.skip("colour-capable recording or profile not available")
    python = REPO / ".venv" / "bin" / "python"
    out = tmp_path / "wb"
    env = dict(os.environ)
    env["OMP_NUM_THREADS"] = "2"
    proc = subprocess.run(
        [str(python), "-m", "tools.scan.cli", "--frames", str(sweep),
         "--profile", str(profile), "--out", str(out), "--count", "3",
         "--scale", "0.25"],
        cwd=str(REPO), env=env, capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, proc.stderr[-800:]
    report = json.loads((out / "scan_report.json").read_text())
    block = report.get("colour")
    assert block and "white_balance" in block, block
    gains = block["white_balance"]["gains"]
    assert len(gains) == 3 and all(0.25 <= g <= 4.0 for g in gains)
    before = block["neutrality_before"]["max_cast"]
    after = block["neutrality_after"]["max_cast"]
    assert after < before, (before, after)
    assert block["clipped_fraction"] < 0.05, block["clipped_fraction"]
