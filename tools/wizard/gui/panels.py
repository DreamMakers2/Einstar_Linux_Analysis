"""Right-hand panels: guidance, progress, observability, capture, controls, log."""
from __future__ import annotations

from PySide6 import QtCore, QtGui, QtWidgets

from . import theme


def _num(value) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _title(text: str) -> QtWidgets.QLabel:
    lab = QtWidgets.QLabel(text.upper())
    lab.setObjectName("title")
    return lab


def _panel(margin: int = 10) -> tuple[QtWidgets.QFrame, QtWidgets.QVBoxLayout]:
    frame = QtWidgets.QFrame()
    frame.setObjectName("panel")
    lay = QtWidgets.QVBoxLayout(frame)
    lay.setContentsMargins(margin, margin, margin, margin)
    lay.setSpacing(6)
    return frame, lay


class GuidancePanel(QtWidgets.QFrame):
    """The one thing the operator has to read: what to do next."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("panel")
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(6)
        lay.addWidget(_title("next move"))

        self.instruction = QtWidgets.QLabel("Waiting for the plate…")
        self.instruction.setObjectName("huge")
        self.instruction.setWordWrap(True)
        self.instruction.setMinimumHeight(104)
        lay.addWidget(self.instruction)

        self.detail = QtWidgets.QLabel("")
        self.detail.setObjectName("muted")
        self.detail.setWordWrap(True)
        lay.addWidget(self.detail)

        row = QtWidgets.QHBoxLayout()
        self.priority = QtWidgets.QLabel("—")
        self.priority.setObjectName("value")
        self.target = QtWidgets.QLabel("")
        self.target.setObjectName("muted")
        self.target.setWordWrap(True)
        row.addWidget(QtWidgets.QLabel("priority:"))
        row.addWidget(self.priority)
        row.addSpacing(10)
        row.addWidget(self.target, 1)
        lay.addLayout(row)

        # transient feedback for the decision just taken: the operator needs to
        # know immediately whether the pose they are holding was captured
        self.feedback = QtWidgets.QLabel("")
        self.feedback.setObjectName("big")
        self.feedback.setMinimumHeight(26)
        lay.addWidget(self.feedback)

        self.ready = QtWidgets.QLabel("NOT READY")
        self.ready.setObjectName("value")
        self.ready.setWordWrap(True)
        lay.addWidget(self.ready)

    def show_feedback(self, text: str, colour: str) -> None:
        """Show the latest decision for a moment, then fade back."""
        self.feedback.setText(text)
        self.feedback.setStyleSheet(f"color: {colour};")
        QtCore.QTimer.singleShot(1800, lambda: self.feedback.setText(""))

    def update_from(self, snap: dict) -> None:
        guide = snap.get("guidance") or {}
        instr = guide.get("instruction") or "Waiting for the plate…"
        self.instruction.setText(instr)
        # scale the headline to the text: a long instruction at 34 px wraps to
        # three lines and overlaps the detail line below it
        budget = len(instr)
        size = 34 if budget <= 20 else (26 if budget <= 40 else (20 if budget <= 64 else 17))
        self.instruction.setStyleSheet(
            f"font-size: {size}px; font-weight: 700;")
        self.detail.setText(guide.get("detail") or "")
        prio = guide.get("priority") or "—"
        self.priority.setText(str(prio))
        self.priority.setStyleSheet(f"color: {theme.ACCENT}; font-weight: 700;")
        target = guide.get("target") or {}
        bits = []
        dist = _num(target.get("distance_mm"))
        if dist is not None:
            bits.append(f"{dist:.0f} mm")
        tilt = target.get("tilt_deg")
        if isinstance(tilt, (list, tuple)) and len(tilt) == 2:
            tx, ty = _num(tilt[0]), _num(tilt[1])
            if tx is not None and ty is not None:
                bits.append(f"tilt {tx:+.0f}°/{ty:+.0f}°")
        roll = _num(target.get("roll_deg"))
        if roll is not None:
            bits.append(f"roll {roll:+.0f}°")
        if target.get("region"):
            bits.append(str(target["region"]))
        family = guide.get("family") or {}
        if isinstance(family, dict):
            for key in ("distance_mm", "tilt_label", "roll_label", "region_label"):
                val = family.get(key)
                if key == "distance_mm":
                    val = _num(val)
                    if val is not None:
                        bits.append(f"family {val:.0f} mm")
                elif val and str(val) not in bits:
                    bits.append(str(val))
        self.target.setText(" · ".join(bits))

        ready = snap.get("readiness") or {}
        if ready.get("ready"):
            self.ready.setText("READY TO FINISH — all coverage dimensions satisfied")
            self.ready.setStyleSheet(f"color: {theme.OK}; font-weight: 700;")
        else:
            missing = ready.get("missing") or []
            text = "NOT READY"
            if missing:
                text += " — missing: " + ", ".join(str(m) for m in missing[:4])
            self.ready.setText(text)
            self.ready.setStyleSheet(f"color: {theme.WARN}; font-weight: 600;")


class GuidancePanelFeedback:
    """Mixin-free helper: the text/colour for one decision (unit-testable)."""


def feedback_text(decision: dict | None) -> tuple[str, str]:
    """(text, colour-name) describing the latest decision for the operator."""
    if not decision:
        return "", "MUTED"
    reasons = [str(r) for r in (decision.get("reasons") or [])]
    if decision.get("accepted"):
        view = decision.get("view_id")
        return (f"✓ captured view {view}" if view is not None
                else "✓ captured"), "OK"
    joined = "; ".join(reasons)
    if "too similar" in joined:
        return "↺ already have this view — move further", "WARN"
    if "moving" in joined or "blur" in joined:
        return "⚠ too fast — hold still", "WARN"
    if "redundant" in joined:
        return "↺ adds nothing new — change the pose", "WARN"
    if any(k in joined for k in ("no pose", "orientation", "ambiguous",
                                 "insufficient", "markers")):
        return "⚠ plate not read — show it fully", "BAD"
    return "✗ not captured" + (f": {reasons[0]}" if reasons else ""), "BAD"


class ProgressPanel(QtWidgets.QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("panel")
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(6)
        lay.addWidget(_title("session"))

        row = QtWidgets.QHBoxLayout()
        self.accepted = self._stat("accepted", theme.OK)
        self.rejected = self._stat("rejected", theme.BAD)
        self.redundant = self._stat("redundant", theme.WARN)
        for w in (self.accepted[0], self.rejected[0], self.redundant[0]):
            row.addWidget(w, 1)
        lay.addLayout(row)

        self.bar = QtWidgets.QProgressBar()
        self.bar.setRange(0, 8)
        self.bar.setValue(0)
        self.bar.setTextVisible(False)
        lay.addWidget(self.bar)
        self.coverage_text = QtWidgets.QLabel("coverage 0 / 8 dimensions")
        self.coverage_text.setObjectName("muted")
        lay.addWidget(self.coverage_text)

        # readiness is a verdict, not a percentage: the operator must never read
        # "80%" as "nearly done" when a whole dimension is missing
        self.readiness = QtWidgets.QLabel("NOT READY")
        self.readiness.setObjectName("value")
        self.readiness.setWordWrap(True)
        lay.addWidget(self.readiness)

        self.dims = QtWidgets.QListWidget()
        self.dims.setMaximumHeight(150)
        self.dims.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
        lay.addWidget(self.dims)

        self.footer = QtWidgets.QLabel("")
        self.footer.setObjectName("muted")
        lay.addWidget(self.footer)

    def _stat(self, label: str, colour: str):
        box = QtWidgets.QVBoxLayout()
        value = QtWidgets.QLabel("0")
        value.setObjectName("big")
        value.setStyleSheet(f"color: {colour};")
        cap = QtWidgets.QLabel(label)
        cap.setObjectName("muted")
        box.addWidget(value)
        box.addWidget(cap)
        holder = QtWidgets.QWidget()
        holder.setLayout(box)
        return holder, value

    def update_from(self, snap: dict) -> None:
        counts = snap.get("counts") or {}
        self.accepted[1].setText(str(counts.get("accepted", 0)))
        self.rejected[1].setText(str(counts.get("rejected", 0)))
        self.redundant[1].setText(str(counts.get("redundant", 0)))
        cov = snap.get("coverage") or {}
        dims = cov.get("dimensions") or {}
        satisfied = int(cov.get("satisfied", 0))
        total = int(cov.get("total", max(len(dims), 1)))
        self.bar.setRange(0, max(total, 1))
        self.bar.setValue(satisfied)
        self.coverage_text.setText(f"coverage {satisfied} / {total} dimensions")
        self.dims.clear()
        for name, d in dims.items():
            ok = bool(d.get("satisfied"))
            text = ("✓ " if ok else "✗ ") + name.replace("_", " ")
            if not ok and d.get("hint"):
                text += "  —  " + str(d["hint"])
            item = QtWidgets.QListWidgetItem(text)
            item.setForeground(QtGui.QColor(theme.OK if ok else theme.WARN))
            self.dims.addItem(item)
        src = snap.get("source") or {}
        cap = snap.get("capture") or {}
        read = snap.get("readiness") or {}
        ready = bool(read.get("ready"))
        missing = [str(m) for m in (read.get("missing") or [])]
        if ready:
            self.readiness.setText("READY — press Finish to build the profile")
            self.readiness.setStyleSheet(f"color: {theme.OK}; font-weight: 700;")
        elif missing:
            shown = ", ".join(m.replace("_", " ") for m in missing[:2])
            more = f" (+{len(missing) - 2} more)" if len(missing) > 2 else ""
            self.readiness.setText(f"NOT READY — missing {shown}{more}")
            self.readiness.setStyleSheet(f"color: {theme.WARN};")
        else:
            self.readiness.setText("NOT READY")
            self.readiness.setStyleSheet(f"color: {theme.WARN};")
        rate = _num(snap.get("processing_fps"))
        src_kind = src.get("kind") or "replay"
        rate_bit = (f"{_num(cap.get('fps')) or 0.0:.1f} fps"
                    if str(src_kind) == "live" else
                    (f"{rate:.1f} decisions/s" if rate is not None else "—"))
        self.footer.setText(
            f"{src_kind}  ·  {rate_bit}  ·  "
            f"{_num(snap.get('session',{}).get('elapsed_s')) or 0.0:.0f} s  ·  "
            f"drops {cap.get('drops', 0)}")


class ObservabilityPanel(QtWidgets.QFrame):
    """Per-parameter uncertainty, colour-coded by verdict."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("panel")
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(6)
        lay.addWidget(_title("parameter observability"))

        self.headline = QtWidgets.QLabel("waiting for the first solve")
        self.headline.setObjectName("muted")
        self.headline.setWordWrap(True)
        lay.addWidget(self.headline)

        self.table = QtWidgets.QListWidget()
        self.table.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
        self.table.setMinimumHeight(150)
        lay.addWidget(self.table, 1)

        self.gates = QtWidgets.QLabel("")
        self.gates.setObjectName("muted")
        self.gates.setWordWrap(True)
        lay.addWidget(self.gates)

    def update_from(self, snap: dict) -> None:
        obs = snap.get("observability") or {}
        params = obs.get("parameters") or {}
        if not params:
            self.headline.setText("waiting for the first solve "
                                  "(needs a few accepted views)")
            self.table.clear()
            return
        verdicts = {}
        for p in params.values():
            verdicts[p.get("verdict", "unknown")] = verdicts.get(
                p.get("verdict", "unknown"), 0) + 1
        cond = _num(obs.get("condition")) or 0.0
        self.headline.setText(
            f"condition {cond:.2e}  ·  rank {obs.get('rank','?')}/{obs.get('n_params','?')}  ·  "
            + "  ".join(f"{k}:{v}" for k, v in sorted(verdicts.items())))
        self.table.clear()
        for name, p in params.items():
            sigma = p.get("sigma")
            unit = p.get("unit", "")
            verdict = p.get("verdict", "unknown")
            sig = _num(sigma)
            text = f"{name:<14} {'—' if sig is None else f'{sig:9.4g} {unit}'}   {verdict}"
            item = QtWidgets.QListWidgetItem(text)
            item.setForeground(QtGui.QColor(theme.VERDICT_COLOURS.get(verdict, theme.MUTED)))
            item.setFont(theme.mono_font(11))
            self.table.addItem(item)
        gates = obs.get("gates") or {}
        if gates:
            bad = [k for k, v in gates.items() if isinstance(v, dict) and not v.get("pass")]
            self.gates.setText("gates: " + ("all pass" if not bad else "FAIL " + ", ".join(bad)))


class CapturePanel(QtWidgets.QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("panel")
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(4)
        lay.addWidget(_title("acquisition"))
        self.rows: dict[str, QtWidgets.QLabel] = {}
        for key in ("state", "rate", "groups", "drops", "disk", "recording", "device"):
            row = QtWidgets.QHBoxLayout()
            cap = QtWidgets.QLabel(key)
            cap.setObjectName("muted")
            cap.setFixedWidth(80)
            val = QtWidgets.QLabel("—")
            val.setObjectName("value")
            val.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
            row.addWidget(cap)
            row.addWidget(val, 1)
            lay.addLayout(row)
            self.rows[key] = val

    def update_from(self, snap: dict) -> None:
        cap = snap.get("capture") or {}
        src = snap.get("source") or {}
        sess = snap.get("session") or {}
        # a replay source has no capture process: the controller still reports a
        # capture dict (state idle, 0 fps), which reads as a dead scanner
        is_live = str(src.get("kind", "")).lower() == "live"
        if not is_live:
            cap = {}
        if not cap:
            # replay/synthetic sources have no capture process: report the
            # *processing* rate rather than a capture rate of 0.00 fps, which
            # reads as a stalled scanner
            state = f"{src.get('kind') or 'replay'} source"
        else:
            state = cap.get("state") or snap.get("state", "—")
        self.rows["state"].setText(str(state))
        colour = {"running": theme.OK, "paused": theme.WARN,
                  "stopped": theme.MUTED}.get(str(state), theme.INFO)
        self.rows["state"].setStyleSheet(f"color: {colour};")
        if is_live and cap:
            self.rows["rate"].setText(f"{_num(cap.get('fps')) or 0.0:.2f} fps · "
                                      f"{_num(cap.get('throughput_mbps')) or 0.0:.1f} MB/s")
            self.rows["groups"].setText(
                f"{cap.get('groups_written', 0)} written · "
                f"{cap.get('groups_on_disk', 0)}/{cap.get('ring_size', 0)} on disk")
        else:
            counts = snap.get("counts") or {}
            rate = _num(snap.get("processing_fps"))
            self.rows["rate"].setText(
                f"{rate:.1f} decisions/s" if rate is not None else "—")
            self.rows["groups"].setText(
                f"{counts.get('accepted', 0)} accepted · "
                f"{counts.get('rejected', 0)} rejected")
        stop = cap.get("stop_reason")
        self.rows["drops"].setText(f"{cap.get('drops', 0)}" + (f" · stop: {stop}" if stop else ""))
        if is_live and cap:
            self.rows["disk"].setText(
                f"{_num(cap.get('disk_free_gb')) or 0.0:.1f} GB free")
        else:
            self.rows["disk"].setText("—")
        rec = sess.get("raw_recording") or "—"
        self.rows["recording"].setText(_elide(rec, 34))
        self.rows["device"].setText(
            f"{src.get('serial') or src.get('kind','—')} · {src.get('firmware') or ''}".strip(" ·"))


def _elide(text: str, n: int) -> str:
    text = str(text)
    return text if len(text) <= n else "…" + text[-(n - 1):]


class ControlsPanel(QtWidgets.QFrame):
    start = QtCore.Signal()
    pause = QtCore.Signal()
    resume = QtCore.Signal()
    finish = QtCore.Signal()
    cancel = QtCore.Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("panel")
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(8)
        lay.addWidget(_title("controls"))

        self.btn_start = QtWidgets.QPushButton("Start recording")
        self.btn_start.setObjectName("primary")
        self.btn_pause = QtWidgets.QPushButton("Pause")
        self.btn_resume = QtWidgets.QPushButton("Resume")
        self.btn_finish = QtWidgets.QPushButton("Finish && build profile")
        self.btn_finish.setObjectName("warn")
        self.btn_cancel = QtWidgets.QPushButton("Cancel (keep data)")
        self.btn_cancel.setObjectName("danger")
        for b in (self.btn_start, self.btn_pause, self.btn_resume,
                  self.btn_finish, self.btn_cancel):
            b.setMinimumHeight(34)
            lay.addWidget(b)
        self.btn_start.clicked.connect(self.start.emit)
        self.btn_pause.clicked.connect(self.pause.emit)
        self.btn_resume.clicked.connect(self.resume.emit)
        self.btn_finish.clicked.connect(self.finish.emit)
        self.btn_cancel.clicked.connect(self.cancel.emit)
        self.hint = QtWidgets.QLabel("")
        self.hint.setObjectName("muted")
        self.hint.setWordWrap(True)
        lay.addWidget(self.hint)

    def update_from(self, snap: dict) -> None:
        state = str(snap.get("state", "idle"))
        rec = state == "recording"
        paused = state == "paused"
        idle = state in ("idle", "done", "cancelled", "error")
        self.btn_start.setEnabled(idle)
        self.btn_pause.setEnabled(rec)
        self.btn_resume.setEnabled(paused)
        self.btn_finish.setEnabled(rec or paused)
        self.btn_cancel.setEnabled(not idle)
        if idle:
            self.hint.setText("Show the original Einstar plate and press Start.")
        elif rec:
            self.hint.setText("Recording. New informative views are captured automatically; "
                              "identical-looking views are rejected and counted.")
        elif paused:
            self.hint.setText("Paused — the trigger and illuminator are off. Resume to continue.")
        else:
            self.hint.setText(state)


class EventLog(QtWidgets.QListWidget):
    """Rolling decision log; the newest 200 entries are kept."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
        self.setStyleSheet(f"QListWidget {{ background: {theme.PANEL};"
                           f" border: 1px solid {theme.BORDER}; border-radius: 8px; }}")

    def add_decision(self, snapshot: dict) -> None:
        dec = snapshot.get("last_decision") or {}
        if not dec:
            return
        accepted = bool(dec.get("accepted"))
        reasons = dec.get("reasons") or []
        view = dec.get("view_id")
        if accepted:
            text = f"✓ view {view} accepted"
            if dec.get("predicted_gain") is not None:
                text += f"  (gain {float(dec['predicted_gain']):.3f})"
        else:
            text = "✗ rejected — " + (", ".join(str(r) for r in reasons) or "no reason")
        item = QtWidgets.QListWidgetItem(text)
        item.setForeground(QtGui.QColor(theme.OK if accepted else theme.BAD))
        self.insertItem(0, item)
        while self.count() > 200:
            self.takeItem(self.count() - 1)
