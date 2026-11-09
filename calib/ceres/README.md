# `calib/ceres` — Ceres prototype and native reference solver

Benchmark artefacts for the question *"is a compiled Ceres solver worth
replacing the SciPy dense-finite-difference bundle adjustment?"*

**Status on this host (2026-11-10):**

| artefact | state | measured |
|---|---|---|
| `calib_ref` (Eigen-free reference LM) | **built and validated** | yes, see `analysis/calibration/audit/solver_report.md` |
| `calib_ceres` (Ceres 2.2 prototype) | **NOT BUILT — blocked, never compiled or run** | no |
| `run.py` (Python wrapper) | works for `calib_ref`; `--binary calib_ceres` will fail until unblocked | — |

---

## 1. Blocker: Eigen 3 headers are missing

Ceres 2.2.0 is installed (`/usr/include/ceres`, `/usr/lib/libceres.so.2.2.0`,
Arch package `ceres-solver 2.2.0-5.1`), but **every Ceres header needs Eigen**
and Eigen is not on this machine:

```
$ ls /usr/include/eigen3
ls: cannot access '/usr/include/eigen3': No such file or directory
$ pacman -Q eigen
error: package 'eigen' was not found
$ printf '#include <ceres/ceres.h>\nint main(){return 0;}\n' > t.cc
$ g++ -std=c++17 t.cc -lceres -o t
/usr/include/ceres/internal/fixed_array.h:33:10:
    fatal error: Eigen/Core: No such file or directory
```

Searched and absent: `/usr/include/eigen3`, `/usr/local/include`,
`/opt/**`, every `site-packages` tree, `~/.cache/pip`, `/var/cache/pacman/pkg`,
`/var/cache/apt/archives`. `pacman -Qi ceres-solver` lists `Depends On:
google-glog suitesparse lapack libstdc++ glibc libgcc` — **eigen is not a
declared dependency**, so the package looks complete while being unusable for
compilation. (Normally `ceres-solver` depends on `eigen`; this packaging does
not.)

### Unblocking (needs network + root; this host was worked offline)

```
sudo pacman -S eigen          # repo currently offers eigen 5.0.1-2
./build.sh                    # now also builds calib_ceres
```

**Compatibility risk:** Ceres 2.2.0 was released against Eigen 3.3/3.4. Eigen
5.0 is newer than that; if `calib_ceres.cpp` fails to compile against Eigen
5.0, install Eigen 3.4 headers and add `-I<eigen-3.4-root>` to `CXXFLAGS`.
Nothing in this directory has been validated against either version.

---

## 2. Files

| file | purpose |
|---|---|
| `problem_io.h` | parser + Rodrigues/derivative helpers shared by both solvers |
| `calib_ref.cpp` | Eigen-free Levenberg–Marquardt, dense normal equations. Same model, analytic Jacobian, Huber loss and Huber-sandwich covariance as `calib/calibrate.py`; used as the compiled data point and as the reference optimum the Ceres build must reproduce. |
| `calib_ceres.cpp` | the Ceres prototype: `ceres::Problem`, autodiff cost functors, `HuberLoss`, `SPARSE_NORMAL_CHOLESKY` / `DENSE_SCHUR`, optional `SubsetManifold` to fix distortion, `ceres::Covariance` for the 39 rig parameters. **Unvalidated source.** |
| `export_problem.py` | writes the text problem from the engine's own initialisation (OpenCV intrinsics + PnP rig + per-view poses) |
| `run.py` | minimal Python wrapper: `solve_observations(...)` → dict with `x`, `rms_px`, `cov_diag`, timings |
| `build.sh` | builds `calib_ref` always, `calib_ceres` only when Eigen is present, and prints the blocker otherwise |
| `examples/synth12.txt` | 12 views × 3 cameras × 40 points, 2880 residuals, 111 parameters |

---

## 3. Problem format (`v1`)

Whitespace separated, `#` starts a comment. One line per entity; one `obs`
line per (camera, view, board point) so partial detections are expressible.

```
# einstar calibration problem v1
n_cameras 3
n_views 12
n_points 40
pitch_mm 27.72
huber_delta 1.0
free_distortion 1                       # 0 = hold k1,k2,p1,p2,k3 at their input values

intrinsics 0 fx fy cx cy k1 k2 p1 p2 k3 # 3 lines, camera index 0..2
intrinsics 1 ...
intrinsics 2 ...
extrinsics 1 rx ry rz tx ty tz          # camera 1 (right) w.r.t. camera 0: X_1 = R X_0 + t
extrinsics 2 rx ry rz tx ty tz          # camera 2 (tex); camera 0 is the gauge (identity)

point 0 X Y Z                           # board frame, mm, Z = 0
...
view 0 rx ry rz tx ty tz                # board -> camera 0 pose
...
obs <cam> <view> <point> <u> <v>        # pixels, residual = pred - observed
```

Parameter vector order (identical to `calib.calibrate._Packer`):
`[intr cam0 (9)][intr cam1 (9)][intr cam2 (9)][extr right (6)][extr tex (6)][view0 (6)]...`

Notes / pitfalls baked into the format:

* **rvec, not quaternion.** The parameterisation is `cv2.Rodrigues`-compatible
  axis–angle so the optimum is directly comparable with SciPy. A Ceres build
  using `QuaternionManifold` would change the local parameterisation (and the
  covariance's tangent space) but not the optimum; keep rvec for A/B tests.
* **Gauge.** Camera 0 is identity and is not a parameter; the board pitch fixes
  the metric scale. Without that, the problem has a 6-DoF gauge freedom and the
  normal equations are singular.
* **Huber delta is in pixels and applies per residual component** (u and v
  separately), matching `scipy.optimize.least_squares(loss="huber",
  f_scale=delta)` and `ceres::HuberLoss(delta)`.
* `free_distortion 0` maps to `ceres::SubsetManifold(9, {4,5,6,7,8})` in the
  Ceres build and to zeroed Jacobian columns in `calib_ref`; the SciPy engine
  equivalent is `calib.calibrate.calibrate(free_kc=False)`.

---

## 4. Usage

```bash
./build.sh                                    # builds calib_ref (and calib_ceres if possible)
./calib_ref examples/synth12.txt --max-iter 100
python3 export_problem.py --case vendor25 --out /tmp/vendor25.txt
./calib_ref /tmp/vendor25.txt
./calib_ceres /tmp/vendor25.txt --solver schur --covariance   # only after Eigen exists
```

Python:

```python
from calib.ceres.run import solve_observations, solve_case
out = solve_case("vendor25")                       # dict: x, rms_px, iterations, cov_diag
from calib import calibrate
result = calibrate.CalibrationResult.from_vector(out["x"], like=result_template)
```

`calib_ref` writes one JSON object on stdout:

```json
{"solver":"cpp_ref_lm","n":111,"m":2880,"iterations":9,"status":"converged_step",
 "rms_px":1.63443,"max_px":20.75,"t_total_s":0.055,"x":[...],"cov_diag":[...]}
```

---

## 5. Measured (see `analysis/calibration/audit/solver_report.md` for the tables)

`calib_ref` on the identical observations and starting point as SciPy:

| case | SciPy engine | SciPy analytic dense | `calib_ref` | RMS agreement |
|---|---|---|---|---|
| synth12 (111 params) | 11.5 s | 0.96 s | 0.055 s | 1.6344 px in all three |
| synth25 (189 params) | 47.0 s | 6.33 s | 0.09 s | 1.4264 px |
| vendor25 (189 params) | 10.6 s | 1.42 s | 0.088 s | 0.3579 px |

Timings at `OPENBLAS_NUM_THREADS=1` (see the report for why that matters: the
default 32-thread OpenBLAS makes the dense SVD 20× slower).

---

## 6. What is still unproven

* `calib_ceres.cpp` has never been compiled; autodiff cost functors, the
  `SubsetManifold` usage and the `ceres::Covariance` call need a build and a
  gradient check (`ceres::GradientChecker`) before anyone trusts them.
* Covariance from `calib_ref` is the same Huber sandwich formula as the engine
  (including its `w·r²` rather than `w²·r²` weighting — see report §covariance);
  it is not an independent verification of the engine's covariance.
* No physical-accuracy claim: all comparisons are on synthetic ground truth or
  in-sample RMS. A faster solver does not make the calibration more accurate.
