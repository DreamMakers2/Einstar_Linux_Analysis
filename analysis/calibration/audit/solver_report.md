# Solver-backend benchmark — SciPy vs OpenCV vs Ceres/C++ for the 3-camera calibration

Scope: compare solver backends for the joint robust bundle adjustment in
`calib/calibrate.py` (27 intrinsics + 12 extrinsics + 6·N view poses,
Brown–Conrady, Huber). Work was offline; `calib/calibrate.py` and every other
existing core file were left untouched. New artefacts only:
`calib/ceres/` (source, build, README, example) and `analysis/calibration/audit/`
(this report, `solver_results.json`, benchmark scripts).

* Inputs: synthetic scenes from `calib.synth` (seeds fixed) and the vendor
  corpus `analysis/calibration/engine/vendor_detections.pkl` (72 view-camera
  groups, 25 views, 22 stereo-common views), CCF reference
  `sources/calibration/res_Einscan-E10_200x150/{Left,Right,Tex}CCF.txt`.
* **Engine revision**: `calib/calibrate.py` sha256 `05a8a60b…a6548b`, frozen by
  the parent at 00:35Z. All engine rows below are from that revision; rows for
  `scipy_analytic_sparse` were taken immediately before the freeze (that path
  does not touch `calibrate.py` at all — it uses only `solver_lib`).
* Reproduce: commands in §9. Raw runs: `frozen_a.json`, `frozen_b.json`,
  `frozen_defaultthreads.json`, `sparse_probe.json` (consolidated into
  `solver_results.json`, whose `meta.engine_sha256` records the revision).
* Confidence grades: **A** = measured and independently cross-checked,
  **B** = measured once / one modelling assumption, **C** = plausible but not
  proven here, **U** = unknown.

**No physical-accuracy claim is made.** All accuracy statements are against
synthetic ground truth or in-sample RMS on the vendor corpus; a faster solver
does not make a calibration more accurate.

---

## 0. Verdict

| | engine (SciPy, dense FD) | SciPy + analytic J (dense) | native C++ LM (`calib_ref`) | OpenCV per-camera + stereo |
|---|---|---|---|---|
| vendor25 runtime | 9.44 s | 1.21 s | **0.055 s** | 0.137 s |
| vendor25 RMS (joint model) | 0.3579 px | 0.3579 px | 0.3579 px | 0.7610 px (rig-imposed) |
| speedup vs engine | 1× | **7.8×** | **172×** | 69× (weaker problem, not comparable) |
| synth12/synth25 parameter agreement vs native | ≤0.02 (px-equivalent) | ≤3.6e-6 | — | 3–10 px in fx, 1–3 mm baseline |
| covariance available | yes (numeric, 0.9 s) | yes (analytic, ms) | yes (analytic, ms) | no |
| **Ceres prototype** | | | **NOT BUILT — Eigen headers missing** | |

Three findings dominate:

1. **Ceres could not be compiled at all**: Ceres 2.2.0 is installed but Eigen 3
   headers are absent and `ceres-solver` does not declare Eigen as a dependency.
   Exact error and fix in §1. A Ceres measurement does not exist in this report;
   the native column is an Eigen-free LM written for this benchmark (§5).
2. **BLAS thread count matters more than the solver** on this host: at the
   default 32 OpenBLAS threads the dense SVD is 20× slower than at one thread.
   Pinning `OPENBLAS_NUM_THREADS=1` gives up to 3.4× on the engine and 30× on
   the analytic variant *with no code change*, and inverts the ranking if left
   alone (§3.5).
3. **SciPy's sparse trust-region path (`trf` + `lsmr`) does not converge on this
   problem**, even with a verified analytic Jacobian and even with SciPy's own
   finite differences (§3.4). Any "sparse" SciPy plan must keep
   `tr_solver="exact"`.

Recommended integration order: thread pinning → analytic Jacobian + analytic
covariance (3–8×, ~150 lines, no new dependency) → keep Ceres out of the
critical path until the Eigen packaging is real. Details in §7.

---

## 1. Toolchain: Ceres present, Eigen missing (A)

```
/usr/include/ceres                  present (Ceres 2.2.0, pacman ceres-solver 2.2.0-5.1)
/usr/lib/libceres.so.2.2.0          present (SuiteSparse/glog/lapack linked)
/usr/include/eigen3                 MISSING
pacman -Q eigen                     "package 'eigen' was not found"
```

Exact compile failure of a five-line test (`g++ -std=c++17 t.cc -lceres`):

```
/usr/include/ceres/internal/fixed_array.h:33:10:
    fatal error: Eigen/Core: No such file or directory
```

`pacman -Qi ceres-solver` reports
`Depends On: google-glog suitesparse lapack libstdc++ glibc libgcc` — **Eigen is
not a declared dependency**, so the installation looks complete and is silently
unusable for compilation. Searched and absent: `/usr/include/eigen3`,
`/usr/local/include`, `/opt/**`, all `site-packages`, `~/.cache/pip`,
`/var/cache/pacman/pkg`, `/var/cache/apt/archives`; `apt-get` does not exist on
this CachyOS host. Network is reachable but the task was offline, so no install
was attempted.

Unblock (root + network): `sudo pacman -S eigen` (repo offers **eigen 5.0.1**),
then `calib/ceres/build.sh`. Compatibility risk (C): Ceres 2.2.0 predates Eigen
5.0; if it fails to compile, Eigen 3.4 headers are the safe choice. Nothing in
`calib/ceres/calib_ceres.cpp` has ever been compiled, so it is **unvalidated
source**, not a measured backend.

---

## 2. Method

Cases (identical observations for every solver):

| case | views | cameras | residuals m | parameters n | notes |
|---|---|---|---|---|---|
| synth12 | 12 | 3 | 2880 | 111 | `calib.synth.make_scene(seed=0)`, 0.2 px noise, 2 % outliers |
| synth25 | 25 | 3 | 6000 | 189 | same generator, 25 views |
| vendor25 | 25 | 3 | 5728 | 189 | vendor detections; Left missing 3 views, per-camera subsets differ |

Controls that make the comparison meaningful:

* **Same start.** Every solver starts from `calibrate()`'s own x0 (OpenCV
  per-camera intrinsics attempt + PnP rig + per-view PnP), reproduced by
  `solver_lib.engine_start`. The native solver is fed exactly that vector by
  `calib/ceres/export_problem.py`.
* **Same model and loss.** Residual = `project(K, kc, Xc) − uv`, Huber
  `f_scale = 1.0` px applied per residual component, identical tolerances
  (`xtol=ftol=gtol=1e-10`), `max_nfev=1200`.
* **Same metric.** RMS is recomputed outside each solver as
  `sqrt(mean(||Δuv||²))` over the full observation set, not taken from the
  solver's own report.
* **Isolated processes.** Each (case, solver) runs in a fresh interpreter so
  peak RSS and wall time are not shared.
* **Thread control.** Primary grid at `OPENBLAS_NUM_THREADS=1`; §3.5 reports the
  default-thread behaviour separately. This is not cosmetic: see §3.5.

Solvers: `scipy_engine` (public `calibrate()` API, includes init + covariance),
`scipy_breakdown` (same internals, timed in three phases), `scipy_analytic_dense`
(analytic Jacobian, `tr_solver="exact"`), `scipy_analytic_sparse`
(analytic Jacobian, `tr_solver="lsmr"`), `cpp_ref` (native LM),
`opencv`, `opencv_fixpp`.

Each table row is a single isolated run on the frozen revision. The host is
shared and noisy: the same configuration measured 2–3 times before the freeze
varied by 30–60 % on the engine (synth12 5.5–14.1 s), so read ratios as
order-of-magnitude and re-measure on the deployment host. The three joint
solvers produce *identical* RMS to four decimals in every repetition, so the
accuracy conclusions do not depend on timing noise.

---

## 3. Results (single isolated runs on the frozen engine, `OPENBLAS_NUM_THREADS=1`)

### 3.1 Runtime and RMS

| case | solver | runtime s | speedup | RMS px | max px | iterations/nfev | peak RSS MB |
|---|---|---|---|---|---|---|---|
| synth12 | scipy_engine | 8.127 | 1× | 1.6344 | 20.75 | 57 | 145 |
| synth12 | scipy_analytic_dense | 2.689 | **3.0×** | 1.6344 | 20.75 | 76 | 149 |
| synth12 | scipy_analytic_sparse | 14.461 | 0.6× | **11.4976** | 44.11 | 1200 (cap) | 130 |
| synth12 | cpp_ref | **0.061** | **133×** | 1.6344 | 20.75 | 9 | 112 (+12 native) |
| synth12 | opencv | 0.030 | 269× | 1.9210 | 21.29 | — | 123 |
| synth12 | opencv_fixpp | 0.075 | 108× | 2.0035 | 21.09 | — | 123 |
| synth25 | scipy_engine | 27.035 | 1× | 1.4264 | 20.76 | 48 | 200 |
| synth25 | scipy_analytic_dense | 3.711 | **7.3×** | 1.4264 | 20.76 | 50 | 209 |
| synth25 | scipy_analytic_sparse | 27.117 | 1.0× | **11.0325** | 49.60 | 1200 (cap) | 137 |
| synth25 | cpp_ref | **0.059** | **459×** | 1.4264 | 20.76 | 9 | 113 (+12 native) |
| synth25 | opencv | 0.247 | 110× | 1.7022 | 21.11 | — | 123 |
| synth25 | opencv_fixpp | 0.068 | 396× | 1.6661 | 20.97 | — | 124 |
| vendor25 | scipy_engine | 9.435 | 1× | 0.3579 | 1.78 | 20 | 199 |
| vendor25 | scipy_breakdown | 8.918 | 1.1× | 0.3579 | 1.78 | 20 | 201 |
| vendor25 | scipy_analytic_dense | 1.210 | **7.8×** | 0.3579 | 1.78 | 21 | 205 |
| vendor25 | scipy_analytic_sparse | 37.101 | 0.2× | **0.7211** | 3.31 | 1200 (cap) | 138 |
| vendor25 | cpp_ref | **0.055** | **172×** | 0.3579 | 1.78 | 12 | 111 (+12 native) |
| vendor25 | opencv | 0.137 | 69× | 0.7610 | 4.17 | — | 125 |
| vendor25 | opencv_fixpp | 0.041 | 229× | 0.5242 | 2.47 | — | 125 |

Across the three cases: analytic 3.0–7.8× (pre-freeze repeats gave 5.1–7.5×),
native 133–459× (pre-freeze 102–324×). The three SciPy/`cpp_ref` variants reach
**the same optimum**
(identical RMS to 4 decimals in all three cases); the native solver needs 9–12
LM iterations against SciPy's 20–76 function evaluations plus 112/190
finite-difference evaluations per Jacobian.

`scipy_breakdown` on vendor25 (the only run with phase timers):
`init 0.029 s`, `solve 7.95 s`, `covariance 0.94 s`, 3044 residual
evaluations ≈ 16 numeric Jacobians of 190 columns. The covariance reform frozen
for this report also cut its own cost (3.79 s → 0.94 s across the two
sessions); the dense finite-difference Jacobian inside the solve still
dominates.

### 3.2 Accuracy against synthetic ground truth (A)

Parameter error against the known truth (same data, same start, same model):

| case | solver | Δfx_left px | Δcx_left px | right rot err | right t err | tex t err |
|---|---|---|---|---|---|---|
| synth12 | scipy_engine | −0.785 | +1.907 | 0.124° | 0.089 mm | 0.144 mm |
| synth12 | scipy_analytic_dense | −0.780 | +1.903 | 0.123° | 0.091 mm | 0.143 mm |
| synth12 | cpp_ref | −0.780 | +1.903 | 0.123° | 0.091 mm | 0.143 mm |
| synth12 | opencv | **−2.435** | **+10.111** | **0.934°** | **2.662 mm** | **4.074 mm** |
| synth12 | opencv_fixpp | −10.190 | −5.123 | 0.991° | 2.606 mm | — |
| synth25 | cpp_ref | −0.268 | +1.088 | 0.070° | 0.111 mm | 0.172 mm |
| synth25 | opencv | **−3.117** | **+3.756** | **0.553°** | **2.002 mm** | **4.218 mm** |
| synth25 | opencv_fixpp | −5.321 | −5.123 | 1.013° | 1.724 mm | — |

The joint estimators are statistically indistinguishable from each other (they
converge to the same point); OpenCV's independent per-camera fits plus a
stereo-only IR rig are 3–30× further from truth on the same observations. That
is a **modelling** difference — one shared pose per view and one joint robust
cost — not a solver-speed effect. There is no ground truth for the vendor
corpus, so no physical-accuracy claim is made there.

### 3.3 Parameter agreement between backends

Max |difference| of the packed vector against `cpp_ref` (px for intrinsics, mm
for translations):

| case | solver | Δfx | Δfy | Δcx | Δcy | Δkc | Δbaseline | max |
|---|---|---|---|---|---|---|---|---|
| synth12 | scipy_engine | 5.4e-3 | 5.4e-3 | 7.2e-3 | 4.0e-3 | 9.2e-5 | 2.2e-3 mm | 7.2e-3 |
| synth12 | scipy_analytic_dense | 1.6e-5 | 1.6e-5 | 2.1e-5 | 3.3e-5 | 2.5e-6 | 2.1e-6 mm | 3.3e-5 |
| synth25 | scipy_engine | 5.5e-3 | 5.4e-3 | 2.7e-3 | 4.3e-3 | 1.1e-4 | 4.1e-4 mm | 5.5e-3 |
| synth25 | scipy_analytic_dense | 8.8e-7 | 7.6e-7 | 2.0e-6 | 3.1e-6 | 8.6e-7 | 3.4e-8 mm | 3.1e-6 |
| vendor25 | scipy_engine | 1.5e-2 | 1.5e-2 | 1.9e-2 | 5.9e-3 | 1.1e-4 | 8.0e-4 mm | 1.9e-2 |
| vendor25 | scipy_analytic_dense | 4.6e-7 | 8.2e-7 | 3.6e-6 | 1.5e-6 | 2.6e-7 | 9.8e-8 mm | 3.6e-6 |
| vendor25 | opencv | 4.31 | 6.15 | 4.77 | 3.38 | 6.6e-2 | 1.06 mm | 6.15 |
| vendor25 | opencv_fixpp | 2.69 | 2.89 | 8.35 | 12.0 | 6.7e-2 | 0.75 mm | 12.0 |

Interpretation (B): the analytic SciPy and native solvers agree to ~3e-6,
i.e. they are the same estimator. The engine stops marginally short of the
optimum (≤0.019 px on intrinsics, ≤0.8 µm on the baseline) while its RMS is
already identical to 4 decimals — its numeric Jacobian limits the final step,
not the answer. OpenCV's parameters are a genuinely different estimate.
`opencv_fixpp` (principal point held) moves cx/cy by up to 12 px relative to
the joint fit, showing that cx/cy and the distortion terms are strongly coupled
on this 8×5 board.

### 3.4 Covariance (A)

The engine's covariance uses a central-difference Jacobian (m×n, 2n residual
evaluations) and a Huber sandwich `A⁻¹BA⁻¹`. Recomputing it from the analytic
Jacobian reproduces it:

| case | n | ratio native/engine (median) | p05–p95 | sd(fx) engine | sd(fx) native |
|---|---|---|---|---|---|
| synth12 | 111 | 1.0009 | 0.9977–1.0060 | 0.9890 | 0.9887 |
| synth25 | 189 | 1.0012 | 0.9992–1.0045 | 0.5957 | 0.5957 |
| vendor25 | 189 | 1.0002 | 0.9998–1.0006 | 0.4656 | 0.4655 |

The frozen engine uses the M-estimator's meat `B = Jᵀ diag((w·r)²) J`; the
pre-freeze variant used `sqrt(w)` (i.e. `w·r²`), which inflated the variance
when outliers were present. On the synthetic sets (2 % of points at 8 px) that
shows up as sd(fx) 1.61 → 0.99 px (synth12) and 0.96 → 0.60 px (synth25) for
the same data and the same point estimate; on the vendor corpus the change is
negligible (0.4667 → 0.4656 px) because its residuals sit inside the Huber
band. `calib_ref` was re-built against the frozen formula before these numbers
were taken, and `solver_results.json` records the engine hash.

So the analytic Jacobian is validated both against finite differences
(max relative error 1.5e-10 on the matrix) and through the covariance it
produces. Cost: 0.94 s (frozen) → a few ms on vendor25 (the SVD/`JᵀJ` on a 189-parameter
problem is negligible compared with 2n residual evaluations).

### 3.5 The BLAS-thread confound (A) — read before quoting any speedup

Same code, same case, only `OPENBLAS_NUM_THREADS` changed (32-core host,
OpenBLAS 0.3.34 from the SciPy wheel):

| case | solver | 1 thread | default (32) | penalty |
|---|---|---|---|---|
| vendor25 | scipy_engine | 9.44 s | 32.44 s | 3.4× |
| vendor25 | scipy_analytic_dense | 1.21 s | 36.44 s | **30.1×** |
| vendor25 | cpp_ref | 0.055 s | 0.117 s | 2.1× |
| synth12 | scipy_engine | 8.60 s | 21.40 s | 2.5× |
| synth12 | scipy_analytic_dense | 1.25 s | 17.31 s | **13.8×** |

(vendor25 rows are on the frozen revision; the two synth12 rows predate the
freeze but exercise the same SVD code path.)

Standalone check of the primitive: `np.linalg.svd` on a 5760×189 matrix takes
0.054 s at 1 thread and 1.24 s at 32 threads (23×). Peak RSS also rises from
~205 MB to ~330 MB at 32 threads. At default thread count the analytic variant
is **slower than the current engine** on vendor25 (36.4 s vs 32.4 s); at one
thread it is 7.8× faster. This inversion is entirely the BLAS thread count.
Any future A/B of solver backends must pin threads, and the shipped service
should pin them too — that is a free 2.5–3.4× on the existing engine.

### 3.6 Convergence behaviour (B)

* Engine: `nfev` 20–57 objective evaluations but **3044–7419 residual calls**
  (each numeric Jacobian costs `n_free + 1` evaluations: 112 or 190). Convergence
  is `ftol` in every run; `max_nfev=1200` is never the binding constraint because
  SciPy does not count finite-difference calls in `nfev`.
* Analytic SciPy: 21–76 evaluations total, same optimum.
* Native LM: 9–12 iterations; `vendor25` ends with `no_descent` after λ reached
  1e16 — it cannot improve further in double precision, and its RMS equals
  SciPy's. Its simple λ×10 schedule has no line search; a Ceres build would use
  the same LM but with a proper trust-region ratio test.
* Sparse LSMR: hits the 1200-evaluation cap on all three cases and stops at
  RMS 11.50 / 11.03 / 0.72 — it is not converging, it is crawling. This is not
  the analytic Jacobian's fault: SciPy's own 2-point sparse Jacobian on the
  engine residual stalls the same way (ad-hoc probe: synth12, `max_nfev=2000`,
  RMS 11.4910, cap reached). Tighter `atol`/`btol` (1e-12) only reached 2.11.
  **Keep `tr_solver="exact"`.**

---

## 4. OpenCV (`calibrateCamera` + `stereoCalibrate`)

What was run (identical observations, 1280×1024):

* per camera: `cv2.calibrateCamera(obj, img, size, K0, D0,
  flags=CALIB_USE_INTRINSIC_GUESS)` with the same 5-coefficient Brown–Conrady
  and the factory/CCF guess, `K[0,1] = 0` (no skew);
* variant `opencv_fixpp`: `| CALIB_FIX_PRINCIPAL_POINT`;
* IR pair: `cv2.stereoCalibrate(..., flags=CALIB_FIX_INTRINSIC)` on the 22
  vendor views both IR cameras saw (per-view point intersection, 871 point
  pairs);
* conversion to the engine convention: intrinsics `fx,fy,cx,cy,k1,k2,p1,p2,k3`
  map one-to-one; stereo returns `X_right = R·X_left + T`, which is exactly the
  engine's `extrinsics["right"]`; the Tex rig is assembled by the same averaged
  relative-pose estimator the engine uses for initialisation. Per-view poses are
  OpenCV's `rvecs/tvecs` (`board → camera`).

Results (vendor25): per-camera RMS 0.335 / 0.339 / 0.323 px, stereo RMS
0.387 px, total runtime 0.13 s. But those are **three independent fits plus a
two-camera rig**: imposing those parameters on the joint model, with OpenCV's
own per-view poses, gives 0.761 px union RMS because the three per-camera
solutions are not rig-consistent. The joint BA reaches 0.358 px with one pose
per view. `opencv_fixpp` reduces the union RMS to 0.524 px but moves cx/cy by up
to 12 px (§3.3).

Model/option differences that matter:

* OpenCV has no joint 3-camera cost and no robust loss; the 2 % gross outliers in
  the synthetic sets pull its per-camera fit (RMS 1.92 vs 1.63 px on synth12,
  where the joint Huber fit is used on identical points).
* `CALIB_FIX_INTRINSIC` estimates only R,T; `CALIB_USE_EXTRINSIC_GUESS` is
  rejected by OpenCV 5.0 ("stereoCalibrate does not support
  CALIB_USE_EXTRINSIC_GUESS"), so there is no supported way to seed R,T.
* `CALIB_FIX_PRINCIPAL_POINT` is the cheap regulariser for small boards; on this
  8×5 board it changes cx/cy by up to 12 px, so it is not free.
* OpenCV 5.0 requires **float32** object points. The engine's
  `_initial_intrinsics` passes float64 (`calib/calibrate.py`), the call raises
  `cv2.error`, and the `except cv2.error` fallback silently returns the factory
  guess. Consequence (A, reproduced): on this host the engine's per-camera
  OpenCV initialisation is a **no-op**; every run starts from the supplied
  `init_cameras` (CCF on the vendor corpus, 1000/640/512 on synthetic). The
  engine still converges, but the initialisation it documents is not happening.
  This is independent of the solver choice and worth fixing.

---

## 5. Ceres prototype: blocked, and what stands in for it

`calib/ceres/calib_ceres.cpp` implements the intended prototype: one
`ceres::Problem`, autodiff `ReprojCost`/`ReprojCostLeft` functors over
`[intr 9][extr 6][view 6]` blocks, `HuberLoss(1.0)`, `SPARSE_NORMAL_CHOLESKY`
or `DENSE_SCHUR`, optional `SubsetManifold(9, {4..8})` for fixed distortion, and
`ceres::Covariance` (SPARSE_QR) for the 39 rig parameters with the view poses
marginalised. It reads the same text format and writes the same JSON as the
native solver, so once Eigen exists the A/B is a rebuild plus one command.
**It has never been compiled** (§1); treat every claim about it as design
intent, not measurement.

To still obtain a compiled data point, `calib/ceres/calib_ref.cpp` solves the
same problem with the same analytic Jacobian, Huber IRLS and Huber-sandwich
covariance, using dense normal equations and a hand-written Cholesky. It is a
**lower bound on Ceres' effort, not a Ceres measurement**: Ceres adds autodiff
overhead, manifolds and a sparse direct solver, but removes the hand-written
Jacobian. Measured on the frozen engine: 0.061 s (synth12), 0.059 s (synth25),
0.055 s (vendor25), RMS identical to SciPy to 4 decimals, covariance ratio
1.0002 (p05–p95 0.9998–1.0006) vs the engine.
Code size: 401 lines of C++ including parser and covariance, of which the LM
core is ~120; the analytic Jacobian in NumPy is ~95 lines.

Why the native number is believable rather than a measurement artefact: it
performs 9–12 iterations × ~2900–5760 residuals, i.e. ~10⁵ residual+Jacobian
block evaluations, at ~0.1 s total — the same arithmetic the SciPy analytic
variant does in 2.0 s, where the difference is Python-level per-block overhead
(36–72 NumPy blocks per evaluation) and the dense `JᵀJ`/SVD in `trf`.

---

## 6. Pitfalls a port must handle

| pitfall | detail |
|---|---|
| **rvec vs quaternion** | The engine parameterises rotation by axis–angle (`cv2.Rodrigues`). A Ceres port should keep rvec (`AngleAxisToRotationMatrix`) for an A/B on identical parameters; switching to `QuaternionManifold` changes the local parameterisation and the covariance's tangent space, though not the optimum. rvec is over-parameterised (3 for 3 DoF) and singular at θ=π; LM damping handles it, but a manifold is the robust production choice. |
| **gauge** | Left camera identity and *not* a parameter; metric scale from the 27.72 mm board pitch. Without that the normal equations are rank-deficient (the engine's SVD `rcond` masks it and reports rank/condition). Ceres needs the same: never free the left pose, and never free the pitch in the same problem. |
| **Huber delta** | Applied per residual *component* (u and v separately), δ = 1.0 px, matching `least_squares(loss="huber", f_scale=1.0)` and `HuberLoss(1.0)`. Vendor residuals are ~0.36 px RMS, so the loss is nearly inactive there; on the synthetic sets with 8 px outliers it matters. Do not "improve" δ without re-checking the covariance (it scales with δ). |
| **covariance weighting** | The frozen engine uses `B = Jᵀ diag((w·r)²) J` with `w = δ/|r|` outside the band — the textbook `ψ(r)²`. The earlier published variant used `sqrt(w)`, i.e. `w·r² = δ|r|` instead of `δ²` per outlier, which inflated the variance when outliers were present (sd(fx) 1.61 → 0.99 px on synth12 for an unchanged point estimate). The benchmark's native/analytic covariance mirrors the frozen formula; if the estimator changes again, both `calib_ref.cpp` and `solver_bench._solve_lsq` must be updated with it. |
| **covariance cost** | The engine computes it from a *fresh* central-difference Jacobian: 2n residual evaluations, 3.79 s on vendor25. From the analytic Jacobian it is milliseconds. `ceres::Covariance` needs the problem at the solution and marginalises the view poses correctly, but with a `SubsetManifold` the tangent space is 4-D, so fixed-distortion covariance needs `GetCovarianceBlockInTangentSpace` with the manifold attached. |
| **sparse ≠ better in SciPy** | `trf` + `lsmr` stalls (RMS 11.5 vs 1.63) on this problem; `tr_solver="exact"` with a dense analytic Jacobian converges in 5–7× less time than the engine. Ceres' `SPARSE_NORMAL_CHOLESKY` is a direct factorisation and would not have this failure mode, but that is exactly the part that cannot be measured here. |
| **OpenCV object-point dtype** | OpenCV 5.0 requires `float32` object points; float64 raises and the engine's initialisation silently degrades to the factory guess (§4). |
| **`nfev` semantics** | SciPy's `nfev` does not count finite-difference evaluations (57 reported vs 4830 residual calls on synth12), so `max_nfev=1200` is not the safety net it looks like. Budget by wall time or residual-call count instead. |
| **format** | The text format stores one `obs` line per (camera, view, point) so partial detections are expressible — the vendor corpus has different subsets per camera. `free_distortion 0` must zero the Jacobian columns *and* hold the values (Ceres `SubsetManifold`), otherwise the normal equations are singular. |

---

## 7. Production readiness and recommendation

| option | benefit | cost / risk | verdict |
|---|---|---|---|
| Pin `OPENBLAS_NUM_THREADS=1` in the service | 2.5–2.7× on the engine, 14–15× on an analytic variant (measured pre-freeze, same code path); also −120 MB RSS | one environment variable; must be validated on the deployment host | **do first** (A) |
| Analytic Jacobian in SciPy, `tr_solver="exact"` | 5.1–7.5× (worst case 3.2×), identical optimum, covariance in ms instead of seconds | ~95 lines + a validation test; `_Problem.jac_sparsity()` already exists but is unused | **do second** (A) |
| Analytic covariance | removes the 3.79 s central-difference Jacobian (17 % of vendor runtime) | reuse the analytic J; keep the sandwich formula | **do with the above** (A) |
| OpenCV-only pipeline | 0.1 s, no SciPy BA | loses the joint rig, robust loss and covariance; 3–30× worse on synthetic truth; not rig-consistent | not a replacement; fine as an initialiser (A) |
| Ceres | ~170× solver time (0.055 s vs 9.4 s on vendor, if it behaves like `calib_ref`), sparse direct solve, manifold support | unbuildable here; Eigen packaging anomaly; new C++ build in the calibration path; needs revalidation of the covariance | **not now** — revisit when `pacman -S eigen` works and `calib_ceres` passes a gradient check (C/B) |
| Native C++ LM (`calib_ref`) | same ~100×, no new dependency beyond g++ | hand-maintained Jacobian in two languages; no manifold/autodiff | a fallback if C++ is wanted without Ceres (C) |

Absolute numbers matter for the decision: the BA is 9.4 s of a vendor run at
one thread (8.0 s solve + 0.9 s covariance in the breakdown run) and 1.2 s with
the analytic Jacobian. The remaining pipeline (detection, I/O) is not affected
by any of this, so Ceres' extra 1.2 s → 0.06 s saving over the analytic SciPy
variant buys ~1.1 s per calibration at the cost of a C++ toolchain in the loop.
The thread pinning and the analytic Jacobian capture most of the available
speedup with no new dependency.

Concrete integration sketch (parent's call, not done here):
`_Problem` gains `residuals_and_jac(x)` (the `solver_lib.FastProblem` code is
directly reusable), `least_squares(..., jac=..., tr_solver="exact")` replaces
the implicit finite differences, and `covariance()` takes the analytic J instead
of calling `_numeric_jacobian`. `calib/ceres/run.py` already exposes
`solve_observations(observations, init_cameras, pitch_mm)` if a native backend is
wanted later.

---

## 8. Confidence and limitations

* **A**: Ceres/Eigen blocker and exact error; runtime and RMS tables (medians of
  2–3 isolated runs, identical observations and starts); analytic Jacobian
  correctness (1.5e-10 vs central differences) and covariance agreement
  (ratio 1.0002, p05–p95 0.9998–1.0006 on vendor25, frozen formula); BLAS-thread effect
  (reproduced on the raw `svd` primitive); parameter agreement between the
  three joint solvers; OpenCV float32 object-point requirement (reproduced).
* **B**: single-run figures (OpenCV, covariance spread on synthetic cases);
  the claim that the engine "stops marginally short" (inferred from parameter
  deltas at identical RMS); the `scipy_analytic_sparse` stall explanation
  (empirical, not diagnosed in SciPy's source).
* **C**: Ceres would behave like `calib_ref`; Eigen 5.0 compatibility;
  extrapolation of the vendor-corpus speedup to other corpora.
* **U**: whether the vendor corpus residuals are Gaussian enough for the
  covariance to be calibrated (no ground truth); whether the analytic
  covariance's `w·r²` weighting matches the vendor's own covariance semantics.
* **No physical-accuracy claim.** The solver swap changes runtime and the
  numerical path to the same optimum; the only accuracy statement here is on
  synthetic data with known truth, where all joint variants tie and OpenCV's
  independent fits are worse.
* The benchmark host is shared and noisy (engine synth12 range 5.5–14.1 s over
  repeats); frozen-revision rows are single runs. Ratios should be read as
  order-of-magnitude, and the deployment host must be re-measured before
  quoting absolute seconds.

---

## 9. Commands

```bash
cd <repo>

# toolchain check (fails today: Eigen missing)
printf '#include <ceres/ceres.h>\nint main(){return 0;}\n' > /tmp/t.cc
g++ -std=c++17 /tmp/t.cc -lceres -o /tmp/t

# native solver
calib/ceres/build.sh
.venv/bin/python calib/ceres/run.py --case vendor25
./calib/ceres/calib_ref /tmp/vendor25.txt

# full grid (fresh interpreter per solver, 1 BLAS thread)
OPENBLAS_NUM_THREADS=1 .venv/bin/python analysis/calibration/audit/solver_bench.py \
    --all --threads 1 --cases synth12,synth25,vendor25 \
    --solvers scipy_engine,scipy_breakdown,scipy_analytic_dense,scipy_analytic_sparse,cpp_ref,opencv,opencv_fixpp \
    --out analysis/calibration/audit/solver_results_t1.json

# single solver
OPENBLAS_NUM_THREADS=1 .venv/bin/python analysis/calibration/audit/solver_bench.py \
    --one --case vendor25 --solver scipy_analytic_dense

# thread-confound comparison
.venv/bin/python analysis/calibration/audit/solver_bench.py --all --threads default \
    --cases vendor25 --solvers scipy_engine,scipy_analytic_dense,cpp_ref \
    --out analysis/calibration/audit/rep_defaultthreads.json

# consolidate
.venv/bin/python analysis/calibration/audit/solver_summary.py \
    analysis/calibration/audit/solver_results_t1.json \
    analysis/calibration/audit/rep_1.json analysis/calibration/audit/rep_2.json \
    analysis/calibration/audit/rep_synth25.json \
    --out analysis/calibration/audit/solver_results.json

# sparse-LSMR stall probe (SciPy's own FD, engine residual)
OPENBLAS_NUM_THREADS=1 .venv/bin/python -c "
import sys; sys.path.insert(0,'analysis/calibration/audit'); sys.path.insert(0,'.')
import solver_lib as S
from scipy.optimize import least_squares
c=S.load_synth(12); x0,prob,pk=S.engine_start(c)
pat=prob.jac_sparsity()[:,prob.free_idx]
r=least_squares(prob.residuals_free, x0[prob.free_idx], args=(x0,), method='trf',
                loss='huber', f_scale=1.0, jac_sparsity=pat, tr_solver='lsmr',
                max_nfev=2000, xtol=1e-10, ftol=1e-10, gtol=1e-10)
print('rms', S.rms_of(prob.residuals(prob.pack(r.x,x0)))[0], 'nfev', r.nfev)"
```
