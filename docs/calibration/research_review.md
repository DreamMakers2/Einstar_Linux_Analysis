# Calibration literature and practice review — adaptive 3-camera calibration for the Einstar plate

Status: 2026-11-10. Scope: what the published record establishes, what it does not, and what that implies
for replacing the vendor's fixed 5-orientation × 5-distance workflow. Every citation was resolved against a
DOI or publisher/standards record; our own inferences are marked **[INFERRED]**, unverified items **[UNKNOWN]**.
Numbers produced here come from `ecc_sim.py` in this directory.

Two results up front, because they change the design:

* **The plate pitch is not observable from reprojection error.** Scaling every board coordinate and board
  translation by *s* leaves all projections identical, so 28.00 mm and 28.28 mm plates produce identical
  residuals, covariance and "accuracy". Any absolute-accuracy claim is a claim about the plate, not the solver;
  the vendor-vs-replacement 1 % scale difference needs an external length to settle.
* **Circular-marker eccentricity bias is real but small for *this* plate.** The closed form derived in §6
  (Δ ≈ *f* (*R*/*d*)²·sinθ/cos³θ) is correct, but its magnitude depends on the actual hole radius. The
  detector benchmark measured the shipped plate's marker radius as **1.77 mm** (not the 7 mm used as a
  placeholder here), so at 240–610 mm the effect is **0.004–0.016 px**, i.e. below the localisation noise;
  switching the perspective correction on changes the real-corpus cross-camera rigid residual by
  +0.0008 mm [−0.0007, +0.0025]. It becomes first-order only for much larger markers or holes (the
  synthetic r = 24–40 px stress cases in `localization_evaluation.md` §3.3, where the correction removes a
  0.39–1.8 px bias). The correction is kept because it is exact and costs ~0.02 ms/marker, not because the
  shipped plate needs it. This supersedes the earlier placeholder calculation below.

---

## 1. Planar-target calibration foundations

**Zhang (2000)** is the reference formulation: one homography per view, two linear constraints per homography
on the image of the absolute conic, closed-form initialisation from ≥3 views, then maximum-likelihood
refinement of intrinsics, distortion and per-view poses. OpenCV's `calibrateCamera` implements this structure
and `stereoCalibrate` adds a second camera's relative pose [OpenCV]. Both are mature engineering practice.

The distortion model is **Brown–Conrady**: a radial polynomial in *r* plus a decentering (tangential) term
from non-collinear lens elements [Brown 1966]. OpenCV adds a rational denominator (`k4..k6`), thin-prism
(`s1..s4`) and tilted-sensor terms, and its documentation is explicit that radial distortion must be monotone
and the map bijective, that the optimiser does **not** enforce this, and that "a failed estimation result may
look deceptively good near the image center but will work poorly" [OpenCV]. That sentence is the most useful
warning in the calibration literature: low residuals near the centre are not evidence of a good model.

Limits: the model is a 2-D post-projection polynomial, not a physical lens model (no field curvature,
astigmatism or entrance-pupil variation). Over a narrow radius range `k1,k2,k3` are strongly collinear, so
high-order terms are identifiable only when markers reach the image corners, and extrapolation beyond the
observed radius is unconstrained.

**Degeneracy.** The classical analysis is Sturm & Maybank (1999); Rojtberg & Kuijper (2018) give a usable
engineering summary.

| configuration | becomes unidentifiable | monitor |
|---|---|---|
| every pose fronto-parallel to the image plane | focal length vs. translation along the optical axis | profile information on `fx,fy`; require ≥1 view with \|tilt\| > 10° per axis |
| board planes all parallel to one image axis | the principal-point coordinate along that axis | separate information on `cx`, `cy`; tilt about both axes, both signs |
| all poses in one plane (pure in-plane translation) | effectively single-plane; focal length and distortion weak | rank of the stacked homography normal equations |
| two planes mirrored about an image axis ("reflection constraint") | sign ambiguity in pose / principal point | reject near-reflections about horizontal or vertical lines |

The vendor plan (0°, x∓18°, y∓18°, five distance bands) satisfies the both-axes rule only just, and
`docs/calibration/model.md` records that the tilted groups carry `angle valid="false"` — the *actual* tilt is
not gated and may be smaller than nominal, so the conditioning is weaker than 18° suggests. **[INFERRED]**

**Gauge and scale.** Fixing the left camera to identity removes the 6-DoF pose gauge; scale is fixed *only*
by the board coordinates. If pitch is free the problem is similarity-invariant and scale unobservable — the
standard bundle-adjustment gauge argument [Triggs et al. 2000; Hartley & Zisserman 2004, ch. 18]. At 500 mm,
1 % is 5 mm.

## 2. Multi-camera rig calibration

**Kalibr** is the strongest work on rigid multi-sensor rigs. Furgale, Rehder & Siegwart (2013) formulate
calibration as batch MAP over a *continuous-time* trajectory in temporal basis functions, jointly estimating
intrinsics, camera–camera extrinsics, camera–IMU extrinsics and inter-sensor time offsets; Rehder, Siegwart &
Furgale (2016) generalise it to arbitrary spatiotemporal systems. The target is an AprilGrid of uniquely
identified tags, so partial occlusion still yields correspondences.

| transfers to a 3-camera rig, no IMU, no motion | does not transfer |
|---|---|
| batch MAP with per-view poses eliminated by Schur complement (confirms the *marginal*, not conditional, rig covariance) | continuous-time trajectory: our observations are stationary dwells, so the spline collapses to one pose per observation — our discrete form is the correct special case |
| time offsets as explicit parameters (only if streams are not hardware-synchronised) | IMU terms |
| self-identifying targets: explains why AprilCal/Kalibr tolerate partial views, which our plate cannot | the implicit requirement of *motion* during capture, which conflicts with dwell/blur gating |

**OpenCV** has per-camera `calibrateCamera` and pairwise `stereoCalibrate` but **no joint N-camera bundle
adjustment in 4.x** — we checked the 4.15 `calib3d.hpp` and there is no `calibrateMultiview`. Three-camera rigs
are therefore usually chained pairwise (L→R, R→T), which propagates and double-counts the shared camera's
error and cannot represent the correlation between the two extrinsics. For a 3-camera rig the joint solve is
not optional. Li, Heng, Koser & Pollefeys (2013) show the industrial alternative: a feature-descriptor target
and a toolbox built for rigs with partial overlap — the situation for a 2-IR + 1-RGB head.

**Time synchronisation and rolling shutter.** With a static scene and rig, a constant per-camera offset is
unobservable; offsets matter only through *motion during exposure*. Li & Mourikis (2014) show how to estimate
camera–IMU offsets online; the lesson is that an unestimated offset is absorbed into extrinsics and reappears
as pose-dependent bias. Two risks: if the RGB sensor is rolling-shutter (plausible, **[UNKNOWN]** for this
device), a moving board produces skew that a global-shutter model cannot represent, and recovering it needs a
continuous-time readout model [Oth et al. 2013]; and auto-exposure or auto-focus changes between views
silently change intrinsics. The sufficient mitigation is already in the design: one observation = one frame
set acquired while stationary, with a stillness gate — Rojtberg & Kuijper (2018) use a 1.5 px mean corner
motion threshold for exactly this reason.

## 3. Active and next-best-view calibration

**AprilCal (Richardson, Strom & Olson, 2013)** is the canonical interactive system and the closest published
ancestor of what we are building:

* target: a 5×7 mosaic of AprilTags, so detection survives partial occlusion;
* it **suggests where to put the target next**, using two scores — a focal-length *stability* score for
  bootstrapping, then **Max ERE**, the maximum expected reprojection error over sampled target points computed
  from the marginal posterior over model parameters with target extrinsics marginalised out;
* candidates are scored by a **hypothetical re-solve including each candidate**, over ~60 poses uniformly
  distributed in the field of view;
* it stops when Max ERE falls below a user-specified pixel threshold.

Reported (648×482, medium-distortion lens, 16 participants, mostly novices): mean test reprojection error
**0.229 px vs 0.728 px for OpenCV**, max test error **1.65 px vs 38.6 px**, focal-length standard deviation
**1.2 px vs 9.0 px**, typically **6–8 images**; Max ERE was validated against the 99.5th percentile of an
independent 60+ image set.

Why it works: it closes the loop between what has been observed and what is still uncertain, and states the
stopping rule in application units (pixels). Limitations: the candidate pool is uniform in the image and does
not avoid the Sturm–Maybank singularities; scoring costs a re-solve per candidate; it is single-camera; and
Max ERE is a **2-D image-space** criterion, not a 3-D volume criterion.

**Rojtberg & Kuijper (2018, ISMAR)** fixes several of those: poses are *generated analytically* from the
currently most-uncertain parameter (split into pinhole and distortion groups), degenerate configurations are
excluded by explicit rules (not parallel to the image plane or to an image axis, reflection constraint, 22.5°
roll offset, 5 % shift for the principal point), the criterion is the **index of dispersion** σ²/C of the worst
parameter, and convergence is a variance-reduction ratio test. Reported: **0.518 px with 9.4 frames**, vs
OpenCV 1.465 px / 10 frames and AprilCal 0.815 px / 13.4 frames — 30 % fewer frames than AprilCal for a 36 %
lower error. Note the inversion in their table: AprilCal had the *highest* training residual (1.54 px) but a
*lower* test error than OpenCV (0.345 px). Residual and accuracy are different quantities and can rank methods
in opposite orders.

**Online vs offline.** AprilCal and Rojtberg are true *online next-best-view*: the decision is made at capture
time from the current state. *Offline subset selection* — choosing k of N already-captured views — is a
different problem (Rojtberg's "compactness" experiment; greedy D-optimal elimination). We need both, in this
order: online guidance so the operator does not waste time, offline pruning to keep the final solve small and
well-conditioned. Conflating them is a common design error.

Recent work has moved to robot hand-eye calibration, where the objective is end-effector accuracy:
Zhang, Xi, Huang & Zheng (2023) do accuracy-driven online NBV for hand-eye. We did not find a published system
doing online NBV for a **multi-camera metrology rig against a 3-D volume objective**. That is a genuine gap,
not a solved problem we are re-implementing. **[INFERRED]** — absence of evidence in our search, not proof of
absence.

The other relevant strand is **information-gain view planning** for reconstruction. Kriegel, Rink, Bodenmüller &
Suppa (2015) plan the next best scan by predicting the information gain over the *surface* of an unknown object,
with the same skeleton we need — candidate set, predicted utility, greedy selection, stop when the predicted
gain falls below cost. The difference is the utility: surface coverage/entropy there, 3-D reconstruction
uncertainty over a fixed working volume here. The skeleton transfers; the objective must be replaced.

## 4. Optimal experimental design

| criterion | objective | property | problem here |
|---|---|---|---|
| **A-optimal** | min tr Σ | average variance, cheap | units must be normalised (px vs mm vs rad) |
| **D-optimal** | max det M | reparameterisation-invariant, confidence-ellipsoid volume | determinant of a mixed-unit matrix is not physically meaningful |
| **E-optimal** | max λ_min(M) | worst-direction guarantee | conservative, ignores the task |
| **T-optimal / target-oriented** | min tr(W Σ) | encodes what you care about | W must be derived from the task |

Kiefer (1959) is the origin; Fedorov & Leonov (2013) and Chaloner & Verdinelli (1995) cover the nonlinear and
Bayesian extensions. Because the model is nonlinear the information matrix depends on the unknown parameters,
so the design must be *sequential/local*: re-linearise at the current estimate, pick the next point greedily.
That is exactly the Schur-complement step in `calib/adaptive.py`, and greedy sequential design is
theoretically respectable.

Two criticisms that matter:

1. **Mixed units.** D-optimality on `[fx,fy,cx,cy,k1,k2,k3,p1,p2,rvec,t]` mixes pixels, radians and
   millimetres; `det Σ` changes if focal length is expressed in mm. Rojtberg's σ²/C is a pragmatic
   normalisation; a principled alternative is to whiten by each parameter's physical scale.
2. **Wrong estimand.** Minimising parameter covariance is not minimising reconstruction error. The map to 3-D
   uncertainty at a probe point X is `Σ_X = J_X Σ_θ J_Xᵀ` (first-order propagation [JCGM 100:2008]; the same
   backward transport is used by Rojtberg). A task-space criterion is e.g. `max_X tr Σ_X` over a grid spanning
   200–600 mm. A design can be D-optimal for `fx` while leaving depth error at 600 mm large, because depth
   sensitivity scales as `Z²/(f·b)` with a fixed baseline.

Closest prior work: AprilCal's Max ERE is task-space, but 2-D pixels, per camera, not 3-D millimetres over a
volume. Strobl & Hirzinger (2006) do OED over robot poses for hand-eye calibration, minimising parameter
covariance. **Task-space OED for multi-camera calibration against a volume objective appears to be
unpublished.** **[INFERRED]**

## 5. Robust estimation and outlier handling

Huber (1964) gives the M-estimator with a quadratic-then-linear loss; Cauchy and Tukey biweight are the common
redescending variants, solved by IRLS. RANSAC (Fischler & Bolles 1981) is right for *gross, sparse* errors — a
mis-assigned marker, a spurious blob, a half-occluded hole — because it needs no good initialisation and
tolerates an arbitrary inlier fraction. It is the wrong tool for *small, dense, systematic* errors, which is
what eccentricity bias is. (The numbers in this section use the 7 mm placeholder radius; the measured
shipped-plate radius is 1.77 mm, which scales every bias in this section by (1.77/7)² ≈ 0.064.)

Three points the codebase must not get wrong:

* **A robust loss changes the covariance.** For an M-estimator the asymptotic covariance is `A⁻¹BA⁻¹` with
  `A = Σ ψ'(r)`, `B = Σ ψ(r)ψ(r)ᵀ`, not `(JᵀWJ)⁻¹`; reporting the Gauss–Newton covariance with a Huber loss
  understates uncertainty. The sandwich form is the same idea as heteroscedasticity-consistent covariance in
  econometrics (White 1980). **[INFERRED]** for our implementation; the formula is standard.
* **Adaptive robust scale hides model bias.** If the scale is estimated from the data and a redescending loss
  is used, a *deterministic* pose-dependent error (bias growing as `tanθ·sec²θ`) is reclassified as
  "outliers". The fit then downweights exactly the high-tilt views carrying distortion information, drifts
  toward fronto-parallel poses, and reports a *smaller* covariance because residuals got smaller. This is not
  hypothetical: at 200 mm / 45° the bias is 2.8 px, larger than any sane RANSAC threshold. **[INFERRED]**
* **Diagnose before downweighting.** Plot residuals against tilt, image radius, marker intensity and marker
  area *before* enabling any robust loss. A trend against tilt is a model error, not an outlier population.

Recommended posture: fixed (not adaptively tuned) robust scale, Huber rather than Tukey for the final solve
(Tukey's hard rejection is too aggressive with 41 points per view), and always report the non-robust solution
and the inlier/outlier counts alongside.

## 6. Circular-marker localisation accuracy

Four families, increasing in correctness:

1. **Intensity-weighted centroid.** Simple and robust, but biased by asymmetric illumination,
   saturation/blooming, partial visibility, and — unavoidably — perspective.
2. **Edge or gradient fitting.** Sub-pixel edge points then a circle/ellipse fit; better SNR, no threshold
   bias, but it still fits the *projected* curve, so perspective bias remains. Ouellet & Hébert (2007) fit
   ellipses directly from image gradients without contour extraction.
3. **Ellipse fitting** (Fitzgibbon, Pilu & Fisher 1999). Statistically efficient for the ellipse parameters —
   but *the centre of the projected ellipse is not the projection of the circle centre*. Heikkilä (2000)
   derives the correction and builds a calibration method on circular control points; Ahn, Warnecke &
   Kotowski (1999) give the photogrammetric formulation and prevention; Mallon & Whelan (2007) compare
   patterns and detectors and quantify the bias; Lenz & Fritsch (1990) is the classic treatment of CCD
   videometry accuracy including blooming and partial coverage.
4. **Conic back-projection.** Fit the ellipse, back-project the cone through the camera centre, intersect with
   the known board plane, and take the centre of the resulting circle (choose between the two solutions by the
   known marker radius). Unbiased; this is what "perspective correction of circular fiducials" means. It needs
   the plane pose, hence an iterative scheme: calibrate → correct control points → recalibrate [Heikkilä 2000].
   Liebold & Maas (2024) is a recent systematic comparison and the best current entry point.

**Magnitude.** We computed the offset between the projected ellipse centre (OpenCV `fitEllipse` on 2000
boundary points) and the true projection of the circle centre, for the vendor's `fx = 1159.1 px` (read from
`LeftCCF.txt`) and a 7 mm radius. The area centroid of the filled projection agrees with the ellipse centre to
within ~0.1 px, so both localisers share the same bias:

| distance | 15° | 30° | 45° | 60° |
|---:|---:|---:|---:|---:|
| 200 mm | 0.41 | **1.09** | **2.84** | **9.87** |
| 300 mm | 0.18 | 0.49 | 1.26 | 4.38 |
| 400 mm | 0.10 | 0.27 | 0.71 | 2.46 |
| 600 mm | 0.05 | 0.12 | 0.32 | 1.09 |

*(pixels, along the tilt direction; `ecc_sim.py`. Closed form `Δ ≈ f (R/d)² sinθ/cos³θ` reproduces these to
≲5 % for θ ≤ 60°, R/d ≤ 0.05; bias scales as R², so halving marker radius halves it.)*

At the vendor's 18° the bias is ~0.1–0.3 px over 240–560 mm — comparable to detector noise and easily absorbed
into the reported residual. At the 30–45° an adaptive system wants for distortion and principal-point
conditioning it is 2–10× the 0.25–0.3 px localisation floor, and it is *systematic in the same direction as the tilt*, so it does not
average out; it aliases into distortion and extrinsics. **This is the largest model error in the current
design and it is invisible to every metric currently reported.** **[INFERRED]** — confirm with the
tilt-stratified residual test of §10.2.

Blooming and partial visibility: saturated pixels flatten the intensity profile and pull centroids toward the
saturated side — keep peak intensity below ~90 % of full well, or use only the monotone edge gradient. Half-seen
holes lose edge support asymmetrically; drop the marker rather than keep a half-observed one. **[INFERRED]**

Target dependence matters: AprilCal and Kalibr localise *tag corners* by intersecting lines fitted to
black/white boundaries (AprilTag's line fit plus quad intersection [Olson 2011]). Corners are not circles and
have no eccentricity bias, so AprilCal's numbers do **not** transfer to a circular-hole plate without explicit
correction.

## 7. Metrology verification practice

**VDI/VDE 2634 Part 2** (2002; area-scanning optical systems) specifies tests on calibrated artefacts: probing error
on a sphere (form and size), **sphere-spacing error**, and **flatness error**. **Part 3** extends this to
multiple-view systems and is what scanner datasheets quote sphere-spacing error against. **ISO 10360-13:2021**
is the corresponding acceptance/reverification standard for optical CMMs. **ISO 5725-1** defines trueness vs
precision and the repeatability/reproducibility limits *r* and *R*. **JCGM 100:2008 (GUM)** supplies the
uncertainty-propagation framework any honest statement should follow.

| claim | requires | available today? |
|---|---|---|
| "the model fits the data to X px" | residuals | yes — a *fit* statement, not accuracy |
| "the estimator is statistically consistent" | NEES/χ² over Monte-Carlo with known truth | yes (simulation only) |
| "the calibration is repeatable" | repeated sessions, ISO 5725 ANOVA | yes, with re-captures |
| "3-D reconstruction is accurate to Y µm" | an artefact with traceable geometry | **no** |
| "scale is correct" | an independent length standard | **no** — the plate cannot check itself |

Three reasons the last two cannot be shortcut. (i) **Scale invariance** (top of report): a pitch error is
exactly unobservable. (ii) **Reprojection error is not accuracy**: Rojtberg & Kuijper's table has AprilCal at
1.54 px training residual beating OpenCV at 0.345 px on test error, and OpenCV warns that a failed calibration
looks good near the centre. (iii) **Simulation is circular**: simulator and estimator share the camera model,
so agreement validates the *implementation*, not the *model*. Simulation can falsify a bug, a wrong Jacobian or
a wrong covariance; it cannot confirm physical accuracy.

## 8. Maturity classification

| method / tool | status | relevance to Einstar | risk |
|---|---|---|---|
| Zhang (2000); OpenCV `calibrateCamera` | **established practice** | initialisation, cross-check; the model the CCF uses | low; known pose-choice sensitivity |
| Brown–Conrady / OpenCV 5- and 8-parameter distortion | **established**, documented limits | matches the vendor CCF exactly | medium: collinear high-order terms; non-monotone solutions possible |
| Joint multi-camera BA (Kalibr-style batch MAP, Schur marginal) | **established** in robotics, **emerging** in industrial metrology | the core of the replacement | low if the marginal (not conditional) covariance is used |
| Kalibr continuous-time / camera–IMU / time-offset machinery | **established** where motion + IMU exist | only the batch-MAP structure and time-offset idea transfer | low if not over-applied |
| AprilCal online NBV | **emerging, validated** (16-person study; research prototype) | direct ancestor of the guidance loop | medium: single-camera, tag-based, expensive scoring |
| Rojtberg & Kuijper analytic pose generation | **research**, one controlled study | best available pose-selection and degeneracy rules | low if re-validated |
| Online NBV for hand-eye (2023) | **research** | template for accuracy-driven NBV | medium: different objective |
| Information-gain NBV for surface reconstruction | **research** (validated on reconstruction tasks) | the candidate/utility/greedy skeleton | medium: objective is surface gain, not 3-D volume uncertainty |
| A/D/E/T-optimality; Bayesian OED | **established theory**, **research** here | the selection engine | medium: mixed units, wrong estimand |
| Task-space OED over a 3-D volume | **research-only / unpublished** | what the brief needs | high: no prior art; validate from first principles |
| Huber / Cauchy / Tukey + IRLS | **established** | gross-error handling | medium: sandwich covariance; bias-hiding |
| RANSAC | **established** | homography/pose hypotheses, gross outliers | low for intended use; wrong tool for bias |
| Ellipse fitting (Fitzgibbon; Ouellet) | **established** | marker localisation precision | low; does not remove perspective bias |
| Conic back-projection (Heikkilä; Ahn) | **established in photogrammetry**, rare in CV toolboxes | the correct fix for the §6 bias | medium: needs pose/plane, iterative |
| VDI/VDE 2634-2/-3, ISO 10360-13, ISO 5725 | **established standards** | the only defensible accuracy basis | low, but needs artefacts we lack |
| NEES / covariance consistency | **established** in tracking, **rare** in calibration | validates uncertainty, not accuracy | low |

## 9. Recommended architecture

Five stages, with an explicit rule for what is online and what is offline.

**Stage 0 — bootstrap (2–3 views).** Focal length only, principal point fixed at the image centre, distortion
zero, as in AprilCal's and Rojtberg's bootstrap. Purpose: a usable projection to drive the guidance overlay.
Do not report these parameters.

**Stage 1 — coverage sweep (constraint satisfaction, not optimisation).** Fill a coverage polytope: board
centre in each cell of a 3×3 (or 4×4) image grid; both tilt axes, both signs, at least one view with
|tilt| > 30°; at least two distances near each end of 200–600 mm; markers at the image periphery at more than
one distance. This makes the problem non-degenerate and guarantees distortion support — exactly what a pure
information-greedy search handles badly, since gain is computed under the current (possibly degenerate)
linearisation.

**Stage 2 — task-space information refinement (online).** Greedy selection over a continuous candidate pool,
scored by predicted **3-D uncertainty at probe points spanning the volume**: `Σ_X = J_X Σ_θ J_Xᵀ`, criterion
`max_X tr Σ_X` (or max eigenvalue for a worst-case guarantee), with `Σ_θ` the *marginal* rig covariance after
Schur-eliminating the candidate pose. Keep D-optimality on whitened parameters as a tie-breaker and degeneracy
monitor, not as the objective.

**Stage 3 — guided continuous acquisition with keyframe capture.** Run pose estimation continuously against
the current calibration and show a live gain/coverage overlay (AprilCal's contribution). Capture only when
(a) the live pose is in the targeted cell, (b) predicted gain exceeds a threshold, (c) the stillness/blur/sync
gate passes. This decouples guidance rate from capture rate and gives the operator something to aim at, which
is the real usability result of AprilCal's study.

**Stage 4 — batch re-solve plus offline pruning.** Re-solve the full joint problem after each accepted view
(warm-started LM; 39 + 6N parameters, a few thousand residuals, milliseconds on CPU — batch is affordable and
better than incremental filtering here). At the end, greedy backward D-optimal elimination drops redundant
views, then re-solve and re-check. Offline pruning must never feed back into online guidance.

**Stage 5 — conservative stop and validation gate.** Stop only when all of: task-space criterion below target;
max predicted 3-D uncertainty over the volume below budget; split-half parameter agreement within covariance;
held-out residual consistent with training residual; no residual-vs-tilt trend. If any check fails, ask for
more views rather than declare success.

Staged coarse-to-fine is also the right *parameterisation* schedule: hold `cx,cy` until both-axis tilt
coverage exists, hold `k3` until periphery coverage at two distances exists, then release. Releasing parameters
before their supporting data exists is how "converged" calibrations end up wrong.

## 10. Validation protocol

**10.1 Mathematical validation (simulation, truth known).** Monte-Carlo over parameter draws and noise seeds;
NEES against the *marginal* covariance, checked against its χ² interval [Bar-Shalom, Li & Kirubarajan 2002];
estimator efficiency (empirical vs predicted covariance); Jacobian correctness by finite differences; gauge
handling (perturb the left-camera pose — nothing should change); **scale invariance** (multiply board
coordinates by 1.01 — residuals must be identical to machine precision; if not, the code leaks scale).
Acceptance: NEES inside the 95 % interval for ≥95 % of trials; empirical/predicted σ ratio in [0.9, 1.1].

**10.2 Real-image consistency (no truth required).** Split-half: partition views, calibrate each half, compare
parameters in units of predicted σ (χ² with 39 DoF). Held-out views: calibrate on 80 %, evaluate reprojection
on the remaining 20 % with the board pose free; held-out RMS ≤ 1.2× training RMS. **Tilt-stratified
residuals**: regress the residual component along the tilt direction on `tanθ·sec²θ`; the slope must be zero
within uncertainty. This is the direct test for the §6 bias and the most valuable new check. Session repeatability
per ISO 5725-1 (*r*, *R*). Acceptance: split-half χ² inside its 95 % interval; held-out ratio ≤ 1.2;
tilt-slope consistent with zero (or a known, corrected bias); *r* ≤ 0.05 px on `fx`.

**10.3 Physical accuracy (artefacts required).** Minimum viable: a calibrated **sphere bar** (two spheres,
certified centre distance) scanned in ≥7 orientations spread through the volume → sphere-spacing error per
VDI/VDE 2634-3; a certified **plane** → flatness error; a step gauge or gauge block for length if available.
Acceptance should be a fraction of the scanner's specified accuracy (e.g. ≤ 1/3 of budget for sphere spacing,
≤ 1/2 for flatness), not invented from residuals. Until an artefact exists the system must print **"accuracy
unverified; scale traceable to the plate pitch only"**, and no document may quote a µm figure. If only one
artefact is affordable, make it the sphere bar: it constrains scale, form and point-to-point distance at once,
and it is what the scanner's own datasheet is specified against.

---

## References

Ahn, S. J., Warnecke, H.-J., & Kotowski, R. (1999). Systematic geometric image measurement errors of circular
object targets: mathematical formulation and prevention. *The Photogrammetric Record*, 16(93), 485–502.
https://doi.org/10.1111/0031-868X.00138

Bar-Shalom, Y., Li, X.-R., & Kirubarajan, T. (2002). *Estimation with Applications to Tracking and
Navigation*. Wiley. https://doi.org/10.1002/0471221279

Brown, D. C. (1966). Decentering distortion of lenses. *Photogrammetric Engineering*, 32(3), 444–462.
(No DOI; origin of the decentering/tangential terms.)

Chaloner, K., & Verdinelli, I. (1995). Bayesian experimental design: a review. *Statistical Science*, 10(3),
273–304. https://doi.org/10.1214/ss/1177009939

Fedorov, V. V., & Leonov, S. L. (2013). *Optimal Design for Nonlinear Response Models*. CRC Press.
https://doi.org/10.1201/b15054

Fischler, M. A., & Bolles, R. C. (1981). Random sample consensus. *Communications of the ACM*, 24(6),
381–395. https://doi.org/10.1145/358669.358692

Fitzgibbon, A., Pilu, M., & Fisher, R. B. (1999). Direct least square fitting of ellipses. *IEEE TPAMI*,
21(5), 476–480. https://doi.org/10.1109/34.765658

Furgale, P., Rehder, J., & Siegwart, R. (2013). Unified temporal and spatial calibration for multi-sensor
systems. *IROS 2013*, 1280–1286. https://doi.org/10.1109/IROS.2013.6696514

Hartley, R., & Zisserman, A. (2004). *Multiple View Geometry in Computer Vision* (2nd ed.). Cambridge
University Press. https://doi.org/10.1017/CBO9780511811685

Heikkilä, J. (2000). Geometric camera calibration using circular control points. *IEEE TPAMI*, 22(10),
1066–1077. https://doi.org/10.1109/34.879788

Huber, P. J. (1964). Robust estimation of a location parameter. *Annals of Mathematical Statistics*, 35(1),
73–101. https://doi.org/10.1214/aoms/1177703732

ISO (2021). *ISO 10360-13:2021 — Geometrical product specifications (GPS): acceptance and reverification tests
for coordinate measuring systems — Part 13: Optical 3D CMS*.

ISO (1994). *ISO 5725-1:1994 — Accuracy (trueness and precision) of measurement methods and results — Part 1:
General principles and definitions*.

JCGM (2008). *Evaluation of measurement data — Guide to the expression of uncertainty in measurement*
(JCGM 100:2008). https://doi.org/10.59161/jcgm100-2008e

Kiefer, J. (1959). Optimum experimental designs. *Journal of the Royal Statistical Society B*, 21(2),
272–304. https://doi.org/10.1111/j.2517-6161.1959.tb00338.x

Kriegel, S., Rink, C., Bodenmüller, T., & Suppa, M. (2015). Efficient next-best-scan planning for autonomous
3D surface reconstruction of unknown objects. *Journal of Real-Time Image Processing*, 10, 611–631.
https://doi.org/10.1007/s11554-013-0386-6

Lenz, R., & Fritsch, D. (1990). Accuracy of videometry with CCD sensors. *ISPRS Journal of Photogrammetry and
Remote Sensing*, 45(2), 90–110. https://doi.org/10.1016/0924-2716(90)90095-S

Li, B., Heng, L., Koser, K., & Pollefeys, M. (2013). A multiple-camera system calibration toolbox using a
feature descriptor-based calibration pattern. *IROS 2013*, 1301–1307.
https://doi.org/10.1109/IROS.2013.6696517

Li, M., & Mourikis, A. I. (2014). Online temporal calibration for camera–IMU systems: theory and algorithms.
*International Journal of Robotics Research*, 33(7), 947–964. https://doi.org/10.1177/0278364913515286

Liebold, F., & Maas, H.-G. (2024). Eccentricity correction methods for circular targets in perspective
projection. *ISPRS Archives*, XLVIII-2/W7-2024, 65–72.
https://doi.org/10.5194/isprs-archives-XLVIII-2-W7-2024-65-2024

Mallon, J., & Whelan, P. F. (2007). Which pattern? Biasing aspects of planar calibration patterns and
detection methods. *Pattern Recognition Letters*, 28(8), 921–930.
https://doi.org/10.1016/j.patrec.2006.12.008

Olson, E. (2011). AprilTag: a robust and flexible visual fiducial system. *ICRA 2011*, 3400–3407.
https://doi.org/10.1109/ICRA.2011.5979561

OpenCV (n.d.). *Camera calibration and 3D reconstruction* (calib3d module documentation, 4.x; accessed
2026-11-10). https://docs.opencv.org/4.x/d9/d0c/group__calib3d.html

Oth, L., Furgale, P., Kneip, L., & Siegwart, R. (2013). Rolling shutter camera calibration. *CVPR 2013*,
1360–1367. https://doi.org/10.1109/CVPR.2013.179

Ouellet, J.-N., & Hébert, P. (2007). A simple operator for very precise estimation of ellipses. *CRV 2007*,
21–28. https://doi.org/10.1109/CRV.2007.8

Rehder, J., Siegwart, R., & Furgale, P. (2016). A general approach to spatiotemporal calibration in multisensor
systems. *IEEE Transactions on Robotics*, 32(2), 383–398. https://doi.org/10.1109/TRO.2016.2529645

Richardson, A., Strom, J., & Olson, E. (2013). AprilCal: assisted and repeatable camera calibration.
*IROS 2013*, 1814–1821. https://doi.org/10.1109/IROS.2013.6696595

Rojtberg, P., & Kuijper, A. (2018). Efficient pose selection for interactive camera calibration. *ISMAR 2018*,
31–36. https://doi.org/10.1109/ISMAR.2018.00026

Strobl, K. H., & Hirzinger, G. (2006). Optimal hand-eye calibration. *IROS 2006*, 4647–4653.
https://doi.org/10.1109/IROS.2006.282250

Sturm, P., & Maybank, S. (1999). On plane-based camera calibration: a general algorithm, singularities,
applications. *CVPR 1999*, 432–437. https://doi.org/10.1109/CVPR.1999.786974

Triggs, B., McLauchlan, P., Hartley, R., & Fitzgibbon, A. (2000). Bundle adjustment — a modern synthesis.
*LNCS* 1883, 298–372. https://doi.org/10.1007/3-540-44480-7_21

VDI/VDE (2002, 2008). *VDI/VDE 2634 — Optical 3-D measuring systems*: Part 2, systems with area scanning
(2002); Part 3, multiple-view systems (2008). Beuth Verlag.

White, H. (1980). A heteroskedasticity-consistent covariance matrix estimator and a direct test for
heteroskedasticity. *Econometrica*, 48(4), 817–838. https://doi.org/10.2307/1912934

Zhang, Z. (2000). A flexible new technique for camera calibration. *IEEE TPAMI*, 22(11), 1330–1334.
https://doi.org/10.1109/34.888718

Zhang, X., Xi, Y., Huang, Z., & Zheng, L. (2023). Active hand-eye calibration via online accuracy-driven
next-best-view selection. *The Visual Computer*, 39, 381–391. https://doi.org/10.1007/s00371-021-02336-7
