#!/usr/bin/env python3
"""Measure the wizard controller's accepted-view storage on real session frames.

The benchmark replays the first N groups of a recorded calibration session
through the real controller twice: once with the production ``ViewStore``
layout (the default) and once with the legacy ``groups/*.pgm`` layout.  The
source session is opened **read-only**; the replay directory holds symlinks and
both output sessions live in a temporary directory that is removed afterwards.

    .venv/bin/python -m tools.wizard.controller_storage_bench \
        --session ~/einstar-calibration-03 --views 24 \
        --factory ~/einstar-private/einstar-profiles/factory-e10.json \
        --out analysis/calibration/controller_storage_2026-11-14.json

The reported numbers are what ``docs/calibration/production_capture.md`` §10
quotes: session-tree bytes, bytes per accepted view, PNG-vs-PGM ratio, staging
ring bytes, wall time and the ``session_io.verify`` verdict.  Nothing here
activates a profile, writes to the scanner or copies a frame into the repo.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time

_REPO = Path(__file__).resolve().parents[2]
for _path in (_REPO, _REPO / "tools"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from tools.wizard import controller, session_io, sources  # noqa: E402


def _tree_bytes(root: Path, *, exclude: tuple[str, ...] = ()) -> dict:
    out = {"total": 0, "views": 0, "groups": 0, "ring": 0, "json": 0}
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root).parts[0]
        if rel in exclude:
            continue
        size = path.stat().st_size
        out["total"] += size
        if rel in out:
            out[rel] += size
        elif path.suffix in (".json", ".jsonl"):
            out["json"] += size
    return out


def _session_groups(base: Path, count: int) -> list[Path]:
    groups_dir = base / "groups"
    if not groups_dir.is_dir():
        raise SystemExit(f"no groups/ directory in {base}")
    found = sorted(p for p in groups_dir.iterdir()
                   if p.is_dir() and p.name.isdigit())
    if not found:
        raise SystemExit(f"no numbered view groups in {groups_dir}")
    return found[:max(1, int(count))]


def _link_frames(groups: list[Path], frames: Path) -> None:
    frames.mkdir(parents=True)
    for index, group in enumerate(groups):
        for cam in range(3):
            (frames / f"frame_{index:06d}_cam{cam}.pgm").symlink_to(
                group / f"cam{cam}.pgm")


def _run(storage: str, root: Path, groups: list[Path], factory: str,
         mode: str) -> dict:
    frames = root / "frames"
    _link_frames(groups, frames)
    source = sources.ReplayDirSource(frames, limit=len(groups))
    session_dir = root / "session"
    ctl = controller.WizardController(
        factory_profile=factory, session_dir=str(session_dir), mode=mode,
        source=source.info, storage=storage)
    started = time.monotonic()
    ctl.begin()
    accepted = rejected = 0
    for group in source:
        decision = ctl.ingest(group)
        accepted += bool(decision.accepted)
        rejected += not decision.accepted
    wall = time.monotonic() - started
    ctl.cancel()
    measured = _tree_bytes(session_dir)
    report = session_io.verify(session_dir)
    snapshot = ctl.snapshot().get("storage") or {}
    return {
        "storage": storage,
        "requested": len(groups),
        "accepted": accepted,
        "rejected": rejected,
        "wall_s": round(wall, 3),
        "bytes": measured,
        "bytes_per_accepted_view": (round(measured["total"] / accepted)
                                    if accepted else None),
        "snapshot_mode": snapshot.get("mode"),
        "snapshot_bytes_per_view_mean": (
            (snapshot.get("bytes_per_view") or {}).get("mean")),
        "verify_ok": bool(report.get("ok")),
        "verify_storage_ok": bool((report.get("storage") or {}).get("ok")),
        "verify_checked": int(report.get("checked") or 0),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", required=True,
                        help="recorded session directory (read-only source)")
    parser.add_argument("--factory", required=True,
                        help="factory profile JSON (read-only)")
    parser.add_argument("--views", type=int, default=24,
                        help="number of real groups to replay (default 24)")
    parser.add_argument("--mode", default="standard",
                        choices=("quick", "standard", "high"))
    parser.add_argument("--out", default="",
                        help="write the measurement JSON here (default stdout)")
    args = parser.parse_args(argv)

    base = Path(os.path.expanduser(args.session)).resolve()
    factory = Path(os.path.expanduser(args.factory)).resolve()
    if not factory.is_file():
        raise SystemExit(f"factory profile not found: {factory}")
    groups = _session_groups(base, args.views)

    tmp = Path(tempfile.mkdtemp(prefix="ctl-storage-bench-"))
    try:
        measurements = [
            _run(mode, tmp / mode, groups, str(factory), args.mode)
            for mode in ("production", "legacy")
        ]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    production, legacy = measurements
    payload = {
        "schema": "einstar.wizard.controller-storage/1",
        "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source_session": str(base),
        "n_groups": len(groups),
        "mode": args.mode,
        "measurements": measurements,
        "comparison": {
            "tree_ratio": (round(legacy["bytes"]["total"]
                                 / production["bytes"]["total"], 3)
                           if production["bytes"]["total"] else None),
            "view_files_ratio": (round(legacy["bytes"]["groups"]
                                       / production["bytes"]["views"], 3)
                                 if production["bytes"]["views"] else None),
            "encode_cost_per_view_s": (
                round((production["wall_s"] - legacy["wall_s"])
                      / max(production["accepted"], 1), 4)),
        },
    }
    text = json.dumps(payload, indent=1)
    if args.out:
        out = Path(os.path.expanduser(args.out))
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text + "\n", encoding="utf-8")
        print(f"wrote {out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
