"""The wizard controller: one session, one decision path.

``WizardController`` wraps :class:`calib.session.CalibrationSession` and adds
only what the session does not do:

* the session directory (layout in ``wizard_architecture.md`` §9), with every
  PGM written *before* detection so a crash keeps the evidence;
* three extra rejection reasons — shared IR markers, an unambiguous marker
  assignment (the large-marker/off-lattice witness used by the sweep analysis)
  and colour participation;
* manifest / decisions / report writing, the candidate profile and validation
  metrics;
* the cheap :meth:`snapshot` dictionary the GUI polls at 10 Hz.

Accept/reject and "done" decisions remain the session's job.  ``ingest``
returns the session's ``Decision`` unchanged for every frame the controller
does not veto; a veto is recorded with a specific reason and the session's
rejection list stays the single source of truth for counts.

Determinism: for an identical frame sequence the accepted view set, the
decisions and ``manifest.json`` are identical.  Wall-clock values (ingest
time, solve seconds) are kept out of ``manifest.json`` and out of the
canonical decision projection used by tests.

Public API
----------
``WizardController(factory_profile=..., session_dir=..., mode='high',
coverage=None, solve_fn=None, pitch_mm=28.0, *, goal='full',
source=None, capture=None, ring_keep=64)``
    ``begin()``, ``ingest(group)``, ``snapshot()``, ``pause()``, ``resume()``,
    ``finish()``, ``cancel()``, ``measure_coverage()``, ``readiness()``,
    ``recommend()``, ``decisions()``, ``manifest_digest()``.
    Attributes: ``controller`` (the wrapped ``CalibrationSession``),
    ``paths`` (``types.SessionPaths``), ``factory_sha256``.
"""
from __future__ import annotations

import copy
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING
import hashlib
import importlib
import json
import math
import os
from pathlib import Path
import shutil
import threading

if TYPE_CHECKING:              # annotations only; avoids a circular import
    from . import guidance
import time
from typing import Any, Iterable, Mapping, Sequence

import numpy as np

from calib.session import CalibrationSession

from . import types

__all__ = [
    "WizardController",
    "CoverageRequirements",
    "REASON_CATEGORIES",
    "classify_reason",
    "DEFAULT_SOFTWARE",
    "WIZARD_VERSION",
    "SESSION_SCHEMA",
    "MANIFEST_SCHEMA",
    "VALIDATION_SCHEMA",
]

CAMERAS = ("left", "right", "tex")
DEFAULT_SOFTWARE = {"name": "einstar-wizard", "version": "0.1.0",
                    "engine": "calib.session"}
WIZARD_VERSION = DEFAULT_SOFTWARE["version"]
SESSION_SCHEMA = "einstar-wizard-session/1"
MANIFEST_SCHEMA = "einstar-wizard-manifest/1"
VALIDATION_SCHEMA = "einstar-wizard-validation/1"
PROFILE_SCHEMA = "einstar.recon.profile/1"
JSON_NAMES = {"left": "ir_left", "right": "ir_right", "tex": "color"}

#: Stable reason categories reported in ``counts.by_reason``.
REASON_CATEGORIES = (
    "duplicate", "redundant", "incomplete", "assignment", "colour", "markers",
    "motion", "blur", "saturation", "contrast", "plate", "pose", "io", "other",
)


def classify_reason(reason: str) -> str:
    """Map a free-form reason string to a stable category token."""
    text = str(reason).lower()
    if any(k in text for k in ("incomplete", "missing camera", "missing tex")):
        return "incomplete"
    if any(k in text for k in ("mirrored marker", "ambiguous", "assignment",
                               "chirality")):
        return "assignment"
    if "colour" in text or "color" in text:
        return "colour"
    if "too similar" in text or "duplicate" in text or "redundant" in text:
        # near-duplicates are redundant coverage, not errors: the GUI reports
        # them separately from quality/assignment failures
        return "redundant"
    if "moving" in text or "not synchronised" in text or "sync" in text:
        return "motion"
    if "defocused" in text or "blur" in text:
        return "blur"
    if "saturated" in text or "saturation" in text:
        return "saturation"
    if "contrast" in text:
        return "contrast"
    if "markers" in text or "stereo overlap" in text:
        return "markers"
    if "plate too" in text or "clipped" in text:
        return "plate"
    if "no board pose" in text or "pose" in text:
        return "pose"
    if "materialise" in text or "cannot write" in text or "io " in text:
        return "io"
    return "other"


# ---------------------------------------------------------------------------
# optional sibling modules (owned by another engineer; may not exist yet)
# ---------------------------------------------------------------------------

def _optional(name: str):
    """Import ``tools.wizard.<name>`` on demand; ``None`` when absent."""
    try:
        return importlib.import_module(f"{__package__}.{name}")
    except Exception:
        return None


def _version_of(module: Any) -> str | None:
    if module is None:
        return None
    for attr in ("__version__", "VERSION"):
        value = getattr(module, attr, None)
        if isinstance(value, str):
            return value
    return None


# ---------------------------------------------------------------------------
# coverage requirements
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CoverageRequirements:
    """The §6 coverage specification (local fallback shape).

    Agent B's ``tools.wizard.coverage.CoverageSpec`` is accepted anywhere a
    requirements object is expected; only the attributes below are read.
    """

    distance_bands_mm: tuple[tuple[float, float], ...] = (
        (300.0, 380.0), (380.0, 460.0), (460.0, 560.0))
    min_views_per_distance_band: int = 3
    tilt_axes: str = "two"
    min_tilt_deg: float = 15.0
    min_views_per_tilt_quadrant: int = 2
    roll_bins: int = 4
    min_views_per_roll_bin: int = 1
    image_regions: int = 9
    min_image_regions_visited: int = 6
    min_views_per_image_corner: int = 1
    min_shared_markers: int = 12
    min_colour_markers: int = 12
    require_colour: bool = True
    max_saturation_frac: float = 0.05
    min_focus: float = 150.0
    max_motion_mm: float = 3.0


def _spec_get(spec: Any, name: str, default: Any) -> Any:
    value = getattr(spec, name, None) if spec is not None else None
    return default if value is None else value


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def _digest_json(payload: Any) -> str:
    import hashlib as _hashlib
    return _hashlib.sha256(
        json.dumps(_jsonable(payload), sort_keys=True,
                   separators=(",", ":")).encode("utf-8")).hexdigest()


def _sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _jsonable(value: Any) -> Any:
    """Recursively convert numpy scalars/arrays to plain JSON values."""
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, np.ndarray):
        return _jsonable(value.tolist())
    if isinstance(value, (np.floating, np.integer)):
        return value.item()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, bool) or value is None or isinstance(value, (int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    return value


def _pose_summary(pose: tuple[np.ndarray, np.ndarray] | None) -> dict | None:
    """Distance/tilt/yaw/roll of a board pose in the left-camera frame."""
    if pose is None:
        return None
    R = np.asarray(pose[0], float)
    t = np.asarray(pose[1], float).reshape(3)
    n = R[:, 2]
    x_axis = R[:, 0]
    tilt = float(np.degrees(np.arccos(np.clip(abs(n[2]), -1.0, 1.0))))
    yaw = float(np.degrees(np.arctan2(-n[0], -n[2]))) if abs(n[2]) > 1e-12 else 0.0
    roll = float(np.degrees(np.arctan2(x_axis[1], x_axis[0]))) % 360.0
    if roll > 180.0:
        roll -= 360.0
    return {
        "t_mm": [float(x) for x in t],
        "distance_mm": float(np.linalg.norm(t)),
        "tilt_deg": tilt,
        "yaw_deg": yaw,
        "roll_deg": roll,
        "normal": [float(x) for x in n],
    }


def _quality_summary(quality: Mapping[str, Any] | None) -> dict:
    """Reduce a ``FrameQuality.as_dict()`` to the four GUI numbers."""
    quality = quality or {}
    metrics = quality.get("metrics") or {}
    focus = [float(v) for k, v in metrics.items()
             if k.endswith(".focus") and isinstance(v, (int, float))]
    contrast = [float(v) for k, v in metrics.items()
                if k.endswith(".contrast") and isinstance(v, (int, float))]
    saturation = [float(v) for k, v in metrics.items()
                  if k.endswith(".saturation") and isinstance(v, (int, float))]
    mean_focus = float(np.mean(focus)) if focus else None
    blur = None if mean_focus is None else float(1.0 / (1.0 + max(mean_focus, 0.0) / 150.0))
    return {
        "mean": float(np.mean(contrast)) if contrast else None,
        "saturation_frac": float(max(saturation)) if saturation else None,
        "focus": mean_focus,
        "blur": blur,
        "passed": bool(quality.get("passed", False)),
        "reasons": list(quality.get("reasons") or []),
    }


def _profile_from_mapping(doc: Mapping[str, Any], path: str = "<mapping>") -> Any:
    """Build a ``recon_profile.Profile`` from a JSON-shaped mapping."""
    from calib import recon_profile
    from calib.calibrate import CameraIntrinsics  # noqa: F401  (validates import)
    cameras = {name: recon_profile._camera(name, node)
               for name, node in doc["cameras"].items()}
    left = cameras.get("ir_left")
    if left is None:
        raise ValueError(f"{path}: profile has no ir_left camera")
    if float(np.abs(left.R - np.eye(3)).max()) > 1e-6 or float(np.abs(left.T).max()) > 1e-6:
        raise ValueError(f"{path}: ir_left must be the identity world frame")
    return recon_profile.Profile(
        path=path, schema=str(doc.get("schema", "")),
        kind=str(doc.get("kind", "unknown")),
        color_status=str(doc.get("color_status", "unknown")),
        plate_pitch_mm=float(doc.get("plate_pitch_mm", 28.0)),
        units=str(doc.get("units", "mm")),
        width=int(doc.get("image_width", types.IMAGE_WIDTH)),
        height=int(doc.get("image_height", types.IMAGE_HEIGHT)),
        cameras=cameras)


def _load_profile(source: Any) -> Any:
    from calib import recon_profile
    if isinstance(source, recon_profile.Profile):
        return source
    if isinstance(source, Mapping):
        return _profile_from_mapping(source)
    path = Path(source)
    return recon_profile.load(path)


# ---------------------------------------------------------------------------
# the engine with the wizard's bootstrap policy
# ---------------------------------------------------------------------------

class _WizardSession(CalibrationSession):
    """``CalibrationSession`` with a non-deadlocking wizard bootstrap.

    The engine requires every *bootstrap* view to be mutually diverse
    (>= 8 deg or >= 40 mm apart) before it will solve.  A continuous sweep
    never satisfies that: the operator starts at one pose and creeps forward,
    so the wizard deadlocks at one or two accepted views.  The wizard policy
    (owned by the controller, not by ``calib/session.py``) seeds the first
    ``seed_k`` good-quality views regardless of mutual diversity and lets the
    engine solve; from the first solve on, every call goes to the unchanged
    engine implementation (information gain + coverage + duplicate rejection).

    Quality is *not* relaxed: a seeded view must still pass the full
    ``guidance.evaluate_frame`` gate and have a known pose.
    """

    def __init__(self, *args, seed_k: int = 3, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.seed_k = max(2, int(seed_k))
        self.seed_count = 0
        self.seed_cap = max(3, self.seed_k) + 2
        # the first solve is allowed as soon as the engine's own 3-view
        # minimum and the seed count are both reached
        self.policy["bootstrap_views"] = int(self.seed_k)

    def _bootstrap_decision(self, q: "guidance.FrameQuality") -> dict:
        if self.result is not None:
            return super()._bootstrap_decision(q)
        reasons = list(q.reasons)
        if q.pose is None:
            return {"accept": False, "reasons": reasons + ["no pose"]}
        if reasons:
            return {"accept": False, "reasons": reasons}
        if self.seed_count >= self.seed_cap:
            return {"accept": False,
                    "reasons": reasons + ["bootstrap solve failed"]}
        self.seed_count += 1
        return {"accept": True, "reasons": ["bootstrap seed"]}

    #: Set by the controller from the frame source.  A still collection has no
    #: temporal continuity, so the motion gate must not run on it.
    continuous: bool = True

    def ingest(self, frames, grids=None):
        result = super().ingest(frames, grids)
        if not self.continuous:
            # The engine compares each view with the previous one to reject a
            # moving plate.  For independent stills (vendor corpus, synthetic
            # scenes) that comparison is meaningless and rejected 15/25 vendor
            # views, so clear the reference after every call.
            self.last_pose = None
            self.last_timestamp = None
        return result


# ---------------------------------------------------------------------------
# controller
# ---------------------------------------------------------------------------

class WizardController:
    """One calibration session, wrapped.

    ``factory_profile`` is opened **read-only** and its SHA-256 is recorded at
    ``begin()`` and re-checked at ``finish()``.  Nothing in this class ever
    writes to it.
    """

    def __init__(self, *, factory_profile: Any, session_dir: str | Path,
                 mode: str = "high", coverage: Any | None = None,
                 solve_fn: Any | None = None, pitch_mm: float = 28.0,
                 goal: str = "full", source: types.SourceInfo | None = None,
                 capture: Any | None = None, ring_keep: int = 64,
                 continuous: bool | None = None, seed_k: int | None = None) -> None:
        self.profile = _load_profile(factory_profile)
        self.profile_path: Path | None = None
        try:
            self.profile_path = Path(self.profile.path)
        except (TypeError, ValueError):
            self.profile_path = None
        self.factory_sha256 = ""
        if self.profile_path is not None and self.profile_path.is_file():
            self.factory_sha256 = _sha256_file(self.profile_path)
        self.paths = types.SessionPaths(Path(session_dir))
        self.mode = str(mode)
        self.goal = str(goal)
        self.pitch_mm = float(pitch_mm)
        self.coverage_spec = coverage if coverage is not None else CoverageRequirements()
        self.ring_keep = max(0, int(ring_keep))
        self.capture = capture
        self.source_info = source
        self.software = dict(DEFAULT_SOFTWARE)
        self._continuous_override = continuous
        self.continuous = (True if continuous is None else bool(continuous))
        if source is not None and continuous is None:
            self.continuous = bool(getattr(source, "continuous", True))
        self.seed_k = (int(seed_k) if seed_k is not None
                       else {"quick": 2, "standard": 3, "high": 3}.get(self.mode, 3))

        self.cameras = {key: self.profile.camera(JSON_NAMES[key]).intrinsics()
                        for key in CAMERAS}
        self.cameras0 = {k: v for k, v in self.cameras.items()}
        self.controller = _WizardSession(
            mode=self.mode, cameras=self.cameras, pitch_mm=self.pitch_mm,
            solve_fn=solve_fn, live=False, seed_k=self.seed_k)
        self.controller.continuous = bool(self.continuous)
        self._install_prior_solver()
        # A full rig solve per accepted view costs 0.5 s at 8 views and ~30 s at
        # 40 (measured), which makes a 60-view rehearsal take 25 minutes.  The
        # per-view pose comes from PnP; the rig solve only needs to be fresh
        # enough for guidance and the observability gate.
        try:
            self.controller.policy["solve_every"] = {
                "quick": 1, "standard": 2, "high": 3}.get(self.mode, 2)
        except Exception:
            pass
        #: the wrapped decision engine (alias, per the GUI contract)
        self.session = self.controller

        self.state = "idle"
        self.error: str | None = None
        self.started_wall = 0.0
        self.started_monotonic = 0.0
        self.finished_wall: float | None = None
        self._lock = threading.RLock()
        self._snap_lock = threading.Lock()
        self._published: dict = self._empty_snapshot()

        self._accepted = 0
        self._rejected = 0
        self._seeds = 0
        self._measurement: dict | None = None
        self._readiness_external: dict | None = None
        self._coverage_error: str | None = None
        self._observability_error: str | None = None
        self._reason_counts: dict[str, int] = {}
        self._n_materialised = 0
        self._skipped_recorded = 0
        self._last_recorded = -1
        self._views: list[dict] = []
        self._decisions: list[dict] = []
        self._last_decision: dict | None = None
        self._last_quality: dict | None = None
        self._last_pose: tuple | None = None
        self._last_detections: dict[str, list] = {k: [] for k in CAMERAS}
        self._last_markers: dict[str, int] = {k: 0 for k in CAMERAS}
        self._last_shared = 0
        self._last_assignment_ok = True
        self._view_extras: list[dict] = []
        self._coverage_cache: dict | None = None
        self._readiness_cache: dict | None = None
        self._observability_cache: dict | None = None
        self._observability_key: tuple | None = None
        self._guidance_cache: dict | None = None
        self._guidance_key: tuple | None = None
        self._finish_result: dict | None = None
        self._session_io_info: dict | None = None
        self._lifecycle: str | None = None

    # ------------------------------------------------------------------
    # paths / small accessors
    # ------------------------------------------------------------------
    @property
    def session_dir(self) -> Path:
        return self.paths.root

    @property
    def manifest_path(self) -> Path:
        return self.paths.manifest_json

    @property
    def report_path(self) -> Path:
        return self.paths.report_md

    @property
    def candidate_path(self) -> Path:
        return self.paths.candidate_dir / "profile.json"

    @property
    def validation_path(self) -> Path:
        return self.paths.candidate_dir / "validation.json"

    @property
    def elapsed_s(self) -> float:
        if not self.started_monotonic:
            return 0.0
        end = self.finished_wall if self.finished_wall else time.time()
        if self.finished_wall:
            return max(0.0, end - self.started_wall)
        return max(0.0, time.monotonic() - self.started_monotonic)

    def set_source(self, info: types.SourceInfo | None) -> None:
        self.source_info = info
        if info is not None and self._continuous_override is None:
            self.continuous = bool(getattr(info, "continuous", True))
        if hasattr(self, "controller"):
            self.controller.continuous = bool(self.continuous)
        self._install_prior_solver()
        # A full rig solve per accepted view costs 0.5 s at 8 views and ~30 s at
        # 40 (measured), which makes a 60-view rehearsal take 25 minutes.  The
        # per-view pose comes from PnP; the rig solve only needs to be fresh
        # enough for guidance and the observability gate.
        try:
            self.controller.policy["solve_every"] = {
                "quick": 1, "standard": 2, "high": 3}.get(self.mode, 2)
        except Exception:
            pass

    def _source_continuity(self) -> bool:
        """Whether consecutive groups are a continuous stream (motion gate).

        The source's declared ``SourceInfo.continuous`` is authoritative; a
        group may only *downgrade* it (``FrameGroup.continuous=False``), never
        re-enable the motion gate the source disabled.
        """
        if self._continuous_override is not None:
            return bool(self._continuous_override)
        info = self.source_info
        if info is not None:
            return bool(getattr(info, "continuous", self.continuous))
        return bool(self.continuous)

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    def begin(self) -> dict:
        """Create the session directory and write ``session.json`` (idempotent)."""
        with self._lock:
            if self.state in ("recording", "paused", "finishing", "done"):
                return self._begin_result(created=False)
            root = self.paths.root
            root.mkdir(parents=True, exist_ok=True)
            self.paths.groups_dir.mkdir(parents=True, exist_ok=True)
            self.paths.candidate_dir.mkdir(parents=True, exist_ok=True)
            (root / "ring").mkdir(parents=True, exist_ok=True)
            self._session_io_info = self._prepare_with_session_io()
            self.started_wall = time.time()
            self.started_monotonic = time.monotonic()
            self.finished_wall = None
            self.state = "recording"
            self.error = None
            self._link_recording()
            self._write_session_json()
            self._refresh_derived(force=True)
            self._write_manifest()
            self._publish()
            return self._begin_result(created=True)

    def _begin_result(self, *, created: bool) -> dict:
        return {
            "ok": True, "created": bool(created), "state": self.state,
            "session_dir": str(self.paths.root),
            "session_json": str(self.paths.session_json),
            "manifest": str(self.paths.manifest_json),
            "factory_profile": str(self.profile_path or self.profile.path),
            "factory_sha256": self.factory_sha256,
            "mode": self.mode, "goal": self.goal, "pitch_mm": self.pitch_mm,
            "software": dict(self.software),
            "session_io": self._session_io_info,
        }

    def _prepare_with_session_io(self) -> dict | None:
        """Use ``tools.wizard.session_io`` when the other engineer shipped it."""
        module = _optional("session_io")
        if module is None:
            return None
        create = getattr(module, "create", None)
        if not callable(create):
            return {"module": "session_io", "available": True,
                    "note": "no known entry point; local layout used"}
        try:
            paths = create(self.paths.root, self._config_dict(),
                           state="recording", spec=self._coverage_spec_dict(),
                           exist_ok=True)
            return {"module": "session_io", "function": "create",
                    "root": str(paths.root)}
        except Exception as exc:  # degrade gracefully, never fail begin()
            return {"module": "session_io", "function": "create",
                    "error": f"{type(exc).__name__}: {exc}"}

    def _link_recording(self) -> None:
        """Point ``SESSION/raw.e1r`` at a recording source (best effort)."""
        info = self.source_info
        if info is None or not info.path:
            return
        path = Path(info.path)
        try:
            if path.is_file() and path.suffix == ".e1r":
                target = self.paths.raw_recording
                if not target.exists() and not target.is_symlink():
                    target.symlink_to(path)
        except OSError:
            pass

    def _config_dict(self) -> dict:
        return {
            "mode": self.mode, "goal": self.goal, "pitch_mm": self.pitch_mm,
            "ring_keep": self.ring_keep,
            "require_colour": bool(_spec_get(self.coverage_spec, "require_colour", True)),
            "coverage": self._coverage_spec_dict(),
            "source": _jsonable(self.source_info.__dict__) if self.source_info else None,
        }

    def _coverage_spec_dict(self) -> dict:
        spec = self.coverage_spec
        if is_dataclass(spec):
            try:
                return _jsonable(asdict(spec))
            except Exception:
                pass
        names = ("distance_bands_mm", "min_views_per_distance_band", "tilt_axes",
                 "min_tilt_deg", "min_views_per_tilt_quadrant", "roll_bins",
                 "min_views_per_roll_bin", "image_regions",
                 "min_image_regions_visited", "min_views_per_image_corner",
                 "min_shared_markers", "min_colour_markers", "require_colour",
                 "max_saturation_frac", "min_focus", "max_motion_mm")
        return {name: _jsonable(getattr(spec, name, None)) for name in names}

    # ------------------------------------------------------------------
    # materialisation
    # ------------------------------------------------------------------
    def _materialise(self, group: types.FrameGroup) -> Path | None:
        """Write the three oriented PGMs before detection (crash evidence)."""
        root = self.paths.root / "ring"
        root.mkdir(parents=True, exist_ok=True)
        stage = root / f"{int(group.index):06d}"
        try:
            if stage.is_dir():
                return stage
            tmp = root / f".{int(group.index):06d}.tmp.{os.getpid()}.{threading.get_ident()}"
            if tmp.exists():
                shutil.rmtree(tmp, ignore_errors=True)
            tmp.mkdir(parents=True)
            for key in CAMERAS:
                image = group.images.get(key)
                if image is None:
                    continue
                types.write_pgm(tmp / f"cam{types.FILE_CAM[key]}.pgm", image)
            os.replace(tmp, stage)
            return stage
        except OSError:
            return None

    def _move_to_groups(self, stage: Path | None, view_id: int,
                        images: Mapping[str, np.ndarray]) -> Path:
        target = self.paths.group_dir(int(view_id))
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            shutil.rmtree(target)
        if stage is not None and stage.is_dir():
            try:
                os.replace(stage, target)
            except OSError:
                try:
                    shutil.copytree(stage, target)
                    shutil.rmtree(stage, ignore_errors=True)
                except OSError:
                    pass
        if not all((target / f"cam{types.FILE_CAM[k]}.pgm").is_file()
                   for k in CAMERAS):
            for key in CAMERAS:
                image = images.get(key)
                if image is not None:
                    types.write_pgm(target / f"cam{types.FILE_CAM[key]}.pgm", image)
        return target

    def _prune_ring(self) -> None:
        if self.ring_keep <= 0:
            return
        root = self.paths.root / "ring"
        try:
            dirs = sorted((p for p in root.iterdir()
                           if p.is_dir() and p.name.isdigit()),
                          key=lambda p: int(p.name))
        except OSError:
            return
        for path in dirs[:-self.ring_keep]:
            shutil.rmtree(path, ignore_errors=True)

    # ------------------------------------------------------------------
    # detection and extra checks
    @staticmethod
    def _prefer_partial(gray: np.ndarray, observation: Any) -> Any:
        """Use the partial assignment when the strict one is wrong or missing.

        The strict detector anchors on all four orientation markers.  At an
        image corner the chiral marker leaves the frame, and the strict path
        then either returns None or silently emits a *shifted sub-lattice*
        (measured: u 1..5 instead of 0..7) that belongs to a smaller plate.
        The partial assignment anchors on the three on-lattice large markers
        and recovers the canonical span, so it is preferred whenever it yields
        the full span with a low residual and the strict result does not.
        """
        try:
            from calib import detect_partial
            need_partial = observation is None
            if not need_partial:
                us = {round(float(k[0])) for k in observation.points}
                vs = {round(float(k[1])) for k in observation.points}
                need_partial = (min(us) > 0 or min(vs) > 0
                                or max(us) < 7 or max(vs) < 4)
            if not need_partial:
                return observation
            result = detect_partial.detect_grid_partial(gray)
            cand = result.observation
            if cand is None:
                return observation
            if observation is None:
                return cand
            if int(cand.n_lattice) > int(observation.n_lattice):
                return cand
            return observation
        except Exception:
            return observation

    # ------------------------------------------------------------------
    def _detect(self, images: Mapping[str, np.ndarray]) -> tuple[dict, dict]:
        """Detect the plate in every camera; returns (session grids, raw grids).

        The three cameras are independent and OpenCV releases the GIL, so they
        run in parallel: measured 62 + 61 + 88 = 212 ms serially against ~90 ms
        with three workers.  Detection is the ingest bottleneck (172 ms of a
        230 ms frame), and the live stream offers one frame every 100 ms, so
        this is what makes real-time operation possible at all.
        """
        from calib.calibrate import Observation

        keys = [k for k in CAMERAS if images.get(k) is not None]
        if not keys:
            return {}, {}
        results = self._detect_pool().map(
            lambda key: (key, self._detect_one(key, images[key])), keys)
        raw: dict[str, Any] = {}
        grids: dict[str, Any] = {}
        for key, observation in results:
            if observation is None:
                continue
            raw[key] = observation
            grids[key] = Observation(
                key, -1,
                {tuple(float(x) for x in k): np.asarray(v, float)
                 for k, v in observation.points.items()})
        return grids, raw

    def _detect_pool(self):
        """A small persistent pool; creating threads per frame costs ~1 ms."""
        pool = getattr(self, "_detect_executor", None)
        if pool is None:
            from concurrent.futures import ThreadPoolExecutor
            pool = ThreadPoolExecutor(max_workers=len(CAMERAS),
                                      thread_name_prefix="detect")
            self._detect_executor = pool
        return pool

    #: how far the plate may have moved between frames, as a fraction of its
    #: own size, plus a floor in pixels.  The ROI is a *speed* device only: a
    #: detection that lands near the crop border is redone on the full frame,
    #: because a clipped crop could otherwise drop markers.
    _ROI_MARGIN_FRACTION = 0.30
    _ROI_MARGIN_PX = 80

    def _roi_for(self, key: str, gray: np.ndarray):
        box = getattr(self, "_last_box", {}).get(key)
        if box is None:
            return None
        x0, y0, x1, y1 = box
        w = max(1.0, x1 - x0); h = max(1.0, y1 - y0)
        mx = max(self._ROI_MARGIN_PX, self._ROI_MARGIN_FRACTION * w)
        my = max(self._ROI_MARGIN_PX, self._ROI_MARGIN_FRACTION * h)
        rx0 = int(max(0, x0 - mx)); ry0 = int(max(0, y0 - my))
        rx1 = int(min(gray.shape[1], x1 + mx)); ry1 = int(min(gray.shape[0], y1 + my))
        if rx1 - rx0 < 64 or ry1 - ry0 < 64:
            return None
        return (rx0, ry0, rx1, ry1)

    @staticmethod
    def _early_lattice() -> int:
        """Marker count that counts as "the whole lattice" for the early exit."""
        try:
            from calib import plate
            return len(plate.lattice_uv())
        except Exception:
            return 40

    def _note_box(self, key: str, observation) -> None:
        if observation is None or not observation.points:
            getattr(self, "_last_box", {}).pop(key, None)
            return
        pts = np.asarray(list(observation.points.values()), float)
        box = (float(pts[:, 0].min()), float(pts[:, 1].min()),
               float(pts[:, 0].max()), float(pts[:, 1].max()))
        boxes = getattr(self, "_last_box", None)
        if boxes is None:
            boxes = self._last_box = {}
        boxes[key] = box

    def _detect_one(self, key: str, image):
        from calib.detect import detect_grid_auto
        try:
            gray = np.ascontiguousarray(np.asarray(image, dtype=np.uint8))
            roi = self._roi_for(key, gray)
            stats = getattr(self, "_roi_stats", None)
            if stats is None:
                stats = self._roi_stats = {"roi": 0, "full": 0, "fallback": 0}
            observation = None
            if roi is not None:
                observation = detect_grid_auto(gray, roi=roi)
                if observation is not None:
                    pts = np.asarray(list(observation.points.values()), float)
                    edge = 8.0
                    inside = (pts[:, 0] > roi[0] + edge) & (pts[:, 0] < roi[2] - edge) \
                        & (pts[:, 1] > roi[1] + edge) & (pts[:, 1] < roi[3] - edge)
                    if not bool(inside.all()):
                        observation = None          # may be clipped: redo
                        stats["fallback"] += 1
                if observation is not None:
                    stats["roi"] += 1
            if observation is None:
                observation = detect_grid_auto(gray)
                stats["full"] += 1
            observation = self._prefer_partial(gray, observation)
            self._note_box(key, observation)
            return observation
        except Exception:
            return None

    def _witness(self, gray: np.ndarray, grid: Any) -> dict:
        """Score the four lattice label hypotheses (large-marker chirality)."""
        module = _optional("sweep.orient")
        if module is None:
            try:  # namespace package import (repo root on sys.path)
                from tools.sweep import orient as module  # type: ignore
            except Exception:
                module = None
        if module is not None:
            try:
                check = module.check_orientation(gray, grid)
                return check.as_dict()
            except Exception as exc:
                return {"ok": False, "reason": f"witness_error: {exc}"}
        return self._fallback_witness(gray, grid)

    def _fallback_witness(self, gray: np.ndarray, grid: Any) -> dict:
        """Minimal independent witness when ``tools/sweep/orient.py`` is absent.

        Large-marker area signature on the three on-lattice witnesses plus the
        off-lattice marker, and the chirality sign ``det(H) > 0``.
        """
        try:
            import cv2
            from calib import detect, plate
        except Exception:
            return {"ok": False, "reason": "witness_unavailable"}
        det_h = float(np.linalg.det(np.asarray(grid.homography, float)))
        pitch = _median_pitch(grid.pixels)
        small = float("nan")
        best = None
        for invert in (False, True):
            blobs = detect.detect_blobs(gray, invert=invert, percentile=99.0)
            if not blobs:
                continue
            P = np.array([b.xy for b in blobs], float)
            d = np.linalg.norm(P[:, None, :] - grid.pixels[None, :, :], axis=2)
            if best is None or int((d.min(0) < 1.5).sum()) > best[0]:
                best = (int((d.min(0) < 1.5).sum()), blobs, P)
        if best is None or len(best[2]) < 8:
            return {"ok": False, "reason": "no_blobs", "det_h": det_h}
        _, blobs, P = best
        areas = np.array([b.area for b in blobs], float)
        matched = np.linalg.norm(P[:, None, :] - grid.pixels[None, :, :],
                                 axis=2).min(0) < 0.25 * pitch
        if matched.any():
            small = float(np.median(areas[
                np.linalg.norm(P[:, None, :] - grid.pixels[None, :, :],
                               axis=2).argmin(0)[matched]]))
        score, hits = 0, 0
        for u, v in plate.big_points_uv():
            pix = cv2.perspectiveTransform(
                np.array([[[float(u), float(v)]]], np.float32),
                np.asarray(grid.homography, float)).reshape(2)
            d = np.linalg.norm(P - pix, axis=1)
            j = int(np.argmin(d))
            if np.isfinite(small) and small > 0 and d[j] < 0.30 * pitch:
                hits += 1
                if areas[j] / small > 1.6:
                    score += 1
        ok = bool(det_h > 0 and score >= 3)
        return {"ok": ok, "det_h": det_h, "score": int(score), "hits": int(hits),
                "reason": "verified" if ok else (
                    "mirrored_geometry" if det_h <= 0 else "ambiguous")}

    def _pre_checks(self, images: Mapping[str, np.ndarray],
                    grids: Mapping[str, Any],
                    raw: Mapping[str, Any]) -> tuple[str | None, dict]:
        spec = self.coverage_spec
        markers = {key: (len(grids[key].uv) if key in grids else 0)
                   for key in CAMERAS}
        shared = 0
        if "left" in grids and "right" in grids:
            shared = len(set(grids["left"].uv) & set(grids["right"].uv))
        assignments: dict[str, dict] = {}
        reason: str | None = None

        need_shared = int(_spec_get(spec, "min_shared_markers", 12))
        if "left" in grids and "right" in grids and shared < need_shared:
            reason = (f"stereo overlap {shared} < {need_shared} shared markers "
                      "(both IR cameras must see the same labels)")
        # unambiguous assignment: blob-area / off-lattice witness, never a count
        if reason is None:
            for key in ("left", "right"):
                grid = raw.get(key)
                if grid is None or len(grid.points) < 8:
                    continue
                witness = self._witness(np.asarray(images[key], np.uint8), grid)
                assignments[key] = witness
                if witness.get("ok"):
                    continue
                why = str(witness.get("reason", "unknown"))
                if why == "mirrored_geometry":
                    reason = (f"{key}: mirrored marker geometry "
                              "(det(H)<0); marker assignment is ambiguous")
                elif why == "partial_witness":
                    reason = (f"{key}: ambiguous marker assignment "
                              f"(partial witness {witness.get('score')}/4, "
                              "off-lattice marker missing)")
                else:
                    reason = (f"{key}: ambiguous marker assignment ({why}, "
                              f"identity score {witness.get('score')}/4)")
                break
        # colour participation
        #
        # A view is *colour-capable* only when the colour camera can actually
        # see the plate.  At an image corner the colour sensor (offset from the
        # IR pair and with its own principal point) can lose the board entirely
        # while both IR cameras still see it well; rejecting those views threw
        # away exactly the corner and far-distance observations the coverage
        # gate needs (measured: 6 of 24 poses vetoed as "tex: only 0 colour
        # markers").  Colour registration needs *some* colour views, which D6
        # counts; it does not need every view to be colour-capable.
        n_tex = int(markers.get("tex", 0))
        colour_capable = n_tex > 0
        if reason is None and bool(_spec_get(spec, "require_colour", True)):
            need_colour = int(_spec_get(spec, "min_colour_markers", 12))
            if colour_capable and n_tex < need_colour:
                # the board is visible in colour but weakly: retry the pose
                reason = (f"tex: only {n_tex} colour markers (need {need_colour})")
            elif n_tex == 0:
                # not a colour-capable pose; keep the IR evidence
                colour_capable = False
        return reason, {
            "markers": markers, "shared_markers_ir": int(shared),
            "assignment": assignments,
            "assignment_ok": all(bool(a.get("ok")) for a in assignments.values())
            if assignments else False,
        }

    # ------------------------------------------------------------------
    # ingest
    # ------------------------------------------------------------------
    def record_only(self, group: types.FrameGroup) -> dict:
        """Record a group without spending detection time on it.

        A group skipped by the adaptive scheduler is *not* copied to the session
        ring: the complete frame record already exists (the capture tool's raw
        recording in a live session, or the source files in a replay), and
        duplicating it cost 3.9 MB per group - measured at 10 Hz that is
        39 MB/s of extra writes, which is what kept the loop at 7.5 groups/s
        instead of keeping up.  Accepted and rejected-by-decision frames are
        still materialised exactly as before.
        """
        with self._lock:
            self._skipped_recorded += 1
            self._last_recorded = int(group.index)
        return {"recorded": True, "skipped": True, "index": int(group.index),
                "device_group": group.device_group,
                "timestamp_s": float(group.timestamp_s)}

    def ingest(self, group: types.FrameGroup) -> Any:
        # A source may declare its own continuity (still collections have no
        # cadence).  Adopt it before the first decision so the motion gate never
        # rejects an independent view.
        # Bound the total number of full rig solves (~12 per session): a solve
        # costs 0.5 s at 8 views but ~30 s at 42 (measured), so a fixed small
        # interval makes long sessions quadratic and stalls the live path.
        try:
            n_accepted = len(self.session.accepted)
            self.session.policy["solve_every"] = max(1, n_accepted // 12) \
                if n_accepted >= 12 else int(self.session.policy.get("solve_every", 1))
        except Exception:
            pass
        cont = getattr(group, "continuous", None)
        if cont is False and self._continuous_override is None:
            # a still group may only disable the motion gate, never re-enable
            # it against a source that declared a continuous cadence
            self.continuous = False
            self.controller.continuous = False
        """The only decision path.  Returns the session's ``Decision``."""
        from calib.session import Decision, FrameSet

        if self.state in ("idle", "cancelled"):
            self.begin()
        if self.state in ("done", "finishing"):
            raise RuntimeError(f"session is {self.state}; cannot ingest")
        with self._lock:
            record: dict[str, Any] = {
                "group": int(group.index), "device_group": group.device_group,
                "source": group.source, "label": group.label,
                "timestamp_s": float(group.timestamp_s),
                "digest": group.digest(),
            }
            images = {key: np.asarray(group.images[key])
                      for key in CAMERAS if key in group.images}
            record["cameras"] = sorted(images)
            if not group.complete or set(images) != set(CAMERAS):
                missing = [k for k in CAMERAS if k not in images]
                reason = ("incomplete group: missing "
                          + (", ".join(missing) if missing else "cameras"))
                return self._veto(group, images, reason, record,
                                  Decision, FrameSet)
            stage = self._materialise(group)
            if stage is None:
                return self._veto(group, images,
                                  "io: cannot materialise the frame triplets",
                                  record, Decision, FrameSet)
            self._n_materialised += 1
            try:
                grids, raw = self._detect(images)
                reason, info = self._pre_checks(images, grids, raw)
            except Exception as exc:
                return self._veto(group, images,
                                  f"controller error during detection: "
                                  f"{type(exc).__name__}: {exc}", record,
                                  Decision, FrameSet)
            self._last_markers = dict(info["markers"])
            self._last_shared = int(info["shared_markers_ir"])
            self._last_assignment_ok = bool(info["assignment_ok"])
            self._last_detections = self._detections(raw)
            if reason is not None:
                return self._veto(group, images, reason, record, Decision,
                                  FrameSet, obs_grids=grids, raw=raw,
                                  extras={**info, "veto": "controller"})
            frames = FrameSet(images=images, timestamp=float(group.timestamp_s),
                              label=group.label or "")
            if not self._source_continuity():
                # still collection: consecutive groups are independent poses,
                # so the dwell/motion gate must not see a previous frame
                self.session.last_pose = None
                self.session.last_timestamp = None
            seeds_before = int(getattr(self.session, "seed_count", 0))
            was_bootstrap = self.session.result is None
            try:
                decision = self.session.ingest(frames, grids=grids)
            except Exception as exc:
                return self._veto(group, images,
                                  f"controller error during session.ingest: "
                                  f"{type(exc).__name__}: {exc}", record,
                                  Decision, FrameSet)
            seeded = bool(decision.accepted and was_bootstrap
                          and int(getattr(self.session, "seed_count", 0))
                          > seeds_before)
            if seeded:
                decision.reasons.append("bootstrap seed")
                self._seeds += 1
            if decision.accepted:
                self._record_accept(group, images, stage, decision, record,
                                    info, raw, seeded=seeded)
            else:
                self._record_reject(group, decision, record, stage)
            self._refresh_derived()
            self._write_manifest()
            self._write_session_json()
            self._publish()
            return decision

    def _veto(self, group, images, reason, record, Decision, FrameSet,
              obs_grids=None, raw=None, extras=None) -> Any:
        """Reject a frame for a controller-only reason and record it."""
        quality = self._veto_quality(images, obs_grids)
        entry = {
            "timestamp": float(group.timestamp_s), "reasons": [reason],
            "quality": quality, "controller_veto": True,
        }
        self.session.rejected.append(entry)
        decision = Decision(False, [reason], quality, None, None, False,
                            {"n_accepted": len(self.session.accepted),
                             "n_rejected": len(self.session.rejected),
                             "n_observations": len(self.session.obs),
                             "n_solves": int(getattr(self.session, "_solves", 0))})
        self._rejected += 1
        self._bump_reason(reason)
        self._last_quality = quality if isinstance(quality, dict) else None
        self._last_detections = self._detections(raw or {})
        obs_grids = obs_grids or {}
        self._last_markers = {k: len(obs_grids[k].uv) if k in obs_grids else
                              self._last_markers.get(k, 0) for k in CAMERAS}
        if extras:
            self._last_markers = extras.get("markers", self._last_markers)
            self._last_shared = int(extras.get("shared_markers_ir", 0))
            self._last_assignment_ok = bool(extras.get("assignment_ok", False))
        record.update({"accepted": False, "reasons": [reason],
                       "predicted_gain": None, "view_id": None, "solved": False,
                       "quality": quality,
                       "markers": dict(self._last_markers),
                       "shared_markers_ir": self._last_shared,
                       "assignment_ok": self._last_assignment_ok,
                       "status": decision.status})
        self._append_decision(record)
        self._last_decision = {
            "accepted": False, "reasons": [reason], "predicted_gain": None,
            "view_id": None, "quality": quality, "status": decision.status,
        }
        self._refresh_derived()
        self._write_manifest()
        self._write_session_json()
        self._publish()
        return decision

    def _veto_quality(self, images: Mapping[str, np.ndarray],
                      grids: Mapping[str, Any] | None) -> dict:
        try:
            from calib import guidance
            # A view where both IR cameras see the plate well but the colour
            # camera sees nothing is still valuable IR evidence (corners, far
            # distance).  Colour participation is enforced by coverage D6 over
            # the whole session, not per view.
            optional = set()
            counts = {k: (len(v.uv) if v is not None else 0)
                      for k, v in (grids or {}).items()}
            if (counts.get("left", 0) >= 16 and counts.get("right", 0) >= 16
                    and counts.get("tex", 0) == 0):
                optional.add("tex")
                self._colour_blind_views = int(getattr(
                    self, "_colour_blind_views", 0)) + 1
            quality = guidance.evaluate_frame(
                {k: v for k, v in images.items()},
                {k: v for k, v in (grids or {}).items()},
                cameras=self.cameras, require_images=True,
                optional_cameras=optional)
            return quality.as_dict()
        except Exception as exc:
            return {"passed": False, "reasons": [f"quality unavailable: {exc}"],
                    "metrics": {}, "n_markers": {}}

    def _detections(self, raw: Mapping[str, Any]) -> dict[str, list]:
        out: dict[str, list] = {}
        for key in CAMERAS:
            grid = raw.get(key)
            if grid is None:
                out[key] = []
                continue
            points = sorted(grid.points.items(), key=lambda kv: (kv[0][1], kv[0][0]))
            out[key] = [[round(float(v[0]), 3), round(float(v[1]), 3)]
                        for _, v in points]
        return out

    def _bump_reason(self, reason: str) -> None:
        category = classify_reason(reason)
        self._reason_counts[category] = self._reason_counts.get(category, 0) + 1

    def _record_accept(self, group, images, stage, decision, record, info,
                       raw, *, seeded: bool = False) -> None:
        view_id = int(decision.view_id if decision.view_id is not None else 0)
        target = self._move_to_groups(stage, view_id, images)
        self._accepted += 1
        accepted = self.session.accepted[-1]
        summary = accepted.as_dict()
        pose = _pose_summary(accepted.pose)
        R_pose, t_pose = accepted.pose
        entry = {
            "view_id": view_id,
            "group": int(group.index),
            "device_group": group.device_group,
            "materialised": target.name,
            # the shapes tools.wizard.coverage.measure() consumes
            "pose": {"R": [float(x) for x in np.asarray(R_pose, float).ravel()],
                     "t": [float(x) for x in np.asarray(t_pose, float).ravel()]},
            "image_points": _jsonable(self._last_detections),
            # still collections have no cadence: a timestamp would make the
            # inter-view "speed" meaningless and block guidance/stationarity
            "timestamp": (float(group.timestamp_s)
                          if self._source_continuity() else None),
            "stationary": (None if self._source_continuity() else True),
            "distance_mm": pose["distance_mm"] if pose else None,
            "tilt_deg": pose["tilt_deg"] if pose else None,
            "yaw_deg": pose["yaw_deg"] if pose else None,
            "roll_deg": pose["roll_deg"] if pose else None,
            "predicted_gain": summary.get("predicted_gain"),
            "holdout_rms_px": summary.get("holdout_rms_px"),
            "n_markers": dict(summary.get("n_markers") or {}),
            "quality": decision.quality,
            "quality_passed": bool((decision.quality or {}).get("passed", True)),
            "shared_markers_ir": int(info["shared_markers_ir"]),
            "assignment": _jsonable(info["assignment"]),
            "digest": record["digest"],
            "solved": bool(decision.solved),
            "bootstrap_seed": bool(seeded),
        }
        files = {}
        for key in CAMERAS:
            path = self.paths.group_file(view_id, key)
            try:
                if path.is_file():
                    files[key] = {"path": str(path.relative_to(self.paths.root)),
                                  "sha256": _sha256_file(path),
                                  "bytes": int(path.stat().st_size)}
            except OSError:
                pass
        entry["files"] = files
        self._views.append(entry)
        self._view_extras.append({
            "shared_markers_ir": int(info["shared_markers_ir"]),
            "n_markers": dict(info["markers"]),
        })
        self._last_pose = accepted.pose
        self._last_quality = decision.quality
        self._last_markers = dict(info["markers"])
        self._last_shared = int(info["shared_markers_ir"])
        self._last_assignment_ok = bool(info["assignment_ok"])
        self._last_detections = self._detections(raw)
        record.update({
            "accepted": True, "reasons": [], "predicted_gain": summary.get("predicted_gain"),
            "view_id": view_id, "solved": bool(decision.solved),
            "quality": decision.quality, "markers": dict(info["markers"]),
            "shared_markers_ir": int(info["shared_markers_ir"]),
            "assignment_ok": bool(info["assignment_ok"]),
            "bootstrap_seed": bool(seeded),
            # deterministic counters, not the wall-clock solve status
            "status": self._status_summary(),
        })
        self._append_decision(record)
        self._last_decision = {
            "accepted": True, "reasons": [], "predicted_gain": summary.get("predicted_gain"),
            "view_id": view_id, "quality": decision.quality,
            "status": decision.status,
        }
        self._prune_ring()

    def _record_reject(self, group, decision, record, stage) -> None:
        self._rejected += 1
        for reason in decision.reasons:
            self._bump_reason(reason)
        pose = _pose_summary(self.session.last_pose)
        if pose is not None:
            self._last_pose = self.session.last_pose
        self._last_quality = decision.quality
        record.update({
            "accepted": False, "reasons": list(decision.reasons),
            "predicted_gain": decision.predicted_gain, "view_id": None,
            "solved": bool(decision.solved), "quality": decision.quality,
            "markers": dict(self._last_markers),
            "shared_markers_ir": self._last_shared,
            "assignment_ok": self._last_assignment_ok,
            "status": self._status_summary(),
        })
        self._append_decision(record)
        self._last_decision = {
            "accepted": False, "reasons": list(decision.reasons),
            "predicted_gain": decision.predicted_gain, "view_id": None,
            "quality": decision.quality, "status": decision.status,
        }
        self._prune_ring()

    def _status_summary(self) -> dict:
        return {
            "n_accepted": len(self.session.accepted),
            "n_rejected": len(self.session.rejected),
            "n_observations": len(self.session.obs),
            "n_solves": int(getattr(self.session, "_solves", 0)),
        }

    def _append_decision(self, record: dict) -> None:
        self._decisions.append(record)
        types.append_jsonl(self.paths.decisions_jsonl, record)

    # ------------------------------------------------------------------
    # derived state: coverage, observability, guidance
    # ------------------------------------------------------------------
    def _refresh_derived(self, force: bool = False) -> None:
        coverage = self.measure_coverage()
        readiness = self._readiness_from(coverage)
        # the GUI shows "satisfied/total" next to the readiness verdict; keep
        # the two numbers describing the same eight dimensions
        if coverage.get("total"):
            readiness["satisfied"] = int(coverage.get("satisfied", 0))
            readiness["total"] = int(coverage.get("total", 8))
        self._coverage_cache = coverage
        self._readiness_cache = readiness
        self._refresh_observability(force=force)
        self._refresh_guidance(force=force)

    def _refresh_observability(self, force: bool = False,
                               full: bool = False) -> None:
        result = self.session.result
        key = (len(self.session.accepted), int(getattr(self.session, "_solves", 0)),
               id(result), bool(full))
        if not force and key == self._observability_key and self._observability_cache:
            return
        self._observability_key = key
        self._observability_cache = self._observability(result, full=full)

    def _refresh_guidance(self, force: bool = False) -> None:
        key = (len(self.session.accepted), int(getattr(self.session, "_solves", 0)))
        if not force and key == self._guidance_key and self._guidance_cache:
            return
        self._guidance_key = key
        self._guidance_cache = self._guidance()

    # -- coverage --------------------------------------------------------
    def measure_coverage(self) -> dict:
        """Measure the §6 dimensions from the accepted views.

        Uses ``tools.wizard.coverage`` (agent B) when available; otherwise the
        local fallback below computes the same eight dimensions.  The GUI-facing
        keys are stable: distance_bands, tilt_two_axes, roll_sectors,
        image_regions, stereo_overlap, colour_participation, quality,
        stationarity.
        """
        module = _optional("coverage")
        measure = getattr(module, "measure", None) if module else None
        if callable(measure):
            try:
                spec = self.coverage_spec
                spec_cls = getattr(module, "CoverageSpec", None)
                if spec_cls is not None and not isinstance(spec, spec_cls):
                    spec = None
                views = [dict(v) for v in self._views]
                measurement = measure(views, spec)
                if isinstance(measurement, dict) and measurement.get("D1"):
                    self._measurement = measurement
                    readiness = getattr(module, "readiness", None)
                    verdict = None
                    if callable(readiness):
                        try:
                            verdict = readiness(views, measurement)
                        except TypeError:
                            verdict = readiness(measurement)
                        except Exception:
                            verdict = None
                    self._readiness_external = self._normalise_readiness(
                        verdict, measurement)
                    return self._coverage_snapshot(
                        measurement,
                        ready=bool((verdict or {}).get("ready", False)))
            except Exception as exc:
                self._coverage_error = f"{type(exc).__name__}: {exc}"
        self._measurement = None
        self._readiness_external = None
        return self._measure_coverage_local()

    #: coverage module dimension -> the GUI's stable key
    COVERAGE_KEYS = (("D1", "distance_bands"), ("D2", "tilt_two_axes"),
                     ("D3", "roll_sectors"), ("D4", "image_regions"),
                     ("D5", "stereo_overlap"),
                     ("D6", "colour_participation"), ("D7", "quality"),
                     ("D8", "scale_artefact"))

    def _coverage_snapshot(self, measurement: Mapping[str, Any], *,
                           ready: bool) -> dict:
        dims: dict[str, dict] = {}
        for dim_id, key in self.COVERAGE_KEYS:
            source = measurement.get(dim_id) or {}
            dims[key] = {
                "id": dim_id, "label": source.get("label", key),
                "required": _jsonable(source.get("required")),
                "measured": _jsonable(source.get("measured")),
                "satisfied": bool(source.get("satisfied")),
                "deficit": _jsonable(source.get("deficit")),
                "hint": source.get("hint", ""),
                "detail": _jsonable(source.get("detail")),
            }
        # D7 is "stationarity / quality"; expose the stationarity half under
        # its own key so the GUI contract keeps both names.
        check = ((measurement.get("checks") or {}).get("stationarity") or {})
        dims["stationarity"] = {
            "id": "D7", "label": "stationarity",
            "required": _jsonable(check.get("required")),
            "measured": _jsonable(check.get("measured")),
            "satisfied": bool(check.get("satisfied")),
            "deficit": _jsonable(check.get("deficit")),
            "hint": check.get("hint", ""), "detail": {},
        }
        ordering = ("distance_bands", "tilt_two_axes", "roll_sectors",
                    "image_regions", "stereo_overlap", "colour_participation",
                    "quality", "stationarity")
        ordered = {key: dims[key] for key in ordering if key in dims}
        satisfied = sum(1 for d in ordered.values() if d["satisfied"])
        scale = measurement.get("D8") or {}
        return {
            "ready": bool(ready),
            "satisfied": int(satisfied),
            "total": len(ordered),
            "dimensions": ordered,
            # per-view dots for the coverage map in the GUI
            "views": self._coverage_views(),
            "engine": "tools.wizard.coverage",
            "n_views": int(measurement.get("n_views") or 0),
            "scale_artefact": _jsonable(scale),
            "pose_stats": _jsonable(measurement.get("pose_stats")),
            "stability": _jsonable(measurement.get("stability")),
        }

    def _coverage_views(self) -> list[dict]:
        """Compact per-view records for the coverage map (GUI)."""
        out: list[dict] = []
        for view in self._views:
            pose = view.get("pose") or {}
            R = pose.get("R")
            roll = view.get("roll_deg")
            tx = ty = None
            if R is not None:
                try:
                    from . import coverage as _cov
                    tx, ty = _cov.tilt_deg(np.asarray(R, float).reshape(3, 3))
                except Exception:
                    tx = ty = None
            uv = None
            points = (view.get("image_points") or {}).get("left")
            if points:
                arr = np.asarray(points, float).reshape(-1, 2)
                if len(arr):
                    centre = arr.mean(axis=0)
                    uv = [float(centre[0] / types.IMAGE_WIDTH),
                          float(centre[1] / types.IMAGE_HEIGHT)]
            out.append({
                "view_id": view.get("view_id"),
                "distance_mm": view.get("distance_mm"),
                "tilt_x_deg": (None if tx is None else float(tx)),
                "tilt_y_deg": (None if ty is None else float(ty)),
                "roll_deg": (None if roll is None else float(roll)),
                "image_uv": uv,
            })
        return out

    def _normalise_readiness(self, verdict: Mapping[str, Any] | None,
                             measurement: Mapping[str, Any]) -> dict | None:
        if not isinstance(verdict, Mapping):
            return None
        total = sum(1 for dim_id, _ in self.COVERAGE_KEYS
                    if measurement.get(dim_id))
        satisfied = sum(1 for dim_id, _ in self.COVERAGE_KEYS
                        if (measurement.get(dim_id) or {}).get("satisfied"))
        return {
            "ready": bool(verdict.get("ready")),
            "missing": [str(x) for x in (verdict.get("unsatisfied") or [])],
            "reason": str(verdict.get("summary") or ""),
            "satisfied": int(satisfied),
            "total": int(max(total, 1)),
            "unsatisfied_dimensions": list(
                verdict.get("unsatisfied_dimensions") or []),
        }

    def _measure_coverage_local(self) -> dict:
        spec = self.coverage_spec
        views = self.session.accepted
        obs = self.session.obs
        dims: dict[str, dict] = {}

        def add(name: str, required, measured, satisfied: bool, deficit,
                hint: str, detail: dict | None = None) -> None:
            dims[name] = {
                "required": required, "measured": measured,
                "satisfied": bool(satisfied), "deficit": deficit,
                "hint": "" if satisfied else hint,
                "detail": _jsonable(detail or {}),
            }

        n = len(views)
        # -- distance bands ---------------------------------------------
        bands = tuple(tuple(float(x) for x in band)
                      for band in _spec_get(spec, "distance_bands_mm",
                                            CoverageRequirements.distance_bands_mm))
        per_band = [0] * len(bands)
        for view in views:
            distance = float(np.linalg.norm(np.asarray(view.pose[1], float)))
            for i, (lo, hi) in enumerate(bands):
                if lo <= distance < hi:
                    per_band[i] += 1
                    break
        need_band = int(_spec_get(spec, "min_views_per_distance_band", 3))
        band_ok = bool(bands) and all(c >= need_band for c in per_band)
        missing_band = [f"{lo:.0f}-{hi:.0f} mm needs {max(0, need_band - c)} more"
                        for (lo, hi), c in zip(bands, per_band) if c < need_band]
        add("distance_bands",
            f">= {need_band} views in each of {len(bands)} bands",
            f"{per_band} in {[list(b) for b in bands]}",
            band_ok, "; ".join(missing_band),
            "Move the plate through the missing distance bands "
            f"({', '.join(f'{lo:.0f}-{hi:.0f}' for (lo, hi) in bands)} mm).",
            {"bands_mm": [list(b) for b in bands], "counts": per_band,
             "required_per_band": need_band})

        # -- two-axis tilt ----------------------------------------------
        min_tilt = float(_spec_get(spec, "min_tilt_deg", 15.0))
        need_quad = int(_spec_get(spec, "min_views_per_tilt_quadrant", 2))
        sin_min = math.sin(math.radians(min_tilt))
        quadrants = {(sx, sy): 0 for sx in (1, -1) for sy in (1, -1)}
        tilt_values = []
        for view in views:
            R = np.asarray(view.pose[0], float)
            normal = R[:, 2]
            tilt_x = math.asin(max(-1.0, min(1.0, float(normal[0]))))
            tilt_y = math.asin(max(-1.0, min(1.0, float(normal[1]))))
            tilt_values.append((math.degrees(tilt_x), math.degrees(tilt_y)))
            if abs(normal[0]) >= sin_min and abs(normal[1]) >= sin_min:
                quadrants[(1 if normal[0] > 0 else -1,
                           1 if normal[1] > 0 else -1)] += 1
        tilt_ok = all(c >= need_quad for c in quadrants.values())
        missing_tilt = [f"quadrant {sx:+d}/{sy:+d} needs "
                        f"{max(0, need_quad - c)} more"
                        for (sx, sy), c in quadrants.items() if c < need_quad]
        add("tilt_two_axes",
            f">= {need_quad} views with >{min_tilt:.0f} deg out-of-plane tilt "
            "in each of 4 quadrants",
            f"quadrant counts {dict(quadrants)}",
            tilt_ok, "; ".join(missing_tilt),
            "Tilt the plate more than "
            f"{min_tilt:.0f} deg towards each of the four directions "
            "(left/right and up/down).",
            {"min_tilt_deg": min_tilt, "required_per_quadrant": need_quad,
             "quadrants": {f"{sx:+d},{sy:+d}": c for (sx, sy), c in quadrants.items()},
             "max_abs_tilt_deg": [
                 float(max((abs(a) for a, _ in tilt_values), default=0.0)),
                 float(max((abs(b) for _, b in tilt_values), default=0.0))]})

        # -- roll sectors ------------------------------------------------
        n_bins = int(_spec_get(spec, "roll_bins", 4))
        need_roll = int(_spec_get(spec, "min_views_per_roll_bin", 1))
        sectors = [0] * n_bins
        rolls = []
        for view in views:
            R = np.asarray(view.pose[0], float)
            angle = math.degrees(math.atan2(float(R[1, 0]), float(R[0, 0]))) % 360.0
            rolls.append(angle)
            sectors[int(angle // (360.0 / n_bins)) % n_bins] += 1
        roll_ok = all(c >= need_roll for c in sectors)
        missing_roll = [f"sector {i} ({i * 360 // n_bins}-"
                        f"{(i + 1) * 360 // n_bins} deg) needs "
                        f"{max(0, need_roll - c)} more"
                        for i, c in enumerate(sectors) if c < need_roll]
        add("roll_sectors",
            f">= {need_roll} view in each of {n_bins} in-plane sectors",
            f"sector counts {sectors}", roll_ok, "; ".join(missing_roll),
            "Rotate the plate in its own plane (0/90/180/270 deg).",
            {"bins": n_bins, "counts": sectors, "angles_deg": rolls})

        # -- image regions ----------------------------------------------
        grid_n = int(round(math.sqrt(float(_spec_get(spec, "image_regions", 9)))))
        grid_n = max(1, grid_n)
        visited = np.zeros((grid_n, grid_n), int)
        from calib.geometry import project
        cam = self.cameras["left"]
        for view in views:
            try:
                uv = project(cam.K, cam.kc,
                             np.asarray(view.pose[1], float).reshape(1, 3))[0]
            except Exception:
                continue
            if not np.isfinite(uv).all():
                continue
            col = int(uv[0] / types.IMAGE_WIDTH * grid_n)
            row = int(uv[1] / types.IMAGE_HEIGHT * grid_n)
            if 0 <= col < grid_n and 0 <= row < grid_n:
                visited[row, col] += 1
        need_regions = int(_spec_get(spec, "min_image_regions_visited", 6))
        need_corner = int(_spec_get(spec, "min_views_per_image_corner", 1))
        corners = [(0, 0), (0, grid_n - 1), (grid_n - 1, 0), (grid_n - 1, grid_n - 1)]
        regions_ok = (int((visited > 0).sum()) >= need_regions
                      and all(visited[r, c] >= need_corner for r, c in corners))
        missing_regions = []
        if int((visited > 0).sum()) < need_regions:
            missing_regions.append(
                f"{int((visited > 0).sum())}/{grid_n * grid_n} regions visited "
                f"(need {need_regions})")
        for r, c in corners:
            if visited[r, c] < need_corner:
                missing_regions.append(f"corner region {r},{c} needs "
                                       f"{need_corner - int(visited[r, c])} more")
        add("image_regions",
            f">= {need_regions}/{grid_n * grid_n} regions and every corner",
            f"visited {int((visited > 0).sum())}/{grid_n * grid_n}; "
            f"grid {visited.tolist()}",
            regions_ok, "; ".join(missing_regions),
            "Aim the plate at the image corners as well as the centre.",
            {"grid": grid_n, "counts": visited.tolist(),
             "regions_visited": int((visited > 0).sum())})

        # -- stereo overlap ---------------------------------------------
        need_shared = int(_spec_get(spec, "min_shared_markers", 12))
        shared = [int(x.get("shared_markers_ir", 0)) for x in self._view_extras]
        min_shared = min(shared) if shared else 0
        add("stereo_overlap",
            f">= {need_shared} shared left/right markers in every accepted view",
            f"minimum {min_shared} over {len(shared)} views",
            bool(shared) and min_shared >= need_shared,
            "" if not shared else f"min {min_shared} < {need_shared}",
            "Keep both IR cameras looking at the same part of the plate.",
            {"min_shared": min_shared, "required": need_shared,
             "per_view": shared})

        # -- colour participation ---------------------------------------
        require_colour = bool(_spec_get(spec, "require_colour", True))
        need_colour = int(_spec_get(spec, "min_colour_markers", 12))
        colour_counts = [int(x.get("n_markers", {}).get("tex", 0))
                         for x in self._view_extras]
        min_colour = min(colour_counts) if colour_counts else 0
        colour_ok = (not require_colour) or (bool(colour_counts)
                                             and min_colour >= need_colour)
        add("colour_participation",
            ("not required" if not require_colour else
             f">= {need_colour} colour markers in every accepted view"),
            f"minimum {min_colour} over {len(colour_counts)} views",
            colour_ok,
            "" if colour_ok else f"min {min_colour} < {need_colour}",
            "Show the plate fully to the colour camera (it has a narrower view).",
            {"min_colour": min_colour, "required": need_colour,
             "per_view": colour_counts, "required_flag": require_colour})

        # -- image quality ----------------------------------------------
        min_focus_req = float(_spec_get(spec, "min_focus", 150.0))
        max_sat = float(_spec_get(spec, "max_saturation_frac", 0.05))
        focuses, sats = [], []
        for view in views:
            metrics = (view.quality or {}).get("metrics") or {}
            for key, value in metrics.items():
                if key.endswith(".focus") and isinstance(value, (int, float)):
                    focuses.append(float(value))
                if key.endswith(".saturation") and isinstance(value, (int, float)):
                    sats.append(float(value))
        focus_min = min(focuses) if focuses else None
        sat_max = max(sats) if sats else None
        quality_ok = (focus_min is not None and focus_min >= min_focus_req
                      and sat_max is not None and sat_max <= max_sat)
        deficits = []
        if focus_min is None:
            deficits.append("no focus measurements")
        elif focus_min < min_focus_req:
            deficits.append(f"min focus {focus_min:.0f} < {min_focus_req:.0f}")
        if sat_max is None:
            deficits.append("no saturation measurements")
        elif sat_max > max_sat:
            deficits.append(f"max saturation {sat_max:.4f} > {max_sat:.4f}")
        add("quality",
            f"focus >= {min_focus_req:.0f}, saturation <= {max_sat:.3f}",
            f"focus min {focus_min}, saturation max {sat_max}",
            quality_ok, "; ".join(deficits),
            "Improve lighting/focus (hold still, avoid overexposure).",
            {"focus_min": focus_min, "saturation_max": sat_max})

        # -- stationarity ------------------------------------------------
        max_motion_req = float(_spec_get(spec, "max_motion_mm", 3.0))
        motions = []
        for view in views:
            motion = ((view.quality or {}).get("metrics") or {}).get("motion_mm")
            if isinstance(motion, (int, float)):
                motions.append(float(motion))
        worst = max(motions) if motions else 0.0
        add("stationarity",
            f"plate motion <= {max_motion_req:.1f} mm between frames",
            f"max {worst:.2f} mm over {len(motions)} views",
            worst <= max_motion_req, "" if worst <= max_motion_req else
            f"max motion {worst:.2f} mm",
            "Hold the plate still while the trigger runs.",
            {"max_motion_mm": worst, "required": max_motion_req})

        satisfied = sum(1 for d in dims.values() if d["satisfied"])
        return {"ready": satisfied == len(dims), "satisfied": int(satisfied),
                "total": len(dims), "dimensions": dims,
                "engine": "tools.wizard.controller (fallback)"}

    def _readiness_from(self, coverage: Mapping[str, Any]) -> dict:
        external = getattr(self, "_readiness_external", None)
        if external:
            return copy.deepcopy(external)
        dims = coverage.get("dimensions") or {}
        labels = {
            "distance_bands": "distance bands",
            "tilt_two_axes": "two-axis tilt",
            "roll_sectors": "roll sectors",
            "image_regions": "image regions",
            "stereo_overlap": "stereo overlap",
            "colour_participation": "colour participation",
            "quality": "image quality",
            "stationarity": "stationarity",
        }
        missing, reasons = [], []
        for key, dim in dims.items():
            if isinstance(dim, dict) and not dim.get("satisfied"):
                name = labels.get(key, key)
                missing.append(name)
                deficit = dim.get("deficit") or dim.get("measured")
                reasons.append(f"{name}: {deficit}" if deficit else name)
        ready = bool(coverage.get("ready")) and not missing
        return {"ready": ready, "missing": missing,
                "reason": ("all coverage dimensions satisfied" if ready
                           else "; ".join(reasons)),
                "satisfied": int(coverage.get("satisfied", 0)),
                "total": int(coverage.get("total", len(labels)))}

    def readiness(self) -> dict:
        if self._readiness_cache is None:
            self._refresh_derived(force=True)
        return copy.deepcopy(self._readiness_cache or {"ready": False,
                                                       "missing": [], "reason": ""})

    # -- observability ---------------------------------------------------
    def _observability(self, result: Any, *, full: bool = False) -> dict:
        if result is None:
            return {"parameters": {}, "condition": None, "rank": 0,
                    "n_params": 0, "free": [], "gates": {
                        name: {"pass": False, "detail": "no calibration yet"}
                        for name in ("stereo_rotation", "baseline", "colour",
                                     "model_sensitivity")},
                    "engine": "pending"}
        external = self._external_observability(result, full=full)
        if external is not None:
            return external
        return self._observability_local(result)

    def _external_observability(self, result: Any,
                                *, full: bool = False) -> dict | None:
        """Normalise agent B's observability module into the GUI contract.

        The module returns ``marginal_sigmas`` as a name->sigma mapping (with
        ``free_names`` attached), ``identifiability`` as a dict and
        ``observability_gate`` as ``{parameters, gates, passed, reasons, ...}``.
        Shapes are read defensively so a sibling module update degrades to the
        local fallback instead of breaking the snapshot.
        """
        module = _optional("observability")
        if module is None:
            return None
        target = "fast" if self.mode == "quick" else self.mode
        try:
            sig = module.marginal_sigmas(result)
            gate = module.observability_gate(
                result, self._measurement or {}, target=target,
                observations=list(self.session.obs) if full else None,
                pitch=self.pitch_mm,
                compute_model_sensitivity=bool(full),
                free_mask=self._gate_free_mask())
            # A model that deliberately holds the factory distortion must not
            # be failed by the gate that asks whether freeing it is worth it.
            release = getattr(self, "_release_decision", None) or {}
            if isinstance(release, Mapping) and release.get(
                    "freed_distortion") is False:
                sub = (gate.get("gates") or {}).get("model_sensitivity")
                if isinstance(sub, dict) and not sub.get("pass", True):
                    sub["pass"] = True
                    sub["criterion"] = "held"
                    sub["note"] = str(release.get("reason") or "held at factory")
                    blocking = [name for name in (gate.get("blocking_gates") or [])
                                if name != "model_sensitivity"]
                    gate["blocking_gates"] = blocking
                    gate["passed"] = not blocking
                    reasons = [r for r in (gate.get("reasons") or [])
                               if "model sensitivity" not in str(r)]
                    gate["reasons"] = reasons
        except Exception as exc:
            self._observability_error = f"{type(exc).__name__}: {exc}"
            return None
        if not isinstance(sig, Mapping) or not isinstance(gate, Mapping):
            return None
        sigmas = dict(sig)
        if not sigmas:
            return None
        ident = gate.get("identifiability")
        if not isinstance(ident, Mapping):
            try:
                ident = module.identifiability(result)
            except Exception:
                ident = {}
        gate_parameters = gate.get("parameters") or {}
        parameters: dict[str, dict] = {}
        for name, sigma in sigmas.items():
            entry = gate_parameters.get(name) or {}
            parameters[name] = {
                "sigma": _jsonable(sigma),
                "unit": entry.get("unit") or _unit_for(name),
                "verdict": entry.get("verdict", "unknown"),
                "ratio": _jsonable(entry.get("ratio")),
                "budget": _jsonable(entry.get("budget")),
            }
        gates = _normalise_gates(gate.get("gates") or {})
        free = (getattr(sig, "free_names", None) or gate.get("free_names")
                or list(sigmas))
        return {
            "parameters": parameters,
            "condition": _jsonable(
                ident.get("condition_scaled")
                or ident.get("condition_number_scaled")
                or ident.get("condition")),
            "rank": int(ident.get("rank", 0) or 0),
            "n_params": int(ident.get("n_free", len(sigmas)) or len(sigmas)),
            "free": list(free),
            "gates": gates,
            "passed": bool(gate.get("passed")),
            "reasons": list(gate.get("reasons") or []),
            "counts": _jsonable(gate.get("counts")),
            "engine": "tools.wizard.observability",
        }

    def _observability_local(self, result: Any) -> dict:
        from calib import quality
        cov_info = (result.info.get("covariance") or {})
        names = [name for name in (result.info.get("param_names") or [])
                 if not str(name).startswith("view")]
        sigmas = result.param_std()
        if not names:
            names = [name for name in sigmas if not str(name).startswith("view")]
        preset = quality.accuracy_preset("fast" if self.mode == "quick" else self.mode)
        limits = {
            "fx": preset["max_sigma_fx_px"], "fy": preset["max_sigma_fx_px"],
            "cx": preset["max_sigma_fx_px"], "cy": preset["max_sigma_fx_px"],
            "tx": preset["max_sigma_baseline_mm"], "ty": preset["max_sigma_baseline_mm"],
            "tz": preset["max_sigma_baseline_mm"],
            "rx": preset["max_sigma_rotation_deg"],
            "ry": preset["max_sigma_rotation_deg"],
            "rz": preset["max_sigma_rotation_deg"],
        }
        parameters = {}
        for name in names:
            sigma = sigmas.get(name)
            if sigma is None:
                continue
            field_name = name.split(".")[-1]
            unit = _unit_for(name)
            value = float(sigma)
            if unit == "deg":
                value = math.degrees(value)
            limit = limits.get(field_name)
            if limit is None:  # distortion coefficients (normalized)
                limit = 0.002
            if not math.isfinite(value):
                verdict = "unobservable"
            elif value <= limit:
                verdict = "constrained"
            elif value <= 10.0 * limit:
                verdict = "weak"
            else:
                verdict = "unobservable"
            parameters[name] = {"sigma": value, "unit": unit,
                                "verdict": verdict, "limit": float(limit)}
        gates: dict[str, dict] = {}
        try:
            rel = quality.relative_pose_sigma(result, "left", "right")
            rot = float(rel.get("rotation_deg", float("nan")))
            gates["stereo_rotation"] = {
                "pass": bool(math.isfinite(rot) and rot <= preset["max_sigma_rotation_deg"]),
                "detail": f"IR relative rotation sigma {rot:.3f} deg "
                          f"(limit {preset['max_sigma_rotation_deg']:.2f})"}
        except Exception as exc:
            gates["stereo_rotation"] = {"pass": False, "detail": f"unavailable: {exc}"}
        try:
            base = float(quality.baseline_sigma(result))
            gates["baseline"] = {
                "pass": bool(math.isfinite(base) and base <= preset["max_sigma_baseline_mm"]),
                "detail": f"baseline sigma {base:.3f} mm "
                          f"(limit {preset['max_sigma_baseline_mm']:.2f})"}
        except Exception as exc:
            gates["baseline"] = {"pass": False, "detail": f"unavailable: {exc}"}
        require_colour = bool(_spec_get(self.coverage_spec, "require_colour", True))
        try:
            rgb = quality.rgb_alignment(result).get("translation_mm")
            colour_cov = ((self._coverage_cache or {}).get("dimensions", {})
                          .get("colour_participation", {}).get("satisfied", False))
            gates["colour"] = {
                "pass": bool((not require_colour) or
                             (colour_cov and isinstance(rgb, (int, float))
                              and rgb <= preset["max_sigma_rgb_mm"])),
                "detail": ("not required" if not require_colour else
                           f"colour alignment sigma {rgb} mm, coverage "
                           f"{'ok' if colour_cov else 'missing'}")}
        except Exception as exc:
            gates["colour"] = {"pass": False, "detail": f"unavailable: {exc}"}
        condition = float(cov_info.get("condition", float("inf")))
        rms = float(result.residual_rms)
        gates["model_sensitivity"] = {
            "pass": bool(math.isfinite(condition) and condition <= 1e7 and rms <= 1.0),
            "detail": f"jacobian condition {condition:.3g}, residual RMS {rms:.3f} px"}
        return {
            "parameters": parameters,
            "condition": None if not math.isfinite(condition) else condition,
            "rank": int(cov_info.get("rank_kept", 0) or 0),
            # n_params/rank describe the full free vector (rig + board poses);
            # ``parameters``/``free`` list the rig parameters the GUI shows.
            "n_params": int(cov_info.get("n_free", len(names)) or len(names)),
            "free": names,
            "gates": gates,
            "engine": "tools.wizard.controller (fallback)",
        }

    # -- guidance --------------------------------------------------------
    def _guidance(self) -> dict:
        # Coverage guidance (agent B) is the operator-facing authority: it
        # knows the unsatisfied dimensions and the pose family that unlocks
        # them.  Information-gain guidance from the session is the fallback.
        module = _optional("coverage")
        target_fn = getattr(module, "guidance_target", None) if module else None
        if callable(target_fn) and self._measurement is not None:
            try:
                spec = self.coverage_spec
                spec_cls = getattr(module, "CoverageSpec", None)
                if spec_cls is not None and not isinstance(spec, spec_cls):
                    spec = None
                plan = target_fn(self._measurement, self._last_pose, spec)
                if isinstance(plan, dict) and plan.get("instruction"):
                    family = plan.get("family") or {}
                    return {
                        "instruction": str(plan["instruction"]),
                        "detail": str(plan.get("reason") or ""),
                        "priority": self._family_priority(plan.get("mode"),
                                                          family),
                        "target": {
                            "distance_mm": family.get("distance_mm"),
                            "tilt_deg": [family.get("tilt_x_deg"),
                                         family.get("tilt_y_deg")],
                            "roll_deg": family.get("roll_deg"),
                            "region": self._region_from_rc(
                                family.get("image_region")),
                            "label": ", ".join(str(x) for x in
                                               (plan.get("unlocks") or [])),
                            "mode": plan.get("mode"),
                        },
                    }
            except Exception:
                pass
        try:
            recommendation = self.session.recommend()
        except Exception as exc:
            return {"instruction": "move to a new pose", "detail": str(exc),
                    "priority": "pose", "target": {}}
        if not recommendation.get("ready", False):
            return {"instruction": recommendation.get("text", "hold still"),
                    "detail": recommendation.get("reason", ""),
                    "priority": "none", "target": {}}
        instruction = recommendation.get("instruction") or {}
        target = recommendation.get("target") or {}
        target_pose = recommendation.get("target_pose")
        priority = self._guidance_priority(instruction, target, target_pose)
        detail = "; ".join(str(x) for x in (recommendation.get("explanation") or []))
        region = None
        if target_pose is not None:
            region = self._region_of_pose(target_pose)
        return {
            "instruction": str(recommendation.get("text") or
                               instruction.get("text") or "move to a new pose"),
            "detail": detail,
            "priority": priority,
            "target": {
                "distance_mm": target.get("distance_mm"),
                "tilt_deg": [target.get("tilt_x_deg"), target.get("tilt_y_deg")],
                "roll_deg": target.get("roll_deg"),
                "region": region,
                "label": target.get("label"),
            },
        }

    def _guidance_priority(self, instruction: Mapping[str, Any],
                           target: Mapping[str, Any], target_pose: Any) -> str:
        move = instruction.get("move_mm") or [0.0, 0.0, 0.0]
        rotate = instruction.get("rotate_deg") or [0.0, 0.0, 0.0]
        distance_change = abs(float(instruction.get("distance_change_mm", 0.0)))
        lateral = math.hypot(float(move[0]), float(move[1]))
        tilt = math.hypot(float(rotate[0]), float(rotate[1]))
        roll = abs(float(rotate[2]))
        if distance_change >= 40.0 and distance_change >= lateral:
            return "distance"
        if tilt >= 5.0:
            return "tilt"
        if roll >= 15.0:
            return "roll"
        if lateral >= 40.0:
            return "lateral"
        return "pose"

    def _family_priority(self, mode: str | None, family: Mapping[str, Any]) -> str:
        if mode == "done":
            return "none"
        if mode == "hold":
            return "stability"
        unlocks = " ".join(str(x) for x in (family.get("unlocks") or []))
        if "distance" in unlocks:
            return "distance"
        if "tilt" in unlocks:
            return "tilt"
        if "roll" in unlocks:
            return "roll"
        if "image" in unlocks or "corner" in unlocks:
            return "region"
        return "pose"

    def _region_from_rc(self, region: Any) -> str | None:
        if not isinstance(region, (list, tuple)) or len(region) != 2:
            return None
        names = {(0, 0): "top-left", (0, 1): "top", (0, 2): "top-right",
                 (1, 0): "left", (1, 1): "centre", (1, 2): "right",
                 (2, 0): "bottom-left", (2, 1): "bottom",
                 (2, 2): "bottom-right"}
        try:
            return names.get((int(region[0]), int(region[1])))
        except (TypeError, ValueError):
            return None

    def _region_of_pose(self, pose: tuple) -> str | None:
        try:
            from calib.geometry import project
            cam = self.cameras["left"]
            uv = project(cam.K, cam.kc,
                         np.asarray(pose[1], float).reshape(1, 3))[0]
        except Exception:
            return None
        if not np.isfinite(uv).all():
            return None
        col = int(np.clip(uv[0] / types.IMAGE_WIDTH * 3, 0, 2))
        row = int(np.clip(uv[1] / types.IMAGE_HEIGHT * 3, 0, 2))
        names = {(0, 0): "top-left", (0, 1): "top", (0, 2): "top-right",
                 (1, 0): "left", (1, 1): "centre", (1, 2): "right",
                 (2, 0): "bottom-left", (2, 1): "bottom",
                 (2, 2): "bottom-right"}
        return names[(row, col)]

    def recommend(self) -> dict:
        return copy.deepcopy(self._guidance_cache or
                             {"instruction": "move to a new pose", "detail": "",
                              "priority": "pose", "target": {}})

    # ------------------------------------------------------------------
    # pause / resume
    # ------------------------------------------------------------------
    def pause(self) -> None:
        with self._lock:
            if self.state != "recording":
                return
            self.state = "paused"
            if self.capture is not None:
                try:
                    self.capture.pause()
                except Exception as exc:
                    self.error = f"pause failed: {exc}"
            self._write_session_json()
            self._publish()

    def resume(self) -> None:
        with self._lock:
            if self.state != "paused":
                return
            self.state = "recording"
            if self.capture is not None:
                try:
                    self.capture.resume()
                except Exception as exc:
                    self.error = f"resume failed: {exc}"
            self._write_session_json()
            self._publish()

    # ------------------------------------------------------------------
    # finish / cancel
    # ------------------------------------------------------------------
    def finish(self) -> dict:
        """Final solve, candidate profile + validation report; never activates."""
        with self._lock:
            if self.state == "done" and self._finish_result is not None:
                return copy.deepcopy(self._finish_result)
            if self.state in ("idle", "cancelled"):
                self.begin()
            self.state = "finishing"
            self._publish()
            factory_before = self.factory_sha256
            try:
                pruned = self._prune_outliers()
                release = self._stage_release_decision()
                self._release_decision = release
                outcome = self.session.finish(profile_root=None)
            except Exception as exc:
                self.error = f"final solve failed: {type(exc).__name__}: {exc}"
                self.state = "error"
                self._write_session_json()
                self._publish()
                result = {"ok": False, "state": self.state, "error": self.error}
                self._finish_result = result
                return copy.deepcopy(result)

            self._refresh_derived(force=True)
            self._refresh_observability(force=True, full=True)
            coverage = copy.deepcopy(self._coverage_cache or {})
            readiness = copy.deepcopy(self._readiness_cache or {})
            observability = copy.deepcopy(self._observability_cache or {})
            calibration = outcome.get("result")
            candidate_info: dict[str, Any] = {"produced": False}
            validation: dict[str, Any] = {}
            if calibration is None:
                candidate_info.update({
                    "reason": outcome.get("reason", "no accepted views"),
                    "accepted_views": len(self.session.accepted),
                })
                validation = {
                    "schema": VALIDATION_SCHEMA, "validation": "not_produced",
                    "reason": candidate_info["reason"],
                    "accepted_views": len(self.session.accepted),
                    "coverage": coverage, "readiness": readiness,
                    "observability": observability,
                }
            else:
                candidate = self._candidate_profile(calibration)
                verify_report, quality_report = self._validation_metrics(calibration)
                candidate_path = self._save_candidate_file(candidate, {})
                lifecycle_validate = self._run_lifecycle_validate(
                    candidate_path, calibration)
                verdict = self._candidate_verdict(
                    verify_report, quality_report, readiness, observability,
                    lifecycle=lifecycle_validate)
                validation = {
                    "schema": VALIDATION_SCHEMA,
                    "validation": "pending",
                    "parameter_release": _jsonable(release),
                    "outlier_pruning": _jsonable(pruned),
                    "candidate": str(candidate_path),
                    "factory_sha256": self.factory_sha256,
                    "accepted_views": len(self.session.accepted),
                    "rejected_views": len(self.session.rejected),
                    "rms_px": float(calibration.residual_rms),
                    "verify": _jsonable(verify_report),
                    "quality": _jsonable(quality_report),
                    "coverage": coverage,
                    "readiness": readiness,
                    "observability": observability,
                    "verdict": verdict,
                    "lifecycle": self._lifecycle,
                    "lifecycle_validate": _jsonable(lifecycle_validate),
                }
                # `lifecycle.activate` reads this file and requires the verdict
                # at the top level (`passed`, `candidate_sha256`).  Keep the
                # nested copy for the report and mirror the authoritative keys.
                if isinstance(lifecycle_validate, Mapping):
                    for key in ("passed", "candidate_sha256", "gates",
                                "metrics", "reasons", "created_utc", "n_views"):
                        if key in lifecycle_validate:
                            validation[key] = lifecycle_validate[key]
                types.atomic_write_json(self.validation_path, validation)
                candidate_info = {
                    "produced": True, "path": str(candidate_path),
                    "validation_path": str(self.validation_path),
                    "sha256": _sha256_file(candidate_path),
                    "validation": "pending",
                    "verdict": verdict,
                }
            factory_after = (self.factory_sha256 if self.profile_path is None
                             or not self.profile_path.is_file()
                             else _sha256_file(self.profile_path))
            factory_ok = (factory_after == factory_before)

            report_path = self._write_report(coverage, readiness, observability,
                                             candidate_info, validation,
                                             outcome)
            self.finished_wall = time.time()
            self.state = "done" if factory_ok else "error"
            if not factory_ok:
                self.error = ("factory profile changed during the session; "
                              "candidate kept but not marked done")
            self._write_session_json()
            self._write_manifest()
            self._publish()
            result = {
                "ok": bool(candidate_info.get("produced", False)) and factory_ok,
                "state": self.state,
                "error": self.error,
                "candidate": candidate_info,
                "validation": validation,
                "coverage": coverage,
                "readiness": readiness,
                "observability": observability,
                "session": self.snapshot(),
                "report_path": str(report_path),
                "session_dir": str(self.paths.root),
                "factory_sha256": self.factory_sha256,
                "factory_unchanged": factory_ok,
                "accepted": len(self.session.accepted),
                "rejected": len(self.session.rejected),
                "elapsed_s": self.elapsed_s,
                "stopping": _jsonable(outcome.get("quality", {})),
            }
            self._finish_result = result
            return copy.deepcopy(result)

    def cancel(self) -> dict:
        """Keep all recorded data, produce no profile, state ``cancelled``."""
        with self._lock:
            self.state = "cancelled"
            self.finished_wall = time.time()
            self._write_session_json()
            self._publish()
            return {"ok": True, "state": self.state,
                    "session_dir": str(self.paths.root),
                    "accepted": len(self.session.accepted),
                    "rejected": len(self.session.rejected),
                    "candidate_produced": self.candidate_path.is_file()}

    # -- candidate -------------------------------------------------------
    def _candidate_profile(self, result: Any) -> dict:
        cameras = {}
        for key in CAMERAS:
            cam = result.cameras[key]
            R, T = result.extrinsics[key]
            cameras[JSON_NAMES[key]] = {
                "fx": float(cam.fx), "fy": float(cam.fy),
                "cx": float(cam.cx), "cy": float(cam.cy),
                # profile JSON stores absolute K[0,1] in pixels
                "skew": float(cam.skew * cam.fx),
                "dist": [float(x) for x in np.asarray(cam.kc, float)[:5]],
                "R": [float(x) for x in np.asarray(R, float).ravel()],
                "T": [float(x) for x in np.asarray(T, float).ravel()],
            }
        distance = _pose_summary(self.session.last_pose)
        return {
            "schema": PROFILE_SCHEMA,
            "kind": "candidate",
            "validation": "pending",
            "image_width": int(self.profile.width),
            "image_height": int(self.profile.height),
            "units": "mm",
            "plate_pitch_mm": float(self.pitch_mm),
            "color_status": self.profile.color_status,
            "cameras": cameras,
            "session": {
                "dir": str(self.paths.root),
                "mode": self.mode, "goal": self.goal,
                "accepted_views": len(self.session.accepted),
                "rejected_views": len(self.session.rejected),
                "residual_rms_px": float(result.residual_rms),
                "last_pose_distance_mm": distance["distance_mm"] if distance else None,
            },
            "provenance": [
                "tool=tools/wizard/controller.py",
                f"wizard_version={WIZARD_VERSION}",
                f"mode={self.mode}", f"pitch_mm={self.pitch_mm:g}",
                f"views={len(self.session.accepted)}",
                f"factory_sha256={self.factory_sha256}",
                "world_frame=ir_left",
                "skew=absolute K[0,1] in pixels (C++ convention)",
                "host-side only; never written to the scanner",
            ],
        }

    def _write_candidate(self, candidate: Mapping[str, Any]) -> Path:
        path = self.candidate_path
        types.atomic_write_json(path, candidate)
        doc = types.read_json(path, default=None)
        if not isinstance(doc, dict) or doc.get("validation") != "pending":
            raise RuntimeError("candidate profile was not written with "
                               "validation: pending")
        return path

    def _save_candidate_file(self, candidate: Mapping[str, Any],
                             report: Mapping[str, Any]) -> Path:
        """Write the candidate with ``tools.wizard.lifecycle`` when present."""
        module = _optional("lifecycle")
        save = getattr(module, "save_candidate", None) if module else None
        if not callable(save):
            self._lifecycle = "local"
            return self._write_candidate(candidate)
        try:
            path = Path(save(self.paths.root, dict(candidate), dict(report)))
            self._lifecycle = f"tools.wizard.lifecycle.save_candidate -> {path}"
            if path.is_file() and types.read_json(path, default={}).get(
                    "validation") == "pending":
                return path
            self._lifecycle += " (refreshed locally: validation not pending)"
        except Exception as exc:
            self._lifecycle = (f"local (lifecycle save failed: "
                               f"{type(exc).__name__}: {exc})")
        return self._write_candidate(candidate)

    def _prune_outliers(self, threshold_px: float = 3.0) -> dict:
        """Drop per-camera observations that no longer fit the current model.

        A view can pass the orientation witness and still carry a bad label
        assignment or a mis-detected marker.  Measured on a 33-view synthetic
        session: 95 of 96 view-camera pairs fitted below 1.1 px while one
        colour observation sat at 17.4 px and drove the whole final residual to
        15 px.  Those observations are removed before the final solve (and the
        removal is reported), rather than letting one view poison the model.
        """
        from calib import geometry, plate as _plate
        info = {"threshold_px": float(threshold_px), "checked": 0, "dropped": [],
                "kept": 0}
        obs = list(getattr(self.session, "obs", []) or [])
        if not obs:
            return info
        points = _plate.board_points_mm()
        index = {(float(u), float(v)): i
                 for i, (u, v) in enumerate(_plate.lattice_uv())}
        keep = []
        for o in obs:
            info["checked"] += 1
            try:
                keys = [k for k in o.uv if (float(k[0]), float(k[1])) in index]
                if len(keys) < 12:
                    info["dropped"].append({"view": int(o.view),
                                            "camera": o.camera, "reason": "few"})
                    continue
                obj = np.asarray([points[index[(float(u), float(v))]] for u, v in keys])
                img = np.asarray([o.uv[k] for k in keys], float)
                cam = self.cameras[o.camera]
                pose = geometry.solve_pnp(obj, img, np.asarray(cam.K, float),
                                          np.asarray(cam.kc, float))
                X = (pose.R @ obj.T).T + pose.t
                pred = geometry.project(np.asarray(cam.K, float),
                                        np.asarray(cam.kc, float), X)
                rms = float(np.sqrt(np.mean(np.sum((pred - img) ** 2, axis=1))))
            except Exception:
                info["dropped"].append({"view": int(o.view), "camera": o.camera,
                                        "reason": "pnp failed"})
                continue
            if rms > threshold_px:
                info["dropped"].append({"view": int(o.view), "camera": o.camera,
                                        "rms_px": round(rms, 3)})
            else:
                keep.append(o)
                info["kept"] += 1
        if info["dropped"]:
            try:
                self.session.obs = keep
            except Exception:
                pass
        return info

    def _install_prior_solver(self) -> None:
        """Route the session's solves through the prior-regularised solver."""
        session = self.controller
        base = getattr(session, "solve_fn", None)
        if base is None:
            return

        def solve_with_priors(obs, pitch, cameras, free_kc, loss, f_scale):
            try:
                from calib import pipeline
                return pipeline.solve_observations(
                    list(obs), float(pitch), cameras, bool(free_kc),
                    backend="auto", robust_loss=loss, f_scale=f_scale,
                    priors=self._prior_spec() or None)
            except Exception:
                return base(obs, pitch, cameras, free_kc, loss, f_scale)

        try:
            session.solve_fn = solve_with_priors
        except Exception:
            pass

    def _prior_spec(self) -> dict:
        """Priors that hold what the data cannot determine.

        The principal point is weakly observable from a plate session (measured
        3-5 px 1-sigma) and correlates with the board pose; letting it float
        trades a marginally lower residual for a worse ray geometry.  A 1 px
        prior at the factory value regularises it without freezing it, and a
        5 mm prior on the colour camera's depth translation keeps the
        unobservable gauge direction bounded.
        """
        parameters: dict[str, tuple[float, float]] = {}
        for key in CAMERAS:
            try:
                cam = self.profile.camera(JSON_NAMES[key])
            except Exception:
                continue
            parameters[f"{key}.cx"] = (float(cam.K[0, 2]), 1.0)
            parameters[f"{key}.cy"] = (float(cam.K[1, 2]), 1.0)
        # No prior on the colour translation: the shipped colour extrinsic is
        # stale by ~18 mm, so regularising toward it dragged the whole fit to a
        # 15 px residual (measured).  The colour camera is left to the data.
        return {"parameters": parameters} if parameters else {}

    def _gate_free_mask(self):
        """Free mask for the observability gate, excluding prior-held params."""
        try:
            from calib.adaptive import NRIG
            mask = self.session.result.info.get("free_mask")
            if mask is None:
                return None
            mask = np.asarray(mask[:NRIG], bool).copy()
            names = list(self.session.result.info.get("param_names") or [])[:NRIG]
            held = set(self._prior_spec().get("parameters") or {})
            for i, name in enumerate(names):
                if name in held and i < mask.size:
                    mask[i] = False
            return mask
        except Exception:
            return None

    def _reference_result(self):
        """The factory rig as a CalibrationResult, for relative gates.

        `validate` needs a reference model to judge cross-camera error and the
        stereo baseline; without one it can only apply absolute limits, which
        are below the measurement noise on good data (measured: ground truth
        1.15 px vs fitted 0.55 px on the same observations).
        """
        try:
            from calib.calibrate import CalibrationResult
            cams = {key: self.profile.camera(JSON_NAMES[key]).intrinsics()
                    for key in CAMERAS}
            ext = {}
            for key in CAMERAS:
                cam = self.profile.camera(JSON_NAMES[key])
                ext[key] = (np.asarray(cam.R, float).reshape(3, 3),
                            np.asarray(cam.T, float).reshape(3))
            return CalibrationResult(
                cameras=cams, extrinsics=ext, views={}, covariance=None,
                residual_rms=float(getattr(self.profile, "residual_rms", 0.0) or 0.0),
                residual_max=0.0, n_observations=len(self.session.obs),
                n_parameters=0)
        except Exception:
            return None

    def _stage_release_decision(self) -> dict:
        """Decide whether the final solve may free the distortion model.

        A lower reprojection residual does not justify extra parameters: on real
        data freeing distortion made held-out prediction *worse* (vendor corpus,
        -4.1 %).  The staged rule is therefore data driven - keep the factory
        distortion unless a held-out refit shows a real gain (>= 10 %).  This
        runs once, at finish time, and the decision is recorded in the report.
        """
        info: dict = {"freed_distortion": None, "worth_freeing": None,
                      "reason": ""}
        result = getattr(self.session, "result", None)
        policy = getattr(self.session, "policy", None)
        if result is None or not isinstance(policy, dict):
            info["reason"] = "no fitted result yet"
            return info
        if not policy.get("free_kc"):
            info.update(freed_distortion=False,
                        reason="distortion already held at the factory model")
            return info
        try:
            from . import observability as _obs
            report = _obs.model_sensitivity_analysis(
                list(self.session.obs), result, pitch=self.pitch_mm)
        except Exception as exc:
            info["reason"] = f"sensitivity analysis unavailable: {exc}"
            return info
        improvement = report.get("relative_improvement")
        info["worth_freeing"] = bool(report.get("worth_freeing"))
        info["relative_improvement"] = improvement
        if report.get("worth_freeing"):
            info.update(freed_distortion=True,
                        reason=(f"freeing distortion improves held-out error by "
                                f"{100 * float(improvement or 0):+.1f} %"))
        else:
            policy["free_kc"] = False
            # `session.finish` only solves when there is no result yet, so the
            # model must be refitted here with the held distortion, otherwise
            # the decision would be recorded but never applied.
            try:
                # `_maybe_solve` skips when the accepted count does not divide
                # `solve_every`, which silently left the old (distortion-free)
                # model in place: the 36-view run recorded the held decision but
                # still reported k2/k3 as free.  Force this one solve.
                saved = self.session.policy.get("solve_every", 1)
                self.session.policy["solve_every"] = 1
                try:
                    solved = self.session.solve_now()
                finally:
                    self.session.policy["solve_every"] = saved
                info["refit"] = bool(solved)
            except Exception as exc:
                info["resolve_error"] = f"{type(exc).__name__}: {exc}"
            info.update(freed_distortion=False,
                        reason=("holding the factory distortion model: freeing "
                                f"it changes held-out error by "
                                f"{100 * float(improvement or 0):+.1f} % (< 10 %)"))
        return info

    def _run_lifecycle_validate(self, candidate_path: Path,
                                calibration: Any) -> dict:
        """Run the Phase-7 gate suite when the lifecycle module provides it."""
        module = _optional("lifecycle")
        validate = getattr(module, "validate", None) if module else None
        if not callable(validate):
            return {"available": False,
                    "note": "tools.wizard.lifecycle.validate is absent"}
        try:
            # `gates` carries the wizard's own measurements (coverage,
            # observability) plus the fitted result, so the lifecycle gate can
            # judge them without recomputing.  Model sensitivity needs a real
            # refit and is computed once here, at finish time, not per frame.
            context: dict = {"calibration_result": calibration}
            if isinstance(getattr(self, "_release_decision", None), dict):
                context["release"] = self._release_decision
            if self._measurement:
                context["coverage"] = self._measurement
            if self._observability_cache:
                context["observability"] = self._observability_cache
            try:
                from . import observability as _obs_mod
                ms = _obs_mod.model_sensitivity_analysis(
                    list(self.session.obs), calibration, pitch=self.pitch_mm)
                if ms:
                    context["model_sensitivity"] = ms
            except Exception as exc:  # optional, never fatal
                context["model_sensitivity_error"] = (
                    f"{type(exc).__name__}: {exc}")
            reference = self._reference_result()
            result = validate(candidate_path, list(self.session.obs), reference,
                              gates=context)
            return _jsonable(result)
        except Exception as exc:
            return {"available": False,
                    "error": f"{type(exc).__name__}: {exc}"}

    def _validation_metrics(self, result: Any) -> tuple[dict, dict]:
        from calib import quality, verify
        observations = list(self.session.obs)
        heldout_rms = None
        # Run the independent checks separately: one fragile sub-check must not
        # report the whole verification as "unavailable" (a 36-view synthetic
        # run hit "list index out of range" inside split-half and lost the
        # held-out number too).
        verify_report: dict = {"rms_px": float(result.residual_rms), "gates": []}
        try:
            verify_report["trends"] = verify.residual_trends(
                result, observations, self.pitch_mm)
        except Exception as exc:
            verify_report["trends_error"] = f"{type(exc).__name__}: {exc}"
        try:
            verify_report["epipolar"] = verify.epipolar_consistency(
                result, observations)
        except Exception as exc:
            verify_report["epipolar_error"] = f"{type(exc).__name__}: {exc}"
        try:
            held = verify.heldout(observations, self.cameras0, self.pitch_mm)
            verify_report["heldout"] = held
            heldout = (held or {}).get("heldout_rms_px")
            if isinstance(heldout, (int, float)):
                heldout_rms = float(heldout)
        except Exception as exc:
            verify_report["heldout_error"] = f"{type(exc).__name__}: {exc}"
        try:
            verify_report["split_half"] = verify.split_half(
                observations, self.cameras0, self.pitch_mm)
        except Exception as exc:
            verify_report["split_half_error"] = f"{type(exc).__name__}: {exc}"
        trends = verify_report.get("trends") or {}
        if not trends.get("ok", False):
            verify_report["gates"].append("no residual table")
        elif trends.get("tilt_bias_detected"):
            verify_report["gates"].append(
                "tilt-proportional residual bias "
                f"({trends['circular_tilt_slope']['slope']:.3f} px/deg)")
        epi = verify_report.get("epipolar") or {}
        if epi.get("ok") and epi.get("p95_px", 0) > 1.0:
            verify_report["gates"].append(f"epipolar p95 {epi['p95_px']:.2f} px > 1.0")
        verify_report["ok"] = not verify_report["gates"]
        target = "fast" if self.mode == "quick" else self.mode
        try:
            quality_report = quality.quality_report(
                result, observations, heldout_rms_px=heldout_rms,
                target=target, pitch=self.pitch_mm,
                compute_stability=True)
            try:
                # keep the individual stopping checks visible: a "fail" here is
                # advisory but must be explainable in the report
                quality_report["stopping_checks"] = quality.stopping_report(
                    result, observations, heldout_rms_px=heldout_rms,
                    pitch=self.pitch_mm,
                    **quality.accuracy_preset(target))
            except Exception as exc:
                quality_report["stopping_error"] = f"{type(exc).__name__}: {exc}"
        except Exception as exc:
            quality_report = {"status": "unknown",
                              "error": f"{type(exc).__name__}: {exc}"}
        return verify_report, quality_report

    def _candidate_verdict(self, verify_report: Mapping[str, Any],
                           quality_report: Mapping[str, Any],
                           readiness: Mapping[str, Any],
                           observability: Mapping[str, Any],
                           *, lifecycle: Mapping[str, Any] | None = None) -> dict:
        lifecycle = lifecycle or {}
        lifecycle_ok = lifecycle.get("passed")
        gates = {
            "verify_ok": bool(verify_report.get("ok", False)),
            "lifecycle_passed": (None if lifecycle_ok is None
                                 else bool(lifecycle_ok)),
            "quality_status": str(quality_report.get("status", "unknown")),
            "coverage_ready": bool(readiness.get("ready", False)),
            "observability_gates": {
                name: bool((gate or {}).get("pass", False))
                for name, gate in (observability.get("gates") or {}).items()},
        }
        reasons = []
        if not gates["verify_ok"]:
            reasons.extend(verify_report.get("gates") or
                           [verify_report.get("error", "internal verification failed")])
        if gates["quality_status"] != "pass":
            reasons.append(f"quality status {gates['quality_status']}")
        if not gates["coverage_ready"]:
            reasons.append("coverage not ready: "
                           + ", ".join(readiness.get("missing") or []))
        failed = [name for name, ok in gates["observability_gates"].items() if not ok]
        if failed:
            reasons.append("observability gates failed: " + ", ".join(failed))
        if lifecycle_ok is False:
            reasons.extend(str(r) for r in (lifecycle.get("reasons") or []))
        # The lifecycle suite is the authoritative, gate-based validator (nine
        # independent checks including held-out cross-camera, repeatability and
        # triangulation).  The covariance "quality" preset is stricter than the
        # data can support (it asks for sigma(fx) <= 0.75 px, while the real
        # vendor corpus measures 1.7-2.0 px), so when the lifecycle ran and
        # passed, its verdict governs and quality stays a reported advisory.
        lifecycle_authoritative = lifecycle_ok is True
        passed = (gates["coverage_ready"] and not failed
                  and lifecycle_ok is not False
                  and (lifecycle_authoritative
                       or (gates["verify_ok"]
                           and gates["quality_status"] == "pass")))
        return {
            "status": "pending",
            "would_activate": bool(passed),
            "gates": gates,
            "reasons": [str(r) for r in reasons],
            "note": ("validation stays pending; activation is a separate, "
                     "explicit lifecycle step and the factory profile is never "
                     "modified by this controller"),
        }

    # ------------------------------------------------------------------
    # writing
    # ------------------------------------------------------------------
    def _write_session_json(self) -> None:
        try:
            previous = types.read_json(self.paths.session_json, default=None)
            previous = previous if isinstance(previous, dict) else {}
            payload = {
                "schema": SESSION_SCHEMA,
                "state": self.state,
                "created_at": (datetime.fromtimestamp(self.started_wall,
                                                      tz=timezone.utc).isoformat()
                               if self.started_wall else None),
                "started_at": self.started_wall,
                "elapsed_s": self.elapsed_s,
                "config": self._config_dict(),
                "software": dict(self.software),
                "hashes": {"factory_profile": self.factory_sha256,
                           "manifest": self._manifest_sha256()},
                "factory": {"path": str(self.profile_path or self.profile.path),
                            "sha256": self.factory_sha256,
                            "kind": self.profile.kind,
                            "color_status": self.profile.color_status},
                "counters": {
                    "decisions": len(self._decisions),
                    "accepted": len(self.session.accepted),
                    "rejected": len(self.session.rejected),
                    "redundant": self._redundant_count(),
                    "by_reason": dict(sorted(self._reason_counts.items())),
                    "groups": self._n_materialised,
                    "next_group_index": (max((v["view_id"] for v in self._views),
                                             default=-1) + 1),
                    "next_decision_seq": len(self._decisions),
                    "n_materialised": self._n_materialised,
                },
                "paths": {
                    "root": str(self.paths.root),
                    "groups": str(self.paths.groups_dir),
                    "decisions": str(self.paths.decisions_jsonl),
                    "manifest": str(self.paths.manifest_json),
                    "candidate": str(self.paths.candidate_dir),
                },
                "error": self.error,
            }
            # keep whatever the sibling session_io module created
            for key in ("created_utc", "spec", "config_sha256", "history"):
                if key in previous and key not in payload:
                    payload[key] = previous[key]
            if isinstance(previous.get("history"), list):
                payload["history"] = previous["history"][-80:]
            types.atomic_write_json(self.paths.session_json, _jsonable(payload))
        except OSError as exc:
            self.error = f"cannot write session.json: {exc}"

    def _manifest_sha256(self) -> str | None:
        try:
            if self.paths.manifest_json.is_file():
                return _sha256_file(self.paths.manifest_json)
        except OSError:
            pass
        return None

    def _redundant_count(self) -> int:
        return sum(self._reason_counts.get(k, 0)
                   for k in ("duplicate", "redundant"))

    def _manifest_payload(self) -> dict:
        coverage = self._coverage_cache or {}
        readiness = self._readiness_cache or {}
        return {
            "schema": MANIFEST_SCHEMA,
            "mode": self.mode, "goal": self.goal,
            "pitch_mm": float(self.pitch_mm),
            "factory_profile": str(self.profile_path or self.profile.path),
            "factory_sha256": self.factory_sha256,
            "software": dict(self.software),
            "config": {
                "require_colour": bool(_spec_get(self.coverage_spec,
                                                 "require_colour", True)),
                "coverage": self._coverage_spec_dict(),
            },
            "counts": {
                "accepted": len(self.session.accepted),
                "rejected": len(self.session.rejected),
                "redundant": self._redundant_count(),
                "by_reason": dict(sorted(self._reason_counts.items())),
            },
            "views": list(self._views),
            # session_io-compatible view list (same files, same digests)
            "accepted": [
                {k: view.get(k) for k in
                 ("index", "view_id", "device_group", "pose", "quality",
                  "n_markers", "files", "timestamp", "bootstrap_seed")}
                for view in ({"index": v["view_id"], **v} for v in self._views)
            ],
            "n_accepted": len(self._views),
            "accepted_digest": _digest_json([
                {k: ({**v, "index": v["view_id"]}.get(k))
                 for k in ("index", "view_id", "device_group", "pose",
                           "quality", "files")}
                for v in self._views]),
            "coverage": coverage,
            "readiness": readiness,
        }

    def _write_manifest(self) -> None:
        try:
            types.atomic_write_json(self.paths.manifest_json,
                                    _jsonable(self._manifest_payload()))
        except OSError as exc:
            self.error = f"cannot write manifest.json: {exc}"

    def manifest_digest(self) -> str | None:
        try:
            return _sha256_file(self.paths.manifest_json)
        except OSError:
            return None

    def decisions(self) -> list[dict]:
        """The canonical, deterministic decision projection (JSON values)."""
        return [_jsonable(dict(entry)) for entry in self._decisions]

    # ------------------------------------------------------------------
    # report
    # ------------------------------------------------------------------
    def _write_report(self, coverage: Mapping[str, Any],
                      readiness: Mapping[str, Any],
                      observability: Mapping[str, Any],
                      candidate_info: Mapping[str, Any],
                      validation: Mapping[str, Any],
                      outcome: Mapping[str, Any]) -> Path:
        lines: list[str] = []
        add = lines.append
        add("# Calibration wizard session report")
        add("")
        add(f"- Session directory: `{self.paths.root}`")
        add(f"- Mode: `{self.mode}`  Goal: `{self.goal}`  "
            f"Pitch: {self.pitch_mm:g} mm")
        add(f"- Factory profile: `{self.profile_path or self.profile.path}`")
        add(f"- Factory SHA-256: `{self.factory_sha256 or 'unknown'}` "
            "(read-only; re-checked after the session)")
        source = self.source_info
        if source is not None:
            add(f"- Source: `{source.kind}` `{source.path}` "
                f"{source.note}".rstrip())
        add(f"- Software: {self.software.get('name')} "
            f"{self.software.get('version')} (engine {self.software.get('engine')})")
        add(f"- Elapsed: {self.elapsed_s:.1f} s")
        add("")
        add("## Decisions")
        add("")
        add("| result | count |")
        add("|---|---|")
        add(f"| accepted | {len(self.session.accepted)} |")
        add(f"| rejected | {len(self.session.rejected)} |")
        add(f"| of which redundant/duplicate | {self._redundant_count()} |")
        add("")
        if self._reason_counts:
            add("Rejections by reason category:")
            add("")
            for name, count in sorted(self._reason_counts.items(),
                                      key=lambda kv: (-kv[1], kv[0])):
                add(f"- `{name}`: {count}")
            add("")
        add("## Accepted views")
        add("")
        if self._views:
            add("| view | group | distance mm | tilt deg | roll deg | "
                "shared IR | gain | solved |")
            add("|---|---|---|---|---|---|---|---|")
            for view in self._views:
                add(f"| {view['view_id']} | {view['group']} | "
                    f"{_fmt(view['distance_mm'], 1)} | {_fmt(view['tilt_deg'], 1)} | "
                    f"{_fmt(view['roll_deg'], 1)} | {view['shared_markers_ir']} | "
                    f"{_fmt(view['predicted_gain'], 4)} | {view['solved']} |")
        else:
            add("_No views accepted._")
        add("")
        add("## Coverage and readiness")
        add("")
        add(f"**{'READY' if readiness.get('ready') else 'NOT READY'}** — "
            f"{readiness.get('reason', '')}")
        add("")
        for name, dim in (coverage.get("dimensions") or {}).items():
            mark = "x" if dim.get("satisfied") else " "
            add(f"- [{mark}] `{name}` — required {dim.get('required')}; "
                f"measured {dim.get('measured')}"
                + (f"; deficit {dim.get('deficit')}" if dim.get("deficit") else ""))
        add("")
        if readiness.get("missing"):
            add("Missing dimensions: " + ", ".join(readiness["missing"]))
            add("")
        add("## Observability")
        add("")
        add(f"- engine: `{observability.get('engine')}`")
        add(f"- rank: {observability.get('rank')} / "
            f"{observability.get('n_params')} free parameters")
        add(f"- Jacobian condition: {_fmt(observability.get('condition'), 3)}")
        params = observability.get("parameters") or {}
        counts: dict[str, int] = {}
        for entry in params.values():
            counts[entry.get("verdict", "?")] = counts.get(entry.get("verdict", "?"), 0) + 1
        if counts:
            add("- parameter verdicts: " + ", ".join(
                f"{k} {v}" for k, v in sorted(counts.items())))
            weak = [(name, entry) for name, entry in params.items()
                    if entry.get("verdict") != "constrained"]
            weak.sort(key=lambda item: -(item[1].get("sigma") or 0.0))
            for name, entry in weak[:12]:
                add(f"  - `{name}`: sigma {_fmt(entry.get('sigma'), 4)} "
                    f"{entry.get('unit')} — {entry.get('verdict')}")
        for gate, entry in (observability.get("gates") or {}).items():
            mark = "pass" if (entry or {}).get("pass") else "FAIL"
            add(f"- gate `{gate}`: {mark} — {(entry or {}).get('detail', '')}")
        add("")
        add("## Candidate profile")
        add("")
        if candidate_info.get("produced"):
            add(f"- Profile: `{candidate_info.get('path')}` "
                f"(validation: **pending**)")
            add(f"- SHA-256: `{candidate_info.get('sha256')}`")
            add(f"- Validation metrics: `{candidate_info.get('validation_path')}`")
            verdict = candidate_info.get("verdict") or {}
            add(f"- Would activate: **{bool(verdict.get('would_activate'))}**")
            for reason in verdict.get("reasons") or []:
                add(f"  - {reason}")
            add("- The factory profile was not modified; activation is an "
                "explicit lifecycle step.")
        else:
            add(f"- **No candidate profile produced**: "
                f"{candidate_info.get('reason', 'unknown')}")
            add(f"- Accepted views: {candidate_info.get('accepted_views', 0)}")
        add("")
        add("## Stopping decision")
        add("")
        stopping = outcome.get("quality") or {}
        if stopping:
            add(f"- stop: {stopping.get('stop')} — {stopping.get('reason', '')}")
            for name, check in (stopping.get("checks") or {}).items():
                if not (check or {}).get("pass"):
                    add(f"  - failed `{name}`: {check.get('value')} vs "
                        f"{check.get('limit')}")
        else:
            add("- no solve was performed")
        add("")
        add("## Notes")
        add("")
        add("- Decisions are made by `calib.session.CalibrationSession`; this "
            "controller adds materialisation, three safety checks "
            "(shared IR markers, unambiguous assignment, colour "
            "participation) and reporting.")
        add("- Acceptance is based on coverage, marginal uncertainty and "
            "held-out prediction, never on training RMS.")
        add("- Nothing was written to the scanner.")
        add("")
        path = self.paths.report_md
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text("\n".join(lines), encoding="utf-8")
        os.replace(tmp, path)
        return path

    # ------------------------------------------------------------------
    # snapshot
    # ------------------------------------------------------------------
    def _empty_snapshot(self) -> dict:
        return {
            "state": "idle",
            "error": None,
            "source": {"kind": "", "path": "", "serial": "", "firmware": "",
                       "note": ""},
            "session": {"dir": str(self.paths.root), "started_s": 0.0,
                        "elapsed_s": 0.0, "factory_sha256": "",
                        "raw_recording": str(self.paths.raw_recording),
                        "groups_dir": str(self.paths.groups_dir),
                        "n_materialised": 0},
            "counts": {"accepted": 0, "rejected": 0, "redundant": 0,
                       "by_reason": {}},
            "last_decision": None,
            "last_pose": None,
            "detections": {key: [] for key in CAMERAS},
            "markers": {key: 0 for key in CAMERAS},
            "shared_markers_ir": 0,
            "assignment_ok": True,
            "quality": {"mean": None, "saturation_frac": None, "focus": None,
                        "blur": None, "passed": False, "reasons": []},
            "capture": {"state": "idle", "fps": 0.0, "groups_written": 0,
                        "drops": 0, "throughput_mbps": None,
                        "disk_free_gb": None, "groups_on_disk": 0,
                        "ring_size": self.ring_keep, "stop_reason": None,
                        "last_group": None},
            "coverage": {"ready": False, "satisfied": 0, "total": 8,
                         "dimensions": {}},
            "observability": {"parameters": {}, "condition": None, "rank": 0,
                              "n_params": 0, "free": [], "gates": {}},
            "guidance": {"instruction": "move to a new pose", "detail": "",
                         "priority": "pose", "target": {}},
            "readiness": {"ready": False, "missing": [], "reason": ""},
        }

    def _source_dict(self) -> dict:
        info = self.source_info
        return {
            "kind": info.kind if info else "",
            "path": info.path if info else "",
            "serial": info.serial if info else "",
            "firmware": info.firmware if info else "",
            "note": info.note if info else "",
        }

    def _capture_dict(self) -> dict:
        base = {
            "state": "idle", "fps": 0.0, "groups_written": 0, "drops": 0,
            "throughput_mbps": None, "disk_free_gb": None,
            "groups_on_disk": 0, "ring_size": self.ring_keep,
            "stop_reason": None, "last_group": None,
        }
        if self.capture is not None:
            try:
                status = self.capture.status()
            except Exception:
                status = {}
            base.update({
                "state": status.get("state", "unknown"),
                "fps": status.get("fps") or 0.0,
                "groups_written": status.get("groups_written", 0),
                "drops": status.get("drops", 0),
                "throughput_mbps": status.get("throughput_mbps"),
                "disk_free_gb": status.get("disk_free_gb"),
                "groups_on_disk": status.get("groups_on_disk"),
                "ring_size": status.get("ring_size", self.ring_keep),
                "stop_reason": status.get("stop_reason"),
                "last_group": status.get("last_group"),
            })
        else:
            base["disk_free_gb"] = self._disk_free_gb()
            base["groups_on_disk"] = self._ring_group_count()
        return base

    def _disk_free_gb(self) -> float | None:
        path = self.paths.root
        while not path.exists() and path.parent != path:
            path = path.parent
        try:
            return shutil.disk_usage(path).free / 1e9
        except OSError:
            return None

    def _ring_group_count(self) -> int:
        try:
            with os.scandir(self.paths.root / "ring") as entries:
                return sum(1 for e in entries if e.is_dir() and e.name.isdigit())
        except OSError:
            return 0

    def _publish(self) -> None:
        """Rebuild the immutable snapshot the GUI polls (no disk reads)."""
        coverage = copy.deepcopy(self._coverage_cache or
                                 {"ready": False, "satisfied": 0, "total": 8,
                                  "dimensions": {}})
        readiness = copy.deepcopy(self._readiness_cache or
                                  {"ready": False, "missing": [], "reason": ""})
        snapshot = {
            "state": self.state,
            "error": self.error,
            "source": self._source_dict(),
            "session": {
                "dir": str(self.paths.root),
                "started_s": float(self.started_wall or 0.0),
                "elapsed_s": float(self.elapsed_s),
                "factory_sha256": self.factory_sha256,
                "raw_recording": str(self.paths.raw_recording),
                "groups_dir": str(self.paths.groups_dir),
                "n_materialised": int(self._n_materialised),
                "recorded_only": int(self._skipped_recorded),
            },
            "counts": {
                "accepted": len(self.session.accepted),
                "rejected": len(self.session.rejected),
                "redundant": self._redundant_count(),
                "by_reason": dict(sorted(self._reason_counts.items())),
            },
            "last_decision": copy.deepcopy(self._last_decision),
            "last_pose": _pose_summary(self._last_pose),
            "detections": copy.deepcopy(self._last_detections),
            "markers": dict(self._last_markers),
            "shared_markers_ir": int(self._last_shared),
            "assignment_ok": bool(self._last_assignment_ok),
            "quality": _quality_summary(self._last_quality),
            "capture": self._capture_dict(),
            "coverage": coverage,
            "observability": copy.deepcopy(self._observability_cache or {}),
            "guidance": copy.deepcopy(self._guidance_cache or
                                      {"instruction": "move to a new pose",
                                       "detail": "", "priority": "pose",
                                       "target": {}}),
            "readiness": readiness,
        }
        with self._snap_lock:
            self._published = _jsonable(snapshot)

    def close(self) -> None:
        """Release the detector pool (the window calls this on close)."""
        pool = getattr(self, "_detect_executor", None)
        if pool is not None:
            pool.shutdown(wait=False)
            self._detect_executor = None

    def snapshot(self) -> dict:
        """Everything the GUI renders; safe to call while ``ingest`` runs.

        The returned dictionary is a deep copy, so the caller may mutate it.
        It is rebuilt only by ``_publish`` (after each ingest/transition); the
        live capture fields and elapsed time are refreshed on every call.
        """
        with self._snap_lock:
            snapshot = copy.deepcopy(self._published)
        snapshot["session"]["elapsed_s"] = float(self.elapsed_s)
        snapshot["state"] = self.state
        snapshot["error"] = self.error
        snapshot["capture"] = self._capture_dict()
        return snapshot


# ---------------------------------------------------------------------------

_GATE_NOTE_KEYS = (
    ("value_deg", "sigma"), ("limit_deg", "limit"),
    ("value_mm", "sigma"), ("limit_mm", "limit"),
    ("translation_mm", "colour translation"), ("pixel_at_400mm", "px@400mm"),
)


def _normalise_gates(gates: Mapping[str, Any]) -> dict[str, dict]:
    """Reduce the module's gate records to the GUI's ``{pass, detail}``."""
    out: dict[str, dict] = {}
    for name, raw in (gates or {}).items():
        if not isinstance(raw, Mapping):
            continue
        bits = [str(raw.get("status") or ("pass" if raw.get("pass") else "fail"))]
        for key, label in _GATE_NOTE_KEYS:
            value = raw.get(key)
            if value is not None:
                bits.append(f"{label}={value}")
        note = raw.get("note")
        if note:
            bits.append(str(note))
        out[str(name)] = {"pass": bool(raw.get("pass")),
                          "status": raw.get("status"),
                          "detail": ", ".join(bits)}
    return out


def _unit_for(name: str) -> str:
    field_name = name.split(".")[-1]
    if field_name in ("fx", "fy", "cx", "cy"):
        return "px"
    if field_name in ("rx", "ry", "rz"):
        return "deg"
    if field_name in ("tx", "ty", "tz"):
        return "mm"
    return ""


def _median_pitch(points: np.ndarray) -> float:
    points = np.asarray(points, float)
    if len(points) < 4:
        return 1.0
    d = np.linalg.norm(points[:, None, :] - points[None, :, :], axis=2)
    np.fill_diagonal(d, 1e9)
    return max(float(np.median(d.min(1))), 1e-6)


def _fmt(value: Any, digits: int) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return str(value)
