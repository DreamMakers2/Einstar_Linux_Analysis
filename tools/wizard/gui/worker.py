"""Background worker: pulls FrameGroups from a source and feeds the controller.

The GUI thread never touches the source or the controller's ingest path; it only
reads ``latest`` (a small, lock-protected snapshot) and asks the controller for a
``snapshot()`` at display rate.
"""
from __future__ import annotations

import queue
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
        while True:
            with self._lock:
                group, self._pending = self._pending, None
            if group is None:
                if self._stop.is_set():
                    break
                self._wake.wait(0.05)
                self._wake.clear()
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
    #: newest complete group, emitted *before* any calibration work touches it.
    #: The display reads this; it never waits for a decision.
    preview = QtCore.Signal(dict)
    error = QtCore.Signal(str)
    finished_ok = QtCore.Signal(dict)

    def __init__(self, controller, source, *, pace_hz: float = 0.0,
                 max_groups: int = 0, parent=None,
                 min_decision_interval_s: float = 0.0,
                 async_decisions: bool | None = None):
        super().__init__(parent)
        self.controller = controller
        self.source = source
        self.pace_hz = float(pace_hz)
        self.max_groups = int(max_groups)
        #: adaptive spacing between full decisions.  0 analyses every group.
        #: The record-only path keeps every frame on disk either way.
        self.min_decision_interval_s = float(min_decision_interval_s)
        #: Live acquisition must never wait for detection.  Replay/headless
        #: keeps the deterministic in-order path unless the caller opts in.
        kind = str(getattr(source, "kind", "") or "").lower()
        self.async_decisions = (str(kind).endswith("live")
                                if async_decisions is None
                                else bool(async_decisions))
        self._last_decision_at: float | None = None
        self._last_ingest_s = 0.0
        self.recorded_only = 0
        self.groups_seen = 0
        #: created here (not inside run(), where it would be parented across
        #: threads and Qt warns), and started by run() when adaptive spacing is on
        self.runner: DecisionRunner | None = None
        if self.async_decisions or self.min_decision_interval_s > 0:
            self.runner = DecisionRunner(controller)
            self.runner.decision.connect(self._on_runner_decision)
            self.runner.error.connect(self.error)
        self._stop = threading.Event()
        self._pause = threading.Event()
        self._lock = threading.Lock()
        self._latest: dict | None = None
        self._count = 0
        self._preview_times: list[float] = []
        self._decision_times: list[float] = []
        self.preview_count = 0
        self.decision_count = 0
        #: accounting that makes the newest-frame policy explicit to the
        #: operator instead of looking like an unexplained 2,004/13,195 gap.
        self.preview_total = 0
        self.decision_total = 0
        self.superseded = 0
        self._decision_lags: list[float] = []
        self._submit_at: float | None = None
        self.end_acquisition: dict = {}
        self.end_info: dict = {}

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

    def _on_runner_decision(self, payload: dict) -> None:
        now = time.monotonic()
        self.decision_count += 1
        self.decision_total += 1
        self._decision_times.append(now)
        if self._submit_at is not None:
            self._decision_lags.append((now - self._submit_at) * 1000.0)
            del self._decision_lags[:-2000]
            self._submit_at = None
        if len(self._decision_times) > 2000:
            del self._decision_times[:1000]
        with self._lock:
            if self._latest is not None:
                self._latest = dict(self._latest, decision=payload)
        self.decision.emit(payload)

    def metrics(self) -> dict:
        """Cadence measurements for the session's evidence file."""
        def summary(times: list[float]) -> dict:
            if len(times) < 2:
                return {"count": len(times), "hz": None, "p50_ms": None,
                        "p95_ms": None, "max_ms": None}
            gaps = sorted((b - a) * 1000.0 for a, b in zip(times, times[1:])
                          if b > a)
            if not gaps:
                return {"count": len(times), "hz": None, "p50_ms": None,
                        "p95_ms": None, "max_ms": None}
            mean = sum(gaps) / len(gaps)
            return {
                "count": len(times),
                "hz": (1000.0 / mean) if mean > 0 else None,
                "p50_ms": gaps[len(gaps) // 2],
                "p95_ms": gaps[min(len(gaps) - 1, int(0.95 * len(gaps)))],
                "max_ms": gaps[-1],
            }
        lags = sorted(self._decision_lags)
        lag = {"count": len(lags),
               "p50_ms": (lags[len(lags) // 2] if lags else None),
               "p95_ms": (lags[min(len(lags) - 1, int(0.95 * len(lags)))]
                          if lags else None),
               "max_ms": (lags[-1] if lags else None)}
        return {"preview": summary(self._preview_times),
                "decision": summary(self._decision_times),
                "decision_lag": lag,
                "groups_seen": int(self.groups_seen),
                "preview_total": int(self.preview_total),
                "decision_total": int(self.decision_total),
                "superseded": int(self.superseded),
                "recorded_only": int(self.recorded_only),
                "async_decisions": bool(self.async_decisions)}

    # -- loop ----------------------------------------------------------
    def run(self) -> None:  # noqa: D401
        if self.async_decisions:
            self._run_split()
            return
        self._run_sync()

    def _publish_preview(self, group) -> None:
        payload_preview = {
            "index": group.index,
            "device_group": group.device_group,
            "timestamp_s": group.timestamp_s,
            "images": dict(group.images),
            "colour_bgr": (group.colour_bgr
                           if getattr(group, "colour_bgr", None) is not None
                           else getattr(self.controller, "_last_colour_bgr", None)),
            "complete": group.complete,
            "source": group.source,
            "decision": None,
        }
        with self._lock:
            self._latest = payload_preview
        self.preview_count += 1
        self.preview_total += 1
        self._preview_times.append(time.monotonic())
        if len(self._preview_times) > 2000:
            del self._preview_times[:1000]
        self.preview.emit(payload_preview)

    def _run_split(self) -> None:
        """Live architecture: reader thread + newest-frame queue + decision.

        The reader owns the source cadence and preview.  The consumer only
        takes the newest complete group; a slower decision never holds an
        older frame and never blocks the reader.  This is what keeps preview
        at the device rate while detection/optimisation run independently.

        End-of-stream is signalled by ``reader_done``, not by a sentinel in the
        queue: a sentinel inserted into a full one-slot queue would evict the
        last real frame (confirmed in review).  The consumer drains the queue
        after the reader finishes, then stops the decision worker; the decision
        worker itself drains one already-submitted frame before exiting.
        """
        q: queue.Queue = queue.Queue(maxsize=1)
        self._decision_queue = q
        period = 1.0 / self.pace_hz if self.pace_hz > 0 else 0.0
        reader_done = threading.Event()
        reader_error: list[str] = []

        def reader() -> None:
            next_at = time.monotonic()
            try:
                for group in self.source:
                    if self._stop.is_set():
                        break
                    while self._pause.is_set() and not self._stop.is_set():
                        time.sleep(0.02)
                    if self._stop.is_set():
                        break
                    self.groups_seen += 1
                    self._publish_preview(group)
                    if period:
                        next_at += period
                        delay = next_at - time.monotonic()
                        if delay > 0:
                            time.sleep(delay)
                    try:
                        q.put_nowait(group)
                    except queue.Full:
                        # One newest frame is all a live calibration can use.
                        # The older frame remains in the raw recording.
                        try:
                            q.get_nowait()
                        except queue.Empty:
                            pass
                        try:
                            q.put_nowait(group)
                        except queue.Full:
                            self.superseded += 1
                            self.recorded_only += 1
                            try:
                                self.controller.record_only(group)
                            except Exception:
                                pass
            except Exception as exc:  # pragma: no cover
                reader_error.append(f"{type(exc).__name__}: {exc}")
            finally:
                reader_done.set()

        reader_thread = threading.Thread(target=reader, daemon=True,
                                         name="einstar-reader")
        reader_thread.start()
        if self.runner is not None:
            self.runner.start()
        try:
            while not self._stop.is_set():
                try:
                    group = q.get(timeout=0.1)
                except queue.Empty:
                    if reader_done.is_set():
                        break
                    continue
                if self.runner is not None:
                    if not self.runner.submit(group):
                        self.controller.record_only(group)
                        self.recorded_only += 1
                        self.superseded += 1
                    else:
                        self._submit_at = time.monotonic()
                        self._last_decision_at = self._submit_at
                        self._count += 1
                        if self.max_groups and self._count >= self.max_groups:
                            break
                else:  # pragma: no cover - async always has a runner here
                    decision = self.controller.ingest(group)
                    payload = (decision.as_dict() if hasattr(decision, "as_dict")
                               else dict(decision))
                    self._on_runner_decision(payload)
                    self._count += 1
        finally:
            self._stop.set()
            # Interrupt a source blocked in iteration, then wait for the reader
            # to actually exit.  Do not report completion while it is alive.
            reader_thread.join(timeout=5.0)
            if reader_thread.is_alive():
                try:
                    close = getattr(self.source, "close", None)
                    if callable(close):
                        close()
                except Exception:
                    pass
                reader_thread.join(timeout=5.0)
            reader_alive = reader_thread.is_alive()
            if reader_error:
                self.error.emit("acquisition reader: " + reader_error[0])
            if reader_alive:
                self.error.emit("acquisition reader did not stop; "
                                "session completion is not claimed")
            runner_alive = False
            if self.runner is not None:
                self.runner.request_stop()
                if not self.runner.wait(30000):
                    runner_alive = True
                    self.error.emit("decision worker did not stop; "
                                    "session completion is not claimed")
                else:
                    self.runner = None
            acquisition = self._finalize_source()
            self.finished_ok.emit(self._end_info(
                groups_ingested=self._count, reader_alive=reader_alive,
                runner_alive=runner_alive, acquisition=acquisition))

    def _finalize_source(self) -> dict:
        """Stop and verify the acquisition before reporting the worker done.

        This is the regression fix for the 52 GB session: stopping the reader
        used to end the session while the capture child kept recording for
        180 s.  Every exit path now stops the supervised source and keeps the
        acquisition verdict.
        """
        verdict: dict = {}
        try:
            stopper = getattr(self.source, "stop", None)
            if callable(stopper):
                verdict = dict(stopper(reason="worker stopped") or {})
        except Exception as exc:
            verdict = {"ok": False, "verified": False, "safe": False,
                       "state": "failed",
                       "reasons": [f"source stop failed: "
                                   f"{type(exc).__name__}: {exc}"]}
        acquisition: dict = {}
        try:
            state_fn = getattr(self.source, "acquisition_state", None)
            if callable(state_fn):
                acquisition = dict(state_fn() or {})
            else:
                capture = getattr(self.source, "capture", None)
                state_fn = getattr(capture, "acquisition_state", None)
                if callable(state_fn):
                    acquisition = dict(state_fn() or {})
        except Exception as exc:
            acquisition = {"state": "failed", "verified": False,
                           "safe": False, "terminal": True,
                           "reasons": [f"acquisition state unavailable: "
                                       f"{type(exc).__name__}: {exc}"]}
        kind = str(getattr(self.source, "kind", "") or "").lower()
        if not acquisition and not kind.endswith("live"):
            # Replay/synthetic/vendor sources own no scanner, so there is no
            # acquisition to verify.  This is an explicit not_applicable
            # verdict, not a failed/unverified one; treating absence as
            # failure broke every headless replay/synthetic finish.
            acquisition = {"state": "not_applicable", "verified": None,
                           "safe": None, "terminal": True, "reasons": []}
        if not verdict:
            # Preserve the tri-state verified/safe values (True/False/None):
            # None means "no scanner was involved" (replay/synthetic/vendor)
            # and must not be flattened to False, which the window would
            # correctly read as an unverified live shutdown.
            verdict = {"ok": bool(acquisition.get("verified")),
                       "verified": acquisition.get("verified"),
                       "safe": acquisition.get("safe"),
                       "state": acquisition.get("state"),
                       "reasons": list(acquisition.get("reasons") or [])}
        verdict["acquisition"] = acquisition
        self.end_acquisition = acquisition
        return verdict

    def _end_info(self, **values) -> dict:
        acquisition = values.pop("acquisition", None) or self.end_acquisition
        info = dict(values)
        if not acquisition or acquisition.get("state") in (
                "not_applicable", None):
            acquisition = dict(acquisition or {})
            acquisition.setdefault("state", "not_applicable")
            acquisition.setdefault("verified", None)
            acquisition.setdefault("safe", None)
            acquisition.setdefault("terminal", True)
        info.update({
            "stop_requested": bool(self._stop.is_set()),
            "acquisition": acquisition,
            "source_final_verdict": (getattr(self.source, "final_verdict", None)
                                     or None),
            "complete": bool(values.get("complete", True))
                        and acquisition.get("verified") is not False,
            "verified": bool(acquisition.get("verified")),
            "safe": bool(acquisition.get("safe")),
            "capture_state": acquisition.get("state"),
            "capture_reasons": list(acquisition.get("reasons") or []),
            "capture_conflicts": list(acquisition.get("conflicts") or []),
        })
        self.end_info = info
        return info

    def _run_sync(self) -> None:
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
                # Publish the newest images first.  This is the whole point of
                # the split: preview cadence is the device cadence, not the
                # detection cadence (the old code updated after ingest, so a
                # 158 ms decision capped the display at ~6 Hz).
                self._publish_preview(group)
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
                    if self.max_groups and self._count >= self.max_groups:
                        break
                    pace()
                    continue
                started = time.monotonic()
                decision = self.controller.ingest(group)
                self._last_ingest_s = time.monotonic() - started
                self._last_decision_at = time.monotonic()
                payload = (decision.as_dict() if hasattr(decision, "as_dict")
                           else dict(decision))
                self._on_runner_decision(payload)
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
            acquisition = self._finalize_source()
            self.finished_ok.emit(self._end_info(
                groups_ingested=self._count, reader_alive=False,
                runner_alive=False, complete=not self._stop.is_set()
                or bool(acquisition.get("verified", True)),
                acquisition=acquisition))


class StopWorker(QtCore.QThread):
    """Run the controller's verified acquisition stop off the GUI thread.

    Used when the reader ended while the acquisition is still live (or its
    verification is pending): the window must keep painting while the child is
    stopped, the readback is checked and the recording is finalized.
    """

    done = QtCore.Signal(dict)

    def __init__(self, controller, reason: str = "window stop", parent=None):
        super().__init__(parent)
        self.controller = controller
        self.reason = str(reason)

    def run(self) -> None:  # noqa: D401
        try:
            stop = getattr(self.controller, "stop_capture", None)
            result = dict(stop(reason=self.reason) or {}) if callable(stop) else {}
        except Exception as exc:
            result = {"attempted": True, "verified": False, "safe": False,
                      "ok": False, "state": "failed",
                      "reasons": [f"{type(exc).__name__}: {exc}"]}
        self.done.emit(result)
