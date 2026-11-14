#!/usr/bin/env python3
"""Reproducible benchmark for the production capture storage path.

Measures, on a *read-only* source of real or synthetic view triplets:

* bytes per accepted view after lossless PNG compression (and the ratio versus
  the uncompressed PGM triplet the legacy layout writes);
* encode+write wall time per view, throughput and the size of the stored
  session;
* peak process RSS while a bounded :class:`~tools.wizard.ring_buffer.
  BoundedRingBuffer` holds its 256 MiB budget and views are being stored;
* full verification (re-hash + decode + pixel-digest) cost.

The source directory is never modified.  Output goes only to ``--out``.

Usage::

    .venv/bin/python -m tools.wizard.storage_bench \
        --groups ~/einstar-calibration-03/groups \
        --manifest ~/einstar-calibration-03/manifest.json \
        --out /tmp/prod-bench --views 60 --json-out /tmp/prod-60.json

    .venv/bin/python -m tools.wizard.storage_bench --estimate-views 60
"""

from __future__ import annotations

# ``python tools/wizard/storage_bench.py`` puts this directory first on
# sys.path, where the package's own ``types.py`` shadows the stdlib module that
# argparse and others import.  Drop the script directory before importing them.
if __name__ == "__main__":                      # pragma: no cover - CLI only
    import os as _os
    import sys as _sys
    _here = _os.path.dirname(_os.path.abspath(__file__))
    _sys.path[:] = [entry for entry in _sys.path
                    if _os.path.abspath(entry or ".") != _here]

import argparse
import json
import os
from pathlib import Path
import resource
import shutil
import statistics
import sys
import time
from typing import Any

import numpy as np

try:  # normal package import
    from . import session_io
    from .ring_buffer import BoundedRingBuffer, DEFAULT_CAPACITY_BYTES
    from .types import FILE_CAM, read_pgm
except ImportError:  # pragma: no cover - executed as a path script
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from tools.wizard import session_io
    from tools.wizard.ring_buffer import BoundedRingBuffer, DEFAULT_CAPACITY_BYTES
    from tools.wizard.types import FILE_CAM, read_pgm

_CAM_BY_INDEX = {int(index): cam for cam, index in FILE_CAM.items()}


def peak_rss_mb() -> float:
    """Peak RSS of this process so far (Linux: KiB)."""
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def select_views(groups_dir: Path, count: int | None) -> list[Path]:
    """Evenly spread selection across the corpus (no pose bias)."""
    dirs = sorted(p for p in Path(groups_dir).iterdir()
                  if p.is_dir() and p.name.isdigit())
    if not dirs:
        raise SystemExit(f"no view directories in {groups_dir}")
    if count is None or count >= len(dirs):
        return dirs
    if count <= 0:
        raise SystemExit("--views must be > 0")
    step = len(dirs) / float(count)
    picked = [dirs[min(len(dirs) - 1, int(i * step))] for i in range(count)]
    # de-duplicate while keeping the even spread
    seen: set[Path] = set()
    out = []
    for path in picked:
        if path not in seen:
            seen.add(path)
            out.append(path)
    return out


def read_view(view_dir: Path) -> tuple[dict[str, np.ndarray], int]:
    """Read the three PGMs of one view (orientation already applied)."""
    images: dict[str, np.ndarray] = {}
    pgm_bytes = 0
    for path in sorted(view_dir.glob("cam*_*.pgm")) + sorted(
            view_dir.glob("cam*.pgm")):
        name = path.stem  # cam0 / cam1 / cam2
        try:
            index = int(name.replace("cam", ""))
        except ValueError:
            continue
        cam = _CAM_BY_INDEX.get(index)
        if cam is None or cam in images:
            continue
        images[cam] = read_pgm(path)
        pgm_bytes += int(path.stat().st_size)
    if not images:
        raise SystemExit(f"{view_dir}: no readable camera PGM")
    return images, pgm_bytes


def load_observations(manifest: Path | None) -> dict[int, dict]:
    if manifest is None or not Path(manifest).is_file():
        return {}
    try:
        doc = json.loads(Path(manifest).read_text())
    except (OSError, ValueError):
        return {}
    out: dict[int, dict] = {}
    for entry in doc.get("accepted") or []:
        try:
            out[int(entry.get("index", entry.get("view_id", -1)))] = dict(entry)
        except (TypeError, ValueError):
            continue
    return out


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(fraction * (len(ordered) - 1)))))
    return ordered[index]


def compare_codecs(groups_dir: str | Path, *, views: int = 24,
                   manifest: str | Path | None = None) -> dict:
    """Measure lossless codecs on real frames (bytes and encode/decode time).

    PNG levels 1/3/9 use the same OpenCV encoder the store uses.  The zstd
    rows shell out to the ``zstd`` CLI once per frame, which includes a process
    spawn — a deliberately fair-to-zstd handicap; it still loses on ratio at
    equal speed.  Decode is measured for PNG only (zstd is not a candidate).
    """
    import subprocess

    selection = select_views(Path(groups_dir), views)
    rows: dict[str, list[float]] = {
        "pgm": [], "png1": [], "png3": [], "png9": [],
        "zstd3": [], "zstd9": [], "zstd19": []}
    times: dict[str, list[float]] = {
        "png1": [], "png3": [], "png9": [], "zstd3": [], "zstd9": [], "zstd19": []}
    decode: dict[str, list[float]] = {"png1": [], "png3": [], "png9": []}
    has_zstd = shutil.which("zstd") is not None

    def zstd(image: np.ndarray, level: int) -> bytes:
        proc = subprocess.run(["zstd", f"-{level}", "-q", "-c", "--no-progress"],
                              input=image.tobytes(), stdout=subprocess.PIPE,
                              check=True)
        return proc.stdout

    for view_dir in selection:
        images, pgm_bytes = read_view(view_dir)
        rows["pgm"].append(pgm_bytes)
        for level, key in ((1, "png1"), (3, "png3"), (9, "png9")):
            t0 = time.perf_counter()
            encoded = [session_io.encode_frame_png(img, level)
                       for img in images.values()]
            times[key].append(time.perf_counter() - t0)
            rows[key].append(sum(len(blob) for blob in encoded))
            t0 = time.perf_counter()
            for img, blob in zip(images.values(), encoded):
                assert np.array_equal(session_io.decode_frame_png(blob), img)
            decode[key].append(time.perf_counter() - t0)
        if has_zstd:
            for level, key in ((3, "zstd3"), (9, "zstd9"), (19, "zstd19")):
                t0 = time.perf_counter()
                rows[key].append(sum(len(zstd(img, level)) for img in images.values()))
                times[key].append(time.perf_counter() - t0)
    count = len(selection)
    result: dict[str, Any] = {"views": count, "frames": count * 3,
                              "zstd_available": has_zstd,
                              "pgm_bytes_per_view": rows["pgm"][0] if rows["pgm"] else 0,
                              "codecs": {}}
    for key in rows:
        if key == "pgm" or not rows[key]:
            continue
        entry = {"bytes_per_view": statistics.mean(rows[key]),
                 "ratio_vs_pgm": (rows["pgm"][0] / statistics.mean(rows[key])
                                  if rows[key] else 0.0),
                 "encode_ms_per_view": 1000 * statistics.mean(times[key])}
        if key.startswith("png"):
            entry["decode_ms_per_view"] = 1000 * statistics.mean(decode[key])
        result["codecs"][key] = entry
    return result


def run(groups_dir: str | Path, out_dir: str | Path, *,
        views: int | None = 60, png_level: int = 3,
        ring_bytes: int = DEFAULT_CAPACITY_BYTES,
        manifest: str | Path | None = None,
        measure_ring: bool = True,
        overwrite: bool = True) -> dict:
    """Store ``views`` real triplets through the production path and measure."""
    groups = Path(groups_dir)
    out = Path(out_dir)
    if out.exists() and overwrite:
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)
    selection = select_views(groups, views)
    observations = load_observations(Path(manifest) if manifest else None)
    if measure_ring:
        ring = BoundedRingBuffer(capacity_bytes=ring_bytes)
    else:  # pragma: no cover - benchmark convenience
        ring = None
    rss_start = peak_rss_mb()
    session = session_io.create(out, {"bench": "storage_bench",
                                      "source_groups": str(groups)},
                                storage="production")
    store = session_io.open_store(session, png_level=png_level)
    per_view: list[float] = []
    pgm_total = 0
    stored_total = 0
    observed_at = 0
    for order, view_dir in enumerate(selection):
        t0 = time.perf_counter()
        images, pgm_bytes = read_view(view_dir)
        pgm_total += pgm_bytes
        if ring is not None:
            ring.push(images, key=order, timestamp_s=order * 0.102)
        observation = dict(observations.get(int(view_dir.name)) or {})
        observation.setdefault("index", int(view_dir.name))
        observation.setdefault("bench_source", str(view_dir))
        record = store.add_images(
            order, images, observation, view_id=int(view_dir.name),
            group_index=order,
            timestamp_s=(None if not observation.get("timestamp")
                         else float(observation["timestamp"])))
        stored_total += int(record["stored_bytes"])
        per_view.append(time.perf_counter() - t0)
        observed_at = order + 1
    store_bytes = session_io.tree_bytes(session.root)
    verify_start = time.perf_counter()
    verification = session_io.verify_views(session)
    verify_s = time.perf_counter() - verify_start
    summary = session_io.storage_summary(session)
    ring_stats = ring.stats() if ring is not None else None
    result = {
        "source_groups": str(groups),
        "source_views_available": len([p for p in groups.iterdir()
                                       if p.is_dir() and p.name.isdigit()]),
        "views": observed_at,
        "png_level": int(png_level),
        "pgm_bytes_per_view": (pgm_total / observed_at) if observed_at else 0.0,
        "stored_bytes_per_view": (stored_total / observed_at) if observed_at else 0.0,
        "ratio_vs_pgm": ((pgm_total / stored_total) if stored_total else 0.0),
        "session_tree_bytes": int(store_bytes),
        "session_bytes_per_view": (store_bytes / observed_at) if observed_at else 0.0,
        "encode_s_per_view": statistics.mean(per_view) if per_view else 0.0,
        "encode_s_p50": percentile(per_view, 0.5),
        "encode_s_p95": percentile(per_view, 0.95),
        "encode_views_per_s": (observed_at / sum(per_view)) if per_view else 0.0,
        "encode_stored_mb_per_s": ((stored_total / 1e6) / sum(per_view)
                                   if per_view else 0.0),
        "verify_s": verify_s,
        "verify_ok": bool(verification.get("ok")),
        "verify_checked_files": int(verification.get("checked") or 0),
        "verify_views_per_s": (observed_at / verify_s) if verify_s else 0.0,
        "summary": summary,
        "ring": ring_stats,
        "peak_rss_mb": peak_rss_mb(),
        "peak_rss_mb_start": rss_start,
        "fs": os.statvfs(session.root).f_bsize,
    }
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--groups", help="source view directory (read-only)")
    ap.add_argument("--manifest", help="session manifest.json for observations")
    ap.add_argument("--out", default="/tmp/einstar-prod-bench",
                    help="scratch output directory (created/removed)")
    ap.add_argument("--views", type=int, default=60,
                    help="views to store (0 = all); with --compare-codecs the "
                         "number of sampled views (default 24)")
    ap.add_argument("--png-level", type=int, default=session_io.DEFAULT_PNG_LEVEL)
    ap.add_argument("--ring-mb", type=float, default=256.0,
                    help="bounded ring budget to hold during the run")
    ap.add_argument("--no-ring", action="store_true")
    ap.add_argument("--estimate-views", type=int, default=None,
                    help="only print the a-priori production estimate")
    ap.add_argument("--compare-codecs", action="store_true",
                    help="measure PNG/zstd on real frames instead of storing")
    ap.add_argument("--json-out", default="")
    args = ap.parse_args(argv)
    if args.estimate_views is not None:
        print(json.dumps(session_io.estimate_session_bytes(args.estimate_views),
                         indent=1))
        return 0
    if args.compare_codecs:
        if not args.groups:
            ap.error("--groups is required with --compare-codecs")
        result = compare_codecs(args.groups, views=(args.views or 24),
                                manifest=args.manifest or None)
        text = json.dumps(result, indent=1, default=str)
        print(text)
        if args.json_out:
            Path(args.json_out).write_text(text + "\n")
        return 0
    if not args.groups:
        ap.error("--groups is required unless --estimate-views is used")
    result = run(args.groups, args.out,
                 views=(args.views if args.views else None),
                 png_level=args.png_level,
                 ring_bytes=int(args.ring_mb * 1024 * 1024),
                 manifest=args.manifest or None,
                 measure_ring=not args.no_ring)
    text = json.dumps(result, indent=1, default=str)
    print(text)
    if args.json_out:
        Path(args.json_out).write_text(text + "\n")
    return 0 if result["verify_ok"] else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
