"""Frame sources for the calibration wizard.

Every source produces :class:`tools.wizard.types.FrameGroup` objects with the
images **already oriented** for the calibration engine:

* ``left``  = ``frame_%06d_cam1.pgm`` as written by the capture tool;
* ``right`` = ``frame_%06d_cam0.pgm`` rotated 180°;
* ``tex``   = ``frame_%06d_cam2.pgm`` debayered (BayerRG → BGR) and rotated
  180°, then reduced to grey for detection (the colour image is kept in
  ``FrameGroup.colour_bgr`` for the GUI).

No source re-implements a rotation: ``types.orient_left``, ``types.orient_right``
and ``types.debayer_bgr`` are the only orientation code paths.

Sources
-------
``ReplayDirSource``    a directory written by ``einstar-capture-frames`` or
                       ``einstar-replay-frames`` (dense ``frame_%06d`` ids +
                       optional ``frames_manifest.json``);
``RecordingSource``    a ``.e1r`` raw USB recording, replayed through the C++
                       tool into a private temporary directory;
``LiveSource``         one supervised ``einstar-capture-frames`` process whose
                       ring directory is followed group by group;
``SyntheticSource``    rendered plates with known truth for tests and the GUI's
                       demo mode (configurable visibility, occlusion, blur,
                       noise and even a mirrored/mis-assigned marker pattern).

Determinism
-----------
``ReplayDirSource``, ``RecordingSource`` and ``SyntheticSource`` are
deterministic: the same input yields bit-identical ``FrameGroup.images``.  A
live capture is not (``LiveSource.deterministic`` is ``False``).

See ``docs/calibration/wizard_architecture.md`` §3 and §5.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import glob
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
import time
from typing import Any, Iterable, Iterator, Mapping, Sequence

import numpy as np

from . import types

try:  # optional supervisor (same package); imported lazily by LiveSource too
    from . import capture as _capture
except Exception:  # pragma: no cover - capture.py is always shipped with this file
    _capture = None


__all__ = [
    "SourceError",
    "CaptureError",
    "SyntheticSpec",
    "ReplayDirSource",
    "RecordingSource",
    "LiveSource",
    "SyntheticSource",
    "render_plate_view",
    "write_replay_frames",
    "read_frames_manifest",
    "DEFAULT_REPLAY_BINARY",
    "DEFAULT_CAPTURE_BINARY",
]

#: Repository root (``.../Einstar_Linux``).
REPO_ROOT = Path(__file__).resolve().parents[2]

#: C++ replay tool, relative to the repository root.
DEFAULT_REPLAY_BINARY = REPO_ROOT / "build" / "driver" / "einstar-replay-frames"
DEFAULT_CAPTURE_BINARY = REPO_ROOT / "build" / "driver" / "einstar-capture-frames"

_FRAME_RE = re.compile(r"^frame_(\d{6,})_cam([0-2])\.pgm$")


class SourceError(RuntimeError):
    """A frame source could not be created, probed or iterated."""


class CaptureError(RuntimeError):
    """The live capture supervisor refused or failed an operation."""


# ---------------------------------------------------------------------------
# directory / manifest helpers
# ---------------------------------------------------------------------------

@dataclass
class FramesManifest:
    """The subset of ``frames_manifest.json`` the wizard needs."""

    path: Path | None = None
    schema: str = ""
    tool: str = ""
    backend: str = ""
    serial: str = ""
    firmware: str = ""
    width: int = types.IMAGE_WIDTH
    height: int = types.IMAGE_HEIGHT
    device_group: dict[int, int] = field(default_factory=dict)
    host_time_ns: dict[int, int] = field(default_factory=dict)
    slot_ids: dict[int, dict[str, int]] = field(default_factory=dict)
    incomplete: dict[int, list[int]] = field(default_factory=dict)
    groups_written: int = 0
    cadence_fps: float | None = None
    cadence_min_ms: float | None = None
    cadence_max_ms: float | None = None
    error: str = ""

    def note(self) -> str:
        bits = [f"manifest={self.path.name if self.path else 'absent'}"]
        if self.backend:
            bits.append(f"backend={self.backend}")
        if self.groups_written:
            bits.append(f"groups={self.groups_written}")
        if self.incomplete:
            bits.append(f"incomplete={len(self.incomplete)}")
        if self.error:
            bits.append(f"manifest_error={self.error}")
        return ", ".join(bits)


def read_frames_manifest(directory: str | Path) -> FramesManifest:
    """Parse ``frames_manifest.json``; never raises (records ``error``)."""
    directory = Path(directory)
    path = directory / "frames_manifest.json"
    out = FramesManifest()
    doc = types.read_json(path, default=None)
    if not isinstance(doc, dict):
        if path.exists():
            out.error = "unreadable JSON"
        return out
    out.path = path
    out.schema = str(doc.get("schema", ""))
    out.tool = str(doc.get("tool", ""))
    out.backend = str(doc.get("backend", ""))
    frame = doc.get("frame") or {}
    try:
        out.width = int(frame.get("width", types.IMAGE_WIDTH))
        out.height = int(frame.get("height", types.IMAGE_HEIGHT))
    except (TypeError, ValueError):
        pass
    device = doc.get("device") or {}
    out.serial = str(device.get("serial", "") or "")
    out.firmware = str(device.get("firmware", "") or "")
    try:
        out.groups_written = int(doc.get("groups_written", 0) or 0)
    except (TypeError, ValueError):
        out.groups_written = 0
    cadence = doc.get("cadence") or {}
    for key, attr in (("fps", "cadence_fps"), ("mean_ms", None),
                      ("min_ms", "cadence_min_ms"), ("max_ms", "cadence_max_ms")):
        value = cadence.get(key)
        if isinstance(value, (int, float)) and attr:
            setattr(out, attr, float(value))
    for entry in doc.get("groups") or []:
        try:
            gid = int(entry.get("group"))
        except (TypeError, ValueError, AttributeError):
            continue
        dg = entry.get("device_group")
        if isinstance(dg, (int, float)):
            out.device_group[gid] = int(dg)
        ns = entry.get("host_time_ns")
        if isinstance(ns, (int, float)):
            out.host_time_ns[gid] = int(ns)
        slots = entry.get("slot_ids")
        if isinstance(slots, dict):
            out.slot_ids[gid] = {str(k): int(v) for k, v in slots.items()
                                 if isinstance(v, (int, float))}
    for entry in doc.get("incomplete_groups") or []:
        try:
            gid = int(entry.get("device_group"))
        except (TypeError, ValueError, AttributeError):
            continue
        present = entry.get("present")
        if isinstance(present, list):
            out.incomplete[gid] = [int(x) for x in present]
    return out


def manifest_is_continuous(manifest: FramesManifest) -> bool:
    """Whether a frames manifest describes a continuous ~10 Hz stream.

    A vendor still collection has no manifest and is not continuous; a live
    capture or a replay of one has a 5-15 fps cadence.  What matters for the
    motion gate is whether consecutive groups are *time-adjacent samples of one
    session*, not whether the host timestamps are tidy: a jittery recording is
    still a continuous capture and must keep the gate.  Measured with the
    failure matrix: an irregular 10 Hz manifest was classified as a still
    collection, which silently bypassed the motion gate entirely.  Only a
    sequence with no usable per-group timestamps (a still collection) or a
    genuinely slow cadence is non-continuous now.
    """
    fps = manifest.cadence_fps
    if fps is not None:
        if fps < 5.0:
            return False
        # jitter is not a reason to drop the motion gate: a 4x cadence ratio is
        # still a live capture, only a slow one (< 5 fps) is a still collection
        return True
    stamps = sorted(manifest.host_time_ns.values())
    if len(stamps) < 10:
        return False
    deltas = np.diff(np.asarray(stamps, dtype=np.float64)) / 1e9
    deltas = deltas[deltas > 0]
    if len(deltas) < 8:
        return False
    median = float(np.median(deltas))
    if not 0.04 <= median <= 0.5:
        return False
    # timestamps exist and the median cadence is live: treat it as continuous
    # even if the host timestamps are jittery (the gate uses the measured speed)
    return True



def scan_frame_files(directory: str | Path) -> dict[int, dict[int, Path]]:
    """Map dense group id -> {stream index: path} for a frame directory.

    A key exists as soon as *one* camera file is visible; callers decide
    whether a group is complete.  Parsing is tolerant of extra files.
    """
    out: dict[int, dict[int, Path]] = {}
    try:
        entries = list(os.scandir(directory))
    except OSError:
        return out
    for entry in entries:
        match = _FRAME_RE.match(entry.name)
        if match is None or not entry.is_file():
            continue
        group_id, camera = int(match.group(1)), int(match.group(2))
        out.setdefault(group_id, {})[camera] = Path(entry.path)
    return out


def _group_images(files: Mapping[int, Path]) -> tuple[dict[str, np.ndarray],
                                                     np.ndarray | None] | None:
    """Read and orient one complete group; ``None`` if any file is unusable.

    All three PGM files must exist and parse.  A partially written directory is
    therefore never turned into a ``FrameGroup``; the caller retries/skips.
    """
    if not all(cam in files for cam in (0, 1, 2)):
        return None
    try:
        raw0 = types.read_pgm(files[0])
        raw1 = types.read_pgm(files[1])
        raw2 = types.read_pgm(files[2])
    except (OSError, ValueError, IndexError):
        return None
    if raw0.ndim != 2 or raw1.ndim != 2 or raw2.ndim != 2:
        return None
    if raw0.shape != raw1.shape or raw0.shape != raw2.shape:
        return None
    if min(raw0.shape) < 16:
        return None
    bgr = types.debayer_bgr(raw2)
    images = {
        "left": types.orient_left(raw1),
        "right": types.orient_right(raw0),
        "tex": types.grey_from_bgr(bgr),
    }
    return images, bgr


def _make_group(*, index: int, file_index: int, device_group: int | None,
                timestamp_s: float, images: Mapping[str, np.ndarray],
                colour_bgr: np.ndarray | None, source: str,
                slot_ids: Mapping[str, int] | None = None,
                label: str = "", continuous: bool = True) -> types.FrameGroup:
    return types.FrameGroup(
        index=int(index),
        device_group=device_group,
        timestamp_s=float(timestamp_s),
        images=dict(images),
        slot_ids=dict(slot_ids or {}),
        complete=True,
        source=source,
        label=label or f"frame {file_index:06d}",
        colour_bgr=colour_bgr,
        continuous=bool(continuous),
    )


# ---------------------------------------------------------------------------
# replay directory
# ---------------------------------------------------------------------------

class ReplayDirSource:
    """Replay a directory of ``frame_%06d_cam{0,1,2}.pgm`` triplets.

    ``start`` skips the first ``start`` groups (by dense file id), ``limit``
    caps the number of yielded groups and ``loop`` restarts after the last
    group.  Incomplete groups are skipped and recorded in ``skipped``; a
    directory that is still being written gets a few short retries before the
    group is skipped, so a reader never crashes on a partial write.

    The source is deterministic: identical directories yield identical images.
    """

    kind = types.SOURCE_REPLAY
    deterministic = True

    #: how many times an incomplete group is re-checked (concurrent writer)
    retry_attempts = 3
    retry_delay_s = 0.04

    def __init__(self, frames_dir: str | Path, *, start: int = 0,
                 limit: int | None = None, loop: bool = False) -> None:
        self.frames_dir = Path(frames_dir)
        if not self.frames_dir.is_dir():
            raise SourceError(f"frames directory does not exist: {self.frames_dir}")
        if start < 0:
            raise ValueError("start must be >= 0")
        if limit is not None and limit < 0:
            raise ValueError("limit must be >= 0 or None")
        self.start = int(start)
        self.limit = None if limit is None else int(limit)
        self.loop = bool(loop)
        self._closed = False
        self._next_index = 0
        self._pass = 0
        self._t0_ns: int | None = None
        self.skipped: list[dict] = []
        self._manifest = read_frames_manifest(self.frames_dir)
        self.continuous = manifest_is_continuous(self._manifest)
        self.info = self._build_info()

    # -- info ------------------------------------------------------------
    def _build_info(self) -> types.SourceInfo:
        m = self._manifest
        return types.SourceInfo(
            kind=types.SOURCE_REPLAY,
            path=str(self.frames_dir),
            serial=m.serial,
            firmware=m.firmware,
            width=m.width,
            height=m.height,
            note=m.note(),
            continuous=bool(self.continuous),
        )

    def refresh_info(self) -> types.SourceInfo:
        self._manifest = read_frames_manifest(self.frames_dir)
        self.continuous = manifest_is_continuous(self._manifest)
        self.info = self._build_info()
        return self.info

    # -- reading ---------------------------------------------------------
    def _timestamp(self, file_index: int) -> float:
        ns = self._manifest.host_time_ns.get(file_index)
        if ns is None:
            return 0.1 * file_index
        if self._t0_ns is None:
            self._t0_ns = ns
        return (ns - self._t0_ns) / 1e9

    def _load(self, file_index: int) -> types.FrameGroup | None:
        files = scan_frame_files(self.frames_dir).get(file_index, {})
        if len(files) < 3:
            return None
        parsed = _group_images(files)
        if parsed is None:
            return None
        images, bgr = parsed
        return _make_group(
            index=self._next_index, file_index=file_index,
            device_group=self._manifest.device_group.get(file_index),
            timestamp_s=self._timestamp(file_index), images=images,
            colour_bgr=bgr, source=types.SOURCE_REPLAY,
            slot_ids=self._manifest.slot_ids.get(file_index),
            continuous=bool(self.continuous),
        )

    def _group_ids(self) -> list[int]:
        return sorted(scan_frame_files(self.frames_dir))

    def _load_with_retry(self, file_index: int) -> types.FrameGroup | None:
        """A group is only yielded once all three PGMs exist and parse."""
        for attempt in range(self.retry_attempts):
            group = self._load(file_index)
            if group is not None:
                return group
            if attempt + 1 < self.retry_attempts:
                time.sleep(self.retry_delay_s)
        return None


    def __iter__(self) -> Iterator[types.FrameGroup]:
        while not self._closed:
            ids = self._group_ids()[self.start:]
            if self.limit is not None:
                ids = ids[: self.limit]
            for file_index in ids:
                if self._closed:
                    return
                try:
                    group = self._load_with_retry(file_index)
                except Exception as exc:
                    # A truncated or unreadable frame must not end the replay:
                    # record it and carry on with the next group (measured: one
                    # corrupt PGM previously stopped iteration after it).
                    self.skipped.append({"file_index": int(file_index),
                                         "reason": f"load failed: "
                                                   f"{type(exc).__name__}: {exc}",
                                         "pass": self._pass})
                    continue
                if group is None:
                    self.skipped.append({"file_index": int(file_index),
                                         "reason": "incomplete or unreadable group",
                                         "pass": self._pass})
                    continue
                self._next_index += 1
                yield group
            if not self.loop or self._closed:
                return
            self._pass += 1
            if not ids:
                time.sleep(0.05)

    # -- lifecycle -------------------------------------------------------
    def close(self) -> None:
        self._closed = True

    def __enter__(self) -> "ReplayDirSource":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


# ---------------------------------------------------------------------------
# .e1r recording
# ---------------------------------------------------------------------------

class RecordingSource(ReplayDirSource):
    """Replay a ``.e1r`` recording by shelling out to ``einstar-replay-frames``.

    The C++ tool verifies the recording (header CRC, per-record CRC, trailer
    digest) *before* it writes anything, so a successful probe is the
    verification result.  Its stdout/stderr/return code are reported in
    :attr:`probe_result`; :attr:`info` carries a one-line summary.

    Nothing is re-framed in Python: the produced directory is read with the
    same code path as :class:`ReplayDirSource`.
    """

    kind = types.SOURCE_REPLAY
    deterministic = True

    def __init__(self, recording_path: str | Path, *, max_groups: int | None = None,
                 binary: str | Path | None = None, keep_temporary: bool = False,
                 start: int = 0, limit: int | None = None,
                 loop: bool = False) -> None:
        self.recording_path = Path(recording_path)
        if not self.recording_path.is_file():
            raise SourceError(f"recording does not exist: {self.recording_path}")
        if max_groups is not None and max_groups < 0:
            raise ValueError("max_groups must be >= 0 or None")
        self.binary = Path(binary) if binary is not None else DEFAULT_REPLAY_BINARY
        self.max_groups = max_groups
        self.keep_temporary = bool(keep_temporary)
        self.probe_result: dict[str, Any] | None = None
        self._probe_lock = threading.Lock()
        self._tmp_root: Path | None = None
        self._inner: ReplayDirSource | None = None
        self._closed = False
        self.start = int(start)
        self.limit = None if limit is None else int(limit)
        self.loop = bool(loop)
        self._next_index = 0
        self._pass = 0
        self.skipped = []
        self._manifest = FramesManifest()
        self.continuous = True          # a .e1r is a continuous USB stream
        self.info = types.SourceInfo(kind=types.SOURCE_REPLAY,
                                     path=str(self.recording_path),
                                     note="recording not probed yet",
                                     continuous=True)

    # -- probe -----------------------------------------------------------
    def probe(self) -> dict:
        """Run the replay tool once and return the verification report."""
        with self._probe_lock:
            if self.probe_result is not None:
                return self.probe_result
            if not self.binary.is_file():
                raise SourceError(f"replay tool not found: {self.binary}")
            if not os.access(self.binary, os.X_OK):
                raise SourceError(f"replay tool is not executable: {self.binary}")
            self._tmp_root = Path(tempfile.mkdtemp(prefix="einstar-replay-"))
            cmd = [str(self.binary), "--in", str(self.recording_path),
                   "--out", str(self._tmp_root), "--manifest"]
            if self.max_groups is not None:
                cmd += ["--max-groups", str(int(self.max_groups))]
            t0 = time.time()
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True,
                                      timeout=1800)
                rc, out, err = proc.returncode, proc.stdout, proc.stderr
            except (OSError, subprocess.SubprocessError) as exc:
                shutil.rmtree(self._tmp_root, ignore_errors=True)
                raise SourceError(f"replay tool failed to run: {exc}") from exc
            elapsed = time.time() - t0
            manifest = read_frames_manifest(self._tmp_root)
            groups = len(scan_frame_files(self._tmp_root))
            result = {
                "ok": rc == 0,
                "verified": rc == 0,
                "returncode": rc,
                "command": cmd,
                "seconds": round(elapsed, 3),
                "groups": groups,
                "manifest": manifest.note(),
                "schema": manifest.schema,
                "stdout_tail": out[-4000:],
                "stderr_tail": err[-4000:],
                "recording": str(self.recording_path),
                "temporary_directory": str(self._tmp_root),
                "error": None if rc == 0 else (err.strip().splitlines()[-1]
                                               if err.strip() else f"exit {rc}"),
            }
            self.probe_result = result
            if rc != 0 or groups == 0:
                shutil.rmtree(self._tmp_root, ignore_errors=True)
                self._tmp_root = None
                raise SourceError(
                    f"recording replay failed (exit {rc}): {result['error']}")
            self._manifest = manifest
            self._inner = ReplayDirSource(
                self._tmp_root, start=self.start, limit=self.limit,
                loop=self.loop)
            self.info = types.SourceInfo(
                kind=types.SOURCE_REPLAY, path=str(self.recording_path),
                serial=manifest.serial, firmware=manifest.firmware,
                width=manifest.width, height=manifest.height,
                note=(f"verified recording; {groups} groups; "
                      f"replay exit 0 in {elapsed:.1f} s"),
                continuous=True)
            return result

    # -- reading ---------------------------------------------------------
    @property
    def frames_dir(self) -> Path | None:
        return self._tmp_root

    def __iter__(self) -> Iterator[types.FrameGroup]:
        self.probe()
        assert self._inner is not None
        for group in self._inner:
            if self._closed:
                return
            self._next_index += 1
            yield replace(group, index=self._next_index - 1)
        self.skipped.extend(self._inner.skipped)

    def close(self) -> None:
        self._closed = True
        if self._inner is not None:
            self._inner.close()
        if self._tmp_root is not None and not self.keep_temporary:
            shutil.rmtree(self._tmp_root, ignore_errors=True)
            self._tmp_root = None


# ---------------------------------------------------------------------------
# live capture
# ---------------------------------------------------------------------------

class LiveSource:
    """Follow one supervised ``einstar-capture-frames`` ring directory.

    The source never speaks USB: it spawns exactly one capture process through
    :class:`tools.wizard.capture.LiveCaptureProcess`, then reads complete
    triplets from ``SESSION/frames`` as the process writes them.  ``pause()``,
    ``resume()`` and ``stop()`` write the control file atomically through the
    supervisor; ``stop()`` waits for the process and verifies the safe final
    state (trigger off, illuminator 0).

    Not deterministic by construction (``deterministic`` is ``False``).
    """

    kind = types.SOURCE_LIVE
    deterministic = False

    def __init__(self, session_dir: str | Path, *,
                 capture_args: Mapping[str, Any] | Any,
                 profile: Any | None = None) -> None:
        if _capture is None:  # pragma: no cover - capture.py is shipped in-package
            raise SourceError("tools.wizard.capture is unavailable")
        self.session_dir = Path(session_dir)
        self.profile = profile
        self.frames_dir = self.session_dir / "frames"
        config = _capture.CaptureConfig.from_mapping(
            capture_args, session_dir=self.session_dir)
        self.capture = _capture.LiveCaptureProcess(config)
        self.poll_interval_s = float(config.poll_interval_s)
        self.final_verdict: dict | None = None
        self._started = False
        self._closed = False
        self._stop_requested = False
        self._seen: set[int] = set()
        self._next_index = 0
        self._manifest = FramesManifest()
        self._manifest_mtime = None

    # -- info ------------------------------------------------------------
    def _refresh_manifest(self) -> None:
        path = self.frames_dir / "frames_manifest.json"
        try:
            stat = path.stat()
        except OSError:
            return
        key = (stat.st_mtime_ns, stat.st_size)
        if key != self._manifest_mtime:
            self._manifest = read_frames_manifest(self.frames_dir)
            self._manifest_mtime = key

    @property
    def info(self) -> types.SourceInfo:
        self._refresh_manifest()
        return types.SourceInfo(
            kind=types.SOURCE_LIVE, path=str(self.session_dir),
            serial=self._manifest.serial, firmware=self._manifest.firmware,
            width=self._manifest.width, height=self._manifest.height,
            note=("live capture; " + self._manifest.note()),
            continuous=True)

    # -- lifecycle -------------------------------------------------------
    def start(self) -> dict:
        if self._started:
            return self.capture.state_summary()
        self.frames_dir.mkdir(parents=True, exist_ok=True)
        self._started = True
        return self.capture.start()

    def pause(self) -> dict:
        return self.capture.pause()

    def resume(self) -> dict:
        return self.capture.resume()

    def stop(self, reason: str = "user request") -> dict:
        """Stop the capture, wait for exit and verify the safe final state."""
        self._stop_requested = True
        self.capture.stop(reason=reason)
        verdict = self.capture.check_safe_final_state()
        self.final_verdict = verdict
        return verdict

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._started and self.capture.running:
            try:
                self.stop(reason="source closed")
            except Exception:
                pass
        self.capture.close()

    def __enter__(self) -> "LiveSource":
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- reading ---------------------------------------------------------
    def __iter__(self) -> Iterator[types.FrameGroup]:
        if not self._started:
            self.start()
        while not self._closed:
            self._refresh_manifest()
            files = scan_frame_files(self.frames_dir)
            for gid in sorted(files):
                if gid in self._seen:
                    continue
                if len(files[gid]) < 3:
                    continue
                parsed = _group_images(files[gid])
                if parsed is None:
                    continue  # still being written; retry next poll
                images, bgr = parsed
                self._seen.add(gid)
                ns = self._manifest.host_time_ns.get(gid)
                ts = (ns / 1e9) if ns is not None else time.time()
                group = _make_group(
                    index=self._next_index, file_index=gid,
                    device_group=self._manifest.device_group.get(gid),
                    timestamp_s=ts, images=images, colour_bgr=bgr,
                    source=types.SOURCE_LIVE,
                    slot_ids=self._manifest.slot_ids.get(gid),
                    continuous=True)
                self._next_index += 1
                yield group
            status = self.capture.status()
            if status.get("state") == "stopped" and self.capture.process_exited():
                # one final drain so files written just before exit are not lost
                files = scan_frame_files(self.frames_dir)
                for gid in sorted(files):
                    if gid in self._seen or len(files[gid]) < 3:
                        continue
                    parsed = _group_images(files[gid])
                    if parsed is None:
                        continue
                    images, bgr = parsed
                    self._seen.add(gid)
                    yield _make_group(
                        index=self._next_index, file_index=gid,
                        device_group=self._manifest.device_group.get(gid),
                        timestamp_s=self._manifest.host_time_ns.get(gid, 0) / 1e9,
                        images=images, colour_bgr=bgr,
                        source=types.SOURCE_LIVE, continuous=True)
                    self._next_index += 1
                return
            if self._stop_requested:
                return
            time.sleep(self.poll_interval_s)


# ---------------------------------------------------------------------------
# synthetic rendering
# ---------------------------------------------------------------------------

#: ``board_pose`` returns a guidance-frame pose whose board +y already points
#: down the image (matching ``calib.adaptive.board_pose``).  Rendering with the
#: plain pose produced a *mirrored* lattice homography (det < 0), so the
#: camera-frame pose is conjugated once here; this is the same normalisation
#: ``calib.guidance.evaluate_frame`` applies to detector poses.
_BOARD_TO_CAMERA = np.diag([1.0, -1.0, -1.0])


@dataclass(frozen=True)
class SyntheticSpec:
    """Deterministic recipe for a rendered three-camera scene.

    Poses are ``(distance_mm, tilt_x_deg, tilt_y_deg, roll_deg)`` tuples in the
    guidance convention (see ``calib.adaptive.board_pose``); ``poses`` may be
    supplied explicitly, otherwise ``pattern`` generates them.

    Failure cases are first-class: ``visible_markers`` and ``drop_uv`` hide
    markers, ``drop_by_camera`` hides markers in one camera only,
    ``occlusion`` paints a background rectangle over part of the plate,
    ``marker_layout='mirror_u'`` renders a *mirrored* large-marker pattern (the
    plate seen from behind: same marker count, wrong chirality), and
    ``blur_sigma`` / ``noise_sigma`` / ``saturation_frac`` degrade the image.
    """

    n_views: int = 12
    seed: int = 0
    pitch_mm: float = 28.0
    pattern: str = "diverse"              # diverse | distance | tilt | single
    distance_mm: float = 400.0
    tilt_deg: tuple[float, float] = (0.0, 0.0)
    roll_deg: float = 0.0
    lateral_mm: tuple[float, float] = (0.0, 0.0)
    poses: Sequence[Any] | None = None
    visible_markers: int | None = None    # keep only the first N lattice markers
    drop_uv: Sequence[tuple[float, float]] = ()
    drop_by_camera: Mapping[str, int] = field(default_factory=dict)
    occlusion: str = "none"               # none | left | right | top | bottom
    occlusion_frac: float = 0.45
    marker_layout: str = "identity"       # identity | mirror_u | mirror_v | rot180
    blur_sigma: float = 0.0
    noise_sigma: float = 2.0
    saturation_frac: float = 0.0
    marker_radius_px: float | None = None  # None = derive from projected pitch
    big_scale: float = 2.4
    background: int = 20
    plate_grey: int = 52
    plate_texture: float = 55.0     # broad illumination field on the plate
    #: fine texture that is a property of the PLATE SURFACE (not of one image):
    #: it is generated once on a millimetre grid and sampled by ray-casting each
    #: plate pixel onto the board, so every camera sees the same pattern and a
    #: stereo matcher has something real to lock onto.  Amplitude in grey
    #: levels, 0 = off (then the per-image speckle above is used).
    surface_texture: float = 0.0
    surface_texture_seed: int = 20261112
    surface_texture_cell_mm: float = 1.5
    marker_grey: int = 245
    big_marker_grey: int = 255
    cameras: Mapping[str, Any] | None = None   # CameraIntrinsics per key
    extrinsics: Mapping[str, tuple] | None = None
    factory_perturb_px: float = 0.0       # deterministic offset for demo mode
    frames_per_pose: int = 1              # >1 emulates a dwell (motion gate)
    loop: bool = False
    source_label: str = ""


def _default_poses(spec: SyntheticSpec) -> list[tuple[float, float, float, float]]:
    """Generate the pose list as ``(distance, tilt_x, tilt_y, roll)``.

    The ``diverse`` pattern is ordered so that a prefix already covers the
    four pose dimensions of the coverage spec: three distance bands, the four
    out-of-plane tilt quadrants, all four roll sectors and the image corners
    (via lateral offsets).  This is what makes a synthetic rehearsal able to
    reach READY; a pure random scene cannot.
    """
    if spec.pattern == "single":
        return [(spec.distance_mm, spec.tilt_deg[0], spec.tilt_deg[1],
                 spec.roll_deg)]
    if spec.pattern == "distance":
        return [(300.0 + 220.0 * (i / max(spec.n_views - 1, 1)), 0.0, 0.0, 0.0)
                for i in range(int(spec.n_views))]
    if spec.pattern == "tilt":
        out = []
        for i in range(int(spec.n_views)):
            tx = 22.0 * np.sin(2.0 * np.pi * i / max(spec.n_views, 1))
            ty = 22.0 * np.cos(2.0 * np.pi * i / max(spec.n_views, 1))
            out.append((spec.distance_mm, float(tx), float(ty),
                        float((90.0 * i) % 360.0)))
        return out
    if spec.pattern != "diverse":
        raise ValueError(f"unknown synthetic pattern {spec.pattern!r}; "
                         "choose diverse, distance, tilt or single")
    distances = (340.0, 420.0, 500.0)
    quadrant = ((1, 1), (-1, 1), (1, -1), (-1, -1))
    out = []
    n = int(spec.n_views)
    # phase A (first 12): distance bands x the four out-of-plane tilt
    # quadrants, roll 0 so the measured signs stay the intended ones.
    # 16 deg clears the coverage minimum (15 deg) with a small margin.  Larger
    # tilts (20-28 deg) foreshorten the plate so strongly for the toed-in right
    # camera that its large-marker area ratio collapses and the orientation
    # witness fails - measured: identity score 0/4 at (+20,+20) for 340-420 mm.
    tilt = max(16.0, float(getattr(spec, "tilt_min_deg", 16.0)))
    for i in range(min(n, 12)):
        d = distances[i % len(distances)]
        sx, sy = quadrant[(i // 3) % 4]
        out.append((d, sx * tilt, sy * tilt, 0.0))
    # phase B (4): in-plane roll sectors, fronto-parallel at mid distance.
    for i in range(12, min(n, 16)):
        out.append((420.0, 0.0, 0.0, float((90 * (i - 12)) % 360)))
    # phase C (rest): image-plane corners and edges via lateral offsets.  The
    # offsets move the plate centre into the 3x3 regions; the plate stays fully
    # visible so the orientation witness remains verifiable.
    for i in range(16, n):
        d = distances[i % len(distances)]
        sx = 1.0 if i % 2 else -1.0
        sy = 1.0 if (i // 2) % 2 else -1.0
        out.append((d, sx * 12.0, sy * 10.0, float((90 * i) % 360)))
    return out


def _lateral_offsets(spec: SyntheticSpec, count: int) -> list[tuple[float, float]]:
    """Lateral board-centre offsets that put the plate into image corners.

    Phase A/B views (index < 16) are centred; the corner phase uses the four
    diagonal offsets that move the plate centre into the 3x3 image corners.
    """
    if spec.lateral_mm != (0.0, 0.0):
        return [tuple(spec.lateral_mm)] * count
    corners = ((88.0, 68.0), (-88.0, 68.0), (88.0, -68.0), (-88.0, -68.0),
               (0.0, 68.0), (0.0, -68.0))
    out = []
    for i in range(count):
        out.append((0.0, 0.0) if i < 16 else corners[(i - 16) % len(corners)])
    return out


def _surface_texture(K, kc, R, t, mask, spec, hu, hv, pitch, plate_mod):
    """Deterministic texture fixed to the plate surface.

    Every pixel inside the plate quad is cast back onto the board plane, so the
    grey level depends on *board coordinates* rather than on the image.  Left
    and right cameras therefore see the same pattern, which is what a stereo
    matcher needs -- per-image speckle cannot be matched and reconstructs as
    noise.
    """
    h, w = mask.shape
    uu, vv = np.mgrid[0:h, 0:w]
    pix = np.stack([uu[mask > 0], vv[mask > 0]], axis=1).astype(np.float64)
    fx, fy = float(K[0, 0]), float(K[1, 1])
    cx, cy = float(K[0, 2]), float(K[1, 2])
    rays = np.stack([(pix[:, 0] - cx) / fx, (pix[:, 1] - cy) / fy,
                     np.ones(len(pix))], axis=1)
    Rb = np.asarray(R, float)
    tb = np.asarray(t, float)
    n = Rb[:, 2]
    d = float(n @ tb)
    denom = rays @ n
    denom[np.abs(denom) < 1e-9] = 1e-9
    scale = d / denom
    Xc = rays * scale[:, None]
    Xb = (Rb.T @ (Xc - tb).T).T
    cell = max(1e-3, float(spec.surface_texture_cell_mm))
    iu = np.clip(np.floor(Xb[:, 0] / cell).astype(np.int64) + 4096, 0, 8191)
    iv = np.clip(np.floor(Xb[:, 1] / cell).astype(np.int64) + 4096, 0, 8191)
    rng = np.random.default_rng(int(spec.surface_texture_seed))
    field = rng.normal(0.0, 1.0, (8192, 8192)).astype(np.float32)
    grey = float(spec.plate_grey) + float(spec.surface_texture) * field[iv, iu]
    out = np.full((h, w), int(spec.plate_grey), np.uint8)
    out[mask > 0] = np.clip(grey, 0, 200).astype(np.uint8)
    return out


def _layout_fn(name: str):
    """Map canonical marker labels to the *printed* plate layout."""
    umax, vmax = 7.0, 4.0
    if name == "identity":
        return lambda u, v: (u, v)
    if name == "mirror_u":
        return lambda u, v: (umax - u, v)
    if name == "mirror_v":
        return lambda u, v: (u, vmax - v)
    if name == "rot180":
        return lambda u, v: (umax - u, vmax - v)
    raise ValueError(f"unknown marker_layout {name!r}")


def render_plate_view(camera: Any, pose: tuple[np.ndarray, np.ndarray],
                      spec: SyntheticSpec | None = None,
                      *, drop_count: int = 0,
                      rng: np.random.Generator | None = None) -> np.ndarray:
    """Render one calibration-oriented grey view of the 41-marker plate.

    ``camera`` is a ``calib.calibrate.CameraIntrinsics``; ``pose`` is a
    guidance-frame board pose.  The image is produced in the calibration
    orientation, so no rotation is applied afterwards.
    """
    from calib import plate
    from calib.geometry import project

    spec = spec or SyntheticSpec()
    rng = rng or np.random.default_rng(spec.seed)
    K = np.asarray(camera.K, float)
    kc = np.asarray(camera.kc, float)
    Rg, t = pose
    R = np.asarray(Rg, float) @ _BOARD_TO_CAMERA
    t = np.asarray(t, float)
    w, h = types.IMAGE_WIDTH, types.IMAGE_HEIGHT
    img = np.full((h, w), int(spec.background), np.uint8)

    pitch = float(spec.pitch_mm)
    centre = np.asarray(plate.current_spec().centre, float)
    # plate quad with a one-pitch margin
    hu = (plate.current_spec().cols / 2.0) * pitch
    hv = (plate.current_spec().rows / 2.0) * pitch
    corners = np.array([[-hu - pitch / 2, -hv - pitch / 2, 0],
                        [hu + pitch / 2, -hv - pitch / 2, 0],
                        [hu + pitch / 2, hv + pitch / 2, 0],
                        [-hu - pitch / 2, hv + pitch / 2, 0]], float)
    try:
        cc = project(K, kc, corners @ R.T + t)
    except Exception:
        cc = None
    import cv2
    if cc is not None and np.all(np.isfinite(cc)):
        poly = np.clip(cc, [0, 0], [w - 1, h - 1]).astype(np.int32)
        if cv2.contourArea(poly) > 200:
            mask = np.zeros((h, w), np.uint8)
            cv2.fillConvexPoly(mask, poly, 255)
            if spec.surface_texture > 0:
                plate_px = _surface_texture(K, kc, R, t, mask,
                                            spec, hu, hv, pitch, plate)
            elif spec.plate_texture > 0:
                # per-pixel speckle: the 99th percentile stays between the
                # plate and the markers (the real corpus behaves the same
                # way), and the few above-threshold plate pixels are isolated
                # single pixels -- below the blob detector's 25 px minimum.
                texture = (float(spec.plate_grey)
                           + float(spec.plate_texture)
                           * np.abs(rng.normal(0.0, 1.0, (h, w))))
                plate_px = np.clip(texture, 0, 200).astype(np.uint8)
            else:
                plate_px = np.full((h, w), int(spec.plate_grey), np.uint8)
            img[mask > 0] = plate_px[mask > 0]

    sym = _layout_fn(spec.marker_layout)
    big_uv = {(float(u), float(v)) for u, v in plate.big_points_uv()}
    lattice = plate.lattice_uv()
    drop = {(float(u), float(v)) for u, v in spec.drop_uv}
    visible = spec.visible_markers

    for i, (u, v) in enumerate(lattice):
        if visible is not None and i >= int(visible):
            break
        if i >= len(lattice) - int(drop_count):
            break
        key = (float(u), float(v))
        if key in drop:
            continue
        ue, ve = sym(key[0], key[1])
        X = np.array([[(ue - centre[0]) * pitch, (ve - centre[1]) * pitch, 0.0]])
        p = project(K, kc, X @ R.T + t)[0]
        if not np.isfinite(p).all() or not (-64 <= p[0] < w + 64 and -64 <= p[1] < h + 64):
            continue
        big = key in big_uv
        scale = _BIG_SCALES.get(key, 1.0) if big else 1.0
        radius = _marker_radius(key[0], key[1], K, kc, R, t, pitch, spec,
                                False, scale)
        cv2.circle(img, (int(round(p[0])), int(round(p[1]))), radius,
                   int(spec.big_marker_grey if big else spec.marker_grey),
                   -1, cv2.LINE_AA)
    # the off-lattice chiral marker (3.5, 3) is always drawn physical
    off = np.asarray(plate.big_points_uv(), float)[-1]
    ue, ve = sym(float(off[0]), float(off[1]))
    X = np.array([[(ue - centre[0]) * pitch, (ve - centre[1]) * pitch, 0.0]])
    p = project(K, kc, X @ R.T + t)[0]
    if np.isfinite(p).all() and -64 <= p[0] < w + 64 and -64 <= p[1] < h + 64:
        radius = _marker_radius(float(off[0]), float(off[1]), K, kc, R, t,
                                pitch, spec, False, 2.4)
        cv2.circle(img, (int(round(p[0])), int(round(p[1]))), radius,
                   int(spec.big_marker_grey), -1, cv2.LINE_AA)

    if spec.occlusion != "none":
        img = _occlude(img, spec)
    if spec.saturation_frac > 0:
        mask = rng.random((h, w)) < float(spec.saturation_frac)
        img[mask] = 255
    if spec.blur_sigma > 0:
        img = cv2.GaussianBlur(img, (0, 0), float(spec.blur_sigma))
    if spec.noise_sigma > 0:
        img = np.clip(img.astype(np.float32)
                      + rng.normal(0.0, float(spec.noise_sigma), (h, w)),
                      0, 255).astype(np.uint8)
    return np.ascontiguousarray(img)


#: The three on-lattice large markers get *distinct* areas.  ``calib.detect``
#: orders blobs by area and derives its anchor order from that; the real plate
#: has slightly different printed marker sizes, and a synthetic plate whose
#: three big markers came out equal made the anchor fit pick the mirrored
#: assignment on roughly half the views.
_BIG_SCALES = {(2.0, 1.0): 2.4, (4.0, 1.0): 2.4, (5.0, 1.0): 2.4}


def _marker_radius(u: float, v: float, K: np.ndarray, kc: np.ndarray,
                   R: np.ndarray, t: np.ndarray, pitch: float,
                   spec: SyntheticSpec, big: bool,
                   radius_factor: float | None = None) -> int:
    """Marker radius from the *local* projected pitch (no row-wrap jumps)."""
    from calib import plate
    from calib.geometry import project
    if spec.marker_radius_px is not None:
        base = float(spec.marker_radius_px)
    else:
        centre = np.asarray(plate.current_spec().centre, float)
        offsets = np.array([[-0.5 * pitch, 0.0, 0.0], [0.5 * pitch, 0.0, 0.0]])
        X = np.array([[(u - centre[0]) * pitch, (v - centre[1]) * pitch, 0.0]]) + offsets
        p = project(K, kc, X @ R.T + t)
        pitch_px = float(np.linalg.norm(p[1] - p[0])) if np.isfinite(p).all() else 0.0
        base = max(2.0, 0.065 * pitch_px) if pitch_px > 0 else 4.0
    factor = spec.big_scale if big else 1.0
    if radius_factor is not None:
        factor = float(radius_factor)
    radius = int(round(base * factor))
    return max(2, min(radius, 26 if factor > 1.5 else 16))


def _occlude(img: np.ndarray, spec: SyntheticSpec) -> np.ndarray:
    """Paint a background rectangle over the requested image band."""
    h, w = img.shape
    frac = float(np.clip(spec.occlusion_frac, 0.05, 0.95))
    if spec.occlusion == "left":
        img[:, : int(round(frac * w))] = spec.background
    elif spec.occlusion == "right":
        img[:, int(round((1.0 - frac) * w)):] = spec.background
    elif spec.occlusion == "top":
        img[: int(round(frac * h)), :] = spec.background
    elif spec.occlusion == "bottom":
        img[int(round((1.0 - frac) * h)):, :] = spec.background
    else:
        raise ValueError(f"unknown occlusion {spec.occlusion!r}")
    return img


def _rolled_pose(d: float, tx: float, ty: float, roll: float,
                 lateral: tuple[float, float],
                 target_sector: int | None = None):
    """Build a pose whose *measured* in-plane roll falls in ``target_sector``.

    ``calib.adaptive.board_pose`` composes the roll before the tilts, so a
    nominal 90 deg roll of a 12 deg-tilted plate can read as 270 deg under
    ``tools.wizard.coverage.roll_deg``.  The synthetic scene must cover the
    sectors the coverage gate *measures*, so the roll is refined in the board
    plane (a rotation about the plate normal does not change the tilt).
    """
    from calib.adaptive import board_pose
    R, t = board_pose(float(d), float(tx), float(ty), float(roll), lateral)
    if target_sector is None:
        return R, t
    try:
        from . import coverage as _cov
    except Exception:
        return R, t
    for delta in (0.0, 90.0, 180.0, 270.0):
        angle = math.radians(delta)
        Rz = np.array([[math.cos(angle), -math.sin(angle), 0.0],
                       [math.sin(angle), math.cos(angle), 0.0],
                       [0.0, 0.0, 1.0]])
        R2 = R @ Rz
        if int((_cov.roll_deg(R2) % 360.0) // 90.0) % 4 == int(target_sector):
            return R2, t
    return R, t


class SyntheticSource:
    """Render plates from a :class:`SyntheticSpec` (tests and GUI demo mode).

    The source also exposes :attr:`profile`: an ``einstar.recon.profile/1``-
    compatible :class:`calib.recon_profile.Profile` describing the cameras used
    for rendering (optionally perturbed by ``factory_perturb_px`` so a demo is
    not trivially solved by its own truth).
    """

    kind = types.SOURCE_SYNTHETIC
    deterministic = True

    def __init__(self, spec: SyntheticSpec | Mapping[str, Any] | None = None) -> None:
        if spec is None:
            spec = SyntheticSpec()
        elif isinstance(spec, Mapping):
            spec = SyntheticSpec(**dict(spec))
        self.spec = spec
        self._closed = False
        self._next_index = 0
        self._pass = 0
        self.poses = []
        self._build_cameras()
        if str(getattr(self.spec, "pattern", "")) == "coverage":
            self.poses = self._plan_coverage_poses()
            self.plan_report = getattr(self, "_plan_report", {})
        else:
            self.poses = self._poses()
        self.continuous = int(spec.frames_per_pose) > 1
        self.info = types.SourceInfo(
            kind=types.SOURCE_SYNTHETIC,
            path=f"synthetic:{spec.pattern}",
            note=(f"{len(self.poses)} rendered views, pitch {spec.pitch_mm:g} mm, "
                  f"layout {spec.marker_layout}"),
            continuous=bool(self.continuous))

    # -- scene -----------------------------------------------------------
    def _poses(self) -> list[tuple[np.ndarray, np.ndarray]]:
        # Use the *coverage* module's inverse so a scene described as
        # (distance, tilt_x, tilt_y, roll) measures back with the same tilt axes
        # the wizard reports.  `calib.adaptive.board_pose` uses a different
        # axis convention (its tilt_x shows up as coverage tilt_y, verified by
        # round-tripping a rendered pose through PnP: request (10,0) -> measured
        # (0.1, 10.1); request (0,18) -> measured (-18.0, 0.1)), which made a
        # synthetic scene unable to satisfy the two-axis tilt dimension.
        try:
            from .coverage import family_pose as _pose
        except Exception:  # pragma: no cover - coverage is optional
            from calib.adaptive import board_pose as _pose
        spec = self.spec
        if spec.poses is not None:
            out = []
            for entry in spec.poses:
                if len(entry) == 2 and np.asarray(entry[0]).shape == (3, 3):
                    out.append((np.asarray(entry[0], float),
                                np.asarray(entry[1], float)))
                else:
                    vals = list(entry) + [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
                    d, tx, ty, roll, lx, ly = vals[:6]
                    try:
                        out.append(_pose(float(d), float(tx), float(ty),
                                         float(roll), lateral_mm=(float(lx), float(ly))))
                    except TypeError:  # board_pose has no lateral argument
                        out.append(_pose(float(d), float(tx), float(ty), float(roll)))
            return out
        base = _default_poses(spec)
        out = []
        patterns = ((1, 1), (-1, 1), (1, -1), (-1, -1),
                    (0, 1), (0, -1), (1, 0), (-1, 0))
        for i, entry in enumerate(base):
            d, tx, ty, roll = (list(entry) + [0.0, 0.0, 0.0, 0.0])[:4]
            if spec.lateral_mm != (0.0, 0.0):
                lateral = tuple(spec.lateral_mm)
            elif i < 16:
                lateral = (0.0, 0.0)
            else:
                # constant angular offset -> the plate centre lands in a
                # specific 3x3 image region at every distance
                sx, sy = patterns[(i - 16) % len(patterns)]
                lateral = (sx * 0.21 * float(d), sy * 0.16 * float(d))
            target = None
            if i >= 12:
                target = ((i - 12) % 4) if i < 16 else ((i - 16) % 4)
            out.append(_rolled_pose(float(d), float(tx), float(ty), float(roll),
                                    lateral, target))
        return out

    def _build_cameras(self) -> None:
        from calib.calibrate import CAMERAS, CameraIntrinsics
        from calib.synth import make_scene
        spec = self.spec
        if spec.cameras is not None and spec.extrinsics is not None:
            self.cameras = {c: spec.cameras[c] for c in CAMERAS}
            self.extrinsics = {c: (np.asarray(spec.extrinsics[c][0], float),
                                   np.asarray(spec.extrinsics[c][1], float))
                               for c in CAMERAS}
            return
        scene = make_scene(n_views=max(1, len(getattr(self, "poses", []) or [])),
                           seed=spec.seed,
                           noise_px=0.0, outlier_frac=0.0,
                           pitch_mm=spec.pitch_mm)
        self.cameras = dict(scene.cameras)
        self.extrinsics = dict(scene.extrinsics)
        self._scene = scene

    # -- profile ---------------------------------------------------------
    def profile_dict(self) -> dict:
        """The rendered rig as an ``einstar.recon.profile/1`` document."""
        from calib.recon_profile import SCHEMA
        spec = self.spec
        rng = np.random.default_rng(spec.seed + 991)
        names = {"left": "ir_left", "right": "ir_right", "tex": "color"}
        cameras = {}
        for key, name in names.items():
            cam = self.cameras[key]
            R, T = self.extrinsics[key]
            if spec.factory_perturb_px:
                fx = cam.fx + float(rng.normal(0.0, spec.factory_perturb_px))
                fy = cam.fy + float(rng.normal(0.0, spec.factory_perturb_px))
                cx = cam.cx + float(rng.normal(0.0, spec.factory_perturb_px))
                cy = cam.cy + float(rng.normal(0.0, spec.factory_perturb_px))
            else:
                fx, fy, cx, cy = cam.fx, cam.fy, cam.cx, cam.cy
            cameras[name] = {
                "fx": float(fx), "fy": float(fy),
                "cx": float(cx), "cy": float(cy),
                "skew": float(cam.skew * cam.fx),
                "dist": [float(x) for x in np.asarray(cam.kc, float)[:5]],
                "R": [float(x) for x in np.asarray(R, float).ravel()],
                "T": [float(x) for x in np.asarray(T, float).ravel()],
            }
        return {
            "schema": SCHEMA, "kind": "synthetic",
            "image_width": types.IMAGE_WIDTH, "image_height": types.IMAGE_HEIGHT,
            "units": "mm", "plate_pitch_mm": float(spec.pitch_mm),
            "color_status": "synthetic",
            "cameras": cameras,
            "provenance": [f"synthetic pattern={spec.pattern}",
                           f"seed={spec.seed}", "generated by tools/wizard/sources.py"],
        }

    @property
    def profile(self):
        from calib import recon_profile
        path = f"<synthetic:{self.spec.pattern}:{self.spec.seed}>"
        doc = self.profile_dict()
        cameras = {name: recon_profile._camera(name, node)
                   for name, node in doc["cameras"].items()}
        return recon_profile.Profile(
            path=path, schema=doc["schema"], kind=doc["kind"],
            color_status=doc["color_status"],
            plate_pitch_mm=doc["plate_pitch_mm"], units=doc["units"],
            width=doc["image_width"], height=doc["image_height"],
            cameras=cameras)

    # -- reading ---------------------------------------------------------
    def _render(self, cam_key: str, pose: tuple[np.ndarray, np.ndarray],
                view_index: int) -> np.ndarray:
        R, t = pose
        Rc, tc = self.extrinsics[cam_key]
        Rw = Rc @ R
        tw = Rc @ t + tc
        rng = np.random.default_rng(int(self.spec.seed) * 7919 + int(view_index))
        drop = int(self.spec.drop_by_camera.get(cam_key, 0))
        return render_plate_view(self.cameras[cam_key], (Rw, tw), self.spec,
                                 drop_count=drop, rng=rng)

    def _plan_coverage_poses(self) -> list[tuple[np.ndarray, np.ndarray]]:
        """Build a pose plan that provably satisfies the coverage families.

        Each candidate is rendered for both IR cameras, detected and checked
        with the *same* orientation witness the wizard uses, so the fixture
        cannot claim coverage the detector would not actually see.  Goals are
        explicit: every one of the nine image regions gets a pose first, then
        every (tilt quadrant, roll sector) pair, then filler spreading across
        the three distance bands.  A rejected candidate is jittered a few times
        before being dropped, which mirrors an operator retrying a view.
        """
        from calib import detect
        try:
            from .coverage import family_pose
        except Exception:  # pragma: no cover
            from calib.adaptive import board_pose as family_pose
        try:
            from tools.sweep import orient
        except Exception:  # pragma: no cover
            orient = None
        spec = self.spec
        rng = np.random.default_rng(int(spec.seed) + 17)
        width, height = types.IMAGE_WIDTH, types.IMAGE_HEIGHT
        K = np.asarray(self.cameras["left"].K, float)
        Kinv = np.linalg.inv(K)

        def translation(distance: float, region: int) -> np.ndarray:
            iu, iv = int(region) % 3, int(region) // 3
            # aim inside the cell but toward the image centre for the far
            # column/row, so a corner view keeps the plate's far edge in frame
            # while its centre still lands in the required region
            fu = 0.5 if iu < 2 else 0.30
            fv = 0.5 if iv < 2 else 0.30
            u = (iu + fu) * width / 3.0
            v = (iv + fv) * height / 3.0
            ray = Kinv @ np.array([u, v, 1.0])
            return float(distance) * ray / ray[2]

        def usable(R, t) -> bool:
            for key in ("left", "right"):
                img = self._render(key, (R, t), 0)
                obs = detect.detect_grid_auto(np.ascontiguousarray(img))
                if obs is None or len(obs.points) < 16:
                    return False
                if orient is not None:
                    chk = orient.check_orientation(img, obs,
                                                   image_shape=img.shape).as_dict()
                    if not chk.get("ok"):
                        return False
            return True

        distances = (350.0, 430.0, 470.0)
        quadrants = ((1, 1), (-1, 1), (1, -1), (-1, -1))
        rolls = (0.0, 90.0, 180.0, 270.0)
        goals: list[tuple[int, tuple[int, int], float, float]] = []
        # 1) every image region once, at a distance that keeps the plate inside
        #    the colour camera too where possible (colour participation D6)
        for region_index in range(9):
            d = 350.0 if region_index in (1, 3, 4, 5, 7) else 430.0
            goals.append((region_index, quadrants[region_index % 4],
                          rolls[region_index % 4], d))
        # 2) every (tilt quadrant, roll sector) pair, so D2 and D3 are covered
        #    by construction rather than by luck
        for qi, quadrant in enumerate(quadrants):
            for ri, roll in enumerate(rolls):
                goals.append(((qi * 2 + ri) % 9, quadrant, roll,
                              distances[(qi + ri) % len(distances)]))
        # 3) filler that spreads across all three distance bands
        while len(goals) < max(int(spec.n_views), 28):
            i = len(goals)
            goals.append((i % 9, quadrants[i % 4], rolls[i % 4],
                          distances[i % len(distances)]))

        plan: list[tuple[np.ndarray, np.ndarray]] = []
        failures: list[dict] = []
        for i, (reg, quadrant, roll, distance) in enumerate(goals):
            placed = False
            for attempt in range(8):
                # back off on retry, but stay inside the intended distance band
                d = float(distance) * (1.0 + 0.05 * attempt)
                r = float(roll) + (0.0 if attempt < 2 else float(rng.normal(0, 8.0)))
                use_reg = int(reg) if (i < 9 or attempt < 5) else int(rng.integers(0, 9))
                if i < 9:
                    q_index = (quadrants.index(quadrant) + attempt // 2) % 4
                    use_quadrant = quadrants[q_index]
                else:
                    use_quadrant = quadrant
                R, _ = family_pose(d, use_quadrant[0] * 16.0,
                                   use_quadrant[1] * 16.0, r)
                t = translation(d, use_reg)
                if usable(R, t):
                    plan.append((R, t))
                    placed = True
                    break
            if not placed:
                failures.append({"region": int(reg), "quadrant": list(quadrant),
                                 "roll": float(roll), "distance": float(distance)})
        regions = []
        for _, t in plan:
            proj = K @ t
            u, v = proj[0] / proj[2], proj[1] / proj[2]
            regions.append(int(v // (height / 3.0)) * 3 + int(u // (width / 3.0)))
        self._plan_report = {"requested": len(goals), "kept": len(plan),
                             "rejected": failures,
                             "regions": sorted(set(regions))}
        return plan

    def __iter__(self) -> Iterator[types.FrameGroup]:
        repeats = max(1, int(self.spec.frames_per_pose))
        while not self._closed:
            for i, pose in enumerate(self.poses):
                if self._closed:
                    return
                images = {key: self._render(key, pose, i)
                          for key in types.CAMERA_KEYS}
                for repeat in range(repeats):
                    group = _make_group(
                        index=self._next_index, file_index=i, device_group=None,
                        timestamp_s=0.1 * self._next_index, images=images,
                        colour_bgr=None, source=types.SOURCE_SYNTHETIC,
                        continuous=bool(self.continuous),
                        label=(f"synthetic {self.spec.pattern} view {i}"
                               + (f" dwell {repeat}" if repeats > 1 else "")))
                    self._next_index += 1
                    yield group
            if not self.spec.loop or self._closed:
                return
            self._pass += 1

    def close(self) -> None:
        self._closed = True

    def __enter__(self) -> "SyntheticSource":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


# ---------------------------------------------------------------------------
# writing a replay directory (tests, demos, CI fixtures)
# ---------------------------------------------------------------------------

def write_replay_frames(groups: Iterable[types.FrameGroup], root: str | Path,
                        *, backend: str = "simulated",
                        device_group_start: int = 1000) -> Path:
    """Write oriented ``FrameGroup`` images as an ``einstar-capture-frames`` dir.

    The inverse orientation is applied so that reading the directory with
    :class:`ReplayDirSource` reproduces the input images: ``cam1`` = left,
    ``cam0`` = right rotated 180°, ``cam2`` = tex rotated 180° (as a grey Bayer
    mosaic; debayering a constant-channel image is the identity in grey).
    """
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    entries = []
    for i, group in enumerate(groups):
        images = group.images
        if not all(k in images for k in types.CAMERA_KEYS):
            continue
        types.write_pgm(root / f"frame_{i:06d}_cam1.pgm",
                        np.ascontiguousarray(images["left"]))
        types.write_pgm(root / f"frame_{i:06d}_cam0.pgm",
                        np.ascontiguousarray(np.rot90(images["right"], 2)))
        types.write_pgm(root / f"frame_{i:06d}_cam2.pgm",
                        np.ascontiguousarray(np.rot90(images["tex"], 2)))
        entries.append({
            "group": i,
            "device_group": int(device_group_start + i) if group.device_group is None
            else int(group.device_group),
            "cameras": 3,
            "host_time_ns": int(round(group.timestamp_s * 1e9)),
            "stream0": str(root / f"frame_{i:06d}_cam0.pgm"),
            "stream1": str(root / f"frame_{i:06d}_cam1.pgm"),
            "stream2": str(root / f"frame_{i:06d}_cam2.pgm"),
        })
    manifest = {
        "schema": "einstar-frames-v1",
        "tool": "tools/wizard/sources.py:write_replay_frames",
        "backend": backend,
        "simulated": True,
        "live": False,
        "output_directory": str(root),
        "frame": {"width": types.IMAGE_WIDTH, "height": types.IMAGE_HEIGHT,
                  "bit_depth": 8, "format": "pgm-p5",
                  "bytes_per_frame": types.IMAGE_WIDTH * types.IMAGE_HEIGHT},
        "camera_map": {"0": "right_ir", "1": "left_ir", "2": "color"},
        "group_id": {"source": "device_trigger_group_counter",
                     "output_ids": "dense_0_based"},
        "complete": True,
        "stopped_early": False,
        "requested_groups": len(entries),
        "groups_written": len(entries),
        "groups_incomplete": 0,
        "groups_absent": 0,
        "groups": entries,
    }
    types.atomic_write_json(root / "frames_manifest.json", manifest)
    return root
