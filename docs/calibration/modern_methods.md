# Modern calibration methods vs the Einstar plate workflow

Scope: what the replacement should borrow, what it must not borrow, and what
would require a different target. Claims about published methods are standard
literature results; claims about *this* scanner are cited to the recovered
code/corpus.

## 1. Method comparison

| method / tool | what it gives | compatible with the Einstar plate? | use in the replacement |
|---|---|---|---|
| **Zhang (2000) planar calibration** | closed-form homography initialisation + maximum-likelihood refinement for a planar target; the model underlying OpenCV's `calibrateCamera` | **yes** — the plate is planar and the 40 hole centres are known | initialisation + baseline comparison; our BA is the ML refinement |
| **OpenCV `calibrateCamera` / `stereoCalibrate`** | Brown–Conrady model, per-view poses, stereo extrinsics, `CALIB_USE_INTRINSIC_GUESS`, fixed-parameter flags | **yes** — same model as the CCF (5-term distortion) | used as an initialiser and as an independent cross-check (see §10.1) |
| **ChArUco / ArUco** | coded markers give unique IDs, partial-view robustness, no grid ambiguity | **no, not with the shipped plate** — requires printing a new target | only if a new target is allowed; would improve partial-view detection and eliminate the big-marker orientation code |
| **AprilTag 3** | robust binary fiducials, long range, low false-positive | **no, requires a new target** | same as ChArUco; useful for a future target |
| **Joint multi-camera bundle adjustment** | one problem over all cameras, views and points; correct uncertainty propagation | **yes** | our `calib/calibrate.py`; the vendor's `TribleCCF` is the same idea |
| **Ceres Solver** | mature LM/trust-region, autodiff, robust losses, Schur, covariance | **yes** | recommended production backend; our prototype uses SciPy for portability |
| **Optimal experimental design / Fisher information** | choose the next view to maximise information; A/D-optimality | **yes** | `calib/adaptive.py`; the vendor uses a fixed 5×5 grid |
| **Uncertainty-driven stopping** | stop when parameter covariance/held-out error meets a target | **yes** | `stopping_report` + held-out check; the vendor always runs 25/25 |
| **Robust sub-pixel extraction** | ellipse fitting / gradient-based edge localisation instead of a thresholded centroid | **yes** | vendor uses ellipse fitting; our centroid is the main accuracy limit (~0.25–0.3 px) |
| **GPU-accelerated detection** | real-time marker detection for guidance | **yes** | not needed for the offline solve; useful for live feedback at 30 fps × 3 cameras |

## 2. What the vendor already does well

* **Planar ML calibration** with the correct Brown–Conrady model and a joint
  3-camera refinement — the same family as Zhang/OpenCV/Ceres.
* **Ellipse-based sub-pixel marker extraction** (Canny 75/150, Gaussian σ=2.1,
  ellipse fitting). Our audit confirms edge/conic localization is 2–20× better
  on synthetic tilted/near-hole cases (0.018–0.029 px vs 0.055–0.544 px), while
  the real-corpus residual floor is model-limited so the centroid still wins
  there (0.367 px).
* **A conservative capture plan** that covers the field and tolerates bad
  detections without a robust loss.

## 3. What the vendor does not do

* No robust loss in the PnP stage (`loss = nullptr`), no covariance, no
  held-out validation, no per-view outlier rejection beyond a fixed invalid
  fraction, no adaptive stopping, no backup, no scale-uncertainty model.
* The fixed 5×5 plan is not optimised; our ablation shows most of its scale
  information is available from 3–5 views and its intrinsic information from
  10–20.

## 4. Recommended replacement stack

1. **Target**: keep the original plate for compatibility; add a **plate-spec
   file** (grid, big-marker code, pitch) so an 8×5 or 8×7 variant can be used.
   If a new target is allowed, a ChArUco/AprilTag board would remove the
   orientation ambiguity and improve partial views, at the cost of a new
   factory calibration and a new mechanical holder.
2. **Detection**: keep the current blob detector for the plate; add
   **ellipse fitting** on the hole edges for sub-pixel refinement (target
   < 0.1 px) and a **GPU/parallel** path for live guidance.
3. **Initialisation**: OpenCV/Zhang homography + PnP, as now.
4. **Refinement**: joint robust BA in **Ceres** (or SciPy for the prototype),
   with Huber/Cauchy loss, Schur complement, and analytic Jacobians.
5. **Uncertainty**: covariance from the reduced camera system; propagate to
   baseline, depth scale and derived quantities; validate with NEES.
6. **Planning**: Fisher-information greedy selection with **diversity
   constraints** (distance bands, tilt signs, image quadrants); stop on
   parameter σ + held-out reprojection + coverage.
7. **Storage**: versioned host profile with immutable factory snapshot; device
   write only through an explicit, verified export.

## 5. CPU/GPU plan for the target machine (i9-14900K, RTX 3080 12 GB, 64 GB)

| stage | current | optimised |
|---|---|---|
| detection (75 images) | ~7 s single-thread Python/OpenCV | < 1 s with a process pool over 12–16 images; ROI around the plate |
| calibration (72 obs, 189 params) | 10.4 s SciPy dense FD | 1.21 s SciPy analytic J (7.8×); 0.055 s native C++ LM (172×); Ceres source cannot compile here (Eigen headers missing) |
| covariance | 3.8 s finite differences | ms with the analytic Jacobian; native LM returns analytic covariance |
| BLAS threads | default 32 OpenBLAS is ~20× slower for the dense SVD | `OPENBLAS_NUM_THREADS=1` gives 2.5–2.7× on the engine, no code change |
| live guidance (3 × 30 fps) | not implemented | downscale to 640×512, detect only the plate ROI, OpenCV CUDA or a small GPU blob kernel; CPU fallback |

Measured on the vendor 25-view problem: SciPy dense 9.44 s, SciPy+analytic
1.21 s, native C++ LM 0.055 s, OpenCV per-camera+stereo 0.137 s (weaker
rig-imposed problem, not directly comparable). Ceres 2.2.0 is installed but
unusable without Eigen 3 headers; do not put it on the critical path until the
packaging is fixed. The native LM is the recommended production path after an
independent cross-check.

The GPU is **not** the bottleneck for the offline solve (a few thousand
residuals); it matters only for live marker feedback. A CPU-first
implementation with a process pool meets the offline target comfortably.

## 6. What would change the target

* **ChArUco/AprilTag**: better partial-view robustness and unique IDs, but the
  shipped plate's holes are the vendor's mechanical/optical reference; a new
  target invalidates the factory calibration and needs a new board SN /
  cloud descriptor. Only do this if the user accepts a new calibration target.
* **Coded/encoded dot patterns**: the vendor's `EncodePointsRecognition` and
  `bigCircleNum` suggest other products use coded targets; the Einstar board
  uses the 4-large-hole orientation code instead.

## 7. References

* Z. Zhang, “A flexible new technique for camera calibration”, *IEEE TPAMI*
  22(11):1330–1334, 2000.
* OpenCV, `calibrateCamera`, `stereoCalibrate`, `solvePnP`, ChArUco tutorial
  (OpenCV 4.x documentation).
* S. Garrido-Jurado et al., “Automatic generation and detection of highly
  reliable fiducial markers under occlusion”, *Pattern Recognition*, 2014
  (ArUco); M. Krogius et al., “AprilTag 3”, 2019.
* S. Agarwal, K. Mierle et al., *Ceres Solver* documentation.
* F. Pukelsheim, *Optimal Design of Experiments*, 2006 (A/D-optimality).
* R. Hartley & A. Zisserman, *Multiple View Geometry*, 2004 (bundle
  adjustment, gauges, covariance).
