#!/usr/bin/env python3
"""Three-camera calibration model for the SHINING 3D Einstar (EinScan10_01).

This module is the single entry point for turning the on-disk calibration
artefacts into a usable pinhole + Brown-Conrady model:

  * the three CCF records ``LeftCCF.txt`` / ``RightCCF.txt`` / ``TexCCF.txt``
    (520 / 520 / 264 bytes, raw little-endian ``float64``), optionally with the
    recovered integer key applied to the two IR records, and
  * the 6568-byte device-info dump, which additionally stores three *unkeyed*
    22-double camera records (``fx fy cx cy kc5 ???? R9 T3``).

Coordinate convention (OpenCV), identical for every camera
-----------------------------------------------------------
    X_cam = R @ X_world + T          (R row-major in the CCF, det = +1)
    x = X_cam[0] / X_cam[2],  y = X_cam[1] / X_cam[2]        (z forward)
    (xd, yd) = distort(x, y, dist)
    u = fx * xd + skew * yd + cx,   v = fy * yd + cy         (px)
    X_world = R.T @ (X_cam - T)

* Units: millimetres for everything 3-D, pixels for everything 2-D.
* Frame: each record's ``R, T`` map *world* -> *camera*; the three cameras of
  one recording therefore share a world frame.  There is no requirement that
  the world frame be attached to any particular camera; use
  :meth:`Calibration.relative_pose` to get camera-to-camera transforms.
* Distortion: Brown-Conrady / OpenCV, five coefficients ``(k1, k2, p1, p2, k3)``.
* Skew: the CCF stores the MATLAB Camera-Calibration-Toolbox ``alpha_c``
  (dimensionless).  In that toolbox the pixel skew is ``alpha_c * fx``, which
  is what :data:`SKEW_MODE_DEFAULT` uses; the alternative raw reading
  ``K[0,1] = alpha_c`` differs by < 0.1 px over the whole image and is
  available as ``skew_mode="raw"``.

Nothing in this file invents numbers: every value comes either from a parsed
record or from an explicit, documented assumption recorded in ``provenance``.
"""

from __future__ import annotations

import hashlib
import json
import math
import struct
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

# --------------------------------------------------------------------------
# constants / paths
# --------------------------------------------------------------------------

IMAGE_WIDTH = 1280
IMAGE_HEIGHT = 1024
IMAGE_SIZE = (IMAGE_WIDTH, IMAGE_HEIGHT)

REPO_ROOT = Path(__file__).resolve().parents[2]
CCF_DIR = REPO_ROOT / "sources/calibration/res_Einscan-E10_200x150"
DUMP_PATH = Path("<device-dump.bin>")

#: Camera names used everywhere.  ``tex`` is the colour camera.
CAMERA_NAMES = ("left", "right", "tex")

#: MATLAB CCToolbox skew is dimensionless and enters as ``K[0,1] = alpha_c*fx``.
SKEW_MODE_DEFAULT = "bouguet"
SKEW_MODES = ("bouguet", "raw")

DIST_NAMES = ("k1", "k2", "p1", "p2", "k3")

# --------------------------------------------------------------------------
# tools/ccf is the authority for the container layout and the IR key
# --------------------------------------------------------------------------

def _import_ccf():
    """Import ``tools/ccf/decode_ccf.py`` without requiring installation."""
    try:
        import decode_ccf as mod  # type: ignore
        return mod
    except ImportError:
        pass
    ccf_dir = REPO_ROOT / "tools/ccf"
    if str(ccf_dir) not in sys.path:
        sys.path.insert(0, str(ccf_dir))
    import decode_ccf as mod  # type: ignore
    return mod


ccf = _import_ccf()

FIELD_NAMES = ccf.FIELD_NAMES
IR_KEY_BEST = np.asarray(ccf.IR_KEY_BEST, float)
IR_KEY_PROVEN = np.asarray(ccf.IR_KEY_PROVEN, float)
KEY_CONFIDENCE = dict(ccf.KEY_CONFIDENCE)

#: Entries whose value is *proved* (exact invariant) rather than hypothesised.
PROVED_KEY_ENTRIES = frozenset(
    [8, 10, 11, 12, 13, 14, 23, 24, 25, 26, 27, 28, 29, 30, 31]
)

#: CCF record layout (indices into the 33 doubles).
IDX_FX, IDX_FY, IDX_CX, IDX_CY, IDX_ALPHA = 0, 1, 4, 5, 8
IDX_KC = slice(10, 15)
IDX_T = slice(20, 23)
IDX_R = slice(23, 32)
IDX_ERROR = 32

#: Dump layout (verified in docs/calibration/model.md §2).
DUMP_HEADER = (0x000, 3)          # u32 count, width, height
DUMP_CAM0 = 0x00C                 # first 22-double camera record
DUMP_CAM_STRIDE = 0xF8            # 248 B = 31 doubles (22 used + 9 zero)
DUMP_CAM_DOUBLES = 22
DUMP_KC = slice(4, 9)
DUMP_EXTRA = 9                    # 10th scalar, meaning unresolved (see docs)
DUMP_R = slice(10, 19)
DUMP_T = slice(19, 22)


# --------------------------------------------------------------------------
# distortion (OpenCV / Brown-Conrady, 5 coefficients)
# --------------------------------------------------------------------------

def distort_normalized(x: np.ndarray, y: np.ndarray, dist) -> tuple[np.ndarray, np.ndarray]:
    """Apply ``(k1, k2, p1, p2, k3)`` to normalized coordinates.

    ``x_d = x(1 + k1 r^2 + k2 r^4 + k3 r^6) + 2 p1 x y + p2 (r^2 + 2 x^2)``
    ``y_d = y(1 + k1 r^2 + k2 r^4 + k3 r^6) + p1 (r^2 + 2 y^2) + 2 p2 x y``
    """
    x = np.asarray(x, float)
    y = np.asarray(y, float)
    k1, k2, p1, p2, k3 = (float(v) for v in np.asarray(dist, float).ravel()[:5])
    r2 = x * x + y * y
    radial = 1.0 + k1 * r2 + k2 * r2 * r2 + k3 * r2 * r2 * r2
    xd = x * radial + 2.0 * p1 * x * y + p2 * (r2 + 2.0 * x * x)
    yd = y * radial + p1 * (r2 + 2.0 * y * y) + 2.0 * p2 * x * y
    return xd, yd


def undistort_normalized(xd: np.ndarray, yd: np.ndarray, dist,
                         iterations: int = 50, tol: float = 1e-14):
    """Invert :func:`distort_normalized` by fixed-point iteration.

    The distortion is a small perturbation for these lenses, so the standard
    fixed point ``x <- (x_d - tangential(x)) / radial(x)`` converges in a
    handful of steps; the loop is bounded and the achieved residual is
    returned to the caller through :func:`undistort_residual` if needed.
    """
    xd = np.asarray(xd, float)
    yd = np.asarray(yd, float)
    k1, k2, p1, p2, k3 = (float(v) for v in np.asarray(dist, float).ravel()[:5])
    x = xd.copy()
    y = yd.copy()
    for _ in range(int(iterations)):
        r2 = x * x + y * y
        radial = 1.0 + k1 * r2 + k2 * r2 * r2 + k3 * r2 * r2 * r2
        tx = 2.0 * p1 * x * y + p2 * (r2 + 2.0 * x * x)
        ty = p1 * (r2 + 2.0 * y * y) + 2.0 * p2 * x * y
        xn = (xd - tx) / radial
        yn = (yd - ty) / radial
        delta = max(float(np.max(np.abs(xn - x))), float(np.max(np.abs(yn - y)))) \
            if x.size else 0.0
        x, y = xn, yn
        if delta < tol:
            break
    return x, y


# --------------------------------------------------------------------------
# camera model
# --------------------------------------------------------------------------

def make_k(fx: float, fy: float, cx: float, cy: float, skew: float = 0.0) -> np.ndarray:
    return np.array([[float(fx), float(skew), float(cx)],
                     [0.0, float(fy), float(cy)],
                     [0.0, 0.0, 1.0]], float)


@dataclass
class CameraModel:
    """One camera: intrinsics + OpenCV distortion + world->camera pose."""

    name: str
    K: np.ndarray
    dist: np.ndarray
    R: np.ndarray
    T: np.ndarray
    image_size: tuple[int, int] = IMAGE_SIZE
    alpha_c: float = 0.0
    #: Dump-only 10th scalar of the 22-double record; meaning unresolved
    #: (it is *not* the CCF ``alpha``; see docs/calibration/model.md §2.3).
    extra: float | None = None
    source: str = ""
    key: np.ndarray | None = None

    # -- basic accessors ---------------------------------------------------
    @property
    def fx(self) -> float: return float(self.K[0, 0])

    @property
    def fy(self) -> float: return float(self.K[1, 1])

    @property
    def cx(self) -> float: return float(self.K[0, 2])

    @property
    def cy(self) -> float: return float(self.K[1, 2])

    @property
    def skew(self) -> float: return float(self.K[0, 1])

    def centre(self) -> np.ndarray:
        """Camera centre in world coordinates: ``C = -R.T @ T``."""
        return -self.R.T @ self.T

    def copy(self, **kw) -> "CameraModel":
        data = dict(name=self.name, K=self.K.copy(), dist=self.dist.copy(),
                    R=self.R.copy(), T=self.T.copy(), image_size=tuple(self.image_size),
                    alpha_c=self.alpha_c, extra=self.extra, source=self.source,
                    key=None if self.key is None else self.key.copy())
        data.update(kw)
        return CameraModel(**data)

    # -- projection --------------------------------------------------------
    def distort(self, x, y):
        return distort_normalized(x, y, self.dist)

    def undistort(self, xd, yd, iterations: int = 50):
        return undistort_normalized(xd, yd, self.dist, iterations)

    def project_normalized(self, x, y) -> tuple[np.ndarray, np.ndarray]:
        """Normalized ray -> pixel (applies distortion and K)."""
        xd, yd = distort_normalized(x, y, self.dist)
        u = self.fx * xd + self.skew * yd + self.cx
        v = self.fy * yd + self.cy
        return u, v

    def project(self, X_cam: np.ndarray) -> np.ndarray:
        """Camera-frame 3-D points (N,3) -> pixels (N,2)."""
        X = np.asarray(X_cam, float).reshape(-1, 3)
        z = X[:, 2]
        if np.any(np.abs(z) < 1e-12):
            raise ValueError("point with zero z cannot be projected")
        x, y = X[:, 0] / z, X[:, 1] / z
        u, v = self.project_normalized(x, y)
        return np.stack([u, v], axis=1)

    def pixel_rays(self, uv: np.ndarray) -> np.ndarray:
        """Pixels (N,2) -> unit-z camera rays (N,3), distortion removed."""
        uv = np.asarray(uv, float).reshape(-1, 2)
        yd = (uv[:, 1] - self.cy) / self.fy
        xd = (uv[:, 0] - self.cx - self.skew * yd) / self.fx
        x, y = self.undistort(xd, yd)
        return np.stack([x, y, np.ones_like(x)], axis=1)

    def backproject(self, uv: np.ndarray, depth: float | np.ndarray) -> np.ndarray:
        """Pixels (N,2) -> camera-frame points at the given z depth."""
        rays = self.pixel_rays(uv)
        return rays * np.asarray(depth, float).reshape(-1, 1)

    # -- serialization -----------------------------------------------------
    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "image_size": list(self.image_size),
            "K": self.K.tolist(),
            "fx": self.fx, "fy": self.fy, "cx": self.cx, "cy": self.cy,
            "skew": self.skew,
            "alpha_c": self.alpha_c,
            "dist": [float(v) for v in self.dist],
            "dist_names": list(DIST_NAMES),
            "R": self.R.tolist(),
            "T": [float(v) for v in self.T],
            "centre": self.centre().tolist(),
            "extra": None if self.extra is None else float(self.extra),
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "CameraModel":
        return cls(
            name=d["name"],
            K=np.asarray(d["K"], float),
            dist=np.asarray(d["dist"], float),
            R=np.asarray(d["R"], float),
            T=np.asarray(d["T"], float),
            image_size=tuple(d.get("image_size", IMAGE_SIZE)),
            alpha_c=float(d.get("alpha_c", 0.0)),
            extra=d.get("extra"),
            source=d.get("source", ""),
            key=None if d.get("key") is None else np.asarray(d["key"], float),
        )


# --------------------------------------------------------------------------
# calibration (a set of three cameras in one world frame)
# --------------------------------------------------------------------------

CONVENTION = ("OpenCV pinhole: X_cam = R @ X_world + T; z forward, x right, "
              "y down; u right, v down; units mm and px")


@dataclass
class Calibration:
    cameras: dict[str, CameraModel]
    image_size: tuple[int, int] = IMAGE_SIZE
    units: str = "mm"
    convention: str = CONVENTION
    source: str = ""
    provenance: dict[str, Any] = field(default_factory=dict)

    # -- accessors ---------------------------------------------------------
    def __getitem__(self, name: str) -> CameraModel:
        return self.cameras[name]

    @property
    def left(self) -> CameraModel: return self.cameras["left"]

    @property
    def right(self) -> CameraModel: return self.cameras["right"]

    @property
    def tex(self) -> CameraModel: return self.cameras["tex"]

    def names(self) -> tuple[str, ...]:
        return tuple(self.cameras)

    # -- geometry ----------------------------------------------------------
    def relative_pose(self, a: str = "left", b: str = "right"):
        """Return ``R_ab, t_ab`` with ``X_b = R_ab @ X_a + t_ab``.

        Both cameras share the world frame, so
        ``X_a = R_a X_w + T_a`` and ``X_b = R_b X_w + T_b`` give
        ``R_ab = R_b R_a^T`` and ``t_ab = T_b - R_ab T_a``.
        """
        ca, cb = self.cameras[a], self.cameras[b]
        Rab = cb.R @ ca.R.T
        tab = cb.T - Rab @ ca.T
        return Rab, tab

    def baseline(self, a: str = "left", b: str = "right") -> float:
        """Distance between the two camera centres (mm)."""
        return float(np.linalg.norm(self.cameras[a].centre() - self.cameras[b].centre()))

    def rig_triangle(self) -> dict[str, float]:
        return {
            "left-right": self.baseline("left", "right"),
            "left-tex": self.baseline("left", "tex"),
            "right-tex": self.baseline("right", "tex"),
        }

    def relative_rotation_deg(self, a: str = "left", b: str = "right") -> float:
        R, _ = self.relative_pose(a, b)
        c = (float(np.trace(R)) - 1.0) / 2.0
        return math.degrees(math.acos(max(-1.0, min(1.0, c))))

    def epipole(self, a: str = "left", b: str = "right") -> np.ndarray:
        """Epipole of camera ``b`` in image ``a`` (pixels).

        That epipole is the projection of camera ``b``'s centre into ``a``:
        ``e = K_a * C_b^a`` with ``C_b^a = -R_ab^T t_ab`` the centre of ``b``
        expressed in ``a``'s frame (``X_b = R_ab X_a + t_ab``).
        """
        Rab, tab = self.relative_pose(a, b)
        t = -Rab.T @ tab
        if abs(t[2]) < 1e-12:
            raise ValueError("epipole at infinity (t_z = 0)")
        p = self.cameras[a].K @ (t / t[2])
        return p[:2] / p[2]

    # -- serialization -----------------------------------------------------
    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "einstar-calibration/1",
            "image_size": list(self.image_size),
            "units": self.units,
            "convention": self.convention,
            "source": self.source,
            "provenance": self.provenance,
            "cameras": {k: v.as_dict() for k, v in self.cameras.items()},
            "rig": {
                "triangle_mm": self.rig_triangle(),
                "relative_rotation_deg": self.relative_rotation_deg("left", "right"),
                "epipole_left_of_right": self.epipole("left", "right").tolist(),
            },
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.as_dict(), indent=indent, sort_keys=False) + "\n"

    def save_json(self, path: str | Path, indent: int = 2) -> Path:
        p = Path(path)
        p.write_text(self.to_json(indent=indent))
        return p

    @classmethod
    def from_dict(cls, d: dict) -> "Calibration":
        if d.get("schema") not in (None, "einstar-calibration/1"):
            raise ValueError(f"unknown model schema {d.get('schema')!r}")
        return cls(
            cameras={k: CameraModel.from_dict(v) for k, v in d["cameras"].items()},
            image_size=tuple(d.get("image_size", IMAGE_SIZE)),
            units=d.get("units", "mm"),
            convention=d.get("convention", CONVENTION),
            source=d.get("source", ""),
            provenance=d.get("provenance", {}),
        )

    @classmethod
    def load_json(cls, path: str | Path) -> "Calibration":
        return cls.from_dict(json.loads(Path(path).read_text()))

    # -- constructors ------------------------------------------------------
    @classmethod
    def from_ccf_dir(cls, directory: str | Path = CCF_DIR,
                     key_mode: str = "best",
                     skew_mode: str = SKEW_MODE_DEFAULT) -> "Calibration":
        """Build the model from the three .txt CCF records.

        ``key_mode`` selects the integer key subtracted from the IR records:
        ``"best"`` (proved entries + the documented hypotheses), ``"proven"``
        (only entries proved by an exact invariant; fx/fy/cx/cy then stay
        keyed) or ``"zero"`` (raw).  The key table and its confidence strings
        come from :mod:`decode_ccf`.
        """
        if skew_mode not in SKEW_MODES:
            raise ValueError(f"skew_mode must be one of {SKEW_MODES}")
        directory = Path(directory)
        cams: dict[str, CameraModel] = {}
        files = {"left": "LeftCCF.txt", "right": "RightCCF.txt", "tex": "TexCCF.txt"}
        digests = {}
        for name, fname in files.items():
            path = directory / fname
            data = path.read_bytes()
            digests[fname] = hashlib.sha256(data).hexdigest()
            rec = ccf.decode_record(data, str(path), key_mode)
            if name != "tex" and key_mode == "zero":
                key = np.zeros(33)
            else:
                key = np.asarray(rec.key, float)
            fx, fy = float(rec.fx), float(rec.fy)
            alpha = float(rec.alpha)
            skew = alpha * fx if skew_mode == "bouguet" else alpha
            cams[name] = CameraModel(
                name=name,
                K=make_k(fx, fy, rec.cx, rec.cy, skew),
                dist=np.asarray(rec.kc, float),
                R=np.asarray(rec.R, float),
                T=np.asarray(rec.T, float),
                alpha_c=alpha,
                source=f"ccf:{path.name}",
                key=key,
            )
        model = cls(cameras=cams, source="ccf",
                    provenance={
                        "format": "33 x float64 LE (CCToolbox Calib_Results packing)",
                        "directory": str(directory),
                        "key_mode": key_mode,
                        "skew_mode": skew_mode,
                        "sha256": digests,
                        "key_confidence": {FIELD_NAMES[i]: KEY_CONFIDENCE.get(i, "zero")
                                           for i in range(33)},
                        "proved_key_entries": sorted(PROVED_KEY_ENTRIES),
                    })
        return model

    @classmethod
    def from_dump(cls, path: str | Path = DUMP_PATH,
                  skew_mode: str = SKEW_MODE_DEFAULT) -> "Calibration":
        """Build the model from the **unkeyed** 22-double records of the dump.

        The records are ``fx fy cx cy kc5 extra R9 T3`` (field order proved in
        docs/calibration/model.md §2).  ``extra`` (d[9]) is kept as
        :attr:`CameraModel.extra` and is *not* used as a distortion
        coefficient; its maximum possible effect is < 0.15 px (see docs).

        The dump also embeds the three keyed CCF records; :func:`load` can
        cross-check them, but this constructor deliberately uses only the
        unkeyed data so that **no unproved key entry is needed**.
        """
        path = Path(path)
        data = path.read_bytes()
        header = struct.unpack_from("<3I", data, DUMP_HEADER[0])
        if header[1:] != IMAGE_SIZE:
            raise ValueError(f"dump image size {header[1:]} != {IMAGE_SIZE}")
        cams: dict[str, CameraModel] = {}
        records = []
        for i, name in enumerate(CAMERA_NAMES):
            off = DUMP_CAM0 + i * DUMP_CAM_STRIDE
            v = np.array(struct.unpack_from(f"<{DUMP_CAM_DOUBLES}d", data, off), float)
            records.append({"index": i, "offset": off,
                            "values": [float(x) for x in v]})
            cams[name] = CameraModel(
                name=name,
                K=make_k(v[0], v[1], v[2], v[3], 0.0),
                dist=np.asarray(v[DUMP_KC], float),
                R=np.asarray(v[DUMP_R], float).reshape(3, 3),
                T=np.asarray(v[DUMP_T], float),
                alpha_c=0.0,
                extra=float(v[DUMP_EXTRA]),
                source=f"dump:0x{off:03X}",
            )
        model = cls(cameras=cams, source="dump",
                    provenance={
                        "path": str(path),
                        "sha256": hashlib.sha256(data).hexdigest(),
                        "size": len(data),
                        "header": {"count": header[0], "width": header[1],
                                   "height": header[2]},
                        "record_layout": "22 x float64: fx fy cx cy kc5 extra R9 T3",
                        "stride_bytes": DUMP_CAM_STRIDE,
                        "key_required": False,
                        "skew_mode": skew_mode,
                    })
        return model

    @classmethod
    def load(cls, source: str = "auto", ccf_dir: str | Path = CCF_DIR,
             dump_path: str | Path = DUMP_PATH, key_mode: str = "best",
             skew_mode: str = SKEW_MODE_DEFAULT) -> "Calibration":
        """Load a model.

        ``source``:
          * ``"ccf"``  - the .txt CCF records with the recovered key (the
            **operational** model: it is the calibration the vendor stack and
            the device firmware use, and the only one that rectifies real
            captures -- see docs/calibration/model.md §7),
          * ``"dump"`` - the unkeyed 22-double records (no key hypothesis, but
            a *different* calibration run; kept as an independent cross-check),
          * ``"auto"`` - the CCF records when present, else the dump.
        """
        if source == "ccf":
            return cls.from_ccf_dir(ccf_dir, key_mode=key_mode, skew_mode=skew_mode)
        if source == "dump":
            return cls.from_dump(dump_path, skew_mode=skew_mode)
        if source == "auto":
            if (Path(ccf_dir) / "LeftCCF.txt").exists():
                return cls.from_ccf_dir(ccf_dir, key_mode=key_mode, skew_mode=skew_mode)
            return cls.from_dump(dump_path, skew_mode=skew_mode)
        raise ValueError(f"unknown source {source!r} (use ccf|dump|auto)")


# --------------------------------------------------------------------------
# stereo pair helper
# --------------------------------------------------------------------------

@dataclass
class StereoPair:
    """A stereo pair with the OpenCV relative pose ``X_b = R X_a + t``."""

    a: CameraModel
    b: CameraModel
    R: np.ndarray
    t: np.ndarray
    baseline: float

    @classmethod
    def from_calibration(cls, calib: Calibration, a: str = "left",
                         b: str = "right") -> "StereoPair":
        R, t = calib.relative_pose(a, b)
        return cls(a=calib.cameras[a], b=calib.cameras[b], R=R, t=t,
                   baseline=float(np.linalg.norm(t)))

    def centre_b_in_a(self) -> np.ndarray:
        return -self.R.T @ self.t

    def as_dict(self) -> dict[str, Any]:
        return {"a": self.a.name, "b": self.b.name,
                "R_ba": self.R.tolist(), "t_ba": self.t.tolist(),
                "baseline_mm": self.baseline}


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Build/print the Einstar calibration model.")
    ap.add_argument("--source", choices=("ccf", "dump", "auto"), default="auto")
    ap.add_argument("--ccf-dir", type=Path, default=CCF_DIR)
    ap.add_argument("--dump", type=Path, default=DUMP_PATH)
    ap.add_argument("--key-mode", choices=("best", "proven", "zero"), default="best")
    ap.add_argument("--skew-mode", choices=SKEW_MODES, default=SKEW_MODE_DEFAULT)
    ap.add_argument("--json-out", type=Path, default=None)
    args = ap.parse_args(argv)

    calib = Calibration.load(args.source, ccf_dir=args.ccf_dir,
                             dump_path=args.dump, key_mode=args.key_mode,
                             skew_mode=args.skew_mode)
    print(f"source: {calib.source}   units: {calib.units}   image: {calib.image_size[0]}x{calib.image_size[1]}")
    print(f"{'camera':6} {'fx':>12} {'fy':>12} {'cx':>10} {'cy':>10} "
          f"{'k1':>10} {'k2':>10} {'p1':>10} {'p2':>10} {'k3':>10}")
    for name, c in calib.cameras.items():
        k1, k2, p1, p2, k3 = c.dist
        print(f"{name:6} {c.fx:12.6f} {c.fy:12.6f} {c.cx:10.4f} {c.cy:10.4f} "
              f"{k1:10.6f} {k2:10.6f} {p1:10.2e} {p2:10.2e} {k3:10.6f}")
    tri = calib.rig_triangle()
    print("rig triangle (mm): " + ", ".join(f"{k}={v:.3f}" for k, v in tri.items()))
    print(f"IR relative rotation: {calib.relative_rotation_deg('left', 'right'):.6f} deg")
    print(f"epipole (right in left): {calib.epipole('left', 'right')}")
    if args.json_out:
        calib.save_json(args.json_out)
        print(f"model written to {args.json_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
