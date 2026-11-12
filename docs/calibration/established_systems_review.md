# Established calibration systems: evidence and adoption review

Reviewed 2026-11-12. Scope: the original Einstar, a rigid three-camera rig (two IR cameras and one texture camera), its existing circular-marker plate, and USB 2 High Speed acquisition. This review uses repository source, recorded evaluations, public manuals, and primary research. No scanner operation, package installation, binary execution, or new performance experiment was performed.

## Evidence and project baseline

**A** means inspectable implementation, established mathematical derivation, or reproducible measurements with stated conditions. **B** means a public vendor operational description or a published experiment whose transfer to Einstar remains unverified. **C** means a product claim, an engineering estimate, or an inferred implementation. Grades qualify individual claims: a good operator manual does not disclose its vendor's optimizer. **Unknown** means the reviewed sources do not establish the detail; it does not mean that the vendor lacks it.

The baseline reads are [README](../../README.md), [STATUS](../STATUS.md), [system design](calibration_system_design.md), [research review](research_review.md), [solver evaluation](solver_evaluation.md), [strategy evaluation](strategy_evaluation.md), and [localization evaluation](localization_evaluation.md). Source inspection covers [solvers.py](../../calib/solvers.py), [calibrate.py](../../calib/calibrate.py), and the [Ceres prototype](../../calib/ceres/README.md). Later [wizard results](wizard_results.md), [operator guide](wizard_operator_guide.md), and [partial recognition report](partial_plate_recognition.md) prevent treating implemented features as proposals.

The November 10 design's statement that the scanner was unavailable is historical. November 12 STATUS reports live smoke tests, a synthetic complete calibration, host profile activation and rollback, but still no independent absolute-accuracy result. The wizard already has previews, guidance, observability, coverage, lifecycle gates, and partial-plate handling. Recommendations below extend or validate these facilities.

## Method table

Gains marked **measured** are existing repository results, not commercial comparisons. Unmeasured expectations have grade C and no accuracy guarantee.

| Technique | Public documentation | Quality | Einstar applicability | Expected gain | Principal risk |
|---|---|---|---|---|---|
| Guided position/distance capture | SHINING EXStar [S1], Artec [S2] | B | Direct UX precedent | Fewer failed attempts; unmeasured here | Fixed green zones can hide missing information |
| Independent scale references | ZEISS TRITOP [S4], BA gauge analysis [S12] | A theory/B product | Essential verification | Resolves otherwise invisible metric scale | Reference uncertainty and temperature |
| ROI, coarse detection, fine localization | HALCON [S7], OpenCV [S8] | A interface/B transfer | Retain existing ROI path; optimize measured hotspots | Lower CPU latency; unmeasured new gain | Cropping loses identity witnesses |
| Intensity centroid plus background fit | Creaform discusses alternatives [S3]; local benchmark | A local/B vendor description | Live tracking | Measured 0.36 ms/marker | Gradient, clipping, aperture bias |
| Subpixel contours and corrected conics | Heikkilä [S10], HALCON [S7] | A method/B transfer | Frozen dwell frames | Local auto: 0.247 versus 0.281 px homography residual | Bad intrinsics or plane corrupt correction |
| Coded-target partial visibility | Kalibr [S9], HALCON [S7] | A implementation | Borrow identity discipline, retain plate | More useful edge views; already partly implemented | Symmetric circle lattice gives false labels |
| Joint rigid-rig calibration | Kalibr [S9], HALCON [S7] | A/B | Core architecture already adopted | Correlated three-camera estimate | Gauge, timing, disconnected observations |
| Analytic derivatives | OpenCV [S8], BA [S12] | A | Already adopted in solver layer | Measured analytic SciPy ~6× old path | Derivative/convention errors |
| Block Schur elimination | Ceres [S13], BA [S12] | A | Eliminate per-view board poses | Better scaling; current speedup unknown | Incorrect elimination ordering |
| Mature C++ optimizer | Ceres [S13], [S14], Eigen [S15] | A library/C project gain | Optional future backend | Maintainability; no measured Ceres advantage | Prototype unvalidated and objective differs |
| Online information-driven views | AprilCal [S11], Rojtberg [S16] | A study/B transfer | Extend current task-space guidance | Local simulated modest gain against faithful vendor baseline | Optimistic model-dependent predictions |
| Robust estimation and diagnostics | Huber [S18], RANSAC [S19] | A | Already present; retain explicit rejection | Gross-error resistance | Systematic bias mistaken for outliers |
| Bounded exposure and ROI statistics | Basler [S6], Photoneo [S5] | A interface/B transfer | Capture-quality policy | Better usable-frame yield; unmeasured | Saturation, motion blur, unsynchronized settings |
| SIMD, bounded parallelism, selective GPU | OpenCV [S8], Ceres [S13] | A capability/C gain | CPU first; benchmark complete path | Detector throughput; no justified GPU estimate | Transfers, oversubscription, stale guidance |

## What commercial systems actually disclose

**SHINING 3D.** EXStar 1.2.2 documents matching the board to the device, keeping it clean, centering a projected indicator until green, moving through distance indicators, and responding to angle/too-close/too-far prompts. This establishes an operator guidance workflow [S1]. It does not publicly establish its circle estimator, Jacobian, robust loss, Schur implementation, GPU allocation, or statistical stopping criterion. The repository's recovered 5-orientation × 5-distance workflow and distortion handling are separate artifact evidence; they must not be attributed to the manual. Later EXStar Hub products are different devices and software generations.

**Artec.** Studio 19 describes a positioning sheet, a green position plane, stationary capture, evaluation before saving, and scanner-specific availability. It explicitly distinguishes temporary reconstruction correction from calibration: correction need not preserve dimensional accuracy [S2]. Adopt the visible progression and evaluation/save boundary. The manual's quality display does not expose uncertainty calibration or a universal millimetre acceptance threshold. Detector, optimizer, and hardware-specific correction models remain proprietary unknowns.

**Creaform.** Its technical white paper explains stereo projection, subpixel target localization alternatives, triangulation, rigid-body pose, and dynamic referencing [S3]. This is stronger technical evidence than a product slogan, but its discussion of barycentres, ellipse fits, and intensity models does not identify which algorithm every shipping scanner uses. Dynamic referencing measures object and instrument targets together to express measurements in the moving part frame; it does not recalibrate lenses or eliminate all optical error. Exact current feature extraction, calibration optimization, and acceleration remain unknown.

**ZEISS/GOM.** TRITOP documents coded and uncoded reference points and two DAkkS-calibrated scale bars [S4]. The transferable lesson is explicit point identity plus traceable length, not adoption of an entire large-object photogrammetry workflow. TRITOP documentation is not evidence that an ATOS/GOM sensor uses the same internal calibration solver. The reviewed material does not establish ATOS/GOM derivative implementation, loss function, or NBV criterion.

**Photoneo.** PhoXi Control exposes shutter/scan multipliers, overexposure warnings, coding choices, and generation-specific options [S5]. Repeating patterns or scans trades acquisition time for light/SNR; some ambient-light suppression uses sensor/projector timing. These documented controls show why illumination and processing must be optimized together. Their implementation relies on Photoneo hardware and does not establish compatible commands or timing on Einstar.

## Detection, localization, and illumination

Industrial stacks separate finding the plate from measuring its marks. HALCON exposes plate finding, mark contours, subpixel edge smoothing, expected diameters, ROI restriction, and insertion of externally determined observation points [S7]. These are public operator contracts, not disclosure of every internal instruction. They support the project's current coarse-to-fine architecture.

Retain coarse threshold/components and geometric labeling, then track projected ROIs with a padded margin and full-frame reacquisition after failure. Optimize Python patch loops only after profiling current wizard replays: the older 75-image evaluation measured **7.55 s detection versus 0.23 s LM**, but it predates current ROI integration. A crop that accelerates detection but removes orientation witnesses is a regression. The partial-recognition report already demonstrates why a low reprojection error cannot distinguish lattice symmetries; temporal and stereo smoothness cannot substitute for a proven identity.

Keep `centroid_bg` for live pose updates and `auto` refinement for accepted frames. Existing one-core measurements give 0.36 and 1.30 ms per marker respectively. At 41 markers in three images, simple serial arithmetic is approximately **44 ms versus 160 ms per triplet**, excluding detection and pose estimation; these are estimates, not observed pipeline latency. The design's 13 ms live estimate uses only 12 markers per camera. Preserve that distinction when specifying guidance rates.

An ellipse centre is generally not the projected circle centre. Heikkilä establishes bias correction for circular control points [S10]. On this plate, the local evaluation estimates radius 1.77 mm and ideal perspective eccentricity only **0.004–0.016 px** in the evaluated range. Therefore, conic processing is justified by measured localization behavior and geometric correctness, not a claim of millimetre improvements from eccentricity correction alone. The hypothesized through-hole thickness bias requires independent plate evidence; it must not become an unconstrained nuisance parameter that absorbs lens or pose errors.

Use per-camera plate-ROI contrast, clipping fraction, edge support, blur, and marker size to supervise exposure. Basler documents exposure bounds and an auto-function ROI [S6]; use that control pattern if corresponding Einstar controls are established. Adjust while seeking the plate, wait for settling, then freeze settings during a dwell and log them with timestamps. Bound exposure by motion blur and cadence, prefer sufficient illumination to excessive gain, and record separate IR/texture policies. White balance is photometric calibration, not correction of texture-camera extrinsics.

No reviewed evidence gives an optimal Einstar exposure value. Existing DN, blur, and saturation thresholds are empirical corpus settings and need later hardware validation. HDR or averaging would increase latency and motion sensitivity; they should be optional rescue measures for stationary captures, not unconditional acquisition policy.

## Joint estimation and library choice

Retain a single objective over all observed cameras and board poses. Camera 0 fixes the rigid-frame gauge; board dimensions set scale. Missing observations are acceptable when the shared-pose/camera graph remains connected and identity is established. Pairwise fits are useful initialization and independent checks, but do not reproduce the joint objective or its cross-camera covariance. HALCON explicitly documents simultaneous minimization; Kalibr documents overlapping camera networks and information-based image selection [S7, S9]. Kalibr's multi-camera selection from an existing recording is distinct from AprilCal's live next-pose guidance [S11].

`calibrate.py` retains the finite-difference SciPy reference. `solvers.py` supplies analytic projection derivatives, free-parameter masks, priors, LM/SciPy/native alternatives, and covariance/information output. Its LM step constructs dense normal equations; Schur marginalization in covariance/planning does **not** mean the optimization itself already uses a sparse Schur solver. Full-model dimensions are 39 rig parameters plus six per board pose: 189 parameters at 25 views. The measured vendor problem has 5,728 scalar residuals. This is small enough that dense methods are reasonable.

For a future compiled path, partition the normal matrix into retained rig variables and independent per-view poses:

```text
[ A  B ][delta_rig ] = -[g_rig ]
[ B' C ][delta_pose]    [g_pose]
S = A - B C^-1 B';  h = g_rig - B C^-1 g_pose
```

Each pose contributes a 6×6 diagonal block to `C` when there are no cross-view factors. Solve those blocks, solve the retained system, then back-substitute; do not explicitly invert matrices. This specializes standard BA elimination to **board poses**, not free landmarks [S12], [S13]. Use QR/SVD or rank-revealing checks where conditioning requires them; forming normal equations squares the Jacobian condition number.

**Ceres:** prefer it over growing a bespoke production C++ optimizer if profiling eventually justifies migration. It provides autodiff, parameter blocks, robust losses, manifolds, solver ordering, and covariance facilities. For this topology, explicitly eliminate board poses first and compare `DENSE_SCHUR` with dense QR/normal Cholesky; `SPARSE_SCHUR` need not win when only a small rig remains. No Ceres timing exists here. The README records a historical missing-Eigen build blocker; this review neither re-tested nor repaired that environment.

There is a concrete prerequisite beyond building: `calib_ceres.cpp` registers **two-component residual blocks** with `HuberLoss`, while Python/SciPy robustifies scalar coordinates. Ceres applies loss to the squared norm of the entire block [S14]. Thus `rho(u²+v²)` differs from `rho(u²)+rho(v²)`; the README's equivalence statement is incorrect for its current source. Establish a shared loss convention before comparing optima, thresholds, and covariance. Ceres's covariance API also does not automatically reproduce the project's empirical robust-sandwich interpretation.

**Eigen:** adopt as a numerical building block when a compiled backend is warranted. It supplies fixed-size matrices and established decompositions [S15]; it is not a calibration model, optimizer policy, or validation system. **Optimized OpenCV:** retain image primitives, PnP, initialization, and projection/Jacobian cross-checks. The evaluated pairwise OpenCV backend is fast but lacks the same robust joint objective. Verify APIs against the pinned major version; OpenCV 4.x and this repository's 5.x environment must not be conflated. **SuiteSparse** is a factorization backend; **HALCON** is a proprietary industrial reference, not a necessary dependency. Adding g2o/GTSAM or a Lie-group library currently has no demonstrated advantage over the existing model and Ceres path.

## View selection, uncertainty, and model selection

AprilCal establishes live uncertainty-driven capture and a stopping score; Rojtberg and Kuijper explicitly avoid singular planar poses and evaluate on separate training/testing data [S11, S16]. Adopt these mechanisms, not their target-specific image counts or pixel results. The repository already goes further toward its actual task by scoring propagated 3-D uncertainty over a working volume, using coverage-first bootstrap and effort as a near-tie breaker.

Keep the statistical claim modest: the strategy review's faithful comparison is **0.205 versus 0.238 mm median simulated model-induced 3-D error**, adaptive full-model versus vendor-plan fixed-distortion, with 6/10 wins and an interval including zero. The stronger **0.193 versus 0.380 mm** result forces both strategies to free five distortion coefficients; it is not evidence of halving EXStar's actual error. None measures physical scanner accuracy.

One mathematical correction to earlier reviews matters: **D-optimal ranking is invariant under a fixed nonsingular linear rescaling of parameters**. If `theta'=T theta`, then `log det(C')=log det(C)+2 log|det T|`; the same constant applies to every candidate. Mixed units alone do not invalidate D-optimal selection. Raw A/E criteria are scale-sensitive; D-optimality can still optimize the wrong task or behave differently under nonlinear reparameterization. Prefer task-space A-optimality because its loss represents reconstruction variance, not because a fixed units change reverses D-optimal rankings [S17].

Retain five-term Brown–Conrady compatibility with staged release and weak justified priors. The local aggressive-view experiment found **0.600 mm** for reduced `k1,k2` versus **0.205 mm** for five terms; model simplification is not automatically safer. Conversely, releasing everything with weak coverage overfits. Inspect held-out residuals, distortion monotonicity, split-half stability, image-radius/tilt trends, and physically meaningful parameter sensitivity. Keep information used for planning distinct from empirically calibrated estimator covariance. Fixed-scale robust estimation handles gross errors; it cannot repair coherent bias [S18], [S19].

## Prioritized adoption and rejection decisions

Effort estimates are engineering judgments for one experienced developer, excluding hardware scheduling: small = 1–3 days; medium = roughly 1–2 weeks; large = multiple weeks. They are not measured delivery commitments.

1. **P0 — retain validation boundaries and resolve scale externally.** Preserve current factory/candidate/validated lifecycle, rollback, refusal states, and serial/board provenance. Plan independent calibrated length and flatness checks across distance and field position. The unresolved ~1% pitch/factory scale difference dominates subpixel optimization; reprojection cannot reveal uniform scale error. Impact: turns internal consistency into a testable dimensional claim; magnitude unknown. Effort: medium plus access to reference artifacts. STATUS's synthetic success does not remove this requirement.

2. **P1 — measure the current detector path before rewriting it.** Replay fixed corpora and report accepted/rejected identity accuracy, p50/p95 triplet latency, guidance freshness, and total time to validated profile. Retain current ROI/reacquisition and two-stage localization; port only dominant patch operations to optimized OpenCV/C++ with reusable buffers. Historical detection/solve ratio is ~33:1. Even eliminating the 0.23 s solve saves only ~3% of their combined 7.78 s; detection offers the larger ceiling. Target a measured 2× detector reduction as an experiment, not a forecast. Effort: medium.

3. **P1 — make exposure supervision reproducible.** Audit existing settings/logging, add missing settle/freeze semantics and per-camera quality evidence, then evaluate later on controlled recordings or authorized hardware. Expected impact: fewer rejected/saturated views; no defensible percentage yet. Effort: small to medium once controls are proven. Do not change undocumented illumination timing.

4. **P1 — validate the existing guidance UX with operators.** Retain one actionable movement instruction, clear hold/captured/rejected feedback, distance and coverage cues, and an explicit final evaluation. Keep numerical diagnostics secondary. Measure completion time, repeated attempts, ambiguous-label rejects, and held-out quality; compare the current wizard with its fixed-plan replay baseline. The reported 33-view/80-second synthetic completion is a rehearsal result, not human usability evidence. Effort: medium.

5. **P2 — keep analytic LM as default; maintain an independent check.** Existing vendor measurements: finite-difference engine 6.11 s, analytic SciPy 1.03 s, LM 0.23 s, native reference 0.25 s. Later covariance-inclusive measurements differ, so compare identical settings and thread counts. Preserve SciPy analytic verification and meaningful derivative checks. The native reference's documented tangential-derivative and fixed-distortion covariance defects exclude it as the preferred production path. Effort: small maintenance; no new speed claim.

6. **P2 — prepare a Ceres migration only behind a parity gate.** First resolve scalar/block robust-loss semantics, free masks, prior treatment, gauge, units, derivatives, and covariance interpretation. Then benchmark identical observations/start points, fixed and free distortion, outliers, and degenerate views. Require equal validated behavior and a material whole-workflow benefit before replacement. Effort: medium to large; gain currently unknown. Eigen/header availability is a build concern, not evidence against Ceres algorithms.

7. **P3 — defer GPU calibration and wholesale dependency changes.** Bound CPU worker pools and avoid concurrent BLAS/OpenCV oversubscription; existing evaluations favor one BLAS thread for these small solves. GPU preprocessing is justified only if complete-path timing, including upload/download and synchronization, improves. Full-resolution 8-bit triplets contain about **3.93 MB**; at the logged 10 Hz they consume about **39.3 MB/s** before overhead. Compute acceleration cannot raise the USB2 transport ceiling. Effort: small CPU profiling; GPU work large and conditional.

## What not to copy

- **Marketing guarantees:** “metrology grade,” “real-time calibration,” “AI,” and “insensitive to vibration” do not specify an algorithm, operating envelope, or uncertainty budget. Do not infer vendor GPU use, Ceres use, NBV use, or absolute performance from those labels.
- **Foreign hardware techniques:** Photoneo pattern repetition/ambient suppression, large-volume external tracking, laser-line correction, and multi-scale-bar workflows depend on different optics and timing. Their principles can inform validation; their commands and throughput claims do not transfer to a three-camera USB2 scanner.
- **Target replacement by convenience:** AprilGrid/ChArUco changes point geometry and the factory reference. Preserve the supplied plate and its proven visible identity witnesses. Do not accept an ambiguous partial lattice merely because an optimizer converges.
- **Excess optimization machinery:** moving-target continuous-time/IMU models, rolling-shutter compensation without readout evidence, massive sparse landmark BA, and GPU optimization are not justified for stationary board dwells with a small fixed rig.
- **Success by residual alone:** training RMS, a green UI, a covariance threshold, agreement with factory CCF, or a simulated plane cannot independently establish metric accuracy. Likewise, a fixed image count cannot certify observability, and a shorter calibration cannot excuse weak edge/tilt coverage.

## Bibliography

Vendor documentation establishes only the described interface or published explanation. Links were reviewed on 2026-11-12; published studies support their own tested configurations.

- **S1.** SHINING 3D, [EXStar 1.2.2: Quick Calibration][S1].
- **S2.** Artec, [Studio 19: Scanner Calibration and Correction][S2].
- **S3.** Creaform, [Dynamic Referencing in 3D Optical Metrology][S3], technical white paper.
- **S4.** ZEISS, [TRITOP: Optical Photogrammetry System][S4], 2026 brochure.
- **S5.** Photoneo, [PhoXi Control manual: capturing parameters][S5].
- **S6.** Basler, [Exposure Auto][S6], product documentation.
- **S7.** MVTec HALCON 24.11, [find_calib_object][S7a] and [calibrate_cameras][S7].
- **S8.** OpenCV, [Camera Calibration and 3D Reconstruction][S8]; [parallel/optimization controls][S8b].
- **S9.** ETH Zürich ASL, [Kalibr: Multiple Camera Calibration][S9]; Furgale, Rehder, Siegwart, *Unified Temporal and Spatial Calibration for Multi-Sensor Systems*, IROS 2013, [DOI][S9b].
- **S10.** Heikkilä, *Geometric Camera Calibration Using Circular Control Points*, IEEE TPAMI 22(10), 2000, [DOI][S10].
- **S11.** Richardson, Strom, Olson, *AprilCal: Assisted and Repeatable Camera Calibration*, IROS 2013, [paper][S11].
- **S12.** Triggs, McLauchlan, Hartley, Fitzgibbon, *Bundle Adjustment—A Modern Synthesis*, Vision Algorithms, LNCS 1883, 2000, [paper][S12].
- **S13–S14.** Ceres Solver, [Solving Non-linear Least Squares][S13] and [Modeling Non-linear Least Squares][S14].
- **S15.** Eigen, [Linear Algebra and Decompositions][S15].
- **S16.** Rojtberg, Kuijper, *Efficient Pose Selection for Interactive Camera Calibration*, ISMAR 2018, [conference record][S16].
- **S17.** Kiefer, *Optimum Experimental Designs*, JRSS B 21(2), 1959, [DOI][S17].
- **S18.** Huber, *Robust Estimation of a Location Parameter*, Annals of Mathematical Statistics 35(1), 1964, [DOI][S18].
- **S19.** Fischler, Bolles, *Random Sample Consensus*, Communications of the ACM 24(6), 1981, [DOI][S19].

[S1]: https://docs.shining3d.com/exstar/1.2.2/en-us/quickcalib/
[S2]: https://docs.artec3d.com/as/19/en/calibrate.html
[S3]: https://www.creaform3d.com/sites/default/files/assets/technological-fundamentals/wp_dynamic-referencing-in-3d-optical-metrology.pdf
[S4]: https://asset-downloads.zeiss.com/catalogs/download/iqs2/d24057ca-06b4-4a0f-9608-bf7b39f7b924/2026_zeiss_tritop_brochure_en.pdf
[S5]: https://docs.photoneo.com/docs/PXC/PXC_manual.html
[S6]: https://docs.baslerweb.com/exposure-auto
[S7]: https://www.mvtec.com/doc/halcon/2411/en/calibrate_cameras.html
[S7a]: https://www.mvtec.com/doc/halcon/2411/en/find_calib_object.html
[S8]: https://docs.opencv.org/4.x/d9/d0c/group__calib3d.html
[S8b]: https://docs.opencv.org/4.x/db/de0/group__core__utils.html
[S9]: https://github.com/ethz-asl/kalibr/wiki/Multiple-camera-calibration
[S9b]: https://doi.org/10.1109/IROS.2013.6696514
[S10]: https://doi.org/10.1109/34.879788
[S11]: https://april.eecs.umich.edu/pdfs/richardson2013iros.pdf
[S12]: https://perception.inrialpes.fr/Publications/2000/TMHF00/Triggs-va99.pdf
[S13]: https://ceres-solver.readthedocs.io/latest/nnls_solving.html
[S14]: https://ceres-solver.readthedocs.io/latest/nnls_modeling.html
[S15]: https://eigen.tuxfamily.org/dox/group__TutorialLinearAlgebra.html
[S16]: https://ismar2018.ismar.net/papers/ismar2018_pcs_paper_1011.html
[S17]: https://doi.org/10.1111/j.2517-6161.1959.tb00338.x
[S18]: https://doi.org/10.1214/aoms/1177703732
[S19]: https://doi.org/10.1145/358669.358692
