#!/usr/bin/env python3
"""Bouguet stereo rectification for the Einstar IR pair (numpy only).

The pair is *not* rectified in hardware: the relative rotation is ~22 deg and
the baseline has a z component, so the raw images are strongly epipolar.  This
module builds the rectification rotations, projection matrices and Q matrix
exactly as ``cv2.stereoRectify`` does for the Bouguet variant, plus the two
inverse maps that ``cv2.initUndistortRectifyMap`` would produce.

Conventions
-----------
``pair.R``/``pair.t`` are the OpenCV relative pose ``X_b = R @ X_a + t``
(camera b = right IR, camera a = left IR).  The *translation from a to b* is
``T = -R.T @ t`` (camera b's centre in camera a's frame); the baseline is
``|T|``.

Rectified frame
---------------
``R_rect`` has rows ``(e1, e2, e3)`` with ``e1 = T/|T|``; therefore a point
``X_a`` in the left camera frame is ``X_rect = R_rect @ X_a`` and the right
camera centre lands on the +x axis at ``(B, 0, 0)``::

    P1 = K_new [R_rect | 0]
    P2 = K_new [R_rect | -R_rect @ C_b]      (C_b = T)

so ``u1 - u2 = f * B / Z`` with ``B > 0`` and::

    Q = [[1, 0, 0, -cx],
         [0, 1, 0, -cy],
         [0, 0, 0,  f],
         [0, 0, -1/Tx, 0]]        with Tx = -B

which yields ``[X, Y, Z, W] = Q @ [u, v, d, 1]`` and ``(X, Y, Z) / W`` in the
rectified left camera frame (mm), ``Z = f * B / d``.

Everything here is deterministic and offline.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from calib_model import (IMAGE_HEIGHT, IMAGE_SIZE, IMAGE_WIDTH, CameraModel,
                         StereoPair, make_k)


# --------------------------------------------------------------------------
# rotations
# --------------------------------------------------------------------------

def rectification_rotation(T: np.ndarray) -> np.ndarray:
    """Bouguet rotation taking the baseline direction ``T`` onto +x.

    Rows are ``e1 = T/|T|``, ``e2 = (z x e1)/|z x e1|``, ``e3 = e1 x e2``.
    This is exactly the construction used by Bouguet's rectification and by
    OpenCV's ``stereoRectify`` when the principal rays are parallel to z.
    """
    T = np.asarray(T, float).reshape(3)
    n = float(np.linalg.norm(T))
    if n < 1e-12:
        raise ValueError("zero baseline: cannot rectify a coincident pair")
    e1 = T / n
    e2 = np.cross(np.array([0.0, 0.0, 1.0]), e1)
    n2 = float(np.linalg.norm(e2))
    if n2 < 1e-9:                      # baseline along z -> pick y as fallback
        e2 = np.cross(np.array([0.0, 1.0, 0.0]), e1)
        n2 = float(np.linalg.norm(e2))
        if n2 < 1e-9:
            raise ValueError("degenerate baseline direction")
    e2 = e2 / n2
    e3 = np.cross(e1, e2)
    return np.vstack([e1, e2, e3])


def default_new_k(a: CameraModel, b: CameraModel, zoom: float = 1.0) -> np.ndarray:
    """Common rectified intrinsics: mean focal length, mean principal point.

    ``f = mean(fx_a, fy_a, fx_b, fy_b)`` keeps the rectified pixels square
    (needed for the simple Q form), ``cx, cy`` are the means of the two
    principal points.  ``zoom`` scales the focal length about the principal
    point (``zoom > 1`` shrinks the field of view, ``< 1`` sees more).
    """
    f = 0.25 * (a.fx + a.fy + b.fx + b.fy) * float(zoom)
    cx = 0.5 * (a.cx + b.cx)
    cy = 0.5 * (a.cy + b.cy)
    return make_k(f, f, cx, cy)


# --------------------------------------------------------------------------
# inverse maps (the numpy equivalent of initUndistortRectifyMap)
# --------------------------------------------------------------------------

def _inverse_map(cam: CameraModel, R: np.ndarray, K_new: np.ndarray,
                 image_size: tuple[int, int]):
    """Destination pixel -> source pixel for one rectified camera."""
    h, w = int(image_size[1]), int(image_size[0])
    u = np.arange(w, dtype=np.float64)[None, :].repeat(h, axis=0)
    v = np.arange(h, dtype=np.float64)[:, None].repeat(w, axis=1)
    x = (u - K_new[0, 2]) / K_new[0, 0]
    y = (v - K_new[1, 2]) / K_new[1, 1]
    # ray in the rectified frame -> ray in this camera's frame (R.T @ ray)
    X = (x[..., None] * R[0] + y[..., None] * R[1] + R[2])
    z = X[..., 2]
    xa = X[..., 0] / z
    ya = X[..., 1] / z
    xd, yd = cam.distort(xa, ya)
    map_x = cam.fx * xd + cam.skew * yd + cam.cx
    map_y = cam.fy * yd + cam.cy
    inside = (map_x >= 0) & (map_x <= w - 1) & (map_y >= 0) & (map_y <= h - 1)
    return map_x.astype(np.float32), map_y.astype(np.float32), inside


def remap_bilinear(image: np.ndarray, map_x: np.ndarray, map_y: np.ndarray,
                   border: float = 0.0) -> np.ndarray:
    """Bilinear ``cv2.remap`` equivalent for a 2-D (or HxWxC) image."""
    img = np.asarray(image, float)
    h, w = img.shape[:2]
    mx = np.asarray(map_x, float)
    my = np.asarray(map_y, float)
    ok = (mx >= 0) & (mx <= w - 1) & (my >= 0) & (my <= h - 1) & np.isfinite(mx) & np.isfinite(my)
    x0 = np.clip(np.floor(mx), 0, w - 1).astype(np.int64)
    y0 = np.clip(np.floor(my), 0, h - 1).astype(np.int64)
    x1 = np.clip(x0 + 1, 0, w - 1)
    y1 = np.clip(y0 + 1, 0, h - 1)
    fx = np.clip(mx - x0, 0.0, 1.0)
    fy = np.clip(my - y0, 0.0, 1.0)
    if img.ndim == 3:
        fx = fx[..., None]
        fy = fy[..., None]
    top = img[y0, x0] * (1 - fx) + img[y0, x1] * fx
    bot = img[y1, x0] * (1 - fx) + img[y1, x1] * fx
    out = top * (1 - fy) + bot * fy
    if img.ndim == 2:
        out = np.where(ok, out, border)
    else:
        out = np.where(ok[..., None], out, border)
    return out


# --------------------------------------------------------------------------
# rectification object
# --------------------------------------------------------------------------

@dataclass
class Rectification:
    pair: StereoPair
    K_new: np.ndarray
    R1: np.ndarray
    R2: np.ndarray
    P1: np.ndarray          # K_new [I | 0]      (rectified-left frame)
    P2: np.ndarray          # K_new [I | -B e_x] (rectified-left frame)
    Q: np.ndarray
    P1_cam: np.ndarray | None = None   # K_new [R1 | 0]      (left camera frame)
    P2_cam: np.ndarray | None = None   # K_new [R1 | -R1 C_b] (left camera frame)
    image_size: tuple[int, int] = IMAGE_SIZE
    map_x_a: np.ndarray | None = None
    map_y_a: np.ndarray | None = None
    map_x_b: np.ndarray | None = None
    map_y_b: np.ndarray | None = None
    valid_a: np.ndarray | None = None
    valid_b: np.ndarray | None = None

    # -- image-domain ------------------------------------------------------
    def remap_a(self, image: np.ndarray, border: float = 0.0) -> np.ndarray:
        return remap_bilinear(image, self.map_x_a, self.map_y_a, border)

    def remap_b(self, image: np.ndarray, border: float = 0.0) -> np.ndarray:
        return remap_bilinear(image, self.map_x_b, self.map_y_b, border)

    def valid_fraction(self) -> dict[str, float]:
        return {"a": float(self.valid_a.mean()), "b": float(self.valid_b.mean())}

    # -- point-domain ------------------------------------------------------
    def rectify_pixels_a(self, uv: np.ndarray) -> np.ndarray:
        """Original left pixels -> rectified left pixels (analytic)."""
        rays = self.pair.a.pixel_rays(uv)
        X = rays @ self.R1.T
        return self._project_new(X)

    def rectify_pixels_b(self, uv: np.ndarray) -> np.ndarray:
        """Original right pixels -> rectified right pixels (analytic)."""
        rays = self.pair.b.pixel_rays(uv)
        X = rays @ self.R2.T
        return self._project_new(X)

    def _project_new(self, X_rect: np.ndarray) -> np.ndarray:
        z = X_rect[:, 2]
        u = self.K_new[0, 0] * X_rect[:, 0] / z + self.K_new[0, 2]
        v = self.K_new[1, 1] * X_rect[:, 1] / z + self.K_new[1, 2]
        return np.stack([u, v], axis=1)

    def disparity(self, uv_a: np.ndarray, uv_b: np.ndarray) -> np.ndarray:
        """Horizontal disparity ``u_a - u_b`` of corresponding points."""
        ra = self.rectify_pixels_a(uv_a)
        rb = self.rectify_pixels_b(uv_b)
        return ra[:, 0] - rb[:, 0]

    # -- depth -------------------------------------------------------------
    def disparity_to_xyz(self, disp: np.ndarray, uv: np.ndarray | None = None) -> np.ndarray:
        """Disparity -> 3-D points in the rectified left camera frame (mm).

        ``uv`` are the rectified *left* pixel coordinates.  When omitted the
        principal point is used, so the result is the ray-wise depth only.
        """
        disp = np.asarray(disp, float).reshape(-1)
        n = disp.size
        if uv is None:
            u = np.full(n, self.K_new[0, 2])
            v = np.full(n, self.K_new[1, 2])
        else:
            uv = np.asarray(uv, float).reshape(-1, 2)
            u, v = uv[:, 0], uv[:, 1]
        p = np.stack([u, v, disp, np.ones(n)], axis=0)
        X = self.Q @ p
        return (X[:3] / X[3]).T

    def depth(self, disp: np.ndarray) -> np.ndarray:
        """``Z = f * B / d`` in mm (positive for positive disparity)."""
        disp = np.asarray(disp, float)
        with np.errstate(divide="ignore", invalid="ignore"):
            return self._f * self._baseline / disp

    @property
    def _f(self) -> float:
        return float(self.K_new[0, 0])

    @property
    def _baseline(self) -> float:
        return float(np.linalg.norm(self.pair.centre_b_in_a()))

    def project_rectified(self, xyz: np.ndarray, which: str = "a") -> np.ndarray:
        """Project 3-D points given in the **rectified left** frame via P1/P2."""
        X = np.asarray(xyz, float).reshape(-1, 3)
        P = self.P1 if which == "a" else self.P2
        h = np.concatenate([X, np.ones((X.shape[0], 1))], axis=1) @ P.T
        return h[:, :2] / h[:, 2:3]

    def project_camera_frame(self, X_a: np.ndarray, which: str = "a") -> np.ndarray:
        """Project points given in the **left camera** frame via P1_cam/P2_cam.

        Identity check used by the validator:
        ``project_camera_frame(X_a) == project_rectified(R1 @ X_a)``.
        """
        X = np.asarray(X_a, float).reshape(-1, 3)
        P = self.P1_cam if which == "a" else self.P2_cam
        h = np.concatenate([X, np.ones((X.shape[0], 1))], axis=1) @ P.T
        return h[:, :2] / h[:, 2:3]

    # -- serialization -----------------------------------------------------
    def as_dict(self) -> dict[str, Any]:
        return {
            "K_new": self.K_new.tolist(),
            "R1": self.R1.tolist(),
            "R2": self.R2.tolist(),
            "P1": self.P1.tolist(),
            "P2": self.P2.tolist(),
            "P1_cam": None if self.P1_cam is None else self.P1_cam.tolist(),
            "P2_cam": None if self.P2_cam is None else self.P2_cam.tolist(),
            "Q": self.Q.tolist(),
            "image_size": list(self.image_size),
            "baseline_mm": self._baseline,
            "focal_px": self._f,
            "valid_fraction": self.valid_fraction(),
        }


def bouguet_rectification(pair: StereoPair,
                          image_size: tuple[int, int] = IMAGE_SIZE,
                          K_new: np.ndarray | None = None,
                          zoom: float = 1.0,
                          build_maps: bool = True) -> Rectification:
    """Full Bouguet rectification of a stereo pair.

    ``image_size`` is ``(width, height)``; it controls the map arrays and must
    match the images the maps will be applied to.  The rectified canvas keeps
    the same size as the input (no automatic cropping); use
    :meth:`Rectification.valid_fraction` to see how much of it is usable.
    """
    w, h = int(image_size[0]), int(image_size[1])
    if w <= 0 or h <= 0:
        raise ValueError(f"bad image size {image_size}")
    T = pair.centre_b_in_a()
    B = float(np.linalg.norm(T))
    R_rect = rectification_rotation(T)
    R1 = R_rect
    R2 = R_rect @ pair.R.T
    if K_new is None:
        K_new = default_new_k(pair.a, pair.b, zoom=zoom)
    K_new = np.asarray(K_new, float)
    # Canonical (OpenCV) rectified-frame projection matrices.
    P1 = K_new @ np.hstack([np.eye(3), np.zeros((3, 1))])
    P2 = K_new @ np.hstack([np.eye(3), np.array([[-B], [0.0], [0.0]])])
    # Same matrices expressed for points in the *left camera* frame.
    P1_cam = K_new @ np.hstack([R1, np.zeros((3, 1))])
    P2_cam = K_new @ np.hstack([R1, (-R1 @ T).reshape(3, 1)])
    Tx = -B
    Q = np.array([[1.0, 0.0, 0.0, -K_new[0, 2]],
                  [0.0, 1.0, 0.0, -K_new[1, 2]],
                  [0.0, 0.0, 0.0, K_new[0, 0]],
                  [0.0, 0.0, -1.0 / Tx, 0.0]], float)
    rect = Rectification(pair=pair, K_new=K_new, R1=R1, R2=R2, P1=P1, P2=P2,
                         Q=Q, image_size=(w, h), P1_cam=P1_cam, P2_cam=P2_cam)
    if build_maps:
        rect.map_x_a, rect.map_y_a, rect.valid_a = _inverse_map(pair.a, R1, K_new, (w, h))
        rect.map_x_b, rect.map_y_b, rect.valid_b = _inverse_map(pair.b, R2, K_new, (w, h))
    return rect


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    import argparse
    from calib_model import Calibration

    ap = argparse.ArgumentParser(description="Bouguet rectification of the IR pair.")
    ap.add_argument("--source", choices=("ccf", "dump", "auto"), default="auto")
    ap.add_argument("--json-out", type=__import__("pathlib").Path, default=None)
    ap.add_argument("--zoom", type=float, default=1.0)
    args = ap.parse_args(argv)

    calib = Calibration.load(args.source)
    pair = StereoPair.from_calibration(calib, "left", "right")
    rect = bouguet_rectification(pair, calib.image_size, zoom=args.zoom)
    d = rect.as_dict()
    print(f"pair: {pair.a.name} -> {pair.b.name}   baseline B = {d['baseline_mm']:.6f} mm")
    print(f"K_new: f = {d['focal_px']:.6f} px, cx = {rect.K_new[0, 2]:.6f}, "
          f"cy = {rect.K_new[1, 2]:.6f}")
    print(f"map shapes: a={rect.map_x_a.shape} b={rect.map_x_b.shape}  (w, h) = {rect.image_size}")
    print(f"valid fraction: {d['valid_fraction']}")
    print("Q =\n", np.array2string(rect.Q, precision=8, suppress_small=False))
    print(f"P1 =\n{np.array2string(rect.P1, precision=6)}")
    print(f"P2 =\n{np.array2string(rect.P2, precision=6)}")
    print(f"P1_cam =\n{np.array2string(rect.P1_cam, precision=6)}")
    if args.json_out:
        import json
        args.json_out.write_text(json.dumps(d, indent=2) + "\n")
        print(f"rectification written to {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
