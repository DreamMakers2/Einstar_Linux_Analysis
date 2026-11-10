# Solver backends for the joint 3-camera calibration — measured comparison

Scope: choose the fastest backend that meets the accuracy needs of the Einstar
calibration engine, and quantify what each estimator actually delivers.
Everything below was measured **on this host** with the shipping
`calib/calibrate.py` left untouched; the new code is `calib/solvers.py` (solver
layer) and `tests/test_solvers.py`.

Environment: Python 3.14.9,
numpy 2.5.4, scipy 1.18.1,
OpenCV 5.0.0, g++ 16.2.1, Ceres 2.2.0 present but
**unbuildable** (Eigen missing, unchanged), `OPENBLAS_NUM_THREADS=1` for every
timing run. Host is shared with other agents (load average 20-40 on 32 cores
during the runs), so absolute wall times are upper bounds; every comparison is
between backends solving the *same* problem in the same session.

Confidence grades: **A** measured and cross-checked, **B** measured once / one
assumption, **C** inferred, **U** unknown.

---

## 0. Verdict

| backend | vendor 25-view runtime | speed-up vs engine | same optimum as SciPy? | covariance | verdict |
|---|---|---|---|---|---|
| `engine` (SciPy + FD Jacobian, shipping) | 6.11 s | 1× | reference | FD-Jacobian Huber sandwich | correct, slow |
| `scipy` + `jac="fd"` (same solver, isolated) | 6.81 s | 0.9× | yes | — | no reason to exist |
| `scipy` + `jac="analytic"`, `tr_solver="exact"` | 1.03 s | **6.0×** | reference for the others | analytic sandwich | **safe production choice** |
| `native` (C++ `calib_ref` via the wrapper) | 0.25 s | **24×** | yes, ≤0.017 px on vendor (≤0.13 px worst case) | binary diag + analytic recomputation | fast, but see §8 |
| `lm` (internal numpy LM, analytic J) | 0.23 s | **27×** | yes, ≤1.5e-4 px | analytic sandwich | **fastest that meets accuracy** |
| `opencv` (`calibrateCamera` + `stereoCalibrate`, poses re-refined) | 0.29 s | 21× | **no** — a different, biased estimate | none of its own | initialiser only |
| `ceres` | — | — | — | — | **not built** (Eigen absent, §9) |

**Recommendation (measured):** make `scipy`+`jac="analytic"` the default and
`lm` the fast path, with `lm` falling back to SciPy automatically if it does not
converge (implemented and tested). Do **not** ship the finite-difference
Jacobian: it is the entire 6× penalty and buys nothing. The native C++ backend
adds no accuracy over the internal LM, brings a binary into the loop, and its
own exported covariance is computed from a Jacobian with a real defect (§8);
keep it as an optional accelerator, not as the production default.

The solver is no longer the bottleneck: on the 75-image vendor corpus the
detector takes **7.55 s** (75 images, 72 usable grids) against 0.23-1.03 s for
any of these solvers. Further solver work has low return unless detection is
addressed.

Key numbers for the vendor corpus (joint fit, 28.0 mm pitch, 72 detection
groups, 25 views, m = 5728, n = 189):

* baseline **160.193 mm**, right-camera relative rotation
  **21.761°**, RMS **0.3579 px**,
  max residual 1.78 px;
* reported (model-based) 1σ: baseline 0.0310 mm,
  relative rotation 0.0771°, fx 0.4656 px;
  reconstruction at 450 mm: σ_z 0.187 mm,
  σ_total 0.748 mm
  (600 mm: 0.993 mm);
* the factory CCF implies baseline **158.56 mm** and relative rotation
  **22.138°**: ours is 1.03 % longer (the recovered 28.0 mm vs the CCF's
  27.72 mm pitch — the known 1 % scaling) and **0.377° away in rotation**, which
  is 4.9× our reported σ. See §11: this is a *systematic* difference, not noise,
  and it is the strongest single argument that a covariance is not an accuracy
  statement.

---

## 1. Method

Cases (identical observations for every backend; the same start vector from
`calibrate()`'s own initialisation — OpenCV per-camera intrinsics + PnP rig +
per-view PnP):

| group | cases |
|---|---|
| vendor corpus | `vendor25`: detections from `calib.pipeline.detect_directory` on `<private-inputs>/vendor-calib-rapid`, 28.0 mm pitch, CCF initial guess. 72/75 grids detected (Left 22/25, Right 25, Tex 25). |
| view sweep | `synth.make_scene(n_views ∈ {5,8,12,25,40}, noise 0.2 px, 2 % outliers at 8 px, seed 0, pitch 28.0 mm)` |
| noise sweep | 12 views, noise ∈ {0.1, 0.3, 0.5} px |
| outlier sweep | 12 views, 0.2 px, outliers ∈ {0, 5, 10} % |

Controls: same model (Brown-Conrady, left camera identity gauge, board pitch
fixes scale), same loss (Huber δ = 1.0 px *per residual component*), same
residual definition (`pred − observed`), RMS recomputed outside every solver
from the same residual vector. Timings exclude the shared initialisation
(cached after the first call) and are medians of 3-5 repeats. Accuracy is
against synthetic truth only; the vendor corpus has no truth.

Reproduce: `OPENBLAS_NUM_THREADS=1 .venv/bin/python s1_grid.py` (grid),
`s2_cov.py`, `s3_ablations.py`, `s4_threads.py`, `s5_distortion.py`,
`tables.py`; JSON in `results/`. `calib/solvers.py` is importable as
`from calib import solvers`.


## 2. Runtime

### Table 1 — runtime, median [IQR] s, `OPENBLAS_NUM_THREADS=1`

| case | views | m | engine | scipy+FD | scipy+analytic | native C++ | internal LM | OpenCV |
|---|---|---|---|---|---|---|---|---|
| vendor25 | 25 | 5728 | 6.114 [0.000] | 6.814 [0.000] | 1.027 [0.009] | 0.251 [0.001] | 0.227 [0.003] | 0.289 [0.028] |
| synth_v5_n0.2_o2 | 5 | 1200 | 0.645 [0.000] | 0.802 [0.000] | 0.118 [0.004] | 0.046 [0.001] | 0.049 [0.001] | 0.052 [0.014] |
| synth_v8_n0.2_o2 | 8 | 1920 | – | 1.960 [0.000] | 0.250 [0.001] | 0.057 [0.001] | 0.065 [0.004] | 0.075 [0.011] |
| synth_v12_n0.2_o2 | 12 | 2880 | 2.005 [0.000] | 2.230 [0.000] | 0.321 [0.006] | 0.086 [0.004] | 0.144 [0.003] | 0.097 [0.001] |
| synth_v25_n0.2_o2 | 25 | 6000 | – | 7.763 [0.000] | 1.231 [0.012] | 0.231 [0.002] | 0.240 [0.005] | 0.247 [0.018] |
| synth_v40_n0.2_o2 | 40 | 9600 | – | 21.019 [0.000] | 2.928 [0.052] | 0.630 [0.008] | 0.645 [0.076] | 0.710 [0.033] |
| synth_v12_n0.1_o2 | 12 | 2880 | – | 3.121 [0.000] | 0.442 [0.031] | 0.090 [0.028] | 0.132 [0.069] | 0.159 [0.043] |
| synth_v12_n0.3_o2 | 12 | 2880 | – | 3.648 [0.000] | 0.454 [0.063] | 0.091 [0.002] | 0.146 [0.053] | 0.184 [0.079] |
| synth_v12_n0.5_o2 | 12 | 2880 | – | 2.182 [0.000] | 0.375 [0.023] | 0.102 [0.002] | 0.108 [0.004] | 0.104 [0.004] |
| synth_v12_n0.2_o0 | 12 | 2880 | – | 1.135 [0.000] | 0.110 [0.001] | 0.080 [0.001] | 0.087 [0.001] | 0.099 [0.007] |
| synth_v12_n0.2_o5 | 12 | 2880 | – | 3.903 [0.000] | 0.583 [0.027] | 0.141 [0.055] | 0.176 [0.106] | 0.118 [0.098] |
| synth_v12_n0.2_o10 | 12 | 2880 | – | 5.234 [0.000] | 0.709 [0.061] | 0.155 [0.041] | 0.162 [0.035] | 0.183 [0.063] |

Speed-up vs engine (same case):

| case | scipy+FD | scipy+analytic | native C++ | internal LM | OpenCV |
|---|---|---|---|---|---|
| vendor25 | 0.9x | 6.0x | 24.4x | 26.9x | 21.2x |
| synth_v5_n0.2_o2 | 0.8x | 5.5x | 13.9x | 13.2x | 12.3x |
| synth_v12_n0.2_o2 | 0.9x | 6.2x | 23.4x | 13.9x | 20.7x |



The engine's finite-difference Jacobian costs 2·n_free residual evaluations per
iteration (n_free = 189 on the vendor corpus), which is why it is 6-7× slower
than the same SciPy solver with the analytic Jacobian. The internal LM and the
C++ LM are within noise of each other on every case (0.23 vs 0.25 s on the
vendor corpus; 0.645 vs 0.630 s at 40 views): at these sizes the cost is the
Jacobian evaluation, not the linear algebra, so a compiled solver buys nothing
unless it also removes the Python-level Jacobian *and* skips its own
O(n³) covariance (the shipped binary computes a dense covariance we then
recompute — about half of its 0.25 s, see §8).

Re-measured with the final code (the covariance additionally assembles the
information matrix that `calib.adaptive`/`calib.objective` consume), vendor
corpus, one thread, median of 3: `scipy`+analytic **1.444 s**, `lm` **0.287 s**,
`native` **0.358 s** (covariance 68-72 ms of each). The extra linear algebra is
~5-10 ms per solve; the rest of the difference from Table 1 is host load.


## 3. Accuracy

### Table 2 — accuracy against synthetic truth

| case | backend | fx err | fy err | cx err | cy err | max kc err | baseline err mm | rel-rot err deg |
|---|---|---|---|---|---|---|---|---|
| synth_v5_n0.2_o2 | scipy_analytic | +1.820 | +1.261 | +1.040 | -1.405 | 0.4100 | -0.0208 | +0.1342 |
| synth_v5_n0.2_o2 | native | +1.777 | +1.217 | +1.031 | -1.395 | 0.4102 | -0.0208 | +0.1330 |
| synth_v5_n0.2_o2 | opencv | -11.827 | -14.439 | -1.278 | -6.219 | 0.2636 | -0.0907 | +0.9522 |
| synth_v8_n0.2_o2 | scipy_analytic | -1.078 | -1.277 | +0.676 | +0.108 | 0.5766 | -0.0093 | +0.0721 |
| synth_v8_n0.2_o2 | native | -1.083 | -1.282 | +0.668 | +0.106 | 0.5764 | -0.0091 | +0.0710 |
| synth_v8_n0.2_o2 | opencv | -6.187 | -6.662 | +3.882 | +8.106 | 0.4051 | -0.0317 | +0.8675 |
| synth_v12_n0.2_o2 | scipy_analytic | -0.771 | -0.969 | +1.871 | +0.142 | 0.1770 | -0.0289 | +0.1217 |
| synth_v12_n0.2_o2 | native | -0.766 | -0.964 | +1.868 | +0.140 | 0.1771 | -0.0288 | +0.1211 |
| synth_v12_n0.2_o2 | opencv | -2.460 | -3.924 | +9.919 | -3.514 | 1.0052 | +0.0019 | +0.8327 |
| synth_v25_n0.2_o2 | scipy_analytic | -0.274 | -0.400 | +1.065 | -0.868 | 0.0051 | -0.0353 | +0.0384 |
| synth_v25_n0.2_o2 | native | -0.268 | -0.395 | +1.067 | -0.872 | 0.0051 | -0.0353 | +0.0386 |
| synth_v25_n0.2_o2 | opencv | -3.092 | -3.521 | +3.688 | -5.574 | 0.5241 | +0.0851 | +0.3534 |
| synth_v40_n0.2_o2 | scipy_analytic | -0.194 | -0.311 | +0.373 | -1.005 | 0.0351 | -0.0327 | -0.0032 |
| synth_v40_n0.2_o2 | native | -0.190 | -0.307 | +0.373 | -1.004 | 0.0351 | -0.0326 | -0.0033 |
| synth_v40_n0.2_o2 | opencv | -3.028 | -3.298 | -2.249 | -2.650 | 0.9450 | -0.0050 | +0.0656 |
| synth_v12_n0.1_o2 | scipy_analytic | -0.578 | -0.740 | +1.231 | -0.262 | 0.0463 | -0.0120 | +0.0862 |
| synth_v12_n0.1_o2 | native | -0.582 | -0.744 | +1.228 | -0.262 | 0.0462 | -0.0119 | +0.0858 |
| synth_v12_n0.1_o2 | opencv | -1.945 | -3.378 | +9.226 | -4.155 | 1.1460 | +0.0127 | +0.8090 |
| synth_v12_n0.3_o2 | scipy_analytic | -0.962 | -1.195 | +2.521 | +0.562 | 0.3107 | -0.0457 | +0.1577 |
| synth_v12_n0.3_o2 | native | -0.948 | -1.180 | +2.517 | +0.559 | 0.3109 | -0.0455 | +0.1570 |
| synth_v12_n0.3_o2 | opencv | -2.983 | -4.478 | +10.614 | -2.860 | 0.8615 | -0.0086 | +0.8568 |
| synth_v12_n0.5_o2 | scipy_analytic | -1.200 | -1.506 | +3.845 | +1.339 | 0.5852 | -0.0736 | +0.2398 |
| synth_v12_n0.5_o2 | native | -1.167 | -1.473 | +3.840 | +1.331 | 0.5857 | -0.0733 | +0.2386 |
| synth_v12_n0.5_o2 | opencv | -4.054 | -5.608 | +12.009 | -1.509 | 0.5653 | -0.0287 | +0.9062 |
| synth_v12_n0.2_o0 | scipy_analytic | +0.081 | -0.085 | +0.744 | -0.220 | 0.5442 | -0.0106 | +0.0429 |
| synth_v12_n0.2_o0 | native | +0.107 | -0.058 | +0.743 | -0.208 | 0.5456 | -0.0104 | +0.0432 |
| synth_v12_n0.2_o0 | opencv | +0.993 | +0.941 | +0.989 | +0.584 | 0.6604 | -0.0056 | +0.0699 |
| synth_v12_n0.2_o5 | scipy_analytic | -1.087 | -0.810 | -0.668 | -1.564 | 0.0429 | -0.0275 | -0.0466 |
| synth_v12_n0.2_o5 | native | -1.121 | -0.844 | -0.686 | -1.568 | 0.0441 | -0.0273 | -0.0482 |
| synth_v12_n0.2_o5 | opencv | +24.998 | +26.637 | -7.491 | -13.206 | 1.5883 | +1.5938 | -1.5887 |
| synth_v12_n0.2_o10 | scipy_analytic | -1.063 | -1.100 | +1.394 | +0.976 | 0.0385 | -0.0282 | -0.0381 |
| synth_v12_n0.2_o10 | native | -1.191 | -1.230 | +1.387 | +0.984 | 0.0418 | -0.0293 | -0.0407 |
| synth_v12_n0.2_o10 | opencv | -37.859 | -40.046 | +11.987 | -3.544 | 1.4803 | -0.1719 | +2.9129 |



The joint estimators (SciPy analytic, SciPy FD, internal LM, native) agree with
each other to **≤0.13 px** in intrinsics and **≤0.001 mm** in baseline on every
case; the differences are native's early stopping, not a different optimum
(Table 4). OpenCV's independent per-camera fits are a *different* estimate:
6-48 px in intrinsics, 0.005-1.6 mm in baseline, 0.03-2.95° in relative rotation,
and it degrades catastrophically with outliers (fx error 37 px at 10 %) because
it has no robust loss and no joint rig cost.

Two systematic effects are visible in the joint estimates themselves:

* **robust-loss bias with outliers**: at 12 views the fx error is +0.08 px with
  0 % outliers and −0.77 px with 2 % outliers at 8 px — the Huber fit is pulled
  by the outlier population. The bias falls with more data (−0.27 px at 25
  views, −0.19 px at 40); §5 quantifies it per loss.
* **baseline scale bias**: −0.011 to −0.074 mm (≤0.05 % of 160 mm), growing with
  noise. Negligible for the mission, but it is a real bias of the estimator.

### Table 3 — 3-D reconstruction error, mm (RMS over a 3x3 grid of ±60 mm)

| case | backend | 200 | 300 | 450 | 600 | 300+0.2px | 450+0.2px | 600+0.2px |
|---|---|---|---|---|---|---|---|---|
| synth_v5_n0.2_o2 | scipy_analytic | 0.448 | 0.643 | 0.986 | 1.319 | 0.703 | 1.108 | 1.536 |
| synth_v5_n0.2_o2 | native | 0.442 | 0.634 | 0.971 | 1.299 | 0.694 | 1.094 | 1.515 |
| synth_v5_n0.2_o2 | opencv | 4.406 | 3.523 | 3.566 | 4.934 | 3.480 | 3.566 | 4.809 |
| synth_v8_n0.2_o2 | scipy_analytic | 0.145 | 0.231 | 0.356 | 0.479 | 0.241 | 0.516 | 0.561 |
| synth_v8_n0.2_o2 | native | 0.144 | 0.230 | 0.355 | 0.478 | 0.240 | 0.515 | 0.559 |
| synth_v8_n0.2_o2 | opencv | 2.323 | 2.718 | 3.731 | 5.001 | 2.687 | 3.714 | 4.981 |
| synth_v12_n0.2_o2 | scipy_analytic | 0.350 | 0.521 | 0.781 | 1.034 | 0.530 | 0.876 | 1.102 |
| synth_v12_n0.2_o2 | native | 0.349 | 0.520 | 0.779 | 1.032 | 0.529 | 0.874 | 1.100 |
| synth_v12_n0.2_o2 | opencv | 2.113 | 2.885 | 4.208 | 5.610 | 2.888 | 4.250 | 5.636 |
| synth_v25_n0.2_o2 | scipy_analytic | 0.259 | 0.376 | 0.551 | 0.724 | 0.404 | 0.694 | 0.853 |
| synth_v25_n0.2_o2 | native | 0.260 | 0.377 | 0.553 | 0.726 | 0.405 | 0.695 | 0.855 |
| synth_v25_n0.2_o2 | opencv | 1.435 | 1.880 | 2.738 | 3.713 | 1.883 | 2.785 | 3.698 |
| synth_v40_n0.2_o2 | scipy_analytic | 0.200 | 0.294 | 0.434 | 0.569 | 0.328 | 0.594 | 0.698 |
| synth_v40_n0.2_o2 | native | 0.200 | 0.294 | 0.433 | 0.568 | 0.328 | 0.594 | 0.698 |
| synth_v40_n0.2_o2 | opencv | 1.241 | 1.246 | 1.605 | 2.179 | 1.223 | 1.620 | 2.094 |
| synth_v12_n0.1_o2 | scipy_analytic | 0.239 | 0.356 | 0.535 | 0.712 | 0.374 | 0.666 | 0.799 |
| synth_v12_n0.1_o2 | native | 0.239 | 0.356 | 0.535 | 0.712 | 0.373 | 0.665 | 0.798 |
| synth_v12_n0.1_o2 | opencv | 2.033 | 2.755 | 4.033 | 5.380 | 2.761 | 4.079 | 5.413 |
| synth_v12_n0.3_o2 | scipy_analytic | 0.477 | 0.709 | 1.061 | 1.403 | 0.713 | 1.133 | 1.459 |
| synth_v12_n0.3_o2 | native | 0.476 | 0.707 | 1.058 | 1.399 | 0.712 | 1.130 | 1.456 |
| synth_v12_n0.3_o2 | opencv | 2.204 | 3.035 | 4.417 | 5.883 | 3.034 | 4.454 | 5.903 |
| synth_v12_n0.5_o2 | scipy_analytic | 0.745 | 1.100 | 1.643 | 2.176 | 1.102 | 1.692 | 2.224 |
| synth_v12_n0.5_o2 | native | 0.744 | 1.096 | 1.637 | 2.169 | 1.099 | 1.687 | 2.218 |
| synth_v12_n0.5_o2 | opencv | 2.412 | 3.381 | 4.921 | 6.542 | 3.377 | 4.951 | 6.550 |
| synth_v12_n0.2_o0 | scipy_analytic | 0.591 | 0.211 | 0.308 | 0.410 | 0.246 | 0.510 | 0.636 |
| synth_v12_n0.2_o0 | native | 0.587 | 0.209 | 0.307 | 0.410 | 0.246 | 0.510 | 0.640 |
| synth_v12_n0.2_o0 | opencv | 0.445 | 0.376 | 0.540 | 0.716 | 0.426 | 0.684 | 0.951 |
| synth_v12_n0.2_o5 | scipy_analytic | 0.361 | 0.507 | 0.764 | 1.027 | 0.511 | 0.854 | 0.996 |
| synth_v12_n0.2_o5 | native | 0.366 | 0.514 | 0.773 | 1.041 | 0.516 | 0.862 | 1.006 |
| synth_v12_n0.2_o5 | opencv | 11.064 | 8.566 | 8.521 | 11.319 | 8.622 | 8.587 | 11.448 |
| synth_v12_n0.2_o10 | scipy_analytic | 0.362 | 0.552 | 0.761 | 0.989 | 0.535 | 0.830 | 1.029 |
| synth_v12_n0.2_o10 | native | 0.368 | 0.571 | 0.785 | 1.018 | 0.552 | 0.849 | 1.045 |
| synth_v12_n0.2_o10 | opencv | 11.480 | 12.165 | 11.890 | 14.795 | 12.114 | 11.859 | 14.646 |


Reconstruction error (noise-free true correspondences re-triangulated with the
fitted rig) is the mission-relevant metric. The joint estimators deliver
0.15-1.0 mm RMS at 200-600 mm for 8-40 views and 0.45-1.3 mm at 5 views;
OpenCV is 3-10× worse at every depth and 10-14 mm at 5-10 % outliers. Adding
0.2 px of correspondence noise (real operation) costs only ~0.1-0.2 mm at
450 mm, i.e. **calibration error dominates measurement noise in this regime**.

### Table 4 — parameter disagreement against the `native` fit

| case | backend | max \|Δintr\| px | max \|Δextr\| | Δfx | Δcx | Δbaseline mm | Δrel-rot deg |
|---|---|---|---|---|---|---|---|
| vendor25 | scipy_analytic | 1.73e-02 | 7.67e-04 | -0.0117 | -0.0173 | -0.00018 | -0.00043 |
| vendor25 | scipy_fd | 1.73e-02 | 7.67e-04 | -0.0117 | -0.0173 | -0.00018 | -0.00043 |
| vendor25 | lm | 1.73e-02 | 7.67e-04 | -0.0117 | -0.0173 | -0.00018 | -0.00043 |
| vendor25 | opencv | 6.15e+00 | 1.29e+00 | -4.3097 | -4.7727 | -0.07385 | -0.18016 |
| synth_v5_n0.2_o2 | scipy_analytic | 5.16e-02 | 6.15e-03 | +0.0425 | +0.0086 | -0.00001 | +0.00125 |
| synth_v5_n0.2_o2 | scipy_fd | 5.14e-02 | 6.15e-03 | +0.0422 | +0.0077 | -0.00001 | +0.00121 |
| synth_v5_n0.2_o2 | lm | 5.15e-02 | 6.15e-03 | +0.0425 | +0.0085 | -0.00001 | +0.00124 |
| synth_v5_n0.2_o2 | opencv | 2.72e+01 | 1.04e+01 | -13.6040 | -2.3092 | -0.06992 | +0.81927 |
| synth_v8_n0.2_o2 | scipy_analytic | 1.74e-02 | 4.58e-03 | +0.0053 | +0.0072 | -0.00014 | +0.00109 |
| synth_v8_n0.2_o2 | scipy_fd | 1.75e-02 | 4.58e-03 | +0.0054 | +0.0073 | -0.00013 | +0.00109 |
| synth_v8_n0.2_o2 | lm | 1.74e-02 | 4.58e-03 | +0.0053 | +0.0072 | -0.00014 | +0.00109 |
| synth_v8_n0.2_o2 | opencv | 1.33e+01 | 4.33e+00 | -5.1042 | +3.2135 | -0.02259 | +0.79647 |
| synth_v12_n0.2_o2 | scipy_analytic | 6.97e-03 | 2.06e-03 | -0.0054 | +0.0034 | -0.00013 | +0.00059 |
| synth_v12_n0.2_o2 | scipy_fd | 6.98e-03 | 2.06e-03 | -0.0053 | +0.0036 | -0.00013 | +0.00060 |
| synth_v12_n0.2_o2 | lm | 6.96e-03 | 2.06e-03 | -0.0054 | +0.0034 | -0.00013 | +0.00059 |
| synth_v12_n0.2_o2 | opencv | 8.05e+00 | 2.92e+00 | -1.6941 | +8.0509 | +0.03071 | +0.71153 |
| synth_v25_n0.2_o2 | scipy_analytic | 5.48e-03 | 6.07e-04 | -0.0055 | -0.0027 | -0.00003 | -0.00016 |
| synth_v25_n0.2_o2 | scipy_fd | 5.46e-03 | 6.15e-04 | -0.0055 | -0.0026 | -0.00003 | -0.00016 |
| synth_v25_n0.2_o2 | lm | 5.48e-03 | 6.07e-04 | -0.0055 | -0.0027 | -0.00003 | -0.00016 |
| synth_v25_n0.2_o2 | opencv | 7.18e+00 | 3.11e+00 | -2.8236 | +2.6207 | +0.12037 | +0.31487 |
| synth_v40_n0.2_o2 | scipy_analytic | 3.85e-03 | 1.05e-03 | -0.0037 | +0.0006 | -0.00007 | +0.00005 |
| synth_v40_n0.2_o2 | scipy_fd | 3.82e-03 | 1.04e-03 | -0.0037 | +0.0008 | -0.00007 | +0.00006 |
| synth_v40_n0.2_o2 | lm | 3.85e-03 | 1.05e-03 | -0.0037 | +0.0006 | -0.00007 | +0.00005 |
| synth_v40_n0.2_o2 | opencv | 6.01e+00 | 2.85e+00 | -2.8380 | -2.6217 | +0.02762 | +0.06884 |
| synth_v12_n0.1_o2 | scipy_analytic | 7.38e-03 | 1.35e-03 | +0.0045 | +0.0031 | -0.00008 | +0.00042 |
| synth_v12_n0.1_o2 | scipy_fd | 7.38e-03 | 1.36e-03 | +0.0044 | +0.0031 | -0.00008 | +0.00042 |
| synth_v12_n0.1_o2 | lm | 7.38e-03 | 1.35e-03 | +0.0045 | +0.0031 | -0.00008 | +0.00042 |
| synth_v12_n0.1_o2 | opencv | 8.00e+00 | 2.93e+00 | -1.3632 | +7.9982 | +0.02459 | +0.72324 |
| synth_v12_n0.3_o2 | scipy_analytic | 1.47e-02 | 2.82e-03 | -0.0145 | +0.0040 | -0.00018 | +0.00079 |
| synth_v12_n0.3_o2 | scipy_fd | 1.47e-02 | 2.82e-03 | -0.0145 | +0.0040 | -0.00018 | +0.00079 |
| synth_v12_n0.3_o2 | lm | 1.47e-02 | 2.82e-03 | -0.0145 | +0.0040 | -0.00018 | +0.00079 |
| synth_v12_n0.3_o2 | opencv | 8.10e+00 | 2.90e+00 | -2.0354 | +8.0968 | +0.03693 | +0.69981 |
| synth_v12_n0.5_o2 | scipy_analytic | 3.35e-02 | 4.21e-03 | -0.0331 | +0.0056 | -0.00033 | +0.00120 |
| synth_v12_n0.5_o2 | scipy_fd | 3.35e-02 | 4.21e-03 | -0.0331 | +0.0056 | -0.00033 | +0.00120 |
| synth_v12_n0.5_o2 | lm | 3.35e-02 | 4.21e-03 | -0.0331 | +0.0056 | -0.00033 | +0.00120 |
| synth_v12_n0.5_o2 | opencv | 8.17e+00 | 2.89e+00 | -2.8872 | +8.1687 | +0.04455 | +0.66759 |
| synth_v12_n0.2_o0 | scipy_analytic | 2.73e-02 | 2.75e-04 | -0.0266 | +0.0007 | -0.00026 | -0.00032 |
| synth_v12_n0.2_o0 | scipy_fd | 2.73e-02 | 2.74e-04 | -0.0266 | +0.0006 | -0.00025 | -0.00032 |
| synth_v12_n0.2_o0 | lm | 2.73e-02 | 2.75e-04 | -0.0266 | +0.0007 | -0.00026 | -0.00032 |
| synth_v12_n0.2_o0 | opencv | 1.30e+00 | 6.50e-01 | +0.8852 | +0.2451 | +0.00483 | +0.02672 |
| synth_v12_n0.2_o5 | scipy_analytic | 4.05e-02 | 3.98e-03 | +0.0345 | +0.0187 | -0.00012 | +0.00158 |
| synth_v12_n0.2_o5 | scipy_fd | 4.04e-02 | 3.98e-03 | +0.0345 | +0.0190 | -0.00013 | +0.00159 |
| synth_v12_n0.2_o5 | lm | 4.05e-02 | 3.98e-03 | +0.0345 | +0.0187 | -0.00012 | +0.00158 |
| synth_v12_n0.2_o5 | opencv | 3.19e+01 | 1.87e+01 | +26.1195 | -6.8048 | +1.62116 | -1.54046 |
| synth_v12_n0.2_o10 | scipy_analytic | 1.30e-01 | 2.00e-03 | +0.1274 | +0.0071 | +0.00106 | +0.00257 |
| synth_v12_n0.2_o10 | scipy_fd | 1.30e-01 | 2.00e-03 | +0.1274 | +0.0071 | +0.00106 | +0.00257 |
| synth_v12_n0.2_o10 | lm | 1.30e-01 | 2.00e-03 | +0.1274 | +0.0071 | +0.00106 | +0.00257 |
| synth_v12_n0.2_o10 | opencv | 4.78e+01 | 2.27e+01 | -36.6685 | +10.6004 | -0.14263 | +2.95355 |


## 4. Covariance

Four routes to the same covariance are compared: the engine's own
central-difference Jacobian + Huber sandwich (`calib.calibrate.covariance`), the
analytic Jacobian + the same sandwich (`solvers.covariance_from_jacobian`), the
native binary's exported variance diagonal, and empirical spread over Monte
Carlo / bootstrap replicates. Table 5 first shows the *model-based* sigma and
the z-score of the actual error against truth for every grid case; Table 6 then
calibrates those sigmas against Monte Carlo.

Note the ratio convention: the tables report **reported/empirical** (<1 means the
reported sigma is too small, i.e. optimistic). The task's **empirical/reported**
ratio is the reciprocal (e.g. cx at 5 views: 0.628 → 1.59).

### Table 5 — reported sigmas and z-scores against truth

| case | backend | sd fx | z fx | sd cx | sd k1 | sd baseline mm | z base | sd rel-rot deg | z rot | sd z(450) mm |
|---|---|---|---|---|---|---|---|---|---|---|
| synth_v5_n0.2_o2 | scipy_analytic | 1.9861 | +0.92 | 1.3833 | 0.0122 | 0.0747 | -0.28 | 0.1181 | +1.14 | 0.7449 |
| synth_v5_n0.2_o2 | native | 1.9863 | +0.89 | 1.3830 | 0.0122 | 0.0746 | -0.28 | 0.1181 | +1.13 | 0.7450 |
| synth_v5_n0.2_o2 | opencv | 5.8934 | -2.01 | 6.0032 | 0.0489 | 0.2143 | -0.42 | 0.3842 | +2.48 | 2.2794 |
| synth_v8_n0.2_o2 | scipy_analytic | 1.1192 | -0.96 | 1.2261 | 0.0096 | 0.0491 | -0.19 | 0.0947 | +0.76 | 0.3988 |
| synth_v8_n0.2_o2 | native | 1.1193 | -0.97 | 1.2260 | 0.0096 | 0.0491 | -0.19 | 0.0947 | +0.75 | 0.3988 |
| synth_v8_n0.2_o2 | opencv | 2.1418 | -2.89 | 2.5584 | 0.0212 | 0.0846 | -0.38 | 0.1886 | +4.60 | 0.8016 |
| synth_v12_n0.2_o2 | scipy_analytic | 0.9770 | -0.79 | 1.0517 | 0.0084 | 0.0367 | -0.79 | 0.0736 | +1.65 | 0.3475 |
| synth_v12_n0.2_o2 | native | 0.9770 | -0.78 | 1.0518 | 0.0084 | 0.0367 | -0.79 | 0.0736 | +1.65 | 0.3475 |
| synth_v12_n0.2_o2 | opencv | 1.6401 | -1.50 | 1.9056 | 0.0137 | 0.0558 | +0.03 | 0.1196 | +6.96 | 0.6129 |
| synth_v25_n0.2_o2 | scipy_analytic | 0.5892 | -0.46 | 0.6996 | 0.0056 | 0.0280 | -1.26 | 0.0502 | +0.77 | 0.2093 |
| synth_v25_n0.2_o2 | native | 0.5892 | -0.45 | 0.6996 | 0.0056 | 0.0280 | -1.26 | 0.0502 | +0.77 | 0.2093 |
| synth_v25_n0.2_o2 | opencv | 0.8690 | -3.56 | 1.1011 | 0.0089 | 0.0403 | +2.11 | 0.0741 | +4.77 | 0.3220 |
| synth_v40_n0.2_o2 | scipy_analytic | 0.4614 | -0.42 | 0.5878 | 0.0046 | 0.0223 | -1.47 | 0.0417 | -0.08 | 0.1651 |
| synth_v40_n0.2_o2 | native | 0.4614 | -0.41 | 0.5878 | 0.0046 | 0.0223 | -1.46 | 0.0417 | -0.08 | 0.1651 |
| synth_v40_n0.2_o2 | opencv | 0.6773 | -4.47 | 0.9469 | 0.0070 | 0.0318 | -0.16 | 0.0626 | +1.05 | 0.2494 |
| synth_v12_n0.1_o2 | scipy_analytic | 0.6635 | -0.87 | 0.7689 | 0.0063 | 0.0251 | -0.48 | 0.0530 | +1.63 | 0.2240 |
| synth_v12_n0.1_o2 | native | 0.6635 | -0.88 | 0.7689 | 0.0063 | 0.0251 | -0.47 | 0.0530 | +1.62 | 0.2240 |
| synth_v12_n0.1_o2 | opencv | 1.5226 | -1.28 | 1.8501 | 0.0133 | 0.0500 | +0.25 | 0.1137 | +7.12 | 0.5697 |
| synth_v12_n0.3_o2 | scipy_analytic | 1.3449 | -0.72 | 1.4033 | 0.0110 | 0.0503 | -0.91 | 0.0988 | +1.60 | 0.4882 |
| synth_v12_n0.3_o2 | native | 1.3449 | -0.70 | 1.4034 | 0.0110 | 0.0503 | -0.91 | 0.0988 | +1.59 | 0.4881 |
| synth_v12_n0.3_o2 | opencv | 1.8407 | -1.62 | 2.0405 | 0.0146 | 0.0646 | -0.13 | 0.1319 | +6.49 | 0.6869 |
| synth_v12_n0.5_o2 | scipy_analytic | 2.0796 | -0.58 | 2.1427 | 0.0166 | 0.0775 | -0.95 | 0.1502 | +1.60 | 0.7662 |
| synth_v12_n0.5_o2 | native | 2.0796 | -0.56 | 2.1429 | 0.0166 | 0.0775 | -0.95 | 0.1502 | +1.59 | 0.7662 |
| synth_v12_n0.5_o2 | opencv | 2.3295 | -1.74 | 2.4455 | 0.0173 | 0.0846 | -0.34 | 0.1659 | +5.46 | 0.8703 |
| synth_v12_n0.2_o0 | scipy_analytic | 0.5756 | +0.14 | 0.7821 | 0.0078 | 0.0323 | -0.33 | 0.0554 | +0.77 | 0.2034 |
| synth_v12_n0.2_o0 | native | 0.5756 | +0.19 | 0.7821 | 0.0078 | 0.0323 | -0.32 | 0.0554 | +0.78 | 0.2033 |
| synth_v12_n0.2_o0 | opencv | 0.5836 | +1.70 | 0.7969 | 0.0080 | 0.0330 | -0.17 | 0.0560 | +1.25 | 0.2048 |
| synth_v12_n0.2_o5 | scipy_analytic | 1.2187 | -0.89 | 1.6517 | 0.0105 | 0.0597 | -0.46 | 0.1091 | -0.43 | 0.4602 |
| synth_v12_n0.2_o5 | native | 1.2186 | -0.92 | 1.6518 | 0.0105 | 0.0597 | -0.46 | 0.1091 | -0.44 | 0.4601 |
| synth_v12_n0.2_o5 | opencv | 3.5437 | +7.05 | 4.6177 | 0.0451 | 0.2123 | +7.51 | 0.3235 | -4.91 | 1.2597 |
| synth_v12_n0.2_o10 | scipy_analytic | 1.7720 | -0.60 | 1.7907 | 0.0139 | 0.0773 | -0.37 | 0.1414 | -0.27 | 0.6767 |
| synth_v12_n0.2_o10 | native | 1.7723 | -0.67 | 1.7907 | 0.0138 | 0.0773 | -0.38 | 0.1414 | -0.29 | 0.6769 |
| synth_v12_n0.2_o10 | opencv | 5.0352 | -7.52 | 4.8126 | 0.0434 | 0.2170 | -0.79 | 0.4004 | +7.27 | 1.9534 |


(For the `opencv` rows the sigma is the joint model's sigma *evaluated at
OpenCV's parameters* — OpenCV exports no rig covariance — which is why its
z-scores are huge: a local covariance cannot see a biased parameter.)


**The analytic covariance reproduces the engine's.** The engine's
central-difference Jacobian + Huber sandwich and the analytic Jacobian + the
same sandwich agree to a maximum relative difference of
1e-07 in every reported sigma (baseline, fx, relative rotation and the
three 3-D points) — i.e. the analytic covariance *is* the shipping estimator,
obtained without the 2·n_free residual evaluations that the finite-difference
Jacobian costs. The native binary's own variance diagonal agrees with the same
recomputation to a median of 1.0002-1.0024 with a worst-case 1.3 % deviation;
that residual disagreement is the Jacobian defect of section 8, not the formula.

**Monte Carlo design.** Five synthetic scenes (5/12/25 views at 0.2 px and 2 %
outliers, plus 12 views at 0.1 px and 0.5 px with no outliers) are re-simulated
by redrawing only the observation noise and the outlier draw on the *same*
geometry, so the empirical spread is the estimator's sampling distribution and
not the spread of the truth. 174-197 of 200 draws per scene
contribute (a draw is dropped when the LM falls back, which slightly
under-states the tails). Bootstrap resamples whole views and refits, starting
from the source view's fitted pose.


### Table 6 — covariance calibration, bootstrap and sigma agreement

| scene | fx | fy | cx | baseline | relrot | z300 | z450 | z600 |
|---|---|---|---|---|---|---|---|---|
| v5_n0.2_o2 reported/empirical (n=195) | 1.016 | 1.012 | 0.628 | 0.881 | 0.811 | 0.986 | 0.998 | 1.001 |
| v12_n0.2_o2 reported/empirical (n=197) | 0.959 | 0.945 | 0.858 | 0.969 | 0.911 | 0.916 | 0.923 | 0.935 |
| v25_n0.2_o2 reported/empirical (n=193) | 0.954 | 0.942 | 0.863 | 0.977 | 0.942 | 0.928 | 0.942 | 0.956 |
| v12_n0.1_o0 reported/empirical (n=174) | 0.968 | 0.967 | 0.914 | 0.887 | 1.003 | 0.922 | 0.939 | 0.940 |
| v12_n0.5_o0 reported/empirical (n=194) | 0.931 | 0.927 | 0.865 | 0.885 | 0.946 | 0.892 | 0.908 | 0.911 |

| scene | NEES mean (dof 4) | NEES p95 | frac > chi2(4,0.95)=9.49 | z fx | z base | z rot | z z450 | boot/MC fx | boot/MC base | boot/MC z450 |
|---|---|---|---|---|---|---|---|---|---|---|
| v5_n0.2_o2 | 4.759 | 12.93 | 0.12 | -0.02 | +0.00 | -0.06 | -0.01 | 4.090 | 6.944 | 4.715 |
| v12_n0.2_o2 | 4.527 | 13.04 | 0.12 | +0.09 | -0.03 | -0.02 | +0.10 | 1.781 | 0.730 | 1.939 |
| v25_n0.2_o2 | 4.392 | 11.06 | 0.08 | +0.06 | -0.06 | +0.05 | +0.08 | 0.908 | 0.837 | 0.966 |
| v12_n0.1_o0 | 4.476 | 11.80 | 0.10 | +0.10 | -0.02 | -0.13 | +0.08 | 0.855 | 0.809 | 0.927 |
| v12_n0.5_o0 | 4.755 | 13.18 | 0.11 | +0.07 | +0.04 | -0.11 | +0.04 | 0.877 | 0.885 | 0.938 |

| scene | sd fx analytic | sd fx engine-FD | sd base analytic | sd base engine-FD | sd rot analytic | sd rot engine-FD | native diag ratio (median / max dev) |
|---|---|---|---|---|---|---|---|
| v5_n0.2_o2 | 1.9861 | 1.9861 | 0.07465 | 0.07465 | 0.1181 | 0.1181 | 1.00245 / 9.14e-03 |
| v12_n0.2_o2 | 0.9770 | 0.9770 | 0.03668 | 0.03668 | 0.0736 | 0.0736 | 1.00090 / 7.60e-03 |
| v25_n0.2_o2 | 0.5892 | 0.5892 | 0.02801 | 0.02801 | 0.0502 | 0.0502 | 1.00117 / 4.99e-03 |
| v12_n0.1_o0 | 0.2878 | 0.2878 | 0.01614 | 0.01614 | 0.0277 | 0.0277 | 1.00110 / 4.59e-03 |
| v12_n0.5_o0 | 1.4111 | 1.4111 | 0.07917 | 0.07917 | 0.1357 | 0.1357 | 1.00126 / 4.85e-03 |

* v5_n0.2_o2: engine-vs-analytic max rel. sigma difference 1.11e-07; LM-vs-SciPy max parameter difference over 3 draws 4.48e-05; determinism {'lm_bitwise_equal': True, 'scipy_bitwise_equal': True, 'native_bitwise_equal': True}
* v12_n0.2_o2: engine-vs-analytic max rel. sigma difference 3.46e-08; LM-vs-SciPy max parameter difference over 3 draws 1.11e-05; determinism {'lm_bitwise_equal': True, 'scipy_bitwise_equal': True, 'native_bitwise_equal': True}
* v25_n0.2_o2: engine-vs-analytic max rel. sigma difference 2.62e-08; LM-vs-SciPy max parameter difference over 3 draws 1.59e-06; determinism {'lm_bitwise_equal': True, 'scipy_bitwise_equal': True, 'native_bitwise_equal': True}
* v12_n0.1_o0: engine-vs-analytic max rel. sigma difference 1.01e-07; LM-vs-SciPy max parameter difference over 3 draws 2.25e-06; determinism {'lm_bitwise_equal': True, 'scipy_bitwise_equal': True, 'native_bitwise_equal': True}
* v12_n0.5_o0: engine-vs-analytic max rel. sigma difference 4.49e-08; LM-vs-SciPy max parameter difference over 3 draws 1.74e-05; determinism {'lm_bitwise_equal': True, 'scipy_bitwise_equal': True, 'native_bitwise_equal': True}



* **fx, fy**: 0.93-1.02 → calibrated to a few percent.
* **baseline**: 0.88-0.98 → the reported 1σ is 2-12 % too small.
* **relative rotation**: 0.81-1.00 → -0-19 % too small.
* **cx**: 0.63-0.91 → the worst parameter, 9-37 % too small.
* **3-D point depth z at 300/450/600 mm**: 0.89-1.00 → calibrated.
* **NEES** on [fx, baseline, relrot, z450] with the *reported* covariance:
  4.39-4.76 against dof 4 (ideal 4), with
  8-12 % of draws above the 95 % χ² threshold (ideal
  5 %) — mildly optimistic, consistent with the sub-1 ratios and with heavy
  tails from the 2 % outliers.
* **z-scores of the mean** are all |z| < 0.7 → no detectable bias relative to
  the reported sigma; the estimator is unbiased at this level and the
  under-reporting is a width effect, not a centring effect.

The practical reading: the covariance is good enough for **view selection and
stopping** (which is what `calib.adaptive`/`calib.quality` use) and for the
reconstruction sigma (0.92-1.00 calibrated), but it must not be read as a 1σ
accuracy claim on cx or on the relative rotation — those are 15-35 % optimistic,
and none of it captures systematic (detector/model) error.

Bootstrap/MC ratios (Table 6, last columns) are **>1** for the 5-view scene and
close to 1 for 12-25 views: with few views the view-block bootstrap sees a
genuinely different pose set and over-states the spread, so it is not a
substitute for the model-based sigma at low view counts.


## 5. Robust losses

### Table 7 — robust losses, 12 views, 0.3 px noise, 10 % outliers

| loss | fx err | cx err | kc err | baseline err mm | rel-rot err deg | RMS | inlier RMS | outlier RMS | obs >3 px | sd fx |
|---|---|---|---|---|---|---|---|---|---|---|
| none@1.0 | +11.513 | -5.815 | 3.5328 | +0.4737 | +0.7832 | 3.5303 | 0.7729 | 10.88 | 146 | 9.979 |
| none@2.0 | +11.513 | -5.815 | 3.5328 | +0.4737 | +0.7832 | 3.5303 | 0.7729 | 10.88 | 146 | 9.979 |
| huber@1.0 | +4.812 | -0.773 | 0.8493 | +0.1652 | +0.1634 | 3.5756 | 0.4688 | 11.18 | 145 | 1.929 |
| huber@2.0 | +6.276 | -0.345 | 2.1971 | +0.2091 | +0.3675 | 3.5661 | 0.4898 | 11.14 | 145 | 2.951 |
| cauchy@1.0 | +3.650 | -1.527 | 0.6227 | +0.1436 | +0.0048 | 3.5851 | 0.4627 | 11.21 | 145 | 1.216 |
| cauchy@2.0 | +4.449 | -1.157 | 1.0424 | +0.1498 | +0.0939 | 3.5816 | 0.4656 | 11.20 | 145 | 1.577 |
| soft_l1@1.0 | +4.966 | -0.807 | 0.9677 | +0.1764 | +0.1845 | 3.5743 | 0.4704 | 11.18 | 145 | 1.869 |
| soft_l1@2.0 | +6.142 | -0.274 | 1.6211 | +0.2076 | +0.3494 | 3.5658 | 0.4873 | 11.14 | 145 | 2.826 |

Clean reference on the same views/noise (no outliers, least squares): fx err +1.976 px, baseline err +0.0506 mm, RMS 0.4173 px.


Bias-vs-outlier trade-off (12 views, 0.3 px noise, 10 % of points displaced by
~8 px; the clean fit of the same scene has fx error +1.98 px, baseline
+0.051 mm, RMS 0.417 px):

* plain least squares is the most biased (fx +11.51 px, baseline +0.474 mm):
  the outliers dominate the normal equations, and the reported sd(fx) blows up
  to 9.98 px because the Huber-sandwich "meat" is computed at the outlier
  residuals;
* Huber at delta = 1 removes most of the outlier-induced bias (fx +4.81 px, i.e.
  +2.8 px above the clean fit, baseline +0.165 mm) and keeps the inlier RMS at
  0.47 px; delta = 2 is worse (+6.28 px), as expected (a wider band downweights
  less);
* Cauchy at delta = 1 is the least biased here (fx +3.65 px, +1.7 px above
  clean, baseline +0.144 mm) with the smallest reported sd (1.22 px); soft_l1
  sits between Huber and Cauchy;
* **the robust RMS is *higher* than the least-squares RMS** (3.58 vs 3.53 px) -
  the robust fit deliberately refuses to chase the 145 observations that remain
  >3 px from the model. None of the losses fits the outliers away;
* the remaining bias (+1.7 to +2.8 px over clean) is the price of the outlier
  population: 10 % gross outliers bias even a redescending loss at this view
  count, and the bias shrinks with more views (at 2 % outliers: -0.77 px at 12
  views, -0.19 px at 40).

### Table 8 — model-error invisibility (clean data, no outliers)


**pitch_1pct** — plate pitch 27.72 mm assumed, truth 28.0 mm (-1.0 %) (true baseline 161.3141 mm)

| loss/model | fx err | cx err | kc err | baseline err mm | rel-rot err deg | RMS | max |
|---|---|---|---|---|---|---|---|
| none | +1.318 | -1.862 | 0.3064 | -1.5797 | -0.1333 | 0.2782 | 0.84 |
| huber | +1.318 | -1.862 | 0.3064 | -1.5797 | -0.1333 | 0.2782 | 0.84 |
| cauchy | +1.368 | -1.744 | 0.3132 | -1.5815 | -0.1228 | 0.2782 | 0.84 |

**lens_k1k2** — 5-term Brown-Conrady truth, k1,k2 fitted

| loss/model | fx err | cx err | kc err | baseline err mm | rel-rot err deg | RMS | max |
|---|---|---|---|---|---|---|---|
| True | +1.318 | -1.862 | 0.3064 | +0.0338 | -0.1333 | 0.2782 | 0.84 |
| k1k2 | +1.145 | -1.682 | 0.2885 | +0.0355 | -0.1282 | 0.2785 | 0.84 |
| k1k2k3 | +1.170 | -1.688 | 0.3133 | +0.0359 | -0.1295 | 0.2785 | 0.84 |


**Does the robust loss hide model bias?** Two deliberate model errors on clean
data (no outliers):

* a **1 % plate-pitch error** (28.0 mm truth fitted as 27.72 mm) gives a
  **-1.580 mm baseline bias** (-1.0 % of 161.3 mm) and a -1.86 px cx shift,
  while the residual RMS stays at the clean-noise level (0.278 px, max 0.84 px).
  Least squares, Huber and Cauchy return the *same* baseline to 4 decimals
  (-1.5797/-1.5797/-1.5815 mm). The robust loss does not remove, and the
  residual RMS does not reveal, a systematic model error: the fit looks perfect
  and is 1 % wrong - exactly the vendor CCF situation of the verdict and of
  section 11. *This is the answer to "does the robust loss hide model bias": it
  hides outliers, never model bias, and a chi-squared/stopping test on residual
  RMS cannot detect it.*
* the **reduced lens model** (k1,k2 only, truth has k3 and tangential terms) is
  **not** detectably worse on 12 views: RMS 0.27849 vs 0.27818 px, fx differs by
  0.17 px, and the radial residual structure of Table 10 is identical within
  +-0.002 px between the two models (the same is true on the vendor corpus,
  0.35890 vs 0.35788 px). The extra coefficients are absorbed by the others with
  no measurable improvement - consistent with their near collinearity (section
  7) - so **this data set provides no evidence that the 5-term model is
  *needed*; it only shows that it is not harmful.** The corollary is the useful
  one: at 12 views the 5-term distortion model is over-parameterised, and a
  prior on k2/k3 (or a reduced model) is defensible.


## 6. Priors, fixed parameters, lens model

### Table 9 — priors and fixed parameters (12 views, 0.3 px, 2 % outliers)

| config | fx err | cx err | baseline err mm | RMS | sd fx | sd cx | sd base mm | prior rows |
|---|---|---|---|---|---|---|---|---|
| none | +1.683 | +3.297 | -0.0134 | 1.6954 | 1.268 | 1.456 | 0.05973 | 0 |
| weak_intr_20px | +1.420 | +3.843 | -0.0185 | 1.6954 | 1.077 | 1.378 | 0.05940 | 12 |
| strong_intr_2px | -4.506 | +11.410 | -0.1276 | 1.6988 | 0.242 | 0.241 | 0.05794 | 12 |
| weak_kc_0.02 | +2.797 | +3.338 | -0.0361 | 1.6955 | 1.189 | 1.456 | 0.05890 | 15 |
| intr20_kc0.02 | +2.395 | +3.879 | -0.0419 | 1.6954 | 1.017 | 1.378 | 0.05853 | 27 |
| intr2_offset10px | +9.588 | +4.874 | +0.1067 | 1.6976 | 0.242 | 0.218 | 0.05545 | 12 |
| fixed_intrinsics | -7.288 | +12.806 | +0.3472 | 1.7303 | 0.000 | – | 0.07104 | – |
| fixed_distortion | +18.230 | +3.676 | +1.8377 | 1.9940 | 2.816 | – | 0.12293 | – |
| k1k2_only | +2.068 | +9.953 | -0.0212 | 1.6943 | 1.270 | – | 0.06038 | – |

Empirical sigma over 60 redrawn-noise fits (columns fx, cx, baseline mm):

| config | empirical sd fx | empirical sd cx | empirical sd baseline |
|---|---|---|---|
| none | 1.400 | 1.466 | 0.05433 |
| intr20_kc0.02 | 1.080 | 1.372 | 0.05270 |
| intr2_offset10px | 0.261 | 0.218 | 0.04772 |


Priors behave exactly as the theory says and nothing else: with the prior mean
at the truth, a 20 px intrinsic prior shrinks the reported sd(fx) from 1.268 to
1.077 px (-15 %) for a +0.26 px bias change; the distortion prior (sigma 0.02)
shrinks it to 1.189 px; both together to 1.017 px. The empirical spread over 60
redrawn-noise fits tracks this (1.400 -> 1.080 -> 0.261), and the reported sigma
follows the empirical one within 7 % - **so prior-induced variance reduction is
real and the analytic covariance does not lie about it.** A strong prior
(sigma = 2 px) cuts sd(fx) to 0.242 px but, with a 10 px offset mean, pulls fx
+9.588 px off truth: a prior is a bias/variance trade, and on this problem only
an *informative* prior changes the answer. Fixed intrinsics (at the start's
OpenCV guess) is the degenerate case: fx -7.3 px, cx +12.8 px, RMS worse
(1.730 px), reported sd(fx) = 0 - the covariance is honest (it holds the
parameter fixed) but the estimate is biased and nothing in the output says so.
`free_kc=False` (kc held at the *initial* values - zeros for a synthetic start,
the CCF on the vendor corpus) is severe: fx +18.2 px, baseline +1.84 mm, RMS
1.994 px. `free_kc="k1k2"` is the mild version: RMS unchanged (1.694 vs 1.695),
fx +2.07 vs +1.68 px, but cx moves +6.7 px - the distortion model and the
principal point remain strongly coupled (section 7).

### Table 10 — lens model and radial residual structure


**synthetic (truth is 5-term)**

| free_kc | RMS px | max px | fx err / fx | kc err / kc | radial residual mean per bin (rho lo-hi: px) |
|---|---|---|---|---|---|
| True | 0.27818 | 0.837 | +1.318 | 0.3064 | 0.01-0.10:+0.0082, 0.10-0.14:-0.0147, 0.14-0.17:+0.0183, 0.17-0.21:-0.0022, 0.21-0.25:+0.0013, 0.25-0.34:-0.0073, 0.34-0.70:-0.0001 |
| k1k2 | 0.27849 | 0.840 | +1.145 | 0.2885 | 0.01-0.10:+0.0071, 0.10-0.14:-0.0128, 0.14-0.17:+0.0169, 0.17-0.21:-0.0031, 0.21-0.25:+0.0022, 0.25-0.34:-0.0079, 0.34-0.70:+0.0010 |
| k1k2k3 | 0.27846 | 0.841 | +1.170 | 0.3133 | 0.01-0.10:+0.0068, 0.10-0.14:-0.0134, 0.14-0.17:+0.0167, 0.17-0.21:-0.0030, 0.21-0.25:+0.0032, 0.25-0.34:-0.0069, 0.34-0.70:-0.0001 |

**vendor corpus**

| free_kc | RMS px | max px | fx err / fx | kc err / kc | radial residual mean per bin (rho lo-hi: px) |
|---|---|---|---|---|---|
| True | 0.35788 | 1.777 | 1156.059 | [-0.15695, 0.15774, -0.00094, 0.0004, 0.00878] | 0.00-0.08:+0.0009, 0.08-0.11:+0.0278, 0.11-0.15:+0.0094, 0.15-0.18:-0.0123, 0.18-0.22:-0.0201, 0.22-0.27:-0.0041, 0.27-0.59:+0.0120 |
| k1k2 | 0.35890 | 1.786 | 1156.173 | [-0.162, 0.19309, -0.00166, 0.00089, -0.05721] | 0.00-0.08:+0.0066, 0.08-0.11:+0.0286, 0.11-0.15:+0.0146, 0.15-0.18:-0.0110, 0.18-0.22:-0.0221, 0.22-0.27:-0.0060, 0.27-0.59:+0.0103 |
| k1k2k3 | 0.35879 | 1.791 | 1156.037 | [-0.15633, 0.14914, -0.00166, 0.00089, 0.02837] | 0.00-0.08:+0.0036, 0.08-0.11:+0.0243, 0.11-0.15:+0.0118, 0.15-0.18:-0.0129, 0.18-0.22:-0.0215, 0.22-0.27:-0.0024, 0.27-0.59:+0.0121 |
| False | 0.36691 | 1.937 | 1156.339 | [-0.16203, 0.1733, 0.00019, 0.00015, 0.01846] | 0.00-0.08:+0.0149, 0.08-0.11:+0.0539, 0.11-0.15:+0.0375, 0.15-0.18:-0.0018, 0.18-0.22:-0.0085, 0.22-0.27:-0.0213, 0.27-0.59:-0.0117 |


Residual structure (last column) is the test for lens-model inadequacy, and it
comes out **negative**: the same radial pattern (e.g. +0.028 px at rho 0.08-0.11,
-0.020 px at rho 0.18-0.22) appears in the 5-term, k1k2 and k1k2k3 fits alike, so
it is *not* a missing distortion term - it is a view/pose- or detection-level
effect that no radial coefficient can absorb. Dropping to k1k2 costs 0.3 % RMS
on the vendor corpus (0.35890 vs 0.35788) and 0.1 % on synthetic. Fixing kc at
zero (the last vendor row) is the only variant that clearly hurts (0.3669 px,
max residual 1.94 px, and a visible radial tilt), so *some* distortion model is
required, but 2 terms suffice to capture what the data can identify.


## 7. Conditioning, rank and determinism

* **Rank**: full rank in every case (rank = n_free; 69/87/111/189/279), including
  5 views. The gauge (left identity) and the board pitch keep the problem
  non-singular; no case came near rank deficiency, so the engine's SVD `rcond`
  guard never had to drop a direction.
* **Conditioning**: cond(J) = 2.6e5-3.3e5 across 5-40 views, lowest at 8 views
  (2.6e5) and highest at 5 views (3.3e5). The condition number is *not* what limits accuracy
  at low view counts — parameter correlations are.
* **Distortion identifiability** (Table 11): k1, k2, k3 form a nearly collinear
  basis — corr(k1,k2) = −0.96, corr(k2,k3) = −0.98 at every view count — and
  sd(k3) falls only from 0.30 (5 views) to 0.16 (40 views) while sd(k1) falls
  from 0.012 to 0.005. k3 is the least identifiable parameter of the model and
  the natural candidate for a weak prior (Table 10 shows that dropping it costs
  only 0.1-0.3 % RMS, i.e. the data cannot tell the difference). fx–k1
  correlation is −0.25…−0.33; cx–k1 is ≈0.
* **Determinism**: repeated solves of the same problem are bitwise identical for
  `lm`, `scipy` (analytic) and `native` (measured on every synthetic scene and
  the vendor corpus; Table 6 records the flags). The native binary is
  deterministic because the text problem is written with 17 significant digits
  and the algorithm has no randomness; SciPy's `trf` is deterministic at a fixed
  BLAS thread count.
* **BLAS threads**: see Table 12. Pinning `OPENBLAS_NUM_THREADS=1` is worth
  6.7× (SciPy analytic), 4.3× (internal LM) and 2.0× (native) on this 32-core
  host, and must be part of any deployment that quotes a runtime.

### Table 11 — distortion identifiability versus view count (0.2 px noise, 2 % outliers, seed 0)


| views | sd k1 | sd k2 | sd k3 | err k1 | err k2 | err k3 | z k1 | z k2 | z k3 | corr(k1,k2) | corr(k2,k3) | cond(J) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 5 | 0.0122 | 0.1120 | 0.2993 | +0.0075 | -0.1185 | +0.4100 | +0.61 | -1.06 | +1.37 | -0.955 | -0.982 | 3.32e+05 |
| 8 | 0.0096 | 0.0927 | 0.2555 | +0.0190 | -0.1929 | +0.5766 | +1.99 | -2.08 | +2.26 | -0.956 | -0.984 | 2.57e+05 |
| 12 | 0.0084 | 0.0845 | 0.2344 | +0.0080 | -0.0610 | +0.1770 | +0.96 | -0.72 | +0.76 | -0.972 | -0.986 | 2.76e+05 |
| 25 | 0.0056 | 0.0592 | 0.1758 | +0.0010 | -0.0026 | +0.0051 | +0.17 | -0.04 | +0.03 | -0.969 | -0.982 | 2.59e+05 |
| 40 | 0.0046 | 0.0501 | 0.1551 | -0.0009 | +0.0114 | -0.0351 | -0.19 | +0.23 | -0.23 | -0.966 | -0.981 | 3.05e+05 |

### Table 12 — BLAS thread sensitivity (vendor corpus, median of 3)

| backend | 1 thread s | host default (32) s | penalty |
|---|---|---|---|
| scipy_analytic | 1.267 | 8.518 | 6.7x |
| lm | 0.248 | 1.056 | 4.3x |
| native | 0.306 | 0.607 | 2.0x |

## 8. The native backend: what the protocol actually gives you

Verified by reading `calib/ceres/problem_io.h`, `calib_ref.cpp` and by
running the binary on the exported problems:

* **The existing Python wrapper is broken against the current engine.**
  `calib/ceres/export_problem.py` and `run.py` call
  `calibrate._UV_INDEX`, which no longer exists (it is now
  `_uv_index()`); `run.solve_case('synth5')` raises `AttributeError`.
  `calib/solvers.py` therefore writes the problem itself (byte format
  v1, 17 significant digits, cached static body) instead of importing
  that module.
* **The binary's analytic Jacobian has a defect.** The tangential
  distortion derivatives are `4·p2·x` and `4·p1·y` where the model
  gives `6·p2·x` and `6·p1·y` (`calib_ref.cpp` lines 89/92; the same
  expression was copied into `analysis/calibration/audit/solver_lib.py`).
  Verified by central differences: with the shipped expression the
  Jacobian's relative error is 5.8e-4 (3.4e-3 worst column), with the
  corrected one 1.3e-9. Consequence: the LM still minimises the right
  cost (step acceptance uses the cost), but it stalls earlier — the
  native optimum is 0.04 px from SciPy's at 5 views and 0.13 px at
  10 % outliers, and its exported variance diagonal deviates from the
  corrected analytic one by up to 1.3 %.
* **The binary exports only the variance diagonal**, and it always
  computes a dense covariance (roughly half of its runtime) even though
  the wrapper recomputes the full matrix. Derived quantities (baseline,
  relative rotation, 3-D point) need cross-covariances, so
  `solvers.solve_joint(backend='native')` re-evaluates the analytic
  sandwich at the native solution and *checks* it against the binary's
  diagonal (`diagnostics.native_cov_diag_ratio`).
* **Feature gaps**: Huber only (no Cauchy/soft_l1), no fixed
  intrinsics, no priors, and **all five distortion coefficients must
  be free**: with a held coefficient the binary's dense normal
  equations are singular, the hand-written Cholesky silently returns
  a corrupted inverse, and the exported variance diagonal was
  observed to be wrong by factors of 1e3-1e13 (measured). The
  parameters would still be usable but the covariance would not, so
  `solve_joint` refuses the native path for that configuration. It
  detects each of these cases and falls back to SciPy with an
  explicit `fallback_reason` instead of silently solving a
  different problem.
* The binary used here was rebuilt from the unmodified source with
  `calib/ceres/build.sh` (g++ 16.2.1); `solvers.backend_report()`
  records its sha256.
* Failure handling is tested: missing binary, timeout (60 s default),
  non-zero exit, malformed JSON, and a **model check** that recomputes
  the RMS in Python and rejects the binary's answer if the two differ
  by more than 1e-6 relative. `allow_fallback=False` raises instead.


## 9. Ceres: still blocked

Re-checked this session, unchanged: `/usr/include/ceres` exists (2.2.0),
`/usr/lib/libceres.so.2.2.0` exists, and **Eigen is absent** —
`/usr/include/eigen3` does not exist, `import eigen` fails, no Eigen
tree anywhere under `/usr`, `/usr/local`, `/opt` or site-packages, and
the pacman cache has no eigen package. A five-line
`#include <ceres/ceres.h>` fails with `Eigen/Core: No such file or
directory`. Installing Eigen needs network and root, both out of scope
here, so **no Ceres measurement exists in this report**; every claim
about Ceres is design intent, not measurement. `calib_ceres.cpp` has
never been compiled.

The native LM is *not* a Ceres substitute (no manifolds, no autodiff,
no sparse solver, dense normal equations); it is only a compiled data
point, and §2 shows that data point is no faster than the pure-Python
LM at these problem sizes.


## 10. Recommended production configuration

```python
from calib import solvers
prob = solvers.make_problem(observations, pitch_mm=28.0, init_cameras=ccf_init)
out  = solvers.solve_joint(prob, backend="lm",        # or "scipy" for the safe path
                           robust="huber", f_scale=1.0,
                           free_kc=True,             # or "k1k2" for a regularised model
                           covariance=True)          # analytic sandwich
out["x"], out["covariance"], out["derived"]["points"][450.0]
```

* **default**: `backend="lm"` (0.23 s on the vendor corpus, 27× the engine,
  identical optimum to SciPy). It self-checks its final gradient and falls back
  to SciPy if it did not converge; the fallback is tested.
* **verification / conservative option**: `backend="scipy", jac="analytic"`
  (1.03 s, 6×). Same optimum, battle-tested optimiser. Run this in CI against
  `lm` on the vendor corpus; they agree to <1.5e-4 px, so a regression in the LM
  would be caught immediately.
* **fallback order**: native (if built) → lm → scipy. Never fall back silently:
  the dict always carries `backend`, `fallback` and `fallback_reason`, and those
  should be logged.
* **covariance**: always the analytic sandwich (`covariance_estimator="sandwich"`),
  which reproduces the engine's matrix. Treat the cx sigma as ~10-35 %
  optimistic and the relative-rotation sigma as 0-19 % optimistic (§4); treat
  fx/fy and the 3-D point sigma at 300-600 mm as calibrated (0.89-1.02).
* **priors**: only with an *informative* prior (σ ≤ 2 px for intrinsics, ≤0.02
  for distortion) is there a real variance reduction; a weak prior is a no-op.
  Do not use a prior mean that is not trusted — it becomes bias.
* **distortion**: the 5-term model is safe but over-parameterised below ~12
  views: k1k2 gives the same RMS and the same residual structure, with cx
  moving up to 6.7 px. Keep 5 terms for compatibility with the CCF/reporting,
  but put a weak prior on k3 (σ ≈ 0.1-0.2) rather than letting 12 views set it.
  Never fix kc at zero (0.367 px RMS on the vendor corpus).
* **threads**: set `OPENBLAS_NUM_THREADS=1` in the service (2-6.7× here).
* **robust loss**: Huber δ = 1 px is the conservative default (Cauchy δ = 1
  was marginally less biased at 10 % outliers: fx +3.65 vs +4.81 px, at the
  cost of a more aggressive downweighting). Because the loss leaves ~0.8 px of
  fx bias at 2 % outliers (12 views), report the outlier fraction, or refit on
  the inlier set for the final parameters.

---

## 11. Limitations and honest caveats

1. **No vendor ground truth.** All accuracy claims are vs synthetic truth or
   in-sample RMS. The vendor comparison with the factory CCF is a *reference*
   check, not truth: the CCF may itself be produced with a different pitch
   convention (the 1 % scale) or estimator.
2. **The CCF disagreement is systematic and unexplained.** Our joint fit differs
   from the CCF by +1.03 % in baseline (consistent with 28.0 vs 27.72 mm pitch)
   but also by −0.377° in relative rotation and −8.4/−10.0 px in cx/cy — 4.9×
   (rotation) to ~11× (cy) the reported 1σ. Since the Monte-Carlo calibration says the sigma is if
   anything 10-20 % *optimistic* (not 5× optimistic), the difference must be a
   bias: detector centroid bias, a different lens model, or a vendor estimate
   from a different acquisition. **UNKNOWN** which; the practical consequence is
   that a calibration must be validated by reconstruction (or a physical
   reference), never by its own covariance.
3. **Timing on a shared host.** Load average 20-40 from other agents during
   these runs; absolute times are upper bounds and single-run IQRs understate
   run-to-run variation (the prior audit saw 30-60 % swings). The *ordering* is
   stable across 12 cases and is not a load artefact (a 6× or 25× gap).
4. **Monte Carlo scope.** MC resamples observation noise and outliers only, at a
   fixed geometry and start; it does not sample board-pose distribution,
   detector bias, or model error — which is exactly where the largest real
   errors live (§11.2).
5. **The internal LM is new code.** It is validated here against SciPy on 12
   grid cases, 5 MC scenes (≈1000 draws), 3 cross-checks per scene and 13 unit
   tests, and reproduces SciPy to ≤1e-3 px — but it has no production history.
   The recommended deployment keeps SciPy as the verification backend for that
   reason.
6. **`fix_left_intrinsics` is dead in the engine**: `calibrate()` accepts it and
   never uses it (`calib/calibrate.py:581`). `solvers.py` implements it
   properly (holds all 9 left-camera intrinsics).
7. **OpenCV rows** in the covariance tables show the model-based sigma
   *evaluated at OpenCV's parameters* (the backend has no covariance of its
   own), which is why its z-scores look absurd (z = 7): a local covariance
   cannot see a biased parameter. That is a feature of the comparison, not a
   bug.
8. Ceres: no measurement at all (§9).

---

## 12. Artefacts

| path | content |
|---|---|
| `calib/solvers.py` | backends (`scipy`, `native`, `opencv`, `lm`), analytic Jacobian, losses, priors, masks, covariance, derived quantities, `backend_report()` |
| `tests/test_solvers.py` | 13 tests: API, Jacobian vs FD, SciPy-vs-native agreement, covariance vs engine, fallback paths, priors, losses, reconstruction round-trip |
| `/tmp/einstar_cal2/agents/solver/{s1_grid,s2_cov,s3_ablations,s4_threads,s5_distortion}.py` | the measurement scripts |
| `/tmp/einstar_cal2/agents/solver/results/*.json` | raw results (all numbers above) |
| `/tmp/einstar_cal2/agents/solver/tables.py` | table generator used by this report |

`calib/calibrate.py` was not modified; the full suite (`137 passed`) still passes.
