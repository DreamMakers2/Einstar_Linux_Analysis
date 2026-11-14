#!/usr/bin/env python3
"""Attribute the wizard decision latency by replaying recorded frame groups.

The live stall question is "where did the 239 ms p50 / 1225 ms p95 decision
go?", and the answer has to come from the real decision path, not from a
micro-benchmark of one helper.  This tool replays recorded ``frame_%06d_camN``
PGM triplets through the real :class:`tools.wizard.controller.WizardController`
(same constructor arguments as ``calibrate_session`` / the GUI adapter), and
times every stage of ``controller.ingest`` with wall-clock timers attached *from
the outside*: nothing in the controller, the session or the sources is modified,
so the measurement cannot change the thing it measures.

Four modes:

``sync``
    Iterate the source and call ``controller.ingest(group)`` inline, one group
    after another.  This is the attribution instrument: per-stage p50/p95/p99,
    the top-level decomposition of one decision, CPU-vs-wall (GIL), child CPU
    for the process pool, and an optional ``cProfile`` dump.
``live``
    Run the *real* ``worker.SessionWorker`` with async decisions, paced at the
    measured device rate, so the newest-frame policy and the drop ratio can be
    reproduced and compared with the live session's ``ui_metrics.json``.
``solve-curve``
    Read a recorded ``session.json`` (read-only) and time the real solver at
    increasing accepted-view counts.  This gives the solve cost curve of an
    actual session instead of the few view counts a 120-frame replay reaches.
``solve-budget``
    Combine the measured curve with the controller's ``solve_every`` policy to
    price the solves of a full 162-view session (how many, how long, and what
    share of the decision thread).

Examples::

    .venv/bin/python -m tools.wizard.bench_pipeline \
        --frames ~/einstar-calibration-fresh-01/frames \
        --factory ~/einstar-private/einstar-profiles/factory-e10.json \
        --mode sync --detect threads --groups 120 \
        --out analysis/calibration/performance/fresh01_sync.json

    .venv/bin/python -m tools.wizard.bench_pipeline --mode live --pace-hz 9.77 ...
    .venv/bin/python -m tools.wizard.bench_pipeline --mode solve-curve \
        --session ~/einstar-calibration-fresh-01 ...

The recorded session is opened read-only.  Every file this tool writes lives in
``--session-dir`` (a fresh temporary directory by default), ``--out`` or
``--pstats``; nothing is written next to the recording and the scanner is never
touched.
"""
from __future__ import annotations

import argparse
import cProfile
import io
import json
import os
import pstats
import resource
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

_REPO = Path(__file__).resolve().parents[2]
for _path in (_REPO, _REPO / "tools"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

SCHEMA = "einstar.wizard.bench-pipeline/1"

#: CalibrationSession mode defaults that the solve policy depends on
#: (calib/session.py MODES): bootstrap_views and the initial solve_every that
#: WizardController installs before any view is accepted.
BOOTSTRAP_VIEWS = {"quick": 3, "standard": 4, "high": 5}
INITIAL_SOLVE_EVERY = {"quick": 1, "standard": 2, "high": 3}

#: default recorded session (read-only): the 120 remaining frame groups
FRESH01 = Path("<calibration-session>")
DEFAULT_FRAMES = FRESH01 / "frames"
DEFAULT_FACTORY = Path(
    "<private-inputs>/einstar-profiles/factory-e10.json")


# ---------------------------------------------------------------------------
# small statistics helpers
# ---------------------------------------------------------------------------

def percentile(values: Iterable[float], q: float) -> float | None:
    """Nearest-rank percentile (q in 0..1) of an unsorted sequence."""
    data = sorted(float(v) for v in values)
    if not data:
        return None
    if len(data) == 1:
        return data[0]
    index = int(round((len(data) - 1) * max(0.0, min(1.0, q))))
    return data[index]


def _ms(value: float | None) -> float | None:
    return None if value is None else round(float(value), 3)


def stage_summary(samples: Mapping[str, list[float]]) -> dict:
    out: dict[str, dict] = {}
    for name, values in samples.items():
        if not values:
            continue
        total = sum(values)
        out[name] = {
            "count": len(values),
            "total_ms": _ms(total),
            "mean_ms": _ms(total / len(values)),
            "p50_ms": _ms(percentile(values, 0.50)),
            "p90_ms": _ms(percentile(values, 0.90)),
            "p95_ms": _ms(percentile(values, 0.95)),
            "p99_ms": _ms(percentile(values, 0.99)),
            "max_ms": _ms(max(values)),
        }
    return out


# ---------------------------------------------------------------------------
# stage instrumentation (attached from the outside)
# ---------------------------------------------------------------------------

#: Stages whose children run concurrently (three camera workers), so the sum of
#: a child's wall time may exceed the parent's.  A residual is only meaningful
#: for serially nested stages.
PARALLEL_STAGES = {"detect"}

#: stage -> parent stage.  ``None`` marks a top-level chunk of one decision.
STAGE_TREE: dict[str, str | None] = {
    "decision": None,
    "ram_ring": "decision",
    "colour": "decision",
    "detect": "decision",
    "detect.grid_auto": "detect",
    "detect.partial": "detect",
    "detect.pool_map": "detect",
    "pre_checks": "decision",
    "session_ingest": "decision",
    "session.quality_pose": "session_ingest",
    "session.refine": "session_ingest",
    "session.holdout": "session_ingest",
    "session.solve": "session_ingest",
    "accept_store": "decision",
    "reject": "decision",
    "veto": "decision",
    "derived": "decision",
    "derived.coverage": "derived",
    "derived.observability": "derived",
    "derived.guidance": "derived",
    "persist": "decision",
    "persist.manifest": "persist",
    "persist.session_json": "persist",
    "publish": "decision",
    "append_decision": "decision",
}


def stage_children() -> dict[str, list[str]]:
    """Reverse the stage tree: parent -> ordered direct children."""
    children: dict[str, list[str]] = {}
    for name, parent in STAGE_TREE.items():
        if parent is not None:
            children.setdefault(parent, []).append(name)
    return children


class StageProfiler:
    """Wall-clock timers on named attributes, plus the modules' hot functions.

    Every wrapper restores the original attribute on exit, so a profiled
    controller behaves exactly like an unprofiled one afterwards.  A missing
    attribute is not an error: the controller is owned by another agent and its
    private helpers may be renamed, so a stage that no longer exists is reported
    as absent rather than breaking the run.
    """

    def __init__(self) -> None:
        self.samples: dict[str, list[float]] = {}
        self.counts: dict[str, int] = {}
        self.missing: list[str] = []
        self._restore: list[tuple[Any, str, Any]] = []

    # -- recording -----------------------------------------------------
    def record(self, stage: str, seconds: float) -> None:
        self.samples.setdefault(stage, []).append(seconds * 1000.0)
        self.counts[stage] = self.counts.get(stage, 0) + 1

    # -- wrapping ------------------------------------------------------
    def wrap(self, owner: Any, attr: str, stage: str, *,
             required: bool = False) -> bool:
        if owner is None or not hasattr(owner, attr):
            self.missing.append(f"{type(owner).__name__}.{attr}" if owner is not None
                                else f"<none>.{attr}")
            if required:
                raise SystemExit(f"bench: required stage {stage!r} not found "
                                 f"({attr!r})")
            return False
        original = getattr(owner, attr)
        if not callable(original):
            self.missing.append(f"{attr} (not callable)")
            return False
        prof = self

        def timed(*args, __original=original, __stage=stage, **kwargs):
            start = time.perf_counter()
            try:
                return __original(*args, **kwargs)
            finally:
                prof.record(__stage, time.perf_counter() - start)

        try:
            setattr(owner, attr, timed)
        except (AttributeError, TypeError):     # pragma: no cover - read-only attr
            self.missing.append(f"{attr} (not writable)")
            return False
        self._restore.append((owner, attr, original))
        return True

    def __enter__(self) -> "StageProfiler":
        return self

    def __exit__(self, *exc: Any) -> None:
        for owner, attr, original in reversed(self._restore):
            try:
                setattr(owner, attr, original)
            except Exception:                   # pragma: no cover
                pass
        self._restore.clear()

    # -- reporting -----------------------------------------------------
    def totals(self) -> dict[str, float]:
        return {name: sum(values) for name, values in self.samples.items()}

    def exclusive_ms(self) -> dict[str, float]:
        """Inclusive total minus the inclusive totals of direct children.

        Only for serially nested stages: the three camera detections run
        concurrently, so their wall times overlap and no residual can be
        derived from them.
        """
        inclusive = self.totals()
        children = stage_children()
        out: dict[str, float] = {}
        for name, total in inclusive.items():
            if name in PARALLEL_STAGES:
                out[name] = total
                continue
            child_total = sum(inclusive.get(child, 0.0)
                              for child in children.get(name, [])
                              if child not in PARALLEL_STAGES)
            out[name] = max(0.0, total - child_total)
        return out

    def decomposition(self, root: str = "decision") -> dict:
        """Serial breakdown of one decision: each direct child + unaccounted."""
        inclusive = self.totals()
        total = inclusive.get(root, 0.0)
        children = stage_children().get(root, [])
        rows: dict[str, dict] = {}
        for child in children:
            value = inclusive.get(child, 0.0)
            if not value:
                continue
            rows[child] = {
                "total_ms": round(value, 3),
                "share": round(value / total, 4) if total else None,
                "count": len(self.samples.get(child, [])),
            }
        accounted = sum(row["total_ms"] for row in rows.values())
        return {
            "root_total_ms": round(total, 3),
            "children": dict(sorted(rows.items(), key=lambda kv: -kv[1]["total_ms"])),
            "accounted_ms": round(accounted, 3),
            "unaccounted_ms": round(max(0.0, total - accounted), 3),
            "unaccounted_share": (round(max(0.0, total - accounted) / total, 4)
                                  if total else None),
        }


def profile_controller(prof: StageProfiler, controller: Any) -> StageProfiler:
    """Attach every decision-path stage to one live controller instance."""
    session = getattr(controller, "session", None)

    # -- top-level chunks of controller.ingest -------------------------
    prof.wrap(controller, "_materialise", "materialise")
    prof.wrap(controller, "_detect", "detect", required=False)
    prof.wrap(controller, "_pre_checks", "pre_checks")
    prof.wrap(controller, "_record_accept", "accept_store")
    prof.wrap(controller, "_record_reject", "reject")
    prof.wrap(controller, "_veto", "veto")
    prof.wrap(controller, "_refresh_derived", "derived")
    prof.wrap(controller, "measure_coverage", "derived.coverage")
    prof.wrap(controller, "_refresh_observability", "derived.observability")
    prof.wrap(controller, "_refresh_guidance", "derived.guidance")
    prof.wrap(controller, "_persist_progress", "persist")
    prof.wrap(controller, "_write_manifest", "persist.manifest")
    prof.wrap(controller, "_write_session_json", "persist.session_json")
    prof.wrap(controller, "_publish", "publish")
    prof.wrap(controller, "_append_decision", "append_decision")

    # -- inside the session --------------------------------------------
    if session is not None:
        prof.wrap(session, "ingest", "session_ingest")
        prof.wrap(session, "solve_fn", "session.solve")
        prof.wrap(session, "_refine", "session.refine")
        prof.wrap(session, "_holdout_rms", "session.holdout")

    # -- module-level hot functions (visible only in the thread path) ---
    try:
        from calib import detect as detect_mod
        prof.wrap(detect_mod, "detect_grid_auto", "detect.grid_auto")
    except Exception:                            # pragma: no cover
        prof.missing.append("calib.detect.detect_grid_auto")
    try:
        from calib import detect_partial
        prof.wrap(detect_partial, "detect_grid_partial", "detect.partial")
    except Exception:                            # pragma: no cover
        prof.missing.append("calib.detect_partial.detect_grid_partial")
    try:
        from calib import guidance
        prof.wrap(guidance, "evaluate_frame", "session.quality_pose")
    except Exception:                            # pragma: no cover
        prof.missing.append("calib.guidance.evaluate_frame")
    return prof


# ---------------------------------------------------------------------------
# controller construction (identical to the GUI/headless session path)
# ---------------------------------------------------------------------------

def build_controller(factory: Path, session_dir: Path, *, mode: str,
                     storage: str, source_info: Any) -> Any:
    from tools.wizard import controller as controller_mod
    ctl = controller_mod.WizardController(
        factory_profile=str(factory), session_dir=str(session_dir),
        mode=mode, storage=storage, source=source_info)
    return ctl


def open_replay(frames: Path, *, start: int = 0, limit: int | None = None):
    from tools.wizard import sources
    return sources.ReplayDirSource(frames, start=start, limit=limit)


# ---------------------------------------------------------------------------
# mode: sync (inline ingest, full attribution)
# ---------------------------------------------------------------------------

def apply_experiment(ctl: Any, experiments: Iterable[str]) -> list[str]:
    """Knock one stage out of the decision path to price it by A/B.

    Only used to *price* a stage; the production default is untouched.  The
    ``no-roi`` experiment is semantically free on the recorded frames (the ROI
    path re-runs the full frame anyway when it is rejected), so its delta is the
    cost of the retry, not of a different detector.
    """
    applied: list[str] = []
    for name in experiments:
        name = str(name).strip()
        if not name:
            continue
        if name == "no-roi":
            ctl._roi_for = lambda *_a, **_k: None
            applied.append("no-roi")
        elif name == "no-solve":
            ctl.session._maybe_solve = lambda: False
            applied.append("no-solve")
        elif name == "no-publish":
            ctl._publish = lambda *_a, **_k: None
            applied.append("no-publish")
        elif name == "no-persist":
            ctl._persist_progress = lambda *_a, **_k: False
            applied.append("no-persist")
        elif name == "no-derived":
            ctl._refresh_derived = lambda *_a, **_k: None
            applied.append("no-derived")
        elif name == "no-accept-store":
            ctl._view_store = None
            applied.append("no-accept-store")
    return applied


def run_sync(frames: Path, factory: Path, *, groups: int = 0, start: int = 0,
             mode: str = "high", storage: str = "production",
             detect: str = "threads", session_dir: Path | None = None,
             cprofile: bool = False, quiet: bool = False,
             experiments: Iterable[str] = ()) -> dict:
    os.environ["EINSTAR_DETECT_PROCESSES"] = "1" if detect == "processes" else "0"
    source = open_replay(frames, start=start, limit=(groups or None))
    tmp = None
    if session_dir is None:
        tmp = Path(tempfile.mkdtemp(prefix="bench-pipeline-"))
        session_dir = tmp / "session"
    session_dir.mkdir(parents=True, exist_ok=True)
    ctl = build_controller(factory, session_dir, mode=mode, storage=storage,
                           source_info=source.info)
    prof = StageProfiler()
    cpu0 = time.process_time()
    wall0 = time.monotonic()
    runner = cProfile.Profile() if cprofile else None
    decode_ms: list[float] = []
    decision_ms: list[float] = []
    decision_cpu_ms: list[float] = []
    decision_children_ms: list[float] = []
    accepted = rejected = 0
    solves = 0
    groups_seen = 0
    applied: list[str] = []
    try:
        ctl.begin()
        profile_controller(prof, ctl)
        prof.wrap(ctl, "ingest", "decision")
        applied = apply_experiment(ctl, experiments)
        if runner is not None:
            runner.enable()
        iterator = iter(source)
        while True:
            t0 = time.perf_counter()
            try:
                group = next(iterator)
            except StopIteration:
                break
            decode_ms.append((time.perf_counter() - t0) * 1000.0)
            groups_seen += 1
            cpu_before = time.process_time()
            wall_before = time.perf_counter()
            solves_before = int(getattr(ctl.session, "_solves", 0))
            decision = ctl.ingest(group)
            decision_ms.append((time.perf_counter() - wall_before) * 1000.0)
            decision_cpu_ms.append((time.process_time() - cpu_before) * 1000.0)
            if decision.accepted:
                accepted += 1
            else:
                rejected += 1
            solves += int(getattr(ctl.session, "_solves", 0)) - solves_before
    finally:
        if runner is not None:
            runner.disable()
        wall = time.monotonic() - wall0
        cpu = time.process_time() - cpu0
        #: shutdown the detection pool so the children are reaped and their
        #: CPU becomes visible in RUSAGE_CHILDREN (it is not while they run)
        pool = getattr(ctl, "_detect_process_pool", None)
        if pool is not None:
            try:
                pool.shutdown(wait=True, cancel_futures=True)
            except Exception:
                pass
            ctl._detect_process_pool = None
        children = resource.getrusage(resource.RUSAGE_CHILDREN)
        try:
            ctl.cancel()
        except Exception:
            pass

    exclusive = prof.exclusive_ms()
    decomposition = prof.decomposition("decision")
    total = sum(prof.samples.get("decision", []))
    detect_total = sum(prof.samples.get("detect", []))
    detect_children = sum(prof.samples.get("detect.grid_auto", [])) + \
        sum(prof.samples.get("detect.partial", []))
    report = {
        "mode": "sync",
        "detect": detect,
        "experiments": applied,
        "session_dir": str(session_dir),
        "frames": str(frames),
        "start": int(start),
        "groups": groups_seen,
        "accepted": accepted,
        "rejected": rejected,
        "solves": solves,
        "wall_s": round(wall, 3),
        "process_cpu_s": round(cpu, 3),
        "process_cpu_over_wall": round(cpu / wall, 3) if wall else None,
        "children_cpu_s": round(children.ru_utime + children.ru_stime, 3),
        "decision": {
            "count": len(decision_ms),
            "total_s": round(total / 1000.0, 3),
            "mean_ms": _ms(sum(decision_ms) / len(decision_ms)) if decision_ms else None,
            "p50_ms": _ms(percentile(decision_ms, 0.50)),
            "p95_ms": _ms(percentile(decision_ms, 0.95)),
            "p99_ms": _ms(percentile(decision_ms, 0.99)),
            "max_ms": _ms(max(decision_ms)) if decision_ms else None,
            "hz": (round(1000.0 * len(decision_ms) / sum(decision_ms), 3)
                   if decision_ms else None),
            "cpu_ms_p50": _ms(percentile(decision_cpu_ms, 0.50)),
            "cpu_ms_p95": _ms(percentile(decision_cpu_ms, 0.95)),
        },
        "decode": {
            "count": len(decode_ms),
            "mean_ms": _ms(sum(decode_ms) / len(decode_ms)) if decode_ms else None,
            "p50_ms": _ms(percentile(decode_ms, 0.50)),
            "p95_ms": _ms(percentile(decode_ms, 0.95)),
            "total_s": round(sum(decode_ms) / 1000.0, 3),
        },
        "stages": stage_summary(prof.samples),
        "exclusive_ms": {k: _ms(v) for k, v in sorted(exclusive.items())},
        "decomposition": decomposition,
        "detect_internals": {
            "detect_total_ms": _ms(detect_total),
            "grid_auto_ms": _ms(sum(prof.samples.get("detect.grid_auto", []))),
            "partial_ms": _ms(sum(prof.samples.get("detect.partial", []))),
            "grid_auto_calls": len(prof.samples.get("detect.grid_auto", [])),
            "partial_calls": len(prof.samples.get("detect.partial", [])),
            "overlap_ratio": (round(detect_children / detect_total, 3)
                              if detect_total else None),
        },
        "missing_stages": sorted(set(prof.missing)),
        "coverage": {
            "accepted_views": len(getattr(ctl.session, "accepted", []) or []),
            "roi_stats": dict(getattr(ctl, "_roi_stats", {}) or {}),
            "detect_error": getattr(ctl, "_last_detect_error", None),
            "persist": dict(getattr(ctl, "_persist_stats", {}) or {}),
            "reasons": dict(sorted((getattr(ctl, "_reason_counts", {}) or {}).items())),
        },
    }
    if runner is not None:
        report["cprofile"] = _cprofile_report(runner, session_dir)
    if tmp is not None:
        shutil.rmtree(tmp, ignore_errors=True)
    if not quiet:
        print(f"[sync/{detect}] groups={groups_seen} accepted={accepted} "
              f"solves={solves} wall={report['wall_s']}s "
              f"decision p50={report['decision']['p50_ms']}ms "
              f"p95={report['decision']['p95_ms']}ms")
    return report


def _cprofile_report(runner: cProfile.Profile, session_dir: Path) -> dict:
    stream = io.StringIO()
    stats = pstats.Stats(runner, stream=stream).sort_stats("cumulative")
    stats.print_stats(45)
    text = stream.getvalue()
    try:
        (session_dir / "profile.txt").write_text(text, encoding="utf-8")
    except OSError:
        pass
    top: list[dict] = []
    for func, (_cc, _nc, tt, ct, _callers) in sorted(
            stats.stats.items(), key=lambda item: item[1][3], reverse=True)[:25]:
        name = func[2] if isinstance(func, tuple) else str(func)
        top.append({"function": str(name), "cumtime_s": round(ct, 4),
                    "tottime_s": round(tt, 4)})
    return {"top_cumulative": top, "text": text[-4000:]}


# ---------------------------------------------------------------------------
# mode: detect (per-camera detection internals, sequential)
# ---------------------------------------------------------------------------

def _noop(payload: bytes) -> int:
    """A pool round trip that does no work: isolates the IPC cost."""
    return len(payload)


def run_detect(frames: Path, *, groups: int = 12, start: int = 0,
               reuse_roi: bool = True, ipc_repeats: int = 20) -> dict:
    """Time detection per camera on real frames, and count threshold passes.

    ``detect_grid_auto`` tries ``2 polarities x 3 percentiles`` and only exits
    after the first combination when the *full* lattice (40 markers) is found
    with a low residual.  This mode counts the passes that actually run on
    recorded frames, prices the ROI retry (the crop is only a speed device; when
    it is rejected the frame is re-detected in full) and prices ``prefer_partial``.
    """
    import numpy as np
    from calib import detect as detect_mod
    from tools.wizard import detection as detection_mod

    #: count detect_grid passes without changing what runs
    passes = {"calls": 0}
    original_grid = detect_mod.detect_grid

    def counting_grid(*args, **kwargs):
        passes["calls"] += 1
        return original_grid(*args, **kwargs)

    frames_on_disk = sorted(p for p in Path(frames).glob("frame_*_cam0.pgm"))
    source = open_replay(frames, start=start, limit=(groups or None))
    full_ms: list[float] = []
    roi_ms: list[float] = []
    partial_ms: list[float] = []
    pass_hist: dict[int, int] = {}
    early_exit = 0
    roi_outcomes = {"ok": 0, "rejected_clipped": 0, "rejected_short": 0,
                    "none": 0}
    per_camera: dict[str, list[float]] = {}
    payload_bytes: dict[str, int] = {"full": 0, "crop": 0}
    boxes: dict[str, tuple] = {}
    detect_mod.detect_grid = counting_grid
    try:
        for group in source:
            for key in sorted(group.images):
                gray = np.ascontiguousarray(np.asarray(group.images[key],
                                                       dtype=np.uint8))
                roi = _roi_from_box(boxes.get(key), gray) if reuse_roi else None
                passes["calls"] = 0
                start_t = time.perf_counter()
                obs = detect_mod.detect_grid_auto(gray, early_lattice=40,
                                                  early_rms=0.5)
                full_ms.append((time.perf_counter() - start_t) * 1000.0)
                per_camera.setdefault(key, []).append(full_ms[-1])
                n_passes = passes["calls"]
                pass_hist[n_passes] = pass_hist.get(n_passes, 0) + 1
                if n_passes <= 1:
                    early_exit += 1
                payload_bytes["full"] += int(gray.nbytes)
                if obs is not None and obs.points:
                    pts = np.asarray(list(obs.points.values()), float)
                    boxes[key] = (float(pts[:, 0].min()), float(pts[:, 1].min()),
                                  float(pts[:, 0].max()), float(pts[:, 1].max()))
                if roi is not None:
                    passes["calls"] = 0
                    start_t = time.perf_counter()
                    roi_obs = detect_mod.detect_grid_auto(gray, roi=roi,
                                                          early_lattice=40,
                                                          early_rms=0.5)
                    roi_ms.append((time.perf_counter() - start_t) * 1000.0)
                    x0, y0, x1, y1 = roi
                    payload_bytes["crop"] += int((x1 - x0) * (y1 - y0))
                    if roi_obs is None:
                        roi_outcomes["none"] += 1
                    else:
                        pts = np.asarray(list(roi_obs.points.values()), float)
                        edge = 8.0
                        inside = ((pts[:, 0] > x0 + edge) & (pts[:, 0] < x1 - edge)
                                  & (pts[:, 1] > y0 + edge) & (pts[:, 1] < y1 - edge))
                        if not bool(inside.all()):
                            roi_outcomes["rejected_clipped"] += 1
                        elif len(roi_obs.points) < 40:
                            # detection.detect_one rejects the crop here and
                            # re-detects the whole frame
                            roi_outcomes["rejected_short"] += 1
                        else:
                            roi_outcomes["ok"] += 1
                start_t = time.perf_counter()
                detection_mod.prefer_partial(gray, obs)
                partial_ms.append((time.perf_counter() - start_t) * 1000.0)
    finally:
        detect_mod.detect_grid = original_grid
        try:
            source.close()
        except Exception:
            pass

    ipc = _ipc_round_trip(frames, repeats=ipc_repeats) if ipc_repeats else {}
    total_full = sum(full_ms)
    return {
        "mode": "detect",
        "groups": len(per_camera.get("left", [])),
        "cameras": sorted(per_camera),
        "frames_available": len(frames_on_disk),
        "full": stage_summary({"full": full_ms})["full"],
        "roi": stage_summary({"roi": roi_ms}).get("roi"),
        "partial": stage_summary({"partial": partial_ms})["partial"],
        "per_camera_p50_ms": {k: _ms(percentile(v, 0.50))
                              for k, v in sorted(per_camera.items())},
        "per_camera_mean_ms": {k: _ms(sum(v) / len(v))
                               for k, v in sorted(per_camera.items())},
        "passes": {
            "histogram": {str(k): v for k, v in sorted(pass_hist.items())},
            "mean_passes": round(sum(int(k) * v for k, v in pass_hist.items())
                                 / max(1, sum(pass_hist.values())), 3),
            "early_exit_fraction": round(early_exit / max(1, sum(pass_hist.values())), 4),
        },
        "roi_outcomes": roi_outcomes,
        "roi_retry_cost_ms": {
            "wasted_mean": _ms(sum(roi_ms) / len(roi_ms)) if roi_ms else None,
            "wasted_total_s": round(sum(roi_ms) / 1000.0, 3),
            "fallback_rate": round(
                (roi_outcomes["rejected_clipped"] + roi_outcomes["rejected_short"]
                 + roi_outcomes["none"]) / max(1, sum(roi_outcomes.values())), 4),
        },
        "payload": {
            "full_bytes_per_group": int(payload_bytes["full"]
                                        / max(1, len(per_camera.get("left", [])))),
            "crop_bytes_per_group": int(payload_bytes["crop"]
                                        / max(1, len(roi_ms) or 1)),
            "crop_ratio": (round(payload_bytes["crop"] / payload_bytes["full"], 3)
                           if payload_bytes["full"] else None),
        },
        "ipc": ipc,
    }


def _roi_from_box(box, gray, margin_fraction: float = 0.30,
                  margin_px: int = 80):
    """The controller's ROI rule (``_ROI_MARGIN_FRACTION``/``_ROI_MARGIN_PX``)."""
    if box is None:
        return None
    x0, y0, x1, y1 = box
    w = max(1.0, x1 - x0); h = max(1.0, y1 - y0)
    mx = max(margin_px, margin_fraction * w)
    my = max(margin_px, margin_fraction * h)
    rx0 = int(max(0, x0 - mx)); ry0 = int(max(0, y0 - my))
    rx1 = int(min(gray.shape[1], x1 + mx)); ry1 = int(min(gray.shape[0], y1 + my))
    if rx1 - rx0 < 64 or ry1 - ry0 < 64:
        return None
    return (rx0, ry0, rx1, ry1)


def _ipc_round_trip(frames: Path, *, repeats: int = 20) -> dict:
    """Price one spawn-pool round trip for the frames the controller submits."""
    import multiprocessing as mp
    from concurrent.futures import ProcessPoolExecutor
    import numpy as np

    source = open_replay(frames, limit=1)
    try:
        group = next(iter(source))
        images = [np.ascontiguousarray(np.asarray(group.images[k], dtype=np.uint8))
                  for k in sorted(group.images)]
    finally:
        try:
            source.close()
        except Exception:
            pass
    payloads = [image.tobytes() for image in images]
    payload_bytes = sum(len(p) for p in payloads)
    #: a crop-sized payload, to price sending only the ROI
    h, w = images[0].shape[:2]
    crops = [p for p in payloads]
    try:
        ctx = mp.get_context("spawn")
        executor = ProcessPoolExecutor(max_workers=3, mp_context=ctx)
    except Exception as exc:                     # pragma: no cover
        return {"error": f"{type(exc).__name__}: {exc}"}
    try:
        executor.submit(_noop, payloads[0]).result()          # warm the children
        for image in images:
            executor.submit(_noop, image.tobytes()).result()
        samples = []
        for _ in range(max(1, repeats)):
            start = time.perf_counter()
            futures = [executor.submit(_noop, p) for p in payloads]
            for future in futures:
                future.result()
            samples.append((time.perf_counter() - start) * 1000.0)
    finally:
        executor.shutdown(wait=True)
    return {
        "payload_bytes_per_group": int(payload_bytes),
        "images_per_group": len(payloads),
        "repeats": max(1, repeats),
        "round_trip_p50_ms": _ms(percentile(samples, 0.50)),
        "round_trip_p95_ms": _ms(percentile(samples, 0.95)),
        "frame_wh": [int(w), int(h)],
        "crop_bytes_hint": int(sum(len(c) for c in crops) / 4),
    }


# ---------------------------------------------------------------------------
# mode: live (real SessionWorker, newest-frame policy, drop ratio)
# ---------------------------------------------------------------------------

def run_live(frames: Path, factory: Path, *, groups: int = 0, start: int = 0,
             mode: str = "high", storage: str = "production",
             detect: str = "processes", pace_hz: float = 9.77,
             session_dir: Path | None = None) -> dict:
    """Drive the real worker so the drop ratio can be compared with the field.

    ``pace_hz`` is the *offered* group rate; the field session delivered 9.77
    groups/s.  With the device-rate offer the worker's own counters
    (``decision_total``, ``superseded``, ``recorded_only``) are directly
    comparable with the live ``ui_metrics.json``.
    """
    os.environ["EINSTAR_DETECT_PROCESSES"] = "1" if detect == "processes" else "0"
    from PySide6 import QtCore
    from tools.wizard.gui import worker as worker_mod

    app = QtCore.QCoreApplication.instance() or QtCore.QCoreApplication([])
    source = open_replay(frames, start=start, limit=(groups or None))
    tmp = None
    if session_dir is None:
        tmp = Path(tempfile.mkdtemp(prefix="bench-live-"))
        session_dir = tmp / "session"
    session_dir.mkdir(parents=True, exist_ok=True)
    ctl = build_controller(factory, session_dir, mode=mode, storage=storage,
                           source_info=source.info)
    prof = StageProfiler()
    wall0 = time.monotonic()
    ctl.begin()
    profile_controller(prof, ctl)
    prof.wrap(ctl, "ingest", "decision")
    w = worker_mod.SessionWorker(ctl, source, pace_hz=pace_hz,
                                 max_groups=0, async_decisions=True)
    errors: list[str] = []
    w.error.connect(lambda message: errors.append(str(message)))
    finished: list[dict] = []
    w.finished_ok.connect(lambda info: finished.append(dict(info or {})))
    w.start()
    #: pump the event loop so the queued decision signals are delivered (the
    #: GUI does exactly this); the worker itself never depends on the pump.
    while w.isRunning() and (time.monotonic() - wall0) < 900.0:
        app.processEvents()
        time.sleep(0.002)
    app.processEvents()
    w.request_stop()
    w.wait(60000)
    wall = time.monotonic() - wall0
    metrics = w.metrics()
    pool = getattr(ctl, "_detect_process_pool", None)
    if pool is not None:
        try:
            pool.shutdown(wait=True, cancel_futures=True)
        except Exception:
            pass
        ctl._detect_process_pool = None
    children = resource.getrusage(resource.RUSAGE_CHILDREN)
    try:
        ctl.cancel()
    except Exception:
        pass
    seen = int(metrics.get("groups_seen") or 0)
    decided = int(metrics.get("decision_total") or 0)
    dropped = int(metrics.get("recorded_only") or 0)
    report = {
        "mode": "live",
        "detect": detect,
        "pace_hz": pace_hz,
        "groups": seen,
        "accepted": len(getattr(ctl.session, "accepted", []) or []),
        "wall_s": round(wall, 3),
        "offered_hz": round(seen / wall, 3) if wall else None,
        "decisions": decided,
        "decision_hz": round(decided / wall, 3) if wall else None,
        "dropped": dropped,
        "drop_ratio": round(dropped / seen, 4) if seen else None,
        "worker_metrics": metrics,
        "worker_errors": errors,
        "finished": finished[-1] if finished else None,
        "children_cpu_s": round(children.ru_utime + children.ru_stime, 3),
        "decision_stage": stage_summary({"decision": prof.samples.get("decision", [])}),
        "newest_frame_policy": (w.newest_frame_policy()
                               if hasattr(w, "newest_frame_policy") else None),
    }
    if tmp is not None:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"[live] offered={report['offered_hz']}Hz decisions={decided} "
          f"({report['decision_hz']}/s) dropped={dropped} "
          f"ratio={report['drop_ratio']}")
    return report


# ---------------------------------------------------------------------------
# mode: solve-curve / solve-budget (read a recorded session.json)
# ---------------------------------------------------------------------------

def _load_recorded_session(session_json: Path) -> dict:
    payload = json.loads(Path(session_json).read_text(encoding="utf-8"))
    resume = payload.get("resume") or {}
    if not resume:
        raise SystemExit(f"no resume block in {session_json}")
    return payload


def _observations_for(session_payload: Mapping[str, Any], n_views: int) -> list:
    """The first ``n_views`` views' observations, as ``calib.calibrate.Observation``."""
    from calib.calibrate import Observation
    rows = (session_payload.get("resume") or {}).get("views") or []
    obs: list = []
    for row in rows:
        if int(row.get("view", 0)) >= int(n_views):
            continue
        points = {}
        for item in row.get("uv") or []:
            points[(float(item[0]), float(item[1]))] = [float(item[2]), float(item[3])]
        obs.append(Observation(str(row.get("camera")), int(row.get("view", 0)),
                               points))
    return obs


def solve_curve(session_json: Path, factory: Path, *, views: list[int],
                mode: str = "high", repeats: int = 1,
                warmup: bool = True) -> dict:
    """Time the real solver at increasing accepted-view counts."""
    payload = _load_recorded_session(session_json)
    config = payload.get("config") or {}
    pitch = float(config.get("pitch_mm") or 28.0)
    tmp = Path(tempfile.mkdtemp(prefix="bench-solve-"))
    try:
        source_info = None
        ctl = build_controller(factory, tmp / "session", mode=mode,
                               storage=str(config.get("storage") or "production"),
                               source_info=source_info)
        ctl.begin()
        session = ctl.session
        solve_fn = session.solve_fn
        policy = dict(getattr(session, "policy", {}) or {})
        kc_views = int(policy.get("kc_views", 8))
        robust = policy.get("robust_loss")
        f_scale = policy.get("f_scale")
        curve = []
        for n in views:
            obs = _observations_for(payload, n)
            cameras = dict(ctl.cameras)
            free_kc = bool(n >= kc_views)
            if warmup:
                # one untimed pass so the first BLAS call does not own the
                # measurement of the smallest problem
                try:
                    solve_fn(obs, pitch, cameras, free_kc, robust, f_scale)
                except Exception:
                    pass
            samples = []
            cpu_samples = []
            for _ in range(max(1, int(repeats))):
                cpu0 = time.process_time()
                start = time.perf_counter()
                result = solve_fn(obs, pitch, cameras, free_kc, robust, f_scale)
                samples.append(time.perf_counter() - start)
                cpu_samples.append(time.process_time() - cpu0)
            ctl.cameras = dict(result.cameras) if hasattr(result, "cameras") else cameras
            #: ``best_s`` is the estimator that matters on a shared host: the
            #: minimum is the sample least polluted by other processes
            curve.append({
                "views": int(n),
                "observations": len(obs),
                "free_kc": free_kc,
                "best_s": round(min(samples), 4),
                "mean_s": round(sum(samples) / len(samples), 4),
                "cpu_best_s": round(min(cpu_samples), 4),
                "cpu_mean_s": round(sum(cpu_samples) / len(cpu_samples), 4),
                "samples_s": [round(s, 4) for s in samples],
                "residual_rms_px": _rms_of(result),
            })
            print(f"[solve-curve] n={n:4d} obs={len(obs):5d} "
                  f"best={curve[-1]['best_s']:.3f}s "
                  f"cpu={curve[-1]['cpu_best_s']:.3f}s "
                  f"rms={curve[-1]['residual_rms_px']}")
        return {"mode": "solve-curve", "session_json": str(session_json),
                "pitch_mm": pitch, "curve": curve,
                "policy": {k: policy.get(k) for k in
                           ("solve_every", "kc_views", "robust_loss", "f_scale")}}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            ctl.cancel()
        except Exception:
            pass


def _rms_of(result: Any) -> float | None:
    for attr in ("residual_rms", "rms", "rms_px"):
        value = getattr(result, attr, None)
        if isinstance(value, (int, float)):
            return round(float(value), 4)
    try:
        import numpy as np
        residuals = getattr(result, "residuals", None)
        if residuals is not None:
            return round(float(np.sqrt(np.mean(np.square(residuals)))), 4)
    except Exception:
        pass
    return None


def _as_curve(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    """Accept either a solve-curve report or the harness payload wrapping it."""
    if "curve" in payload:
        return payload
    inner = payload.get("result")
    if isinstance(inner, Mapping):
        if "curve" in inner:
            return inner
        if isinstance(inner.get("curve_run"), Mapping):
            return inner["curve_run"]
    return payload


def solve_budget(curve: Mapping[str, Any], *, views: int = 162,
                 bootstrap_views: int = 5, initial_solve_every: int = 3,
                 measured_per_decision_s: float = 0.0,
                 sessions: int = 1) -> dict:
    """Price the solve policy of the controller over a full session.

    ``solve_every`` is recomputed by ``WizardController.ingest`` as
    ``max(2, n_accepted // 12)`` once 12 views are accepted, and
    ``CalibrationSession._maybe_solve`` solves when ``n % solve_every == 0``
    (plus the bootstrap trigger).  Play that rule forward and look up each solve
    in the measured curve to get the session's total solve time.
    """
    points = sorted((int(row["views"]),
                    float(row.get("best_s", row.get("s", 0.0))))
                    for row in curve.get("curve") or [])
    if not points:
        raise SystemExit("solve_budget needs a non-empty curve")

    def cost(n: int) -> float:
        exact = dict(points).get(n)
        if exact is not None:
            return exact
        lower = [(v, s) for v, s in points if v <= n]
        upper = [(v, s) for v, s in points if v >= n]
        if not lower:
            return points[0][1]
        if not upper:
            # beyond the largest measured problem: linear extrapolation in the
            # observation count (the solver's cost is dominated by the dense
            # normal equations, i.e. ~n^2..n^3; report the brackets too)
            v0, s0 = lower[-1]
            if len(lower) >= 2:
                v1, s1 = lower[-2]
                slope = (s0 - s1) / max(1, v0 - v1)
                return s0 + slope * (n - v0)
            return s0 * (n / max(1, v0)) ** 2
        v0, s0 = lower[-1]
        v1, s1 = upper[0]
        span = max(1, v1 - v0)
        return s0 + (s1 - s0) * (n - v0) / span

    solves: list[dict] = []
    #: exact order of operations of WizardController.ingest / _maybe_solve:
    #: the policy is derived from the view count *before* the frame is accepted,
    #: and the session solves when the post-append count hits a multiple.  A
    #: loop that derives ``every`` from the post-append count finds 42 solves
    #: instead of the real 33, so the order matters.
    accepted = 0
    solved_once = False
    for _ in range(int(views)):
        if accepted >= 12:
            every = max(2, accepted // 12)
        else:
            every = max(1, int(initial_solve_every))
        accepted += 1
        n = accepted
        if not solved_once and n < max(3, int(bootstrap_views)):
            continue
        if n % every != 0 and n != int(bootstrap_views):
            continue
        solves.append({"n_accepted": n, "solve_every": every,
                       "cost_s": round(cost(n), 3)})
        solved_once = True
    total = sum(row["cost_s"] for row in solves)
    return {
        "views": int(views),
        "solves": len(solves),
        "solve_total_s": round(total, 3),
        "solve_per_solve_s": {
            "p50": round(percentile([r["cost_s"] for r in solves], 0.50), 3),
            "p95": round(percentile([r["cost_s"] for r in solves], 0.95), 3),
            "max": round(max((r["cost_s"] for r in solves), default=0.0), 3),
        },
        "multi_second_solves": sum(1 for r in solves if r["cost_s"] >= 1.0),
        "curve": solves,
        "curve_points": [{"views": v, "s": s} for v, s in points],
        "extrapolated_beyond": (solves[-1]["n_accepted"]
                                if solves and solves[-1]["n_accepted"] > points[-1][0]
                                else None),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _expand(value: str) -> Path:
    return Path(os.path.expanduser(value)).resolve()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", default="sync",
                        choices=("sync", "live", "detect", "solve-curve",
                                 "solve-budget"))
    parser.add_argument("--frames", default=str(DEFAULT_FRAMES),
                        help="recorded frames directory (read-only)")
    parser.add_argument("--session", default=str(FRESH01),
                        help="recorded session directory or session.json "
                             "(read-only; solve-curve/solve-budget)")
    parser.add_argument("--factory", default=str(DEFAULT_FACTORY))
    parser.add_argument("--session-dir", default="",
                        help="where the replay session is written "
                             "(default: a fresh /tmp directory)")
    parser.add_argument("--groups", type=int, default=120,
                        help="groups to replay (0 = all available)")
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--mode-name", dest="calib_mode", default="high",
                        choices=("quick", "standard", "high"))
    parser.add_argument("--storage", default="production",
                        choices=("production", "diagnostic", "legacy"))
    parser.add_argument("--detect", default="threads",
                        choices=("threads", "processes"),
                        help="detection executor (live uses processes)")
    parser.add_argument("--pace-hz", type=float, default=9.77,
                        help="offered group rate for --mode live")
    parser.add_argument("--cprofile", action="store_true")
    parser.add_argument("--experiment", default="",
                        help="comma-separated A/B knock-outs for --mode sync: "
                             "no-roi,no-solve,no-publish,no-persist,no-derived,"
                             "no-accept-store (pricing only)")
    parser.add_argument("--views", default="8,16,25,40,60,84,120,162",
                        help="comma-separated accepted-view counts for "
                             "solve-curve/solve-budget")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--curve", default="",
                        help="JSON from a previous solve-curve run (solve-budget)")
    parser.add_argument("--out", default="", help="write the report JSON here")
    args = parser.parse_args(argv)

    frames = _expand(args.frames)
    factory = _expand(args.factory)
    if not factory.is_file():
        raise SystemExit(f"factory profile not found: {factory}")
    session_dir = _expand(args.session_dir) if args.session_dir else None
    view_counts = [int(v) for v in str(args.views).split(",") if v.strip()]

    report: dict
    if args.mode == "sync":
        report = run_sync(frames, factory, groups=args.groups, start=args.start,
                          mode=args.calib_mode, storage=args.storage,
                          detect=args.detect, session_dir=session_dir,
                          cprofile=args.cprofile,
                          experiments=[e for e in str(args.experiment).split(",")
                                       if e.strip()])
    elif args.mode == "detect":
        report = run_detect(frames, groups=args.groups or 12, start=args.start,
                            ipc_repeats=args.repeats and 20)
    elif args.mode == "live":
        report = run_live(frames, factory, groups=args.groups, start=args.start,
                          mode=args.calib_mode, storage=args.storage,
                          detect=args.detect, pace_hz=args.pace_hz,
                          session_dir=session_dir)
    elif args.mode == "solve-curve":
        session_path = _expand(args.session)
        if session_path.is_dir():
            session_path = session_path / "session.json"
        report = solve_curve(session_path, factory, views=view_counts,
                             mode=args.calib_mode, repeats=args.repeats)
    else:
        if args.curve:
            curve = _as_curve(json.loads(
                _expand(args.curve).read_text(encoding="utf-8")))
        else:
            session_path = _expand(args.session)
            if session_path.is_dir():
                session_path = session_path / "session.json"
            curve = solve_curve(session_path, factory, views=view_counts,
                               mode=args.calib_mode, repeats=args.repeats)
            report_curve = curve
        report = solve_budget(curve if not args.curve else curve,
                             views=max(view_counts) if view_counts else 162,
                             bootstrap_views=BOOTSTRAP_VIEWS.get(
                                 args.calib_mode, 5),
                             initial_solve_every=INITIAL_SOLVE_EVERY.get(
                                 args.calib_mode, 3))
        if not args.curve:
            report = {"mode": "solve-budget", "curve_run": report_curve,
                      "budget": report}

    payload = {
        "schema": SCHEMA,
        "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "argv": list(sys.argv[1:] if argv is None else argv),
        "host": {"cpu_count": os.cpu_count(),
                 "python": sys.version.split()[0],
                 "blas_threads": os.environ.get("OPENBLAS_NUM_THREADS", "default"),
                 "omp_threads": os.environ.get("OMP_NUM_THREADS", "default"),
                 "loadavg": (list(os.getloadavg()) if hasattr(os, "getloadavg")
                             else None)},
        "source": str(frames),
        "factory": str(factory),
        "result": report,
    }
    text = json.dumps(payload, indent=1)
    if args.out:
        out = _expand(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text + "\n", encoding="utf-8")
        print(f"wrote {out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
