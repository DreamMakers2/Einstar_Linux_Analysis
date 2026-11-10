# Einstar adaptive calibration — system design and technical decisions

Status: 2026-11-10. This document records the architecture that was built, the
evidence for each choice, and the alternatives that were measured and rejected.
Companion documents:

* `docs/calibration/research_review.md` — literature and standards review
  (AprilCal, Kalibr, Zhang/OpenCV, OED, robust estimation, VDI/VDE 2634,
  ISO 10360-13);
* `docs/calibration/localization_evaluation.md` — seven sub-pixel localisers on
  the real vendor corpus and on synthetic ground truth;
* `docs/calibration/solver_evaluation.md` — SciPy / LM / native C++ / OpenCV /
  Ceres comparison, covariance calibration, robust losses, priors;
* `docs/calibration/strategy_evaluation.md` — Monte-Carlo comparison of capture
  strategies and the guided session;
* `docs/calibration/plate_spec_recovered.md` — the plate geometry.

---

## 1. Objective and constraints

Replace EXStar's fixed 5 orientations x 5 distances procedure with a system that
achieves the best practical combination of **physical 3-D accuracy,
repeatability, reliability, operator effort and compute cost** on the original
Einstar scanner, without changing the hardware, the plate or the factory
calibration.  The scanner is not available for this phase: everything is built
and validated offline, and the acquisition-facing interfaces are designed for
later live use.

Hard bounds:

* the original 8 x 5 plate with 41 markers and 28.0 mm pitch
  (`plate_spec_recovered.md`);
* three cameras (left/right IR + texture), a rigid rig, Brown-Conrady
  distortion, a planar target;
* no writes to the scanner; the factory CCF stays immutable;
* CPU only (the solve is a few thousand residuals — GPU is not the bottleneck);
* no physical measurements or photographs.  Physical accuracy therefore cannot
  be *proven* here; §10 states exactly what can and cannot be claimed.

## 2. What the evidence base fixes

| fact | value | source |
|---|---|---|
| marker pitch | 28.0 mm | vendor 17x14 lattice at 14.00 mm, markers on every second node |
| large markers | 3 on-lattice + 1 off-lattice | `plate.py`, corpus detection |
| camera model | 5-term Brown-Conrady | CCF records |
| vendor corpus | 25 poses x 3 cameras | `res/` + capture |
| vendor CCF | written on the same corpus | device record timestamp + byte identity |
| 1 % scale gap | 28.0 vs 27.72 mm equivalent | CCF baseline 158.557 vs our 160.14 mm |
| pitch observability | **zero** from reprojection | global scale gauge; only an external length can settle it |

The last row is decisive for the validation design: no residual, covariance,
NEES or held-out test can detect a wrong pitch.  Agreement with the CCF is
therefore *not* evidence of physical accuracy; it is a consistency check.

## 3. Design space and what was adopted

Maturity classification follows the research review (§8 there).

| approach | maturity | decision for Einstar |
|---|---|---|
| Zhang / OpenCV planar initialisation | established | used for PnP initialisation and as an independent cross-check |
| joint multi-camera bundle adjustment | established | core solver (3 cameras, rigid rig, per-view poses) |
| Kalibr-style batch MAP + Schur marginal | established (structure only) | adopted; the IMU/continuous-time machinery is irrelevant for stationary dwells |
| AprilCal-style *online* next-best-view | validated research prototype | adopted in spirit: greedy gain, but with a **task-space 3-D** objective instead of tag-corner ERE (tag corners have no eccentricity bias and its absolute numbers do not transfer) |
| A/D/E-optimal experimental design | established theory | A-optimal on the **task** covariance; D/E reported, D rejected for mixed units |
| robust loss (Huber/Cauchy) | established | fixed-scale Huber δ = 1 px (never adaptive scale; see §7) |
| sandwich covariance | established | reported; GN/Fisher used for *design* only |
| circular-marker eccentricity / conic back-projection | established for large markers | implemented; negligible on this plate (r = 1.77 mm), exact for large markers |
| ChArUco / AprilTag target | established but **changes the target** | rejected: invalidates the factory calibration and mechanical reference |
| Ceres backend | mature, but unavailable here | rejected for this build (Eigen absent); LM+SciPy covers the need |

## 4. Architecture

Five stages, each with a hard gate.  The operator only ever sees stage 3.

```
             ┌─────────────────────────────────────────────────────────┐
  frames ──► │ 0 bootstrap: 3-5 diverse views (easiest diverse pose)   │
             │ 1 coverage: distance x tilt x roll x quadrant bins,     │
             │    stereo overlap, 3 large markers, field edges        │
             │ 2 task-space gain: 3-D volume covariance, A-optimal,    │
             │    effort-aware ranking, predicted Δσ in mm            │
             │ 3 guided dwell capture: quality gates + accept/reject   │
             │    + operator instruction; two-stage localisation      │
             │ 4 batch LM + sandwich covariance; saturation stop       │
             │ 5 verification: held-out, split-half, epipolar,          │
             │    tilt-slope bias, versioned host profile              │
             └─────────────────────────────────────────────────────────┘
```

### 4.1 From a frame set to an observation

A *captured set* is the three synchronised images of one instant.  A set is
committed only if:

* every camera has ≥16 markers, and left/right share ≥16 (the triangulating
  pair); the three on-lattice large markers are present;
* plate area 3–92 % of the frame, saturation <2 %, IR contrast ≥14 DN,
  tex contrast ≥5 DN, IR focus ≥150, tex focus ≥55 (thresholds measured on the
  iverson corpus; see `guidance.DEFAULT_QUALITY`);
* camera timestamp skew ≤3 ms;
* the board is stationary: <3 mm and <1.5° between consecutive frames (a dwell
  of ≥0.25 s).  The first frame after a move is rejected as "moving"; the next
  stationary frame is the keyframe;
* the predicted task gain exceeds the mode threshold, or the pose opens a new
  coverage bin.

Rejections are returned with the exact reason; nothing is silently dropped.

### 4.2 Two-stage localisation

Live tracking uses `centroid_bg` (0.36 ms/marker; 13 ms for 3 cameras x 12
markers = 26 % of one core at 20 fps).  The committed dwell frame is refined
with `auto` (perspective-corrected conic fused with the centroid; 1.3 ms/marker,
53 ms for 41 markers).  On the vendor corpus `auto`/`conic_perspective` reduce
the leave-one-out homography residual from 0.281 to 0.247 px and the per-marker
disagreement from 0.082 to 0.009 px; the resulting joint fit has 0.33 px RMS vs
0.37 px for the centroid pipeline.  The `ellipse` localiser is never shipped
(worst split-half repeatability, 0.094 px).

### 4.3 Task-space objective (the core decision)

Minimising intrinsic-parameter covariance is the wrong estimand.  The system
propagates the rig covariance to the 3-D covariance of a reconstructed point at
45 samples over the 200–600 mm volume (a cone at ±35 % of the distance):

```
X_hat = T(u_L, u_R; theta)                 (undistort + triangulate)
G(X)  = dX_hat/dtheta                      (finite differences of T)
Sigma_X = G Sigma_theta G^T + Sigma_noise
```

`Sigma_theta` is the Gauss-Newton (Fisher) marginal covariance of the 39 rig
parameters after eliminating the board poses by their Schur complement.  A
candidate view adds its predicted observations, its six pose parameters are
eliminated, and the task objective is re-evaluated.  A-optimal (mean trace),
D-optimal (mean log-det), E-optimal (worst direction), depth-variance and
surface-variance variants are available; A-optimal on the volume is the default,
D is used only for reporting because its determinant mixes px² and mm² units.

Two details make the predictor correct:

1. **Consistent information units.** The prior must be the *Fisher*
   information `A/sigma²`, not `inv(robust sandwich covariance)`.  Mixing the
   two made a single view appear to remove 60 % of the variance; with the
   consistent form the same view predicts 19.3 % and the actual re-solve gives
   19.7 % (2 % relative error).
2. **Visibility masking.** Candidate Jacobians are zeroed for markers outside
   each camera's image; the view is rejected unless ≥8 markers are in both IR
   images and the large-marker code is visible.  Without this, invisible
   residuals contribute fictitious information.

The task metric also yields an exact linear form for A-optimality,
`trace(G C G^T) = trace(C M)` with `M = sum_p G_p^T G_p`, which is what the
A/D/E comparison uses.

### 4.4 Acceptance, effort and operator instructions

Candidates are ranked by predicted task-variance reduction.  Operator effort is
a **lexicographic tie-break**, not part of the utility: of the candidates within
10 % of the best predicted gain, the session recommends the easiest one.  This
follows the measurement that putting effort in the utility costs ~0.03 mm at
25 views, whereas effort among near-equal candidates is free.  The effort model
is a documented design choice, not measured human data:

```
effort = 0.15 + lateral/150 mm + depth/150 mm + angle/25 deg
```
The recommendation is translated into plain language from the relative camera
motion: "move left 13 cm, up 6 cm, forward 1.6 cm, rotate the plate 80°".
Angles above 60° are described as a plate rotation rather than a Rodrigues
component.  The pose convention is normalised so that a PnP pose of the board
can be compared with the candidate pool (the detector's lattice assignment is
mirrored in v relative to the vendor lattice frame; the calibration is
invariant to that global rotation, pose-space guidance is not).

### 4.5 Solver and covariance

The batch problem is solved with the internal LM backend
(`calib/solvers.py`): analytic Jacobian, Huber δ=1, 0.23 s on the 25-view vendor
problem (27x the engine's finite-difference SciPy path), identical optimum to
SciPy (≤1.5e-4 px).  SciPy+analytic is the verification path; a native C++
binary exists but has a documented 4-vs-6 tangential-distortion Jacobian defect
and a singular covariance when distortion is held, so it is not on the critical
path.  Covariance is the analytic Huber sandwich; Monte Carlo calibration is
0.93–1.02 for fx, 0.88–0.98 for the baseline, 0.89–1.00 for 3-D σ at 300–600 mm,
but 0.63–0.91 for cx and 0.81–1.00 for relative rotation — those are reported
with that caveat, not as accuracy claims.

### 4.6 Stopping

Stopping is conservative and multi-gate:

* mandatory quality gates: views, distance bins, tilt signs, image quadrants,
  observations, numerical rank, parameter stability, held-out RMS, σ(fx),
  σ(baseline), σ(rotation), σ(RGB–IR);
* measured saturation: the median 3-D σ improvement over the last four solves
  must be below 15 µm (standard), 5 µm (high), 50 µm (quick);
* predicted marginal benefit: the best remaining view must change the median σ
  by less than the mode threshold;
* no unused reachable pose left.

Held-out RMS is genuine: for each accepted view the residual under the
*previous* calibration is recorded before the view enters the fit.  The
stability gate compares rig parameters with a one-view-drop refit and excludes
the weakly identified distortion coefficients from the pass/fail decision
(reported separately).

### 4.7 Host-side profiles

`calib/profiles.py` writes versioned profiles with an immutable factory
snapshot; `integration.to_ccf_records` produces the 33-double CCF layout with
the factory IR trailer preserved.  `CalibrationSession.finish(profile_root,
serial)` persists the final solve as a new revision (CCF files plus the
covariance and parameter names) and activates it host-side.  Nothing is written
to the scanner; a device write would require an explicit, separately audited
step.

## 5. Component map

| file | role |
|---|---|
| `calib/plate.py` | recovered plate geometry, frames, distance refs |
| `calib/detect.py` | blob front-end + grid assignment (unchanged front-end) |
| `calib/localize.py` | 7 sub-pixel localisers + two-stage refinement |
| `calib/calibrate.py` | joint 3-camera BA, Schur covariance (reference path) |
| `calib/solvers.py` | LM/SciPy/native backends, analytic Jacobian, priors, derived quantities |
| `calib/objective.py` | task-space 3-D covariance, A/D/E objectives, candidate scoring |
| `calib/guidance.py` | quality gates, instructions, reachability, acceptance, coverage |
| `calib/session.py` | adaptive session state machine, modes, stopping |
| `calib/simloop.py` | closed-loop operator/camera simulator |
| `calib/verify.py` | held-out, split-half, epipolar, tilt-slope bias diagnostics |
| `calib/ui.py` | headless dashboard prototype |
| `calib/quality.py` | stopping report, coverage, depth/rig uncertainty |
| `calib/profiles.py` | versioned host profiles |

## 6. Why a *guided continuous* workflow rather than pure subset selection

Offline subset selection can only choose from frames that already exist; it
cannot tell the operator to rotate the plate to the one pose that identifies
the weakest direction.  The measured ranking (`strategy_evaluation.md`) shows
the online task-space planner reaching lower 3-D error than the vendor 5x5 plan
at the same capture budget (0.193 vs 0.380 mm at 25 views, 10/10 trials), and
the coverage-first bootstrap guarantees that the weak directions are excited
before any greedy choice.  The guided session adds the parts a subset selector
cannot provide: dwell/blur/sync gating, explained rejections and a stopping
decision.  Its mode comparison is underpowered at two trials, so the modes are
offered as operator profiles rather than as an accuracy guarantee.

## 7. Rejected alternatives and why

| alternative | measured reason for rejection |
|---|---|
| adaptive-scale robust loss | reclassifies deterministic tilt-proportional bias as outliers and *shrinks* the reported covariance; use fixed δ |
| D-optimal on raw parameters | mixes px, radians and mm in one determinant; not physically meaningful |
| parameter-covariance-only selection | improves fx/cx but not necessarily the 3-D volume error; the task objective is the estimand |
| effort inside the utility | at 25 views the effort-weighted strategy was 0.222 mm vs 0.193 mm for task-only (10 trials); effort belongs in the *live* tie-break, not in the accuracy objective |
| ellipse localiser | worst split-half repeatability (0.094 px) and 8 % marker rejections |
| 5-term distortion at ≤12 views | indistinguishable from k1k2 in RMS, but k3 is weakly identified (σ 0.30 at 5 views) and destabilises the intrinsics; keep 5 terms for CCF compatibility with a weak k3 prior |
| Ceres | not installable here (no Eigen, no root); no measurement exists, so it is not on the critical path |
| native C++ LM | 0.25 s vs 0.23 s for LM, with a verified Jacobian defect and a singular held-distortion covariance |
| AprilCal's raw ERE | designed for tag corners; on circular holes the ERE and 3-D objectives differ, and its absolute numbers do not transfer |

## 8. Failure modes and diagnostics

| failure | detector | remedy |
|---|---|---|
| motion during capture | board-centre/angle change >3 mm/1.5° per frame | reject, ask to hold |
| blur / defocus | Laplacian variance in the plate ROI | reject with "defocused" |
| saturation | fraction of pixels ≥250 | reject |
| camera desynchronisation | timestamp skew >3 ms | reject |
| wrong grid orientation | large-marker code missing | reject "large markers 2/3" |
| redundant view | predicted gain below threshold and same bin | reject "redundant view" |
| model bias (tilt) | significant radial residual vs tilt slope | flag; conic localiser / reject high tilt |
| over-fitting / instability | split-half z, one-view-drop stability | do not accept as high-accuracy profile |
| pitch/scale error | **invisible** | requires an external length artefact |

## 9. Reproduce

```bash
# tests (157)
HOME=/tmp OPENBLAS_NUM_THREADS=1 .venv/bin/python -m pytest -q

# real corpus, fast path + best localiser
REPO=<repo>
.venv/bin/python -m calib.cli calibrate <private-inputs>/vendor-calib-rapid \
  --factory sources/calibration/res_Einscan-E10_200x150 --no-anchor \
  --solver lm --localize auto --out /tmp/calib_lm.json

# guided session demo (closed-loop synthetic operator, writes a dashboard)
.venv/bin/python -m calib.cli guide --mode standard --max-views 20 --out /tmp/session_demo

# strategy Monte Carlo and session modes
cd analysis/experiments
python3 strategy_bench.py --trials 10 --workers 12 --out /tmp/strategy_mc.json
python3 session_modes.py  --trials 4  --workers 8  --out /tmp/session_modes.json
```

## 10. What can and cannot be claimed

**Can be claimed (measured):** internal consistency, repeatability, parameter
and 3-D uncertainty (with the covariance caveats in §4.5), detector accuracy on
synthetic ground truth, strategy ranking on synthetic ground truth, and
agreement with the vendor CCF on the shared corpus.

**Cannot be claimed:** absolute physical accuracy in millimetres, ISO/VDI
conformity, or that the 28.0 mm pitch is the plate's true pitch.  The pitch is
a scale gauge; the 1 % CCF-vs-nominal difference is unresolved and visible in
every report.  The honest statement is: *accuracy unverified; scale traceable
to the plate pitch only*.

A physical validation requires, in order of value:

1. a calibrated sphere bar (VDI/VDE 2634-2 sphere-spacing test) measured at
   200/300/400/500/600 mm and across the field — this tests scale, distortion
   and depth;
2. a flatness artefact (VDI/VDE 2634-3) for form error;
3. repeated scans of the same artefact (ISO 5725 / ISO 10360-13) for
   repeatability and reproducibility;
4. a reversal/hysteresis test at the volume extremes.

Until (1) exists, no millimetre-accuracy claim should be made, and the CCF is
only a reference, never truth.
