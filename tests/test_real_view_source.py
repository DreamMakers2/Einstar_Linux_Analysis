"""The recorded-view replay source used for real-image GUI rehearsals.

Skipped unless the private fresh-01 session is present; it never copies images
into the repository and never writes to the session directory.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from tools.wizard import types

FRESH = Path.home() / "einstar-calibration-fresh-01"


def _available() -> bool:
    return (FRESH / "views.jsonl").is_file() and (FRESH / "views").is_dir()


pytestmark = pytest.mark.skipif(not _available(), reason="fresh-01 not present")


def test_source_turns_recorded_views_into_a_cadenced_stream():
    from tools.wizard.real_view_source import RealViewSource

    source = RealViewSource(FRESH, select=12, holds=3, hold_repeats=4,
                           extra_frames=(4631,), frames_dir=FRESH / "frames")
    groups = list(source)
    # selection is refined by the measured coverage gate, so it may add
    # views beyond the requested seed count
    assert len(source.selected) >= 12
    assert source.info.continuous is True
    assert len(groups) > 12
    # timestamps are strictly increasing and one domain
    stamps = [g.timestamp_s for g in groups]
    assert all(b > a for a, b in zip(stamps, stamps[1:]))
    # hold frames repeat one recorded pose at ~10 Hz
    hold = [g for g in groups if "(hold" in g.label]
    assert hold
    assert all(0.05 <= b.timestamp_s - a.timestamp_s <= 0.2
               for a, b in zip(hold, hold[1:])
               if a.label.split(" (")[0] == b.label.split(" (")[0])
    assert all(g.images["left"].shape == (types.IMAGE_HEIGHT, types.IMAGE_WIDTH)
               for g in groups)
    assert all(g.continuous for g in groups)
    source.close()


def test_source_never_writes_to_the_session():
    from tools.wizard.real_view_source import RealViewSource

    before = sorted(p.name for p in FRESH.iterdir())
    RealViewSource(FRESH, select=3, holds=1, hold_repeats=1)
    after = sorted(p.name for p in FRESH.iterdir())
    assert before == after
