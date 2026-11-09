#!/usr/bin/env python3
"""Minimal Python-callable wrapper around the standalone C++ solvers.

This is the integration surface for the parent: it takes the same
``calib.calibrate.Observation`` objects the engine already produces, writes the
text problem, runs a binary and returns a plain dict.

    from calib.ceres.run import solve_observations
    out = solve_observations(observations, init_cameras, pitch_mm)
    x = out["x"]                      # packed like calibrate.CalibrationResult.vector()
    result = calibrate.CalibrationResult.from_vector(x, like)

Returns ``rms_px``, ``max_px``, ``iterations``, ``status``, ``x``, ``cov_diag``
and wall/CPU timings.  ``cov_diag`` is the marginal variance diagonal of the 39
rig parameters (9 per camera, then 6 per non-gauge camera); per-view poses are
nuisance parameters and are marginalized, not returned.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
for p in (str(ROOT), str(HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)

from calib.calibrate import CAMERAS  # noqa: E402

try:
    import export_problem  # running as a script
except ImportError:  # imported as calib.ceres.run
    from . import export_problem  # type: ignore


def _binary(name: str) -> Path:
    path = HERE / name
    if not path.exists():
        raise FileNotFoundError(
            f"{path} missing -- run {HERE / 'build.sh'} first")
    return path


def solve_observations(observations, init_cameras, pitch_mm: float,
                       fixed_distortion: bool = False, huber_delta: float = 1.0,
                       binary: str = "calib_ref", max_iter: int = 100,
                       obj=None) -> dict:
    """Run the C++ solver on the engine's observations."""
    if obj is None:
        from calib import plate
        obj = plate.board_points_mm(pitch_mm)
    fd, path = tempfile.mkstemp(suffix=".txt", prefix="einstar_problem_")
    Path(path).unlink(missing_ok=True)
    try:
        export_problem.write(path, obj, list(observations), init_cameras,
                             pitch_mm, huber_delta=huber_delta,
                             free_distortion=not fixed_distortion)
        return run_problem_file(path, binary=binary, max_iter=max_iter)
    finally:
        Path(path).unlink(missing_ok=True)


def run_problem_file(path: str | Path, binary: str = "calib_ref",
                     max_iter: int = 100) -> dict:
    exe = _binary(binary)
    t0 = time.perf_counter()
    p = subprocess.run([str(exe), str(path), "--max-iter", str(max_iter)],
                       capture_output=True, text=True)
    wall = time.perf_counter() - t0
    if p.returncode != 0:
        raise RuntimeError(f"{exe.name} failed ({p.returncode}): {p.stderr[-800:]}")
    out = json.loads(p.stdout.strip().splitlines()[-1])
    out["wall_s"] = wall
    out["binary"] = exe.name
    return out


def solve_case(case: str = "vendor25", fixed_distortion: bool = False,
               binary: str = "calib_ref") -> dict:
    """Convenience for the benchmark cases (synth12, synth25, vendor25)."""
    obj, obs, init, pitch = export_problem.build(case)
    out = solve_observations(obs, init, pitch, fixed_distortion=fixed_distortion,
                             binary=binary)
    out["case"] = case
    return out


def _summary(out: dict) -> str:
    x = np.asarray(out["x"], float)
    lines = [f"{out.get('case', '?')}: rms {out['rms_px']:.4f} px  "
             f"max {out['max_px']:.2f} px  iters {out['iterations']} "
             f"({out['status']})  {out['t_total_s'] * 1e3:.1f} ms"]
    for i, c in enumerate(CAMERAS):
        v = x[9 * i:9 * i + 9]
        lines.append(f"  {c:5s} fx {v[0]:9.3f} fy {v[1]:9.3f} "
                     f"cx {v[2]:9.3f} cy {v[3]:9.3f} k1 {v[4]:+.4f}")
    for i, c in enumerate(("right", "tex"), start=1):
        v = x[9 * 3 + 6 * (i - 1):9 * 3 + 6 * i]
        lines.append(f"  {c:5s} t  {v[3]:9.3f} {v[4]:9.3f} {v[5]:9.3f} mm")
    return "\n".join(lines)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", default="vendor25")
    ap.add_argument("--binary", default="calib_ref")
    ap.add_argument("--fixed-distortion", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    result = solve_case(a.case, fixed_distortion=a.fixed_distortion,
                        binary=a.binary)
    print(json.dumps(result) if a.json else _summary(result))
