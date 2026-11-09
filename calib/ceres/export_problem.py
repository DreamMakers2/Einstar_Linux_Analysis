#!/usr/bin/env python3
"""Export a calibration problem to the text format read by calib_ref / calib_ceres.

The exported file carries the same *initial* values the SciPy engine starts
from (OpenCV per-camera intrinsics + PnP rig + per-view poses), so a C++ run is
directly comparable with ``calib.calibrate.calibrate``.

Usage
-----
    python calib/ceres/export_problem.py --case synth12 --out /tmp/p.txt
    python calib/ceres/export_problem.py --case vendor25 --out /tmp/v.txt
    python calib/ceres/export_problem.py --case synth12 --out ... --fixed-distortion
"""

from __future__ import annotations

import argparse
import pickle
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from calib import ccf, plate                      # noqa: E402
from calib import calibrate as engine             # noqa: E402
from calib.calibrate import CAMERAS, Observation  # noqa: E402
from calib.geometry import rvec_from_R            # noqa: E402

CCF_DIR = ROOT / "sources/calibration/res_Einscan-E10_200x150"
VENDOR_PKL = ROOT / "analysis/calibration/engine/vendor_detections.pkl"
CAM_KEY = {"Left": "left", "Right": "right", "Tex": "tex"}
KIND = {"left": "Left", "right": "Right", "tex": "Tex"}


def _layout(obs):
    used = sorted({int(o.view) for o in obs})
    remap = {v: i for i, v in enumerate(used)}
    return [Observation(o.camera, remap[int(o.view)], o.uv) for o in obs], len(used)


def build(name: str):
    """Return (obj, obs, init_cameras, pitch_mm)."""
    if name.startswith("synth"):
        from calib import synth
        n = int(name.replace("synth", "") or 12)
        sc = synth.make_scene(n_views=n, seed=0)
        init = {c: engine.CameraIntrinsics(1000.0, 1000.0, 640.0, 512.0, np.zeros(5))
                for c in CAMERAS}
        return plate.board_points_mm(sc.pitch_mm), sc.observations, init, sc.pitch_mm

    raw = pickle.loads(VENDOR_PKL.read_bytes())
    obs = [Observation(CAM_KEY[cam], int(view),
                       {tuple(k): np.asarray(v, float) for k, v in uv.items()})
           for (cam, view), uv in raw.items()]
    init = {CAM_KEY[n]: engine.CameraIntrinsics.from_ccf(
        ccf.load(CCF_DIR / f"{n}CCF.txt", ir=(n != "Tex")))
        for n in ("Left", "Right", "Tex")}
    return plate.board_points_mm(plate.DEFAULT_PITCH_MM), obs, init, plate.DEFAULT_PITCH_MM


def write(path: str | Path, obj, obs, init, pitch_mm: float,
          huber_delta: float = 1.0, free_distortion: bool = True) -> dict:
    obs, n_views = _layout(obs)
    cams0 = engine._initial_intrinsics(obs, init, obj, True)
    views0, extr0 = engine._initial_poses(obs, cams0, obj)
    lines = [
        "# einstar calibration problem v1 (see calib/ceres/README.md)",
        f"n_cameras {len(CAMERAS)}",
        f"n_views {n_views}",
        f"n_points {len(obj)}",
        f"pitch_mm {pitch_mm!r}",
        f"huber_delta {huber_delta!r}",
        f"free_distortion {1 if free_distortion else 0}",
    ]
    for i, c in enumerate(CAMERAS):
        v = cams0[c].as_vec()
        lines.append("intrinsics %d %s" % (i, " ".join(f"{t:.12g}" for t in v)))
    for i, c in enumerate(("right", "tex"), start=1):
        R, t = extr0[c]
        v = np.concatenate([rvec_from_R(R), t])
        lines.append("extrinsics %d %s" % (i, " ".join(f"{t:.12g}" for t in v)))
    for i, X in enumerate(obj):
        lines.append("point %d %.12g %.12g %.12g" % (i, X[0], X[1], X[2]))
    for v, (R, t) in enumerate(views0):
        p = np.concatenate([rvec_from_R(R), t])
        lines.append("view %d %s" % (v, " ".join(f"{q:.12g}" for q in p)))
    for o in obs:
        idx = [engine._UV_INDEX[k] for k in o.uv if k in engine._UV_INDEX]
        if not idx:
            continue
        uv = np.array([o.uv[tuple(plate.lattice_uv()[i])] for i in idx], float)
        cam = CAMERAS.index(o.camera)
        for j, i in enumerate(idx):
            lines.append("obs %d %d %d %.9g %.9g" % (cam, o.view, i, uv[j, 0], uv[j, 1]))
    Path(path).write_text("\n".join(lines) + "\n")
    return {"path": str(path), "n_views": n_views, "n_points": len(obj),
            "n_observations": len(obs)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", default="synth12")
    ap.add_argument("--out", required=True)
    ap.add_argument("--huber", type=float, default=1.0)
    ap.add_argument("--fixed-distortion", action="store_true")
    a = ap.parse_args()
    obj, obs, init, pitch = build(a.case)
    info = write(a.out, obj, obs, init, pitch, huber_delta=a.huber,
                 free_distortion=not a.fixed_distortion)
    print(f"wrote {info['path']}  views={info['n_views']} "
          f"points={info['n_points']} obs_groups={info['n_observations']}")


if __name__ == "__main__":
    main()
