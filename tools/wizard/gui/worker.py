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


class SessionWorker(QtCore.QThread):
    """Runs the capture loop off the UI thread."""

    decision = QtCore.Signal(dict)
    error = QtCore.Signal(str)
    finished_ok = QtCore.Signal(dict)

    def __init__(self, controller, source, *, pace_hz: float = 0.0,
                 max_groups: int = 0, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.source = source
        self.pace_hz = float(pace_hz)
        self.max_groups = int(max_groups)
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
        try:
            for group in self.source:
                if self._stop.is_set():
                    break
                while self._pause.is_set() and not self._stop.is_set():
                    time.sleep(0.02)
                if self._stop.is_set():
                    break
                started = time.monotonic()
                decision = self.controller.ingest(group)
                payload = decision.as_dict() if hasattr(decision, "as_dict") else dict(decision)
                with self._lock:
                    self._latest = {
                        "index": group.index,
                        "device_group": group.device_group,
                        "timestamp_s": group.timestamp_s,
                        "images": dict(group.images),
                        "colour_bgr": getattr(group, "colour_bgr", None),
                        "complete": group.complete,
                        "source": group.source,
                        "decision": payload,
                    }
                self.decision.emit(payload)
                self._count += 1
                if self.max_groups and self._count >= self.max_groups:
                    break
                if period:
                    next_at += period
                    delay = next_at - time.monotonic()
                    if delay > 0:
                        time.sleep(delay)
                elif time.monotonic() - started > 5.0:
                    # never let an unexpected stall look like a hang
                    self.error.emit("ingest took > 5 s for one group")
        except Exception as exc:  # pragma: no cover - surface to the UI
            self.error.emit(f"{type(exc).__name__}: {exc}")
        finally:
            self.finished_ok.emit({"groups_ingested": self._count,
                                   "stopped": self._stop.is_set()})
