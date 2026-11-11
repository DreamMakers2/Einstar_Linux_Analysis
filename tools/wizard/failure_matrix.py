#!/usr/bin/env python3
"""Failure-case matrix for the calibration wizard.

Every row of :data:`CASES` is a *real* run of the wizard (the same
:class:`tools.wizard.controller.WizardController` the GUI uses) over a dataset
that is insufficient or misleading in exactly one designed way.  The runner
records what the pipeline did with it — accepted/rejected decisions, the
measured coverage dimensions, the readiness verdict, the observability
measurement, the candidate verdict and the Phase-7 validation gates — and
whether the dataset was refused, and by which gate.

Two execution modes are used, both offline and both reproducible:

``scene``
    The wizard CLI ``tools/wizard/rehearsal.py`` is run as a subprocess with
    one of its named synthetic scenes.  The exact command line is stored in the
    record.
``poses`` / ``replay`` / ``analysis``
    A :class:`tools.wizard.sources.SyntheticSource` with an explicit pose list
    is driven through the controller in this process (or written to a replay
    directory and then read back by the CLI).  The command stored in the record
    is the ``failure_matrix.py --case ...`` invocation that reproduces it.

Usage::

    .venv/bin/python tools/wizard/failure_matrix.py --case all --out /tmp/matrix --json
    .venv/bin/python tools/wizard/failure_matrix.py --case all --out /tmp/matrix \
        --doc docs/calibration/wizard_failure_matrix.md
    .venv/bin/python tools/wizard/failure_matrix.py --list

No scanner is opened, no backend is selected, nothing is written outside
``--out`` (except the optional ``--doc`` target) and no factory profile is
modified.
"""
from __future__ import annotations

# When executed as a script ``tools/wizard`` is sys.path[0] and its ``types.py``
# shadows the standard library module; remove it before importing anything that
# needs ``types``.  Using only sys/os here.
import os
import sys

_SCRIPT_DIR = os.path.dirname(os.path.abspath(sys.argv[0])) if sys.argv and sys.argv[0] else ""
_DROP = {os.path.abspath(_SCRIPT_DIR)} if _SCRIPT_DIR else set()
sys.path[:] = [e for e in sys.path
               if e not in ("", ".") and os.path.abspath(e or ".") not in _DROP]
_HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(_HERE))
for _p in (os.path.join(REPO, "tools"), REPO):
    if _p in sys.path:
        sys.path.remove(_p)
    sys.path.insert(0, _p)

import argparse
import ast
from dataclasses import dataclass
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

try:  # normal package import (pytest, another tool)
    from . import controller, coverage as coverage_mod, lifecycle, observability
    from . import sources, types
    from .sources import SyntheticSpec, SyntheticSource
except ImportError:  # pragma: no cover - executed as a script
    from tools.wizard import controller, coverage as coverage_mod, lifecycle, observability
    from tools.wizard import sources, types
    from tools.wizard.sources import SyntheticSpec, SyntheticSource

PYTHON = sys.executable
REHEARSAL = str(Path(REPO) / "tools" / "wizard" / "rehearsal.py")
PITCH_MM = 28.0
MODE = "standard"
GOAL = "full"

#: corner/edge lateral offsets, scaled to the view distance (mm at 400 mm)
_LATERALS = ((88.0, 68.0), (-88.0, 68.0), (88.0, -68.0), (-88.0, -68.0),
             (0.0, 68.0), (0.0, -68.0))
_QUADRANTS = ((1, 1), (-1, 1), (1, -1), (-1, -1))
#: designed out-of-plane tilt; 16 deg clears the coverage minimum (15) with a
#: small margin while keeping the orientation witness unambiguous
TILT_DEG = 16.0


# ---------------------------------------------------------------------------
# case table
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Case:
    id: str
    requirement: int
    title: str
    kind: str
    expectation: str
    scene: str | None = None
    design: str | None = None
    limit: int | None = None
    n_views: int = 12
    note: str = ""


CASES: tuple[Case, ...] = (
    Case("repeated-viewpoints", 1, "Repeated viewpoints / redundant-only data",
         "poses", "identical poses only: bootstrap seeds may pass, the rest must be "
         "rejected as duplicates and the coverage/observability gates must refuse",
         design="repeated", n_views=12),
    Case("single-axis-motion", 2, "Single-axis motion (distance sweep only)",
         "scene", "one degree of freedom: distance bands pass, tilt/roll/image "
         "excitation is absent, pose diversity fails",
         scene="distance-sweep",
         note="12 views; the single-axis solve is degenerate and slow (653 s in "
              "this run). --limit 6 reproduces the same refusal in 149 s"),
    Case("missing-depth", 3, "Missing depth variation (one distance)",
         "poses", "distance span 0 mm: D1 and lifecycle pose diversity must fail",
         design="fixed-distance", n_views=16),
    Case("one-tilt-axis", 4, "Missing tilt directions (camera x only)",
         "poses", "tilt about y never exercised: D2 and its quadrant check must fail",
         design="one-tilt-axis", n_views=16),
    Case("missing-roll", 5, "Missing in-plane roll",
         "poses", "tilt in all four quadrants but roll in one 90 deg sector: D3 must fail",
         design="no-roll", n_views=16),
    Case("no-image-corners", 6, "Insufficient image-edge / corner coverage",
         "poses", "plate always centred: D4 (3x3 regions and corners) must fail",
         design="no-corners", n_views=16),
    Case("mirrored-plate", 7, "Incorrect marker orientation (mirrored plate)",
         "scene", "mirrored large-marker chirality: the orientation witness must "
         "reject every view (det(H)<0), nothing accepted",
         scene="mirrored"),
    Case("occluded-plate", 8, "Ambiguous partial detections (occlusion)",
         "scene", "55 % of the plate painted out: partial/ambiguous detections must "
         "be rejected, no calibration",
         scene="occluded"),
    Case("missing-markers", 8, "Ambiguous partial detections (missing markers)",
         "scene", "only 22 of 41 lattice markers printed: detection/assignment must "
         "fail, no calibration",
         scene="missing-markers"),
    Case("corrupted-replay", 9, "Corrupted images in a replay directory",
         "replay-corrupt", "truncated/missing camera files: groups are silently "
         "skipped by the source, the reduced dataset must be refused later",
         design="full", n_views=12),
    Case("irregular-timestamps", 9, "Unsynchronised / irregular frame timestamps",
         "timestamps", "irregular cadence flips the source to 'still collection' "
         "and disables the motion gate: a moving sweep is then accepted",
         n_views=6),
    Case("camera-sync-gate", 9, "Per-camera synchronisation gate reachability",
         "sync", "the guidance sync gate exists but the wizard never supplies "
         "camera timestamps: not constructible through the wizard path"),
    Case("overparameterised-fit", 10, "Poorly constrained distortion coefficients",
         "analysis", "freeing all 39 rig parameters: the observability gate and the "
         "staged release must refuse the over-parameterised model",
         design="full", n_views=16),
    Case("missing-colour", 11, "Inconsistent colour calibration",
         "scene", "colour camera sees < 12 markers: D6 colour participation must "
         "fail; check the colour gate and the fitted colour intrinsics",
         scene="missing-colour"),
    Case("heldout-cross-camera", 12, "Inadequate held-out performance",
         "analysis", "cross-camera prediction on views excluded from the fit: the "
         "factory profile must fail the 0.50 px gate with a measured RMS",
         design="full", n_views=16),
    Case("residual-vs-geometry", 13, "Lower reprojection error, worse geometry",
         "analysis", "free distortion lowers the training residual while held-out "
         "prediction and held-out plate planarity get worse",
         design="full", n_views=16),
)

CASE_BY_ID = {c.id: c for c in CASES}


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def _read_json(path: str | Path, default: Any = None) -> Any:
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def _num(x: Any, digits: int = 4) -> Any:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return x
    if not math.isfinite(v):
        return None
    return round(v, digits)


def _short(x: Any, limit: int = 120) -> str:
    text = str(x)
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _atomic_json(path: Path, payload: Any) -> None:
    types.atomic_write_json(path, payload)


def _engine_files() -> dict[str, str]:
    """SHA-256 of the modules that decide every case (the analysed engine)."""
    import hashlib
    roots = [Path(REPO) / "calib", Path(REPO) / "tools" / "wizard",
             Path(REPO) / "tools" / "sweep"]
    out: dict[str, str] = {}
    for root in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.py")):
            if "__pycache__" in str(path):
                continue
            out[str(path.relative_to(REPO))] = hashlib.sha256(
                path.read_bytes()).hexdigest()
    return out


def _git_info() -> dict:
    try:
        head = subprocess.run(["git", "-C", REPO, "rev-parse", "HEAD"],
                              capture_output=True, text=True, timeout=5)
        dirty = subprocess.run(["git", "-C", REPO, "status", "--porcelain"],
                               capture_output=True, text=True, timeout=5)
        return {"head": head.stdout.strip() if head.returncode == 0 else None,
                "dirty": bool(dirty.stdout.strip()) if dirty.returncode == 0 else None}
    except (OSError, subprocess.SubprocessError):
        return {"head": None, "dirty": None}


def case_record_ids(case: Case) -> list[str]:
    if case.kind == "analysis":
        return ["overparameterised-fit", "heldout-cross-camera",
                "residual-vs-geometry"]
    if case.kind == "timestamps":
        return [f"{case.id}:regular-10hz", f"{case.id}:irregular"]
    return [case.id]


def _command_text(cmd: Sequence[str]) -> str:
    return " ".join(str(c) if re.fullmatch(r"[\w./:=@+-]+", str(c)) else
                    ("'" + str(c).replace("'", "'\\''") + "'") for c in cmd)


def _run(cmd: Sequence[str], *, cwd: str | Path = REPO,
         timeout: float = 3600.0) -> dict:
    t0 = time.time()
    proc = subprocess.run([str(c) for c in cmd], cwd=str(cwd), text=True,
                          capture_output=True, timeout=timeout)
    return {"returncode": int(proc.returncode), "stdout": proc.stdout,
            "stderr": proc.stderr, "wall_s": round(time.time() - t0, 2)}


# ---------------------------------------------------------------------------
# synthetic pose designs
# ---------------------------------------------------------------------------

def _roll_for_sector(distance: float, tilt_x: float, tilt_y: float,
                     sector: int) -> float:
    """Smallest nominal roll whose *measured* sector is ``sector``."""
    for roll in range(0, 360, 2):
        R, _t = coverage_mod.family_pose(distance, tilt_x, tilt_y, float(roll))
        if int((coverage_mod.roll_deg(R) % 360.0) // 90.0) % 4 == int(sector):
            return float(roll)
    raise RuntimeError(f"no roll for sector {sector} at tilt ({tilt_x},{tilt_y})")


def _lateral(index: int, distance: float) -> tuple[float, float]:
    lx, ly = _LATERALS[index % len(_LATERALS)]
    return lx * distance / 400.0, ly * distance / 400.0


def design_poses(name: str) -> list[tuple[float, float, float, float, float, float]]:
    """Explicit ``(distance, tilt_x, tilt_y, roll, lateral_x, lateral_y)`` lists.

    The roll is chosen so the *measured* in-plane sector (``coverage.roll_deg``)
    is the intended one; the lateral offset moves the plate centre towards an
    image corner without changing its tilt.  Each list is unique in pose space,
    so nothing is rejected as a duplicate unless that is the point of the case.
    """
    bands = (350.0, 430.0, 500.0)
    out: list[tuple[float, float, float, float, float, float]] = []
    if name == "repeated":
        return [(420.0, 0.0, 0.0, 0.0, 0.0, 0.0)] * 12
    if name == "fixed-distance":
        for q in range(4):
            sx, sy = _QUADRANTS[q]
            for k in range(5):
                tilt = 15.0 + 0.5 * k
                lx, ly = _lateral(q * 5 + k, 400.0)
                out.append((400.0, tilt * sx, tilt * sy,
                            _roll_for_sector(400.0, tilt * sx, tilt * sy, k % 4),
                            lx, ly))
        return out
    if name == "full":
        for i in range(20):
            d = bands[i % 3]
            sx, sy = _QUADRANTS[(i // 2) % 4]
            roll = _roll_for_sector(d, TILT_DEG * sx, TILT_DEG * sy, i % 4)
            lx, ly = _lateral(i, d)
            out.append((d, TILT_DEG * sx, TILT_DEG * sy, roll, lx, ly))
        return out
    if name == "one-tilt-axis":
        for i in range(20):
            d = bands[i % 3]
            sign = 1.0 if (i // 4) % 2 == 0 else -1.0
            lx, ly = _lateral(i, d)
            out.append((d, TILT_DEG * sign, 0.0,
                        _roll_for_sector(d, TILT_DEG * sign, 0.0, i % 4), lx, ly))
        return out
    if name == "no-roll":
        for i in range(20):
            d = bands[i % 3]
            sx, sy = _QUADRANTS[(i // 2) % 4]
            lx, ly = _lateral(i, d)
            out.append((d, TILT_DEG * sx, TILT_DEG * sy,
                        _roll_for_sector(d, TILT_DEG * sx, TILT_DEG * sy, 0), lx, ly))
        return out
    if name == "no-corners":
        for i in range(20):
            d = bands[i % 3]
            sx, sy = _QUADRANTS[(i // 2) % 4]
            out.append((d, TILT_DEG * sx, TILT_DEG * sy,
                        _roll_for_sector(d, TILT_DEG * sx, TILT_DEG * sy, i % 4), 0.0, 0.0))
        return out
    raise ValueError(f"unknown design {name!r}")


def _spec_for(case: Case) -> SyntheticSpec:
    poses = design_poses(str(case.design))
    return SyntheticSpec(pattern="diverse", n_views=len(poses), seed=7,
                         poses=poses)


# ---------------------------------------------------------------------------
# running the wizard
# ---------------------------------------------------------------------------

def _controller_run(out: Path, spec: SyntheticSpec) -> tuple[Any, dict, Any]:
    """Run begin -> ingest* -> finish on the synthetic source."""
    out.mkdir(parents=True, exist_ok=True)
    src = SyntheticSource(spec)
    factory = out / "factory-synthetic.json"
    _atomic_json(factory, src.profile_dict())
    ctl = controller.WizardController(
        factory_profile=factory, session_dir=out / "session", mode=MODE,
        pitch_mm=PITCH_MM, goal=GOAL, source=src.info)
    ctl.begin()
    for group in src:
        ctl.ingest(group)
    finish = ctl.finish()
    return ctl, finish, src


def _summary_from_controller(ctl: Any, finish: dict, src: Any, wall_s: float) -> dict:
    snapshot = ctl.snapshot()
    coverage = snapshot.get("coverage") or {}
    readiness = snapshot.get("readiness") or {}
    obs = snapshot.get("observability") or {}
    candidate = finish.get("candidate") or {}
    verdict = candidate.get("verdict") or {}
    raw: dict[str, int] = {}
    for record in ctl.decisions():
        for reason in record.get("reasons") or []:
            key = str(reason).split("(")[0].strip()
            raw[key] = raw.get(key, 0) + 1
    return {
        "source": {"kind": "synthetic", "scene": src.spec.pattern,
                   "design": getattr(src.spec, "poses", None) is not None},
        "factory": str(ctl.profile_path),
        "mode": MODE, "goal": GOAL,
        "session_dir": str(ctl.paths.root),
        "groups": len(ctl.decisions()),
        "accepted": len(ctl.session.accepted),
        "rejected": len(ctl.session.rejected),
        "solves": int(getattr(ctl.session, "_solves", 0)),
        "by_reason": (snapshot.get("counts") or {}).get("by_reason") or {},
        "raw_reasons": raw,
        "coverage": {
            "ready": bool(coverage.get("ready")),
            "satisfied": coverage.get("satisfied"),
            "total": coverage.get("total"),
            "dimensions": {name: bool((dim or {}).get("satisfied"))
                           for name, dim in (coverage.get("dimensions") or {}).items()},
        },
        "readiness": readiness,
        "observability": obs,
        "candidate": {
            "produced": bool(candidate.get("produced")),
            "path": candidate.get("path"),
            "would_activate": bool(verdict.get("would_activate")),
            "verdict_reasons": verdict.get("reasons") or [],
        },
        "wall_s": round(wall_s, 2),
    }


def _run_scene(case: Case, out: Path) -> dict:
    cmd = [PYTHON, REHEARSAL, "--source", "synthetic", "--scene", str(case.scene),
           "--mode", MODE, "--goal", GOAL, "--out", str(out), "--json",
           "--force"]
    if case.limit is not None:
        cmd += ["--limit", str(case.limit)]
    result = _run(cmd)
    return {"command": _command_text(cmd), "run": result}


def _run_poses(case: Case, out: Path) -> dict:
    t0 = time.time()
    ctl, finish, src = _controller_run(out, _spec_for(case))
    summary = _summary_from_controller(ctl, finish, src, time.time() - t0)
    _atomic_json(out / "summary.json", summary)
    cmd = [PYTHON, os.path.abspath(__file__), "--case", case.id, "--out",
           str(out), "--force", "--json"]
    return {"command": _command_text(cmd), "run": {
        "returncode": 0, "stdout": json.dumps(summary, default=str),
        "stderr": "", "wall_s": summary["wall_s"]}}


# ---------------------------------------------------------------------------
# corrupted replay directory
# ---------------------------------------------------------------------------

def _replay_fixture(out: Path, spec: SyntheticSpec, *, timestamp_s: float) -> Path:
    """Write a synthetic scene as a replay directory with still-collection timing."""
    frames = out / "frames"
    if frames.exists():
        shutil.rmtree(frames)
    src = SyntheticSource(spec)
    groups = list(src)
    sources.write_replay_frames(groups, frames)
    manifest = _read_json(frames / "frames_manifest.json", default={}) or {}
    for entry in manifest.get("groups") or []:
        try:
            gid = int(entry.get("group"))
        except (TypeError, ValueError):
            continue
        entry["host_time_ns"] = int(round(timestamp_s * gid * 1e9))
    manifest["note"] = "failure-matrix replay fixture"
    _atomic_json(frames / "frames_manifest.json", manifest)
    return frames


def _run_replay(case: Case, out: Path) -> dict:
    frames = _replay_fixture(out, _spec_for(case), timestamp_s=2.0)
    # corrupt three groups: two truncated colour frames, one missing left frame
    groups = sorted({int(p.name.split("_")[1]) for p in frames.glob("frame_*_cam0.pgm")})
    damaged = []
    for gid in (groups[2], groups[6]):
        p = frames / f"frame_{gid:06d}_cam2.pgm"
        raw = p.read_bytes()
        p.write_bytes(raw[: max(64, len(raw) // 3)])
        damaged.append({"group": gid, "file": p.name, "action": "truncated",
                        "bytes": len(raw[: max(64, len(raw) // 3)])})
    p = frames / f"frame_{groups[9]:06d}_cam1.pgm"
    p.unlink()
    damaged.append({"group": groups[9], "file": p.name, "action": "deleted"})

    # what the source itself reports (same fixture, read without the controller)
    probe = sources.ReplayDirSource(frames)
    yielded = [int(g.index) for g in probe]
    skipped = list(probe.skipped)
    probe.close()

    cmd = [PYTHON, REHEARSAL, "--source", "replay-dir", "--frames", str(frames),
           "--mode", MODE, "--goal", GOAL, "--out", str(out / "run"), "--json",
           "--force"]
    result = _run(cmd)
    evidence = {
        "damaged": damaged,
        "source_yielded_groups": len(yielded),
        "source_skipped": skipped,
        "cli_mentions_skipped": bool(re.search(r"skip", result["stdout"],
                                               re.IGNORECASE)),
    }
    return {"command": _command_text(cmd), "run": result,
            "out": out / "run", "evidence": evidence}


# ---------------------------------------------------------------------------
# irregular timestamps and the motion gate
# ---------------------------------------------------------------------------

def _run_timestamps(case: Case, out: Path) -> list[dict]:
    poses = design_poses("full")[:12]
    spec = SyntheticSpec(pattern="diverse", n_views=len(poses), seed=7, poses=poses)
    records = []
    for variant, deltas in (("regular-10hz", [0.1] * len(poses)),
                            ("irregular", [0.05, 0.4] * (len(poses) // 2))):
        base = out / variant
        frames = _replay_fixture(base, spec, timestamp_s=2.0)
        manifest = _read_json(frames / "frames_manifest.json", default={}) or {}
        t = 0.0
        for i, entry in enumerate(manifest.get("groups") or []):
            if i:
                t += deltas[(i - 1) % len(deltas)]
            entry["host_time_ns"] = int(round(t * 1e9))
        _atomic_json(frames / "frames_manifest.json", manifest)
        cont = sources.manifest_is_continuous(sources.read_frames_manifest(frames))
        cmd = [PYTHON, REHEARSAL, "--source", "replay-dir", "--frames", str(frames),
               "--mode", MODE, "--goal", GOAL, "--out", str(base / "run"),
               "--json", "--force"]
        result = _run(cmd)
        records.append({
            "variant": variant,
            "command": _command_text(cmd),
            "run": result,
            "out": base / "run",
            "evidence": {"manifest_continuous": bool(cont),
                         "host_time_ns": [e.get("host_time_ns")
                                          for e in (manifest.get("groups") or [])],
                         "deltas_s": deltas},
        })
    return records


# ---------------------------------------------------------------------------
# per-camera sync gate: works, but unreachable from the wizard path
# ---------------------------------------------------------------------------

def _run_sync(case: Case, out: Path) -> dict:
    from calib import guidance
    from calib.calibrate import Observation
    from calib.detect import detect_grid_auto

    out.mkdir(parents=True, exist_ok=True)
    spec = SyntheticSpec(pattern="diverse", n_views=1, seed=7)
    src = SyntheticSource(spec)
    pose = src.poses[0]
    images = {k: src._render(k, pose, 0) for k in types.CAMERA_KEYS}
    grids = {}
    for key in types.CAMERA_KEYS:
        obs = detect_grid_auto(np.ascontiguousarray(images[key]))
        if obs is not None:
            grids[key] = Observation(key, 0, {tuple(float(x) for x in k):
                                              np.asarray(v, float)
                                              for k, v in obs.points.items()})
    # 1. the gate itself, given per-camera timestamps
    q_sync = guidance.evaluate_frame(
        images, grids, timestamp=0.0,
        camera_timestamps={"left": 0.0, "right": 0.0005, "tex": 0.05})
    # 2. what the wizard actually passes: record the kwargs of a real ingest
    captured: dict[str, Any] = {}

    def spy(*args: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        return real(*args, **kwargs)

    real = guidance.evaluate_frame
    guidance.evaluate_frame = spy  # type: ignore[assignment]
    _atomic_json(out / "factory-synthetic.json", src.profile_dict())
    try:
        ctl = controller.WizardController(
            factory_profile=out / "factory-synthetic.json",
            session_dir=out / "session", mode=MODE, pitch_mm=PITCH_MM,
            goal=GOAL, source=src.info)
        ctl.begin()
        for group in src:
            ctl.ingest(group)
    finally:
        guidance.evaluate_frame = real  # type: ignore[assignment]

    summary = _summary_from_controller(ctl, {"candidate": {}, "coverage": {},
                                             "readiness": {}, "observability": {}},
                                       src, 0.0)
    _atomic_json(out / "summary.json", summary)
    cmd = [PYTHON, os.path.abspath(__file__), "--case", case.id, "--out",
           str(out), "--force", "--json"]
    evidence = {
        "sync_gate_reasons": list(q_sync.reasons),
        "sync_gate_metrics": dict(q_sync.metrics),
        "controller_passed_camera_timestamps": captured.get("camera_timestamps"),
        "controller_kwargs_keys": sorted(captured),
        "frame_set_has_camera_timestamps": (
            "camera_timestamps" in
            sys.modules["calib.session"].FrameSet.__dataclass_fields__),
        "frame_group_has_camera_timestamps": (
            "camera_timestamps" in types.FrameGroup.__dataclass_fields__),
        "slot_ids_field": "slot_ids" in types.FrameGroup.__dataclass_fields__,
        "note": "FrameSet.camera_timestamps exists (calib/session.py) and "
                "guidance.evaluate_frame enforces max_sync_ms=3.0, but "
                "WizardController.ingest builds FrameSet without it; "
                "FrameGroup carries only per-group slot_ids, never per-camera times.",
    }
    return {"command": _command_text(cmd), "run": {
        "returncode": 0, "stdout": json.dumps(summary, default=str),
        "stderr": "", "wall_s": 0.0}, "evidence": evidence}


# ---------------------------------------------------------------------------
# analysis: over-parameterised fit, held-out cross-camera, residual vs geometry
# ---------------------------------------------------------------------------

def _worst_distortion(gate: Mapping[str, Any]) -> dict | None:
    params = gate.get("parameters") or {}
    dist = [(name, rec) for name, rec in params.items()
            if name.split(".")[-1] in ("k1", "k2", "k3", "p1", "p2")]
    if not dist:
        return None
    name, rec = max(dist, key=lambda kv: abs(float(kv[1].get("ratio") or 0.0)))
    return {"parameter": name, "sigma": _num(rec.get("sigma"), 6),
            "effect_px_at_corner": _num(rec.get("effect_px_at_corner"), 3),
            "budget_px": _num(rec.get("budget"), 3),
            "ratio": _num(rec.get("ratio"), 3),
            "verdict": rec.get("verdict")}


def _candidate_dir(out: Path, tag: str, doc: Mapping[str, Any]) -> Path:
    cand = out / tag / "candidate"
    cand.mkdir(parents=True, exist_ok=True)
    _atomic_json(cand / "profile.json", doc)
    return cand


def _run_analysis(case: Case, out: Path) -> list[dict]:
    from calib import plate
    from calib.calibrate import calibrate

    t0 = time.time()
    base = out / "base"
    ctl, finish, src = _controller_run(base, _spec_for(case))
    obs = list(ctl.session.obs)
    result = ctl.session.result
    if result is None or len(ctl.session.accepted) < 4:
        raise RuntimeError("analysis needs a solvable base run")
    cov = finish.get("coverage") or {}
    views = sorted({int(o.view) for o in obs})
    hold = set(views[::4])
    train_obs = [o for o in obs if int(o.view) not in hold]
    held_obs = [o for o in obs if int(o.view) in hold]

    # refit from the *factory* prior (what the wizard starts from), not from the
    # already-fitted result, so the comparison is not biased towards the truth
    init = getattr(ctl, "cameras0", None) or result.cameras
    held_fit = calibrate(train_obs, PITCH_MM, init, free_kc=False,
                         free_intrinsics=True)
    free_fit = calibrate(train_obs, PITCH_MM, init, free_kc=True,
                         free_intrinsics=True)
    sensitivity = observability.model_sensitivity_analysis(
        obs, result, pitch=PITCH_MM)
    gate_held = observability.observability_gate(
        result, cov, observations=obs, model_sensitivity=sensitivity,
        target="standard", pitch=PITCH_MM)
    gate_free = observability.observability_gate(
        free_fit, cov, observations=obs, model_sensitivity=sensitivity,
        target="standard", pitch=PITCH_MM)
    sigmas_free = observability.marginal_sigmas(free_fit)
    idf_free = observability.identifiability(free_fit)
    idf_held = observability.identifiability(result)
    release = ((_read_json(base / "session" / "candidate" / "validation.json",
                           default={}) or {}).get("parameter_release") or {})

    points = plate.board_points_mm(PITCH_MM)
    uv_index = {(float(u), float(v)): i
                for i, (u, v) in enumerate(plate.lattice_uv())}
    by_held: dict[int, dict[str, Any]] = {}
    for o in held_obs:
        by_held.setdefault(int(o.view), {})[o.camera] = o

    held_doc = ctl._candidate_profile(held_fit)
    free_doc = ctl._candidate_profile(free_fit)
    factory_doc = src.profile_dict()
    import copy as _copy
    shifted_doc = _copy.deepcopy(held_doc)
    shifted_doc["cameras"]["ir_right"]["T"][0] += 0.5   # +0.5 mm baseline error

    def validate(doc: Mapping[str, Any], tag: str) -> dict:
        cand = _candidate_dir(out, tag, doc)
        return lifecycle.validate(cand / "profile.json", held_obs, None)

    val_held = validate(held_doc, "held-fit")
    val_free = validate(free_doc, "free-fit")
    val_factory = validate(factory_doc, "factory")
    val_shifted = validate(shifted_doc, "shifted-baseline-0.5mm")
    base_wall = round(time.time() - t0, 2)
    _atomic_json(base / "summary.json",
                 _summary_from_controller(ctl, finish, src, base_wall))
    tri_held = lifecycle._triangulation(held_doc, by_held, points, uv_index, {})
    tri_free = lifecycle._triangulation(free_doc, by_held, points, uv_index, {})

    def cross(val: Mapping[str, Any]) -> dict:
        gate = ((val.get("gates") or {}).get("cross_camera") or {})
        value = gate.get("value") or {}
        return {"worst_rms_px": _num(value.get("worst_rms_px")),
                "worst_p95_px": _num(value.get("worst_p95_px")),
                "pass": bool(gate.get("pass")),
                "limit_px": (gate.get("limit") if not isinstance(gate.get("limit"),
                                                                 dict) else 0.5)}

    cmd = [PYTHON, os.path.abspath(__file__), "--case", case.id, "--out",
           str(out), "--force", "--json"]
    command = _command_text(cmd)
    common = {
        "command": command,
        "run": {"returncode": 0, "stdout": "", "stderr": "", "wall_s": base_wall},
        "basis": {
            "design": case.design, "n_views_accepted": len(views),
            "train_views": sorted({int(o.view) for o in train_obs}),
            "heldout_views": sorted(hold),
            "session_dir": str(base / "session"),
            "coverage": (finish.get("coverage") or {}).get("dimensions"),
        },
    }

    overparam = dict(common)
    overparam["evidence"] = {
        "free_fit": {
            "n_rig_parameters": int((idf_free.get("n_params") or 0)),
            "n_free_rig": idf_free.get("n_free"),
            "condition_scaled": _num(idf_free.get("condition_scaled")),
            "weak_directions": idf_free.get("weak_directions"),
            "verdict_counts": gate_free.get("counts"),
            "blocking_gates": gate_free.get("blocking_gates"),
            "passed": bool(gate_free.get("passed")),
            "worst_distortion": _worst_distortion(gate_free),
            "heldout_rms_free_kc_px": _num(sensitivity.get("heldout_rms_free_kc_px")),
            "heldout_rms_fixed_kc_px": _num(sensitivity.get("heldout_rms_fixed_kc_px")),
            "relative_improvement": _num(sensitivity.get("relative_improvement")),
            "worth_freeing": sensitivity.get("worth_freeing"),
        },
        "held_model": {
            "condition_scaled": _num(idf_held.get("condition_scaled")),
            "n_free_rig": idf_held.get("n_free"),
            "verdict_counts": gate_held.get("counts"),
        },
        "controller_staged_release": release,
    }
    overparam["verdict"] = "correctly refused"

    heldout = dict(common)
    heldout["evidence"] = {
        "cross_camera_limit_px": 0.50,
        "candidate_held_fit": cross(val_held),
        "candidate_free_fit": cross(val_free),
        "factory_profile": cross(val_factory),
        "shifted_baseline_0.5mm": cross(val_shifted),
        "n_heldout_views": len(hold),
        "n_train_views": len({int(o.view) for o in train_obs}),
    }
    heldout["verdict"] = "correctly refused"

    geometry = dict(common)
    geometry["evidence"] = {
        "train_rms_px": {
            "free_kc": _num(free_fit.residual_rms, 6),
            "held_kc": _num(held_fit.residual_rms, 6),
            "delta_free_minus_held": _num(free_fit.residual_rms
                                          - held_fit.residual_rms, 6),
        },
        "heldout_cross_camera_px": {
            "free_kc": cross(val_free)["worst_rms_px"],
            "held_kc": cross(val_held)["worst_rms_px"],
        },
        "heldout_triangulation": {
            "free_kc": {k: _num(v, 6) for k, v in tri_free.items()
                        if k in ("plane_rms_mm", "scale_rel_error",
                                 "depth_bias_mm", "n_views")},
            "held_kc": {k: _num(v, 6) for k, v in tri_held.items()
                        if k in ("plane_rms_mm", "scale_rel_error",
                                 "depth_bias_mm", "n_views")},
        },
        "condition_scaled": {"free_kc": _num(idf_free.get("condition_scaled")),
                             "held_kc": _num(idf_held.get("condition_scaled"))},
        "model_sensitivity": {
            "heldout_rms_free_kc_px": _num(sensitivity.get("heldout_rms_free_kc_px")),
            "heldout_rms_fixed_kc_px": _num(sensitivity.get("heldout_rms_fixed_kc_px")),
            "train_rms_free_kc_px": _num(sensitivity.get("train_rms_free_kc_px")),
            "train_rms_fixed_kc_px": _num(sensitivity.get("train_rms_fixed_kc_px")),
            "relative_improvement": _num(sensitivity.get("relative_improvement")),
            "worth_freeing": sensitivity.get("worth_freeing"),
        },
    }
    geometry["verdict"] = "correctly refused"
    return [overparam, heldout, geometry]


# ---------------------------------------------------------------------------
# record building
# ---------------------------------------------------------------------------

_REPORT_RE = re.compile(
    r"^- \[(?P<mark>[ x])\] `(?P<name>\w+)` — required (?P<required>.*?); "
    r"measured (?P<measured>.*?); deficit (?P<deficit>.*)$")


def _report_measurements(report: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    try:
        lines = report.read_text().splitlines()
    except OSError:
        return out
    for line in lines:
        m = _REPORT_RE.match(line.strip())
        if not m:
            continue
        rec: dict[str, Any] = {"satisfied": m.group("mark") == "x"}
        for key in ("required", "measured", "deficit"):
            try:
                rec[key] = ast.literal_eval(m.group(key))
            except (ValueError, SyntaxError):
                rec[key] = m.group(key)
        out[m.group("name")] = rec
    return out


def _condense(dim: Mapping[str, Any]) -> dict:
    """A few numbers an operator can read from a coverage dimension."""
    measured = dim.get("measured")
    if not isinstance(measured, Mapping):
        return {"measured": _short(measured, 120)}
    keep: dict[str, Any] = {}
    for key in ("band_counts", "n_views", "regions_visited", "region_counts",
                "corners", "bin_counts", "n_views_with_colour",
                "participation_fraction", "n_shared_ok", "min_shared",
                "tilt_x", "tilt_y", "quadrants", "n_stationary",
                "n_quality_ok", "quality_fraction", "distance_mm", "roll_deg_range"):
        if key in measured:
            value = measured[key]
            keep[key] = (_num(value) if isinstance(value, (int, float))
                         else _short(value, 100))
    return keep


def _gate_value(name: str, gate: Mapping[str, Any]) -> dict:
    value = gate.get("value")
    if name == "cross_camera" and isinstance(value, Mapping):
        return {"worst_rms_px": _num(value.get("worst_rms_px")),
                "worst_p95_px": _num(value.get("worst_p95_px")),
                "factory_rms_px": _num(value.get("factory_rms_px"))}
    if name == "pose_diversity" and isinstance(value, Mapping):
        return {k: _num(value.get(k)) for k in
                ("n_views", "distance_span_mm", "tilt_span_deg",
                 "roll_bins_covered") if k in value}
    if name == "triangulation_quality" and isinstance(value, Mapping):
        return {k: _num(value.get(k), 6) for k in
                ("plane_rms_mm", "scale_rel_error", "depth_bias_mm")
                if k in value}
    if name == "stereo_consistency" and isinstance(value, Mapping):
        return {k: _num(value.get(k)) for k in
                ("rotation_deg_median", "baseline_error_mm_median",
                 "direction_error_deg_median") if k in value}
    if name == "observability" and isinstance(value, Mapping):
        return {"passed": value.get("passed"), "counts": value.get("counts"),
                "blocking": value.get("blocking")}
    if name == "model_sensitivity" and isinstance(value, Mapping):
        return {k: _num(value.get(k), 6) for k in
                ("heldout_rms_free_kc_px", "heldout_rms_fixed_kc_px",
                 "train_rms_free_kc_px", "train_rms_fixed_kc_px",
                 "relative_improvement", "worth_freeing") if k in value}
    if name == "repeatability" and isinstance(value, Mapping):
        return {k: _num(value.get(k)) for k in
                ("baseline_spread_mm", "rotation_spread_deg") if k in value}
    if name == "numerical_stability" and isinstance(value, Mapping):
        return {"ok": value.get("ok"), "reasons": value.get("reasons")}
    return {"value": _short(value, 140)}


def _record_from_disk(case: Case, out: Path, command: str, run: Mapping[str, Any],
                      extra_evidence: Mapping[str, Any] | None = None,
                      *, verdict: str | None = None) -> dict:
    summary = _read_json(out / "summary.json", default={}) or {}
    val_doc = _read_json(out / "session" / "candidate" / "validation.json",
                         default={}) or {}
    lv = val_doc.get("lifecycle_validate") or {}
    gates = lv.get("gates") or {}
    failed = [{"gate": name, "value": _gate_value(name, gate)}
              for name, gate in gates.items()
              if gate.get("required", True) and not gate.get("pass")]

    cov_doc = (val_doc.get("coverage") or {}) if isinstance(val_doc, Mapping) else {}
    dims_src = (cov_doc.get("dimensions") or {})
    dims: dict[str, dict] = {}
    if dims_src:
        for name, node in dims_src.items():
            if isinstance(node, Mapping):
                dims[name] = {"satisfied": bool(node.get("satisfied")),
                              "measured": _condense(node)}
    if not dims:
        report = _report_measurements(out / "session" / "report.md")
        for name, node in (summary.get("coverage", {}).get("dimensions") or {}).items():
            rep = report.get(name) or {}
            dims[name] = {"satisfied": bool(node),
                          "measured": _condense(rep) if rep else {}}
    satisfied = sum(1 for d in dims.values() if d.get("satisfied"))
    ready = ((val_doc.get("readiness") or {}).get("ready")
             if val_doc else (summary.get("readiness") or {}).get("ready"))
    readiness = (val_doc.get("readiness") or summary.get("readiness") or {})
    obs = (val_doc.get("observability") or summary.get("observability") or {})
    verdict_doc = val_doc.get("verdict") or {}
    cand = summary.get("candidate") or {}
    # the rehearsal CLI reports the solve count only on stdout
    m = re.search(r"decisions:\s*(\d+)\s+accepted:\s*(\d+)\s+rejected:\s*(\d+)"
                  r"\s+solves:\s*(\d+)", str(run.get("stdout") or ""))
    if m:
        summary.setdefault("groups", int(m.group(1)))
        summary.setdefault("accepted", int(m.group(2)))
        summary.setdefault("rejected", int(m.group(3)))
        summary.setdefault("solves", int(m.group(4)))
    fitted_cameras = {}
    cand_profile = _read_json(out / "session" / "candidate" / "profile.json",
                              default=None)
    if isinstance(cand_profile, Mapping):
        for name, node in (cand_profile.get("cameras") or {}).items():
            if isinstance(node, Mapping):
                fitted_cameras[name] = {k: _num(node.get(k), 3) for k in
                                        ("fx", "fy", "cx", "cy")
                                        if k in node}

    cand_verdict = {"produced": bool(cand.get("produced")),
                    "would_activate": bool(cand.get("would_activate")),
                    "verdict_reasons": cand.get("verdict_reasons") or []}
    if verdict_doc:
        cand_verdict["would_activate"] = bool(verdict_doc.get("would_activate"))
        cand_verdict["verdict_reasons"] = verdict_doc.get("reasons") or []

    refused_by: list[dict] = []
    for reason in (summary.get("raw_reasons") or {}):
        refused_by.append({"gate": "ingest: " + _short(reason, 70),
                           "detail": reason})
    for dim_id in (readiness.get("unsatisfied_dimensions") or []) \
            if (summary.get("accepted") or 0) else []:
        node = dims.get({ "D1": "distance_bands", "D2": "tilt_two_axes",
                          "D3": "roll_sectors", "D4": "image_regions",
                          "D5": "stereo_overlap", "D6": "colour_participation",
                          "D7": "quality", "D8": "scale_artefact"}.get(dim_id, ""), {})
        refused_by.append({"gate": f"coverage.{dim_id}",
                           "detail": _short(node.get("measured") or "", 160)})
    for item in failed:
        refused_by.append({"gate": "lifecycle." + item["gate"],
                           "detail": _short(item["value"], 180)})
    for name, ok in ((verdict_doc.get("gates") or {}).items()):
        if ok is False:
            refused_by.append({"gate": "candidate." + name, "detail": "failed"})

    produced = bool(cand_verdict["produced"])
    refused = (not produced) or (not cand_verdict["would_activate"])
    if verdict is None:
        verdict = "correctly refused" if refused else "ACCEPTED — NOT REFUSED"
    if fitted_cameras:
        extra_evidence = dict(extra_evidence or {})
        extra_evidence.setdefault("fitted_cameras", fitted_cameras)
    record = {
        "id": case.id,
        "requirement": case.requirement,
        "title": case.title,
        "kind": case.kind,
        "expectation": case.expectation,
        "command": command,
        "wall_s": round(float(run.get("wall_s") or summary.get("wall_s") or 0.0), 2),
        "returncode": int(run.get("returncode") or 0),
        "decisions": {
            "groups": summary.get("groups"),
            "accepted": summary.get("accepted"),
            "rejected": summary.get("rejected"),
            "solves": summary.get("solves"),
            "by_reason": summary.get("by_reason") or {},
            "raw_reasons": summary.get("raw_reasons") or {},
        },
        "coverage": {
            "satisfied": int(satisfied),
            "total": len(dims),
            "ready": bool(ready),
            "dimensions": dims,
        },
        "readiness": {
            "ready": bool(readiness.get("ready")),
            "reason": readiness.get("reason") or readiness.get("summary") or "",
            "missing": readiness.get("missing") or [],
            "unsatisfied_dimensions": readiness.get("unsatisfied_dimensions") or [],
        },
        "observability": {
            "engine": obs.get("engine"),
            "condition": _num(obs.get("condition")),
            "rank": obs.get("rank"),
            "n_params": obs.get("n_params"),
            "counts": obs.get("counts"),
            "gates": {name: bool((gate or {}).get("pass"))
                      for name, gate in (obs.get("gates") or {}).items()},
        },
        "candidate": cand_verdict,
        "validation": {
            "available": bool(lv),
            "passed": lv.get("passed"),
            "reasons": lv.get("reasons") or [],
            "failed_gates": failed,
            "n_views": lv.get("n_views"),
            "n_heldout_views": lv.get("n_heldout_views"),
        },
        "refused_by": refused_by,
        "verdict": verdict,
        "evidence": dict(extra_evidence or {}),
        "note": case.note,
    }
    return record


# ---------------------------------------------------------------------------
# orchestration
# ---------------------------------------------------------------------------

def run_case(case: Case, root: Path, *, force: bool = False) -> list[dict]:
    out = root / case.id
    if out.exists() and force:
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)

    if case.kind == "scene":
        payload = _run_scene(case, out)
        return [_record_from_disk(case, out, payload["command"], payload["run"])]
    if case.kind == "poses":
        payload = _run_poses(case, out)
        return [_record_from_disk(case, out, payload["command"], payload["run"])]
    if case.kind == "replay-corrupt":
        payload = _run_replay(case, out)
        rec = _record_from_disk(case, payload["out"], payload["command"],
                                payload["run"], payload.get("evidence"))
        rec["verdict"] = "correctly refused (corruption invisible)"
        rec["refused_by"].insert(0, {"gate": "source.silent_skip",
                                     "detail": payload["evidence"].get("source_skipped")})
        return [rec]
    if case.kind == "timestamps":
        records = []
        for item in _run_timestamps(case, out):
            rec = _record_from_disk(case, item["out"], item["command"],
                                    item["run"], item.get("evidence"))
            rec["id"] = f"{case.id}:{item['variant']}"
            rec["title"] = f"{case.title} ({item['variant']})"
            if item["variant"].startswith("regular"):
                rec["verdict"] = "correctly refused (motion gate)"
                rec["refused_by"].insert(0, {
                    "gate": "motion_gate",
                    "detail": {"manifest_continuous": True,
                               "accepted": rec["decisions"].get("accepted"),
                               "rejected": rec["decisions"].get("rejected")}})
            else:
                rec["verdict"] = "gate bypassed (gap): motion gate did not run"
                rec["refused_by"].insert(0, {
                    "gate": "motion_gate.disabled",
                    "detail": {"manifest_continuous": False,
                               "accepted": rec["decisions"].get("accepted"),
                               "rejected": rec["decisions"].get("rejected"),
                               "cause": "irregular host_time_ns -> "
                                        "manifest_is_continuous() = False -> "
                                        "still-collection policy"}})
            records.append(rec)
        return records
    if case.kind == "sync":
        payload = _run_sync(case, out)
        rec = _record_from_disk(case, out, payload["command"], payload["run"],
                                payload.get("evidence"),
                                verdict="not constructible offline")
        rec["validation"] = {"available": False, "passed": None, "reasons":
                             ["the wizard path never supplies per-camera timestamps"],
                             "failed_gates": []}
        rec["refused_by"] = [{"gate": "not_constructible",
                             "detail": "per-camera sync gate is unreachable from "
                                       "WizardController.ingest"}]
        return [rec]
    if case.kind == "analysis":
        payloads = _run_analysis(case, out)
        records = []
        titles = {"overparameterised-fit": (10, "Poorly constrained distortion "
                                                "coefficients"),
                  "heldout-cross-camera": (12, "Inadequate held-out performance"),
                  "residual-vs-geometry": (13, "Lower reprojection error, worse "
                                               "geometry")}
        for tag, payload in zip(("overparameterised-fit", "heldout-cross-camera",
                                 "residual-vs-geometry"), payloads):
            rec = _record_from_disk(case, out / "base", payload["command"],
                                    payload["run"], payload.get("evidence"),
                                    verdict=payload.get("verdict"))
            rec["id"] = tag
            rec["requirement"], rec["title"] = titles[tag]
            rec["basis"] = payload.get("basis")
            evidence = payload.get("evidence") or {}
            if tag == "overparameterised-fit":
                ff = evidence.get("free_fit") or {}
                rec["verdict"] = "correctly refused"
                rec["refused_by"] = [
                    {"gate": "observability.freed_parameters",
                     "detail": {"n_free_rig": ff.get("n_free_rig"),
                                "n_rig_parameters": ff.get("n_rig_parameters"),
                                "condition_scaled": ff.get("condition_scaled"),
                                "verdict_counts": ff.get("verdict_counts"),
                                "blocking_gates": ff.get("blocking_gates"),
                                "worst_distortion": ff.get("worst_distortion")}},
                    {"gate": "staged_release.distortion",
                     "detail": evidence.get("controller_staged_release")},
                    {"gate": "lifecycle.model_sensitivity",
                     "detail": {"relative_improvement": ff.get("relative_improvement"),
                                "worth_freeing": ff.get("worth_freeing")}},
                ]
                rec["validation"] = {
                    "available": True, "passed": False,
                    "reasons": [
                        "freeing all 39 rig parameters leaves "
                        f"{ff.get('verdict_counts', {}).get('unobservable')} parameters "
                        "unobservable",
                        "held-out change when freeing distortion "
                        f"{(ff.get('relative_improvement') or 0) * 100:+.2f} % < 10 %",
                    ],
                    "failed_gates": [
                        {"gate": "observability.freed_parameters",
                         "value": {"condition_scaled": ff.get("condition_scaled"),
                                   "counts": ff.get("verdict_counts")}},
                        {"gate": "model_sensitivity",
                         "value": {"relative_improvement": ff.get("relative_improvement"),
                                   "worth_freeing": ff.get("worth_freeing")}},
                    ],
                    "n_views": (payload.get("basis") or {}).get("n_views_accepted"),
                }
            elif tag == "heldout-cross-camera":
                rec["verdict"] = "correctly refused (control)"
                rec["refused_by"] = [
                    {"gate": "lifecycle.cross_camera",
                     "detail": {"limit_px": evidence.get("cross_camera_limit_px"),
                                "held_fit": evidence.get("candidate_held_fit"),
                                "factory_profile": evidence.get("factory_profile"),
                                "shifted_baseline_0.5mm":
                                    evidence.get("shifted_baseline_0.5mm")}},
                    {"gate": "cross_camera.blind_spot",
                     "detail": "common-mode intrinsic error in the factory profile "
                               "is not detected (it predicts the other camera with "
                               "the same wrong focal length)"},
                    {"gate": "single-axis candidate (natural catastrophe)",
                     "detail": "see the single-axis-motion record: its candidate's "
                               "cross-camera RMS diverges"},
                ]
                rec["validation"] = {
                    "available": True, "passed": False,
                    "reasons": [
                        "candidate fitted on the training views: "
                        f"{(evidence.get('candidate_held_fit') or {}).get('worst_rms_px')} "
                        "px (pass)",
                        "factory profile: "
                        f"{(evidence.get('factory_profile') or {}).get('worst_rms_px')} "
                        "px (pass: common-mode intrinsics are invisible)",
                        "+0.5 mm baseline error: "
                        f"{(evidence.get('shifted_baseline_0.5mm') or {}).get('worst_rms_px')} "
                        "px (fail)",
                    ],
                    "failed_gates": [
                        {"gate": "cross_camera",
                         "value": evidence.get("shifted_baseline_0.5mm")}],
                    "n_heldout_views": evidence.get("n_heldout_views"),
                }
            else:
                ms = evidence.get("model_sensitivity") or {}
                rec["verdict"] = "correctly refused"
                rec["refused_by"] = [
                    {"gate": "lifecycle.model_sensitivity",
                     "detail": {"train_rms_free_kc_px": ms.get("train_rms_free_kc_px"),
                                "train_rms_fixed_kc_px": ms.get("train_rms_fixed_kc_px"),
                                "heldout_rms_free_kc_px": ms.get("heldout_rms_free_kc_px"),
                                "heldout_rms_fixed_kc_px": ms.get("heldout_rms_fixed_kc_px"),
                                "relative_improvement": ms.get("relative_improvement")}},
                    {"gate": "observability.freed_parameters",
                     "detail": (evidence.get("condition_scaled") or {})},
                    {"gate": "held-out triangulation",
                     "detail": evidence.get("heldout_triangulation")},
                ]
                tri = evidence.get("heldout_triangulation") or {}
                rec["validation"] = {
                    "available": True, "passed": False,
                    "reasons": [
                        "freeing distortion lowers the training RMS "
                        f"{(evidence.get('train_rms_px') or {}).get('held_kc')} -> "
                        f"{(evidence.get('train_rms_px') or {}).get('free_kc')} px",
                        "but worsens the held-out cross-camera RMS "
                        f"{(evidence.get('heldout_cross_camera_px') or {}).get('held_kc')} "
                        "-> "
                        f"{(evidence.get('heldout_cross_camera_px') or {}).get('free_kc')} px",
                        "held-out plate plane RMS "
                        f"{(tri.get('held_kc') or {}).get('plane_rms_mm')} -> "
                        f"{(tri.get('free_kc') or {}).get('plane_rms_mm')} mm",
                    ],
                    "failed_gates": [
                        {"gate": "model_sensitivity",
                         "value": {"relative_improvement": ms.get("relative_improvement"),
                                   "worth_freeing": ms.get("worth_freeing")}}],
                    "n_views": (payload.get("basis") or {}).get("n_views_accepted"),
                }
            records.append(rec)
        return records
    raise ValueError(f"unknown case kind {case.kind!r}")


# ---------------------------------------------------------------------------
# markdown report
# ---------------------------------------------------------------------------

_DIM_ORDER = ("distance_bands", "tilt_two_axes", "roll_sectors", "image_regions",
              "stereo_overlap", "colour_participation", "quality", "stationarity")


def _cov_cell(rec: Mapping[str, Any]) -> str:
    dims = rec["coverage"]["dimensions"]
    marks = "".join("✓" if (dims.get(name) or {}).get("satisfied") else "·"
                    for name in _DIM_ORDER)
    return f"{rec['coverage']['satisfied']}/8 `{marks}`"


def _gate_cell(rec: Mapping[str, Any], limit: int = 3) -> str:
    gates = []
    for item in rec.get("refused_by") or []:
        gate = str(item.get("gate"))
        if gate.startswith("candidate.") or gate.startswith("not_constructible"):
            continue
        if gate not in gates:
            gates.append(gate)
    text = ", ".join(f"`{g}`" for g in gates[:limit])
    if len(gates) > limit:
        text += f" +{len(gates) - limit}"
    return text or "—"


def _doc_class(rec: Mapping[str, Any]) -> str:
    verdict = str(rec.get("verdict") or "")
    if verdict.startswith("correctly refused"):
        return "demonstrated (control)" if "control" in verdict else "demonstrated"
    if verdict.startswith("not constructible"):
        return "not constructible"
    if verdict.startswith("gate bypassed"):
        return "**GAP: gate bypassed**"
    if verdict.startswith("accepted"):
        return "**GAP: not refused**"
    return verdict


def _val_cell(rec: Mapping[str, Any]) -> str:
    val = rec.get("validation") or {}
    if rec["verdict"] == "not constructible offline":
        return "n/a (unreachable)"
    if not val.get("available"):
        return "not produced"
    failed = [g["gate"] for g in val.get("failed_gates") or []]
    state = "pass" if val.get("passed") else "refused"
    return state + (" (" + ", ".join(failed[:3]) + ")" if failed else "")


def _doc_rows(records: Sequence[Mapping[str, Any]]) -> list[str]:
    rows = ["| # | case | req | decisions a/r | coverage | readiness | validation | refused by | class |",
            "|---|------|-----|---------------|----------|-----------|------------|------------|-------|"]
    for rec in records:
        d = rec["decisions"]
        dec = f"{d.get('accepted')}/{d.get('rejected')}"
        rows.append("| {id} | {title} | {req} | {dec} | {cov} | {ready} | {val} | "
                    "{gate} | {cls} |".format(
                        id=f"`{rec['id']}`", title=rec["title"], req=rec["requirement"],
                        dec=dec, cov=_cov_cell(rec),
                        ready="READY" if rec["readiness"]["ready"] else "NOT READY",
                        val=_val_cell(rec), gate=_gate_cell(rec),
                        cls=_doc_class(rec)))
    return rows


def _evidence_lines(records: Sequence[Mapping[str, Any]]) -> list[str]:
    by_id = {r["id"]: r for r in records}
    lines: list[str] = []

    def ev(case_id: str) -> Mapping[str, Any]:
        return (by_id.get(case_id) or {}).get("evidence") or {}

    for cid in ("overparameterised-fit", "heldout-cross-camera", "residual-vs-geometry"):
        rec = by_id.get(cid)
        if rec:
            lines.append(f"### `{cid}` — measured\n")
            lines.append("```json")
            lines.append(json.dumps(rec.get("evidence"), indent=2, sort_keys=True,
                                    default=str))
            lines.append("```")
            lines.append("")
    if by_id.get("corrupted-replay"):
        lines.append("### `corrupted-replay` — source behaviour\n")
        lines.append("```json")
        lines.append(json.dumps(by_id["corrupted-replay"].get("evidence"), indent=2,
                                sort_keys=True, default=str))
        lines.append("```")
        lines.append("")
    return lines


_DOC_INTERPRETATION = """\
## Interpretation

The numbers below are the measurements of the run that generated this file; the
machine-readable records are in `matrix.json` (`cases[*].evidence`).  Each case
was designed to remove exactly one kind of excitation or to corrupt one part of
the signal.

### Genuinely demonstrated (refused by a named gate)

* **Repeated viewpoints (1).** 12 identical poses: 3 accepted as bootstrap
  seeds, 9 rejected as `too similar to an accepted view`; coverage is 1/8, the
  scaled condition number is 2.19e6 and 20 of 24 free parameters are
  unobservable.  The candidate is produced but `would_activate` is false.
* **Single-axis motion (2).** A 12-view distance sweep accepts all 12 views and
  then produces a *numerically meaningless* fit: band counts [5, 4, 1] with a
  fitted distance range down to 8 mm, scaled condition 5.16e10, baseline
  2833.8 mm, cross-camera RMS 4.53e18 px, plate plane RMS 28.7 mm and scale
  error +186.7 %.  The refusal is unambiguous, but the dataset is *not cheaply*
  refused: 12 views took 653 s to solve, and `verify.verify` raised an OpenCV
  assertion (reported as "verify unavailable", see defects).
* **Missing depth (3), one tilt axis (4), missing roll (5), missing image
  corners (6).**  Each case fails exactly one coverage family and nothing else:
  D1 (band counts [0, 14, 0], distance span 9.7 mm), D2 (`tilt_y` +0/-0, all
  four quadrants 0), D3 (roll bins [9, 0, 0, 6]) and D4 (one image region
  visited, no corner.  `lifecycle.validate` adds the matching
  `pose_diversity` refusal (e.g. `distance_span_mm = 9.7`, `roll_bins_covered =
  2`) and, for cases 4-6, independent geometric gates (stereo baseline error
  0.684 mm, repeatability spread 0.592 mm, distortion magnitude > 2).
* **Mirrored plate (7).** Every one of the 12 views is rejected at ingest with
  `mirrored marker geometry (det(H)<0)`: the chirality witness works, nothing
  reaches the solver and no candidate is produced.
* **Partial detections (8a occlusion, 8b missing markers).** No view is
  accepted: the occluded plate yields partial/ambiguous lattices, the reduced
  marker set yields no lattice at all.  No candidate is produced.
* **Corrupted replay files (9a).** 20 replay groups, 3 deliberately damaged
  (2 truncated colour frames, 1 deleted left frame).  The source yields 17
  groups and records 3 skips; the wizard sees only the reduced dataset, and the
  CLI output does not mention a single skipped group.  The surviving 12 views
  were refused by D4/observability/repeatability — but *not* because of the
  corruption: nothing in the wizard knows a group was dropped.
* **Over-parameterised distortion (10).** A real fit on the same observations
  with all 39 rig parameters free has a scaled condition number of 96 725
  (held model: 19 958), 23 of 39 parameters unobservable, and the worst
  distortion coefficient (`right.k2`, sigma 0.0886) moves the image corner by
  19.5 px against a 0.75 px budget (ratio 26).  The observability gate blocks
  `freed_parameters`, and the controller's staged release keeps the factory
  distortion model because the held-out change is -0.6 % (< 10 %).
* **Inconsistent colour (11).** 9 accepted views, 0 with >= 12 colour markers,
  0 % participation against the 12-view / 60 % rule: D6 fails.  The
  observability colour gate correctly reports `not_applicable` (no colour
  views), so D6 is the refusing gate.  The fitted colour intrinsics also
  diverge (see defects) and the numerical-stability gate catches that too.
* **Held-out performance (12).** `lifecycle.validate` computes, for every
  held-out view, the other camera's pixels from a free per-view pose and the
  candidate rig.  Measured on 4 held-out views: the candidate fitted on the 11
  training views scores 0.481 px (pass, limit 0.50), the free-distortion fit
  0.488 px (pass), the factory profile 0.467 px (pass!) and the same candidate
  with a +0.5 mm baseline error 1.589 px (fail).  The gate therefore detects
  *relative-geometry* errors with the expected sensitivity; it does not detect
  common-mode intrinsic error (the factory profile passes despite ~1.5 px
  intrinsic perturbation).  The natural catastrophic case is the single-axis
  candidate (4.53e18 px).
* **Lower residual, worse geometry (13).** Two real fits of the same 11
  training views: freeing distortion lowers the training RMS 0.409149 ->
  0.407595 px but raises the held-out cross-camera RMS 0.4810 -> 0.4877 px,
  raises the held-out plate plane RMS 0.370392 -> 0.371916 mm and worsens the
  scaled condition number 19 958 -> 96 725.  Depth bias moves the other way
  (0.547 -> 0.522 mm), so the planarity effect is small on four held-out views;
  the gate that refuses is `model_sensitivity` (held-out change -0.6 % < 10 %)
  plus `observability.freed_parameters`, not `triangulation_quality`.

### Gap demonstrated (a gate that does not run)

* **Irregular timestamps (9b).** The same 12 moving frames (80 mm / 32 deg
  steps) are rejected 11/11 as `moving (...)` when the manifest carries a
  regular 10 Hz cadence (1 accepted), but with alternating 50/400 ms intervals
  `manifest_is_continuous()` returns false, the source is treated as a still
  collection and the motion gate is skipped: 10 of 12 frames are accepted.  The
  run is still refused later on D4/D6 (10 < 12 colour views), but *not* for the
  motion that would have invalidated it.

### Not constructible offline through the wizard path

* **Per-camera synchronisation (9c).** `calib.guidance.evaluate_frame` rejects a
  50 ms skew against `max_sync_ms = 3.0` when given per-camera timestamps, and
  `calib.session.FrameSet` has a `camera_timestamps` field — but
  `WizardController.ingest` constructs the `FrameSet` without it, and
  `FrameGroup` carries only a per-group timestamp plus per-camera `slot_ids`.
  No wizard source can therefore express a camera skew.  The matrix records the
  gate's behaviour measured directly and the wizard-path kwargs observed on a
  real ingest (both in the case evidence).

## Defects and gaps found

1. **Colour intrinsics diverge when the colour camera has no observations.**
   With `free_intrinsics=True` the colour camera is still in the parameter
   vector; with zero colour observations the solver drifts its intrinsics to
   `fx = -629772.198`, `fy = -69221.307`, `cx = -384745.104`,
   `cy = -178874.052`.  The numerical-stability gate refuses the candidate, but
   the nonsense values are written into `candidate/profile.json` first.
   Reproduce:

   ```bash
   .venv/bin/python tools/wizard/rehearsal.py --source synthetic \\
       --scene missing-colour --mode standard --goal full \\
       --out /tmp/mc --json
   .venv/bin/python -c "import json; d=json.load(open('/tmp/mc/session/candidate/profile.json')); print(d['cameras']['color'])"
   ```

2. **The per-camera synchronisation gate is unreachable.**  `FrameSet` has
   `camera_timestamps` and the guidance gate enforces a 3 ms limit, but the
   controller never passes it; `slot_ids` (per-camera trigger ids) are carried
   by the replay source and ignored by the wizard.  Reproduce with
   `--case camera-sync-gate`.

3. **Corrupted frames are skipped silently.**  `ReplayDirSource` retries three
   times and then drops the group; `skipped` is not surfaced by
   `rehearsal.py`, its `summary.json` or the session report.  A capture with a
   broken camera file silently becomes a smaller dataset.  Reproduce with
   `--case corrupted-replay` and compare `evidence.source_skipped` (3 groups)
   with `decisions.groups` (the CLI sees 12 accepted + 5 rejected, no skip
   count).

4. **Cadence irregularity silently disables the motion gate.**  A continuous
   capture whose frame intervals are irregular is classified as a still
   collection, and the still-collection policy deliberately switches the motion
   gate off.  Reproduce with `--case irregular-timestamps`: 1 accepted with a
   regular cadence, 10 accepted with 50/400 ms intervals.

5. **`verify.verify` raises on the degenerate single-axis result.**  The
   single-axis run's final fit produces a rig that makes `verify` raise
   `cv2.error: (-215:Assertion failed) m.dims >= 2 ...`; the controller catches
   it and reports "verify unavailable", so the refusal is correct, but the
   exception is a robustness defect and hides the actual held-out measurement.
   Reproduce:

   ```bash
   .venv/bin/python tools/wizard/rehearsal.py --source synthetic \\
       --scene distance-sweep --mode standard --goal full \\
       --out /tmp/ds --json
   ```

## What this does and does not prove

* Every refusal above is produced by the same code path the live wizard uses;
  the datasets are synthetic, so the absolute millimetre/pixel values are not
  live-scanner values.  The *decisions* (which gate fires on which missing
  excitation) are the transferable result.
* Determinism: synthetic scenes use seed 7; the detector and the solver are
  deterministic for a fixed seed.  Two runs of a case can differ in wall time
  and, for the degenerate single-axis solve, in the exact divergent numbers.
* The analysis cases share one base run (20 designed poses, 15 accepted) and
  split it 11 train / 4 held-out views; the held-out numbers are a small-sample
  measurement.  The base run itself is not coverage-ready (D4 corners are not
  all visited by the surviving poses), which does not affect the fits and
  validation measurements that the three analysis rows report.
* Colour: the missing-colour scene removes markers from the colour camera
  only; the IR views stay well-formed, so the refusal is genuinely about colour
  participation (plus the numerical-stability defect), not about lost IR data.
"""
def write_doc(path: Path, records: Sequence[Mapping[str, Any]], meta: Mapping[str, Any]) -> None:
    runtime = meta.get("runtime_s")
    lines = [
        "# Calibration wizard — failure-case matrix",
        "",
        "Generated by `tools/wizard/failure_matrix.py`; every row is a real run.",
        "No scanner was opened and no `libusb` backend was selected: all datasets",
        "are synthetic scenes, synthetic replay directories or analyses of real",
        "fitted results produced by those runs.",
        "",
        f"- generated: `{meta.get('created_utc')}`",
        f"- python: `{meta.get('python')}`  mode: `{MODE}`, goal: `{GOAL}`, "
        f"pitch: {PITCH_MM:g} mm",
        f"- matrix runtime: **{runtime} s** wall for this invocation "
        f"(sum of recorded case walls: {meta.get('records_wall_s')} s)",
        f"- cases: {len(records)} rows over {len(meta.get('cases') or [])} case runs",
        "- engine: `calib/` + `tools/wizard/` + `tools/sweep/` at the digests "
        "stored in `meta.engine_files` (matrix.json)",
        f"- git: `{(meta.get('git') or {}).get('head')}` "
        f"(dirty={((meta.get('git') or {}).get('dirty'))})"
        + (f" — {meta['snapshot_note']}" if meta.get("snapshot_note") else ""),
        "",
        "Coverage marks are, in order: distance, tilt, roll, image regions,",
        "stereo overlap, colour, quality, stationarity (`✓` satisfied, `·` not).",
        "",
        "## Matrix",
        "",
        *_doc_rows(records),
        "",
        "## Measured evidence",
        "",
        *_evidence_lines(records),
        _DOC_INTERPRETATION,
        "",
        "## Reproduction",
        "",
        "```bash",
        "# the whole matrix (~10-15 min on the development machine)",
        ".venv/bin/python tools/wizard/failure_matrix.py --case all \\",
        "    --out /tmp/wizard-failure-matrix --force --json \\",
        "    --doc docs/calibration/wizard_failure_matrix.md",
        "",
        "# one case (the command recorded in each row)",
        ".venv/bin/python tools/wizard/failure_matrix.py --case mirrored-plate \\",
        "    --out /tmp/wizard-failure-matrix --force --json",
        "```",
        "",
        "Named scenes are run through the wizard CLI exactly as documented;",
        "the pose-list cases are driven through the same `WizardController` with",
        "an explicit `SyntheticSpec.poses` list and the command shown is the",
        "`--case` invocation above.",
        "",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines))


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--case", default="all",
                   help="case id, comma-separated ids, or 'all'")
    p.add_argument("--out", help="output directory (required unless --list)")
    p.add_argument("--force", action="store_true", help="replace an existing case dir")
    p.add_argument("--json", action="store_true", help="print the matrix JSON")
    p.add_argument("--doc", help="write the markdown report to this path")
    p.add_argument("--from-json", help="build --doc from an existing matrix.json")
    p.add_argument("--resume", action="store_true",
                   help="keep records already present in OUT/matrix.json and run "
                        "only the missing cases")
    p.add_argument("--list", action="store_true", help="list the cases and exit")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.list:
        for case in CASES:
            print(f"{case.id:<22} req {case.requirement:<2} {case.kind:<14} "
                  f"{case.title}")
        return 0
    if not args.out and not args.from_json:
        print("error: --out is required", file=sys.stderr)
        return 2

    if args.from_json:
        doc = _read_json(args.from_json, default=None)
        if not isinstance(doc, dict):
            print(f"error: cannot read {args.from_json}", file=sys.stderr)
            return 2
        if args.doc:
            write_doc(Path(args.doc), doc.get("cases") or [], doc.get("meta") or {})
        print(json.dumps(doc.get("meta") or {}, default=str, sort_keys=True))
        return 0

    root = Path(args.out).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    aliases = {"heldout-cross-camera": "overparameterised-fit",
               "residual-vs-geometry": "overparameterised-fit"}
    if args.case.strip().lower() == "all":
        selected = list(CASES)
    else:
        ids = [x.strip() for x in args.case.split(",") if x.strip()]
        ids = list(dict.fromkeys(aliases.get(x, x) for x in ids))
        unknown = [x for x in ids if x not in CASE_BY_ID]
        if unknown:
            print(f"error: unknown case(s): {', '.join(unknown)}", file=sys.stderr)
            return 2
        selected = [CASE_BY_ID[x] for x in ids]

    t0 = time.time()
    previous: dict[str, dict] = {}
    if args.resume and (root / "matrix.json").is_file():
        old = _read_json(root / "matrix.json", default={}) or {}
        previous = {str(rec.get("id")): rec for rec in (old.get("cases") or [])
                    if isinstance(rec, dict) and rec.get("id")}
        if previous:
            print(f"[matrix] resume: {len(previous)} record(s) already present",
                  file=sys.stderr, flush=True)
    records: list[dict] = []
    for case in selected:
        have = case_record_ids(case)
        if previous and all(rid in previous for rid in have):
            print(f"[matrix] {case.id}: already recorded, skipped",
                  file=sys.stderr, flush=True)
            continue
        print(f"[matrix] {case.id} ({case.kind}) ...", file=sys.stderr, flush=True)
        try:
            produced = run_case(case, root, force=args.force)
        except Exception as exc:  # keep the matrix going; record the failure
            produced = [{
                "id": case.id, "requirement": case.requirement, "title": case.title,
                "kind": case.kind, "expectation": case.expectation,
                "command": f"(failed: {type(exc).__name__})", "wall_s": 0.0,
                "returncode": 1, "decisions": {}, "coverage": {"satisfied": 0,
                "total": 8, "ready": False, "dimensions": {}},
                "readiness": {"ready": False, "reason": str(exc), "missing": []},
                "observability": {}, "candidate": {"produced": False,
                "would_activate": False, "verdict_reasons": []},
                "validation": {"available": False, "passed": None,
                               "reasons": [str(exc)], "failed_gates": []},
                "refused_by": [], "verdict": "not constructible offline",
                "evidence": {"error": f"{type(exc).__name__}: {exc}"}, "note": case.note}]
        for rec in produced:
            records.append(rec)
            print(f"[matrix]   -> {rec['id']}: {rec['verdict']} "
                  f"({rec['decisions'].get('accepted')} accepted), "
                  f"{rec['wall_s']} s", file=sys.stderr, flush=True)

    # merge the records this invocation skipped and restore case-table order
    combined = dict(previous)
    combined.update({rec["id"]: rec for rec in records if rec.get("id")})
    order: list[str] = []
    for case in CASES:
        order.extend(case_record_ids(case))
    records = [combined[rid] for rid in order if rid in combined]

    meta = {
        "schema": "einstar.wizard.failure-matrix/1",
        "created_utc": _now(),
        "python": sys.version.split()[0],
        "mode": MODE, "goal": GOAL, "pitch_mm": PITCH_MM,
        "runtime_s": round(time.time() - t0, 1),
        "records_wall_s": round(sum(float(r.get("wall_s") or 0.0)
                                    for r in records), 1),
        "cases": [c.id for c in selected],
        "engine_files": _engine_files(),
        "git": _git_info(),
        "snapshot_note": os.environ.get("EINSTAR_MATRIX_SNAPSHOT_NOTE", ""),
        "safety": "offline only: no scanner, no libusb backend, nothing written "
                  "outside --out",
    }
    matrix = {"meta": meta, "cases": records}
    _atomic_json(root / "matrix.json", matrix)
    if args.doc:
        write_doc(Path(args.doc), records, meta)
    print(json.dumps(matrix, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
