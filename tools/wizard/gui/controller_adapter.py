"""Adapter between the GUI and the calibration controller.

The GUI only needs five calls (``begin/ingest/pause/resume/finish/cancel``) plus
``snapshot()``.  The real controller lives in ``tools.wizard.controller``; when it
is not importable (unit tests, layout work) a deterministic demo controller with
the same surface is used instead, so the window can always be developed and
rendered.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import numpy as np

_HERE = Path(__file__).resolve()
for _p in (_HERE.parents[2], _HERE.parents[3]):   # tools/wizard, repo root
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


def real_controller_available() -> bool:
    try:
        from wizard import controller  # noqa: F401  (tools/wizard on sys.path)
        return hasattr(controller, "WizardController")
    except Exception:
        return False


def make_controller(*, factory_profile: str, session_dir: str, mode: str = "high",
                    source_kind: str = "replay", **kwargs):
    """Return the real controller when available, else the demo one."""
    if real_controller_available():
        from wizard.controller import WizardController
        return WizardController(factory_profile=factory_profile,
                                session_dir=session_dir, mode=mode, **kwargs)
    return DemoController(factory_profile=factory_profile, session_dir=session_dir,
                          mode=mode, source_kind=source_kind)


class DemoController:
    """Deterministic stand-in: accepts the first N distinct poses, then rejects.

    It exists so the GUI can be built and snapshot-tested without the real
    controller; it is never used for calibration and says so in the UI.
    """

    def __init__(self, *, factory_profile: str, session_dir: str, mode: str = "high",
                 source_kind: str = "replay", **_):
        self.factory_profile = factory_profile
        self.session_dir = Path(session_dir)
        self.mode = mode
        self.source_kind = source_kind
        self.state = "idle"
        self.started = 0.0
        self.accepted: list[dict] = []
        self.rejected: list[dict] = []
        self.last_decision: dict = {}
        self.last_points: dict = {"left": np.zeros((0, 2)), "right": np.zeros((0, 2)),
                                  "tex": np.zeros((0, 2))}
        self.last_pose = None

    # -- controller surface --------------------------------------------
    def begin(self) -> dict:
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.state = "recording"
        self.started = time.time()
        return {"ok": True, "demo": True}

    def ingest(self, group):
        # crude but deterministic: a view is "new" if it is far from all accepted
        pts = self._detect(group)
        n_left = len(pts["left"])
        pose = None
        if n_left >= 12:
            pose = np.array([n_left * 0.01, n_left * 0.02, 400.0])
        accepted = False
        reasons = []
        if self.state != "recording":
            reasons.append("not recording")
        elif n_left < 12:
            reasons.append(f"only {n_left} markers")
        elif pose is not None and self.accepted and any(
                np.linalg.norm(pose - a["pose"]) < 1.5 for a in self.accepted):
            reasons.append("duplicate pose")
        else:
            accepted = True
        if pose is not None:
            self.last_pose = pose
        self.last_points = pts
        rec = {"accepted": accepted, "reasons": reasons,
               "predicted_gain": 0.05 if accepted else None,
               "view_id": len(self.accepted) if accepted else None,
               "quality": self._quality(group)}
        self.last_decision = rec
        if accepted:
            self.accepted.append({"pose": pose, "points": pts, "quality": rec["quality"]})
        else:
            self.rejected.append(rec)
        return _DemoDecision(rec)

    def snapshot(self) -> dict:
        dims = self._coverage()
        sat = sum(1 for d in dims.values() if d["satisfied"])
        ready = sat == len(dims)
        return {
            "state": self.state,
            "error": None,
            "demo": True,
            "source": {"kind": self.source_kind, "path": str(self.session_dir),
                       "serial": "DEMO", "firmware": "", "note": "demo controller"},
            "session": {"dir": str(self.session_dir), "started_s": self.started,
                        "elapsed_s": max(0.0, time.time() - self.started),
                        "factory_sha256": "demo",
                        "raw_recording": str(self.session_dir / "raw.e1r"),
                        "groups_dir": str(self.session_dir / "groups"),
                        "n_materialised": len(self.accepted)},
            "counts": {"accepted": len(self.accepted), "rejected": len(self.rejected),
                       "redundant": sum(1 for r in self.rejected
                                        if "duplicate" in " ".join(r["reasons"])),
                       "by_reason": {}},
            "last_decision": self.last_decision,
            "last_pose": (None if self.last_pose is None else
                          {"t_mm": self.last_pose.tolist(),
                           "distance_mm": float(self.last_pose[2]),
                           "tilt_deg": 6.0, "yaw_deg": 3.0, "roll_deg": -178.0}),
            "detections": {k: v.tolist() for k, v in self.last_points.items()},
            "markers": {k: len(v) for k, v in self.last_points.items()},
            "shared_markers_ir": min(len(self.last_points["left"]),
                                     len(self.last_points["right"])),
            "assignment_ok": True,
            "quality": (self.last_decision.get("quality") or {}),
            "capture": {"state": "running" if self.state == "recording" else self.state,
                        "fps": 10.0, "groups_written": len(self.accepted) + len(self.rejected),
                        "drops": 0, "throughput_mbps": 39.4, "disk_free_gb": 70.0,
                        "groups_on_disk": 0, "ring_size": 60, "stop_reason": None},
            "coverage": {"ready": ready, "satisfied": sat, "total": len(dims),
                         "dimensions": dims,
                         "views": [{"distance_mm": float(a["pose"][2]),
                                    "tilt_x_deg": 6.0, "tilt_y_deg": 0.0,
                                    "roll_deg": -178.0,
                                    "image_uv": [0.35 + 0.3 * (i % 3) / 3.0,
                                                 0.45 + 0.1 * (i // 3) / 3.0]}
                                   for i, a in enumerate(self.accepted)]},
            "observability": self._observability(),
            "guidance": {"instruction": ("Enough views — press Finish"
                                         if ready else "Move to a new pose"),
                         "detail": "demo controller: coverage is simulated",
                         "priority": "demo", "target": {}},
            "readiness": {"ready": ready,
                          "missing": [k for k, d in dims.items() if not d["satisfied"]],
                          "reason": "demo"},
        }

    def pause(self) -> None:
        self.state = "paused"

    def resume(self) -> None:
        self.state = "recording"

    def finish(self) -> dict:
        self.state = "done"
        return {"ok": True, "demo": True, "profile": None}

    def cancel(self) -> dict:
        self.state = "cancelled"
        return {"ok": True, "demo": True}

    # -- internals -----------------------------------------------------
    def _detect(self, group):
        out = {}
        for key in ("left", "right", "tex"):
            img = group.images.get(key)
            if img is None:
                out[key] = np.zeros((0, 2))
                continue
            try:
                from calib.detect import detect_grid_auto
                obs = detect_grid_auto(np.ascontiguousarray(img, dtype=np.uint8))
                out[key] = (np.zeros((0, 2)) if obs is None
                            else np.asarray(list(obs.points.values()), float))
            except Exception:
                out[key] = np.zeros((0, 2))
        return out

    def _quality(self, group):
        img = group.images.get("left")
        if img is None:
            return {"mean": 0.0, "saturation_frac": 0.0, "focus": 0.0, "blur": 0.0}
        import cv2
        return {"mean": float(img.mean()),
                "saturation_frac": float((img >= 250).mean()),
                "focus": float(cv2.Laplacian(img, cv2.CV_32F).var()),
                "blur": 0.4}

    def _coverage(self):
        n = len(self.accepted)
        return {
            "distance_bands": {"required": "3 bands", "measured": f"{min(n,3)}",
                               "satisfied": n >= 3, "deficit": "",
                               "hint": "Move closer" if n < 3 else ""},
            "tilt_two_axes": {"required": "both axes", "measured": "1 axis",
                              "satisfied": False, "deficit": "second axis",
                              "hint": "Tilt the plate left/right"},
            "roll_sectors": {"required": "4 sectors", "measured": "1",
                             "satisfied": False, "deficit": "3",
                             "hint": "Rotate the plate in its plane"},
            "image_regions": {"required": "9 regions", "measured": "1",
                              "satisfied": False, "deficit": "8",
                              "hint": "Move the plate toward an image corner"},
            "stereo_overlap": {"required": "12 markers", "measured": "40",
                               "satisfied": True, "deficit": "", "hint": ""},
            "colour_participation": {"required": "12 markers", "measured": "40",
                                     "satisfied": True, "deficit": "", "hint": ""},
            "quality": {"required": "sharp", "measured": "ok", "satisfied": True,
                        "deficit": "", "hint": ""},
            "stationarity": {"required": "stable", "measured": "ok", "satisfied": True,
                             "deficit": "", "hint": ""},
        }

    def _observability(self):
        if not self.accepted:
            return {}
        names = ["left.fx", "left.fy", "left.cx", "left.cy", "left.k1",
                 "right.fx", "right.fy", "right.cx", "right.cy", "right.k1",
                 "right.rx", "right.ry", "right.rz", "right.tx", "right.ty", "right.tz"]
        params = {n: {"sigma": (0.02 if n.endswith(("rx", "ry", "rz")) else 6.8),
                      "unit": "deg" if n.endswith(("rx", "ry", "rz")) else "px",
                      "verdict": ("constrained" if n.endswith(("rx", "rz"))
                                  else "weak" if n == "right.ry" else "unobservable")}
                  for n in names}
        return {"parameters": params, "condition": 1.057e6, "rank": 26, "n_params": 26,
                "free": names, "gates": {"stereo_rotation": {"pass": True, "detail": "demo"}}}


class _DemoDecision:
    def __init__(self, payload):
        self._payload = payload

    def as_dict(self):
        return dict(self._payload)


class _LocalReplaySource:
    """Temporary replay fallback so the GUI can be exercised before
    ``tools/wizard/sources.py`` lands.  Deleted as soon as it exists."""

    def __init__(self, frames: str, loop: bool = False):
        from wizard import types
        self._types = types
        self.root = Path(frames)
        self.loop = loop
        self.info = types.SourceInfo(kind="replay", path=str(self.root))
        self.deterministic = True

    def __iter__(self):
        from wizard import types
        idx = 0
        while True:
            emitted = 0
            for n in sorted(int(p.stem.split("_")[1]) for p in
                            self.root.glob("frame_*_cam1.pgm")):
                try:
                    left = types.read_pgm(self.root / f"frame_{n:06d}_cam1.pgm")
                    right = types.read_pgm(self.root / f"frame_{n:06d}_cam0.pgm")
                    raw = types.read_pgm(self.root / f"frame_{n:06d}_cam2.pgm")
                except (OSError, ValueError):
                    continue
                bgr = types.debayer_bgr(raw)
                yield types.FrameGroup(
                    index=idx, device_group=None, timestamp_s=idx / 10.0,
                    images={"left": types.orient_left(left),
                            "right": types.orient_right(right),
                            "tex": types.grey_from_bgr(bgr)},
                    slot_ids={}, complete=True, source="replay", label=str(n),
                    colour_bgr=bgr)
                idx += 1
                emitted += 1
            if not self.loop or emitted == 0:
                return

    def close(self):
        return None


def open_source(kind: str, *, frames: str | None = None, recording: str | None = None,
                session_dir: str | None = None, scene: str | None = None,
                pace_hz: float = 0.0, loop: bool = False):
    """Build a frame source for the GUI (real sources when available)."""
    try:
        from wizard import sources
    except Exception:
        sources = None
    if sources is None:
        if kind == "replay" and frames:
            return _LocalReplaySource(frames, loop=loop)
        raise RuntimeError("tools/wizard/sources.py is not available yet; "
                           "only --source replay --frames DIR works")
    if kind == "vendor":
        from wizard.vendor_source import VendorCorpusSource
        return VendorCorpusSource(frames)
    if kind == "replay":
        return sources.ReplayDirSource(frames, loop=loop)
    if kind == "recording":
        return sources.RecordingSource(recording)
    if kind == "synthetic":
        return sources.SyntheticSource(scene or "default")
    if kind == "live":
        # Keep the newest 120 groups on disk (12 s at 10 Hz, ~470 MB); the
        # controller copies accepted triplets into SESSION/groups and the raw
        # recording keeps every group.
        return sources.LiveSource(session_dir, capture_args={"ring": 120},
                                  profile=None)
    raise ValueError(f"unknown source kind {kind!r}")
