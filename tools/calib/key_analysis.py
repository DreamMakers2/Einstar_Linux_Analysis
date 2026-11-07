#!/usr/bin/env python3
"""What can (and cannot) be proved about the CCF key's unproved entries.

The CCF stores ``stored = true + key`` per field.  For the IR records the key
is *proved* for the rotation block (orthonormality), the five distortion
coefficients (exact integers against the unkeyed dump record) and alpha (the
only physical integer).  It is **not** proved for ``fx, fy, cx, cy``: a common
integer shift of those four would leave every intrinsic invariant intact.

This script quantifies the remaining freedom instead of guessing:

* the unkeyed 22-double records in the dump are a *second* calibration of the
  same three cameras; their distortion block is bit-identical to the CCF's, so
  the distortion key is exact, while their intrinsics/extrinsics differ by the
  amount a re-calibration moves them;
* the colour camera is stored unkeyed in *both* encodings, so its difference
  ``CCF - dump`` is a direct, key-free measurement of that re-calibration
  spread (``tau``);
* every integer key consistent with ``|stored - dump - key| <= tau`` is
  feasible.  A second, stronger model assumes the re-calibration offset is the
  same systematic vector for all three cameras and estimates it from the
  colour camera.

It also demonstrates the *negative* result: rectification is exact for **any**
``K``, so epipolar geometry cannot pin the focal length (the epipole only
moves off-image).  Depth uncertainty from a wrong focal length is
``dZ = Z * df / f``; from a wrong baseline ``dZ = Z * dB / B``.

Run::

    python3 tools/calib/key_analysis.py
    python3 tools/calib/key_analysis.py --json-out /tmp/key_analysis.json
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from calib_model import (CCF_DIR, DUMP_PATH, IDX_CX, IDX_CY, IDX_FX, IDX_FY,
                         Calibration, StereoPair, ccf)
from rectify import bouguet_rectification

#: Fields whose absolute key is not proved.
UNPROVED = {"fx": IDX_FX, "fy": IDX_FY, "cx": IDX_CX, "cy": IDX_CY}


def _stored(directory: Path) -> dict[str, np.ndarray]:
    out = {}
    for name, fname in (("left", "LeftCCF.txt"), ("right", "RightCCF.txt"),
                        ("tex", "TexCCF.txt")):
        rec = ccf.decode_record((directory / fname).read_bytes(), fname, "best")
        out[name] = np.asarray(rec.stored, float)
    return out


def _dump_records(path: Path) -> dict[str, np.ndarray]:
    import struct
    data = path.read_bytes()
    names = ("left", "right", "tex")
    return {n: np.array(struct.unpack_from("<22d", data, 0x00C + i * 0xF8), float)
            for i, n in enumerate(names)}


@dataclass
class KeyAnalysis:
    tau: float
    delta_colour: dict[str, float]
    base: dict[str, dict[str, float]]
    feasible: dict[str, dict[str, list[int]]]
    common_offset: dict[str, dict[str, int]]
    square_pixel: dict[str, Any]
    depth_uncertainty: dict[str, float]

    def as_dict(self) -> dict[str, Any]:
        return {
            "tau_px": self.tau,
            "colour_ccf_minus_dump_px": self.delta_colour,
            "unkeyed_stored_minus_dump_px": self.base,
            "feasible_keys": self.feasible,
            "common_offset_keys": self.common_offset,
            "square_pixel_constraint": self.square_pixel,
            "depth_uncertainty_mm_at_300mm": self.depth_uncertainty,
        }


def analyse(ccf_dir: Path = CCF_DIR, dump_path: Path = DUMP_PATH,
            tau: float | None = None, focal_nominal: float = 1159.0,
            baseline_nominal: float = 158.557, z: float = 300.0) -> KeyAnalysis:
    stored = _stored(ccf_dir)
    dump = _dump_records(dump_path)

    # --- key-free measurement of the re-calibration offset (colour camera) ---
    delta = {
        "fx": float(stored["tex"][IDX_FX] - dump["tex"][0]),
        "fy": float(stored["tex"][IDX_FY] - dump["tex"][1]),
        "cx": float(stored["tex"][IDX_CX] - dump["tex"][2]),
        "cy": float(stored["tex"][IDX_CY] - dump["tex"][3]),
    }
    if tau is None:
        tau = max(abs(v) for v in delta.values())

    base = {cam: {} for cam in ("left", "right")}
    feasible = {cam: {} for cam in ("left", "right")}
    common = {cam: {} for cam in ("left", "right")}
    for cam in ("left", "right"):
        for fname, didx in (("fx", 0), ("fy", 1), ("cx", 2), ("cy", 3)):
            b = float(stored[cam][UNPROVED[fname]] - dump[cam][didx])
            base[cam][fname] = b
            k_lo = int(np.floor(b - tau))
            k_hi = int(np.ceil(b + tau))
            feasible[cam][fname] = [k for k in range(k_lo, k_hi + 1)
                                    if abs(b - k) <= tau + 1e-12]
            common[cam][fname] = int(np.round(b - delta[fname]))

    # --- square-pixel constraint on k0 - k1 ---------------------------------
    sq = {}
    for cam in ("left", "right"):
        diff = float(stored[cam][IDX_FX] - stored[cam][IDX_FY])
        sq[cam] = {
            "stored_fx_minus_fy": diff,
            "implied_key0_minus_key1": int(np.round(diff)),
            "residual_fx_minus_fy_px": diff - round(diff),
            "alternative_11_residual_px": diff - 11.0,
            "alternative_13_residual_px": diff - 13.0,
        }
    sq["colour_fx_minus_fy_px"] = float(stored["tex"][IDX_FX] - stored["tex"][IDX_FY])

    # --- depth uncertainty --------------------------------------------------
    calib_ccf = Calibration.from_ccf_dir(ccf_dir)
    calib_dump = Calibration.from_dump(dump_path)
    Bc = calib_ccf.baseline("left", "right")
    Bd = calib_dump.baseline("left", "right")
    f = focal_nominal
    depth_unc = {
        "z_mm": z,
        "depth_per_px_focal_mm": z / f,
        "depth_for_1px_key_ambiguity_mm": z / f * 1.0,
        "depth_per_px_disparity_at_z_mm": z * z / (f * Bc),
        "depth_half_px_quantisation_mm": 0.5 * z * z / (f * Bc),
        "baseline_ccf_mm": Bc,
        "baseline_dump_mm": Bd,
        "baseline_spread_mm": abs(Bd - Bc),
        "depth_from_baseline_spread_mm": z * abs(Bd - Bc) / Bc,
        "lateral_per_px_principal_point_mm": z / f,
        "depth_from_1px_differential_cx_mm": z * z / (f * Bc),
    }
    return KeyAnalysis(tau=float(tau), delta_colour=delta, base=base,
                       feasible=feasible, common_offset=common,
                       square_pixel=sq, depth_uncertainty=depth_unc)


def epipolar_constraint_demo(ccf_dir: Path = CCF_DIR,
                             keys=(0, 4, 8, 9, 12, 16)) -> list[dict[str, float]]:
    """Show that epipolar rectification cannot recover the focal-key.

    For each candidate ``fx`` key we rebuild the model, rectify, and measure
    the rectified row residual on synthetic points.  It stays at the numerical
    floor for every key while the epipole moves -- i.e. the geometry is
    invariant, the *scale* is not.
    """
    import struct
    out = []
    rng = np.random.default_rng(7)
    for k in keys:
        calib = Calibration.from_ccf_dir(ccf_dir)
        key = np.zeros(33)
        key[IDX_FX] = k
        for cam in ("left", "right"):
            c = calib.cameras[cam]
            # rebuild this camera's K with the candidate fx
            fx = float(c.K[0, 0] - 0.0)  # already decoded with key 8
            fx_candidate = fx + (8 - k)  # undo 8, apply k
            c.K = np.array([[fx_candidate, c.K[0, 1], c.K[0, 2]],
                            [0.0, c.K[1, 1], c.K[1, 2]],
                            [0.0, 0.0, 1.0]])
        pair = StereoPair.from_calibration(calib, "left", "right")
        rect = bouguet_rectification(pair, calib.image_size)
        X = rng.uniform([-100, -80, 250], [100, 80, 450], size=(500, 3))
        Xa = X @ rect.R1
        Xb = Xa @ pair.R.T + pair.t
        pa = pair.a.project(Xa)
        pb = pair.b.project(Xb)
        ra = rect.rectify_pixels_a(pa)
        rb = rect.rectify_pixels_b(pb)
        out.append({
            "fx_key": int(k),
            "fx_px": float(fx_candidate),
            "rectified_row_residual_px": float(np.abs(ra[:, 1] - rb[:, 1]).max()),
            "epipole_x_px": float(calib.epipole("left", "right")[0]),
        })
    return out


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ccf-dir", type=Path, default=CCF_DIR)
    ap.add_argument("--dump", type=Path, default=DUMP_PATH)
    ap.add_argument("--json-out", type=Path, default=None)
    ap.add_argument("--skip-epipolar-demo", action="store_true")
    args = ap.parse_args(argv)

    if not Path(args.dump).exists():
        print(f"dump not present at {args.dump}; cannot run the cross-check")
        return 2
    a = analyse(args.ccf_dir, args.dump)
    print("== key-free measurement of the re-calibration offset ==")
    print("   colour camera (unkeyed in both encodings), CCF - dump [px]:")
    print("   " + ", ".join(f"{k}={v:+.6f}" for k, v in a.delta_colour.items()))
    print(f"   tau = max|offset| = {a.tau:.6f} px")

    print("\n== unkeyed `stored - dump` [px] and feasible integer keys ==")
    print(f"   {'cam':5} {'field':3} {'stored-dump':>14}  feasible keys        common-offset key")
    for cam in ("left", "right"):
        for f in ("fx", "fy", "cx", "cy"):
            print(f"   {cam:5} {f:3} {a.base[cam][f]:14.6f}  "
                  f"{str(a.feasible[cam][f]):20}  {a.common_offset[cam][f]:+d}")

    print("\n== square-pixel constraint ==")
    for cam in ("left", "right"):
        s = a.square_pixel[cam]
        print(f"   {cam}: stored fx-fy = {s['stored_fx_minus_fy']:.6f} -> "
              f"key0-key1 = {s['implied_key0_minus_key1']} "
              f"(residual {s['residual_fx_minus_fy_px']:+.4f} px; "
              f"k=11 would leave {s['alternative_11_residual_px']:+.4f}, "
              f"k=13 {s['alternative_13_residual_px']:+.4f})")
    print(f"   colour camera fx-fy = {a.square_pixel['colour_fx_minus_fy_px']:+.6f} px")

    print("\n== depth uncertainty ==")
    for k, v in a.depth_uncertainty.items():
        print(f"   {k:45} {v:.6f}")

    payload = a.as_dict()
    if not args.skip_epipolar_demo:
        demo = epipolar_constraint_demo(args.ccf_dir)
        payload["epipolar_demo"] = demo
        print("\n== epipolar geometry does not constrain the focal key ==")
        print(f"   {'fx key':>6} {'fx [px]':>10} {'max row residual [px]':>22} {'epipole x [px]':>15}")
        for row in demo:
            print(f"   {row['fx_key']:6d} {row['fx_px']:10.3f} "
                  f"{row['rectified_row_residual_px']:22.3e} {row['epipole_x_px']:15.1f}")

    if args.json_out:
        args.json_out.write_text(json.dumps(payload, indent=2) + "\n")
        print(f"\nwritten to {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
