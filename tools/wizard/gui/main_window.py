"""Main window: layout, display timer and control wiring."""
from __future__ import annotations

import time

from PySide6 import QtCore, QtGui, QtWidgets

from . import (controller_adapter, panels, plate3d, status as status_rules,
               theme, tiles, worker)


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, controller, source, *, pace_hz: float = 0.0,
                 headless: bool = False, max_groups: int = 0, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.source = source
        try:
            value = getattr(source, "continuous", None)
            if value is None:
                value = getattr(getattr(source, "info", None), "continuous", True)
            continuous = bool(value)
            controller._continuous_override = continuous
            controller.controller.continuous = continuous
        except Exception:
            pass
        self.headless = headless
        self.setWindowTitle("Einstar calibration wizard")
        self.resize(1600, 950)
        self.setStyleSheet(theme.app_stylesheet())

        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        root = QtWidgets.QVBoxLayout(central)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(8)
        root.addWidget(self._build_header())

        body = QtWidgets.QHBoxLayout()
        body.setSpacing(8)
        root.addLayout(body, 1)

        # left: 2x2 tiles
        grid = QtWidgets.QGridLayout()
        grid.setSpacing(8)
        self.tile_left = tiles.CameraTile("LEFT IR  ·  cam1")
        self.tile_right = tiles.CameraTile("RIGHT IR  ·  cam0 (rotated 180°)")
        self.tile_colour = tiles.CameraTile("COLOUR  ·  cam2 (BayerRG, rotated 180°)")
        self.tile_coverage = tiles.CoverageTile()
        grid.addWidget(self.tile_left, 0, 0)
        grid.addWidget(self.tile_right, 0, 1)
        grid.addWidget(self.tile_colour, 1, 0)
        grid.addWidget(self.tile_coverage, 1, 1)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        grid.setRowStretch(0, 1)
        grid.setRowStretch(1, 1)
        left = QtWidgets.QWidget()
        left.setLayout(grid)
        body.addWidget(left, 5)

        # middle column: guidance + progress + observability
        middle = QtWidgets.QVBoxLayout()
        middle.setSpacing(8)
        self.guidance = panels.GuidancePanel()
        self.progress = panels.ProgressPanel()
        self.plate3d = plate3d.PlatePreview()
        self.plate3d.setMinimumHeight(230)
        # engineering detail lives behind a disclosure: the operator needs the
        # instruction and the progress, not the covariance matrix
        # a disclosure that starts closed: the operator sees the instruction
        # and the progress; the covariance table is one click away
        self.diagnostics = QtWidgets.QGroupBox("Advanced diagnostics")
        self.diagnostics.setObjectName("panel")
        self.diagnostics.setCheckable(True)
        self.diagnostics.setChecked(False)
        diag_lay = QtWidgets.QVBoxLayout(self.diagnostics)
        diag_lay.setContentsMargins(6, 6, 6, 6)
        self.observability = panels.ObservabilityPanel()
        diag_lay.addWidget(self.observability)
        self.diagnostics.toggled.connect(
            lambda shown: (self.observability.setVisible(bool(shown)),
                           self.diagnostics.adjustSize()))
        self.observability.setVisible(False)
        middle.addWidget(self.guidance, 0)
        middle.addWidget(self.progress, 0)
        middle.addWidget(self.plate3d, 1)
        middle.addWidget(self.diagnostics, 0)
        mid_host = QtWidgets.QWidget()
        mid_host.setLayout(middle)
        mid_host.setMinimumWidth(380)
        body.addWidget(mid_host, 3)

        # right column: capture, controls, log
        right = QtWidgets.QVBoxLayout()
        right.setSpacing(8)
        self.capture = panels.CapturePanel()
        self.controls = panels.ControlsPanel()
        self.log = panels.EventLog()
        right.addWidget(self.capture, 0)
        right.addWidget(self.controls, 0)
        right.addWidget(self.log, 1)
        right_host = QtWidgets.QWidget()
        right_host.setLayout(right)
        right_host.setMinimumWidth(300)
        body.addWidget(right_host, 2)

        self.status = QtWidgets.QStatusBar()
        self.setStatusBar(self.status)
        self._status_right = QtWidgets.QLabel("idle")
        self.status.addPermanentWidget(self._status_right)

        self.controls.start.connect(self._on_start)
        self.controls.pause.connect(self._on_pause)
        self.controls.resume.connect(self._on_resume)
        self.controls.finish.connect(self._on_finish)
        self.controls.cancel.connect(self._on_cancel)

        self.worker: worker.SessionWorker | None = None
        self._last_index = -1
        self._error: str | None = None
        self._started_at = 0.0
        self._decision_count = 0
        self._rate_count = 0
        self._rate_at = 0.0
        self._last_decision_at = 0.0
        self._zero_fps_since = 0.0
        self._stall_logged = False
        self._last_3d_at = 0.0
        self._finish_worker = None
        self._finish_started_at = 0.0
        self._3d_interval_s = 0.1        # 10 Hz upper bound
        self._3d_intrinsics_set = False

        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(100)  # 10 Hz display refresh

        if pace_hz:
            self._pace_hz = pace_hz
        else:
            self._pace_hz = 0.0
        #: minimum spacing between full decisions in stream time; the worker
        #: also raises it automatically when a decision costs longer than the
        #: spacing.  Every group is recorded either way.
        self._min_decision_interval_s = 0.0
        self._max_groups = max_groups
        self._closing = False

    # -- header ---------------------------------------------------------
    def _build_header(self) -> QtWidgets.QWidget:
        bar = QtWidgets.QFrame()
        bar.setObjectName("panel")
        lay = QtWidgets.QHBoxLayout(bar)
        lay.setContentsMargins(14, 8, 14, 8)
        title = QtWidgets.QLabel("EINSTAR  ·  CALIBRATION WIZARD")
        title.setObjectName("value")
        lay.addWidget(title)
        lay.addSpacing(16)
        self.header_device = QtWidgets.QLabel("no device")
        self.header_device.setObjectName("muted")
        lay.addWidget(self.header_device)
        lay.addStretch(1)
        self.header_state = QtWidgets.QLabel("IDLE")
        self.header_state.setObjectName("value")
        lay.addWidget(self.header_state)
        lay.addSpacing(16)
        self.header_clock = QtWidgets.QLabel("00:00")
        self.header_clock.setObjectName("value")
        self.header_clock.setFont(theme.mono_font(12))
        lay.addWidget(self.header_clock)
        return bar

    # -- controls -------------------------------------------------------
    def _on_start(self) -> None:
        if self.worker is not None and self.worker.isRunning():
            return
        # Start capture from this long-lived thread, not from the reader
        # thread: Linux PR_SET_PDEATHSIG follows the spawning thread.
        try:
            starter = getattr(self.source, "start", None)
            if callable(starter):
                starter()
        except Exception as exc:
            self._fail(f"could not start acquisition: {type(exc).__name__}: {exc}")
            return
        try:
            self.controller.begin()
        except Exception as exc:
            try:
                close = getattr(self.source, "close", None)
                if callable(close):
                    close()
            except Exception:
                pass
            self._fail(f"begin failed: {type(exc).__name__}: {exc}")
            return
        self._started_at = time.time()
        self._error = None
        self.worker = worker.SessionWorker(
            self.controller, self.source, pace_hz=self._pace_hz,
            max_groups=self._max_groups, parent=self,
            min_decision_interval_s=getattr(self, "_min_decision_interval_s", 0.0))
        self.worker.decision.connect(self._on_decision)
        self.worker.error.connect(self._fail)
        self.worker.finished_ok.connect(self._on_worker_done)
        self.worker.start()
        self.log.insertItem(0, QtWidgets.QListWidgetItem("▶ recording started"))
        self.refresh()

    def _on_pause(self) -> None:
        if self.worker is not None:
            self.worker.request_pause()
        try:
            self.controller.pause()
        except Exception as exc:
            self._fail(str(exc))
        self.log.insertItem(0, QtWidgets.QListWidgetItem("⏸ paused"))

    def _on_resume(self) -> None:
        if self.worker is not None:
            self.worker.request_resume()
        try:
            self.controller.unpause()
        except Exception as exc:
            self._fail(str(exc))
        self.log.insertItem(0, QtWidgets.QListWidgetItem("▶ resumed"))

    def _on_finish(self) -> None:
        """Start the final solve on its own thread and keep the window alive.

        Measured 38.3 s at 35 views.  Running it here froze the UI completely -
        no repaint, no cursor change, no feedback - which is why pressing Finish
        looked like a hang.  The buttons are disabled for the duration, the
        status line shows elapsed time, and closing the window waits for it.
        """
        if self._finish_worker is not None and self._finish_worker.isRunning():
            return
        self._finish_started_at = time.time()
        self._finish_worker = worker.FinishWorker(self.controller, self.worker,
                                                  parent=self)
        self._finish_worker.progress.connect(self._on_finish_progress)
        self._finish_worker.done.connect(self._on_finish_done)
        self._finish_worker.failed.connect(self._on_finish_failed)
        self.controls.setEnabled(False)
        self.guidance.show_feedback("⏳ building the profile…", theme.WARN)
        self.log.insertItem(0, QtWidgets.QListWidgetItem(
            "⏳ finish started — solving and validating; the window stays live"))
        self._finish_worker.start()

    def _on_finish_progress(self, elapsed: float) -> None:
        self.status.showMessage(f"building the profile… {elapsed:.0f} s")
        self.guidance.show_feedback(f"⏳ building the profile… {elapsed:.0f} s",
                                    theme.WARN)

    def _on_finish_failed(self, message: str) -> None:
        self._finish_worker = None
        self._fail(f"finish failed: {message}")
        self.controls.setEnabled(True)
        self.refresh()

    def _on_finish_done(self, result: dict) -> None:
        self._finish_worker = None
        self.controls.setEnabled(True)
        ok = bool(result.get("ok"))
        prof = result.get("profile") or {}
        text = ("✅ session finished — candidate profile written (validation: pending)"
                if ok else
                f"⚠ session finished but not ready: {result.get('reason') or result.get('quality', {})}")
        self.log.insertItem(0, QtWidgets.QListWidgetItem(text))
        self.status.showMessage(text, 20000)
        self.controls.setEnabled(True)
        self.refresh()

    def _on_cancel(self) -> None:
        if self.worker is not None and self.worker.isRunning():
            self.worker.request_stop()
            self.worker.wait(4000)
        try:
            self.controller.cancel()
        except Exception as exc:
            self._fail(str(exc))
        self.log.insertItem(0, QtWidgets.QListWidgetItem("⏹ cancelled — data kept"))
        self.refresh()

    def _on_decision(self, payload: dict) -> None:
        self._decision_count += 1
        self._rate_count += 1
        self._last_decision_at = time.time()
        self._stall_logged = False
        text, colour = panels.feedback_text(payload)
        self.guidance.show_feedback(text, getattr(theme, colour, theme.MUTED))
        self.log.add_decision({"last_decision": payload})

    def processing_fps(self) -> float:
        """Decisions processed per second since the previous sample."""
        now = time.time()
        if self._rate_at <= 0.0:
            self._rate_at, self._rate_count = now, 0
            return 0.0
        span = now - self._rate_at
        if span < 1.0:
            return self._cached_fps if hasattr(self, "_cached_fps") else 0.0
        self._cached_fps = self._rate_count / span
        self._rate_at, self._rate_count = now, 0
        return self._cached_fps

    def _on_worker_done(self, info: dict) -> None:
        self.status.showMessage(
            f"worker finished: {info.get('groups_ingested', 0)} groups ingested", 8000)

    def _fail(self, message: str) -> None:
        self._error = message
        self.log.insertItem(0, QtWidgets.QListWidgetItem(f"‼ {message}"))
        self.status.showMessage(message, 15000)

    # -- refresh --------------------------------------------------------
    def refresh(self) -> None:
        try:
            snap = self.controller.snapshot()
        except Exception as exc:
            self._fail(f"snapshot failed: {type(exc).__name__}: {exc}")
            return
        if self._error:
            snap["error"] = self._error
        snap["processing_fps"] = self.processing_fps()
        if self._closing:
            return
        latest = self.worker.latest if self.worker is not None else None
        detections = snap.get("detections") or {}

        if latest is not None and latest["index"] != self._last_index:
            self._last_index = latest["index"]
            left = latest["images"].get("left")
            right = latest["images"].get("right")
            colour = latest.get("colour_bgr")
            axes_left = self._axes_from_snapshot(snap, "left")
            self.tile_left.set_frame(left, detections.get("left"), axes_left)
            self.tile_right.set_frame(right, detections.get("right"),
                                      self._axes_from_snapshot(snap, "right"))
            self.tile_colour.set_frame(colour, None, None, colour=True)

        markers = snap.get("markers") or {}
        self.tile_left.set_status(f"{markers.get('left', 0)} markers  ·  "
                                  f"shared IR {snap.get('shared_markers_ir', 0)}")
        self.tile_right.set_status(f"{markers.get('right', 0)} markers")
        self.tile_colour.set_status(f"{markers.get('tex', 0)} markers  ·  "
                                    f"assignment {'ok' if snap.get('assignment_ok') else 'AMBIGUOUS'}")

        capture = snap.get("capture") or {}
        state = str(snap.get("state", "idle"))
        # never claim RECORDING while nothing is arriving: the operator could not
        # tell a slow session from a dead one (the reported failure)
        fps_reported = capture.get("fps")
        try:
            fps_reported = None if fps_reported is None else float(fps_reported)
        except (TypeError, ValueError):
            fps_reported = None
        # only a live capture can report "0 fps"; a replay source has no capture
        # process, and judging it by a capture rate produced false stalls
        source_kind = str((snap.get("source") or {}).get("kind", "")).lower()
        if source_kind != "live":
            fps_reported = None
        now = time.time()
        if fps_reported is not None and fps_reported <= 0.05:
            if self._zero_fps_since <= 0.0:
                self._zero_fps_since = now
        else:
            self._zero_fps_since = 0.0
        since_decision = (now - self._last_decision_at) if self._last_decision_at else None
        reason = status_rules.stall_reason(
            state, since_decision, fps_reported,
            seconds_at_zero=(now - self._zero_fps_since) if self._zero_fps_since else 0.0)
        colour_state = {"recording": theme.OK, "paused": theme.WARN,
                        "done": theme.INFO, "error": theme.BAD}.get(state, theme.MUTED)
        if reason is not None:
            colour_state = theme.BAD
            if not self._stall_logged:
                self._stall_logged = True
                self.log.insertItem(0, QtWidgets.QListWidgetItem(
                    f"‼ capture stalled — {reason}"))
        self.header_state.setText(status_rules.display_state(state, reason))
        self.header_state.setStyleSheet(f"color: {colour_state};")
        src = snap.get("source") or {}
        where = src.get("serial") or src.get("path") or ""
        if len(str(where)) > 60:
            where = "…" + str(where)[-59:]
        self.header_device.setText(f"{src.get('kind','—')}  ·  {where}")
        elapsed = (time.time() - self._started_at) if self._started_at else 0.0
        self.header_clock.setText(f"{int(elapsed)//60:02d}:{int(elapsed)%60:02d}")
        # scanner status: what the device is doing and what we are keeping up with
        rate = self.processing_fps()
        live_fps = None
        if str((snap.get("source") or {}).get("kind", "")).lower() == "live":
            try:
                live_fps = float((snap.get("capture") or {}).get("fps"))
            except (TypeError, ValueError):
                live_fps = None
        bits = [f"{rate:.1f} decisions/s"]
        if live_fps is not None:
            bits.insert(0, f"{live_fps:.1f} fps")
        self.header_device.setText(
            f"{src.get('kind') or 'replay'}  ·  " + "  ·  ".join(bits))

        cov = snap.get("coverage") or {}
        views = cov.get("views") or []
        regions = self._region_grid(views)
        distances = [float(v.get("distance_mm") or 0) for v in views]
        tilts = [(float(v.get("tilt_x_deg") or 0), float(v.get("tilt_y_deg") or 0))
                 for v in views]
        rolls = [float(v.get("roll_deg") or 0) for v in views]
        self.tile_coverage.set_data(regions, distances, tilts, rolls,
                                    bool(cov.get("ready")))

        self.guidance.update_from(snap)
        self.progress.update_from(snap)
        self._update_plate3d(snap)
        self.observability.update_from(snap)
        self.capture.update_from(snap)
        self.controls.update_from(snap)

        if snap.get("error"):
            self.status.showMessage(str(snap["error"]), 5000)
        elif capture.get("state"):
            self._status_right.setText(
                f"{capture.get('state')}  ·  {self._decision_count} decisions")

    def _update_plate3d(self, snap: dict) -> None:
        """Refresh the 3-D preview at most every ``_3d_interval_s``.

        It is a *preview*: when the pipeline is behind (the measured decision
        rate drops or a stall is showing) the interval is lengthened, and when
        the window is closing it is skipped entirely, so the drawing can never
        take time from acquisition or the calibration.
        """
        if self._closing:
            return
        now = time.time()
        rate = self.processing_fps()
        self._3d_interval_s = 0.1 if rate >= 2.0 else 0.2
        if now - self._last_3d_at < self._3d_interval_s:
            return
        self._last_3d_at = now
        camera = snap.get("camera_left") or {}
        if not self._3d_intrinsics_set and camera:
            self.plate3d.set_intrinsics(camera.get("fx", 1150.0),
                                        camera.get("fy", 1150.0),
                                        camera.get("cx", 640.0),
                                        camera.get("cy", 512.0))
            self._3d_intrinsics_set = True
        pose = snap.get("pose_rt")
        if not pose:
            self.plate3d.set_status("waiting for the plate…")
            return
        self.plate3d.set_pose(pose.get("R"), pose.get("t"))
        summary = snap.get("last_pose") or {}
        dist = summary.get("distance_mm")
        tilt = summary.get("tilt_deg")
        roll = summary.get("roll_deg")
        bits = []
        if dist is not None:
            bits.append(f"{float(dist):.0f} mm")
        if tilt is not None:
            bits.append(f"tilt {float(tilt):.0f}°")
        if roll is not None:
            bits.append(f"roll {float(roll):.0f}°")
        self.plate3d.set_status("  ·  ".join(bits) or "pose available")

    # -- small snapshot helpers (tolerant of missing keys) --------------
    def _axes_from_snapshot(self, snap: dict, camera: str):
        axes = (snap.get("overlays") or {}).get(camera)
        if not axes:
            return None
        try:
            return (axes["origin"], axes["u"], axes["v"])
        except Exception:
            return None

    @staticmethod
    def _region_grid(views):
        import numpy as np
        counts = np.zeros((3, 3), int)
        for view in views:
            uv = view.get("image_uv")
            if not uv:
                continue
            r = int(np.clip(float(uv[1]) * 3, 0, 2))
            c = int(np.clip(float(uv[0]) * 3, 0, 2))
            counts[r, c] += 1
        return counts

    # -- shutdown -------------------------------------------------------
    def shutdown(self, timeout_s: float = 20.0) -> bool:
        """Stop the worker; True when it has actually finished.

        Calling ``QThread.wait`` once and giving up leaves a running thread that
        Qt then destroys with the window - which aborts the process.  That is the
        crash a long solver pause produced: the wait timed out after 5 s, the
        window went away, and the process died with the thread still inside the
        optimisation.  Now the wait keeps the event loop alive (so the window
        stays responsive) and reports honestly whether the worker is gone.
        """
        self._closing = True
        self._timer.stop()
        finisher = self._finish_worker
        if finisher is not None and finisher.isRunning():
            # same rule as the ingest worker: never destroy a running QThread
            deadline = time.time() + float(timeout_s)
            while finisher.isRunning() and time.time() < deadline:
                QtWidgets.QApplication.processEvents()
                time.sleep(0.01)
            if finisher.isRunning():
                return False
            self._finish_worker = None
        worker = self.worker
        if worker is None or not worker.isRunning():
            return True
        worker.request_stop()
        deadline = time.time() + float(timeout_s)
        while worker.isRunning() and time.time() < deadline:
            QtWidgets.QApplication.processEvents()
            time.sleep(0.01)
        return not worker.isRunning()

    def closeEvent(self, event):  # noqa: N802 (Qt naming)
        """Never let Qt destroy the window while the worker thread runs.

        Destroying a running ``QThread`` aborts the process, and a solver pause
        can last tens of seconds.  The close is therefore deferred: the request
        is refused now, the worker is asked to stop, and the close is retried
        shortly while the window stays responsive.
        """
        worker = self.worker
        finisher = self._finish_worker
        finishing = finisher is not None and finisher.isRunning()
        if finishing or (worker is not None and worker.isRunning()):
            self._closing = True
            self._timer.stop()
            if worker is not None and worker.isRunning():
                worker.request_stop()
            event.ignore()
            self.status.showMessage(
                "stopping: waiting for the final solve to finish…" if finishing
                else "stopping: waiting for the optimisation to finish…", 0)
            QtCore.QTimer.singleShot(200, self.close)
            return
        self._closing = True
        self._timer.stop()
        super().closeEvent(event)

    # -- headless -------------------------------------------------------
    def snapshot_png(self, path: str) -> None:
        QtWidgets.QApplication.processEvents()
        pix = self.grab()
        pix.save(path)
