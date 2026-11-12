"""Supervisor for the C++ live-capture process.

The wizard never speaks USB.  It spawns exactly **one**
``build/driver/einstar-capture-frames`` and controls it through the control
file defined in ``docs/calibration/wizard_architecture.md`` §5:

* ``--session-dir DIR``  standard layout (``raw.e1r``, ``frames/``,
  ``status.json``, ``frames_manifest.json``);
* ``--control PATH``     atomically written command file (``start`` |
  ``pause`` | ``resume`` | ``stop``; last line wins);
* ``--ring N``           keep only the newest N complete groups on disk;
* ``--status-file PATH`` live status JSON (``einstar-live-status-v1`` plus the
  wizard extension fields).

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
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import threading
import time
from typing import Any, Mapping, Sequence

from . import types

__all__ = [
    "CaptureConfig",
    "LiveCaptureProcess",
    "CaptureError",
    "SAFE_SETTINGS",
    "DEFAULT_BINARY",
    "CAPTURE_TOOL_NAME",
    "CONTROL_COMMANDS",
    "find_capture_processes",
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


class CaptureError(RuntimeError):
    """The supervisor refused or failed an operation."""


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
        self._validate()

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
        """The exact argv (verified safe settings by default)."""
        cmd = [str(self.binary),
               "--session-dir", str(self.session_dir),
               "--control", str(self.control),
               "--exposure-control", str(self.exposure_control),
               "--ring", str(int(self.ring))]
        # --session-dir already supplies DIR/status.json; passing --status-file
        # as well is a usage error in the tool.  Only forward an override.
        default_status = (self.session_dir / "status.json").resolve()
        if Path(str(self.status)).resolve() != default_status:
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
            self.config.frames_dir.mkdir(parents=True, exist_ok=True)
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
            atexit.register(self._atexit_cleanup)
            return {
                "ok": True, "pid": self._proc.pid, "command": self.command,
                "lock": str(self.lock_path),
                "stale_lock_removed": self.stale_lock_removed,
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
        """Stop via the control file; SIGINT only if that fails within 5 s."""
        with self._lock:
            self.stop_reason = str(reason)
            if self._proc is None:
                self._cleanup_lock()
                return {"ok": True, "already_stopped": True, "reason": reason,
                        "escalated_sigint": False, "exit_code": None}
            if self._proc.poll() is not None:
                self.exit_code = int(self._proc.returncode)
                self._cleanup_lock()
                return {"ok": True, "already_stopped": True, "reason": reason,
                        "escalated_sigint": False, "exit_code": self.exit_code}
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
            return {
                "ok": not still_running, "already_stopped": False,
                "reason": reason, "exit_code": rc,
                "escalated_sigint": escalated,
                "still_running": still_running,
                "waited_s": wait_s,
                "control_file": str(self.config.control),
            }

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
        """Assert the documented shutdown invariants from the last status.

        Expected after a graceful stop: ``state == "stopped"``, every trigger
        count 0 (or the trigger off) and ``laser_percent == 0``.  Missing
        fields are *failures*, not passes: an unverified illuminator is not
        evidence that it is off.
        """
        status = self.status()
        reasons: list[str] = []
        raw = status.get("raw", {})
        if raw.get("cleanup_attempted") and not raw.get("cleanup_verified"):
            reasons.append("device cleanup readback is unverified")
        state = status.get("state")
        if state != "stopped":
            reasons.append(f"state is {state!r}, expected 'stopped'")
            # The child may have been killed before it could publish a stopped
            # status.  Once it is gone, recover the device from the parent.
            if self.process_exited():
                recovery = self.emergency_safe_off()
                if recovery.get("ok"):
                    reasons.append("child exited without final status; "
                                   "emergency safe-off readback verified")
                else:
                    reasons.append("child exited without final status; "
                                   "emergency safe-off failed")
        counts = status.get("trigger_counts")
        if isinstance(counts, dict) and counts:
            nonzero = {k: v for k, v in counts.items()
                       if isinstance(v, (int, float)) and int(v) != 0}
            if nonzero:
                reasons.append(f"trigger counts are not zero: {nonzero}")
        elif status.get("trigger_on") is True:
            reasons.append("trigger is still on")
        elif status.get("trigger_period_us") not in (None, 0):
            reasons.append(f"trigger period is {status['trigger_period_us']} us, "
                           "expected 0/off")
        elif counts is None and status.get("trigger_on") is None \
                and status.get("trigger_period_us") is None:
            reasons.append("trigger state is unavailable in the status file")
        laser = status.get("laser_percent")
        if laser is None:
            reasons.append("laser state is unavailable in the status file")
        elif int(laser) != 0:
            reasons.append(f"laser is {laser}%, expected 0%")
        if self.running:
            reasons.append("capture process is still running")
        elif self._proc is not None and self.exit_code not in (0, None):
            reasons.append(f"capture process exited with {self.exit_code}")
        simulated = bool(status.get("raw", {}).get("simulated")) or \
            self.config.backend == "simulated"
        note = ""
        if simulated:
            # the simulated transport has no trigger/laser hardware; the safe
            # final state there is a graceful exit and no live process.
            hardware = [r for r in reasons
                        if r.startswith(("trigger", "laser state", "laser is"))]
            reasons = [r for r in reasons if r not in hardware]
            if hardware:
                note = ("simulated backend: trigger/illuminator are not "
                        "hardware controls; verified graceful stop instead")
        return {
            "ok": not reasons,
            "simulated": simulated,
            "note": note,
            "state": state,
            "stop_reason": status.get("stop_reason"),
            "trigger_counts": counts,
            "trigger_on": status.get("trigger_on"),
            "trigger_period_us": status.get("trigger_period_us"),
            "laser_percent": laser,
            "reasons": reasons,
            "status_path": str(self.config.status),
            "exit_code": self.exit_code,
            "escalated_sigint": self.escalated_sigint,
            "commands": list(self.commands),
            "states": self.poll_states(),
            "process_running": self.running,
            "safe_off": dict(self._safe_off_result or {}),
        }
