"""Supervisor for the C++ live-capture process.

The wizard never speaks USB.  It spawns exactly **one**
``build/driver/einstar-capture-frames`` and controls it through the control
file defined in ``docs/calibration/wizard_architecture.md`` §5:

* ``--out DIR/frames --status-file DIR/status.json`` (production, the
  default): bounded frame ring plus status; **no** raw stream;
* ``--session-dir DIR`` (diagnostic/developer only): the historical layout,
  which *implies* a lossless ``raw.e1r`` recording of every transfer
  (51.98 GB for the 22.4 min session-03);
* ``--control PATH``     atomically written command file (``start`` |
  ``pause`` | ``resume`` | ``stop``; last line wins);
* ``--ring N``           keep only the newest N complete groups on disk;
* ``--status-file PATH`` live status JSON (``einstar-live-status-v1`` plus the
  wizard extension fields).

Storage modes
-------------
``CaptureConfig.record`` selects the mode.  ``False`` (the default) is
*production*: nothing in the session grows with elapsed time except the
accepted views, which ``tools.wizard.session_io`` stores compressed.  ``True``
is *diagnostic*: the exact raw transfer stream is kept for regression and
forensics.  Select it explicitly, e.g.
``CaptureConfig(..., record=True)`` / ``{"storage": "diagnostic"}`` / the
``EINSTAR_CAPTURE_STORAGE=diagnostic`` environment variable (mapping path).

This module contains **no** acquisition logic: it builds the verified command
line, starts one process at a time, serialises control commands, parses the
status file and checks the safe final state (state ``stopped``, trigger counts
0, illuminator 0).

The verified safe settings are the ones measured on the live unit::

    --trigger 100000 --laser 100
    --strobe 0:1000 --strobe 1:300
    --exposure-us 0:1200 --exposure-us 1:1200 --exposure-us 2:5000
    --gain 0:105 --gain 1:105 --gain 2:180

A stop that needs a signal is reported: SIGINT is only sent when the control
file did not stop the process within :attr:`CaptureConfig.escalation_timeout_s`
seconds (5 s).  That is a diagnostic, not the normal path.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import atexit
from enum import Enum
import json
import os
from pathlib import Path
import shutil
import signal
import struct
import subprocess
import threading
import time
from typing import Any, Mapping, Sequence
import zlib

from . import types

__all__ = [
    "CaptureConfig",
    "LiveCaptureProcess",
    "CaptureError",
    "AcquisitionState",
    "AcquisitionEvidence",
    "ACQUISITION_SCHEMA",
    "SAFE_SETTINGS",
    "DEFAULT_BINARY",
    "CAPTURE_TOOL_NAME",
    "CONTROL_COMMANDS",
    "find_capture_processes",
    "read_recording_trailer",
]

REPO_ROOT = Path(__file__).resolve().parents[2]
CAPTURE_TOOL_NAME = "einstar-capture-frames"
DEFAULT_BINARY = REPO_ROOT / "build" / "driver" / CAPTURE_TOOL_NAME

#: Exact settings verified on the live unit (do not change casually).
SAFE_SETTINGS: dict[str, Any] = {
    "trigger_us": 100000,
    "laser_percent": 100,
    "strobe": ((0, 1000), (1, 300)),
    "exposure_us": {0: 1200, 1: 1200, 2: 5000},
    "gain": {0: 105, 1: 105, 2: 180},
}

CONTROL_COMMANDS = ("start", "pause", "resume", "stop")

#: One authoritative acquisition lifecycle, shared by the supervisor, the
#: controller, the GUI and the diagnostics panel.  ``running`` is never inferred
#: from the last status file alone, and no state implies a safe scanner unless
#: process exit, hardware cleanup readback and recording finalization are all
#: evidenced (see :class:`AcquisitionEvidence`).
class AcquisitionState(str, Enum):
    IDLE = "idle"
    STARTING = "starting"
    RUNNING = "running"
    PAUSING = "pausing"
    PAUSED = "paused"
    STOPPING = "stopping"
    STOPPING_UNVERIFIED = "stopping_unverified"
    STOPPED_VERIFIED = "stopped_verified"
    FAILED = "failed"


ACQUISITION_SCHEMA = "einstar.acquisition-state/1"

#: states that end the acquisition lifecycle for the UI (terminal evidence)
TERMINAL_STATES = frozenset({
    AcquisitionState.STOPPED_VERIFIED,
    AcquisitionState.STOPPING_UNVERIFIED,
    AcquisitionState.FAILED,
})


class CaptureError(RuntimeError):
    """The supervisor refused or failed an operation."""


# ---------------------------------------------------------------------------
# raw recording (.e1r) trailer evidence
# ---------------------------------------------------------------------------

E1R_MAGIC = b"EINSTAR1"
E1R_RECORD_MAGIC = 0x52315345          # "ES1R"
E1R_RECORD_HEADER = 36
E1R_TRAILER_PAYLOAD = 32
E1R_TRAILER_RECORD = E1R_RECORD_HEADER + E1R_TRAILER_PAYLOAD + 4


def read_recording_trailer(path: str | Path) -> dict:
    """Read the last record of an ``.e1r`` file and identify its trailer.

    This is a bounded tail read (72 bytes), never a scan of the recording.  It
    proves that a CRC-valid trailer record is physically present at the end of
    the file and reports its declared transfer/byte counts.  It deliberately
    does **not** recompute the FNV-1a digest over the transfer payload: the C++
    verifier does that, and for a multi-gigabyte file the parent cannot do it
    within the shutdown deadline.  ``digest_verified`` is therefore always
    ``False`` here; the state machine only treats the trailer as *supporting*
    evidence and requires the final ``status.json`` state and the frames
    manifest for a verified completion.
    """
    path = Path(path)
    out: dict[str, Any] = {
        "path": str(path),
        "present": False,
        "crc_valid": False,
        "file_bytes": None,
        "record_type": None,
        "trailer_digest": None,
        "trailer_transfers": None,
        "trailer_bytes": None,
        "digest_verified": False,
        "note": "",
    }
    try:
        size = path.stat().st_size
        out["file_bytes"] = int(size)
    except OSError as exc:
        out["note"] = f"recording not readable: {exc}"
        return out
    if size < 64 + E1R_TRAILER_RECORD:
        out["note"] = "recording is too small to contain a trailer record"
        return out
    try:
        with path.open("rb") as handle:
            header = handle.read(64)
            if header[:8] != E1R_MAGIC:
                out["note"] = "not an EINSTAR1 recording (bad file magic)"
                return out
            handle.seek(int(size) - E1R_TRAILER_RECORD)
            record = handle.read(E1R_TRAILER_RECORD)
    except OSError as exc:
        out["note"] = f"cannot read recording tail: {exc}"
        return out
    if len(record) != E1R_TRAILER_RECORD:
        out["note"] = "short tail read"
        return out
    magic, total, record_type = struct.unpack_from("<IIB", record, 0)
    payload_len = struct.unpack_from("<I", record, 32)[0]
    out["record_type"] = int(record_type)
    if magic != E1R_RECORD_MAGIC or total != E1R_TRAILER_RECORD \
            or record_type != 3 or payload_len != E1R_TRAILER_PAYLOAD:
        out["note"] = ("the final record is not a complete trailer record "
                       "(truncated or still being written)")
        return out
    covered = record[:E1R_RECORD_HEADER + E1R_TRAILER_PAYLOAD]
    stored = struct.unpack_from("<I", record, E1R_RECORD_HEADER
                                + E1R_TRAILER_PAYLOAD)[0]
    out["crc_valid"] = bool((zlib.crc32(covered) & 0xFFFFFFFF) == stored)
    if not out["crc_valid"]:
        out["note"] = "trailer record CRC does not match"
        return out
    digest, transfers, byte_count, _reserved = struct.unpack_from(
        "<QQQQ", record, E1R_RECORD_HEADER)
    out.update({
        "present": True,
        "trailer_digest": int(digest),
        "trailer_transfers": int(transfers),
        "trailer_bytes": int(byte_count),
        "note": ("CRC-valid trailer record present; transfer digest/count "
                 "matching requires the C++ verifier"),
    })
    return out


def _signal_name(returncode: int | None) -> str | None:
    if returncode is None or returncode >= 0:
        return None
    try:
        return signal.Signals(-int(returncode)).name
    except ValueError:
        return f"SIG{-int(returncode)}"


@dataclass
class AcquisitionEvidence:
    """The single answer to "what is the scanner doing right now?".

    ``verified`` is true only when *all three* independent pieces of evidence
    exist:

    1. the capture child process has terminated;
    2. the device cleanup readback is verified (live: trigger counts 0/0 and
       illuminator 0 %; simulator: simulated shutdown verified);
    3. the recording is finalized (final ``status.json`` state ``stopped`` and
       ``frames/frames_manifest.json`` present; a CRC-valid ``.e1r`` trailer is
       supporting evidence, not a substitute).

    ``safe`` is an alias of ``verified``.  Every other terminal state carries
    the concrete missing evidence in ``reasons``; a stopped UI reader never
    implies a stopped scanner.
    """

    schema: str = ACQUISITION_SCHEMA
    state: str = AcquisitionState.IDLE.value
    state_label: str = "idle"
    terminal: bool = False
    verified: bool = False
    safe: bool = False
    session_dir: str = ""
    updated_at_s: float = 0.0
    child: dict = field(default_factory=dict)
    device: dict = field(default_factory=dict)
    recording: dict = field(default_factory=dict)
    stop: dict = field(default_factory=dict)
    status: dict = field(default_factory=dict)
    watchdog: dict = field(default_factory=dict)
    orphans: list = field(default_factory=list)
    reasons: list = field(default_factory=list)
    conflicts: list = field(default_factory=list)
    evidence_notes: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "schema": self.schema,
            "state": self.state,
            "state_label": self.state_label,
            "terminal": bool(self.terminal),
            "verified": bool(self.verified),
            "safe": bool(self.safe),
            "session_dir": self.session_dir,
            "updated_at_s": float(self.updated_at_s),
            "child": dict(self.child),
            "device": dict(self.device),
            "recording": dict(self.recording),
            "stop": dict(self.stop),
            "status": dict(self.status),
            "watchdog": dict(self.watchdog),
            "orphans": list(self.orphans),
            "reasons": list(self.reasons),
            "conflicts": list(self.conflicts),
            "evidence_notes": list(self.evidence_notes),
        }


_STATE_LABELS = {
    AcquisitionState.IDLE: "idle",
    AcquisitionState.STARTING: "starting",
    AcquisitionState.RUNNING: "running",
    AcquisitionState.PAUSING: "pausing",
    AcquisitionState.PAUSED: "paused",
    AcquisitionState.STOPPING: "stopping",
    AcquisitionState.STOPPING_UNVERIFIED: "stopped — UNVERIFIED",
    AcquisitionState.STOPPED_VERIFIED: "stopped — verified",
    AcquisitionState.FAILED: "failed",
}


# ---------------------------------------------------------------------------
# storage modes
# ---------------------------------------------------------------------------

#: Production (the default): the C++ tool writes the bounded ``frames/`` ring
#: through ``--out``/``--status-file`` and **no** ``raw.e1r``.  Accepted views
#: are stored compressed by ``tools.wizard.session_io``.
STORAGE_PRODUCTION = "production"
#: Diagnostic/developer: the historical ``--session-dir`` layout, which makes
#: the tool stream every raw transfer into ``raw.e1r`` (51.98 GB for a 22 min
#: session).  Explicitly selected only; never the default.
STORAGE_DIAGNOSTIC = "diagnostic"
STORAGE_MODES = (STORAGE_PRODUCTION, STORAGE_DIAGNOSTIC)
#: Operator-level opt-in for a diagnostic session without touching the GUI:
#: ``EINSTAR_CAPTURE_STORAGE=diagnostic tools/wizard/gui/app.py --source live``.
STORAGE_ENV_VAR = "EINSTAR_CAPTURE_STORAGE"
_DIAGNOSTIC_WORDS = ("diagnostic", "developer", "lossless", "debug", "raw")
_PRODUCTION_WORDS = ("production", "compact", "default", "normal")
_FALSE_WORDS = ("0", "false", "no", "off", "none", "")


def normalise_storage_mode(value: Any) -> str:
    """Normalise a storage-mode request to ``production`` or ``diagnostic``."""
    text = str(value if value is not None else "").strip().lower()
    if text in _DIAGNOSTIC_WORDS:
        return STORAGE_DIAGNOSTIC
    if text in _PRODUCTION_WORDS:
        return STORAGE_PRODUCTION
    raise ValueError(f"unknown capture storage mode: {value!r} "
                     f"(expected production or diagnostic)")


def normalise_record(value: Any) -> bool:
    """Interpret a 'keep the lossless .e1r recording' request."""
    if isinstance(value, bool):
        return value
    text = str(value if value is not None else "").strip().lower()
    if text in _FALSE_WORDS:
        return False
    if text in ("1", "true", "yes", "on"):
        return True
    return normalise_storage_mode(text) == STORAGE_DIAGNOSTIC


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------

def _norm_key(key: str) -> str:
    return str(key).strip().lstrip("-").replace("-", "_")


def _as_path(value: Any) -> Path:
    return Path(os.path.expanduser(str(value)))


@dataclass(frozen=True)
class CaptureConfig:
    """Everything the command line and the supervisor need.

    Fields default to the verified safe settings; a mapping passed by the GUI
    may override them (values are validated).  ``extra_args`` is appended
    verbatim at the end of the command line.
    """

    session_dir: Path
    binary: Path = DEFAULT_BINARY
    ring: int = 0
    #: Keep the lossless ``raw.e1r`` stream (diagnostic/developer mode).  The
    #: default is **False**: a production session stores accepted views
    #: compressed and never records every frame "just in case".
    record: bool = False
    control: Path | None = None
    status: Path | None = None
    #: tiny volatile-settings file polled by the capture tool.  The wizard
    #: writes it atomically; unchanged content is a no-op on the device.
    exposure_control: Path | None = None
    groups: int = 0
    #: wall-clock safety deadline.  ``None`` (the default) leaves the tool's own
    #: rule in charge: with ``--control`` it runs until told to stop, which is
    #: what a live calibration session needs.  A fixed 30 s default here silently
    #: ended every real session half a minute in, and the wizard then correctly -
    #: but uselessly - reported the capture as stalled.
    timeout_seconds: int | None = None
    max_seconds: float | None = None
    trigger_us: int = SAFE_SETTINGS["trigger_us"]
    laser_percent: int = SAFE_SETTINGS["laser_percent"]
    strobe: tuple[tuple[int, int], ...] = SAFE_SETTINGS["strobe"]
    exposure_us: Mapping[int, int] = field(
        default_factory=lambda: dict(SAFE_SETTINGS["exposure_us"]))
    gain: Mapping[int, int] = field(
        default_factory=lambda: dict(SAFE_SETTINGS["gain"]))
    backend: str = "libusb"
    extra_args: tuple[str, ...] = ()
    poll_interval_s: float = 0.1
    escalation_timeout_s: float = 5.0
    queue_depth: int | None = None
    #: True when the caller supplied ``status`` explicitly (see ``command``).
    status_explicit: bool = False

    # -- derived paths ---------------------------------------------------
    def __post_init__(self) -> None:
        object.__setattr__(self, "session_dir", _as_path(self.session_dir))
        object.__setattr__(self, "binary", _as_path(self.binary))
        if self.control is None:
            object.__setattr__(self, "control", self.session_dir / "control.txt")
        else:
            object.__setattr__(self, "control", _as_path(self.control))
        if self.status is None:
            object.__setattr__(self, "status_explicit", False)
            object.__setattr__(self, "status", self.session_dir / "status.json")
        else:
            object.__setattr__(self, "status_explicit", True)
            object.__setattr__(self, "status", _as_path(self.status))
        if self.exposure_control is None:
            object.__setattr__(self, "exposure_control",
                               self.session_dir / "exposure-control.txt")
        else:
            object.__setattr__(self, "exposure_control",
                               _as_path(self.exposure_control))
        object.__setattr__(self, "strobe",
                           tuple((int(r), int(v)) for r, v in self.strobe))
        object.__setattr__(self, "exposure_us",
                           {int(k): int(v) for k, v in self.exposure_us.items()})
        object.__setattr__(self, "gain", {int(k): int(v) for k, v in self.gain.items()})
        object.__setattr__(self, "extra_args", tuple(str(a) for a in self.extra_args))
        object.__setattr__(self, "record", bool(self.record))
        self._validate()
        self._validate_storage_args()

    def _validate(self) -> None:
        if int(self.ring) < 0:
            raise ValueError("ring must be >= 0 (0 keeps every group)")
        if int(self.groups) < 0:
            raise ValueError("groups must be >= 0 (0 = until stopped)")
        if self.timeout_seconds is not None and int(self.timeout_seconds) <= 0:
            raise ValueError("timeout_seconds must be > 0 when given")
        if self.max_seconds is not None and float(self.max_seconds) <= 0:
            raise ValueError("max_seconds must be > 0 or None")
        if not 0 <= int(self.trigger_us) <= 4_000_000:
            raise ValueError("trigger_us out of range")
        if not 0 <= int(self.laser_percent) <= 100:
            raise ValueError("laser_percent must be 0..100")
        for cam, us in self.exposure_us.items():
            if cam not in (0, 1, 2) or not 1 <= int(us) <= 1_000_000:
                raise ValueError(f"bad exposure for camera {cam}: {us}")
        for cam, g in self.gain.items():
            if cam not in (0, 1, 2) or not 0 <= int(g) <= 65_535:
                raise ValueError(f"bad gain for camera {cam}: {g}")
        for route, value in self.strobe:
            if route not in (0, 1) or not 0 <= value <= 65_535:
                raise ValueError(f"bad strobe route/value {route}:{value}")
        if self.backend not in ("simulated", "libusb"):
            raise ValueError("backend must be 'simulated' or 'libusb'")

    def _validate_storage_args(self) -> None:
        """Refuse a command line that contradicts the selected storage mode.

        ``--session-dir`` implies ``raw.e1r`` in the C++ tool, so it can not be
        smuggled in through ``extra_args`` in production mode; likewise
        ``--record``.  The diagnostic mode owns those flags.
        """
        forbidden = ("--record", "--session-dir")
        found = [a for a in self.extra_args
                 if a.split("=", 1)[0] in forbidden]
        if not self.record and found:
            raise ValueError(
                f"{', '.join(found)} would record the lossless raw stream; "
                "select diagnostic storage (record=True) explicitly")
        if self.record and any(a.split("=", 1)[0] == "--out"
                               for a in self.extra_args):
            raise ValueError("--out contradicts diagnostic recording")

    @property
    def storage_mode(self) -> str:
        """``production`` (default) or ``diagnostic`` (keeps ``raw.e1r``)."""
        return STORAGE_DIAGNOSTIC if self.record else STORAGE_PRODUCTION

    @property
    def lossless_recording(self) -> bool:
        return bool(self.record)

    # -- construction ----------------------------------------------------
    @classmethod
    def from_mapping(cls, args: "CaptureConfig | Mapping[str, Any] | Sequence[str] | None",
                     *, session_dir: str | Path | None = None) -> "CaptureConfig":
        """Build a config from a GUI-friendly mapping.

        Keys may be spelled like the CLI flags (``"--max-seconds"``,
        ``"exposure-us"``).  Unknown keys are ignored; a sequence is treated as
        ``extra_args``.
        """
        if isinstance(args, cls):
            if session_dir is not None and Path(session_dir) != args.session_dir:
                return replace(args, session_dir=_as_path(session_dir))
            return args
        if args is None:
            if session_dir is None:
                raise ValueError("session_dir is required")
            return cls(session_dir=_as_path(session_dir))
        if isinstance(args, (str, bytes)):
            raise TypeError("capture_args must be a mapping or a sequence of flags")
        if not isinstance(args, Mapping):
            if session_dir is None:
                raise ValueError("session_dir is required")
            return cls(session_dir=_as_path(session_dir),
                       extra_args=tuple(str(a) for a in args))
        values: dict[str, Any] = {}
        extra: list[str] = []
        for raw_key, value in args.items():
            key = _norm_key(raw_key)
            if key in ("session_dir", "out", "output_directory"):
                if session_dir is None:
                    session_dir = value
                continue
            if key in ("binary", "program", "exe", "executable"):
                values["binary"] = value
            elif key in ("ring", "ring_size"):
                values["ring"] = int(value)
            elif key in ("control", "control_file"):
                values["control"] = value
            elif key in ("status", "status_file"):
                values["status"] = value
            elif key in ("groups", "group_count"):
                values["groups"] = int(value)
            elif key in ("timeout", "timeout_seconds"):
                values["timeout_seconds"] = int(value)
            elif key in ("max_seconds", "max_secs"):
                values["max_seconds"] = None if value is None else float(value)
            elif key in ("trigger", "trigger_us", "trigger_period_us"):
                values["trigger_us"] = 0 if str(value).lower() == "off" else int(value)
            elif key in ("laser", "laser_percent"):
                values["laser_percent"] = int(value)
            elif key in ("strobe", "strobe_routes"):
                values["strobe"] = _parse_strobe(value)
            elif key in ("exposure", "exposure_us", "exposures"):
                values["exposure_us"] = _parse_cam_values(value, "exposure")
            elif key in ("gain", "gains"):
                values["gain"] = _parse_cam_values(value, "gain")
            elif key in ("record", "record_lossless", "lossless"):
                values["record"] = normalise_record(value)
            elif key in ("diagnostic", "developer"):
                values["record"] = normalise_record(value)
            elif key in ("storage", "storage_mode", "storage_layout"):
                values["record"] = (normalise_storage_mode(value)
                                    == STORAGE_DIAGNOSTIC)
            elif key in ("backend",):
                values["backend"] = str(value)
            elif key in ("extra_args", "extra"):
                extra.extend(str(a) for a in value)
            elif key in ("poll_interval", "poll_interval_s"):
                values["poll_interval_s"] = float(value)
            elif key in ("escalation_timeout", "escalation_timeout_s"):
                values["escalation_timeout_s"] = float(value)
            elif key in ("queue_depth",):
                values["queue_depth"] = int(value)
            else:
                # tolerate CLI flags passed as a mapping: append them
                flag = "--" + key.replace("_", "-")
                if isinstance(value, bool):
                    if value:
                        extra.append(flag)
                else:
                    extra.extend([flag, str(value)])
        if session_dir is None:
            raise ValueError("capture_args must contain session_dir")
        if "record" not in values:
            # Operator-level opt-in without a GUI change.  Only the mapping
            # path (the wizard/GUI configuration) honours this; the explicit
            # dataclass constructor always states what it wants.
            env = os.environ.get(STORAGE_ENV_VAR)
            if env:
                values["record"] = normalise_record(env)
        return cls(session_dir=_as_path(session_dir), extra_args=tuple(extra),
                   **values)

    # -- command ---------------------------------------------------------
    @property
    def frames_dir(self) -> Path:
        return self.session_dir / "frames"

    @property
    def recording_path(self) -> Path:
        return self.session_dir / "raw.e1r"

    def command(self) -> list[str]:
        """The exact argv (verified safe settings by default).

        Production (the default) uses ``--out`` + ``--status-file``: the tool
        keeps the bounded ``frames/`` ring and writes **no** ``raw.e1r``.
        Diagnostic mode uses ``--session-dir``, which implies the lossless
        streaming recording - that is the 51.98 GB behaviour, now opt-in.
        """
        cmd = [str(self.binary)]
        if self.record:
            cmd += ["--session-dir", str(self.session_dir)]
        else:
            cmd += ["--out", str(self.frames_dir),
                    "--status-file", str(self.status)]
        cmd += ["--control", str(self.control),
                "--exposure-control", str(self.exposure_control),
                "--ring", str(int(self.ring))]
        # --session-dir already supplies DIR/status.json; passing --status-file
        # as well is a usage error in the tool.  Only forward an override.
        default_status = (self.session_dir / "status.json").resolve()
        if (self.record
                and Path(str(self.status)).resolve() != default_status):
            cmd += ["--status-file", str(self.status)]
        cmd += ["--backend", self.backend,
               "--groups", str(int(self.groups))]
        if self.timeout_seconds is not None:
            cmd += ["--timeout-seconds", str(int(self.timeout_seconds))]
        if self.max_seconds is not None:
            cmd += ["--max-seconds", f"{float(self.max_seconds):g}"]
        cmd += ["--trigger",
                "off" if int(self.trigger_us) == 0 else str(int(self.trigger_us))]
        if self.backend == "libusb":
            # illuminator/exposure/gain are device commands: the simulator has
            # no command channel and rejects them.
            cmd += ["--laser", str(int(self.laser_percent))]
            for route, value in self.strobe:
                cmd += ["--strobe", f"{route}:{value}"]
            for cam in sorted(self.exposure_us):
                cmd += ["--exposure-us", f"{cam}:{int(self.exposure_us[cam])}"]
            for cam in sorted(self.gain):
                cmd += ["--gain", f"{cam}:{int(self.gain[cam])}"]
        if self.queue_depth:
            cmd += ["--queue-depth", str(int(self.queue_depth))]
        cmd += list(self.extra_args)
        return cmd

    def as_dict(self) -> dict:
        return {
            "binary": str(self.binary), "session_dir": str(self.session_dir),
            "ring": int(self.ring), "control": str(self.control),
            "status": str(self.status), "groups": int(self.groups),
            "exposure_control": str(self.exposure_control),
            "timeout_seconds": (None if self.timeout_seconds is None
                                else int(self.timeout_seconds)),
            "max_seconds": self.max_seconds,
            "trigger_us": int(self.trigger_us),
            "laser_percent": int(self.laser_percent),
            "strobe": [list(x) for x in self.strobe],
            "exposure_us": {str(k): int(v) for k, v in self.exposure_us.items()},
            "gain": {str(k): int(v) for k, v in self.gain.items()},
            "backend": self.backend,
            "record": bool(self.record),
            "storage_mode": self.storage_mode,
            "extra_args": list(self.extra_args),
        }


def _parse_strobe(value: Any) -> tuple[tuple[int, int], ...]:
    if isinstance(value, Mapping):
        return tuple(sorted((int(k), int(v)) for k, v in value.items()))
    out = []
    if isinstance(value, (list, tuple)):
        for item in value:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                out.append((int(item[0]), int(item[1])))
            else:
                route, _, val = str(item).partition(":")
                out.append((int(route), int(val)))
    else:
        route, _, val = str(value).partition(":")
        out.append((int(route), int(val)))
    return tuple(out)


def _parse_cam_values(value: Any, name: str) -> dict[int, int]:
    if isinstance(value, Mapping):
        return {int(k): int(v) for k, v in value.items()}
    out: dict[int, int] = {}
    if isinstance(value, (list, tuple)):
        for item in value:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                out[int(item[0])] = int(item[1])
            else:
                cam, _, val = str(item).partition(":")
                out[int(cam) if cam else 3] = int(val)
    else:
        out[3] = int(value)
    if 3 in out:  # "value" applies to every camera
        all_value = out.pop(3)
        out.update({cam: all_value for cam in (0, 1, 2)})
    return out


# ---------------------------------------------------------------------------
# process discovery
# ---------------------------------------------------------------------------

def _cmdline(pid: int) -> list[str]:
    try:
        data = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return []
    return [part.decode("utf-8", "replace") for part in data.split(b"\0") if part]


def find_capture_processes(name: str = CAPTURE_TOOL_NAME,
                           *, exclude: Sequence[int] = ()) -> list[dict]:
    """Scan ``/proc`` for running capture processes (empty on non-Linux)."""
    if not os.path.isdir("/proc"):
        return []
    found = []
    skip = set(int(p) for p in exclude) | {os.getpid()}
    try:
        entries = list(os.scandir("/proc"))
    except OSError:
        return []
    for entry in entries:
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid in skip:
            continue
        cmd = _cmdline(pid)
        if not cmd:
            continue
        if Path(cmd[0]).name != name:
            continue
        found.append({"pid": pid, "command": cmd[:8], "session_dir": _option(cmd, "--session-dir")})
    return sorted(found, key=lambda item: item["pid"])


def _option(cmd: Sequence[str], name: str) -> str:
    for i, token in enumerate(cmd[:-1]):
        if token == name:
            return cmd[i + 1]
    return ""


# ---------------------------------------------------------------------------
# supervisor
# ---------------------------------------------------------------------------

class LiveCaptureProcess:
    """One capture process, controlled only through files and signals."""

    def __init__(self, config: CaptureConfig | Mapping[str, Any],
                 *, session_dir: str | Path | None = None) -> None:
        self.config = (config if isinstance(config, CaptureConfig)
                       else CaptureConfig.from_mapping(config,
                                                       session_dir=session_dir))
        self._proc: subprocess.Popen | None = None
        self._lock = threading.RLock()
        self._states: list[dict] = []
        self._last_state: str | None = None
        self.commands: list[dict] = []
        self.exit_code: int | None = None
        self.stop_reason: str | None = None
        self.escalated_sigint = False
        self.stale_lock_removed: dict | None = None
        #: what a reused session directory moved aside on the last start()
        self.previous_output: dict | None = None
        self._token = f"{os.getpid()}-{time.time_ns()}"
        self._stdout_path = self.config.session_dir / "capture.stdout.log"
        self._stderr_path = self.config.session_dir / "capture.stderr.log"
        self._disk_cache: tuple[float, int | None] = (0.0, None)
        self._groups_cache: tuple[float, int | None] = (0.0, None)
        self._closed = False
        self._exposure_current = {int(k): int(v)
                                  for k, v in config.exposure_us.items()}
        self._gain_current = {int(k): int(v) for k, v in config.gain.items()}
        self._heartbeat_stop: threading.Event | None = None
        self._heartbeat_thread: threading.Thread | None = None
        self._safe_off_attempted = False
        self._safe_off_result: dict | None = None
        # -- acquisition evidence (one authoritative record) --------------
        self._started_at: float | None = None
        self._exited_at: float | None = None
        self._stop_requested_at: float | None = None
        self._stop_reason: str | None = None
        self._stop_attempts = 0
        self._final_acquisition: dict | None = None
        self._orphan_cache: tuple[float, list] = (0.0, [])
        self._manifest_cache: tuple[tuple[int, int], dict] = ((0, 0), {})
        self._stderr_cache: tuple[tuple[int, int], dict] = ((0, 0), {})

    # -- simple properties ----------------------------------------------
    @property
    def binary(self) -> Path:
        return self.config.binary

    @property
    def lock_path(self) -> Path:
        return self.config.session_dir / "capture.lock"

    @property
    def command(self) -> list[str]:
        return self.config.command()

    @property
    def process(self) -> subprocess.Popen | None:
        return self._proc

    @property
    def pid(self) -> int | None:
        return None if self._proc is None else int(self._proc.pid)

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def process_exited(self) -> bool:
        return self._proc is not None and self._proc.poll() is not None

    # -- lock ------------------------------------------------------------
    def read_lock(self) -> dict | None:
        doc = types.read_json(self.lock_path, default=None)
        return doc if isinstance(doc, dict) else None

    def lock_is_live(self, lock: Mapping[str, Any] | None = None) -> bool:
        lock = self.read_lock() if lock is None else lock
        if not lock:
            return False
        try:
            pid = lock.get("pid")
            pid = None if pid is None else int(pid)
        except (TypeError, ValueError):
            return False
        if pid is None:
            # window between O_EXCL creation and the child's pid being known
            try:
                started = float(lock.get("started_at", 0.0))
            except (TypeError, ValueError):
                started = 0.0
            return (time.time() - started) < 10.0
        cmd = _cmdline(pid)
        if not cmd:
            return False
        return Path(cmd[0]).name == self.config.binary.name

    def _acquire_lock(self) -> None:
        self.config.session_dir.mkdir(parents=True, exist_ok=True)
        for attempt in range(2):
            payload = json.dumps({
                "pid": None, "binary": str(self.config.binary),
                "token": self._token, "started_at": time.time(),
                "session_dir": str(self.config.session_dir),
            })
            try:
                fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                             0o644)
            except FileExistsError:
                lock = self.read_lock()
                if lock is not None and self.lock_is_live(lock):
                    raise CaptureError(
                        "another einstar-capture-frames holds "
                        f"{self.lock_path} (pid {lock.get('pid')})")
                if lock is None and self.lock_path.exists():
                    # unreadable lock: treat as stale only if no capture process
                    foreign = find_capture_processes(self.config.binary.name)
                    if foreign:
                        raise CaptureError(
                            "cannot verify the capture lock and a capture "
                            f"process is running: {foreign}")
                self.stale_lock_removed = lock or {"path": str(self.lock_path),
                                                   "unreadable": True}
                try:
                    self.lock_path.unlink()
                except OSError as exc:
                    raise CaptureError(f"cannot remove stale lock "
                                       f"{self.lock_path}: {exc}") from exc
                if attempt == 0:
                    continue
                raise CaptureError(f"cannot create lock {self.lock_path}")
            else:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(payload + "\n")
                return

    def _write_lock_pid(self, pid: int) -> None:
        types.atomic_write_json(self.lock_path, {
            "pid": int(pid), "binary": str(self.config.binary),
            "token": self._token, "started_at": time.time(),
            "session_dir": str(self.config.session_dir),
            "command": self.command,
        })

    def _cleanup_lock(self) -> None:
        """Remove *our* lock; never another process's."""
        lock = self.read_lock()
        if not lock:
            return
        if lock.get("token") not in (None, self._token):
            return
        try:
            os.unlink(self.lock_path)
        except OSError:
            pass

    # -- control file ----------------------------------------------------
    def _write_control(self, command: str) -> dict:
        if command not in CONTROL_COMMANDS:
            raise ValueError(f"unknown control command {command!r}")
        path = self.config.control
        assert path is not None
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(command + "\n", encoding="utf-8")
        os.replace(tmp, path)
        record = {"command": command, "at_s": time.time()}
        self.commands.append(record)
        return record

    def set_volatile(self, *, exposure_us: Mapping[int, int] | None = None,
                     gain: Mapping[int, int] | None = None,
                     laser_percent: int | None = None,
                     source: str = "manual") -> dict:
        """Write a volatile exposure/gain/laser change for the capture tool.

        The file is written atomically and the C++ session applies it only while
        running.  Nothing here writes persistent device state; the capture tool
        validates each value against the device range and keeps the previous
        setting if a write is refused.
        """
        if not self.running:
            raise CaptureError("capture is not running; refusing a volatile change")
        path = self.config.exposure_control
        assert path is not None
        lines: list[str] = []
        if exposure_us:
            vals = dict(self._exposure_current)
            vals.update({int(k): int(v) for k, v in exposure_us.items()})
            for cam, us in sorted(vals.items()):
                if cam not in (0, 1, 2) or not 10 <= us <= 1_000_000:
                    raise ValueError(f"bad exposure for camera {cam}: {us}")
            # The C++ parser intentionally requires all three cameras in one
            # block; merge the omitted ones from current state rather than
            # writing a partial block the device would ignore.
            lines.append("exposure " + " ".join(f"{k}:{v}" for k, v in sorted(vals.items())))
        if gain:
            vals = dict(self._gain_current)
            vals.update({int(k): int(v) for k, v in gain.items()})
            for cam, value in sorted(vals.items()):
                if cam not in (0, 1, 2) or not 0 <= value <= 65_535:
                    raise ValueError(f"bad gain for camera {cam}: {value}")
            lines.append("gain " + " ".join(f"{k}:{v}" for k, v in sorted(vals.items())))
        if laser_percent is not None:
            value = int(laser_percent)
            if not 0 <= value <= 100:
                raise ValueError("laser_percent must be 0..100")
            lines.append(f"laser {value}")
        if not lines:
            return {"applied": False, "reason": "no settings"}
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.replace(tmp, path)
        if exposure_us:
            self._exposure_current.update({int(k): int(v)
                                           for k, v in exposure_us.items()})
        if gain:
            self._gain_current.update({int(k): int(v)
                                       for k, v in gain.items()})
        record = {"at_s": time.time(), "source": str(source),
                  "exposure_us": None if not exposure_us else
                  {str(k): int(v) for k, v in exposure_us.items()},
                  "gain": None if not gain else
                  {str(k): int(v) for k, v in gain.items()},
                  "laser_percent": laser_percent}
        self.commands.append(record)
        return record

    # -- lifecycle -------------------------------------------------------
    def _start_heartbeat(self, write_fd: int, proc: subprocess.Popen) -> None:
        """Pipe ownership stays with this thread until it closes its own fd."""
        stop = threading.Event()
        self._heartbeat_stop = stop

        def pulse() -> None:
            try:
                while not stop.is_set() and proc.poll() is None:
                    try:
                        os.write(write_fd, b".")
                    except BlockingIOError:
                        # A full pipe means the child stopped consuming pulses;
                        # never block the GUI on the safety channel.
                        pass
                    except OSError:
                        break
                    stop.wait(0.25)
            finally:
                os.close(write_fd)

        self._heartbeat_thread = threading.Thread(
            target=pulse, name="capture-heartbeat", daemon=True)
        self._heartbeat_thread.start()

    def _stop_heartbeat(self) -> None:
        if self._heartbeat_stop is not None:
            self._heartbeat_stop.set()
        if self._heartbeat_thread is not None:
            self._heartbeat_thread.join(timeout=0.5)
        self._heartbeat_stop = None
        self._heartbeat_thread = None

    # -- previous-run isolation -----------------------------------------
    def _previous_path(self, path: Path) -> Path:
        """A sibling that does not exist yet, stamped with the current UTC."""
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        base = path.with_name(f"{path.name}.previous-{stamp}")
        candidate = base
        index = 2
        while candidate.exists():
            candidate = path.with_name(f"{base.name}-{index}")
            index += 1
        return candidate

    def archive_previous_output(self) -> dict:
        """Move a previous run's frames and raw stream aside; never delete.

        Reusing a session directory is normal operator behaviour (the GUI
        default is a stable path), but the capture tool writes dense frame ids
        from 0.  Overwriting in place would silently mix two physical attempts:
        the wizard would read the old frames as if they were live, and the old
        ``frames_manifest.json`` would supply another run's timestamps and
        device-group ids.  Measured on ``einstar-calibration-fresh-01``: 70 of
        162 accepted views came from the previous attempt, in a second clock
        domain, and the first ~3 minutes of the new attempt were spent
        replaying them.

        Renaming is O(1) on the same filesystem and preserves every byte for
        inspection.  A failure refuses the start: mixing two sessions is worse
        than not starting.
        """
        info: dict[str, Any] = {"archived": [], "reused": False}
        session_dir = self.config.session_dir
        frames = self.config.frames_dir
        if frames.exists():
            try:
                non_empty = next(frames.iterdir(), None) is not None
            except OSError:
                non_empty = True
            if non_empty:
                dest = self._previous_path(frames)
                try:
                    os.replace(frames, dest)
                except OSError as exc:
                    raise CaptureError(
                        f"cannot move the previous frame ring {frames} aside: "
                        f"{exc}") from exc
                info["archived"].append({"kind": "frames", "path": str(frames),
                                         "moved_to": str(dest)})
                info["reused"] = True
        # A diagnostic session keeps raw.e1r in the session root; never let a
        # new run truncate a previous (possibly huge) recording.
        for raw in sorted(session_dir.glob("*.e1r")):
            try:
                size = raw.stat().st_size
            except OSError:
                size = 0
            if size <= 0:
                continue
            dest = self._previous_path(raw)
            try:
                os.replace(raw, dest)
            except OSError as exc:
                raise CaptureError(
                    f"cannot move the previous recording {raw} aside: {exc}"
                ) from exc
            info["archived"].append({"kind": "recording", "path": str(raw),
                                     "moved_to": str(dest),
                                     "bytes": int(size)})
            info["reused"] = True
        # The capture tool applies an existing exposure-control.txt at startup.
        # Leaving the previous attempt's file behind seeds a new recording with
        # its pinned values (fresh-01 started at 2325 us / 20000 us / gain 300
        # instead of the configured baseline).  A stale control.txt can also
        # contain "stop".  Move both with the frames.
        for name in ("exposure-control.txt", "control.txt"):
            path = session_dir / name
            if not path.is_file():
                continue
            dest = self._previous_path(path)
            try:
                os.replace(path, dest)
            except OSError:
                try:
                    path.unlink()
                except OSError:
                    pass
            else:
                info["archived"].append({"kind": name, "path": str(path),
                                         "moved_to": str(dest)})
                info["reused"] = True
        # A stale status.json would describe the previous attempt (typically
        # "stopped"); the reader would then believe the new capture had ended
        # before its first status was written.  Move it with the frames.
        status_path = self.config.status
        if status_path is not None and Path(status_path).is_file():
            dest = self._previous_path(Path(status_path))
            try:
                os.replace(status_path, dest)
            except OSError:
                try:
                    Path(status_path).unlink()
                except OSError:
                    pass
            else:
                info["archived"].append({"kind": "status",
                                         "path": str(status_path),
                                         "moved_to": str(dest)})
                info["reused"] = True
        frames.mkdir(parents=True, exist_ok=True)
        self.previous_output = info
        return info

    def start(self) -> dict:
        """Start one capture process; refuse if any other is alive."""
        with self._lock:
            if self.running:
                raise CaptureError(f"capture already running (pid {self.pid})")
            if not self.config.binary.is_file():
                raise CaptureError(f"capture tool not found: {self.config.binary}")
            if not os.access(self.config.binary, os.X_OK):
                raise CaptureError(f"capture tool is not executable: "
                                   f"{self.config.binary}")
            foreign = find_capture_processes(self.config.binary.name)
            if foreign:
                pids = ", ".join(str(f["pid"]) for f in foreign)
                raise CaptureError(
                    "another einstar-capture-frames is already running "
                    f"(pid {pids}); one capture process at a time")
            self._acquire_lock()
            self.config.session_dir.mkdir(parents=True, exist_ok=True)
            archived = self.archive_previous_output()
            if self.config.control is not None:
                self._write_control("start")
            log_out = self._stdout_path.open("wb")
            log_err = self._stderr_path.open("wb")
            self._stop_heartbeat()
            read_fd, write_fd = os.pipe2(os.O_CLOEXEC | os.O_NONBLOCK)
            environment = os.environ.copy()
            environment["EINSTAR_CAPTURE_PARENT_PID"] = str(os.getpid())
            environment["EINSTAR_CAPTURE_HEARTBEAT_FD"] = str(read_fd)
            try:
                self._proc = subprocess.Popen(
                    self.command, stdin=subprocess.DEVNULL, stdout=log_out,
                    stderr=log_err, cwd=str(self.config.session_dir),
                    start_new_session=True, pass_fds=(read_fd,), env=environment)
            except OSError as exc:
                os.close(write_fd)
                self._cleanup_lock()
                raise CaptureError(f"cannot start capture tool: {exc}") from exc
            finally:
                os.close(read_fd)
                log_out.close()
                log_err.close()
            self._start_heartbeat(write_fd, self._proc)
            self._write_lock_pid(self._proc.pid)
            self._states = []
            self._last_state = None
            self._started_at = time.time()
            self._exited_at = None
            self._stop_requested_at = None
            self._stop_reason = None
            self._stop_attempts = 0
            self._final_acquisition = None
            self._orphan_cache = (0.0, [])
            atexit.register(self._atexit_cleanup)
            return {
                "ok": True, "pid": self._proc.pid, "command": self.command,
                "lock": str(self.lock_path),
                "stale_lock_removed": self.stale_lock_removed,
                "archived_output": archived,
                "stdout": str(self._stdout_path),
                "stderr": str(self._stderr_path),
            }

    def pause(self) -> dict:
        with self._lock:
            if not self.running:
                raise CaptureError("capture is not running")
            record = self._write_control("pause")
            return {"ok": True, "command": "pause", "record": record,
                    "pid": self.pid}

    def resume(self) -> dict:
        with self._lock:
            if not self.running:
                raise CaptureError("capture is not running")
            record = self._write_control("resume")
            return {"ok": True, "command": "resume", "record": record,
                    "pid": self.pid}

    def stop(self, reason: str = "user request",
             timeout: float | None = None) -> dict:
        """Stop via the control file; SIGINT only if that fails within 5 s.

        ``ok`` means the child terminated **and** the acquisition evidence is
        verified (process exit + device readback + recording finalization).
        ``stopped`` reports child termination alone; ``verified``/``reasons``
        carry the authoritative verdict.  A caller must never infer a safe
        scanner from ``stopped`` alone.
        """
        with self._lock:
            self.stop_reason = str(reason)
            if self._stop_requested_at is None:
                self._stop_requested_at = time.time()
            self._stop_reason = str(reason)
            if self._proc is None:
                self._cleanup_lock()
                acq = self.acquisition_state()
                return {"ok": bool(acq.get("verified")), "stopped": True,
                        "already_stopped": True, "reason": reason,
                        "escalated_sigint": False, "exit_code": None,
                        "verified": bool(acq.get("verified")),
                        "acquisition": acq,
                        "reasons": list(acq.get("reasons") or [])}
            if self._proc.poll() is not None:
                self.exit_code = int(self._proc.returncode)
                if self._exited_at is None:
                    self._exited_at = time.time()
                self._cleanup_lock()
                acq = self.acquisition_state()
                return {"ok": bool(acq.get("verified")), "stopped": True,
                        "already_stopped": True, "reason": reason,
                        "escalated_sigint": False, "exit_code": self.exit_code,
                        "verified": bool(acq.get("verified")),
                        "acquisition": acq,
                        "reasons": list(acq.get("reasons") or [])}
            self._stop_attempts += 1
            self._write_control("stop")
            wait_s = (self.config.escalation_timeout_s if timeout is None
                      else float(timeout))
            rc = self.wait(wait_s)
            escalated = False
            if rc is None:
                escalated = True
                self.escalated_sigint = True
                try:
                    self._proc.send_signal(signal.SIGINT)
                except OSError:
                    pass
                rc = self.wait(wait_s)
            still_running = rc is None
            if not still_running:
                self._cleanup_lock()
            acq = self.acquisition_state()
            return {
                "ok": bool(not still_running and acq.get("verified")),
                "stopped": not still_running,
                "already_stopped": False,
                "reason": reason, "exit_code": rc,
                "escalated_sigint": escalated,
                "still_running": still_running,
                "waited_s": wait_s,
                "control_file": str(self.config.control),
                "verified": bool(acq.get("verified")),
                "acquisition": acq,
                "reasons": list(acq.get("reasons") or []),
            }

    def stop_and_verify(self, reason: str = "user request",
                        timeout: float | None = None) -> dict:
        """Stop, then return the authoritative acquisition evidence snapshot."""
        result = self.stop(reason=reason, timeout=timeout)
        acq = self.acquisition_state()
        self._final_acquisition = acq
        result["acquisition"] = acq
        result["verified"] = bool(acq.get("verified"))
        result["safe"] = bool(acq.get("safe"))
        result["reasons"] = list(acq.get("reasons") or [])
        return result

    def wait(self, timeout: float | None = None) -> int | None:
        """Wait for exit; returns the exit code or ``None`` on timeout."""
        proc = self._proc
        if proc is None:
            return self.exit_code
        try:
            code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None
        self.exit_code = int(code)
        if self._exited_at is None:
            self._exited_at = time.time()
        self.poll_states()
        self._cleanup_lock()
        return self.exit_code

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.running:
            try:
                self.stop(reason="supervisor closed")
            except Exception:
                pass
        elif self._proc is not None:
            try:
                self._proc.wait(timeout=0.1)
            except subprocess.TimeoutExpired:
                pass
        # EOF is the fallback if the ordinary control/signal stop did not finish.
        self._stop_heartbeat()
        self._cleanup_lock()
        try:
            self._final_acquisition = self.acquisition_state()
        except Exception:
            pass
        try:
            atexit.unregister(self._atexit_cleanup)
        except Exception:
            pass

    def _atexit_cleanup(self) -> None:
        """Never leave a live process behind with a *stale* lock.

        If the child is still running the lock is accurate and is kept (its pid
        really is alive); a short graceful SIGINT is attempted first.
        """
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.send_signal(signal.SIGINT)
                proc.wait(timeout=2.0)
            except (OSError, subprocess.TimeoutExpired):
                return
        self._cleanup_lock()

    def __enter__(self) -> "LiveCaptureProcess":
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop(reason="context exit")
        self.close()

    # -- status ----------------------------------------------------------
    def _existing_dir(self) -> Path:
        path = self.config.session_dir
        while not path.exists() and path.parent != path:
            path = path.parent
        return path

    def disk_free_gb(self) -> float | None:
        now = time.monotonic()
        stamp, value = self._disk_cache
        if now - stamp < 2.0 and value is not None:
            return float(value) / 1e9
        try:
            free = shutil.disk_usage(self._existing_dir()).free
        except OSError:
            return None
        self._disk_cache = (now, int(free))
        return free / 1e9

    def groups_on_disk(self) -> int | None:
        """Number of complete triplets currently in the ring directory."""
        now = time.monotonic()
        stamp, value = self._groups_cache
        if now - stamp < 0.5 and value is not None:
            return value
        try:
            count = 0
            with os.scandir(self.config.frames_dir) as entries:
                for entry in entries:
                    if entry.name.startswith("frame_") and entry.name.endswith("_cam1.pgm"):
                        count += 1
        except OSError:
            return None
        self._groups_cache = (now, int(count))
        return count

    def status(self) -> dict:
        """Normalised view of ``status.json`` (never raises)."""
        path = self.config.status
        assert path is not None
        raw = types.read_json(path, default=None)
        raw = raw if isinstance(raw, dict) else {}
        state = raw.get("state")
        if not isinstance(state, str):
            if raw.get("running") is False or raw.get("stopped") is True:
                state = "stopped"
            elif self._proc is not None:
                state = "stopped" if self._proc.poll() is not None else "running"
            elif raw:
                state = "unknown"
            else:
                state = "starting"
        elapsed = raw.get("elapsed_s")
        groups_written = raw.get("groups_written", 0)
        fps = raw.get("fps")
        if fps is None and isinstance(elapsed, (int, float)) and elapsed > 0:
            fps = float(groups_written) / float(elapsed)
        drops = raw.get("drops", raw.get("frames_dropped", 0))
        laser = raw.get("laser_percent")
        if laser is None and isinstance(raw.get("illuminator"), dict):
            laser = raw["illuminator"].get("laser_percent")
        trigger_counts = raw.get("trigger_counts")
        if trigger_counts is None and "trigger_counts" in raw:
            trigger_counts = raw.get("trigger_counts")
        try:
            age = time.time() - path.stat().st_mtime if path.exists() else None
        except OSError:
            age = None
        return {
            "schema": raw.get("schema"),
            "state": state,
            "stop_reason": raw.get("stop_reason"),
            "fps": None if fps is None else float(fps),
            "groups_written": int(groups_written or 0),
            "groups_requested": raw.get("groups_requested"),
            "last_group": raw.get("latest_group", raw.get("last_group")),
            "drops": int(drops or 0),
            "throughput_mbps": raw.get("throughput_mbps"),
            "disk_free_gb": self.disk_free_gb(),
            "groups_on_disk": self.groups_on_disk(),
            "ring_size": int(self.config.ring),
            "laser_percent": laser,
            "trigger_counts": trigger_counts,
            "trigger_period_us": raw.get("trigger_period_us"),
            "trigger_on": raw.get("trigger_on"),
            "exposure_us": raw.get("exposure_us"),
            "gain": raw.get("gain"),
            # Shutdown evidence is part of the public status contract; before
            # this it existed only in the raw document and the controller
            # therefore always reported ``cleanup_verified: None``.
            "cleanup_attempted": raw.get("cleanup_attempted"),
            "cleanup_verified": raw.get("cleanup_verified"),
            "frames_completed": raw.get("frames_completed"),
            "frames_dropped": raw.get("frames_dropped"),
            "bytes_received": raw.get("bytes_received"),
            "bytes_written": raw.get("bytes_written"),
            "simulated": raw.get("simulated"),
            "live": raw.get("live"),
            "elapsed_s": elapsed,
            "age_s": age,
            "path": str(path),
            "available": bool(raw),
            "pid": self.pid,
            "process_running": self.running,
            "raw": raw,
        }

    def poll_states(self) -> list[dict]:
        """Record every observed state transition; return the history."""
        status = self.status()
        state = status.get("state")
        if state != self._last_state:
            self._last_state = state
            self._states.append({
                "state": state,
                "stop_reason": status.get("stop_reason"),
                "at_s": time.time(),
                "groups_written": status.get("groups_written"),
                "laser_percent": status.get("laser_percent"),
                "trigger_counts": status.get("trigger_counts"),
            })
        return list(self._states)

    @property
    def state_history(self) -> list[dict]:
        return list(self._states)

    def state_summary(self) -> dict:
        status = self.status()
        return {"state": status["state"], "pid": self.pid,
                "running": self.running, "groups_written": status["groups_written"],
                "stop_reason": status["stop_reason"]}

    @property
    def final_acquisition(self) -> dict | None:
        """The last authoritative evidence captured by :meth:`close`."""
        return dict(self._final_acquisition or {}) or None

    # -- authoritative acquisition evidence ------------------------------
    def _stderr_evidence(self) -> dict:
        path = self._stderr_path
        try:
            stat = path.stat()
        except OSError:
            return {"path": str(path), "available": False, "tail": "",
                    "deadline_exceeded": False, "cleanup_unverified": False,
                    "lines": []}
        key = (stat.st_mtime_ns, stat.st_size)
        if key == self._stderr_cache[0]:
            return dict(self._stderr_cache[1])
        try:
            with path.open("rb") as handle:
                size = stat.st_size
                handle.seek(max(0, size - 8192))
                text = handle.read().decode("utf-8", "replace")
        except OSError:
            text = ""
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        out = {
            "path": str(path),
            "available": bool(size),
            "tail": text[-4096:],
            "lines": lines[-8:],
            "deadline_exceeded": "cleanup deadline exceeded" in text,
            "cleanup_unverified": "UNVERIFIED" in text,
        }
        self._stderr_cache = (key, out)
        return dict(out)

    def _manifest_evidence(self) -> dict:
        path = self.config.frames_dir / "frames_manifest.json"
        try:
            stat = path.stat()
        except OSError:
            return {"path": str(path), "present": False, "valid": False,
                    "bytes": 0, "error": "not present"}
        key = (stat.st_mtime_ns, stat.st_size)
        cached = self._manifest_cache
        if cached[0] == key and cached[1]:
            return dict(cached[1])
        info = {"path": str(path), "present": True, "valid": False,
                "bytes": int(stat.st_size), "error": ""}
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
            info["valid"] = isinstance(doc, dict)
            if not info["valid"]:
                info["error"] = "manifest is not a JSON object"
        except (OSError, ValueError) as exc:
            info["error"] = f"{type(exc).__name__}: {exc}"
        self._manifest_cache = (key, info)
        return dict(info)

    def _orphan_processes(self) -> list[dict]:
        """Other capture processes still attached to *this* session."""
        now = time.monotonic()
        stamp, cached = self._orphan_cache
        if now - stamp < 1.0 and cached:
            return [dict(item) for item in cached]
        if self.running:
            # Our own child is alive; it is not an orphan.  Skip the /proc walk
            # on the 10 Hz diagnostics path.
            return []
        session = self.config.session_dir.resolve()
        found: list[dict] = []
        try:
            for item in find_capture_processes(self.config.binary.name,
                                               exclude=[os.getpid()]):
                raw = item.get("session_dir") or ""
                try:
                    same = bool(raw) and Path(raw).expanduser().resolve() == session
                except (OSError, RuntimeError):
                    same = False
                if same:
                    found.append(dict(item))
        except Exception:
            found = []
        self._orphan_cache = (now, found)
        return [dict(item) for item in found]

    def _device_evidence(self, status: Mapping[str, Any]) -> dict:
        raw = status.get("raw") or {}
        simulated = bool(raw.get("simulated")) or self.config.backend == "simulated"
        attempted = status.get("cleanup_attempted")
        verified = status.get("cleanup_verified")
        counts = status.get("trigger_counts")
        laser = status.get("laser_percent")
        trigger_ok = False
        counts_normalised: dict | None = None
        if isinstance(counts, Mapping):
            try:
                counts_normalised = {str(k): int(v) for k, v in counts.items()}
                trigger_ok = all(v == 0 for v in counts_normalised.values()) \
                    and set(counts_normalised) >= {"mono", "rgb"}
            except (TypeError, ValueError):
                trigger_ok = False
        illuminator_ok = laser is not None and int(laser) == 0
        reasons: list[str] = []
        if simulated:
            hardware_ok = verified is True
            if not hardware_ok:
                reasons.append("simulated cleanup was not verified")
            # The simulator has no hardware trigger/illuminator registers.
            trigger_ok = True
            illuminator_ok = laser in (0, None)
        else:
            hardware_ok = (attempted is True and verified is True
                           and trigger_ok and illuminator_ok)
            if attempted is not True:
                reasons.append("device cleanup was never attempted")
            elif verified is not True:
                reasons.append("device cleanup readback is unverified")
            if not trigger_ok:
                if counts is None:
                    reasons.append("trigger state is unavailable in the status file")
                else:
                    reasons.append("trigger counts are not zero: "
                                   f"{counts!r}")
            if not illuminator_ok:
                if laser is None:
                    reasons.append("laser state is unavailable in the status file")
                else:
                    reasons.append(f"laser is {laser}%, expected 0%")
        return {
            "simulated": simulated,
            "cleanup_attempted": attempted,
            "cleanup_verified": verified,
            "hardware_verified": bool(hardware_ok),
            "trigger_verified": bool(trigger_ok),
            "trigger_counts": counts_normalised if counts_normalised
                              is not None else counts,
            "trigger_period_us": status.get("trigger_period_us"),
            "illuminator_verified": bool(illuminator_ok),
            "laser_percent": laser,
            "reasons": reasons,
            "source": str(self.config.status),
        }

    def _recording_evidence(self, status: Mapping[str, Any]) -> dict:
        path = self.config.recording_path
        manifest = self._manifest_evidence()
        trailer = read_recording_trailer(path)
        try:
            raw_bytes = int(path.stat().st_size)
        except OSError:
            raw_bytes = 0
        groups = int(status.get("groups_written") or 0)
        requested = bool(self.config.record)
        status_state = status.get("state")
        if not requested:
            # Production sessions do not record the raw stream at all: there
            # is nothing to finalise and no trailer to demand.  A leftover
            # raw.e1r from an older run is reported, never trusted.
            return {
                "path": str(path), "bytes": raw_bytes,
                "requested": False,
                "storage_mode": self.config.storage_mode,
                "groups_written": groups,
                "needs_finalization": False,
                "finalized": status_state == "stopped",
                "status_state": status_state,
                "manifest": manifest,
                "trailer": trailer,
                "trailer_bytes_match": None,
                "stale_raw_present": bool(raw_bytes),
                "reasons": [],
                "note": ("lossless .e1r recording was not requested (production "
                         "storage mode); accepted views are stored compressed"),
            }
        needs_finalization = raw_bytes > 0 and (groups > 0 or trailer.get("present"))
        trailer_bytes = trailer.get("trailer_bytes")
        bytes_received = status.get("bytes_received")
        bytes_match = None
        if isinstance(trailer_bytes, int) and isinstance(bytes_received, (int, float)):
            bytes_match = int(trailer_bytes) == int(bytes_received)
        finalized = bool(needs_finalization
                         and status_state == "stopped"
                         and manifest.get("valid")
                         and trailer.get("present"))
        if not needs_finalization:
            finalized = status_state == "stopped"
        reasons: list[str] = []
        if needs_finalization:
            if status_state != "stopped":
                reasons.append(f"final status is {status_state!r}, not 'stopped'")
            if not manifest.get("valid"):
                reasons.append(
                    "recording finalization is unverified: "
                    f"{manifest.get('path')} is missing or invalid")
            if not trailer.get("present"):
                reasons.append("recording trailer is missing or truncated")
            if bytes_match is False:
                reasons.append(
                    "recording trailer byte count does not match status "
                    f"bytes_received ({trailer_bytes} vs {bytes_received})")
        return {
            "path": str(path),
            "bytes": raw_bytes,
            "requested": True,
            "storage_mode": self.config.storage_mode,
            "groups_written": groups,
            "needs_finalization": bool(needs_finalization),
            "finalized": bool(finalized),
            "status_state": status_state,
            "manifest": manifest,
            "trailer": trailer,
            "trailer_bytes_match": bytes_match,
            "reasons": reasons,
        }

    def acquisition_state(self, *, now: float | None = None) -> dict:
        """The one authoritative acquisition verdict (see class docstring).

        Cheap enough for the 10 Hz diagnostics path: one small status read, a
        72-byte recording tail read, a cached manifest check and (only when the
        child is gone) a rate-limited orphan scan.  Never raises.
        """
        now = time.time() if now is None else float(now)
        status = self.status()
        raw = status.get("raw") or {}
        state_raw = status.get("state")
        proc_started = self._proc is not None
        proc_alive = bool(self.running)
        if proc_started and not proc_alive and self._exited_at is None:
            self._exited_at = now
        stop_requested = self._stop_requested_at is not None
        stop_age = (None if self._stop_requested_at is None
                    else max(0.0, now - self._stop_requested_at))
        exit_code = self.exit_code
        if exit_code is None and proc_started and not proc_alive \
                and self._proc is not None:
            try:
                exit_code = self._proc.returncode
            except Exception:
                exit_code = None
        exit_signal = _signal_name(exit_code)
        stderr = self._stderr_evidence()
        device = self._device_evidence(status)
        recording = self._recording_evidence(status)
        orphans = self._orphan_processes()
        lock_live = self.lock_is_live()
        terminated = bool((proc_alive is False)
                          and (proc_started or not lock_live))
        if orphans:
            terminated = False
        reasons: list[str] = []
        conflicts: list[str] = []
        notes: list[str] = []
        terminal = False

        if proc_alive:
            if stop_requested:
                state = AcquisitionState.STOPPING
                if stop_age is not None and stop_age > (
                        self.config.escalation_timeout_s + 2.0):
                    reasons.append(
                        f"capture process is still running {stop_age:.0f} s "
                        "after the stop request")
            elif state_raw in ("stopping", "stopped"):
                state = AcquisitionState.STOPPING
                reasons.append(
                    f"final status says {state_raw!r} but the capture process "
                    "is still running")
            elif state_raw == "paused":
                state = AcquisitionState.PAUSED
            elif state_raw == "starting":
                state = AcquisitionState.STARTING
            elif state_raw == "running":
                state = AcquisitionState.RUNNING
            elif self._started_at is not None and now - self._started_at < 5.0:
                state = AcquisitionState.STARTING
            else:
                state = AcquisitionState.RUNNING
            reasons.append(
                f"capture process is still running (pid {self.pid})")
            if state in (AcquisitionState.RUNNING, AcquisitionState.STARTING,
                         AcquisitionState.PAUSED):
                age = status.get("age_s")
                if isinstance(age, (int, float)) and age > 5.0:
                    reasons.append(
                        f"status file has not been updated for {age:.1f} s")
        elif not status.get("available") and not proc_started and not orphans:
            state = AcquisitionState.IDLE
        elif state_raw == "stopped":
            terminal = True
            if (terminated and device.get("hardware_verified")
                    and recording.get("finalized") and not orphans):
                state = AcquisitionState.STOPPED_VERIFIED
            else:
                state = AcquisitionState.STOPPING_UNVERIFIED
        elif stop_requested or state_raw in ("stopping",):
            terminal = True
            state = AcquisitionState.STOPPING_UNVERIFIED
        elif not proc_started and state_raw in ("running", "starting", "paused"):
            terminal = True
            state = AcquisitionState.FAILED
            reasons.append(
                "no live capture process owns this session; the last status "
                f"({state_raw!r}) is stale")
        elif proc_started:
            terminal = True
            state = AcquisitionState.FAILED
        elif status.get("available"):
            terminal = True
            state = AcquisitionState.STOPPING_UNVERIFIED
        else:
            state = AcquisitionState.IDLE

        # ---- the three required evidence groups --------------------------
        if not terminated:
            if not proc_alive:
                reasons.append("capture process termination is not evidenced")
        if state == AcquisitionState.FAILED and exit_code is not None:
            reasons.insert(
                0, f"capture process exited unexpectedly (exit {exit_code}"
                   + (f", {exit_signal}" if exit_signal else "") + ")")
        if not device.get("hardware_verified") and not proc_alive:
            reasons.extend(device.get("reasons") or [])
        if not recording.get("finalized") and not proc_alive:
            if state_raw != "stopped":
                reasons.append(f"state is {state_raw!r}, expected 'stopped'")
            reasons.extend(recording.get("reasons") or [])
        if orphans:
            reasons.append(
                "capture process(es) still running for this session: "
                + ", ".join(str(item.get("pid")) for item in orphans))
        if state == AcquisitionState.STOPPING_UNVERIFIED and not proc_alive \
                and proc_started and not stop_requested \
                and state_raw not in ("stopping", "stopped"):
            reasons.insert(0, "capture process exited without a stop request")
        # verified requires every group, regardless of how we got here
        verified = bool(
            terminal
            and state not in (AcquisitionState.FAILED,)
            and terminated
            and device.get("hardware_verified")
            and recording.get("finalized")
            and not orphans
            and state_raw == "stopped")
        if verified:
            state = AcquisitionState.STOPPED_VERIFIED
            reasons = []
        elif state in (AcquisitionState.STOPPED_VERIFIED,):
            state = AcquisitionState.STOPPING_UNVERIFIED

        # ---- supporting notes and contradictions -------------------------
        if device.get("hardware_verified"):
            if device.get("simulated"):
                notes.append("simulated pipeline shutdown verified "
                             "(no hardware trigger/illuminator)")
            else:
                notes.append(
                    "device cleanup readback published: trigger "
                    f"{device.get('trigger_counts')}, illuminator "
                    f"{device.get('laser_percent')} %")
        trailer = recording.get("trailer") or {}
        if not recording.get("requested", True):
            notes.append(
                "lossless .e1r recording was not requested (production storage "
                "mode); there is no recording trailer to verify")
        elif recording.get("stale_raw_present"):
            notes.append(
                "a raw.e1r file exists although production storage was "
                "selected; it is stale and was not used as evidence")
        if trailer.get("present"):
            notes.append(
                "recording trailer present (CRC-valid): "
                f"{trailer.get('trailer_transfers')} transfers, "
                f"{trailer.get('trailer_bytes')} bytes; full transfer digest "
                "not recomputed by the parent")
        if exit_code is not None:
            notes.append(f"capture child exit {exit_code}"
                         + (f" ({exit_signal})" if exit_signal else ""))
        if stderr.get("deadline_exceeded"):
            message = "process safety: cleanup deadline exceeded; " \
                      "illuminator state UNVERIFIED; forcing SIGKILL"
            conflicts.append(
                "capture.stderr.log reports the process-safety cleanup "
                "deadline was exceeded and the child was SIGKILLed")
            if recording.get("finalized") and device.get("hardware_verified"):
                conflicts.append(
                    "cleanup readback was verified and the recording trailer "
                    "was written before the SIGKILL; the kill interrupted final "
                    "status/manifest publication")
        if device.get("cleanup_verified") is True and not device.get(
                "hardware_verified") and not device.get("simulated"):
            conflicts.append(
                "status.json says cleanup_verified=true but the trigger/"
                "illuminator readback is missing or nonzero")
        if state_raw == "stopped" and proc_alive:
            conflicts.append("status.json says stopped while the child is alive")
        if trailer.get("present") and not recording.get(
                "manifest", {}).get("valid"):
            conflicts.append(
                "the .e1r trailer is present but frames_manifest.json is "
                "absent/invalid; recording finalization is incomplete")
        if recording.get("trailer_bytes_match") is False:
            conflicts.append("trailer byte count disagrees with status "
                             "bytes_received")

        return AcquisitionEvidence(
            state=state.value,
            state_label=_STATE_LABELS[state],
            terminal=bool(terminal),
            verified=bool(verified),
            safe=bool(verified),
            session_dir=str(self.config.session_dir),
            updated_at_s=now,
            child={
                "pid": self.pid,
                "running": proc_alive,
                "started": proc_started,
                "started_at_s": self._started_at,
                "exited_at_s": self._exited_at,
                "exit_code": exit_code,
                "exit_signal": exit_signal,
                "lock_live": bool(lock_live),
                "orphans": [int(item.get("pid")) for item in orphans
                            if item.get("pid") is not None],
            },
            device=device,
            recording=recording,
            stop={
                "requested": bool(stop_requested),
                "reason": self._stop_reason,
                "at_s": self._stop_requested_at,
                "age_s": stop_age,
                "attempts": int(self._stop_attempts),
                "escalated_sigint": bool(self.escalated_sigint),
            },
            status={
                "available": bool(status.get("available")),
                "state": state_raw,
                "stop_reason": status.get("stop_reason"),
                "path": status.get("path"),
                "age_s": status.get("age_s"),
                "fps": status.get("fps"),
                "groups_written": status.get("groups_written"),
                "bytes_written": status.get("bytes_written"),
                "laser_percent": status.get("laser_percent"),
                "trigger_counts": status.get("trigger_counts"),
                "cleanup_attempted": status.get("cleanup_attempted"),
                "cleanup_verified": status.get("cleanup_verified"),
            },
            watchdog={
                "stderr_path": stderr.get("path"),
                "deadline_exceeded": bool(stderr.get("deadline_exceeded")),
                "cleanup_unverified_warning": bool(
                    stderr.get("cleanup_unverified")),
                "last_lines": list(stderr.get("lines") or []),
            },
            orphans=[int(item.get("pid")) for item in orphans
                     if item.get("pid") is not None],
            reasons=list(dict.fromkeys(str(r) for r in reasons if r)),
            conflicts=list(dict.fromkeys(str(c) for c in conflicts if c)),
            evidence_notes=notes,
        ).to_dict()

    # -- safe final state ------------------------------------------------
    def emergency_safe_off(self, *, timeout_s: float = 15.0) -> dict:
        """Best-effort trigger/illuminator off after the child died.

        A SIGKILLed capture child cannot run its own cleanup.  The parent opens
        the scanner through the same executable in a dedicated volatile-only
        mode after the child has exited, stops the trigger and turns the laser
        and strobes off, then verifies the readback.  This is not a substitute
        for a normal stop and does not claim optical darkness.
        """
        if self._safe_off_attempted:
            return dict(self._safe_off_result or {})
        self._safe_off_attempted = True
        if self.config.backend != "libusb":
            self._safe_off_result = {"attempted": False,
                                     "ok": True,
                                     "reason": "simulated backend"}
            return dict(self._safe_off_result)
        if not self.config.binary.is_file():
            self._safe_off_result = {"attempted": False, "ok": False,
                                     "reason": "capture binary missing"}
            return dict(self._safe_off_result)
        cmd = [str(self.config.binary), "--backend", "libusb", "--safe-off"]
        try:
            proc = subprocess.run(cmd, stdin=subprocess.DEVNULL,
                                  capture_output=True, text=True,
                                  timeout=float(timeout_s),
                                  cwd=str(self.config.session_dir))
            self._safe_off_result = {
                "attempted": True,
                "ok": proc.returncode == 0,
                "returncode": int(proc.returncode),
                "stdout": proc.stdout[-2000:],
                "stderr": proc.stderr[-2000:],
                "command": cmd,
            }
        except Exception as exc:
            self._safe_off_result = {
                "attempted": True, "ok": False,
                "reason": f"{type(exc).__name__}: {exc}", "command": cmd,
            }
        return dict(self._safe_off_result)

    def check_safe_final_state(self) -> dict:
        """Assert the full shutdown contract from recorded evidence.

        ``ok`` requires **all** of: child termination, device cleanup readback
        (trigger 0/0, illuminator 0 % for live; simulated shutdown for the
        simulator) and recording finalization (final ``stopped`` status; when a
        lossless ``.e1r`` was requested, additionally a valid
        ``frames/frames_manifest.json`` and a CRC-valid trailer).  A
        ``cleanup_verified: true`` field alone is not enough: the observed
        52 GB session had exactly that field while the process-safety watchdog
        SIGKILLed the child before the frames manifest was written.

        A production session has no ``.e1r`` and therefore nothing to
        finalise; its ``recording.requested`` is ``False`` and the compressed
        views are verified by ``tools.wizard.session_io`` instead.
        """
        status = self.status()
        acquisition = self.acquisition_state()
        device = acquisition.get("device") or {}
        recording = acquisition.get("recording") or {}
        reasons = list(acquisition.get("reasons") or [])
        counts = status.get("trigger_counts")
        laser = status.get("laser_percent")
        state = status.get("state")
        simulated = bool(device.get("simulated"))
        note = ""
        if simulated:
            note = ("simulated backend: trigger/illuminator are not hardware "
                    "controls; verified simulated shutdown instead")
        safe_off = dict(self._safe_off_result or {})
        if (not acquisition.get("verified") and not device.get("hardware_verified")
                and self.process_exited()):
            # The child is gone but never verified darkness; recover the
            # volatile device state from the parent.  This is recovery
            # evidence, not a substitute for the missing final status.
            recovery = self.emergency_safe_off()
            safe_off = dict(recovery)
            if recovery.get("ok"):
                note = (note + "; " if note else "") + (
                    "emergency safe-off readback verified after the child "
                    "exited without a verified final status")
            else:
                reasons.append("child exited without a verified device state; "
                               "emergency safe-off failed")
        return {
            "ok": bool(acquisition.get("verified")),
            "verified": bool(acquisition.get("verified")),
            "safe": bool(acquisition.get("safe")),
            "simulated": simulated,
            "note": note,
            "state": state,
            "stop_reason": status.get("stop_reason"),
            "trigger_counts": counts,
            "trigger_on": status.get("trigger_on"),
            "trigger_period_us": status.get("trigger_period_us"),
            "laser_percent": laser,
            "reasons": list(dict.fromkeys(str(r) for r in reasons if r)),
            "conflicts": list(acquisition.get("conflicts") or []),
            "status_path": str(self.config.status),
            "exit_code": acquisition.get("child", {}).get("exit_code"),
            "exit_signal": acquisition.get("child", {}).get("exit_signal"),
            "escalated_sigint": self.escalated_sigint,
            "commands": list(self.commands),
            "states": self.poll_states(),
            "process_running": self.running,
            "recording_finalized": bool(recording.get("finalized")),
            "recording": recording,
            "trailer": recording.get("trailer"),
            "acquisition_state": acquisition.get("state"),
            "acquisition": acquisition,
            "safe_off": safe_off,
        }
