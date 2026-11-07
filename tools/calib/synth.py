#!/usr/bin/env python3
"""Synthetic round-trip validation of the rectification/triangulation chain.

The generator is deliberately boring: random 3-D points in front of the rig,
projected through the *real* camera models (intrinsics + distortion + pose),
then rectified, differenced into an ideal disparity, and triangulated back.
Because every step is analytic, the round-trip error must sit at the
floating-point floor; any larger number means a convention bug, not noise.

Two modes are reported:

* ``continuous`` disparity - the exact disparity ``u_a - u_b``;
* ``integer`` disparity    - rounded to whole pixels, which is what a block
  matcher produces; the resulting depth error is the *quantisation* floor,
  ``dZ = Z^2 / (f B) * 0.5 px``.

The scene is sampled in the rectified left camera frame so that the ground
truth is directly comparable with the Q-matrix output.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from calib_model import Calibration, StereoPair
from rectify import Rectification, bouguet_rectification
from triangulate import stats, triangulate_dlt


# --------------------------------------------------------------------------
# scene
# --------------------------------------------------------------------------

def sample_scene(rect: Rectification, n: int = 5000, seed: int = 0,
                 z_range: tuple[float, float] = (250.0, 450.0),
                 margin_px: float = 24.0) -> tuple[np.ndarray, np.ndarray]:
    """Random points seen by the rectified left camera.

    Returns ``(X_rect, uv_rect)`` where ``uv_rect`` are the rectified pixel
    coordinates the points project to (before distortion / rectification of
    the physical cameras).
    """
    if z_range[0] <= 0 or z_range[1] <= z_range[0]:
        raise ValueError("z_range must be positive and increasing")
    rng = np.random.default_rng(seed)
    w, h = rect.image_size
    pool = None
    if rect.valid_a is not None and rect.valid_b is not None:
        # sample only where both cameras have real data after rectification
        pool = np.flatnonzero((rect.valid_a & rect.valid_b).ravel())
    if pool is not None and pool.size >= n:
        flat = rng.choice(pool, size=n, replace=False)
        v, u = np.divmod(flat, w)
        u = u.astype(float) + rng.uniform(-0.5, 0.5, n)
        v = v.astype(float) + rng.uniform(-0.5, 0.5, n)
    else:
        lo_u, hi_u = margin_px, w - 1 - margin_px
        hi_u = max(hi_u, lo_u + 1)
        lo_v = margin_px
        hi_v = max(h - 1 - margin_px, lo_v + 1)
        u = rng.uniform(lo_u, hi_u, n)
        v = rng.uniform(lo_v, hi_v, n)
    z = rng.uniform(z_range[0], z_range[1], n)
    f = rect.K_new[0, 0]
    cx, cy = rect.K_new[0, 2], rect.K_new[1, 2]
    x = (u - cx) / f
    y = (v - cy) / f
    X = np.stack([x * z, y * z, z], axis=1)
    return X, np.stack([u, v], axis=1)


def project_scene(rect: Rectification, X_rect: np.ndarray):
    """Rectified-frame points -> original pixels in both cameras (with distortion)."""
    pair = rect.pair
    X_a = np.asarray(X_rect, float) @ rect.R1        # row-vector form of R1.T @ X
    X_b = X_a @ pair.R.T + pair.t
    pa = pair.a.project(X_a)
    pb = pair.b.project(X_b)
    return pa, pb, X_a, X_b


def _inside(uv: np.ndarray, size: tuple[int, int]) -> np.ndarray:
    w, h = size
    return ((uv[:, 0] >= 0) & (uv[:, 0] <= w - 1) &
            (uv[:, 1] >= 0) & (uv[:, 1] <= h - 1))


# --------------------------------------------------------------------------
# round trip
# --------------------------------------------------------------------------

@dataclass
class RoundTrip:
    seed: int
    n_requested: int
    n_used: int
    z_range: tuple[float, float]
    integer_disparity: bool
    true_xyz: np.ndarray
    est_xyz: np.ndarray
    disp_true: np.ndarray
    disp_used: np.ndarray
    uv_left_rect: np.ndarray
    uv_right_rect: np.ndarray
    pa: np.ndarray
    pb: np.ndarray
    dlt_xyz: np.ndarray | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    # -- metrics -----------------------------------------------------------
    def err3d(self) -> np.ndarray:
        return np.linalg.norm(self.est_xyz - self.true_xyz, axis=1)

    def summary(self) -> dict[str, Any]:
        err = self.err3d()
        dz = self.est_xyz[:, 2] - self.true_xyz[:, 2]
        dxy = np.linalg.norm(self.est_xyz[:, :2] - self.true_xyz[:, :2], axis=1)
        # px metric: reproject the *estimate* through the original camera models
        pair = self.extra["rect"].pair
        R1 = self.extra["rect"].R1
        Xa_est = self.est_xyz @ R1
        Xb_est = Xa_est @ pair.R.T + pair.t
        e_a = np.linalg.norm(pair.a.project(Xa_est) - self.pa, axis=1)
        e_b = np.linalg.norm(pair.b.project(Xb_est) - self.pb, axis=1)
        out = {
            "seed": self.seed,
            "n_requested": self.n_requested,
            "n_used": self.n_used,
            "z_range_mm": list(self.z_range),
            "integer_disparity": self.integer_disparity,
            "err3d_mm": stats(err),
            "err_z_mm": stats(dz),
            "err_xy_mm": stats(dxy),
            "disp_true_px": stats(self.disp_true),
            "disp_used_minus_true_px": stats(self.disp_used - self.disp_true),
            "reprojection_left_px": stats(e_a),
            "reprojection_right_px": stats(e_b),
        }
        if self.dlt_xyz is not None:
            out["dlt_vs_q_mm"] = stats(np.linalg.norm(self.dlt_xyz - self.est_xyz, axis=1))
        return out


def run_round_trip(calib: Calibration | None = None,
                   rect: Rectification | None = None,
                   n: int = 5000, seed: int = 0,
                   z_range: tuple[float, float] = (250.0, 450.0),
                   margin_px: float = 24.0,
                   integer_disparity: bool = False,
                   cross_check_dlt: bool = True) -> RoundTrip:
    """Project -> distort -> rectify -> disparity -> triangulate -> compare."""
    if rect is None:
        if calib is None:
            calib = Calibration.load("auto")
        rect = bouguet_rectification(StereoPair.from_calibration(calib, "left", "right"),
                                     calib.image_size)
    X_true, uv_target = sample_scene(rect, n=n, seed=seed, z_range=z_range,
                                     margin_px=margin_px)
    pa, pb, _, _ = project_scene(rect, X_true)
    keep = _inside(pa, rect.image_size) & _inside(pb, rect.image_size)
    X_true, pa, pb = X_true[keep], pa[keep], pb[keep]

    ra = rect.rectify_pixels_a(pa)
    rb = rect.rectify_pixels_b(pb)
    keep2 = _inside(ra, rect.image_size) & _inside(rb, rect.image_size)
    X_true, pa, pb, ra, rb = X_true[keep2], pa[keep2], pb[keep2], ra[keep2], rb[keep2]
    disp_true = ra[:, 0] - rb[:, 0]
    disp_used = np.round(disp_true) if integer_disparity else disp_true.copy()
    est = rect.disparity_to_xyz(disp_used, ra)
    dlt = None
    if cross_check_dlt:
        dlt = triangulate_dlt(rect.P1, rect.P2, ra, rb)
    rt = RoundTrip(seed=seed, n_requested=n, n_used=int(X_true.shape[0]),
                   z_range=z_range, integer_disparity=integer_disparity,
                   true_xyz=X_true, est_xyz=est, disp_true=disp_true,
                   disp_used=disp_used, uv_left_rect=ra, uv_right_rect=rb,
                   pa=pa, pb=pb, dlt_xyz=dlt, extra={"rect": rect})
    return rt


def quantisation_floor(rect: Rectification, z: np.ndarray) -> np.ndarray:
    """Depth error from half a pixel of disparity: ``Z^2 / (f B) * 0.5``."""
    f = rect.K_new[0, 0]
    B = float(np.linalg.norm(rect.pair.centre_b_in_a()))
    return np.asarray(z, float) ** 2 / (f * B) * 0.5


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Synthetic round-trip validation.")
    ap.add_argument("--source", choices=("ccf", "dump", "auto"), default="auto")
    ap.add_argument("--n", type=int, default=20000)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--zmin", type=float, default=250.0)
    ap.add_argument("--zmax", type=float, default=450.0)
    ap.add_argument("--integer", action="store_true",
                    help="round disparity to whole pixels (block-matcher floor)")
    ap.add_argument("--json-out", type=Path, default=None)
    args = ap.parse_args(argv)

    calib = Calibration.load(args.source)
    pair = StereoPair.from_calibration(calib, "left", "right")
    rect = bouguet_rectification(pair, calib.image_size)
    rt = run_round_trip(rect=rect, n=args.n, seed=args.seed,
                        z_range=(args.zmin, args.zmax),
                        integer_disparity=args.integer)
    s = rt.summary()
    print(f"source={calib.source}  points used {s['n_used']}/{s['n_requested']}  "
          f"z=[{args.zmin}, {args.zmax}] mm  integer_disparity={args.integer}")
    print(f"  3-D error (mm):        {s['err3d_mm']}")
    print(f"  depth error (mm):      {s['err_z_mm']}")
    print(f"  reprojection L/R (px): {s['reprojection_left_px']} / {s['reprojection_right_px']}")
    if "dlt_vs_q_mm" in s:
        print(f"  DLT vs Q (mm):         {s['dlt_vs_q_mm']}")
    print(f"  disparity (px):        {s['disp_true_px']}")
    if args.integer:
        z = rt.true_xyz[:, 2]
        print(f"  quantisation floor (mm): mean {quantisation_floor(rect, z).mean():.4f}, "
              f"max {quantisation_floor(rect, z).max():.4f}")
    if args.json_out:
        args.json_out.write_text(json.dumps(s, indent=2) + "\n")
        print(f"summary written to {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
