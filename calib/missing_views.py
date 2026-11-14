"""Capture only the views a recorded session is missing, then combine them.

The recovery of session-03 established that the expensive part of a
calibration (a long, multi-pose recording) is *already done*: the remaining
gate is a handful of genuine, distinct stationary holds (D7) and, in the raw
session, four right-corner views that the complete raw recording already
contains.  Repeating a 22-minute capture to collect four one-second holds is
the wrong operator experience.

This module is the offline-first half of the completion workflow.  It is
pure (no capture, no scanner, no profile activation) and consists of:

* :func:`load_base_evidence` -- read an existing session **read-only** and
  rebuild the coverage views, the accepted solver evidence and the
  stationarity observation stream (from ``session.json["resume"]``, or from
  ``decisions.jsonl`` exactly the way
  ``analysis/calibration/coverage/validate_session03.py`` does when the older
  session predates the stored stream).  File digests are recorded and
  re-checked so a changed base session is refused.
* :func:`plan` -- turn the unsatisfied coverage dimensions into a *minimal
  ordered plan* of board poses (hard cap :data:`MAX_PLAN_POSES`).  D7's
  deficit is ``min_stationary_views - n_intervals``; the target poses are
  chosen to be distinct from each other and from the recorded views in
  distance band, tilt quadrant, roll sector and image cell.
* :class:`HoldTracker` -- the pure state machine an operator's gestures are
  fed into: intentional movement between positions resets the attempt to
  ``ALIGNING``; micro-instability inside the tolerance keeps accumulating;
  a completed hold yields the averaged pose plus real measured duration and
  speed.  It never accepts while the pose drifts beyond the jitter tolerance.
* :class:`ExtensionStore` -- the append-only, crash-safe extension session
  (``extension.json`` + ``holds.jsonl``).  Existing lines are never rewritten
  and a changed base digest is refused.
* :func:`combine` / :func:`assess` -- base accepted views plus dwell views and
  measured stationarity rows, so that
  ``tools.wizard.coverage.measure(views, stationarity_observations=...)``
  works unchanged.

Activation gate
---------------
Nothing in this module activates, installs or writes a profile.  Assessing a
session -- even a READY one -- only reports readiness; :func:`activation_gate`
always returns ``activated: False``.  Activation stays the explicit lifecycle
step (``tools.wizard.lifecycle.activate``) after a validated candidate.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

__all__ = [
    "MAX_PLAN_POSES",
    "SCHEMA",
    "EXTENSION_SCHEMA",
    "HOLD_SCHEMA",
    "BaseEvidence",
    "BaseSessionChanged",
    "Combined",
    "ExtensionError",
    "ExtensionStore",
    "HoldConfig",
    "HoldStatus",
    "HoldTracker",
    "MissingViewsError",
    "assessment_json",
    "activation_gate",
    "assess",
    "combine",
    "describe_move",
    "describe_target",
    "load_base_evidence",
    "plan",
    "pose_signature",
    "readiness_verdict",
    "verify_base_digests",
]

SCHEMA = "einstar.missing-views/1"
EXTENSION_SCHEMA = "einstar.missing-views.extension/1"
HOLD_SCHEMA = "einstar.missing-views.hold/1"

#: Hard cap on how many board poses one plan may ask for.  A D7 deficit is at
#: most ``min_stationary_views`` (6) and the other dimensions are usually
#: already satisfied by the base session; the cap keeps an operator from ever
#: being sent back into a long walk.
MAX_PLAN_POSES = 8

#: The image plane the coverage grid refers to (``tools.wizard.types``).
IMAGE_WIDTH = 1280
IMAGE_HEIGHT = 1024

#: Small files a session digest covers.  The 52 GB ``raw.e1r`` is deliberately
#: *not* hashed here (76 s at 681 MB/s on the real file): its identity is
#: recorded by size and mtime and the recorder trailer is the authoritative
#: full-file check.
_SESSION_DIGEST_FILES = ("session.json", "manifest.json", "decisions.jsonl",
                         "status.json")
_RAW_RECORDING = "raw.e1r"


class MissingViewsError(RuntimeError):
    """The session cannot be used for the completion workflow."""


class BaseSessionChanged(MissingViewsError):
    """The base session's recorded files changed since the evidence was read."""


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, np.ndarray):
        return _json_safe(value.tolist())
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, (np.floating, float)):
        return float(value)
    if isinstance(value, (np.integer, int)):
        return int(value)
    return value


def _sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _cov():
    """Lazily import :mod:`tools.wizard.coverage`.

    ``calib`` is the engine package and normally has no dependency on the
    wizard tools; keeping the import lazy means importing
    ``calib.missing_views`` never fails on a machine that only ships the
    calibration engine (it fails at the first call that genuinely needs the
    coverage bins).
    """
    from tools.wizard import coverage
    return coverage


def _spec_object(spec: Any = None, base: "BaseEvidence | None" = None):
    """Coerce a spec-like value to :class:`tools.wizard.coverage.CoverageSpec`.

    Accepts ``None`` (use the base session's recorded spec, else the current
    defaults), a mapping (the session's ``spec``/``config.coverage`` layout) or
    an existing spec object.  Unknown legacy keys are ignored: the real
    session-03 recorded ``min_image_regions_visited`` /
    ``min_views_per_image_corner`` / ``max_motion_mm``, which the correction
    replaced with ``min_image_region_views`` / ``min_corner_views`` /
    ``stationary_speed_mm_s``.  The additive defaults are therefore used for
    those, exactly as the controller does when it is given the legacy object.
    """
    coverage = _cov()
    spec_cls = coverage.CoverageSpec
    if spec is None:
        raw = getattr(base, "spec", None) if base is not None else None
        if not raw:
            return spec_cls()
        spec = raw
    if isinstance(spec, spec_cls):
        return spec
    if isinstance(spec, Mapping):
        allowed = {f.name for f in spec_cls.__dataclass_fields__.values()}
        kwargs = {}
        for key, value in spec.items():
            if key not in allowed:
                continue
            if key == "distance_bands_mm":
                value = tuple(tuple(float(x) for x in band) for band in value)
            elif isinstance(value, list):
                value = tuple(value)
            kwargs[key] = value
        try:
            return spec_cls(**kwargs)
        except TypeError:
            return spec_cls()
    return spec_cls()


def _finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _pose_rt(pose: Any) -> tuple[np.ndarray, np.ndarray] | None:
    """Accept ``{"R":..,"t":..}``, ``(R, t)`` or ``None``."""
    if pose is None:
        return None
    if isinstance(pose, Mapping):
        R, t = pose.get("R"), pose.get("t")
    elif isinstance(pose, (tuple, list)) and len(pose) == 2:
        R, t = pose
    else:
        return None
    if R is None or t is None:
        return None
    try:
        R = np.asarray(R, float).reshape(3, 3)
        t = np.asarray(t, float).reshape(3)
    except (TypeError, ValueError):
        return None
    if not (np.all(np.isfinite(R)) and np.all(np.isfinite(t))):
        return None
    return R, t


def _view_image_point(view: Mapping[str, Any]) -> np.ndarray | None:
    """The plate-centre proxy used by coverage D4: left marker centroid."""
    points = view.get("image_points") or {}
    if isinstance(points, Mapping):
        for camera in ("left", "right", "tex"):
            raw = points.get(camera)
            if raw is None:
                continue
            arr = np.asarray(raw, float).reshape(-1, 2)
            if len(arr):
                return arr.mean(axis=0)
    centre = view.get("plate_centre_px")
    if centre is not None:
        try:
            return np.asarray(centre, float).reshape(2)
        except (TypeError, ValueError):
            return None
    return None


def _image_cell(view_or_uv: Any) -> tuple[int, int] | None:
    """3x3 cell (row, col) of the plate centre, or ``None``."""
    if isinstance(view_or_uv, Mapping):
        uv = _view_image_point(view_or_uv)
    else:
        uv = view_or_uv
    if uv is None:
        return None
    try:
        uv = np.asarray(uv, float).reshape(2)
    except (TypeError, ValueError):
        return None
    if not np.all(np.isfinite(uv)):
        return None
    col = int(np.clip(uv[0] / (IMAGE_WIDTH / 3.0), 0, 2))
    row = int(np.clip(uv[1] / (IMAGE_HEIGHT / 3.0), 0, 2))
    return row, col


_CELL_NAMES = {(0, 0): "top-left", (0, 1): "top", (0, 2): "top-right",
               (1, 0): "left", (1, 1): "centre", (1, 2): "right",
               (2, 0): "bottom-left", (2, 1): "bottom", (2, 2): "bottom-right"}


def cell_name(cell: Any) -> str | None:
    if not isinstance(cell, (tuple, list)) or len(cell) != 2:
        return None
    try:
        return _CELL_NAMES.get((int(cell[0]), int(cell[1])))
    except (TypeError, ValueError):
        return None


def pose_signature(pose: Any, spec: Any = None,
                   view: Mapping[str, Any] | None = None) -> dict | None:
    """Coarse, machine-readable pose identity used for D7 diversity.

    ``(distance band, tilt quadrant, roll sector, image cell)`` -- the same
    bins the coverage gate counts, so "a genuinely distinct hold" means the
    same thing to the plan, the tracker and the assessment.
    """
    parsed = _pose_rt(pose)
    if parsed is None:
        return None
    R, t = parsed
    coverage = _cov()
    spec = _spec_object(spec)
    metrics = coverage.pose_metrics({"pose": (R, t)})
    if metrics is None:
        return None
    band = spec.band_index(metrics["distance_mm"])
    quadrant = (1 if metrics["tilt_x_deg"] >= 0.0 else -1,
                1 if metrics["tilt_y_deg"] >= 0.0 else -1)
    roll_bin = spec.roll_bin(metrics["roll_deg"])
    cell = _image_cell(view) if view is not None else None
    return {"band_index": band, "quadrant": quadrant,
            "roll_bin": int(roll_bin), "cell": cell,
            "distance_mm": float(metrics["distance_mm"]),
            "tilt_x_deg": float(metrics["tilt_x_deg"]),
            "tilt_y_deg": float(metrics["tilt_y_deg"]),
            "roll_deg": float(metrics["roll_deg"])}


def _signature_key(signature: Mapping[str, Any] | None) -> tuple | None:
    if not signature:
        return None
    cell = signature.get("cell")
    return (signature.get("band_index"),
            tuple(signature.get("quadrant") or (0, 0)),
            signature.get("roll_bin"),
            None if cell is None else tuple(cell))


# ---------------------------------------------------------------------------
# base evidence
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BaseEvidence:
    """Read-only facts recovered from an existing session directory."""

    session_dir: Path
    views: tuple[dict, ...]
    accepted: tuple[dict, ...]
    observation_rows: tuple[dict, ...]
    stationarity_observations: tuple[dict, ...]
    digests: dict[str, str | None]
    file_sizes: dict[str, int]
    factory_sha256: str | None
    pitch_mm: float
    spec: dict | None
    state: str | None
    source: str
    notes: tuple[str, ...]
    counters: dict = field(default_factory=dict)
    raw_recording: dict | None = None

    # -- convenience ----------------------------------------------------
    def summary(self) -> dict:
        return {
            "session_dir": str(self.session_dir),
            "state": self.state,
            "n_accepted": len(self.accepted),
            "n_views": len(self.views),
            "n_stationarity_observations": len(self.stationarity_observations),
            "stationarity_source": self.source,
            "digests": dict(self.digests),
            "file_sizes": dict(self.file_sizes),
            "factory_sha256": self.factory_sha256,
            "pitch_mm": float(self.pitch_mm),
            "counters": _json_safe(dict(self.counters)),
            "raw_recording": dict(self.raw_recording or {}),
            "notes": list(self.notes),
        }

    def last_pose(self) -> tuple[np.ndarray, np.ndarray] | None:
        for view in reversed(self.views):
            pose = _pose_rt(view.get("pose"))
            if pose is not None:
                return pose
        return None

    def signatures(self, spec: Any = None) -> list[dict]:
        spec = _spec_object(spec, self)
        out = []
        for view in self.views:
            signature = pose_signature(view.get("pose"), spec, view)
            if signature is not None:
                out.append(signature)
        return out

    def as_json(self) -> dict:
        """Compact JSON-safe projection (no view arrays)."""
        return _json_safe(self.summary())


def _digests_of(session_dir: Path) -> tuple[dict[str, str | None], dict[str, int]]:
    digests: dict[str, str | None] = {}
    sizes: dict[str, int] = {}
    for name in _SESSION_DIGEST_FILES:
        path = session_dir / name
        try:
            if path.is_file():
                sizes[name] = int(path.stat().st_size)
                digests[name] = _sha256_file(path)
            else:
                digests[name] = None
                sizes[name] = 0
        except OSError:
            digests[name] = None
            sizes[name] = 0
    return digests, sizes


def _raw_recording_fact(session_dir: Path) -> dict | None:
    path = session_dir / _RAW_RECORDING
    try:
        stat = path.stat()
    except OSError:
        return None
    return {"name": _RAW_RECORDING, "bytes": int(stat.st_size),
            "mtime_ns": int(stat.st_mtime_ns),
            "sha256": None,
            "note": "not hashed here: the 52 GB recording is verified by the "
                    "recorder trailer / einstar-stream-frames --verify-only"}


def verify_base_digests(evidence: BaseEvidence) -> dict:
    """Recompute the recorded small-file digests and compare.

    Returns ``{"ok": bool, "changed": [...], "missing": [...],
    "checked": {...}}``.  A session is expected to be read-only while it is
    used as base evidence; anything else is a different evidence set.
    """
    digests, sizes = _digests_of(evidence.session_dir)
    changed, missing = [], []
    for name, expected in evidence.digests.items():
        actual = digests.get(name)
        if expected is None:
            continue
        if actual is None:
            missing.append(name)
        elif actual != expected:
            changed.append(name)
    return {"ok": not changed and not missing,
            "changed": changed, "missing": missing,
            "expected": dict(evidence.digests), "actual": digests,
            "sizes": sizes}


def _resume_block(payload: Mapping[str, Any]) -> dict:
    block = payload.get("resume") if isinstance(payload, Mapping) else None
    if not isinstance(block, Mapping) or not block.get("accepted"):
        raise MissingViewsError(
            "session.json has no resumable 'resume.accepted' block; this "
            "session cannot be extended (nothing was written)")
    return dict(block)


def _views_by_view(rows: Sequence[Mapping[str, Any]]) -> tuple[
        dict[int, dict[str, list[list[float]]]], dict[int, dict[str, set]]]:
    """Group resume ``views`` rows by view id and camera (pixels + labels)."""
    pixels: dict[int, dict[str, list[list[float]]]] = {}
    labels: dict[int, dict[str, set]] = {}
    for entry in rows:
        try:
            view_id = int(entry["view"])
        except (KeyError, TypeError, ValueError):
            continue
        camera = str(entry.get("camera"))
        points: list[list[float]] = []
        seen: set = set()
        for row in entry.get("uv") or []:
            try:
                seen.add((float(row[0]), float(row[1])))
                points.append([float(row[2]), float(row[3])])
            except (IndexError, TypeError, ValueError):
                continue
        pixels.setdefault(view_id, {})[camera] = points
        labels.setdefault(view_id, {})[camera] = seen
    return pixels, labels


def _coverage_views(accepted: Sequence[Mapping[str, Any]],
                    pixels: Mapping[int, Mapping[str, list[list[float]]]],
                    labels: Mapping[int, Mapping[str, set]]) -> list[dict]:
    """Rebuild the per-view records ``tools.wizard.coverage.measure`` reads.

    This mirrors ``WizardController.resume`` exactly: ``resume.accepted`` has
    the pose/quality, ``resume.views`` has every camera's ``(label, pixel)``
    rows.  Grouping those rows reconstructs ``image_points`` (D4) and the
    shared-marker count (D5) without re-running detection.
    """
    views: list[dict] = []
    for entry in accepted:
        try:
            view_id = int(entry["view_id"])
        except (KeyError, TypeError, ValueError):
            continue
        pose = _pose_rt(entry)
        if pose is None:
            continue
        view_pixels = dict(pixels.get(view_id) or {})
        view_labels = labels.get(view_id) or {}
        shared = (len(view_labels.get("left", set())
                        & view_labels.get("right", set()))
                  if "left" in view_labels and "right" in view_labels
                  else int(entry.get("shared_markers_ir") or 0))
        coverage = _cov()
        metrics = coverage.pose_metrics({"pose": pose}) or {}
        quality = dict(entry.get("quality") or {})
        views.append({
            "view_id": view_id,
            "group": entry.get("group"),
            "pose": {"R": np.asarray(pose[0], float).tolist(),
                     "t": np.asarray(pose[1], float).tolist()},
            "image_points": {camera: rows
                             for camera, rows in view_pixels.items()},
            "timestamp": _finite(entry.get("timestamp")),
            "distance_mm": metrics.get("distance_mm"),
            "tilt_deg": metrics.get("tilt_deg"),
            "roll_deg": metrics.get("roll_deg"),
            "n_markers": dict(entry.get("n_markers") or {}),
            "quality": quality,
            "quality_passed": (bool(quality.get("passed"))
                               if quality.get("passed") is not None else None),
            "shared_markers_ir": int(shared),
            "assignment_ok": bool(entry.get("assignment_ok", True)),
            "dwell": bool(entry.get("dwell", False)),
            "label": str(entry.get("label") or ""),
            "recovered_from_raw_group": entry.get("recovered_from_raw_group"),
        })
    return views


def _decision_rows(session_dir: Path) -> tuple[list[dict], str | None]:
    path = session_dir / "decisions.jsonl"
    if not path.is_file():
        return [], None
    rows: list[dict] = []
    bad = 0
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                bad += 1
                continue
            if isinstance(row, dict):
                rows.append(row)
    note = None
    if bad:
        note = f"{bad} unparseable decision line(s) skipped"
    return rows, note


def load_base_evidence(session_dir: str | Path, *,
                       reconstruct_stationarity: bool = True,
                       verify_unchanged: bool = True) -> BaseEvidence:
    """Read an existing session **read-only** and rebuild its evidence.

    Preference order for the stationarity stream:

    1. ``session.json["resume"]["stationarity_observations"]`` (written by the
       corrected controller, including across resume);
    2. otherwise the recorded ``decisions.jsonl`` rows, passed through exactly
       as ``validate_session03.py`` does (``coverage.measure(views,
       stationarity_observations=decisions)``); that is how the real session-03
       -- which predates the stored stream -- is measured;
    3. otherwise no stream (D7 stationarity is reported *unestablished*, never
       silently passed).

    Nothing in the base directory is opened for writing.  The small session
    files are hashed before and after the read; a mismatch raises
    :class:`BaseSessionChanged`.
    """
    root = Path(session_dir).expanduser().resolve()
    if not root.is_dir():
        raise MissingViewsError(f"not a session directory: {root}")
    session_path = root / "session.json"
    if not session_path.is_file():
        raise MissingViewsError(f"no session.json in {root}")

    digests_before, sizes_before = _digests_of(root)
    try:
        payload = json.loads(session_path.read_text())
    except (OSError, ValueError) as exc:
        raise MissingViewsError(f"cannot read {session_path}: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise MissingViewsError(f"{session_path} is not a JSON object")

    block = _resume_block(payload)
    raw_views = [dict(row) for row in (block.get("views") or [])
                 if isinstance(row, Mapping)]
    raw_accepted = [dict(row) for row in (block.get("accepted") or [])
                    if isinstance(row, Mapping)]
    if not raw_accepted:
        raise MissingViewsError(
            f"{root}: resume block has no accepted views")

    pixels, labels = _views_by_view(raw_views)
    views = _coverage_views(raw_accepted, pixels, labels)
    if not views:
        raise MissingViewsError(
            f"{root}: could not rebuild a single coverage view from the "
            "resume block (no accepted entry has a usable pose)")

    notes: list[str] = []
    stream: list[dict] = [
        dict(row) for row in (block.get("stationarity_observations") or [])
        if isinstance(row, Mapping) and row]
    source = "stored_stream"
    if stream:
        notes.append(
            f"stationarity stream: {len(stream)} stored observation(s) from "
            "session.json resume")
    elif reconstruct_stationarity:
        decision_rows, decision_note = _decision_rows(root)
        if decision_note:
            notes.append(decision_note)
        if decision_rows:
            stream = decision_rows
            source = "decisions_jsonl"
            notes.append(
                f"stationarity stream: {len(stream)} observation(s) "
                "reconstructed from decisions.jsonl (the session predates the "
                "stored stream)")
        else:
            source = "unavailable"
            notes.append(
                "no stored stationarity stream and no decisions.jsonl: D7 "
                "stationarity is unestablished for the base session")
    else:
        source = "unavailable"
        notes.append("stationarity reconstruction disabled by the caller")

    raw = _raw_recording_fact(root)
    if raw is not None:
        notes.append(
            f"raw recording present ({raw['bytes']} B) and left untouched; "
            "its digest is not recomputed here")

    evidence = BaseEvidence(
        session_dir=root,
        views=tuple(views),
        accepted=tuple(raw_accepted),
        observation_rows=tuple(raw_views),
        stationarity_observations=tuple(stream),
        digests=dict(digests_before),
        file_sizes=dict(sizes_before),
        factory_sha256=(block.get("factory_sha256")
                        or (payload.get("hashes") or {}).get("factory_profile")),
        pitch_mm=float(block.get("pitch_mm")
                       or (payload.get("config") or {}).get("pitch_mm") or 28.0),
        spec=(payload.get("spec")
              or (payload.get("config") or {}).get("coverage")),
        state=payload.get("state"),
        source=source,
        notes=tuple(notes),
        counters=dict(block.get("counters") or {}),
        raw_recording=raw,
    )
    if verify_unchanged:
        check = verify_base_digests(evidence)
        if not check["ok"]:
            raise BaseSessionChanged(
                "the base session changed while it was being read: "
                f"changed={check['changed']} missing={check['missing']}")
    return evidence


# ---------------------------------------------------------------------------
# hold tracker
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class HoldConfig:
    """Thresholds of the hold state machine.

    ``hold_s`` is how long a pose must be *continuously* inside tolerance
    before it is accepted (the coverage spec's dwell floor is 0.3 s; one
    second is a comfortable, unambiguous operator hold).  ``move_mm`` /
    ``move_deg`` are per-sample jumps that mean "the operator deliberately
    moved to another position"; ``jitter_mm`` / ``jitter_deg`` bound how far
    the pose may wander around the hold anchor; ``max_speed_mm_s`` /
    ``max_rate_deg_s`` are the D7 stationarity thresholds, and a sample above
    them restarts the clean window instead of being counted as a hold sample.
    """

    hold_s: float = 1.0
    move_mm: float = 8.0
    move_deg: float = 3.0
    jitter_mm: float = 2.5
    jitter_deg: float = 1.0
    max_speed_mm_s: float = 3.0
    max_rate_deg_s: float = 1.5
    min_samples: int = 2
    max_gap_s: float = 1.0
    timeout_s: float = 15.0

    def as_dict(self) -> dict:
        return {"hold_s": float(self.hold_s), "move_mm": float(self.move_mm),
                "move_deg": float(self.move_deg),
                "jitter_mm": float(self.jitter_mm),
                "jitter_deg": float(self.jitter_deg),
                "max_speed_mm_s": float(self.max_speed_mm_s),
                "max_rate_deg_s": float(self.max_rate_deg_s),
                "min_samples": int(self.min_samples),
                "max_gap_s": float(self.max_gap_s),
                "timeout_s": float(self.timeout_s)}


@dataclass
class HoldStatus:
    """One update's observable state (fed to the GUI every frame)."""

    state: str = "ALIGNING"
    progress: float = 0.0
    elapsed_s: float = 0.0
    duration_s: float = 0.0
    speed_mm_s: float | None = None
    rate_deg_s: float | None = None
    dt_s: float | None = None
    samples: int = 0
    reason: str | None = None
    rejected: dict | None = None
    hold: dict | None = None

    def as_dict(self) -> dict:
        return {"state": self.state, "progress": float(self.progress),
                "elapsed_s": float(self.elapsed_s),
                "duration_s": float(self.duration_s),
                "speed_mm_s": self.speed_mm_s,
                "rate_deg_s": self.rate_deg_s, "dt_s": self.dt_s,
                "samples": int(self.samples), "reason": self.reason,
                "rejected": _json_safe(self.rejected),
                "hold": _json_safe(self.hold)}


def _rotation_angle_deg(Ra: np.ndarray, Rb: np.ndarray) -> float:
    dR = np.asarray(Rb, float) @ np.asarray(Ra, float).T
    cosine = (float(np.trace(dR)) - 1.0) / 2.0
    return math.degrees(math.acos(min(1.0, max(-1.0, cosine))))


def _mean_rotation(Rs: Sequence[np.ndarray]) -> np.ndarray:
    """Chordal mean of rotations (SVD projection onto SO(3))."""
    stack = np.stack([np.asarray(R, float) for R in Rs])
    U, _, Vt = np.linalg.svd(stack.mean(axis=0))
    R = U @ Vt
    if np.linalg.det(R) < 0.0:
        U[:, -1] *= -1.0
        R = U @ Vt
    return R


@dataclass
class _Sample:
    timestamp: float
    R: np.ndarray
    t: np.ndarray
    speed_mm_s: float | None = None
    rate_deg_s: float | None = None
    dt_s: float | None = None


class HoldTracker:
    """Pure state machine over a stream of ``(timestamp_s, pose_R, pose_t)``.

    States::

        ALIGNING --(two consecutive still samples)--> HOLDING
        HOLDING  --(hold_s elapsed, all samples inside tolerance)--> ACCEPTED
        ACCEPTED --(the operator moves)--> ALIGNING

    ``HOLDING`` reports ``progress`` in ``0..1`` for the UI ring and keeps
    accumulating through *small residual jitter* (the pose stays inside
    ``jitter_mm`` / ``jitter_deg`` of the anchor).  A *large* per-sample
    change is intentional movement and resets the attempt to ``ALIGNING``.
    A hold that ends before ``hold_s`` is rejected with a reason.  A pose that
    wanders beyond the jitter tolerance is never accepted: the attempt is
    abandoned with ``reason="hold_unstable"``.
    """

    def __init__(self, config: HoldConfig | Mapping[str, Any] | None = None,
                 *, now: float | None = None) -> None:
        if isinstance(config, Mapping):
            config = HoldConfig(**{k: v for k, v in config.items()
                                   if k in HoldConfig.__dataclass_fields__})
        self.config = config or HoldConfig()
        self.reset(now=now)

    # -- state ----------------------------------------------------------
    def reset(self, *, now: float | None = None) -> None:
        self._prev: _Sample | None = None
        self._window: list[_Sample] = []
        self._state = "ALIGNING"
        self._armed = True
        self._last = HoldStatus()
        self._accepted: list[dict] = []
        self._rejected: list[dict] = []
        self._created = now

    @property
    def state(self) -> str:
        return self._state

    @property
    def accepted_holds(self) -> list[dict]:
        return [dict(hold) for hold in self._accepted]

    @property
    def rejected_attempts(self) -> list[dict]:
        return [dict(row) for row in self._rejected]

    def status(self) -> HoldStatus:
        return self._last

    # -- the update -----------------------------------------------------
    def update(self, timestamp_s: float, R: Any, t: Any) -> HoldStatus:
        cfg = self.config
        timestamp = float(timestamp_s)
        R = np.asarray(R, float).reshape(3, 3)
        t = np.asarray(t, float).reshape(3)
        if not (math.isfinite(timestamp) and np.all(np.isfinite(R))
                and np.all(np.isfinite(t))):
            self._last = HoldStatus(state=self._state, reason="pose_unavailable")
            return self._last

        sample = _Sample(timestamp=timestamp, R=R, t=t)
        if self._prev is None:
            self._prev = sample
            self._window = [sample]
            self._last = HoldStatus(state=self._state, samples=1)
            return self._last

        dt = timestamp - self._prev.timestamp
        if dt <= 0.0:
            # a repeated / out-of-order frame is not new evidence
            self._last = HoldStatus(state=self._state,
                                    progress=self._last.progress,
                                    elapsed_s=self._last.elapsed_s,
                                    samples=len(self._window),
                                    reason="timestamp_not_advancing")
            return self._last
        if dt > float(cfg.max_gap_s):
            self._prev = sample
            self._window = [sample]
            self._state = "ALIGNING"
            self._armed = True
            self._last = HoldStatus(state=self._state, reason="hold_interrupted",
                                    dt_s=dt, samples=1)
            return self._last

        step_mm = float(np.linalg.norm(t - self._prev.t))
        step_deg = _rotation_angle_deg(self._prev.R, R)
        speed = step_mm / dt
        rate = step_deg / dt
        sample.speed_mm_s = speed
        sample.rate_deg_s = rate
        sample.dt_s = dt
        moving = (step_mm > float(cfg.move_mm)
                  or step_deg > float(cfg.move_deg))
        still = (speed <= float(cfg.max_speed_mm_s)
                 and rate <= float(cfg.max_rate_deg_s))

        # -- ACCEPTED is a one-update event; fall back to ALIGNING --------
        if self._state == "ACCEPTED":
            self._state = "ALIGNING"
            self._armed = False

        if self._state == "ALIGNING":
            if moving:
                # deliberate movement: re-arm and wait for stillness
                self._armed = True
                self._prev = sample
                self._window = [sample]
                self._last = HoldStatus(state="ALIGNING", dt_s=dt,
                                        speed_mm_s=speed, rate_deg_s=rate,
                                        samples=1)
                return self._last
            if not self._armed:
                # just accepted a hold: require a real move before the next one
                self._prev = sample
                self._window = [sample]
                self._last = HoldStatus(state="ALIGNING", dt_s=dt,
                                        speed_mm_s=speed, rate_deg_s=rate,
                                        samples=1,
                                        reason="move_to_the_next_pose")
                return self._last
            if still:
                # The previous sample and this one agree: start the attempt.
                # The previous sample is only a valid anchor if its *own*
                # measured step was also still -- otherwise a fast move into
                # the pose would pollute the accepted hold's motion metrics
                # (and its first stream row) with a large speed.
                previous = self._prev
                previous_still = (
                    (previous.speed_mm_s is None
                     or previous.speed_mm_s <= float(cfg.max_speed_mm_s))
                    and (previous.rate_deg_s is None
                         or previous.rate_deg_s <= float(cfg.max_rate_deg_s)))
                self._window = ([previous, sample] if previous_still
                                else [sample])
                self._state = "HOLDING"
                self._prev = sample
                self._last = self._status(sample)
                return self._last
            self._prev = sample
            self._window = [sample]
            self._last = HoldStatus(state="ALIGNING", dt_s=dt,
                                    speed_mm_s=speed, rate_deg_s=rate,
                                    samples=1)
            return self._last

        # -- HOLDING ------------------------------------------------------
        anchor = self._window[0]
        deviation_mm = float(np.linalg.norm(t - anchor.t))
        deviation_deg = _rotation_angle_deg(anchor.R, R)
        attempt_duration = self._prev.timestamp - anchor.timestamp
        if moving or deviation_mm > float(cfg.jitter_mm) \
                or deviation_deg > float(cfg.jitter_deg):
            # movement or drift: never accept while beyond the tolerance
            if moving:
                # a deliberate move to another position: the attempt simply
                # ended before it was long enough
                reason = "hold_too_short"
                detail = ("the plate moved before the hold was long enough "
                          f"({attempt_duration:.2f} s of {cfg.hold_s:.2f} s)")
            else:
                # small steps that nevertheless walked the pose out of the
                # tolerance: drift, never a hold
                reason = "hold_unstable"
                detail = ("the pose drifted beyond the hold tolerance "
                          f"({deviation_mm:.2f} mm / {deviation_deg:.2f} deg)")
            rejected = self._reject(reason, attempt_duration, anchor, detail)
            self._state = "ALIGNING"
            self._armed = True
            self._prev = sample
            self._window = [sample]
            self._last = HoldStatus(state="ALIGNING", dt_s=dt,
                                    speed_mm_s=speed, rate_deg_s=rate,
                                    samples=1, reason=reason,
                                    rejected=rejected)
            return self._last

        if not still:
            # micro-instability outside the D7 thresholds: keep HOLDING but the
            # clean window restarts at this sample, so the accepted hold is a
            # contiguous run of genuinely stationary measurements
            self._window = [sample]
            self._prev = sample
            self._last = HoldStatus(state="HOLDING", progress=0.0,
                                    elapsed_s=0.0, duration_s=0.0,
                                    dt_s=dt, speed_mm_s=speed,
                                    rate_deg_s=rate, samples=1,
                                    reason="hold_unsteady")
            return self._last

        self._window.append(sample)
        self._prev = sample
        elapsed = timestamp - self._window[0].timestamp
        if elapsed > float(cfg.timeout_s):
            rejected = self._reject("hold_timeout", elapsed, anchor,
                                    f"hold attempt exceeded {cfg.timeout_s:.1f} s")
            self._state = "ALIGNING"
            self._armed = True
            self._window = [sample]
            self._last = HoldStatus(state="ALIGNING", dt_s=dt,
                                    speed_mm_s=speed, rate_deg_s=rate,
                                    samples=1, reason="hold_timeout",
                                    rejected=rejected)
            return self._last
        if elapsed + 1e-9 >= float(cfg.hold_s) \
                and len(self._window) >= int(cfg.min_samples):
            hold = self._hold(self._window)
            self._accepted.append(hold)
            self._state = "ACCEPTED"
            self._armed = False
            self._window = []
            self._last = HoldStatus(state="ACCEPTED", progress=1.0,
                                    elapsed_s=float(cfg.hold_s),
                                    duration_s=float(hold["duration_s"]),
                                    dt_s=dt, speed_mm_s=speed,
                                    rate_deg_s=rate,
                                    samples=int(hold["n_samples"]),
                                    reason="accepted", hold=hold)
            return self._last
        self._last = self._status(sample, elapsed)
        return self._last

    # -- helpers --------------------------------------------------------
    def _status(self, sample: _Sample, elapsed: float | None = None) -> HoldStatus:
        if elapsed is None:
            elapsed = (0.0 if not self._window else
                       sample.timestamp - self._window[0].timestamp)
        progress = 0.0
        if self.config.hold_s > 0:
            progress = min(1.0, max(0.0, elapsed / float(self.config.hold_s)))
        return HoldStatus(state=self._state, progress=progress,
                          elapsed_s=float(elapsed),
                          duration_s=float(elapsed),
                          dt_s=sample.dt_s, speed_mm_s=sample.speed_mm_s,
                          rate_deg_s=sample.rate_deg_s,
                          samples=len(self._window),
                          reason="holding" if self._state == "HOLDING" else None)

    def _reject(self, reason: str, duration_s: float,
                anchor: _Sample, detail: str) -> dict:
        row = {"reason": reason, "duration_s": float(max(0.0, duration_s)),
               "required_s": float(self.config.hold_s),
               "detail": str(detail)}
        self._rejected.append(row)
        return row

    def _hold(self, window: Sequence[_Sample]) -> dict:
        masses = [s for s in window if s.speed_mm_s is not None]
        Rs = [s.R for s in window]
        ts = [s.t for s in window]
        R_mean = _mean_rotation(Rs)
        t_mean = np.asarray(ts, float).mean(axis=0)
        durations = [s.dt_s for s in masses if s.dt_s]
        speeds = [s.speed_mm_s for s in masses]
        rates = [s.rate_deg_s for s in masses]
        spread_mm = max(float(np.linalg.norm(np.asarray(t, float) - t_mean))
                        for t in ts)
        spread_deg = max(_rotation_angle_deg(R_mean, R) for R in Rs)
        samples = [{"t": float(s.timestamp),
                    "dt_s": (None if s.dt_s is None else float(s.dt_s)),
                    "speed_mm_s": (None if s.speed_mm_s is None
                                   else float(s.speed_mm_s)),
                    "rate_deg_s": (None if s.rate_deg_s is None
                                   else float(s.rate_deg_s))}
                   for s in window]
        return {
            "schema": HOLD_SCHEMA,
            "pose": {"R": np.asarray(R_mean, float).tolist(),
                     "t": [float(x) for x in t_mean]},
            "duration_s": float(window[-1].timestamp - window[0].timestamp),
            "n_samples": len(window),
            "started_ts": float(window[0].timestamp),
            "ended_ts": float(window[-1].timestamp),
            "dt_s": (float(np.median(durations)) if durations else None),
            "mean_speed_mm_s": (float(np.mean(speeds)) if speeds else None),
            "mean_rate_deg_s": (float(np.mean(rates)) if rates else None),
            "max_speed_mm_s": (float(np.max(speeds)) if speeds else None),
            "max_rate_deg_s": (float(np.max(rates)) if rates else None),
            "pose_spread_mm": float(spread_mm),
            "pose_spread_deg": float(spread_deg),
            "samples": samples,
            "config": self.config.as_dict(),
        }


# ---------------------------------------------------------------------------
# extension store
# ---------------------------------------------------------------------------

class ExtensionError(MissingViewsError):
    """The extension session is invalid or cannot be extended."""


class ExtensionStore:
    """Append-only extension session containing accepted dwell holds.

    Layout::

        EXTENSION/
          extension.json   manifest: base path + base digests + factory digest
          holds.jsonl      one JSON line per accepted hold, append-only

    The manifest is written once at creation (atomically).  ``holds.jsonl`` is
    only ever appended to -- existing bytes are never rewritten -- and every
    append is flushed and ``fsync``-ed, so a crash can lose at most the tail of
    the line being written.  Loading tolerates that truncated last line
    (reporting it); a base session whose recorded files no longer match the
    digests in the manifest is refused.
    """

    MANIFEST = "extension.json"
    HOLDS = "holds.jsonl"

    def __init__(self, root: str | Path, *, manifest: Mapping[str, Any],
                 verify: bool = True) -> None:
        self.root = Path(root).expanduser().resolve()
        self._manifest = dict(manifest)
        self._next_id = 0
        self._warnings: list[str] = []
        if verify:
            self.verify_base()

    # -- construction ---------------------------------------------------
    @classmethod
    def create(cls, root: str | Path, *,
               base_session_dir: str | Path,
               base_digests: Mapping[str, str | None] | "BaseEvidence",
               factory_digest: str | None = None,
               pitch_mm: float = 28.0,
               spec: Mapping[str, Any] | None = None,
               base_state: str | None = None,
               now: str | None = None) -> "ExtensionStore":
        directory = Path(root).expanduser().resolve()
        base = Path(base_session_dir).expanduser().resolve()
        if directory == base:
            raise ExtensionError(
                "the extension must be a NEW directory, never the base session")
        if isinstance(base_digests, BaseEvidence):
            digests = dict(base_digests.digests)
            factory_digest = factory_digest or base_digests.factory_sha256
            base_state = base_state or base_digests.state
        else:
            digests = {str(k): v for k, v in dict(base_digests).items()}
        directory.mkdir(parents=True, exist_ok=True)
        manifest_path = directory / cls.MANIFEST
        if manifest_path.is_file():
            raise ExtensionError(
                f"{manifest_path} already exists; open the existing extension")
        manifest = {
            "schema": EXTENSION_SCHEMA,
            "created_utc": now or _utc_now(),
            "base": {"session_dir": str(base), "digests": dict(digests),
                     "state": base_state},
            "factory_profile_digest": factory_digest,
            "pitch_mm": float(pitch_mm),
            "spec": _json_safe(spec or {}),
            "holds_file": cls.HOLDS,
            "read_only_base": True,
            "activation": activation_gate(),
        }
        _atomic_write_json(manifest_path, manifest)
        store = cls(directory, manifest=manifest, verify=False)
        store.verify_base()
        return store

    @classmethod
    def open(cls, root: str | Path, *, verify_base: bool = True) -> "ExtensionStore":
        directory = Path(root).expanduser().resolve()
        path = directory / cls.MANIFEST
        if not path.is_file():
            raise ExtensionError(f"no {cls.MANIFEST} in {directory}")
        try:
            manifest = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            raise ExtensionError(f"cannot read {path}: {exc}") from exc
        if not isinstance(manifest, Mapping) or manifest.get("schema") != EXTENSION_SCHEMA:
            raise ExtensionError(f"{path} is not a {EXTENSION_SCHEMA} manifest")
        store = cls(directory, manifest=manifest, verify=False)
        store._load_hold_count()
        if verify_base:
            store.verify_base()
        return store

    # -- manifest / digests ---------------------------------------------
    def manifest(self) -> dict:
        return _json_safe(dict(self._manifest))

    @property
    def base_session_dir(self) -> Path:
        return Path((self._manifest.get("base") or {}).get("session_dir", ""))

    @property
    def warnings(self) -> list[str]:
        return list(self._warnings)

    def verify_base(self) -> dict:
        """Recompute the base digests; refuse a changed base session."""
        base = self.base_session_dir
        expected = dict((self._manifest.get("base") or {}).get("digests") or {})
        if not base.is_dir():
            raise BaseSessionChanged(f"base session directory is gone: {base}")
        actual, sizes = _digests_of(base)
        changed, missing = [], []
        for name, digest in expected.items():
            if digest is None:
                continue
            if actual.get(name) is None:
                missing.append(name)
            elif actual.get(name) != digest:
                changed.append(name)
        if changed or missing:
            raise BaseSessionChanged(
                "the base session changed since the extension was created: "
                f"changed={changed} missing={missing}; refusing to combine "
                "evidence from a different recording")
        return {"ok": True, "changed": [], "missing": [], "base": str(base)}

    # -- holds ----------------------------------------------------------
    @property
    def holds_path(self) -> Path:
        return self.root / self.HOLDS

    def _load_hold_count(self) -> None:
        self._next_id = len(self.load_holds(strict=False))

    def load_holds(self, *, strict: bool = False) -> list[dict]:
        """Load every complete hold line (crash-truncated tail tolerated)."""
        path = self.holds_path
        if not path.is_file():
            return []
        holds: list[dict] = []
        partial: list[int] = []
        with path.open("r", encoding="utf-8") as handle:
            lines = handle.readlines()
        for index, line in enumerate(lines):
            text = line.strip()
            if not text:
                continue
            try:
                row = json.loads(text)
            except ValueError:
                if strict and index < len(lines) - 1:
                    raise ExtensionError(
                        f"corrupt hold record at line {index + 1} of {path} "
                        "(not the last line)")
                partial.append(index + 1)
                continue
            if not isinstance(row, Mapping):
                raise ExtensionError(f"hold line {index + 1} is not an object")
            holds.append(dict(row))
        if partial:
            self._warnings = [
                f"ignored crash-truncated/incomplete hold line(s) {partial}"]
        for position, hold in enumerate(holds):
            hold.setdefault("hold_id", position + 1)
        return holds

    def holds(self) -> list[dict]:
        return self.load_holds()

    def append_hold(self, hold: Mapping[str, Any]) -> dict:
        """Append one accepted hold; never rewrites existing lines."""
        self.verify_base()
        path = self.holds_path
        path.parent.mkdir(parents=True, exist_ok=True)
        record = _json_safe(dict(hold))
        record.setdefault("schema", HOLD_SCHEMA)
        record["hold_id"] = int(self._next_id) + 1
        record.setdefault("created_utc", _utc_now())
        payload = json.dumps(record, sort_keys=True, separators=(",", ":"))
        with path.open("ab+") as handle:
            handle.seek(0, os.SEEK_END)
            if handle.tell():
                handle.seek(-1, os.SEEK_END)
                if handle.read(1) != b"\n":
                    # a crash left a partial line: terminate it (append only)
                    handle.seek(0, os.SEEK_END)
                    handle.write(b"\n")
            handle.write(payload.encode("utf-8") + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        self._next_id += 1
        return record

    # -- convenience ----------------------------------------------------
    def as_json(self) -> dict:
        return {"manifest": self.manifest(),
                "n_holds": len(self.load_holds()),
                "holds_path": str(self.holds_path),
                "warnings": self.warnings}


def _atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(_json_safe(payload), indent=1, sort_keys=True)
                   + "\n", encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# combine / assess
# ---------------------------------------------------------------------------

class Combined(tuple):
    """``(views, stationarity_observations)`` -- unpackable, attribute access."""

    __slots__ = ()

    def __new__(cls, views: Sequence[Mapping[str, Any]],
                stationarity_observations: Sequence[Mapping[str, Any]]):
        return tuple.__new__(cls, (list(views), list(stationarity_observations)))

    @property
    def views(self) -> list[dict]:
        return self[0]

    @property
    def stationarity_observations(self) -> list[dict]:
        return self[1]

    @property
    def n_dwell_views(self) -> int:
        return sum(1 for view in self[0] if view.get("dwell"))


def _dwell_view(hold: Mapping[str, Any], index: int) -> dict:
    pose = _pose_rt(hold.get("pose"))
    if pose is None:
        raise ExtensionError(
            f"hold {hold.get('hold_id', index + 1)} has no usable pose")
    coverage = _cov()
    metrics = coverage.pose_metrics({"pose": pose}) or {}
    quality = dict(hold.get("quality") or {})
    return {
        "view_id": hold.get("view_id", f"hold-{hold.get('hold_id', index + 1)}"),
        "hold_id": hold.get("hold_id", index + 1),
        "pose": {"R": np.asarray(pose[0], float).tolist(),
                 "t": np.asarray(pose[1], float).tolist()},
        "image_points": hold.get("image_points") or {},
        "timestamp": _finite(hold.get("ended_ts")),
        "distance_mm": metrics.get("distance_mm"),
        "tilt_deg": metrics.get("tilt_deg"),
        "roll_deg": metrics.get("roll_deg"),
        "n_markers": dict(hold.get("n_markers") or {}),
        "quality": quality,
        "quality_passed": (bool(quality.get("passed"))
                           if quality.get("passed") is not None else None),
        "shared_markers_ir": int(hold.get("shared_markers_ir") or 0),
        "assignment_ok": bool(hold.get("assignment_ok", True)),
        "dwell": True,
        "extension": True,
        "hold": True,
        "duration_s": _finite(hold.get("duration_s")),
        "mean_speed_mm_s": _finite(hold.get("mean_speed_mm_s")),
        "mean_rate_deg_s": _finite(hold.get("mean_rate_deg_s")),
        "target": _json_safe(hold.get("target")),
    }


def _hold_rows(hold: Mapping[str, Any], view_id: Any) -> list[dict]:
    """Stationarity stream rows of one accepted hold (measured, not filled)."""
    samples = [s for s in (hold.get("samples") or [])
               if isinstance(s, Mapping) and _finite(s.get("t")) is not None]
    if len(samples) >= 2:
        config = dict(hold.get("config") or {})
        max_speed = float(config.get("max_speed_mm_s") or 3.0)
        max_rate = float(config.get("max_rate_deg_s") or 1.5)
        rows = []
        for sample in samples:
            speed = _finite(sample.get("speed_mm_s"))
            rate = _finite(sample.get("rate_deg_s"))
            still = ((speed is None or speed <= max_speed)
                     and (rate is None or rate <= max_rate))
            rows.append({
                "timestamp": float(sample["t"]),
                "group": hold.get("group"),
                "view_id": view_id,
                "accepted": True,
                "extension": True,
                "hold_id": hold.get("hold_id"),
                "stationary": bool(still),
                "stationarity_speed_mm_s": speed,
                "stationarity_rate_deg_s": rate,
                "stationarity_window_s": _finite(sample.get("dt_s")),
            })
        return rows
    # A hold record without per-sample rows still carries the measured
    # aggregate; two rows at the measured start/end are the honest minimum.
    started = _finite(hold.get("started_ts"))
    ended = _finite(hold.get("ended_ts"))
    duration = _finite(hold.get("duration_s"))
    if started is None or ended is None or duration is None or duration <= 0:
        raise ExtensionError(
            f"hold {hold.get('hold_id')} has neither samples nor a measured "
            "start/end duration")
    # Without per-sample rows only the aggregate survives.  Do not claim
    # "stationary" from an aggregate that is missing or above the D7 limit:
    # a fabricated still would let an unmeasured hold count as evidence.
    mean_speed = _finite(hold.get("mean_speed_mm_s"))
    mean_rate = _finite(hold.get("mean_rate_deg_s"))
    config = dict(hold.get("config") or {})
    max_speed = float(config.get("max_speed_mm_s") or 3.0)
    max_rate = float(config.get("max_rate_deg_s") or 1.5)
    if mean_speed is None or mean_rate is None:
        raise ExtensionError(
            f"hold {hold.get('hold_id')} has no per-sample rows and no "
            "measured mean motion; refusing to mark it stationary")
    still = mean_speed <= max_speed and mean_rate <= max_rate
    return [{
        "timestamp": float(started), "group": hold.get("group"),
        "view_id": view_id, "accepted": True, "extension": True,
        "hold_id": hold.get("hold_id"), "stationary": bool(still),
        "stationarity_speed_mm_s": mean_speed,
        "stationarity_rate_deg_s": mean_rate,
        "stationarity_window_s": float(duration),
    }, {
        "timestamp": float(ended), "group": hold.get("group"),
        "view_id": view_id, "accepted": True, "extension": True,
        "hold_id": hold.get("hold_id"), "stationary": bool(still),
        "stationarity_speed_mm_s": mean_speed,
        "stationarity_rate_deg_s": mean_rate,
        "stationarity_window_s": float(duration),
    }]


def combine(base_views: Any, extension_holds: Sequence[Mapping[str, Any]], *,
            base_stationarity_observations: Sequence[Mapping[str, Any]] | None = None
            ) -> Combined:
    """Base accepted views + one ``dwell=True`` view per accepted hold.

    ``base_views`` may be a :class:`BaseEvidence` (then its stationarity rows
    are included automatically) or a plain sequence of coverage view records.
    The returned :class:`Combined` unpacks as ``(views, stationarity_rows)``
    and also exposes ``.views`` / ``.stationarity_observations``, so

    ``coverage.measure(combined.views, stationarity_observations=combined.stationarity_observations)``

    works unchanged.  Dwell views are excluded from D1-D6 by the coverage
    module and counted only for D7 quality/stationarity, which is exactly the
    contract for this evidence.
    """
    if isinstance(base_views, BaseEvidence):
        base = list(base_views.views)
        if base_stationarity_observations is None:
            base_stationarity_observations = base_views.stationarity_observations
    else:
        base = [dict(view) for view in (base_views or [])]
    rows = [dict(row) for row in (base_stationarity_observations or [])]
    views = list(base)
    for index, hold in enumerate(extension_holds or []):
        view = _dwell_view(hold, index)
        views.append(view)
        rows.extend(_hold_rows(hold, view.get("view_id")))
    return Combined(views, rows)


@dataclass
class Assessment:
    """Coverage measurement + readiness + the explicit non-activation gate."""

    measurement: dict
    readiness: dict
    activation: dict
    n_dwell_views: int = 0

    @property
    def ready(self) -> bool:
        return bool(self.readiness.get("ready"))

    @property
    def satisfied(self) -> int:
        return int(sum(1 for dim in _DIMENSIONS
                       if (self.measurement.get(dim) or {}).get("satisfied")))

    @property
    def total(self) -> int:
        return len(_DIMENSIONS)

    def dimensions(self) -> dict:
        out = {}
        for dim in _DIMENSIONS:
            node = self.measurement.get(dim) or {}
            out[dim] = {"satisfied": bool(node.get("satisfied")),
                        "label": node.get("label"),
                        "deficit": node.get("deficit"),
                        "hint": node.get("hint"),
                        "checks": list(node.get("checks") or [])}
        return out

    def as_dict(self) -> dict:
        return {
            "schema": SCHEMA,
            "ready": self.ready,
            "satisfied": self.satisfied,
            "total": self.total,
            "n_views": int(self.measurement.get("n_views") or 0),
            "n_dwell_views": int(self.n_dwell_views),
            "unsatisfied_dimensions": list(
                self.readiness.get("unsatisfied_dimensions") or []),
            "unsatisfied": list(self.readiness.get("unsatisfied") or []),
            "summary": self.readiness.get("summary", ""),
            "dimensions": self.dimensions(),
            "activation": dict(self.activation),
        }


def _measurement_of(assessment: Any) -> dict:
    if isinstance(assessment, Assessment):
        return assessment.measurement
    if isinstance(assessment, Mapping):
        measurement = assessment.get("measurement")
        if isinstance(measurement, Mapping) and "checks" in measurement:
            return dict(measurement)
        if "checks" in assessment or "D1" in assessment:
            return dict(assessment)
    raise TypeError("expected an Assessment or a coverage measurement dict")


_DIMENSIONS = ("D1", "D2", "D3", "D4", "D5", "D6", "D7", "D8")


def assess(views: Any, stationarity_observations: Sequence[Mapping[str, Any]] | None = None,
           spec: Any = None) -> Assessment:
    """Measure coverage over combined evidence; never activates anything."""
    coverage = _cov()
    if isinstance(views, Combined):
        if stationarity_observations is None:
            stationarity_observations = views.stationarity_observations
        views = views.views
    elif isinstance(views, BaseEvidence):
        if stationarity_observations is None:
            stationarity_observations = views.stationarity_observations
        views = views.views
    spec_object = _spec_object(spec)
    measurement = coverage.measure(
        list(views or []), spec_object,
        stationarity_observations=(None if stationarity_observations is None
                                   else list(stationarity_observations)))
    verdict = coverage.readiness(measurement)
    n_dwell = sum(1 for view in (views or [])
                  if isinstance(view, Mapping) and view.get("dwell"))
    return Assessment(measurement=measurement,
                      readiness=_json_safe(verdict), activation=activation_gate(),
                      n_dwell_views=int(n_dwell))


def _as_assessment(value: Any) -> Assessment:
    """Accept an :class:`Assessment`, a measurement dict, or views."""
    if isinstance(value, Assessment):
        return value
    if isinstance(value, Mapping) and ("checks" in value or "D1" in value):
        coverage = _cov()
        measurement = dict(value)
        return Assessment(measurement=measurement,
                          readiness=_json_safe(coverage.readiness(measurement)),
                          activation=activation_gate(),
                          n_dwell_views=int(measurement.get("n_dwell_views") or 0))
    return assess(value)


def readiness_verdict(assessment: Any) -> dict:
    """The compact operator-facing verdict (no per-view detail)."""
    return _as_assessment(assessment).as_dict()


def assessment_json(assessment: Assessment | Any) -> dict:
    """JSON-safe compact assessment (measurement detail stays in memory)."""
    return _json_safe(_as_assessment(assessment).as_dict())


def activation_gate() -> dict:
    """The explicit activation gate.

    Combining and assessing evidence never activates a profile.  This is the
    single, self-describing gate the CLI, the controller and the docs use.
    """
    return {
        "activated": False,
        "activation_available_here": False,
        "requires": ("an explicit tools.wizard.lifecycle.activate(candidate, "
                     "store_root, ...) call after a validated candidate and a "
                     "separate operator decision"),
        "reason": ("missing-view capture/assessment is acquisition evidence "
                   "only; it never writes, installs or activates a profile"),
        "factory_profile_modified": False,
    }


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------

def _base_views(base: Any) -> list[dict]:
    if base is None:
        return []
    if isinstance(base, BaseEvidence):
        return list(base.views)
    if isinstance(base, Combined):
        return [view for view in base.views if not view.get("dwell")]
    return [dict(view) for view in base if isinstance(view, Mapping)]


def _last_pose(views: Sequence[Mapping[str, Any]]) -> tuple | None:
    for view in reversed(views):
        pose = _pose_rt(view.get("pose"))
        if pose is not None:
            return pose
    return None


def _nearest_band(spec: Any, distance_mm: float) -> int:
    centres = [0.5 * (lo + hi) for lo, hi in spec.distance_bands_mm]
    return int(min(range(len(centres)), key=lambda i: abs(centres[i] - distance_mm)))


def _median_distance(views: Sequence[Mapping[str, Any]], fallback: float = 420.0) -> float:
    distances = []
    coverage = _cov()
    for view in views:
        metrics = coverage.pose_metrics(view)
        if metrics is not None:
            distances.append(float(metrics["distance_mm"]))
    return float(np.median(distances)) if distances else float(fallback)


def _target_pose(distance_mm: float, tilt_x_deg: float, tilt_y_deg: float,
                 roll_deg: float, cell: tuple[int, int],
                 *, corner: bool = False) -> tuple[np.ndarray, np.ndarray]:
    """A board pose whose centre lands in ``cell`` (3x3, row/col)."""
    coverage = _cov()
    row, col = int(cell[0]), int(cell[1])
    fx, fy = (0.27, 0.21) if corner else (0.22, 0.17)
    lateral = np.array([(col - 1) * fx * float(distance_mm),
                        (row - 1) * fy * float(distance_mm)], float)
    z = math.sqrt(max(float(distance_mm) ** 2
                      - float(lateral @ lateral), 1.0))
    return coverage.family_pose(z, float(tilt_x_deg), float(tilt_y_deg),
                                float(roll_deg), (float(lateral[0]),
                                                  float(lateral[1])))


def describe_target(target: Mapping[str, Any]) -> str:
    """Static operator sentence for a machine target (no current pose needed)."""
    verb = "hold the plate at" if target.get("kind") == "dwell" \
        else "put the plate at"
    parts = [f"{verb} ~{float(target['distance_mm']):.0f} mm"]
    tx = float(target.get("tilt_x_deg") or 0.0)
    ty = float(target.get("tilt_y_deg") or 0.0)
    tilts = []
    if abs(tx) >= 2.0:
        tilts.append(f"{abs(tx):.0f} deg {'right' if tx > 0 else 'left'}")
    if abs(ty) >= 2.0:
        tilts.append(f"{abs(ty):.0f} deg {'up' if ty > 0 else 'down'}")
    if tilts:
        parts.append("tilt " + " and ".join(tilts))
    roll = float(target.get("roll_deg") or 0.0)
    signed_roll = (roll + 180.0) % 360.0 - 180.0
    if abs(signed_roll) >= 5.0:
        parts.append(f"rotate it {abs(signed_roll):.0f} deg "
                     f"{'clockwise' if signed_roll < 0 else 'anticlockwise'}")
    name = target.get("image_cell_name") or cell_name(target.get("image_cell"))
    if name:
        parts.append(f"with the plate centre in the {name} of the image")
    return ", ".join(parts)


def _machine_target(spec: Any, distance_mm: float, band_index: int,
                    tilt_x_deg: float, tilt_y_deg: float, roll_deg: float,
                    roll_bin: int, cell: tuple[int, int], *,
                    hold_s: float | None, kind: str,
                    extra: Mapping[str, Any] | None = None) -> dict:
    cell = (int(cell[0]), int(cell[1]))
    target = {
        "kind": kind,
        "distance_mm": float(distance_mm),
        "distance_range_mm": list(spec.distance_bands_mm[band_index])
        if 0 <= band_index < len(spec.distance_bands_mm) else None,
        "band_index": int(band_index),
        "tilt_x_deg": float(tilt_x_deg),
        "tilt_y_deg": float(tilt_y_deg),
        "tilt_quadrant": ("+x" if tilt_x_deg >= 0 else "-x")
        + ("+y" if tilt_y_deg >= 0 else "-y"),
        "roll_deg": float(roll_deg),
        "roll_sector": int(roll_bin),
        "roll_range_deg": [float(roll_bin) * spec.roll_bin_size_deg(),
                           float(roll_bin + 1) * spec.roll_bin_size_deg()],
        "image_cell": [cell[0], cell[1]],
        "image_cell_name": cell_name(cell),
        "hold_s": (None if hold_s is None else float(hold_s)),
    }
    if extra:
        target.update(_json_safe(dict(extra)))
    return target


def _effort(current: tuple | None, target: tuple) -> dict:
    coverage = _cov()
    if current is None:
        return {"score": 0.0, "lateral_mm": 0.0, "depth_mm": 0.0,
                "rotation_deg": 0.0, "move_mm": [0.0, 0.0, 0.0]}
    return _json_safe(coverage.pose_effort(current, target))


def _reachable(current: tuple | None, target: tuple) -> bool | None:
    """One-natural-movement reachability, using ``calib.guidance.reachable``."""
    if current is None:
        return None
    try:
        from calib import guidance
        from calib.adaptive import ViewCandidate
        R, t = target
        candidate = ViewCandidate(0.0, 0.0, 0.0, 0.0, R, t, "target")
        return bool(guidance.reachable(current, [candidate]))
    except Exception:
        return None


def _pose_entry(*, dimension: str, kind: str, reason: str,
                instruction: str, target: Mapping[str, Any],
                pose: tuple[np.ndarray, np.ndarray],
                current: tuple | None, spec: Any,
                expected: str, visits_needed: int = 1,
                hold_s: float | None = None) -> dict:
    R, t = pose
    return {
        "dimension": dimension,
        "kind": kind,
        "reason": str(reason),
        "instruction": str(instruction),
        "description": describe_target(target),
        "expected": str(expected),
        "target": _json_safe(dict(target)),
        "machine_target": _json_safe(dict(target)),
        "pose": {"R": np.asarray(R, float).tolist(),
                 "t": [float(x) for x in np.asarray(t, float).ravel()]},
        "hold_s": (None if hold_s is None else float(hold_s)),
        "visits_needed": int(visits_needed),
        "effort": _effort(current, (R, t)),
        "reachable": _reachable(current, (R, t)),
    }


def describe_move(current: tuple | None, target_pose: tuple) -> str:
    """Plate-relative movement words (the operator holds the plate).

    ``calib.guidance.instruction`` describes *scanner* motion around a fixed
    plate (its docstring is explicit about that: "what the operator does when
    holding the scanner").  The completion workflow is the inverse situation --
    the scanner stays on its stand and the operator moves the plate -- so the
    same axes and words are used with a plate-relative delta: the plate centre
    translation ``tt - tc`` in the camera frame, the change of the measured
    tilt/roll angles, and the change of the camera distance ``norm(t)``.
    ``calib.guidance.reachable`` is reused unchanged for the one-move
    reachability check.
    """
    if current is None:
        return ""
    coverage = _cov()
    rc, tc = current
    rt, tt = target_pose
    delta = np.asarray(tt, float) - np.asarray(tc, float)
    parts: list[str] = []
    if abs(float(delta[0])) >= 15.0:
        parts.append(f"move the plate "
                     f"{'right' if delta[0] > 0 else 'left'} "
                     f"{abs(float(delta[0])):.0f} mm")
    if abs(float(delta[1])) >= 15.0:
        parts.append(f"{'down' if delta[1] > 0 else 'up'} "
                     f"{abs(float(delta[1])):.0f} mm")
    distance_change = (float(np.linalg.norm(tt)) - float(np.linalg.norm(tc)))
    if abs(distance_change) >= 10.0:
        parts.append(f"{'further' if distance_change > 0 else 'closer'} by "
                     f"{abs(distance_change):.0f} mm")
    tx_c, ty_c = coverage.tilt_deg(np.asarray(rc, float))
    tx_t, ty_t = coverage.tilt_deg(np.asarray(rt, float))
    dtx, dty = tx_t - tx_c, ty_t - ty_c
    if abs(dtx) >= 3.0:
        parts.append(f"tilt {abs(dtx):.0f} deg "
                     f"{'right' if dtx > 0 else 'left'}")
    if abs(dty) >= 3.0:
        parts.append(f"tilt {abs(dty):.0f} deg "
                     f"{'up' if dty > 0 else 'down'}")
    roll_c = coverage.roll_deg(np.asarray(rc, float))
    roll_t = coverage.roll_deg(np.asarray(rt, float))
    droll = (roll_t - roll_c + 180.0) % 360.0 - 180.0
    if abs(droll) >= 10.0:
        parts.append(f"rotate the plate {abs(droll):.0f} deg "
                     f"{'clockwise' if droll < 0 else 'anticlockwise'}")
    return ", ".join(parts)


def _relative_instruction(current: tuple | None, target_pose: tuple,
                          suffix: str) -> str:
    """``"<plate move>, <suffix>"`` with a useful fallback for no pose."""
    move = describe_move(current, target_pose)
    text = (move + ", " + suffix).strip() if move else suffix
    text = text.strip()
    if not text:
        return suffix
    return text[:1].upper() + text[1:]


def _dwell_entries(measurement: Mapping[str, Any], base_views: Sequence[Mapping[str, Any]],
                   spec: Any, deficit: int) -> list[dict]:
    """The minimal set of distinct D7 poses, balanced over the coverage bins."""
    deficit = int(max(0, deficit))
    coverage = _cov()
    tilt = float(spec.min_tilt_deg) + 5.0
    band_centres = [0.5 * (lo + hi) for lo, hi in spec.distance_bands_mm]
    # Roll-sector interiors.  ``0/90/180/270`` are unambiguous operator
    # orientations ("rotate it 90 deg") and still fall in four distinct
    # coverage roll bins for the four-sector spec.
    roll_centres = [(i * 360.0 / max(int(spec.roll_bins), 1))
                    for i in range(int(spec.roll_bins))]
    quadrants = [(1, 1), (1, -1), (-1, 1), (-1, -1)]
    cells = [(0, 0), (0, 2), (2, 0), (2, 2), (0, 1), (1, 0), (1, 2), (2, 1),
             (1, 1)]
    base_keys = set()
    for view in base_views:
        signature = pose_signature(view.get("pose"), spec, view)
        key = _signature_key(signature)
        if key is not None:
            base_keys.add(key)

    candidates = []
    for band_index, distance in enumerate(band_centres):
        for quadrant in quadrants:
            for roll_index, roll in enumerate(roll_centres):
                for cell in cells:
                    target = _target_pose(distance, quadrant[0] * tilt,
                                          quadrant[1] * tilt, roll, cell)
                    candidates.append({
                        "band_index": band_index, "distance_mm": float(distance),
                        "quadrant": quadrant, "roll_bin": roll_index,
                        "roll_deg": float(roll), "cell": cell,
                        "pose": target,
                    })
    available = [c for c in candidates
                 if _signature_key({"band_index": c["band_index"],
                                    "quadrant": c["quadrant"],
                                    "roll_bin": c["roll_bin"],
                                    "cell": c["cell"]}) not in base_keys]
    if len(available) < deficit:
        available = candidates
    usage_band = Counter()
    usage_quad = Counter()
    usage_roll = Counter()
    usage_cell = Counter()
    chosen: list[dict] = []
    chosen_keys: set = set()
    current = _last_pose(base_views)
    while len(chosen) < deficit:
        best = None
        best_score = None
        for candidate in available:
            key = (candidate["band_index"], candidate["quadrant"],
                   candidate["roll_bin"], candidate["cell"])
            if key in chosen_keys:
                continue
            score = (usage_band[key[0]] + usage_quad[key[1]]
                     + usage_roll[key[2]] + usage_cell[key[3]],
                     round(float(_effort(current, candidate["pose"])["score"]), 6),
                     candidate["band_index"], candidate["quadrant"],
                     candidate["roll_bin"], candidate["cell"])
            if best_score is None or score < best_score:
                best, best_score = candidate, score
        if best is None:
            break
        chosen.append(best)
        chosen_keys.add((best["band_index"], best["quadrant"],
                         best["roll_bin"], best["cell"]))
        usage_band[best["band_index"]] += 1
        usage_quad[best["quadrant"]] += 1
        usage_roll[best["roll_bin"]] += 1
        usage_cell[best["cell"]] += 1
        current = best["pose"]

    intervals = int((((measurement.get("checks") or {})
                      .get("stationarity") or {}).get("measured") or {})
                    .get("n_stationary_intervals") or 0)
    required = int(spec.min_stationary_views)
    # order the chosen *set* by travel: nearest-neighbour from the last
    # recorded pose, then from each target.  The set is already balanced over
    # the coverage bins; ordering only makes the walk shorter.
    ordered: list[dict] = []
    remaining = list(chosen)
    current = _last_pose(base_views)
    while remaining:
        best = min(remaining, key=lambda candidate: round(float(
            _effort(current, candidate["pose"])["score"]), 6))
        ordered.append(best)
        remaining.remove(best)
        current = best["pose"]
    chosen = ordered
    entries = []
    previous = _last_pose(base_views)
    for index, choice in enumerate(chosen):
        target = _machine_target(
            spec, choice["distance_mm"], choice["band_index"],
            choice["quadrant"][0] * tilt, choice["quadrant"][1] * tilt,
            choice["roll_deg"], choice["roll_bin"], choice["cell"],
            hold_s=1.0, kind="dwell")
        suffix = (f"then hold it still for {1.0:.1f} s.  This is hold "
                  f"{index + 1} of {len(chosen)} needed to reach "
                  f"{required} distinct dwell intervals.")
        instruction = _relative_instruction(previous, choice["pose"], suffix)
        reason = (f"D7 stationarity needs {deficit} more distinct dwell "
                  f"interval(s): {intervals} recorded, {required} required.  "
                  "A dwell counts only when it is a genuine still hold at a "
                  "pose not already covered by the session.")
        entries.append(_pose_entry(
            dimension="D7", kind="dwell", reason=reason,
            instruction=instruction, target=target, pose=choice["pose"],
            current=previous, spec=spec,
            expected="one distinct stationary dwell interval", hold_s=1.0))
        entries[-1]["visits_needed"] = 1
        previous = choice["pose"]
    return entries


def _pose_dimension_entries(measurement: Mapping[str, Any],
                            base_views: Sequence[Mapping[str, Any]],
                            spec: Any) -> list[dict]:
    """Best-effort pose targets for D1-D4 (the pose-coverage dimensions).

    The base session normally satisfies these already; this path exists so the
    workflow does not fall back to "walk around until the gate is happy" when
    it does not.  It reuses the coverage module's bin definitions and
    ``calib.guidance.instruction`` for the operator sentence.
    """
    coverage = _cov()
    checks = measurement.get("checks") or {}
    entries: list[dict] = []
    current = _last_pose(base_views)
    median = _median_distance(base_views)
    band_index = _nearest_band(spec, median)
    band_distance = 0.5 * (spec.distance_bands_mm[band_index][0]
                           + spec.distance_bands_mm[band_index][1])

    # D1 -- an unvisited distance band (one solid pose per missing band).
    for check_name, dimension in (("distance_bands", "D1"),):
        node = checks.get(check_name) or {}
        if node.get("satisfied", True):
            continue
        measured = node.get("measured") or {}
        counts = measured.get("band_counts") or []
        for index, count in enumerate(counts):
            need = int(spec.min_views_per_distance_band) - int(count)
            if need <= 0:
                continue
            distance = 0.5 * (spec.distance_bands_mm[index][0]
                              + spec.distance_bands_mm[index][1])
            pose = _target_pose(distance, 0.0, 0.0, 0.0, (1, 1))
            target = _machine_target(spec, distance, index, 0.0, 0.0, 0.0,
                                     0, (1, 1), hold_s=None, kind="pose")
            reason = (f"D1 distance band {index} has {count} view(s); "
                      f"{spec.min_views_per_distance_band} required.")
            instruction = _relative_instruction(
                current, pose, "capture one sharp still")
            entries.append(_pose_entry(
                dimension=dimension, kind="pose", reason=reason,
                instruction=instruction, target=target, pose=pose,
                current=current, spec=spec,
                expected="one view inside the missing distance band",
                visits_needed=need))
            current = pose

    # D2 -- missing tilt signs / quadrants.
    tilt_checks = (checks.get("tilt_axis_x"), checks.get("tilt_axis_y"),
                   checks.get("tilt_quadrants"))
    if any(node and not node.get("satisfied", True) for node in tilt_checks):
        stats = measurement.get("pose_stats") or {}
        centre_x = float(stats.get("tilt_x_centre_deg") or 0.0)
        centre_y = float(stats.get("tilt_y_centre_deg") or 0.0)
        tilt = float(spec.min_tilt_deg) + 5.0
        quadrants = checks.get("tilt_quadrants") or {}
        measured = quadrants.get("measured") or {}
        for name, sign_x, sign_y in (("+x+y", 1, 1), ("+x-y", 1, -1),
                                     ("-x+y", -1, 1), ("-x-y", -1, -1)):
            count = int(measured.get(name, 0) or 0)
            need = int(spec.min_views_per_tilt_quadrant) - count
            if need <= 0:
                continue
            pose = _target_pose(band_distance, centre_x + sign_x * tilt,
                                centre_y + sign_y * tilt, 0.0, (1, 1))
            target = _machine_target(
                spec, band_distance, band_index, centre_x + sign_x * tilt,
                centre_y + sign_y * tilt, 0.0, 0, (1, 1), hold_s=None,
                kind="pose")
            reason = (f"D2 tilt quadrant {name} has {count} view(s); "
                      f"{spec.min_views_per_tilt_quadrant} required.")
            instruction = _relative_instruction(
                current, pose, "capture one sharp still")
            entries.append(_pose_entry(
                dimension="D2", kind="pose", reason=reason,
                instruction=instruction, target=target, pose=pose,
                current=current, spec=spec,
                expected="one view in the missing tilt quadrant",
                visits_needed=need))
            current = pose

    # D3 -- missing roll sectors.
    node = checks.get("roll_sectors") or {}
    if node and not node.get("satisfied", True):
        counts = (node.get("measured") or {}).get("bin_counts") or []
        size = spec.roll_bin_size_deg()
        for index, count in enumerate(counts):
            need = int(spec.min_views_per_roll_bin) - int(count)
            if need <= 0:
                continue
            roll = (index + 0.5) * size
            pose = _target_pose(band_distance, 0.0, 0.0, roll, (1, 1))
            target = _machine_target(spec, band_distance, band_index, 0.0,
                                     0.0, roll, index, (1, 1), hold_s=None,
                                     kind="pose")
            reason = (f"D3 roll sector {index} has {count} view(s); "
                      f"{spec.min_views_per_roll_bin} required.")
            instruction = _relative_instruction(
                current, pose, "capture one sharp still")
            entries.append(_pose_entry(
                dimension="D3", kind="pose", reason=reason,
                instruction=instruction, target=target, pose=pose,
                current=current, spec=spec,
                expected="one view in the missing roll sector",
                visits_needed=need))
            current = pose

    # D4 -- missing image corners / regions (0.27/0.21 offsets; see
    # docs/calibration/coverage_stationarity_fix.md, validated on the factory
    # rig at 340/420/510 mm with >= 28 markers in each IR camera).
    corners = checks.get("image_corners") or {}
    regions = checks.get("image_regions") or {}
    if (corners and not corners.get("satisfied", True)) or \
            (regions and not regions.get("satisfied", True)):
        distance = band_distance
        cells: list[tuple[int, int]] = []
        corner_names = {(0, 0): "top_left", (0, 2): "top_right",
                        (2, 0): "bottom_left", (2, 2): "bottom_right"}
        corner_counts = (corners.get("measured") or {}).get("corners") or {}
        for cell, name in corner_names.items():
            if int(corner_counts.get(name, 0) or 0) < int(spec.min_corner_views):
                cells.append(cell)
        region_counts = (regions.get("measured") or {}).get("region_counts") or []
        if regions and not regions.get("satisfied", True) and len(region_counts) >= 9:
            for row in range(3):
                for col in range(3):
                    cell = (row, col)
                    if region_counts[row * 3 + col] == 0 and cell not in cells:
                        cells.append(cell)
        for cell in cells:
            pose = _target_pose(distance, 0.0, 0.0, 0.0, cell,
                                corner=not (1 in cell))
            target = _machine_target(spec, distance, band_index, 0.0, 0.0,
                                     0.0, 0, cell, hold_s=None, kind="pose")
            label = cell_name(cell) or "image cell"
            reason = (f"D4 genuinely lacks a view with the plate centre in the "
                      f"{label} cell.")
            instruction = _relative_instruction(
                current, pose,
                f"put the plate centre in the {label} of the image and capture "
                "one sharp still")
            entries.append(_pose_entry(
                dimension="D4", kind="pose", reason=reason,
                instruction=instruction, target=target, pose=pose,
                current=current, spec=spec,
                expected="one view in the missing image cell",
                visits_needed=1))
            current = pose

    # D5 / D6 -- visibility problems, not pose-coverage deficits: one central
    # pose with the specific recorded hint so the operator can act, while the
    # reason says plainly that this may need a re-detection rather than a new
    # calibration walk.
    for check_name, dimension in (("stereo_overlap", "D5"),
                                  ("colour", "D6")):
        node = checks.get(check_name) or {}
        if not node or node.get("satisfied", True):
            continue
        pose = _target_pose(band_distance, 0.0, 0.0, 0.0, (1, 1))
        target = _machine_target(spec, band_distance, band_index, 0.0, 0.0,
                                 0.0, 0, (1, 1), hold_s=None, kind="visibility")
        reason = str(node.get("hint") or f"{dimension} is unsatisfied")
        entries.append(_pose_entry(
            dimension=dimension, kind="visibility", reason=reason,
            instruction=_relative_instruction(
                current, pose, "hold both IR cameras' view of the plate and "
                "capture one sharp still"),
            target=target, pose=pose, current=current, spec=spec,
            expected=f"{dimension} visibility evidence (may need re-detection)",
            visits_needed=1))
        current = pose

    # D8 -- a known-length artefact is placed in view; a pose alone cannot
    # satisfy it, so the entry states that explicitly.
    node = checks.get("scale_artefact") or {}
    if node and node.get("required", False) and not node.get("satisfied", True):
        pose = _target_pose(band_distance, 0.0, 0.0, 0.0, (1, 1))
        target = _machine_target(spec, band_distance, band_index, 0.0, 0.0,
                                 0.0, 0, (1, 1), hold_s=None,
                                 kind="scale_artefact")
        entries.append(_pose_entry(
            dimension="D8", kind="scale_artefact",
            reason=str(node.get("hint") or "D8 scale artefact is unsatisfied"),
            instruction=_relative_instruction(
                current, pose, "put a known-length artefact on the plate and "
                "capture it in this pose"),
            target=target, pose=pose, current=current, spec=spec,
            expected="scale-artefact views (an object, not a board pose alone)",
            visits_needed=int(spec.min_views_per_scale_artefact)))
    return entries


def plan(assessment: Any, base_evidence: Any = None, spec: Any = None, *,
         cap: int = MAX_PLAN_POSES, now: str | None = None) -> dict:
    """Minimal ordered pose plan that closes the remaining coverage deficit.

    D7's deficit is ``min_stationary_views - n_intervals`` and is emitted as
    that many distinct hold targets; the pose dimensions (D1-D4) emit one
    target per missing bin/cell.  The plan is capped at ``cap`` poses and any
    remainder is reported in ``deferred`` instead of silently dropped.

    "Distinct from the existing dwell poses" is enforced against **every
    accepted view of the base session**, because the recovered stationarity
    stream records intervals (timestamps/motion), not the board pose of each
    old dwell.  Every accepted view is therefore treated as already covered,
    which is a conservative superset of the true dwell set.

    Every pose entry carries ``reason``, ``instruction`` (operator language),
    ``dimension``, ``target`` (machine-readable: distance range, tilt/roll
    sector, image cell, hold length) and ``pose`` (the full target pose).
    """
    measurement = _measurement_of(assessment)
    coverage = _cov()
    spec_object = _spec_object(spec)
    base_views = _base_views(base_evidence)
    verdict = coverage.readiness(measurement)
    cap = max(0, int(cap))

    entries: list[dict] = []
    if not verdict.get("ready"):
        entries.extend(_pose_dimension_entries(measurement, base_views,
                                               spec_object))
        stationarity = ((measurement.get("checks") or {})
                        .get("stationarity") or {})
        required = int(spec_object.min_stationary_views)
        intervals = int((stationarity.get("measured") or {})
                        .get("n_stationary_intervals") or 0)
        deficit = max(0, required - intervals)
        if stationarity and not stationarity.get("satisfied", True) and deficit:
            entries.extend(_dwell_entries(measurement, base_views,
                                          spec_object, deficit))

    kept = entries[:cap]
    deferred = entries[cap:]
    for index, entry in enumerate(kept):
        entry["index"] = index + 1
    deferred_counts: dict[str, int] = {}
    for entry in deferred:
        key = str(entry.get("dimension") or "?")
        deferred_counts[key] = deferred_counts.get(key, 0) + 1
    notes = []
    if deferred:
        notes.append(
            f"{len(deferred)} further pose(s) are deferred by the "
            f"{cap}-pose cap ({deferred_counts}); run a second extension after "
            "this one if they are still missing.")
    if not kept:
        notes.append("nothing to capture: the assessed evidence is already "
                     "READY (or no posed dimension is outstanding).")
    if verdict.get("ready"):
        notes.append("the session is READY as assessed; capturing more views "
                     "is optional refinement, not a requirement")
    return {
        "schema": f"{SCHEMA}/plan",
        "created_utc": now or _utc_now(),
        "ready_before": bool(verdict.get("ready")),
        "satisfied": int(sum(1 for dim in _DIMENSIONS
                             if (measurement.get(dim) or {}).get("satisfied"))),
        "total": len(_DIMENSIONS),
        "cap": cap,
        "n_planned": len(kept),
        "estimated_minutes": round(float(len(kept)) * 0.4, 2),
        "poses": kept,
        "deferred": [{"dimension": entry.get("dimension"),
                      "kind": entry.get("kind"),
                      "reason": entry.get("reason")} for entry in deferred],
        "deferred_counts": deferred_counts,
        "notes": notes,
        "activation": activation_gate(),
    }
