#!/usr/bin/env python3
"""Full-corpus, memory-bounded feature extraction for Einstar raw recordings.

Pipeline
--------
1. ``einstar-stream-frames --pipe --single-pass --fast-pacing`` reads the
   ``.e1r`` recording once, reassembles every three-camera trigger group and
   writes the frames to stdout as a 64-byte-header binary stream.
2. This driver reads that stream in the parent, writes each complete group's
   three grayscale frames into one bounded raw file under ``--temp-dir``
   (tmpfs), and submits a small task (paths + wire metadata) to a process pool.
3. A worker loads the three frames, runs the existing detector
   (``tools.wizard.detection.detect_one`` / ``calib.detect``) on every camera,
   computes quality metrics and the left/right factory-intrinsics PnP pose, and
   deletes the group's temp file.  Rows are written to group-range bucket files
   and merged in ascending group id at the end.

Backpressure comes from the OS pipe: once ``--max-inflight-groups`` tasks are
pending the parent stops reading, the C++ pipe buffer fills, and the reader
blocks.  RAM/tmpfs are therefore O(in-flight), never O(corpus).  No images are
stored for the corpus.

Detection failures still produce a row (zero marker counts, no pose) so the
corpus denominator is known.  The C++ trailer digest/counts are checked against
the emitted group/frame counts; a mismatch fails the run and the partial JSONL
is renamed ``.partial``.

Row schema (one JSON object per line, sorted by ``group``)::

    schema             "einstar-offline-group-features/1"
    group              int   absolute dense group id (same as frame_%06d)
    device_group       int   raw trigger-group counter from the wire header
    host_time_ns       int   real capture-clock timestamp of the group (min of
                             the three frame completions, recorded in the .e1r)
    frame_ids          [3]   per-camera device slot ids
    frames             [3]   {sha256, fnv1a64, width, height} per camera
                             (USB stream order 0=right IR, 1=left IR, 2=colour)
    marker_counts      {left,right,tex}  detected lattice markers (0..40)
    uv                 {left,right,tex}  [[x,y],...] marker PIXEL coordinates,
                             sorted by marker label (v,u) exactly like the live
                             manifest image_points
    uv_labels          {left,right,tex}  [[u,v],...] labels aligned with ``uv``
    image_region       {left,right,tex}  {centroid_px:[x,y], bbox:[x0,y0,x1,y1],
                             cell:[col,row], cell_name:"top-right", ...} from the
                             detected marker pixels (3x3 image cells, row 0 top)
    quality            {left,right,tex}  plate-ROI metrics identical to
                             calib.guidance.evaluate_frame: active (1/0),
                             area_frac (convex hull / image), focus (Laplacian
                             variance on the marker bbox + 24 px), contrast
                             (p95-p5 on that ROI), saturation (whole image,
                             fraction >= 250); null when no detection
    whole_frame        {left,right,tex}  {mean,std,p05,p50,p99} whole-image
                             statistics (not part of the quality rule)
    pose               null | {R:[9], t:[3] mm, camera:"left"|"right",
                             reprojection_rms_px, plate_centre_px}
    shared_markers_ir  int   identical lattice labels seen by both IR cameras
    assignment_ok      bool  both IR cameras have the full 40-marker,
                             non-mirrored (det H > 0) lattice assignment
"""

from __future__ import annotations

import argparse
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
import hashlib
import json
import multiprocessing as mp
import os
# Keep BLAS/OpenCV single-threaded inside each worker: 16-24 worker processes
# times a 32-thread OpenCV pool otherwise oversubscribes the CPU badly.
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
from pathlib import Path
import resource
import shutil
import struct
import subprocess
import sys
import time
from typing import Any

import cv2
import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from calib import geometry, plate  # noqa: E402
from calib.recon_profile import load as load_profile  # noqa: E402
from tools.wizard.detection import detect_one  # noqa: E402

# ---------------------------------------------------------------------------
# Pipe protocol (see docs/calibration/streaming_replay.md)
# ---------------------------------------------------------------------------
# 64-byte little-endian header, payload follows:
#   u32 magic ("FRM1"=0x314D5246 frame, "END1"=0x31444E45 terminator)
#   u16 version, u16 reserved
#   u32 group, u32 camera, u32 frame_id, u32 width, u32 height, u32 reserved
#   u64 device_group, u64 host_time_ns
#   u32 payload_bytes, u32 reserved, u64 fnv1a64(pixels)
PIPE_HEADER = struct.Struct("<IHHIIIIIIQQIIQ")
PIPE_FRAME_MAGIC = 0x314D5246
PIPE_END_MAGIC = 0x31444E45
PIPE_VERSION = 1
FRAME_WIDTH = 1280
FRAME_HEIGHT = 1024
FRAME_BYTES = FRAME_WIDTH * FRAME_HEIGHT
GROUP_BYTES = FRAME_BYTES * 3
CAMERA_NAMES = ("right", "left", "tex")
CAMERA_PROFILE_NAMES = ("ir_right", "ir_left", "color")
CELL_NAMES = (
    ("top-left", "top-centre", "top-right"),
    ("middle-left", "middle-centre", "middle-right"),
    ("bottom-left", "bottom-centre", "bottom-right"),
)

SCHEMA_ROW = "einstar-offline-group-features/1"
SCHEMA_MANIFEST = "einstar-offline-feature-manifest/1"
BUCKET_SIZE = 500

# Worker-side globals (populated by the pool initializer).
_WORKER: dict[str, Any] = {}


def read_exact(stream, size: int) -> bytes:
    """Read exactly ``size`` bytes from a pipe; raise EOFError on a short read."""
    view = bytearray(size)
    view_mv = memoryview(view)
    got = 0
    while got < size:
        count = stream.readinto(view_mv[got:])
        if not count:
            raise EOFError(f"pipe closed with {size - got} byte(s) outstanding")
        got += count
    return bytes(view)


def read_exact_into(stream, view: memoryview) -> None:
    """Fill ``view`` from the pipe, tolerating short reads."""
    got = 0
    size = len(view)
    while got < size:
        count = stream.readinto(view[got:])
        if not count:
            raise EOFError(f"pipe closed with {size - got} byte(s) outstanding")
        got += count


def sha256_file(path: Path, chunk: int = 8 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def peak_rss_kb() -> int:
    try:
        with open("/proc/self/status", encoding="ascii") as handle:
            for line in handle:
                if line.startswith("VmHWM:"):
                    return int(line.split()[1])
    except OSError:
        pass
    return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)


def _init_worker(profile_path: str) -> None:
    cv2.setNumThreads(1)
    profile = load_profile(profile_path)
    _WORKER["cameras"] = {
        name: profile.camera(name).intrinsics()
        for name in CAMERA_PROFILE_NAMES
    }
    _WORKER["points"] = plate.board_points_mm()
    _WORKER["index"] = {(float(u), float(v)): i
                        for i, (u, v) in enumerate(plate.lattice_uv())}
    _WORKER["n_lattice"] = len(plate.lattice_uv())


def _roi(gray: np.ndarray, pixels: list[list[float]] | np.ndarray,
         pad: int = 24) -> np.ndarray:
    """Marker bounding box + 24 px padding, exactly like calib.guidance._roi."""
    if pixels is None or len(pixels) == 0:
        return gray
    array = np.asarray(pixels, float)
    h, w = gray.shape[:2]
    x0 = max(0, int(np.floor(array[:, 0].min())) - pad)
    x1 = min(w, int(np.ceil(array[:, 0].max())) + pad)
    y0 = max(0, int(np.floor(array[:, 1].min())) - pad)
    y1 = min(h, int(np.ceil(array[:, 1].max())) + pad)
    if x1 - x0 < 8 or y1 - y0 < 8:
        return gray
    return gray[y0:y1, x0:x1]


def _focus_metric(gray: np.ndarray,
                  pixels: list[list[float]] | np.ndarray) -> float:
    """Laplacian variance on the plate ROI (calib.guidance.focus_metric)."""
    roi = _roi(gray, pixels)
    return float(cv2.Laplacian(roi.astype(np.float32), cv2.CV_32F).var())


def _quality(gray: np.ndarray, pixels: list[list[float]]) -> dict[str, Any]:
    """Plate-ROI quality metrics identical to calib.guidance.evaluate_frame.

    ``quality`` keeps exactly the live-session keys so the cache is comparable
    with decisions.jsonl / D7: ``active`` (1/0), ``area_frac`` (convex hull of
    detected markers), ``focus`` and ``contrast`` on the padded marker ROI, and
    whole-image ``saturation`` (fraction >= 250).  Undefined values are null
    when no detection exists.  Whole-frame statistics live under
    ``whole_frame`` and are not part of the quality rule.
    """
    quality: dict[str, Any] = {
        "active": 1.0 if pixels else 0.0,
        "area_frac": None,
        "focus": None,
        "contrast": None,
        "saturation": float(np.mean(gray >= 250)),
    }
    if pixels:
        array = np.asarray(pixels, np.float32)
        quality["area_frac"] = float(
            cv2.contourArea(cv2.convexHull(array))) / float(gray.size)
        roi = _roi(gray, array)
        quality["contrast"] = float(np.percentile(roi, 95) -
                                    np.percentile(roi, 5))
        quality["focus"] = _focus_metric(gray, array)
    return quality


def _whole_frame(gray: np.ndarray) -> dict[str, float]:
    """Extra whole-frame statistics (not used by the live quality rule)."""
    flat = gray.reshape(-1)
    p05, p50, p99 = np.percentile(flat, [5.0, 50.0, 99.0])
    return {
        "mean": float(flat.mean()),
        "std": float(flat.std()),
        "p05": float(p05),
        "p50": float(p50),
        "p99": float(p99),
    }


def _image_region(pixels: list[list[float]]) -> dict[str, Any]:
    """Marker centroid/bbox and the 3x3 image cell that contains the centroid."""
    if not pixels:
        return {"centroid_px": None, "bbox": None, "cell": None,
                "cell_name": None}
    array = np.asarray(pixels, float)
    array = array[np.isfinite(array).all(axis=1)]
    if len(array) == 0:
        return {"centroid_px": None, "bbox": None, "cell": None,
                "cell_name": None}
    cx, cy = (float(array[:, 0].mean()), float(array[:, 1].mean()))
    bbox = [float(array[:, 0].min()), float(array[:, 1].min()),
            float(array[:, 0].max()), float(array[:, 1].max())]
    col = min(2, max(0, int(cx * 3.0 / FRAME_WIDTH)))
    row = min(2, max(0, int(cy * 3.0 / FRAME_HEIGHT)))
    return {"centroid_px": [cx, cy], "bbox": bbox, "cell": [col, row],
            "cell_name": CELL_NAMES[row][col]}


def _pose(uv_by_camera: dict[str, list[list[float]]],
          pixels_by_camera: dict[str, list[list[float]]]) -> tuple[
              dict[str, Any] | None, str | None]:
    """PnP from the left IR camera, falling back to the right.

    Returns ``(pose, error)``.  A malformed observation (wrong point count,
    non-finite coordinates, degenerate/collinear points - which makes OpenCV
    raise ``cv2.error``) must never abort a corpus scan: it yields
    ``pose=None`` plus a short error note instead.
    """
    points = _WORKER["points"]
    index = _WORKER["index"]
    errors: list[str] = []
    for name, profile_name in (("left", "ir_left"), ("right", "ir_right")):
        try:
            uv = uv_by_camera.get(name) or []
            pixels = pixels_by_camera.get(name) or []
            if not uv and not pixels:
                continue  # camera saw nothing: not a malformed observation
            if len(uv) < 12 or len(pixels) < 12:
                errors.append(f"{name}: only {len(uv)} labelled markers (<12)")
                continue
            obj = []
            img = []
            for (u, v), pixel in zip(uv, pixels):
                key = (float(u), float(v))
                if key not in index:
                    continue
                # Reject non-finite values before OpenCV sees them.
                if not (np.isfinite(key[0]) and np.isfinite(key[1]) and
                        np.isfinite(float(pixel[0])) and
                        np.isfinite(float(pixel[1]))):
                    errors.append(f"{name}: non-finite marker coordinate")
                    break
                obj.append(points[index[key]])
                img.append(pixel)
            if len(obj) < 12:
                if not errors:
                    errors.append(f"{name}: only {len(obj)} labelled markers")
                continue
            obj_array = np.asarray(obj, float)
            img_array = np.asarray(img, float)
            if not (np.isfinite(obj_array).all() and
                    np.isfinite(img_array).all()):
                errors.append(f"{name}: non-finite PnP input")
                continue
            if np.linalg.matrix_rank(obj_array - obj_array.mean(axis=0)) < 2:
                errors.append(f"{name}: degenerate (collinear) marker points")
                continue
            camera = _WORKER["cameras"][profile_name]
            pose = geometry.solve_pnp(obj_array, img_array,
                                      np.asarray(camera.K, float),
                                      np.asarray(camera.kc, float))
            if pose is None:
                errors.append(f"{name}: solvePnP returned None")
                continue
            prediction = geometry.project(
                np.asarray(camera.K, float), np.asarray(camera.kc, float),
                (pose.R @ obj_array.T).T + pose.t)
            residual = img_array - prediction
            rms = float(np.sqrt(np.mean(np.sum(residual * residual, axis=1))))
            # Board origin (plate centre) projected through the solved pose.
            centre = geometry.project(np.asarray(camera.K, float),
                                      np.asarray(camera.kc, float),
                                      np.asarray(pose.t, float).reshape(1, 3))
            return {
                "R": [float(x) for x in np.asarray(pose.R, float).reshape(-1)],
                "t": [float(x) for x in np.asarray(pose.t, float).reshape(-1)],
                "camera": name,
                "reprojection_rms_px": rms,
                "plate_centre_px": [float(centre[0][0]),
                                    float(centre[0][1])],
            }, None
        except (cv2.error, ValueError, TypeError, np.linalg.LinAlgError,
                FloatingPointError, IndexError) as exc:
            errors.append(f"{name}: {type(exc).__name__}: {exc}")
            continue
    return None, (errors[0] if errors else None)


def _process_group(task: tuple) -> dict[str, Any]:
    """Worker entry point: measure/detect/PnP one complete group, then delete it.

    Every per-camera step is exception-safe: one malformed frame (bad
    detection, degenerate pose, non-finite data) produces a row with zero
    markers / ``pose=None`` and an ``errors`` note, never a worker crash.
    """
    (group, device_group, host_ns, frame_ids, fnvs, temp_file) = task
    try:
        blob = Path(temp_file).read_bytes()
        if len(blob) != GROUP_BYTES:
            raise RuntimeError(f"group {group}: temp file has {len(blob)} "
                               f"bytes, expected {GROUP_BYTES}")
        view = memoryview(blob)
        frames = []
        uv_by_camera: dict[str, list[list[float]]] = {}
        pixels_by_camera: dict[str, list[list[float]]] = {}
        quality_by_camera: dict[str, Any] = {}
        whole_by_camera: dict[str, Any] = {}
        region_by_camera: dict[str, Any] = {}
        marker_counts: dict[str, int] = {}
        errors: dict[str, str] = {}
        assignment_ok = True
        for camera in range(3):
            name = CAMERA_NAMES[camera]
            start = camera * FRAME_BYTES
            frame_view = view[start:start + FRAME_BYTES]
            gray = np.frombuffer(frame_view, dtype=np.uint8).reshape(
                FRAME_HEIGHT, FRAME_WIDTH)
            started = time.perf_counter()
            uv_by_camera[name] = []
            pixels_by_camera[name] = []
            marker_counts[name] = 0
            try:
                outcome = detect_one(gray)
                observation = outcome.observation
                if observation is not None:
                    # Same order as the live manifest image_points / guidance:
                    # sorted by marker label (v, then u).  Non-finite markers
                    # are dropped (and reported) instead of reaching OpenCV.
                    points = sorted(observation.points.items(),
                                    key=lambda kv: (kv[0][1], kv[0][0]))
                    finite = []
                    dropped = 0
                    for key, value in points:
                        coordinates = (float(key[0]), float(key[1]),
                                       float(value[0]), float(value[1]))
                        if all(np.isfinite(coordinates)):
                            finite.append((key, value))
                        else:
                            dropped += 1
                    if dropped:
                        errors[name] = (
                            f"dropped {dropped} non-finite marker(s)")
                    uv_by_camera[name] = [[float(k[0]), float(k[1])]
                                          for k, _ in finite]
                    pixels_by_camera[name] = [[float(v[0]), float(v[1])]
                                              for _, v in finite]
                    marker_counts[name] = len(finite)
                    region_by_camera[name] = _image_region(
                        pixels_by_camera[name])
                    if camera in (0, 1):
                        full = (int(observation.n_lattice) ==
                                int(_WORKER["n_lattice"]) and
                                float(np.linalg.det(np.asarray(
                                    observation.homography, float))) > 0.0)
                        assignment_ok = assignment_ok and full
                else:
                    region_by_camera[name] = _image_region([])
            except (cv2.error, ValueError, TypeError,
                    np.linalg.LinAlgError, FloatingPointError, IndexError) as exc:
                errors[name] = f"detect/region: {type(exc).__name__}: {exc}"
                uv_by_camera[name] = []
                pixels_by_camera[name] = []
                marker_counts[name] = 0
                region_by_camera[name] = _image_region([])
            try:
                quality_by_camera[name] = _quality(
                    gray, pixels_by_camera[name])
                whole_by_camera[name] = _whole_frame(gray)
            except (cv2.error, ValueError, TypeError,
                    np.linalg.LinAlgError, FloatingPointError, IndexError) as exc:
                quality_by_camera[name] = {
                    "active": 0.0 if not pixels_by_camera[name] else 1.0,
                    "area_frac": None, "focus": None, "contrast": None,
                    "saturation": None}
                whole_by_camera[name] = {}
                errors[name] = (errors.get(name, "") +
                                f" quality: {type(exc).__name__}: {exc}").strip()
            frames.append({
                "sha256": hashlib.sha256(frame_view).hexdigest(),
                # The C++ pass only computes FNV when asked (--digests); the
                # worker always records a sha256, which is authoritative.
                "fnv1a64": int(fnvs[camera]) if fnvs[camera] else None,
                "width": FRAME_WIDTH,
                "height": FRAME_HEIGHT,
                "detect_ms": (time.perf_counter() - started) * 1000.0,
            })
        pose, pose_error = _pose(uv_by_camera, pixels_by_camera)
        if pose_error:
            errors["pose"] = pose_error
        left = {tuple(round(float(c), 3) for c in pair)
                for pair in uv_by_camera.get("left") or []}
        right = {tuple(round(float(c), 3) for c in pair)
                 for pair in uv_by_camera.get("right") or []}
        return {
            "schema": SCHEMA_ROW,
            "group": int(group),
            "device_group": int(device_group),
            "host_time_ns": int(host_ns),
            "frame_ids": [int(value) for value in frame_ids],
            "frames": frames,
            "marker_counts": marker_counts,
            # Pixels sorted by marker label (v,u), exactly like the live
            # manifest image_points; labels are kept alongside for pose work.
            "uv": pixels_by_camera,
            "uv_labels": uv_by_camera,
            "image_region": region_by_camera,
            "quality": quality_by_camera,
            "whole_frame": whole_by_camera,
            "pose": pose,
            "shared_markers_ir": len(left & right),
            "assignment_ok": bool(assignment_ok),
            "errors": errors,
        }
    finally:
        try:
            os.unlink(temp_file)
        except OSError:
            pass


def _write_bucket(directory: Path, group: int, row: dict[str, Any]) -> None:
    bucket = group // BUCKET_SIZE
    path = directory / f"part_{bucket:05d}.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, separators=(",", ":")) + "\n")


def _merge_buckets(directory: Path, output: Path) -> int:
    """Sort each group-range bucket, then concatenate in ascending order."""
    parts = sorted(directory.glob("part_*.jsonl"))
    rows = 0
    last_group = -1
    with output.open("w", encoding="utf-8") as out:
        for part in parts:
            entries = []
            with part.open(encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    row = json.loads(line)
                    entries.append((int(row["group"]), line))
            entries.sort(key=lambda item: item[0])
            for group, line in entries:
                if group <= last_group:
                    raise RuntimeError(
                        f"feature rows are not strictly ordered: {group} after "
                        f"{last_group}")
                last_group = group
                out.write(line + "\n")
                rows += 1
            part.unlink()
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--in", dest="input", required=True,
                        help="raw .e1r recording (read-only)")
    parser.add_argument("--out", dest="output", required=True,
                        help="feature cache directory (features.jsonl etc.)")
    parser.add_argument("--tool",
                        default=str(REPO_ROOT / "build/driver/einstar-stream-frames"),
                        help="einstar-stream-frames binary")
    parser.add_argument("--workers", type=int, default=16,
                        help="detector worker processes (default 16)")
    parser.add_argument("--max-groups", type=int, default=0,
                        help="stop after N complete groups (0 = all)")
    parser.add_argument("--queue-depth", type=int, default=32,
                        help="outstanding transport reads in the C++ reader")
    parser.add_argument("--max-inflight-groups", type=int, default=128,
                        help="groups whose temp files may exist at once "
                             "(default 128, ~0.5 GB on tmpfs)")
    parser.add_argument("--temp-dir", default="/tmp/einstar-offline-frames",
                        help="bounded temp directory for group chunks (tmpfs)")
    parser.add_argument("--profile",
                        default="<private-inputs>/einstar-profiles/"
                                "factory-e10.json",
                        help="factory reconstruction profile for pose")
    parser.add_argument("--paced", action="store_true",
                        help="keep the virtual USB pacing model (slower; for "
                             "equivalence checks)")
    parser.add_argument("--skip-sha256", action="store_true",
                        help="skip the recording sha256 (quick benchmarks)")
    parser.add_argument("--sha256", default="",
                        help="reuse a known recording sha256 (hex)")
    parser.add_argument("--sha256-file", default="",
                        help="read the recording sha256 from a file (first "
                             "token); avoids re-reading 52 GB")
    parser.add_argument("--json", default="",
                        help="C++ report path (default OUT/einstar-report.json)")
    parser.add_argument("--log", default="",
                        help="C++ stderr log (default OUT/einstar-stderr.log)")
    args = parser.parse_args(argv)

    input_path = Path(args.input).resolve()
    if not input_path.is_file():
        print(f"input recording not found: {input_path}", file=sys.stderr)
        return 2
    tool = Path(args.tool).resolve()
    if not tool.is_file():
        print(f"streaming tool not found: {tool}", file=sys.stderr)
        return 2
    profile_path = Path(args.profile)
    if not profile_path.is_file():
        print(f"factory profile not found: {profile_path}", file=sys.stderr)
        return 2

    output_dir = Path(args.output).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    features_path = output_dir / "features.jsonl"
    manifest_path = output_dir / "manifest.json"
    report_path = Path(args.json).resolve() if args.json else \
        output_dir / "einstar-report.json"
    log_path = Path(args.log).resolve() if args.log else \
        output_dir / "einstar-stderr.log"
    for stale in output_dir.glob("part_*.jsonl"):
        stale.unlink()
    partial_features = output_dir / "features.jsonl.partial"
    if partial_features.exists():
        partial_features.unlink()

    temp_root = Path(args.temp_dir).resolve()
    temp_run = temp_root / f"run-{os.getpid()}"
    if temp_run.exists():
        shutil.rmtree(temp_run)
    temp_run.mkdir(parents=True)
    workers = max(1, args.workers)
    inflight_limit = max(1, args.max_inflight_groups)

    started_wall = time.time()
    print(f"[extract] recording {input_path} "
          f"({input_path.stat().st_size} bytes)", file=sys.stderr)
    sha256 = None
    sha256_source = "none"
    if args.sha256:
        sha256 = args.sha256.strip().lower()
        sha256_source = "argument"
    elif args.sha256_file:
        token = Path(args.sha256_file).read_text(encoding="utf-8").split()
        if not token:
            print(f"empty sha256 file: {args.sha256_file}", file=sys.stderr)
            return 2
        sha256 = token[0].strip().lower()
        sha256_source = f"file:{args.sha256_file}"
    if sha256 is None and not args.skip_sha256:
        sha_started = time.time()
        sha256 = sha256_file(input_path)
        sha256_source = "computed"
        print(f"[extract] sha256 {sha256} in {time.time() - sha_started:.1f}s",
              file=sys.stderr)
    elif sha256 is not None:
        print(f"[extract] sha256 {sha256} reused from {sha256_source}",
              file=sys.stderr)

    command = [str(tool), "--in", str(input_path), "--pipe", "--single-pass",
               "--queue-depth", str(args.queue_depth), "--json", str(report_path)]
    if not args.paced:
        command.append("--fast-pacing")
    if args.max_groups > 0:
        command += ["--max-groups", str(args.max_groups)]
    print("[extract] " + " ".join(command), file=sys.stderr)

    context = mp.get_context("spawn")
    executor = ProcessPoolExecutor(max_workers=workers, mp_context=context,
                                   initializer=_init_worker,
                                   initargs=(str(profile_path),))
    futures: dict[Any, int] = {}
    rows_written = 0
    frames_read = 0
    bytes_read = 0
    end_counts = None
    last_progress = time.time()
    last_progress_frames = 0
    log_handle = log_path.open("wb")
    process = subprocess.Popen(command, stdout=subprocess.PIPE,
                               stderr=log_handle)
    assert process.stdout is not None
    group_buffer = bytearray(GROUP_BYTES)
    group_view = memoryview(group_buffer)
    current_group: int | None = None
    current_device_group = 0
    current_host_ns = 0
    current_frames: list[tuple[int, int]] = []  # (frame_id, fnv1a64)

    groups_with_errors = 0

    def collect(done) -> None:
        nonlocal rows_written, groups_with_errors
        for future in done:
            group_id = futures.pop(future)
            try:
                row = future.result()
            except BaseException as exc:  # surface the group id, then fail
                raise RuntimeError(
                    f"worker failed for group {group_id}: "
                    f"{type(exc).__name__}: {exc}") from exc
            if int(row["group"]) != group_id:
                raise RuntimeError(f"worker returned group {row['group']} for "
                                   f"task {group_id}")
            _write_bucket(output_dir, group_id, row)
            rows_written += 1
            if row.get("errors"):
                groups_with_errors += 1

    try:
        while True:
            header_bytes = read_exact(process.stdout, PIPE_HEADER.size)
            (magic, version, _reserved, group, camera, frame_id, width, height,
             _reserved3, device_group, host_ns, payload_bytes, _reserved2,
             fnv1a64) = PIPE_HEADER.unpack(header_bytes)
            if magic == PIPE_END_MAGIC:
                end_counts = (device_group, host_ns)
                if current_group is not None:
                    raise RuntimeError(
                        f"pipe ended inside group {current_group}")
                break
            if magic != PIPE_FRAME_MAGIC:
                raise RuntimeError(f"bad pipe magic 0x{magic:08X}")
            if version != PIPE_VERSION:
                raise RuntimeError(f"unsupported pipe version {version}")
            if (width, height) != (FRAME_WIDTH, FRAME_HEIGHT) or \
                    payload_bytes != FRAME_BYTES:
                raise RuntimeError(
                    f"unexpected frame geometry {width}x{height} "
                    f"payload={payload_bytes}")
            if current_group is None:
                current_group = group
                current_device_group = device_group
                current_host_ns = host_ns
                current_frames = []
            elif group != current_group:
                raise RuntimeError(
                    f"group {current_group} ended after "
                    f"{len(current_frames)} frame(s); group {group} started")
            if camera != len(current_frames):
                raise RuntimeError(
                    f"group {group}: camera {camera} arrived out of order "
                    f"(expected {len(current_frames)})")
            offset = camera * FRAME_BYTES
            read_exact_into(process.stdout,
                            group_view[offset:offset + payload_bytes])
            current_frames.append((frame_id, fnv1a64))
            frames_read += 1
            bytes_read += payload_bytes
            if camera != 2:
                continue
            # Complete group: 3.9 MB on tmpfs, one task, bounded in-flight.
            chunk_path = temp_run / f"g{group:06d}.raw"
            chunk_path.write_bytes(group_buffer)
            futures[executor.submit(
                _process_group,
                (group, current_device_group, current_host_ns,
                 tuple(frame_id for frame_id, _ in current_frames),
                 tuple(fnv for _, fnv in current_frames),
                 str(chunk_path)))] = group
            current_group = None
            if len(futures) >= inflight_limit:
                done, _ = wait(futures, return_when=FIRST_COMPLETED)
                collect(done)
            else:
                done, _ = wait(futures, timeout=0,
                               return_when=FIRST_COMPLETED)
                if done:
                    collect(done)

            now = time.time()
            if rows_written and now - last_progress >= 10.0:
                rate = (frames_read - last_progress_frames) / 3.0 / \
                    max(1e-9, now - last_progress)
                elapsed = now - started_wall
                groups_read = frames_read // 3
                print(f"[extract] rows={rows_written} groups_read={groups_read} "
                      f"frames={frames_read} ({bytes_read / 1e6:.0f} MB), "
                      f"{rate:.1f} rows/s, {elapsed:.0f}s, "
                      f"inflight={len(futures)}", file=sys.stderr)
                last_progress = now
                last_progress_frames = frames_read

        while futures:
            done, _ = wait(futures, return_when=FIRST_COMPLETED)
            collect(done)
        return_code = process.wait()
    except BaseException:
        process.kill()
        process.wait()
        executor.shutdown(wait=False, cancel_futures=True)
        raise
    finally:
        log_handle.close()
        executor.shutdown(wait=True)
        shutil.rmtree(temp_run, ignore_errors=True)

    manifest: dict[str, Any] = {}
    if end_counts is None:
        end_counts = (None, None)
    elapsed = time.time() - started_wall
    report = json.loads(report_path.read_text()) if report_path.is_file() else {}
    verification = (report.get("verification") or {})
    replay = (report.get("replay") or {})
    trailer_ok = bool(verification.get("trailer_verified"))
    require_trailer = args.max_groups == 0  # partial runs stop before EOF
    cpp_groups = int(replay.get("groups_written", -1))
    cpp_frames = int(replay.get("frames_written", -1))
    consistent = (rows_written == cpp_groups and frames_read == cpp_frames and
                  end_counts[0] == rows_written and end_counts[1] == frames_read)
    manifest = {
        "schema": SCHEMA_MANIFEST,
        "generated_at_unix": time.time(),
        "row_schema": SCHEMA_ROW,
        "recording": {
            "path": str(input_path),
            "size_bytes": input_path.stat().st_size,
            "sha256": sha256,
            "sha256_source": sha256_source,
        },
        "verification": verification,
        "tool": {
            "command": command,
            "exit_code": return_code,
            "stderr_log": str(log_path),
            "report": str(report_path),
            "trailer_verified": trailer_ok,
            "trailer_required": require_trailer,
            "peak_rss_kb": int((report.get("memory") or {}).get("peak_rss_kb",
                                                               0)),
            "wall_seconds": report.get("timing", {}).get("wall_seconds"),
            "throughput_mbps": report.get("timing", {}).get("throughput_mbps"),
        },
        "extraction": {
            "workers": workers,
            "max_inflight_groups": inflight_limit,
            "fast_pacing": not args.paced,
            "max_groups": args.max_groups,
            "partial": args.max_groups > 0,
            "groups": rows_written,
            "frames": frames_read,
            "payload_bytes": bytes_read,
            "elapsed_seconds": elapsed,
            "groups_per_second": rows_written / max(1e-9, elapsed),
            "megapixels_per_second": (frames_read * FRAME_BYTES / 1e6) /
                                     max(1e-9, elapsed),
            "peak_rss_orchestrator_kb": peak_rss_kb(),
            "peak_rss_worker_kb": int(
                resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss),
            "cv2_version": cv2.__version__,
            "numpy_version": np.__version__,
            "python_version": sys.version.split()[0],
            "groups_with_errors": groups_with_errors,
            "consistent_with_tool": bool(consistent),
        },
    }

    if (require_trailer and not trailer_ok) or not consistent:
        reason = ("trailer verification failed" if not trailer_ok else
                  "row/frame counts do not match the tool report")
        print(f"[extract] FAILED: {reason} "
              f"(rows={rows_written} cpp_groups={cpp_groups} "
              f"frames={frames_read} cpp_frames={cpp_frames} "
              f"end={end_counts})", file=sys.stderr)
        partial = output_dir / "features.jsonl.partial"
        if features_path.exists():
            features_path.rename(partial)
        manifest["failed"] = reason
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
        return 1

    rows = _merge_buckets(output_dir, features_path)
    if rows != rows_written:
        print(f"[extract] FAILED: merged {rows} rows, expected {rows_written}",
              file=sys.stderr)
        return 1
    without = 0
    with features_path.open(encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            if not any(row["marker_counts"].values()):
                without += 1
    manifest["extraction"]["groups_without_detection"] = without
    manifest["extraction"]["features_path"] = str(features_path)
    manifest["extraction"]["features_bytes"] = features_path.stat().st_size
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"[extract] wrote {rows} rows -> {features_path} "
          f"({features_path.stat().st_size / 1e6:.1f} MB) in {elapsed:.1f}s; "
          f"trailer verified; manifest {manifest_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
