"""Scan session: record → depth → track → fuse → mesh, headless and GUI-ready.

One object drives both the command-line scanner and the Qt window, so what a
test exercises is what a user runs.  The reconstruction engine is the existing
C++ `recon_cli`; this module never re-implements depth.

Frame flow
----------
``process(index)``
    1. reconstruct frame ``index`` from the frames directory (C++ engine),
    2. load the point cloud and estimate normals,
    3. align it to the previous accepted cloud (tracking baseline),
    4. integrate it into the volume **only if** the tracking pair is trusted,
    5. return a status record (depth statistics, pose, tracking quality).

``finish()``
    extract the mesh, write an OBJ and a JSON report.

Untrusted poses
---------------
A frame whose alignment fails, or whose geometry is degenerate, is *not*
integrated.  The session counts it and reports it; fusion never silently absorbs
a wrong pose, which is the failure mode that corrupts a model beyond repair.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
from typing import Any, Sequence

import numpy as np

from tools.fuse import tsdf
from tools.scan import color as scan_color
from tools.scan import export as scan_export
from tools.track import baseline as track

__all__ = ["ScanConfig", "ScanSession", "FrameStatus"]


@dataclass
class ScanConfig:
    frames_dir: Path
    profile: Path
    out_dir: Path
    recon_binary: Path
    voxel_mm: float = 5.0
    trunc_mm: float | None = None
    input_voxel_mm: float = 8.0
    scale: float = 0.5
    #: Measured on real frames: a 0.4 threshold improves a *single frame's*
    #: plane RMS from 0.293-0.300 mm to 0.259-0.272 mm while keeping ~96 % of
    #: the plate points.  In the fusion path it is counter-productive - at
    #: scale 0.25 a 4-frame mesh fell from 17 133 to 2 233 vertices because the
    #: fused surface needs the points, and averaging already suppresses the
    #: low-confidence ones.  So the default is off here and the flag is exposed
    #: for single-frame analysis.
    min_confidence: float = 0.0
    #: grey-world white balance on the highlight set (measured cast 29 % -> 2.5 %)
    white_balance: bool = True
    max_frames: int | None = None
    keep_frames: bool = True
    ascii_ply: bool = True

    def __post_init__(self) -> None:
        self.frames_dir = Path(self.frames_dir)
        self.profile = Path(self.profile)
        self.out_dir = Path(self.out_dir)
        self.recon_binary = Path(self.recon_binary)


@dataclass
class FrameStatus:
    index: int
    ok: bool
    reason: str = ""
    depth_valid_fraction: float = float("nan")
    depth_p05_mm: float = float("nan")
    depth_p50_mm: float = float("nan")
    depth_p95_mm: float = float("nan")
    depth_sigma_p50_mm: float = float("nan")
    points: int = 0
    tracking_ok: bool = False
    tracking_reason: str = ""
    translation_mm: float = float("nan")
    rotation_deg: float = float("nan")
    rms_mm: float = float("nan")
    inlier_ratio: float = float("nan")
    condition: float = float("nan")
    recon_seconds: float = float("nan")
    tracking_seconds: float = float("nan")
    fusion_seconds: float = float("nan")
    integrated: bool = False

    def as_dict(self) -> dict:
        out = {}
        for key, value in self.__dict__.items():
            if isinstance(value, float):
                out[key] = None if not np.isfinite(value) else round(value, 4)
            else:
                out[key] = value
        return out


class ScanSession:
    """A scanning run over a frames directory."""

    def __init__(self, config: ScanConfig) -> None:
        self.config = config
        self.config.out_dir.mkdir(parents=True, exist_ok=True)
        self._work = Path(tempfile.mkdtemp(prefix="einstar-scan-",
                                           dir=str(self.config.out_dir)))
        self._frames: list[Any] = []
        self._poses: list[np.ndarray] = []
        self._statuses: list[FrameStatus] = []
        self._volume: tsdf.Volume | None = None
        self._rejected = 0
        self._started = time.time()
        self._last_cloud = None
        self._last_cloud_for_track = None
        self._colour: dict | None = None
        self._mesh = None
        self._finished: dict | None = None

    # -- properties -------------------------------------------------------
    @property
    def statuses(self) -> list[FrameStatus]:
        return list(self._statuses)

    @property
    def n_frames(self) -> int:
        return len(self._frames)

    @property
    def n_rejected(self) -> int:
        return int(self._rejected)

    def elapsed_s(self) -> float:
        return time.time() - self._started

    def snapshot(self) -> dict:
        """Everything a GUI needs to draw one update."""
        last = self._statuses[-1].as_dict() if self._statuses else {}
        depth = None
        cloud = None
        if self._last_cloud is not None:
            depth = {
                "p05_mm": last.get("depth_p05_mm"),
                "p50_mm": last.get("depth_p50_mm"),
                "p95_mm": last.get("depth_p95_mm"),
            }
            pts = np.asarray(self._last_cloud.points, float)
            if len(pts) > 4000:
                idx = np.linspace(0, len(pts) - 1, 4000).astype(int)
                pts = pts[idx]
            cloud = pts.round(1).tolist()
        volume = None
        if self._volume is not None:
            volume = {"voxels": self._volume.n_voxels,
                      "observed": int((self._volume.weight > 0).sum()),
                      "memory_mb": round(self._volume.memory_mb(), 1),
                      "voxel_mm": self._volume.voxel_mm}
        return {
            "frames_processed": len(self._statuses),
            "frames_integrated": len(self._frames),
            "frames_rejected": self.n_rejected,
            "elapsed_s": round(self.elapsed_s(), 2),
            "last": last,
            "depth": depth,
            "cloud": cloud,
            "volume": volume,
            "finished": self._finished is not None,
        }

    # -- internals --------------------------------------------------------
    def _reconstruct(self, index: int) -> tuple[Path, dict]:
        out = self._work / f"recon_{index:06d}"
        out.mkdir(parents=True, exist_ok=True)
        cmd = [str(self.config.recon_binary), "reconstruct",
               "--frames", str(self.config.frames_dir), "--id", str(index),
               "--profile", str(self.config.profile), "--out", str(out),
               "--scale", f"{self.config.scale:g}",
               "--min-confidence", f"{self.config.min_confidence:g}"]
        if self.config.ascii_ply:
            cmd.append("--ascii-ply")
        proc = subprocess.run(cmd, capture_output=True, text=True)
        report_path = out / "recon_report.json"
        report: dict = {"ok": False, "error": proc.stderr.strip()[-500:]}
        if report_path.is_file():
            try:
                report = json.loads(report_path.read_text())
            except Exception as exc:
                report = {"ok": False, "error": f"report unreadable: {exc}"}
        return out / "recon_cloud.ply", report

    def process(self, index: int) -> FrameStatus:
        cfg = self.config
        status = FrameStatus(index=index, ok=False)
        t0 = time.time()
        ply, report = self._reconstruct(index)
        status.recon_seconds = time.time() - t0
        depth = report.get("depth") or {}
        stereo = report.get("stereo") or {}
        status.depth_valid_fraction = float(depth.get("valid_fraction")
                                            or stereo.get("valid_fraction") or float("nan"))
        status.depth_p05_mm = float(depth.get("p05_mm") or float("nan"))
        status.depth_p50_mm = float(depth.get("p50_mm") or float("nan"))
        status.depth_p95_mm = float(depth.get("p95_mm") or float("nan"))
        status.depth_sigma_p50_mm = float(depth.get("sigma_p50_mm") or float("nan"))
        if not report.get("ok") or not ply.is_file():
            status.reason = str(report.get("error") or "reconstruction failed")
            self._statuses.append(status)
            return status
        try:
            cloud = track.load_clouds([ply], voxel_mm=cfg.input_voxel_mm)[0]
            # keep the engine's per-point colour (present when the profile has a
            # validated colour record) so the mesh can carry it
            try:
                raw_pts, raw_rgb = track.read_ply_coloured(ply)
                if raw_rgb is not None and len(raw_pts):
                    from scipy.spatial import cKDTree
                    _, near = cKDTree(raw_pts).query(cloud.points, k=1)
                    cloud.colors = np.asarray(raw_rgb, np.uint8)[near]
            except Exception:
                pass
        except Exception as exc:
            status.reason = f"cloud load failed: {exc}"
            self._statuses.append(status)
            return status
        status.points = len(cloud)
        self._last_cloud = cloud
        # -- tracking against the previous integrated frame
        pose = np.eye(4)
        if self._last_cloud_for_track is not None:
            t1 = time.time()
            pair = track.align_point_to_plane(self._last_cloud_for_track, cloud)
            status.tracking_seconds = time.time() - t1
            status.tracking_ok = bool(pair.ok)
            status.tracking_reason = pair.reason
            status.translation_mm = pair.translation_mm
            status.rotation_deg = pair.rotation_deg
            status.rms_mm = pair.rms_mm
            status.inlier_ratio = pair.inlier_ratio
            status.condition = pair.condition
            if pair.ok:
                # pair maps previous -> current; the world pose of the current
                # frame is the inverse of that, applied to the previous pose
                pose = self._poses[-1] @ np.linalg.inv(pair.transform)
            else:
                status.reason = f"tracking lost: {pair.reason}"
                self._rejected += 1
                self._statuses.append(status)
                return status
        # -- fusion
        if cfg.min_confidence > 0:
            conf = np.clip(1.0 - np.abs(np.asarray(cloud.points, float)[:, 2]
                                        - np.median(np.asarray(cloud.points, float)[:, 2]))
                           / 50.0, 0.0, 1.0)
        else:
            conf = None
        world = (pose[:3, :3] @ np.asarray(cloud.points, float).T).T + pose[:3, 3]
        world_n = (pose[:3, :3] @ np.asarray(cloud.normals, float).T).T
        if self._volume is None:
            self._volume = tsdf.make_volume([world], voxel_mm=cfg.voxel_mm,
                                            margin_mm=25.0,
                                            max_voxels=60_000_000)
        t2 = time.time()
        tsdf.integrate(self._volume, world, world_n, trunc_mm=cfg.trunc_mm,
                       confidence=conf)
        status.fusion_seconds = time.time() - t2
        status.integrated = True
        status.ok = True
        self._poses.append(pose)
        self._frames.append(cloud)
        self._last_cloud_for_track = cloud
        self._statuses.append(status)
        return status

    # the first frame has nothing to align to

    def finish(self, *, mesh: bool = True) -> dict:
        if self._finished is not None:
            return dict(self._finished)
        out: dict = {"frames": len(self._frames),
                     "rejected": int(self._rejected),
                     "elapsed_s": round(self.elapsed_s(), 2)}
        result: dict = {}
        if mesh and self._volume is not None:
            pts = np.vstack([np.asarray(c.points, float) for c in self._frames]) \
                if self._frames else None
            nrm = np.vstack([np.asarray(c.normals, float) for c in self._frames]) \
                if self._frames else None
            verts, faces = tsdf.extract_mesh(self._volume, points=pts, normals=nrm)
            colours = None
            try:
                coloured = [c for c in self._frames
                            if getattr(c, "colors", None) is not None
                            and len(c.colors) == len(c.points)]
                if coloured:
                    cpts = np.vstack([np.asarray(c.points, float) for c in coloured])
                    crgb = np.vstack([np.asarray(c.colors, np.uint8) for c in coloured])
                    from scipy.spatial import cKDTree
                    _, near = cKDTree(cpts).query(verts, k=1)
                    colours = crgb[near].astype(float)
                    if self.config.white_balance and len(colours) >= 64:
                        try:
                            wb = scan_color.estimate_white_balance(colours)
                            before = scan_color.neutrality(colours)
                            colours = scan_color.apply_white_balance(colours, wb)
                            after = scan_color.neutrality(colours)
                            self._colour = {
                                "white_balance": wb.as_dict(),
                                "neutrality_before": before,
                                "neutrality_after": after,
                                "clipped_fraction": float(np.mean(
                                    np.any(colours >= 254.5, axis=1))),
                            }
                        except Exception as exc:
                            self._colour = {"white_balance_error": str(exc)}
                    colours = np.clip(np.rint(colours), 0, 255).astype(np.uint8)
            except Exception:
                colours = None
            written = scan_export.export_mesh(
                self.config.out_dir, verts, faces, stem="scan",
                comment="einstar scan", colors=colours)
            result = {"obj": written.get("obj"), "ply": written.get("ply"),
                      "stl": written.get("stl"),
                      "vertices": int(len(verts)), "faces": int(len(faces))}
            self._mesh = (verts, faces)
        out.update(result)
        if self._colour is not None:
            out["colour"] = self._colour
        out["poses"] = [[[float(x) for x in row] for row in p] for p in self._poses]
        out["frames_status"] = [s.as_dict() for s in self._statuses]
        report = self.config.out_dir / "scan_report.json"
        report.write_text(json.dumps(out, indent=1))
        out["report"] = str(report)
        project = scan_export.save_project(
            self.config.out_dir / "scan_project.json", config=self.config,
            report=out, mesh={k: out.get(k) for k in ("obj", "ply", "stl")},
            extra={"voxel_mm": self.config.voxel_mm,
                   "frames": len(self._frames)})
        out["project"] = str(project)
        self._finished = out
        return dict(out)

    def close(self) -> None:
        if not self.config.keep_frames:
            shutil.rmtree(self._work, ignore_errors=True)
