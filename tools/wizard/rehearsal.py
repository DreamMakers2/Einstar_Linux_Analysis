#!/usr/bin/env python3
"""Headless end-to-end rehearsal for the calibration wizard.

Drives exactly the same :class:`tools.wizard.controller.WizardController` the
GUI uses, over a replay directory, a ``.e1r`` recording, the vendor 25-view
corpus or a rendered synthetic scene.  It prints one compact progress line per
decision and finishes with the counts by reason, the coverage measurement, the
readiness verdict, the observability summary, the candidate verdict and the
session report path.  ``--json`` adds a single machine-readable summary as the
last stdout line.

Usage::

    .venv/bin/python tools/wizard/rehearsal.py \\
        --source replay-dir --frames DIR --out OUT [--goal full|demo]
    .venv/bin/python tools/wizard/rehearsal.py \\
        --source recording --recording RAW.e1r --out OUT
    .venv/bin/python tools/wizard/rehearsal.py \\
        --source synthetic --scene diverse --out OUT
    .venv/bin/python tools/wizard/rehearsal.py \\
        --source vendor-corpus --corpus ~/einstar-private/vendor-calib-rapid --out OUT

No scanner is opened, no profile is activated and nothing is written outside
``--out``.
"""
from __future__ import annotations

# Running this file as a script puts ``tools/wizard`` on sys.path[0], where
# ``types.py`` would shadow the standard library ``types`` module (pathlib and
# argparse import it indirectly).  Remove that directory and put the repo root
# first -- using only ``sys``/``os``, before importing anything else.
import os
import sys

_SCRIPT_DIR = os.path.dirname(os.path.abspath(sys.argv[0])) if sys.argv and sys.argv[0] else ""
sys.path[:] = [entry for entry in sys.path
               if entry not in ("", ".", _SCRIPT_DIR, "tools/wizard")
               and os.path.abspath(entry or ".") != _SCRIPT_DIR]
REPO = os.path.dirname(os.path.dirname(_SCRIPT_DIR))
for _path in (os.path.join(REPO, "tools"), REPO):
    if _path in sys.path:
        sys.path.remove(_path)
    sys.path.insert(0, _path)

from pathlib import Path

import argparse
from dataclasses import replace
import json
import math
import os
import shutil
import time
from typing import Any

try:  # normal package import
    from . import controller, sources, types
except ImportError:  # pragma: no cover - executed as a script
    from tools.wizard import controller, sources, types


DEFAULT_FACTORY = Path.home() / "einstar-private" / "einstar-profiles" / "factory-e10.json"

#: Named synthetic scenes (deterministic; used by CI and the GUI demo mode).
SCENES: dict[str, dict[str, Any]] = {
    # self-validating plan: every image region (incl. all four corners), every
    # tilt quadrant and two roll sectors, each pose checked by rendering,
    # detection and the orientation witness before it is emitted
    "coverage": dict(pattern="coverage", n_views=24),
    # deliberately sufficient: every family, ~2.5x the minimum view count, for
    # the end-to-end "validated profile" demonstration
    "coverage-mid": dict(pattern="coverage", n_views=36),
    "coverage-large": dict(pattern="coverage", n_views=60),
    "coverage-small": dict(pattern="coverage", n_views=12),
    "diverse": dict(pattern="diverse", n_views=24),
    "diverse-small": dict(pattern="diverse", n_views=12),
    "distance-sweep": dict(pattern="distance", n_views=12),
    "tilt-sweep": dict(pattern="tilt", n_views=12),
    "single": dict(pattern="single", n_views=1),
    # failure cases the reviewer explicitly asked to be reproducible
    "blurred": dict(pattern="diverse", n_views=12, blur_sigma=4.5),
    "noisy": dict(pattern="diverse", n_views=12, noise_sigma=25.0),
    "mirrored": dict(pattern="diverse", n_views=12, marker_layout="mirror_u"),
    "missing-markers": dict(pattern="diverse", n_views=12, visible_markers=22),
    "missing-colour": dict(pattern="diverse", n_views=12,
                           drop_by_camera={"tex": 32}),
    "occluded": dict(pattern="diverse", n_views=12, occlusion="left",
                     occlusion_frac=0.55),
}


# ---------------------------------------------------------------------------
# sources
# ---------------------------------------------------------------------------

def build_source(args: argparse.Namespace) -> tuple[Any, Path | None, dict]:
    """Build the frame source; returns (source, synthetic_profile_path, meta)."""
    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    if args.source == "replay-dir":
        if not args.frames:
            raise SystemExit("--frames is required for --source replay-dir")
        src = sources.ReplayDirSource(Path(args.frames).expanduser(),
                                      start=args.start, limit=args.limit)
        return src, None, {"kind": "replay-dir", "path": str(Path(args.frames).expanduser())}
    if args.source == "recording":
        if not args.recording:
            raise SystemExit("--recording is required for --source recording")
        src = sources.RecordingSource(Path(args.recording).expanduser(),
                                      max_groups=args.limit)
        probe = src.probe()
        return src, None, {"kind": "recording",
                           "path": str(Path(args.recording).expanduser()),
                           "probe": {k: probe.get(k) for k in
                                     ("ok", "verified", "returncode", "groups",
                                      "seconds", "error")}}
    if args.source == "synthetic":
        scene = args.scene or "diverse"
        if scene not in SCENES:
            raise SystemExit(f"unknown --scene {scene!r}; choose "
                             + ", ".join(sorted(SCENES)))
        spec = sources.SyntheticSpec(seed=args.seed,
                                     factory_perturb_px=args.factory_perturb_px,
                                     **SCENES[scene])
        if args.pitch_mm:
            spec = replace(spec, pitch_mm=float(args.pitch_mm))
        src = sources.SyntheticSource(spec)
        profile_path = out / "factory-synthetic.json"
        types.atomic_write_json(profile_path, src.profile_dict())
        return src, profile_path, {"kind": "synthetic", "scene": scene,
                                   "seed": args.seed,
                                   "pattern": spec.pattern}
    if args.source == "vendor-corpus":
        corpus = Path(args.corpus or args.frames or "").expanduser()
        if not corpus or not corpus.is_dir():
            raise SystemExit("--corpus (or --frames) must be the vendor corpus "
                             "directory")
        try:
            from .vendor_source import VendorCorpusSource
        except ImportError:  # pragma: no cover - script mode
            from tools.wizard.vendor_source import VendorCorpusSource
        src = VendorCorpusSource(corpus)
        return src, None, {"kind": "vendor-corpus", "path": str(corpus)}
    raise SystemExit(f"unknown --source {args.source!r}")


# ---------------------------------------------------------------------------
# output helpers
# ---------------------------------------------------------------------------

def _fmt(value: Any, digits: int = 1) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return str(value)


def _progress(index: int, total: int | None, group: types.FrameGroup,
              decision: Any) -> str:
    marker = decision.quality.get("n_markers") if isinstance(
        decision.quality, dict) else None
    marker = marker or {}
    cameras = "/".join(str(marker.get(k, "?")) for k in ("left", "right", "tex"))
    pose = None
    if decision.status and isinstance(decision.status, dict):
        pose = decision.status.get("last_pose")
    if decision.accepted:
        seed = "bootstrap seed" in (decision.reasons or [])
        return (f"[{index:>4}/{(total if total else '?'):<4}] "
                f"{'seed' if seed else 'ACCEPT':<6} group {int(group.index):>4}  "
                f"markers L/R/T {cameras:<10} view {decision.view_id}  "
                f"gain {_fmt(decision.predicted_gain, 4)}"
                + ("  (bootstrap seed)" if seed else ""))
    reasons = "; ".join(str(r) for r in (decision.reasons or []))[:150]
    return (f"[{index:>4}/{(total if total else '?'):<4}] reject group "
            f"{int(group.index):>4}  markers L/R/T {cameras:<10} {reasons}")


def _coverage_table(coverage: dict) -> list[str]:
    lines = []
    dims = coverage.get("dimensions") or {}
    for name, dim in dims.items():
        mark = "x" if dim.get("satisfied") else " "
        measured = dim.get("measured")
        text = json.dumps(measured, default=str) if not isinstance(
            measured, str) else measured
        lines.append(f"  [{mark}] {name:<22} {text[:160]}")
    return lines


def _observability_table(obs: dict) -> list[str]:
    lines = []
    if not obs:
        return ["  (unavailable)"]
    counts: dict[str, int] = {}
    for entry in (obs.get("parameters") or {}).values():
        verdict = entry.get("verdict", "unknown")
        counts[verdict] = counts.get(verdict, 0) + 1
    lines.append(f"  engine        : {obs.get('engine')}")
    lines.append(f"  condition     : {_fmt(obs.get('condition'), 3)}")
    lines.append(f"  rank          : {obs.get('rank')} / {obs.get('n_params')} "
                 "free parameters")
    lines.append("  verdicts      : " + ", ".join(
        f"{k} {v}" for k, v in sorted(counts.items())))
    for name, gate in (obs.get("gates") or {}).items():
        status = "pass" if (gate or {}).get("pass") else "FAIL"
        lines.append(f"  gate {name:<18}: {status}  "
                     f"{str((gate or {}).get('detail', ''))[:110]}")
    weak = sorted(((name, entry) for name, entry in
                   (obs.get("parameters") or {}).items()
                   if entry.get("verdict") != "constrained"),
                  key=lambda item: -float(item[1].get("sigma") or 0.0))
    for name, entry in weak[:8]:
        lines.append(f"    {name:<16} sigma {_fmt(entry.get('sigma'), 4)} "
                     f"{entry.get('unit') or ''}  {entry.get('verdict')}")
    return lines


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Headless calibration-wizard rehearsal",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--source", required=True,
                   choices=("replay-dir", "recording", "synthetic",
                            "vendor-corpus"))
    p.add_argument("--frames", help="replay directory (or vendor corpus)")
    p.add_argument("--recording", help=".e1r raw USB recording")
    p.add_argument("--corpus", help="vendor corpus directory")
    p.add_argument("--scene", default="diverse",
                   help="synthetic scene: " + ", ".join(sorted(SCENES)))
    p.add_argument("--seed", type=int, default=7, help="synthetic seed")
    p.add_argument("--out", required=True, help="output directory")
    p.add_argument("--factory", help="factory recon profile JSON")
    p.add_argument("--mode", default="high", choices=("quick", "standard", "high"))
    p.add_argument("--goal", default="full", choices=("full", "demo"))
    p.add_argument("--pitch-mm", type=float, default=28.0)
    p.add_argument("--factory-perturb-px", type=float, default=1.5,
                   help="synthetic only: perturb the factory intrinsics (px)")
    p.add_argument("--limit", type=int, default=None,
                   help="process at most N groups")
    p.add_argument("--start", type=int, default=0,
                   help="skip the first N groups (replay-dir)")
    p.add_argument("--max-groups", type=int, default=None,
                   help="demo goal: hard cap on groups (default 60)")
    p.add_argument("--ring-keep", type=int, default=24,
                   help="rejected frame groups kept on disk for evidence")
    p.add_argument("--force", action="store_true",
                   help="replace OUT/session if it already exists")
    p.add_argument("--json", action="store_true",
                   help="print the machine-readable summary as the last line")
    p.add_argument("--quiet", action="store_true",
                   help="only print accepted decisions and the summary")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    out = Path(args.out).expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    session_dir = out / "session"
    if session_dir.exists():
        if not args.force:
            print(f"error: {session_dir} already exists (use --force to replace)",
                  file=sys.stderr)
            return 2
        if session_dir.parent != out:
            print("refusing to remove a directory outside --out", file=sys.stderr)
            return 2
        shutil.rmtree(session_dir)

    factory = Path(args.factory).expanduser() if args.factory else DEFAULT_FACTORY
    t0 = time.time()
    try:
        src, synthetic_profile, source_meta = build_source(args)
    except (sources.SourceError, OSError, ValueError) as exc:
        print(f"error: cannot open source: {exc}", file=sys.stderr)
        return 2
    if synthetic_profile is not None:
        factory = synthetic_profile
    if not Path(factory).is_file():
        print(f"error: factory profile not found: {factory}", file=sys.stderr)
        return 2

    cap = None
    if args.goal == "demo":
        cap = args.max_groups if args.max_groups is not None else 60
    elif args.max_groups is not None:
        cap = args.max_groups
    if args.limit is not None:
        cap = args.limit if cap is None else min(cap, args.limit)

    info = src.info
    print("=" * 78)
    print("calibration wizard rehearsal")
    print(f"  source      : {info.kind} {info.path}")
    if info.note:
        print(f"  source note : {info.note}")
    print(f"  continuous  : {info.continuous}")
    print(f"  factory     : {factory}")
    print(f"  session     : {session_dir}")
    print(f"  mode/goal   : {args.mode}/{args.goal}"
          + (f"  (cap {cap})" if cap else ""))
    print("=" * 78)

    ctl = controller.WizardController(
        factory_profile=factory, session_dir=session_dir, mode=args.mode,
        pitch_mm=args.pitch_mm, goal=args.goal, source=info,
        ring_keep=args.ring_keep)
    begun = ctl.begin()
    print(f"  factory sha256: {begun.get('factory_sha256')}")
    print("-" * 78)

    total = cap if cap is not None else None
    index = 0
    try:
        for group in src:
            if cap is not None and index >= cap:
                break
            decision = ctl.ingest(group)
            if not args.quiet or decision.accepted:
                print(_progress(index + 1, total, group, decision), flush=True)
            index += 1
    except KeyboardInterrupt:
        print("\ninterrupted: keeping recorded data and cancelling")
        ctl.cancel()
        return 130
    finally:
        try:
            src.close()
        except Exception:
            pass

    wall_ingest = time.time() - t0
    snapshot = ctl.snapshot()
    print("-" * 78)
    print(f"decisions: {index}   accepted: {len(ctl.session.accepted)}   "
          f"rejected: {len(ctl.session.rejected)}   "
          f"solves: {ctl.session._solves}   ingest wall: {wall_ingest:.1f} s")

    print("\ndecisions by reason category:")
    by_reason = snapshot.get("counts", {}).get("by_reason") or {}
    for name, count in sorted(by_reason.items(), key=lambda kv: (-kv[1], kv[0])):
        print(f"  {name:<22} {count}")
    # the raw reason strings are what the operator sees
    raw: dict[str, int] = {}
    for record in ctl.decisions():
        for reason in record.get("reasons") or []:
            key = str(reason).split("(")[0].strip()
            raw[key] = raw.get(key, 0) + 1
    if raw:
        print("\ntop rejection reasons:")
        for name, count in sorted(raw.items(), key=lambda kv: (-kv[1], kv[0]))[:8]:
            print(f"  {count:>4}  {name[:110]}")

    finish = ctl.finish()
    # refresh after finish(): the full observability gate suite runs there
    snapshot = ctl.snapshot()
    coverage = snapshot.get("coverage") or {}
    readiness = snapshot.get("readiness") or {}
    observability = snapshot.get("observability") or {}
    print("\ncoverage:")
    for line in _coverage_table(coverage):
        print(line)
    print(f"\nreadiness: {'READY' if readiness.get('ready') else 'NOT READY'} "
          f"({coverage.get('satisfied')}/{coverage.get('total')} dimensions)")
    print(f"  reason: {readiness.get('reason')}")
    if readiness.get("missing"):
        print("  missing: " + ", ".join(readiness["missing"]))
    print("\nobservability (after the final solve):")
    for line in _observability_table(finish.get("observability") or observability):
        print(line)
    candidate = finish.get("candidate") or {}
    verdict = candidate.get("verdict") or {}
    print("\ncandidate:")
    if candidate.get("produced"):
        print(f"  profile      : {candidate.get('path')}")
        print(f"  validation   : pending  (would activate: "
              f"{bool(verdict.get('would_activate'))})")
        print(f"  sha256       : {candidate.get('sha256')}")
        for reason in (verdict.get("reasons") or [])[:8]:
            print(f"  gate note    : {reason}")
    else:
        print(f"  not produced : {candidate.get('reason', finish.get('error'))}")
    print(f"\nsession report: {finish.get('report_path')}")
    print(f"wall time     : {time.time() - t0:.1f} s")

    summary = {
        "source": source_meta,
        "factory": str(factory),
        "factory_sha256": begun.get("factory_sha256"),
        "mode": args.mode,
        "goal": args.goal,
        "session_dir": str(session_dir),
        "report_path": finish.get("report_path"),
        "manifest": str(ctl.manifest_path),
        "manifest_sha256": ctl.manifest_digest(),
        "groups": index,
        "accepted": len(ctl.session.accepted),
        "rejected": len(ctl.session.rejected),
        "redundant": snapshot.get("counts", {}).get("redundant"),
        "by_reason": by_reason,
        "raw_reasons": raw,
        "coverage": {
            "ready": bool(coverage.get("ready")),
            "satisfied": coverage.get("satisfied"),
            "total": coverage.get("total"),
            "dimensions": {name: bool((dim or {}).get("satisfied"))
                           for name, dim in (coverage.get("dimensions") or {}).items()},
        },
        "readiness": readiness,
        "observability": {
            "engine": observability.get("engine"),
            "condition": observability.get("condition"),
            "rank": observability.get("rank"),
            "n_params": observability.get("n_params"),
            "gates": {name: bool((gate or {}).get("pass"))
                      for name, gate in (observability.get("gates") or {}).items()},
        },
        "candidate": {
            "produced": bool(candidate.get("produced")),
            "path": candidate.get("path"),
            "validation": "pending" if candidate.get("produced") else None,
            "would_activate": bool(verdict.get("would_activate")),
            "verdict_reasons": verdict.get("reasons") or [],
        },
        "stopping": (finish.get("stopping") or {}).get("stop"),
        "wall_s": round(time.time() - t0, 3),
    }
    types.atomic_write_json(out / "summary.json", summary)
    if args.json:
        print(json.dumps(summary, default=str, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
