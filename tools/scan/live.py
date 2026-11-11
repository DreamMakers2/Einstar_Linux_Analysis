"""Live scanning source: own the capture process, follow its frames.

The scanning application must be able to scan *while the scanner is capturing*,
without a second process owning the USB device.  This module starts the existing
capture supervisor (exactly one process, the same one the calibration wizard
uses), follows the frames manifest it keeps rewriting, and yields frame ids as
complete groups appear.

Safety is inherited from the supervisor: stop() sends the control-file `stop`,
waits for the process to exit and verifies the safe final state (trigger off,
illuminator off).  A scanning session never talks to USB directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import time
from typing import Iterator

from wizard import capture as capture_mod


@dataclass
class CaptureSource:
    session_dir: Path
    capture_binary: Path
    backend: str = "libusb"
    ring: int = 120
    trigger_us: int = 100_000
    laser_percent: int = 100
    poll_s: float = 0.2
    startup_timeout_s: float = 20.0
    extra_args: tuple[str, ...] = ()

    frames_dir: Path = field(init=False)
    process: capture_mod.LiveCaptureProcess | None = field(default=None, init=False)
    _seen: set[int] = field(default_factory=set, init=False)
    _stopped: bool = field(default=False, init=False)
    last_error: str = ""
    started_at: float = 0.0

    def __post_init__(self) -> None:
        self.session_dir = Path(self.session_dir)
        self.capture_binary = Path(self.capture_binary)
        self.frames_dir = self.session_dir / "frames"

    # -- lifecycle --------------------------------------------------------
    def start(self) -> dict:
        config = capture_mod.CaptureConfig(
            session_dir=self.session_dir,
            binary=self.capture_binary,
            backend=self.backend,
            ring=int(self.ring),
            groups=0,
            trigger_us=int(self.trigger_us),
            laser_percent=int(self.laser_percent),
            extra_args=tuple(self.extra_args))
        self.process = capture_mod.LiveCaptureProcess(config)
        state = self.process.start()
        self.started_at = time.time()
        deadline = time.time() + float(self.startup_timeout_s)
        while time.time() < deadline:
            status = self._status()
            if str(status.get("state")) in {"running", "recording"}:
                return {"started": True, "state": status.get("state"),
                        "frames_dir": str(self.frames_dir)}
            if self.process is not None and self.process.process_exited():
                break
            time.sleep(0.1)
        self.last_error = "capture did not start"
        return {"started": False, "error": self.last_error}

    def _status(self) -> dict:
        if self.process is None:
            return {}
        try:
            return self.process.status() or {}
        except Exception:
            return {}

    def stop(self) -> dict:
        self._stopped = True
        out: dict = {"stopped": False}
        if self.process is None:
            return out
        try:
            self.process.stop()
        except Exception as exc:
            out["error"] = f"{type(exc).__name__}: {exc}"
        try:
            code = self.process.wait(30.0)
            out["exit"] = code
        except Exception as exc:
            out["wait_error"] = f"{type(exc).__name__}: {exc}"
        try:
            out["safe"] = self.process.check_safe_final_state()
            out["status"] = self.process.status()
        except Exception as exc:
            out["safe_error"] = f"{type(exc).__name__}: {exc}"
        out["stopped"] = True
        return out

    # -- frames -----------------------------------------------------------
    def _manifest_groups(self) -> list[int]:
        """Complete frame ids currently on disk.

        The capture tool writes the frames manifest only when it finalises, so a
        live session must follow the frame files themselves.  Three camera files
        for the same id means the group is complete.
        """
        pattern = re.compile(r"^frame_(\d+)_cam(\d)\.pgm$")
        found: dict[int, set[int]] = {}
        try:
            names = os.listdir(self.frames_dir)
        except OSError:
            return []
        for name in names:
            match = pattern.match(name)
            if not match:
                continue
            found.setdefault(int(match.group(1)), set()).add(int(match.group(2)))
        return sorted(fid for fid, cams in found.items() if {0, 1, 2} <= cams)

    def _files_ready(self, frame_id: int) -> bool:
        return all((self.frames_dir / f"frame_{frame_id:06d}_cam{c}.pgm").is_file()
                   for c in (0, 1, 2))

    def ids(self, *, max_frames: int | None = None,
            timeout_s: float | None = None,
            poll_s: float | None = None) -> Iterator[int]:
        """Yield frame ids as complete groups appear, until stopped or capped."""
        poll = float(poll_s if poll_s is not None else self.poll_s)
        deadline = None if timeout_s is None else time.time() + float(timeout_s)
        emitted = 0
        while not self._stopped:
            if deadline is not None and time.time() > deadline:
                return
            for frame_id in self._manifest_groups():
                if self._stopped:
                    return
                if frame_id in self._seen or not self._files_ready(frame_id):
                    continue
                self._seen.add(frame_id)
                emitted += 1
                yield frame_id
                if max_frames is not None and emitted >= int(max_frames):
                    return
            time.sleep(poll)

    # -- status -----------------------------------------------------------
    def snapshot(self) -> dict:
        status = self._status()
        return {"state": status.get("state"),
                "stop_reason": status.get("stop_reason"),
                "groups_written": status.get("groups_written"),
                "groups_on_disk": status.get("groups_on_disk"),
                "frames_seen": len(self._seen),
                "elapsed_s": round(time.time() - self.started_at, 1)
                if self.started_at else 0.0}
