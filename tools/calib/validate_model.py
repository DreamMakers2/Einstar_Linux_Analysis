#!/usr/bin/env python3
"""Invariant checks for the Einstar calibration model.

Everything asserted here is a *mathematical* property of the model, not a
claim about a physical scanner.  The checks are grouped as

1. camera model  - rotation, intrinsic matrix, distortion, principal point;
2. rig           - baseline, triangle, relative pose, epipole;
3. rectification - rotations, projection matrices, Q, map shapes;
4. geometry      - epipolar rows, Q vs DLT, map vs analytic, distortion
                   invertibility, camera-frame vs rectified-frame projections;
5. serialization - JSON round trip.

Run::

    python3 tools/calib/validate_model.py                 # both sources present
    python3 tools/calib/validate_model.py --source dump
    python3 tools/calib/validate_model.py --source ccf --verbose

Exit status is non-zero if any check fails.
"""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from calib_model import (CCF_DIR, DUMP_PATH, IMAGE_HEIGHT, IMAGE_WIDTH,
                         Calibration, StereoPair)
from rectify import bouguet_rectification
from synth import run_round_trip
from triangulate import triangulate_dlt


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


def _orthonormal(R: np.ndarray, tol: float = 1e-9) -> tuple[bool, str]:
    det = float(np.linalg.det(R))
    err = float(np.abs(R @ R.T - np.eye(3)).max())
    return (abs(det - 1.0) < tol and err < tol,
            f"det={det:.12f} max|RR^T-I|={err:.3e}")


def run_checks(calib: Calibration, verbose: bool = False) -> list[Check]:
    checks: list[Check] = []
    add = lambda n, ok, d: checks.append(Check(n, bool(ok), d))  # noqa: E731
    rect = bouguet_rectification(StereoPair.from_calibration(calib, "left", "right"),
                                 calib.image_size)

    # ---- 1. camera model -------------------------------------------------
    for name, cam in calib.cameras.items():
        ok, d = _orthonormal(cam.R)
        add(f"{name}: R orthonormal, det=+1", ok, d)
        K = cam.K
        tri = abs(K[1, 0]) < 1e-12 and abs(K[2, 0]) < 1e-12 and abs(K[2, 1]) < 1e-12
        add(f"{name}: K upper triangular with K[2,2]=1",
            tri and abs(K[2, 2] - 1.0) < 1e-12,
            f"K[1,0]={K[1,0]:.1e} K[2,0]={K[2,0]:.1e} K[2,1]={K[2,1]:.1e} K[2,2]={K[2,2]:.6f}")
        add(f"{name}: fx, fy positive and finite",
            np.isfinite([cam.fx, cam.fy]).all() and cam.fx > 0 and cam.fy > 0,
            f"fx={cam.fx:.6f} fy={cam.fy:.6f}")
        add(f"{name}: principal point inside the image",
            0 < cam.cx < IMAGE_WIDTH and 0 < cam.cy < IMAGE_HEIGHT,
            f"cx={cam.cx:.3f} cy={cam.cy:.3f} (image {IMAGE_WIDTH}x{IMAGE_HEIGHT})")
        add(f"{name}: |fx/fy - 1| < 1e-2 (square-ish pixels)",
            abs(cam.fx / cam.fy - 1) < 1e-2,
            f"fx/fy = {cam.fx / cam.fy:.8f} ({cam.fx - cam.fy:+.4f} px)")
        k1, k2, p1, p2, k3 = cam.dist
        add(f"{name}: distortion coefficients in physical range",
            all(abs(v) < 1.0 for v in (k1, k2, k3)) and abs(p1) < 1e-2 and abs(p2) < 1e-2,
            f"k1={k1:+.6f} k2={k2:+.6f} p1={p1:+.3e} p2={p2:+.3e} k3={k3:+.6f}")
        # distortion invertibility on a normalized grid
        g = np.linspace(-0.6, 0.6, 41)
        gx, gy = np.meshgrid(g, g)
        xd, yd = cam.distort(gx, gy)
        xu, yu = cam.undistort(xd, yd)
        add(f"{name}: undistort(distort(x)) == x",
            max(np.abs(xu - gx).max(), np.abs(yu - gy).max()) < 1e-12,
            f"max residual {max(np.abs(xu - gx).max(), np.abs(yu - gy).max()):.3e}")

    # ---- 2. rig ----------------------------------------------------------
    B = calib.baseline("left", "right")
    add("IR baseline in 50..250 mm", 50 < B < 250, f"|C_left - C_right| = {B:.6f} mm")
    tri = calib.rig_triangle()
    add("rig triangle sides positive and consistent with the baseline",
        all(v > 0 for v in tri.values()) and tri["left-right"] == B,
        ", ".join(f"{k}={v:.6f}" for k, v in tri.items()))
    Rab, tab = calib.relative_pose("left", "right")
    ok, d = _orthonormal(Rab)
    add("relative rotation R_ba is orthonormal", ok, d)
    add("|t_ba| equals the baseline",
        abs(np.linalg.norm(tab) - B) < 1e-9,
        f"|t_ba|={np.linalg.norm(tab):.9f} vs {B:.9f} mm")
    Cb_left = -Rab.T @ tab                     # right centre in the left camera frame
    Cb_world = calib.cameras["right"].centre() - calib.cameras["left"].centre()
    add("camera centre from relative pose matches the world-frame centre",
        np.linalg.norm(Cb_left - calib.cameras["left"].R @ Cb_world) < 1e-9,
        f"residual {np.linalg.norm(Cb_left - calib.cameras['left'].R @ Cb_world):.3e} mm")
    ep = calib.epipole("left", "right")
    add("epipole is finite and far outside the image (pair is not rectified)",
        np.isfinite(ep).all() and abs(ep[0]) > IMAGE_WIDTH,
        f"e = ({ep[0]:.3f}, {ep[1]:.3f}) px")
    add("IR relative rotation is large enough to require rectification",
        calib.relative_rotation_deg("left", "right") > 1.0,
        f"{calib.relative_rotation_deg('left', 'right'):.6f} deg")

    # ---- 3. rectification ------------------------------------------------
    ok1, d1 = _orthonormal(rect.R1)
    ok2, d2 = _orthonormal(rect.R2)
    add("R1, R2 orthonormal", ok1 and ok2, f"R1 {d1}; R2 {d2}")
    add("P1 is K_new [I|0]",
        np.allclose(rect.P1[:, :3], rect.K_new) and np.allclose(rect.P1[:, 3], 0),
        f"max deviation {np.abs(rect.P1 - np.hstack([rect.K_new, np.zeros((3,1))])).max():.3e}")
    Bp = float(np.linalg.norm(rect.pair.centre_b_in_a()))
    add("P2 is K_new [I|-B e_x]",
        np.allclose(rect.P2[:, :3], rect.K_new)
        and np.allclose(rect.P2[:, 3], [-rect.K_new[0, 0] * Bp, 0, 0], atol=1e-6),
        f"P2[:,3]={rect.P2[:,3]}")
    Qexp = np.array([[1, 0, 0, -rect.K_new[0, 2]],
                     [0, 1, 0, -rect.K_new[1, 2]],
                     [0, 0, 0, rect.K_new[0, 0]],
                     [0, 0, -1.0 / -Bp, 0]], float)
    add("Q matches the closed form for the derived P1/P2",
        np.allclose(rect.Q, Qexp, atol=1e-9),
        f"max deviation {np.abs(rect.Q - Qexp).max():.3e}")
    w, h = calib.image_size
    add("rectification maps have shape (H, W) = (1024, 1280)",
        rect.map_x_a.shape == (h, w) and rect.map_y_a.shape == (h, w)
        and rect.map_x_b.shape == (h, w) and rect.map_y_b.shape == (h, w),
        f"a={rect.map_x_a.shape} b={rect.map_x_b.shape}")
    add("rectification maps are finite",
        all(np.isfinite(m).all() for m in (rect.map_x_a, rect.map_y_a,
                                           rect.map_x_b, rect.map_y_b)),
        "all map entries finite")
    vf = rect.valid_fraction()
    add("at least half of the rectified canvas is valid in both views",
        vf["a"] > 0.5 and vf["b"] > 0.5,
        f"a={vf['a']:.4f} b={vf['b']:.4f}")

    # ---- 4. geometry -----------------------------------------------------
    rng = np.random.default_rng(12345)
    X = rng.uniform([-100, -80, 250], [100, 80, 450], size=(3000, 3))
    Xa = X @ rect.R1
    Xb = Xa @ rect.pair.R.T + rect.pair.t
    pa = rect.pair.a.project(Xa)
    pb = rect.pair.b.project(Xb)
    ra = rect.rectify_pixels_a(pa)
    rb = rect.rectify_pixels_b(pb)
    row = np.abs(ra[:, 1] - rb[:, 1]).max()
    add("rectified epipolar lines are horizontal (row residual)",
        row < 1e-9, f"max |v_a - v_b| = {row:.3e} px over {len(X)} points")

    d = ra[:, 0] - rb[:, 0]
    Xq = rect.disparity_to_xyz(d, ra)
    Xd = triangulate_dlt(rect.P1, rect.P2, ra, rb)
    add("Q triangulation and P1/P2 DLT agree",
        np.abs(Xq - Xd).max() < 1e-8,
        f"max |Q - DLT| = {np.abs(Xq - Xd).max():.3e} mm")
    add("Q triangulation reproduces the rectified-frame ground truth",
        np.abs(Xq - X).max() < 1e-8,
        f"max |X_est - X_true| = {np.abs(Xq - X).max():.3e} mm")

    # camera-frame vs rectified-frame projection matrices
    e1 = np.abs(rect.project_camera_frame(Xa, "a") - rect.project_rectified(X, "a")).max()
    e2 = np.abs(rect.project_camera_frame(Xa, "b") - rect.project_rectified(X, "b")).max()
    add("camera-frame and rectified-frame projection matrices agree",
        max(e1, e2) < 1e-8, f"max deviation {max(e1, e2):.3e} px")

    # map vs analytic inverse at integer destination pixels
    hh, ww = rect.image_size[1], rect.image_size[0]
    vv, uu = np.mgrid[0:hh:17, 0:ww:17]
    uu = uu.ravel().astype(float)
    vv = vv.ravel().astype(float)
    mx = rect.map_x_a[vv.astype(int), uu.astype(int)]
    my = rect.map_y_a[vv.astype(int), uu.astype(int)]
    okm = (mx >= 0) & (mx <= ww - 1) & (my >= 0) & (my <= hh - 1)
    src = np.stack([mx[okm], my[okm]], axis=1)
    back = rect.rectify_pixels_a(src)
    err_back = np.abs(back - np.stack([uu[okm], vv[okm]], axis=1)).max() if okm.any() else np.inf
    # the maps are stored as float32 (as OpenCV does), so the inverse is exact
    # to float32 rounding of ~1e-4 px, not to 1e-12.
    add("map is the inverse of the analytic rectification",
        err_back < 1e-3,
        f"max |analytic(map(u,v)) - (u,v)| = {err_back:.3e} px "
        f"(float32 map storage) over {int(okm.sum())} samples")

    # a full synthetic round trip at the floating-point floor
    rt = run_round_trip(rect=rect, n=1500, seed=3, integer_disparity=False)
    s = rt.summary()
    add("synthetic round trip is at the numerical floor (mm)",
        s["err3d_mm"]["max"] < 1e-8,
        f"max 3-D error {s['err3d_mm']['max']:.3e} mm, "
        f"max reprojection {max(s['reprojection_left_px']['max'], s['reprojection_right_px']['max']):.3e} px")

    # ---- 5. serialization ------------------------------------------------
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "model.json"
        calib.save_json(p)
        back = Calibration.load_json(p)
        same = all(np.allclose(calib.cameras[n].K, back.cameras[n].K)
                   and np.allclose(calib.cameras[n].R, back.cameras[n].R)
                   and np.allclose(calib.cameras[n].T, back.cameras[n].T)
                   for n in calib.cameras)
        add("JSON round trip preserves K, R, T", same,
            f"{len(calib.cameras)} cameras, {p.stat().st_size} bytes")

    if verbose:
        print(f"  model source={calib.source}  cameras={list(calib.cameras)}")
        print(f"  K_new f={rect.K_new[0,0]:.6f} cx={rect.K_new[0,2]:.6f} cy={rect.K_new[1,2]:.6f}")
        print(f"  valid fraction {vf}")
        print(f"  epipole {ep}")
    return checks


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", choices=("ccf", "dump", "both"), default="both")
    ap.add_argument("--ccf-dir", type=Path, default=CCF_DIR)
    ap.add_argument("--dump", type=Path, default=DUMP_PATH)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    labels = {
        "ccf": "OPERATIONAL (vendor stack / capture-time calibration, key bounded)",
        "dump": "INDEPENDENT UNKEYED (different calibration run; cross-check only)",
    }
    sources = []
    if args.source in ("ccf", "both"):
        sources.append(("ccf", lambda: Calibration.from_ccf_dir(args.ccf_dir)))
    if args.source in ("dump", "both"):
        if Path(args.dump).exists():
            sources.append(("dump", lambda: Calibration.from_dump(args.dump)))
        elif args.source == "dump":
            print(f"dump not found at {args.dump}")
            return 2

    total_failed = 0
    for label, factory in sources:
        calib = factory()
        checks = run_checks(calib, verbose=args.verbose)
        width = max(len(c.name) for c in checks)
        print(f"calibration model validation  [source={label}: {labels[label]}]")
        print("-" * (width + 14))
        failed = 0
        for c in checks:
            if not c.ok:
                failed += 1
            print(f"[{'PASS' if c.ok else 'FAIL'}] {c.name:<{width}}  {c.detail}")
        print("-" * (width + 14))
        print(f"{len(checks) - failed}/{len(checks)} checks passed"
              + ("" if failed == 0 else f"  ({failed} FAILED)"))
        print()
        total_failed += failed
    return 0 if total_failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
