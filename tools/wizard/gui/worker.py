"""Background worker: pulls FrameGroups from a source and feeds the controller.

The GUI thread never touches the source or the controller's ingest path; it only
reads ``latest`` (a small, lock-protected snapshot) and asks the controller for a
``snapshot()`` at display rate.
"""
from __future__ import annotations

import threading
import time

import numpy as np
from PySide6 import QtCore


class DecisionRunner(QtCore.QThread):
    """Runs ``controller.ingest`` on a thread of its own.

    Acquisition must never wait for a decision: a decision costs ~0.16 s and the
    camera offers a group every 0.1 s, so deciding inline makes the loop fall
    behind the stream no matter how the pacing is arranged (measured: 6.8 of 10
    groups/s).  The queue holds at most one pending group - a calibration frame
    that is two frames stale is useless, and a longer queue would only delay the
    guidance.
    """

    decision = QtCore.Signal(dict)
    error = QtCore.Signal(str)
    busy = False

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        self._lock = threading.Lock()
        self._pending = None
        self._stop = threading.Event()
        self._wake = threading.Event()

    def submit(self, group) -> bool:
        """Hand a group to the runner; False when it is already busy."""
        with self._lock:
            if self.busy:
                return False
            self.busy = True
            self._pending = group
        self._wake.set()
        return True

    def request_stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def run(self) -> None:  # noqa: D401
        while not self._stop.is_set():
            self._wake.wait(0.05)
            self._wake.clear()
            with self._lock:
                group, self._pending = self._pending, None
            if group is None:
                continue
            try:
                decision = self.controller.ingest(group)
                payload = (decision.as_dict() if hasattr(decision, "as_dict")
                           else dict(decision))
                self.decision.emit(payload)
            except Exception as exc:  # pragma: no cover - surfaced to the UI
                self.error.emit(f"{type(exc).__name__}: {exc}")
            finally:
                with self._lock:
                    self.busy = False


class FinishWorker(QtCore.QThread):
    """Runs the final solve and the validation gates off the GUI thread.

    ``controller.finish()`` is the heaviest single operation in the wizard:
    measured 38.3 s at 35 accepted views (10 solver passes, one of which is a
    22.6 s identifiability analysis).  Run inline it froze the window for that
    whole time with no repaint and no feedback, which is indistinguishable from
    a crash.  This thread owns it; the window keeps painting and shows elapsed
    time.
    """

    done = QtCore.Signal(dict)
    failed = QtCore.Signal(str)
    #: emitted once per second so the UI can show that work is still happening
    progress = QtCore.Signal(float)

    def __init__(self, controller, session_worker=None, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.session_worker = session_worker
        self._stop = threading.Event()

    def run(self) -> None:  # noqa: D401
        started = time.monotonic()
        try:
            worker = self.session_worker
            if worker is not None and worker.isRunning():
                # stopping the ingest worker must not block the GUI thread
                worker.request_stop()
                while worker.isRunning() and not self._stop.is_set():
                    if time.monotonic() - started > 30.0:
                        break
                    self.progress.emit(time.monotonic() - started)
                    time.sleep(0.1)
            try:
                result = self.controller.finish()
            except Exception as exc:            # surface, never crash the UI
                self.failed.emit(f"{type(exc).__name__}: {exc}")
                return
            self.done.emit(dict(result or {}))
        finally:
            pass

    def request_stop(self) -> None:
        self._stop.set()


class SessionWorker(QtCore.QThread):
    """Runs the capture loop off the UI thread."""

    decision = QtCore.Signal(dict)
    error = QtCore.Signal(str)
    finished_ok = QtCore.Signal(dict)

    def __init__(self, controller, source, *, pace_hz: float = 0.0,
                 max_groups: int = 0, parent=None,
                 min_decision_interval_s: float = 0.0):
        super().__init__(parent)
        self.controller = controller
        self.source = source
        self.pace_hz = float(pace_hz)
        self.max_groups = int(max_groups)
        #: adaptive spacing between full decisions.  0 analyses every group.
        #: The record-only path keeps every frame on disk either way.
        self.min_decision_interval_s = float(min_decision_interval_s)
        self._last_decision_at: float | None = None
        self._last_ingest_s = 0.0
        self.recorded_only = 0
        self.groups_seen = 0
        #: created here (not inside run(), where it would be parented across
        #: threads and Qt warns), and started by run() when adaptive spacing is on
        self.runner: DecisionRunner | None = None
        if self.min_decision_interval_s > 0:
            self.runner = DecisionRunner(controller)
            self.runner.decision.connect(self.decision)
            self.runner.error.connect(self.error)
        self._stop = threading.Event()
        self._pause = threading.Event()
        self._lock = threading.Lock()
        self._latest: dict | None = None
        self._count = 0

    # -- control -------------------------------------------------------
    def request_stop(self) -> None:
        self._stop.set()
        self._pause.clear()

    def request_pause(self) -> None:
        self._pause.set()

    def request_resume(self) -> None:
        self._pause.clear()

    @property
    def paused(self) -> bool:
        return self._pause.is_set()

    @property
    def latest(self) -> dict | None:
        with self._lock:
            return self._latest

    # -- loop ----------------------------------------------------------
    def run(self) -> None:  # noqa: D401
        period = 1.0 / self.pace_hz if self.pace_hz > 0 else 0.0
        next_at = time.monotonic()
        def pace() -> None:
            """Sleep the *remainder* of one stream period for this iteration.

            An absolute schedule silently loses catch-up time whenever a
            decision overruns the period (measured: the loop settled at 7.3 of
            10 groups/s even though it was only ~50 % busy).  Sleeping
            ``period - elapsed`` keeps the average at the stream rate whenever
            the average work fits, and never sleeps more than the period.
            """
            if not period:
                return
            delay = period - (time.monotonic() - iteration_started)
            if delay > 0:
                time.sleep(delay)

        try:
            if self.runner is not None:
                self.runner.start()
            for group in self.source:
                if self._stop.is_set():
                    break
                while self._pause.is_set() and not self._stop.is_set():
                    time.sleep(0.02)
                if self._stop.is_set():
                    break
                self.groups_seen += 1
                iteration_started = time.monotonic()
                now = iteration_started
                # adaptive scheduling on measured cost, not on stream time: the
                # decision interval is the time one decision actually takes, so
                # the loop spends at most ~1/1.25 of its time deciding and always
                # remains able to consume every arriving group.
                # opt-in: with the default 0 the loop decides every group
                # exactly as before.  Enabling it trades decision rate for
                # keeping up with the stream, which changes *which* frames are
                # analysed - so it must never switch itself on.
                interval = (max(self.min_decision_interval_s,
                                1.25 * self._last_ingest_s)
                            if self.min_decision_interval_s > 0 else 0.0)
                if (interval > 0.0 and self._last_decision_at is not None
                        and now - self._last_decision_at < interval):
                    self.controller.record_only(group)
                    self.recorded_only += 1
                    pace()
                    continue
                if self.runner is not None:
                    # non-blocking: acquisition keeps its cadence, the decision
                    # runs on its own thread and reports when it is done
                    if not self.runner.submit(group):
                        self.controller.record_only(group)
                        self.recorded_only += 1
                    else:
                        self._last_decision_at = time.monotonic()
                    self._count += 1
                    pace()
                    continue
                started = time.monotonic()
                decision = self.controller.ingest(group)
                self._last_ingest_s = time.monotonic() - started
                self._last_decision_at = time.monotonic()
                payload = decision.as_dict() if hasattr(decision, "as_dict") else dict(decision)
                with self._lock:
                    self._latest = {
                        "index": group.index,
                        "device_group": group.device_group,
                        "timestamp_s": group.timestamp_s,
                        "images": dict(group.images),
                        "colour_bgr": (getattr(group, "colour_bgr", None)
                                       or getattr(self.controller, "_last_colour_bgr", None)),
                        "complete": group.complete,
                        "source": group.source,
                        "decision": payload,
                    }
                self.decision.emit(payload)
                self._count += 1
                if self.max_groups and self._count >= self.max_groups:
                    break
                pace()
                if time.monotonic() - started > 5.0 and not period:
                    # never let an unexpected stall look like a hang
                    self.error.emit("ingest took > 5 s for one group")
        except Exception as exc:  # pragma: no cover - surface to the UI
            self.error.emit(f"{type(exc).__name__}: {exc}")
        finally:
            if self.runner is not None:
                self.runner.request_stop()
                self.runner.wait(15000)
                self.runner = None
            self.finished_ok.emit({"groups_ingested": self._count,
                                   "stopped": self._stop.is_set()})
