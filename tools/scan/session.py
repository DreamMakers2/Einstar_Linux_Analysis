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
from tools.track import projective as track_proj

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
    #: "projective" tracks on the engine's depth maps, restricted to the near
    #: object by a depth-only mask.  Measured against the plate's own PnP pose
    #: on real frames: median tilt error 0.24 deg at scale 0.5 (cloud 0.58) and
    #: 0.06 deg at scale 1.0.  It costs ~200 ms/frame of tracking against ~76 ms,
    #: which is ~13 % of the whole scan loop.  Falls back to "cloud" when the
    #: depth map or its rectified intrinsics are unavailable.
    tracking: str = "projective"
    #: cap on the ICP point count.  Measured at scale 0.5: 82 k masked points
    #: take 316 ms and 30 k take 87 ms with the same residual (0.095 mm) and
    #: rotation within 0.02 deg, so the cap buys ~3.6x for free.
    tracking_max_points: int = 30000
    #: disparity search range.  Its width dominates the matching cost, and the
    #: default 150 mm lower bound is below the scanner's working distance:
    #: measured 247 ms at 150-800 mm against 203 ms at 250-800 mm with the same
    #: valid fraction (42.5 % -> 42.9 %).
    min_depth_mm: float = 250.0
    max_depth_mm: float = 800.0
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

    @classmethod
    def from_project(cls, project_path, *, out_dir=None, voxel_mm=None,
                     trunc_mm=None, input_voxel_mm=None, scale=None,
                     **overrides) -> "ScanSession":
        """Re-open a saved scan for reprocessing, reusing its recorded poses.

        A project stores the frames directory, the reconstruction settings and
        every accepted pose, so a scan can be re-fused at a different voxel size
        without re-tracking.  That is what makes an offline refinement of a
        recorded scan possible (and it is deterministic: the poses are inputs,
        not re-estimated).
        """
        from tools.scan import export as scan_export
        doc = scan_export.load_project(project_path)
        cfg = dict(doc.get("config") or {})
        report = doc.get("report") or {}
        cfg["out_dir"] = str(Path(out_dir) if out_dir else
                             Path(cfg.get("out_dir") or ".") / "reprocessed")
        if voxel_mm is not None:
            cfg["voxel_mm"] = float(voxel_mm)
        if trunc_mm is not None:
            cfg["trunc_mm"] = float(trunc_mm)
        if input_voxel_mm is not None:
            cfg["input_voxel_mm"] = float(input_voxel_mm)
        if scale is not None:
            cfg["scale"] = float(scale)
        cfg.update(overrides)
        config = ScanConfig(**cfg)
        session = cls(config)
        poses = [np.asarray(p, float) for p in (report.get("poses") or [])]
        session.replay_poses = poses
        session.replay_order = [int(s.get("index")) for s in
                                (report.get("frames_status") or [])
                                if s.get("integrated")]
        session.source_project = str(project_path)
        return session

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
        self._prev_depth = None
        self._intrinsics = None
        self._lost_run = 0
        self._recoveries = 0
        #: when set (reprocessing), these poses are used instead of tracking
        self.replay_poses: list[np.ndarray] | None = None
        self.replay_order: list[int] | None = None
        self.source_project: str | None = None
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

    def preview_mesh(self, target_faces: int = 6000):
        """A decimated mesh of the fused volume, for the live 3-D view.

        Re-extracting at a coarser resolution is not possible from a fixed
        voxel grid, so the full mesh is decimated by taking every k-th face and
        remapping the vertices it still uses.  It is a preview only: the final
        mesh written by finish() is never decimated.
        """
        from tools.fuse import tsdf as _tsdf
        if self._volume is None or not self._frames:
            return None
        try:
            pts = np.vstack([np.asarray(c.points, float) for c in self._frames])
            nrm = np.vstack([np.asarray(c.normals, float) for c in self._frames])
            verts, faces = _tsdf.extract_mesh(self._volume, points=pts, normals=nrm)
        except Exception:
            return None
        if len(faces) == 0:
            return None
        stride = max(1, int(np.ceil(len(faces) / float(max(1, target_faces)))))
        faces = faces[::stride]
        used = np.unique(faces)
        remap = -np.ones(len(verts), np.int64)
        remap[used] = np.arange(len(used))
        return verts[used], remap[faces]

    # -- internals --------------------------------------------------------
    def _reconstruct(self, index: int) -> tuple[Path, dict]:
        out = self._work / f"recon_{index:06d}"
        out.mkdir(parents=True, exist_ok=True)
        cmd = [str(self.config.recon_binary), "reconstruct",
               "--frames", str(self.config.frames_dir), "--id", str(index),
               "--profile", str(self.config.profile), "--out", str(out),
               "--scale", f"{self.config.scale:g}",
               "--min-confidence", f"{self.config.min_confidence:g}",
               # only the depth map and the report are read back by the session;
               # the diagnostic maps cost ~24 ms/frame in a live loop
               "--maps", "depth",
               "--min-depth", f"{self.config.min_depth_mm:g}",
               "--max-depth", f"{self.config.max_depth_mm:g}"]
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
        if self.replay_poses is not None:
            # reprocessing: the recorded pose is authoritative, and tracking is
            # skipped entirely (it was already validated when the scan was made)
            order = self.replay_order or []
            if len(self._poses) >= len(self.replay_poses):
                status.reason = "no recorded pose left for this frame"
                self._statuses.append(status)
                return status
            pose = np.asarray(self.replay_poses[len(self._poses)], float)
            status.tracking_ok = True
            status.tracking_reason = "replayed pose"
            self._integrate(cloud, pose, status)
            return status
        depth_frame = None
        if str(getattr(cfg, "tracking", "projective")) == "projective":
            depth_path = ply.parent / "recon_depth_mm.tiff"
            if depth_path.is_file():
                try:
                    # K must come from the reconstruction report: the depth map
                    # is on the SCALED rectified grid, and the profile's
                    # full-resolution intrinsics are wrong for it (that mistake
                    # once made the tracker report a tenth of the true motion)
                    raw = track_proj.load_depth(depth_path)
                    # isolate the object: a static background damps a moving
                    # object's motion (measured: 0.47 deg -> 0.06 deg tilt
                    # error) and the mask also cuts the ICP point count
                    depth_frame = track_proj.mask_to_object(
                        raw, track_proj.object_mask(raw))
                except Exception:
                    depth_frame = None
        if depth_frame is not None and self._prev_depth is not None:
            t1 = time.time()
            r = track_proj.align_projective(
                self._prev_depth, depth_frame,
                max_points=int(getattr(cfg, "tracking_max_points", 0) or 0))
            status.tracking_seconds = time.time() - t1
            status.tracking_ok = bool(r["ok"])
            status.tracking_reason = str(r["reason"])
            # defensive reads: a tracker result must never abort a scan
            status.translation_mm = float(r.get("translation_mm") or 0.0)
            status.rotation_deg = float(r.get("rotation_deg") or 0.0)
            status.rms_mm = r.get("rms_mm")
            status.condition = r.get("condition")
            try:
                n_px = max(1, len(depth_frame.points()[0]))
                status.inlier_ratio = float(r.get("inliers") or 0) / n_px
            except Exception:
                status.inlier_ratio = 0.0
            if r.get("ok"):
                pose = self._poses[-1] @ np.linalg.inv(
                    np.asarray(r.get("transform", np.eye(4)), float))
                if self._lost_run:
                    # this frame re-anchored to the last accepted one after a run
                    # of failures; say so rather than presenting it as seamless
                    status.tracking_reason = "recovered"
                    self._recoveries += 1
                    self._lost_run = 0
            else:
                status.reason = f"tracking lost: {r.get('reason') or 'unknown'}"
                self._rejected += 1
                # keep the reference at the last ACCEPTED frame: updating it to a
                # rejected one makes every later frame align against a pose that
                # was never trusted, so a single glitch becomes a permanent loss
                self._lost_run += 1
                self._statuses.append(status)
                return status
            self._prev_depth = depth_frame
            self._integrate(cloud, pose, status)
            return status
        if depth_frame is not None:
            self._prev_depth = depth_frame
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
        self._integrate(cloud, pose, status)
        return status

    def _integrate(self, cloud, pose: np.ndarray, status: FrameStatus) -> None:
        """Fuse one cloud at ``pose``; shared by the live and replay paths."""
        cfg = self.config
        if cfg.min_confidence > 0:
            z = np.asarray(cloud.points, float)[:, 2]
            conf = np.clip(1.0 - np.abs(z - np.median(z)) / 50.0, 0.0, 1.0)
        else:
            conf = None
        world = (pose[:3, :3] @ np.asarray(cloud.points, float).T).T + pose[:3, 3]
        world_n = (pose[:3, :3] @ np.asarray(cloud.normals, float).T).T
        if self._volume is None:
            self._volume = tsdf.make_volume([world], voxel_mm=cfg.voxel_mm,
                                            margin_mm=25.0,
                                            max_voxels=60_000_000)
        t0 = time.time()
        tsdf.integrate(self._volume, world, world_n, trunc_mm=cfg.trunc_mm,
                       confidence=conf)
        status.fusion_seconds = time.time() - t0
        status.integrated = True
        status.ok = True
        self._poses.append(pose)
        self._frames.append(cloud)
        self._last_cloud_for_track = cloud
        self._last_cloud = cloud
        self._statuses.append(status)

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
        if self._recoveries:
            out["recoveries"] = int(self._recoveries)
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
