"""Live-sweep frame loading, marker detection and the UV-assignment audit.

The two real captures are ``einstar-frames-v1`` recordings written by
``einstar-capture-frames``::

    <root>/frames_manifest.json         groups[]: group, device_group, stream0..2
    <root>/frame_%06d_cam{0,1,2}.pgm    P5 1280x1024 8-bit

Stream order is the USB order, not the calibration order:

    stream 0 = right IR,  rotated 180 deg
    stream 1 = left  IR,  already upright
    stream 2 = colour,    raw BayerRG, rotated 180 deg

``load_view`` returns a *calibration-oriented* grayscale image for each camera,
i.e. the image the factory ``CameraIntrinsics`` were calibrated for.

The plate has 40 lattice markers plus 4 large ones at (2,1), (4,1), (5,1) and
the off-lattice (3.5,3).  ``calib.detect`` recovers the lattice but returns
``big_markers = ()``, so this module re-derives the large-marker evidence and
audits the (u,v) assignment against it (``audit_assignment``).
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

from calib import detect, plate

#: stream index -> calibration camera name
STREAM_CAMERA = {0: "right", 1: "left", 2: "tex"}
CAMERA_STREAM = {v: k for k, v in STREAM_CAMERA.items()}

#: stream indices whose images are stored rotated by 180 degrees
ROTATED = {0: True, 1: False, 2: True}

#: large markers of the recovered plate (board units)
BIG_ON_LATTICE = ((2.0, 1.0), (4.0, 1.0), (5.0, 1.0))
BIG_OFF_LATTICE = (3.5, 3.0)

#: the four in-plane symmetries of the 8x5 lattice.  A lattice relabelling is a
#: rigid rotation of the planar board, so reprojection residuals cannot see it.
DIHEDRAL = {
    "id": lambda u, v: (u, v),
    "flip_u": lambda u, v: (7.0 - u, v),
    "flip_v": lambda u, v: (u, 4.0 - v),
    "rot180": lambda u, v: (7.0 - u, 4.0 - v),
}


# --------------------------------------------------------------------------
# manifest
# --------------------------------------------------------------------------

@dataclass
class Sweep:
    root: Path
    schema: str
    groups: list[dict]
    manifest: dict

    @property
    def name(self) -> str:
        return self.root.name

    def streams(self, i: int) -> dict[str, Path]:
        g = self.groups[i]
        return {STREAM_CAMERA[c]: self.root / Path(g[f"stream{c}"]).name
                for c in (0, 1, 2)}

    def group_ids(self) -> np.ndarray:
        return np.array([int(g["group"]) for g in self.groups], int)

    def device_groups(self) -> np.ndarray:
        return np.array([int(g["device_group"]) for g in self.groups], np.int64)

    def frame_ids(self, cam: int) -> np.ndarray:
        return np.array([int(g.get("frame_ids", {}).get(str(cam), -1))
                         for g in self.groups], int)


def load_sweep(root: str | Path) -> Sweep:
    root = Path(os.path.expanduser(str(root)))
    man = json.loads((root / "frames_manifest.json").read_text())
    if man.get("schema") != "einstar-frames-v1":
        raise ValueError(f"{root}: unexpected schema {man.get('schema')!r}")
    return Sweep(root, man["schema"], man["groups"], man)


# --------------------------------------------------------------------------
# images
# --------------------------------------------------------------------------

def load_view(streams: dict[str, Path], camera: str) -> np.ndarray:
    """Load one camera image, oriented and grayscale as calibrated."""
    cam = CAMERA_STREAM[camera]
    raw = cv2.imread(str(streams[camera]), cv2.IMREAD_UNCHANGED)
    if raw is None:
        raise FileNotFoundError(streams[camera])
    if raw.ndim != 2:
        raise ValueError(f"{streams[camera]}: expected single-channel PGM")
    if camera == "tex":
        bgr = cv2.cvtColor(raw, cv2.COLOR_BayerRG2BGR)
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    else:
        gray = raw
    if ROTATED[cam]:
        gray = cv2.rotate(gray, cv2.ROTATE_180)
    return np.ascontiguousarray(gray)


def image_size(streams: dict[str, Path]) -> tuple[int, int]:
    a = cv2.imread(str(streams["left"]), cv2.IMREAD_UNCHANGED)
    if a is None:
        raise FileNotFoundError(streams["left"])
    return int(a.shape[1]), int(a.shape[0])


# --------------------------------------------------------------------------
# detection + assignment audit
# --------------------------------------------------------------------------

@dataclass
class ViewDetection:
    camera: str
    n: int
    rms: float
    max_residual: float
    points: dict[tuple[float, float], np.ndarray]
    #: area of the blob matched to each canonical lattice label
    label_area: dict[tuple[float, float], float]
    #: the single largest blob not matched to a lattice label (candidate (3.5,3))
    off_blob: Optional[tuple[float, float, float]]
    #: every unassigned blob as (area, x, y)
    unassigned: list = None

    def as_dict(self) -> dict:
        return {
            "camera": self.camera, "n": int(self.n), "rms": float(self.rms),
            "max_residual": float(self.max_residual),
            "points": [[float(u), float(v), float(p[0]), float(p[1])]
                       for (u, v), p in sorted(self.points.items())],
            "label_area": [[float(u), float(v), float(a)]
                           for (u, v), a in sorted(self.label_area.items())],
            "off_blob": None if self.off_blob is None else [float(x) for x in self.off_blob],
            "unassigned": [[float(a), float(x), float(y)] for a, x, y in
                           (self.unassigned or [])],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ViewDetection":
        pts = {(float(u), float(v)): np.array([x, y], float)
               for u, v, x, y in d["points"]}
        area = {(float(u), float(v)): float(a) for u, v, a in d["label_area"]}
        off = None if d["off_blob"] is None else tuple(float(x) for x in d["off_blob"])
        un = [tuple(float(x) for x in r) for r in d.get("unassigned", [])]
        return cls(d["camera"], int(d["n"]), float(d["rms"]),
                   float(d["max_residual"]), pts, area, off, un)


def _best_blobs(gray: np.ndarray, obs, refine_mode: str):
    """Rebuild the blob list of the polarity/threshold that produced ``obs``."""
    ref = np.array([obs.points[k] for k in sorted(obs.points)], float)
    best, best_score = None, np.inf
    for invert in (False, True):
        for pct in (99.0, 99.4, 98.5):
            blobs = detect.detect_blobs(gray, invert=invert, percentile=pct,
                                        refine_mode=refine_mode)
            if not blobs:
                continue
            P = np.array([b.xy for b in blobs], float)
            d = np.linalg.norm(P[:, None, :] - ref[None, :, :], axis=2).min(0)
            score = float(np.max(d))
            if score < best_score:
                best, best_score = blobs, score
            if score < 1e-9:
                return blobs
    return best if best is not None else []


def detect_view(gray: np.ndarray, camera: str,
                refine_mode: str = "centroid") -> Optional[ViewDetection]:
    """Detect the 40 lattice markers and keep the large-marker evidence."""
    obs = detect.detect_grid_auto(gray, refine_mode=refine_mode)
    if obs is None:
        return None
    blobs = _best_blobs(gray, obs, refine_mode)
    P = np.array([b.xy for b in blobs], float) if blobs else np.zeros((0, 2))
    A = np.array([b.area for b in blobs], float) if blobs else np.zeros(0)
    label_area, used = {}, set()
    for k, p in obs.points.items():
        if len(P) == 0:
            break
        d = np.linalg.norm(P - p, axis=1)
        i = int(d.argmin())
        if d[i] < 3.0:
            label_area[k] = float(A[i])
            used.add(i)
    off, un = None, []
    if len(A):
        rest = [(A[i], P[i]) for i in range(len(A)) if i not in used]
        un = [(float(a), float(p[0]), float(p[1])) for a, p in rest]
        if rest:
            a, p = max(rest, key=lambda z: z[0])
            off = (float(a), float(p[0]), float(p[1]))
    return ViewDetection(camera, obs.n_lattice, float(obs.rms),
                         float(obs.max_residual), dict(obs.points), label_area, off, un)


# --------------------------------------------------------------------------
# assignment audit
# --------------------------------------------------------------------------

def relabel(points: dict, fn) -> dict:
    return {fn(u, v): p for (u, v), p in points.items()}


def homography_rms(points: dict) -> float:
    keys = sorted(points.keys())
    uv = np.array(keys, float)
    xy = np.array([points[k] for k in keys], float)
    if len(uv) < 4:
        return float("nan")
    H, _ = cv2.findHomography(uv, xy, 0)
    if H is None:
        return float("nan")
    pred = cv2.perspectiveTransform(uv.reshape(-1, 1, 2), H).reshape(-1, 2)
    return float(np.sqrt(np.mean(np.sum((pred - xy) ** 2, axis=1))))


def audit_assignment(det: ViewDetection) -> dict:
    """Test the four lattice relabellings against independent big-marker evidence.

    * reprojection residuals are *invariant* under all four maps (the 8x5
      lattice is symmetric and a planar relabelling is a rigid rotation), so
      the detector cannot be trusted on residuals alone;
    * the three on-lattice large markers carry ~3-4x the blob area of a small
      marker and sit at specific labels -- that breaks ``flip_u`` and the
      combined maps;
    * the off-lattice large marker at (3.5,3) is not among the 40 labels; the
      nearest large unassigned blob to its prediction breaks ``flip_v``.
    """
    out: dict = {"n": int(det.n), "rms_detected": float(det.rms), "hypotheses": {}}
    for name, fn in DIHEDRAL.items():
        out["hypotheses"][name] = {"homography_rms": homography_rms(relabel(det.points, fn))}
    onl = set(BIG_ON_LATTICE)
    areas = [det.label_area.get(k, float("nan")) for k in BIG_ON_LATTICE]
    out["big_on_lattice_area"] = [None if not np.isfinite(a) else float(a) for a in areas]
    others = [a for k, a in det.label_area.items() if tuple(k) not in onl]
    top = sorted(det.label_area.items(), key=lambda z: -z[1])[:3]
    out["top3_labels"] = [[float(u), float(v)] for (u, v), _ in top]
    out["top3_areas"] = [float(a) for _, a in top]
    small = float(np.median(others)) if others else None
    out["small_median_area"] = small
    out["area_ratio"] = (float(min(areas) / small)
                         if small and np.all(np.isfinite(areas)) else None)
    out["area_signature_ok"] = bool(
        len(top) == 3 and {tuple(k) for k, _ in top} == onl
        and others and np.all(np.isfinite(areas))
        and min(areas) > 1.8 * small)
    # off-lattice marker: is a *large* unassigned blob where (3.5,3) projects?
    out["off_blob"] = None if det.off_blob is None else [float(x) for x in det.off_blob]
    out["off_lattice_consistent"] = None
    out["off_lattice_dist_px"] = None
    if det.off_blob is not None and len(det.points) >= 6 and small:
        keys = sorted(det.points.keys())
        uv = np.array(keys, float)
        xy = np.array([det.points[k] for k in keys], float)
        H, _ = cv2.findHomography(uv, xy, 0)
        if H is not None:
            pred = cv2.perspectiveTransform(
                np.array([[BIG_OFF_LATTICE]], float), H).reshape(2)
            out["off_lattice_pred"] = [float(pred[0]), float(pred[1])]
            cands = [(float(a), float(x), float(y)) for a, x, y in
                     _unassigned(det)]
            big = [c for c in cands if c[0] >= 1.8 * small]
            use = big if big else cands
            if use:
                d = [float(np.hypot(x - pred[0], y - pred[1])) for _, x, y in use]
                out["off_lattice_dist_px"] = float(min(d))
                out["off_lattice_consistent"] = bool(min(d) < 6.0)
    out["verdict"] = _verdict(out)
    return out


def _unassigned(det: ViewDetection):
    """Blobs not matched to a lattice label, as (area, x, y)."""
    if not det.unassigned:
        return []
    return det.unassigned


def _verdict(out: dict) -> str:
    if out.get("n", 0) < 8:
        return "rejected_too_few_points"
    if not out.get("area_signature_ok"):
        return "ambiguous_labels"
    if out.get("off_lattice_consistent") is False:
        return "ambiguous_off_lattice"
    if out.get("off_lattice_consistent") is None:
        return "unchecked_off_lattice"
    return "unambiguous"


# --------------------------------------------------------------------------
# cached detection over a sweep
# --------------------------------------------------------------------------

def detect_sweep(sweep: Sweep, cameras: tuple[str, ...] = ("left", "right", "tex"),
                 cache: Optional[Path] = None, verbose: bool = False) -> dict:
    """Detect every group/camera; results are JSON-cacheable."""
    if cache is not None and Path(cache).exists():
        raw = json.loads(Path(cache).read_text())
        return {c: [None if v is None else ViewDetection.from_dict(v)
                    for v in raw[c]] for c in cameras}
    out: dict[str, list] = {c: [] for c in cameras}
    for i in range(len(sweep.groups)):
        streams = sweep.streams(i)
        for c in cameras:
            try:
                gray = load_view(streams, c)
            except Exception:
                out[c].append(None)
                continue
            d = detect_view(gray, c)
            out[c].append(d)
        if verbose and (i % 50 == 0):
            ok = {c: (out[c][-1] is not None) for c in cameras}
            print(f"  {sweep.name} {i}/{len(sweep.groups)} {ok}", flush=True)
    if cache is not None:
        Path(cache).parent.mkdir(parents=True, exist_ok=True)
        Path(cache).write_text(json.dumps(
            {c: [None if d is None else d.as_dict() for d in out[c]]
             for c in cameras}, separators=(",", ":")))
    return out
