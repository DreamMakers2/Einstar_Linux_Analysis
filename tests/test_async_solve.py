"""The periodic rig solve must not block the decision path.

Measured on the real fresh-01 observations, one solve costs 2.7 s at 40 views
and 12.4 s at 84.  Running it inside ``ingest`` produced a decision p95 of
1.2 s, a 15.4 s maximum, and 81 % of captured groups never decided - which in
turn starved stationary-hold evidence.  These tests pin the worker contract:
``ingest`` returns with ``solved=False``, the result still appears, and a final
``finish`` is unaffected.
"""
from __future__ import annotations

import time
from pathlib import Path

from tools.wizard import controller, sources, types


def _fixture(tmp_path: Path, *, n_views: int = 12, pattern: str = "distance"):
    """A synthetic stream delivered directly (a still/independent-group source).

    ``ReplayDirSource`` declares a continuous cadence, so the synthetic poses
    (which jump tens of millimetres between neighbouring views) trip the motion
    gate; the direct source marks groups independent, which is what these tests
    need in order to reach several accepted views quickly.
    """
    spec = sources.SyntheticSpec(n_views=n_views, seed=7, pattern=pattern)
    synthetic = sources.SyntheticSource(spec)
    groups = list(synthetic)
    factory = tmp_path / "factory.json"
    types.atomic_write_json(factory, synthetic.profile_dict())
    return groups, synthetic, factory


def _wait_for(predicate, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_async_solve_installs_a_result_without_blocking_ingest(tmp_path):
    groups, synthetic, factory = _fixture(tmp_path, n_views=10)
    ctl = controller.WizardController(
        factory_profile=factory, session_dir=tmp_path / "session",
        mode="quick", source=synthetic.info, ring_keep=4, async_solve=True)
    ctl.begin()
    assert ctl.async_solve is True
    timings = []
    solved_flags = []
    for group in groups:
        started = time.perf_counter()
        decision = ctl.ingest(group)
        timings.append(time.perf_counter() - started)
        solved_flags.append(bool(getattr(decision, "solved", False)))
    assert len(ctl.session.accepted) >= 6

    # The decision path never ran the optimiser inline...
    assert not any(solved_flags)
    # ...but the background worker did install a calibration.
    assert _wait_for(lambda: ctl.session.result is not None)
    assert ctl.solve_stats["installed"] >= 1
    assert ctl.solve_stats["errors"] == 0
    assert ctl.solve_stats["last_views"] > 0
    # Ingestion never blocks on the optimiser.  The absolute wall time still
    # depends on detection and on machine load, so the contract under test is
    # that the decision path never reports an inline solve; per-stage timings
    # live in tools/wizard/bench_pipeline.py.
    assert max(timings) < 30.0, max(timings)

    ctl.finish()
    assert ctl.state == "done"
    assert ctl._solve_thread is None
    ctl.cancel()


def test_synchronous_mode_still_solves_inline(tmp_path):
    groups, synthetic, factory = _fixture(tmp_path, n_views=8)
    ctl = controller.WizardController(
        factory_profile=factory, session_dir=tmp_path / "session",
        mode="quick", source=synthetic.info, ring_keep=4, async_solve=False)
    ctl.begin()
    decisions = [ctl.ingest(group) for group in groups]
    assert ctl.session.result is not None
    assert any(bool(d.solved) for d in decisions) or ctl.solve_stats["started"] == 0
    ctl.cancel()
