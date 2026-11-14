"""The acquisition worker's newest-frame policy and decision-thread budget.

These tests drive the *real* ``tools.wizard.gui.worker.SessionWorker`` with a
fake controller, so they measure the scheduling behaviour and the instrumentation
without a scanner, a Qt widget or a factory profile.

Qt is initialised in a child process: this suite's GUI tests document that
PySide6 does not survive being initialised inside the same interpreter as the
rest of the suite (``tests/test_wizard_gui.py``), and driving a real event loop
inside pytest is exactly the hazard that warning is about.  The child prints one
JSON object; every assertion is made here on measured numbers.
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
if not PYTHON.is_file():  # pragma: no cover - fall back to the running one
    PYTHON = Path(sys.executable)

pytest.importorskip("PySide6")

#: the child script: fake source/controller, real SessionWorker, JSON on stdout
SCRIPT = r'''
import json, sys, time
sys.path.insert(0, {repo!r})
sys.path.insert(0, {tools!r})
from PySide6 import QtCore
from tools.wizard.gui import worker as worker_mod

class Group:
    __slots__ = ("index", "device_group", "timestamp_s", "images",
                 "colour_bgr", "complete", "source")
    def __init__(self, index):
        self.index = index
        self.device_group = index
        self.timestamp_s = index * 0.1
        self.images = {{}}
        self.colour_bgr = None
        self.complete = True
        self.source = "fake"

class Source:
    kind = "replay"
    continuous = True
    def __init__(self, count, interval_s):
        self.count = count
        self.interval_s = interval_s
        self.final_verdict = None
    def __iter__(self):
        next_at = time.monotonic()
        for index in range(self.count):
            yield Group(index)
            if self.interval_s:
                next_at += self.interval_s
                delay = next_at - time.monotonic()
                if delay > 0:
                    time.sleep(delay)

class Controller:
    """A decision costs a fixed time and records what it was given."""
    _last_colour_bgr = None
    state = "recording"
    def __init__(self, decision_s, fail=False):
        self.decision_s = decision_s
        self.fail = fail
        self.decided = []
        self.recorded = []
        self.started = []
    def ingest(self, group):
        self.started.append(time.monotonic())
        time.sleep(self.decision_s)
        if self.fail:
            raise RuntimeError("decision failed")
        self.decided.append(group.index)
        return {{"index": group.index, "accepted": True, "reasons": []}}
    def record_only(self, group):
        self.recorded.append(group.index)
        return {{"recorded": True}}

spec = json.loads({spec!r})
app = QtCore.QCoreApplication.instance() or QtCore.QCoreApplication([])

if spec.get("scenario") == "runner_pending_drop":
    # deterministic: a group is handed over and the stop arrives before the
    # decision thread ever picks it up
    controller = Controller(spec["decision_s"])
    runner = worker_mod.DecisionRunner(controller)
    accepted = runner.submit(Group(0))
    runner.request_stop(drain=False)
    runner.start()
    exited = runner.wait(spec.get("wait_ms", 5000))
    print("@@" + json.dumps({{
        "accepted": bool(accepted),
        "exited": bool(exited),
        "stats": runner.stats(),
        "decided": len(controller.decided),
    }}))
    raise SystemExit(0)

if spec.get("scenario") == "runner_wakes_immediately":
    # the runner must not sit in a 50 ms poll before noticing the first group
    controller = Controller(spec["decision_s"])
    runner = worker_mod.DecisionRunner(controller)
    runner.start()
    time.sleep(0.2)                      # let it reach its wait
    handed = time.monotonic()
    runner.submit(Group(0))
    while not controller.decided and time.monotonic() - handed < 2.0:
        app.processEvents()
        time.sleep(0.001)
    latency_ms = (time.monotonic() - handed) * 1000.0
    runner.request_stop()
    runner.wait(5000)
    print("@@" + json.dumps({{"handoff_latency_ms": round(latency_ms, 3),
                            "decided": len(controller.decided)}}))
    raise SystemExit(0)

source = Source(spec["count"], spec.get("interval_s", 0.0))
controller = Controller(spec["decision_s"], spec.get("fail", False))
w = worker_mod.SessionWorker(
    controller, source, pace_hz=spec.get("pace_hz", 0.0),
    max_groups=spec.get("max_groups", 0),
    async_decisions=spec.get("async_decisions", True),
    min_decision_interval_s=spec.get("min_decision_interval_s", 0.0))
errors = []
w.error.connect(lambda message: errors.append(str(message)))
w.start()

deadline = time.monotonic() + spec.get("timeout_s", 30.0)
paused = False
stop_at = spec.get("stop_after_s")
stop_sent = False
started_at = time.monotonic()
while time.monotonic() < deadline:
    app.processEvents()
    if w.isRunning() is False:
        break
    elapsed = time.monotonic() - started_at
    if spec.get("pause_after_s") is not None and not paused \
            and elapsed > spec["pause_after_s"]:
        w.request_pause()
        paused = True
        paused_at = elapsed
    if spec.get("resume_after_s") is not None and paused \
            and elapsed > spec["resume_after_s"]:
        w.request_resume()
    if stop_at is not None and not stop_sent and elapsed > stop_at:
        w.request_stop()
        stop_sent = True
        stop_wall = time.monotonic()
    time.sleep(0.002)
app.processEvents()
stopped = False
if w.isRunning():
    w.request_stop()
    stopped = not w.wait(spec.get("wait_ms", 20000))
result = {{
    "metrics": w.metrics(),
    "errors": errors,
    "decided": len(controller.decided),
    "recorded": len(controller.recorded),
    "running_after": bool(w.isRunning()),
    "stop_wait_failed": stopped,
    "pause_to_stop_s": (None if not stop_sent or not paused
                        else round(stop_wall - started_at - paused_at, 3)),
    "exit_after_stop_s": (None if not stop_sent
                          else round(time.monotonic() - stop_wall, 3)),
}}
print("@@" + json.dumps(result))
'''


def _run_qt(spec: dict, timeout: int = 120) -> dict:
    """Run the worker scenario in a child process and return its measurements."""
    script = SCRIPT.format(repo=str(REPO), tools=str(REPO / "tools"),
                           spec=json.dumps(spec))
    env = dict(os.environ)
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    env.pop("DISPLAY", None)
    env.pop("WAYLAND_DISPLAY", None)
    proc = subprocess.run([str(PYTHON), "-c", script], cwd=str(REPO), env=env,
                          capture_output=True, text=True, timeout=timeout)
    assert proc.returncode == 0, (
        f"worker scenario failed: rc={proc.returncode}\n"
        f"stdout={proc.stdout[-3000:]}\nstderr={proc.stderr[-3000:]}")
    for line in reversed(proc.stdout.splitlines()):
        if line.startswith("@@"):
            return json.loads(line[2:])
    raise AssertionError(f"no measurement line in output:\n{proc.stdout[-3000:]}")


# ---------------------------------------------------------------------------
# the newest-frame policy
# ---------------------------------------------------------------------------

def test_newest_frame_policy_is_explicit_and_measured():
    """A 100 Hz offer with a 150 ms decision must *state* where the frames go."""
    out = _run_qt({"count": 40, "interval_s": 0.01, "pace_hz": 100.0,
                   "decision_s": 0.15, "timeout_s": 20})
    metrics = out["metrics"]
    policy = metrics["newest_frame_policy"]
    assert out["errors"] == []
    assert policy["queue_depth"] == 1
    assert policy["drop_when_busy"] is True
    # the stream offered far more than the decision thread could take
    assert policy["groups_seen"] >= 20
    assert metrics["decision_total"] >= 2
    assert metrics["decision_total"] < policy["groups_seen"]
    assert policy["decided"] == metrics["decision_total"]
    assert policy["dropped"] == policy["groups_seen"] - policy["decided"]
    assert 0.0 < policy["dropped_fraction"] < 1.0
    # every dropped group was recorded, not silently lost
    assert out["recorded"] + metrics["decision_total"] >= policy["groups_seen"] - 1
    # the decision thread reports its own cost, not just the cadence
    duration = metrics["decision_duration"]
    assert duration["count"] == metrics["decision_total"]
    assert 130.0 <= duration["p50_ms"] <= 400.0
    assert duration["max_ms"] >= duration["p50_ms"]
    # and the overload verdict is derived from those numbers
    overload = metrics["overload"]
    assert overload["overloaded"] is True
    assert overload["sustainable_hz"] is not None
    assert overload["sustainable_hz"] < 100.0
    assert metrics["decision_thread"]["max_queue_depth"] == 1
    assert metrics["decision_thread"]["busy_fraction"] > 0.2


def test_lag_measures_the_submission_that_produced_the_decision():
    """The lag can never be shorter than the decision it waited for.

    The previous instrumentation stored the submit time on the consumer side, so
    a submit that happened while the decision signal was queued overwrote it and
    the reported lag belonged to a later submission (fresh-01 lost 26 of 866
    lags and understated the rest).
    """
    out = _run_qt({"count": 24, "interval_s": 0.02, "pace_hz": 50.0,
                   "decision_s": 0.1, "timeout_s": 20})
    metrics = out["metrics"]
    durations = metrics["decision_duration"]
    lag = metrics["decision_lag"]
    assert durations["count"] >= 2
    assert lag["count"] >= 1
    # lag = queue wait + decision, so it can never be shorter than the decision
    assert lag["max_ms"] >= durations["p50_ms"] * 0.95, (lag, durations)


# ---------------------------------------------------------------------------
# the preview path
# ---------------------------------------------------------------------------

def test_preview_never_waits_for_a_decision():
    """The display keeps the device cadence while a slow decision runs."""
    out = _run_qt({"count": 60, "interval_s": 0.02, "pace_hz": 50.0,
                   "decision_s": 0.5, "timeout_s": 25})
    metrics = out["metrics"]
    preview = metrics["preview"]
    assert metrics["preview_total"] >= 20
    # the decision takes 500 ms; a preview that waited for it could not show a
    # gap below that
    assert preview["max_ms"] < 200.0, preview
    assert preview["p50_ms"] < 60.0, preview
    # and the frames shown *while* a decision was running are counted
    assert metrics["preview_during_decision"] >= 3
    assert metrics["preview_total"] > metrics["decision_total"]


def test_preview_gap_is_reported_as_an_overload_event():
    """A stalled reader shows up as an explicit event, not a silent gap."""
    out = _run_qt({"count": 30, "interval_s": 0.25, "pace_hz": 4.0,
                   "decision_s": 0.05, "timeout_s": 25})
    metrics = out["metrics"]
    # a 250 ms offer against a 250 ms period is not an overload
    assert metrics["preview_gap_max_ms"] is not None
    assert metrics["preview_gap_max_ms"] >= 200.0
    assert "overload_events" in metrics


# ---------------------------------------------------------------------------
# pause / stop semantics
# ---------------------------------------------------------------------------

def test_pause_stops_spending_decisions():
    """Pause must stop the decision thread, not only the reader."""
    out = _run_qt({"count": 200, "interval_s": 0.01, "pace_hz": 100.0,
                   "decision_s": 0.05, "pause_after_s": 0.4,
                   "timeout_s": 12})
    metrics = out["metrics"]
    policy = metrics["newest_frame_policy"]
    assert metrics["dropped_paused"] > 0
    assert policy["drop_when_paused"] is True
    # the reader was paused too: the offer stopped, so the run ended by timeout
    assert out["errors"] == []


def test_user_stop_does_not_start_new_work():
    """A stop while a long decision runs must not queue another one behind it."""
    out = _run_qt({"count": 200, "interval_s": 0.005, "pace_hz": 200.0,
                   "decision_s": 1.2, "stop_after_s": 0.3,
                   "timeout_s": 20, "wait_ms": 15000})
    metrics = out["metrics"]
    thread = metrics["decision_thread"]
    assert out["running_after"] is False
    assert out["stop_wait_failed"] is False
    # the in-flight decision finished, nothing else was started
    assert metrics["decision_total"] <= 2
    # every group offered during the 1.2 s decision was refused, not queued
    assert thread["refused_busy"] >= 1
    assert out["recorded"] >= 1
    # prompt: the stop did not wait for a second 1.2 s decision
    assert out["exit_after_stop_s"] < 2.5


def test_pending_group_is_dropped_by_a_prompt_stop():
    """A group handed over just before a stop is not decided (and is counted)."""
    out = _run_qt({"scenario": "runner_pending_drop", "decision_s": 0.5,
                   "wait_ms": 5000})
    assert out["accepted"] is True
    assert out["exited"] is True
    assert out["decided"] == 0
    assert out["stats"]["dropped_on_stop"] == 1


def test_runner_wakes_on_the_handoff_not_on_a_poll_interval():
    """The old 50 ms poll added up to 50 ms to every handoff and to a stop."""
    out = _run_qt({"scenario": "runner_wakes_immediately", "decision_s": 0.01})
    assert out["decided"] == 1
    assert out["handoff_latency_ms"] < 25.0, out


def test_errors_are_surfaced_and_the_loop_keeps_going():
    out = _run_qt({"count": 12, "interval_s": 0.02, "pace_hz": 50.0,
                   "decision_s": 0.01, "fail": True, "timeout_s": 20})
    metrics = out["metrics"]
    assert out["errors"], "a failing decision must be reported to the UI"
    assert "RuntimeError" in out["errors"][0]
    assert metrics["decision_duration"]["count"] >= 1


# ---------------------------------------------------------------------------
# the synchronous (replay/headless) path
# ---------------------------------------------------------------------------

def test_sync_path_records_decision_durations():
    out = _run_qt({"count": 4, "interval_s": 0.0, "decision_s": 0.05,
                   "async_decisions": False, "timeout_s": 20})
    metrics = out["metrics"]
    assert metrics["decision_total"] == 4
    duration = metrics["decision_duration"]
    assert duration["count"] == 4
    assert 20.0 <= duration["p50_ms"] <= 500.0
    assert metrics["overload"]["offer_hz"] is None
    assert metrics["newest_frame_policy"]["queue_depth"] == 1
