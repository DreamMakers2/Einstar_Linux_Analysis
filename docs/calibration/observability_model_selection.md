# Observability, rank/condition and model selection for the Einstar session

Status: analysis of the preserved 297-view session
(`<calibration-session>`, manifest + decisions + `session.json`
copied to `analysis/session03/`).  Everything below is reproducible from the
exact observations the live session used: `session.json["resume"]["views"]`
holds 891 observation groups (297 views x 3 cameras, 34 202 marker points) with
their lattice keys, so no re-detection is involved.

Machine-readable evidence: `analysis/calibration/solver/` (index in
`INDEX.json`).  Code: `calib/solvers.py`, `calib/calibrate.py`,
`tools/wizard/observability.py`; tests in `tests/test_solver_equivalence.py`
and `tests/test_calibration_audit.py`.

## 1. What the diagnostics actually said, and what produced it

| diagnostic | value | emitted by | verdict |
|---|---|---|---|
| `condition` | 9.83e+03 | `tools/wizard/observability.identifiability()["condition_scaled"]`, i.e. the eigenvalue ratio of `D S D`, `D = diag(1/sqrt(S_ii))`, of the 33-parameter marginal rig information | **genuine but not a failure**: the gate threshold is 1e7 (`lifecycle.max_condition_scaled`), so this value never blocked anything |
| `rank` | 33/33 | same function: `identifiability()["rank"] / ["n_free"]`, where `n_free` is the number of *free rig parameters* after the controller removed the six prior-held principal-point coordinates (`_gate_free_mask`) | **misleading**: 33 is the Jacobian column count of the gate's parameter subset, not an observability count.  The cutoff is `lambda > lambda_max * rank_rtol**2` with `rank_rtol = 1e-10`, i.e. a 1e-20 relative eigenvalue, which is nearly always satisfied |
| weak focal lengths | `left.fx` 0.518 px, `right.fx` 0.457, `tex.fx` 0.452 (budget 0.35 px at target `high`) | `observability_gate` -> `marginal_sigmas` -> `rig_information(result)` = `result.marginal_information` | **parameterisation/reporting bug**, not the data (see §2) |
| unknown principal-point uncertainty | none reported | `marginal_sigmas(result, free_mask)`: only *free* parameters get a sigma | **genuine but expected**: `cx`,`cy` are held by a 1.0 px Gaussian prior at the factory value (`controller._prior_spec`), so the model has no measurement of them.  The diagnostics say nothing because the gate has no "held parameter" verdict |
| several unobservable radial-distortion parameters | `left.k2` 0.0099 (ratio 5.8), `left.k3` 0.0197 (5.8), `right.k2` 0.0115 (6.8), `right.k3` 0.0239 (7.1), `tex.k2` 0.0100 (6.3), `tex.k3` 0.0180 (5.9) | same `marginal_sigmas` + `_distortion_effect_px` (coefficient sigma expressed as a pixel displacement at the image corner, budget = 0.35 px) | **genuine**: with all five Brown-Conrady terms free, `k2`/`k3` are unobservable at the image-corner accuracy that matters, on this plate/diversity |
| `freed_parameters` gate failed | 6 unobservable free parameters | `observability_gate` | **genuine and correctly blocking** — the model freed parameters the data cannot constrain |
| `model_sensitivity` gate failed | held-out change 0.018 % (< 10 % required) | `observability.model_sensitivity_analysis` | **conclusion correct, evidence wrong**: the two refits were SciPy TRF with `max_nfev=400` on ~1460 free parameters; SciPy needs 1461 residual evaluations for one finite-difference Jacobian, so it stopped before completing one and compared the *initial guess* with itself.  Recomputed with the analytic LM and properly converged refits (203 s, 2 x 236 views) the held-out change is **-0.014 %** (0.36845 px free vs 0.36840 px fixed): freeing distortion makes prediction marginally *worse*, so the gate's conclusion stands on valid evidence |

Bottom line: `rank 33/33` and `condition 9.83e3` are informational numbers that
never failed; the two failing gates are (a) real — the live model freed
distortion it cannot observe — and (b) the focal-length sigma was inflated by a
covariance-scaling bug.

## 2. The covariance bug that produced "weak focal length"

`calib.calibrate.calibration()` (SciPy path) and `calib.solvers.covariance_from_jacobian()`
(published LM path) both built the *marginal* rig information as the Schur
complement of the Gauss-Newton information
`A = J^T W J`, then `observability.marginal_sigmas` inverted it.

`A` has no residual scale: `A^-1` is the covariance for **unit-variance**
residuals.  The published covariance is either `sigma_MAD^2 A^-1` or the Huber
sandwich `A^-1 B A^-1`, both of which are ~`sigma_px^2` smaller.  Inverting the
bare Schur complement therefore overstates every sigma by about
`1/sigma_px ~ 2.8` (measured: `sigma_px = 0.354`, `f_scale = 0.8`).  The
solvers path additionally used `pinv(A_vv, rcond=1e-12)` on the **squared**
spectrum, a different rank cutoff from the covariance's `1e-10` on singular
values, so the two paths also disagreed in shape.

Measured on the 297-view `free5` fit, with the gate's production free mask:

| quantity | shipped code | fixed code |
|---|---|---|
| `left.fx` 1-sigma | 0.735 px | 0.202 px |
| `left.k2` 1-sigma | 0.0200 | 0.0099 |
| `param_std["left.fx"]` (covariance) | 0.20 px | 0.20 px |
| max relative difference marginal-sigma vs `param_std` | 3.7x | < 1e-4 |

Fix: the marginal information is now the inverse of the rig block of the
**published** covariance (`calib/solvers.py`, `calib/calibrate.py`), so
`marginal_sigmas` and `param_std` are the same estimator by construction.  The
regression tests pin the invariant `inv(C_rig) == marginal_information` and
`sigma_gate == param_std`.

A second, unit-free condition number (`condition_scaled`: the condition number
of the column-normalised Jacobian) is now reported next to the raw one, because
the raw number mixes px, mm, rad and distortion coefficients and has no
absolute meaning.  On the 297-view model: raw 7.6e5, scaled 233-7 500 depending
on the model (see §4).

## 3. Is the reported rank/condition honest?

- **Rank.** `identifiability()["rank"]` uses `rank_rtol=1e-10` on singular
  values, i.e. eigenvalues above `1e-20 * lambda_max`.  With double precision
  and a well-scaled problem that test essentially never fails, which is why
  every model tried here reports full rank (24/24, 27/27, 30/30, 33/33, 39/39)
  *including* the one whose six distortion parameters are unobservable.  The
  number is a numerical-stability check, as its own docstring says, but the GUI
  prints it as "rank n/n free parameters" next to the condition number and it
  reads as an observability claim.  The honest observability numbers are the
  per-parameter verdicts (`counts`), `weak_directions` and the eigen tail.
- **Condition.** `condition_scaled` is unit-free and its 1e7 threshold is
  meaningful; 9.83e3 (reproduced here as 7.2e3-7.5e3 on a slightly different
  optimum) is far below it.  It is not the reason anything failed.
- **Gauge.** The model fixes the left camera to the identity and lets each
  board pose float; the metric scale is the board pitch (28.0 mm).  There is no
  residual gauge freedom in the parameterisation, but two *structural*
  correlations remain and they matter for classification:
  `corr(left.fx, view_tz) = 0.962` and `corr(left.cx, view_tx) = 0.991`,
  because the left camera *is* the gauge: the board pose is expressed in its
  frame.  `_pose_absorption()` downgrades a "constrained" parameter to "weak"
  when `|corr| >= 0.95`, so the gauge camera is systematically penalised for a
  property of the parameterisation, not of the data.  On this session that rule
  did not change any verdict (the focal sigmas were already "weak" by the buggy
  sigma), but it is the next reporting defect in line.
- **Board symmetry.** The 40-point lattice used by the fit is symmetric under
  `(u,v) -> (7-u, 4-v)` and under `v -> 4-v` (180 deg about the board x axis),
  so each *per-view* board pose has a discrete 4-fold ambiguity.  The rig
  parameters are invariant under it (the pose is a nuisance parameter), but the
  recorded `manifest.views[*].pose` and the fitted `result.views` can sit on
  different branches — measured on views 0-4, the recorded pose equals the
  fitted pose composed with `diag(1,-1,-1)` to <0.12 rad.  Anything that
  compares poses across views (tilt/roll coverage, motion gate, triangulation
  in the board frame) must use one branch consistently.

## 4. Model selection (297 accepted views, factory profile)

Every configuration is fitted with the published analytic LM, Huber
`f_scale=0.8`, the controller's principal-point prior where stated, and the
same 297 views; the held-out number is a refit on 4/5 of the views evaluated on
the remaining 1/5 with a free per-view PnP pose (no view in both).

| configuration | free rig params | residual RMS | condition (scaled) | rank | 1-sigma fx | unobservable | held-out RMS* |
|---|---|---|---|---|---|---|---|
| factory distortion held, `cx/cy` prior | 18 | 12.808 (robust scale 0.354) | 1.70e3 | 18/18 | 0.202 px | 0 | 0.47 px |
| `k1` free only, `cx/cy` prior | 21 | 12.808 | 2.20e3 | 21/21 | 0.208 px | 0 | 0.47 px |
| `k1,k2` free, `cx/cy` prior | 24 | 12.808 | 2.7e4 (39-param mask) | 30/30 | - | 0 | 0.47 px |
| all five coefficients free, `cx/cy` prior | 33 | 12.808 | 7.5e3 | 33/33 | 0.202 px | **6** (`k2`,`k3` x3) | 0.47 px |
| all five coefficients free, converged refit, held-out | - | - | - | - | - | - | 0.368 px free vs 0.368 px fixed (-0.014 %) |
| all five coefficients, no prior | 39 | 12.809 | 6.8e4 | 39/39 | 0.202 px | 6 | 0.47 px |
| **chosen**: distortion held, `cx/cy` **hard-fixed at factory**, 4 gross-outlier views culled (293 views) | 18 | 0.651 (view median 0.469, p90 0.953, max 1.93) | 1.80e3 | 18/18 | 0.20 px | 0 | 0.770 +/- 0.019 |

\* the 0.47 px rows are the 5-fold held-out of the unculled 297-view fits; they
are dominated by one bad texture group (view 36, 374 px, §5).  Per camera:
left 0.41, right 0.42, tex 0.74 px (median over views).  The chosen row is the
culled 3-fold held-out (0.766 +/- 0.018 over all cameras; left 0.41-0.42,
right 0.46-0.47, tex 1.11-1.18 — the texture camera is the weak one).

Observations:

1. **Freeing distortion buys nothing measurable.**  The residual RMS moves by
   0.001 px between "factory distortion held" and "all five coefficients free",
   the held-out error does not move at all, and six of the fifteen extra
   parameters are unobservable.  The staged-release rule (>= 10 % held-out
   gain) is right to hold them; the live session's failure is that the policy
   frees distortion at 8 accepted views (`kc_views = 8` in mode `high`) and
   never revisits that decision before `freed_parameters` runs.
2. **`k1` alone is defensible in principle** (0 unobservable, condition 2.2e3,
   sigma_k1 = 1.4e-3 -> 1.6 px effect at the image corner), but it does not
   improve the fit (~0.001 px) and the factory value is already inside its
   1-sigma, so the conservative choice is to hold it.
3. **The principal point cannot be measured from these views.**  With a 1 px
   prior its posterior sigma is 0.44-0.53 px and the split-half spread is
   0.5-0.7 px (ratio ~2, §6); freeing it changes the RMS by 0.001 px but makes
   the ray geometry worse-conditioned (`cx` and the per-view board `tx` are 0.99
   correlated).  **A soft prior is not a fix**: with the production 1 px prior
   the MAP solution moved `left.cx` by -2.02 px (621.896 vs 623.912),
   `right.cx` by -0.40 px and `tex.cy` by -1.49 px, i.e. it optimised the very
   parameter it was supposed to protect, while the reported free-parameter count
   and the identifiability matrix excluded it.  The candidate therefore
   **hard-fixes** `cx/cy` at the factory values for all three cameras (no prior,
   not optimised, `delta = 0.0 px` by construction).  This is justified only
   because the factory principal point is trusted to ~0.5 px; nothing in this
   session measures it independently.

### Does D4 (image corners) matter for the parameters we are willing to free?

Counterfactual with Fisher information at the 297-view solution
(`corner_counterfactual.json`): add 48 synthetic corner/edge views (8 plate
positions x 3 depths x 2 tilts, 0.3 px noise, projected through the factory
rig):

| parameter | sigma without corner views | with corner views | ratio |
|---|---|---|---|
| `left.cx` | 0.738 | 0.421 | **0.57** |
| `left.cy` | 0.619 | 0.455 | 0.74 |
| `left.k1` | 3.2e-3 | 1.4e-3 | **0.44** |
| `left.k2` | 2.0e-2 | 3.5e-3 | **0.18** |
| `left.fx` | 0.798 | 0.858 | 1.08 |

So: corner views are what makes the **principal point and radial distortion**
observable (they cut `cx` by 43 % and `k2` by 82 %), and they do **not** help
the focal length.  For the defensible model — distortion and principal point
held at the factory values, focal lengths and extrinsics free — D4 is *not* a
calibration-observability requirement; it remains a **validation** requirement
(distortion at the field edge, triangulation quality across the field, scale
checks), which is how the coverage gate should describe it.  For any model that
frees `cx/cy` or `k2`, D4 *is* an observability requirement, and the session's
self-reinforcing deadlock (acceptance maximises task-variance gain under the
current model, so corner views look redundant, so D4 never fills) is real.

## 5. Data quality and the culling rule

Per-observation-group residuals of the 297-view fit (`cfg_held_kc.json`,
`e1_held_prior.json`):

- median per-view RMS 0.472 px, p90 1.04 px;
- **view 36 texture group: RMS 374 px** (max 574 px) — a gross detection
  failure, not a model error (left/right of the same view are 0.48 px);
- views 6, 129, 161: 2.0-3.8 px (texture mostly);
- the *reported* training RMS of 12.8 px is entirely due to view 36's 40
  residuals; the robust scale is 0.354 px.

Culling rule (the one `calib.pipeline.calibrate_directory` already uses): drop
whole views whose reprojection RMS exceeds `max(2.0 px, 3 x median view RMS)`.
On this session that removes exactly `[6, 36, 129, 161]` (4 of 297 views, 1.3 %
of groups).  No other observation is touched; no threshold was relaxed.

## 6. Independent validation of the candidate

Candidate artefacts: `analysis/calibration/solver/final/profile.json`
(schema `einstar.recon.profile/1`, round-tripped through
`calib.recon_profile.load`), `final/validation.json` and
`final/final_metrics.json`.  Nothing was written to the session's `candidate/`
directory and nothing was activated.

See `analysis/calibration/solver/final/validation.json`.  The candidate is:
factory distortion held, `cx/cy` held at the factory value with the production
1 px prior, `fx, fy` per camera and the six-DoF right/tex extrinsics free,
per-view poses free (nuisance).

- **held-out views** (3 folds, disjoint, free PnP pose per view):
  left 0.41, right 0.42, tex 0.74 px; all-camera mean over folds reported in
  the JSON.
- **split-half** (3 independent random halves of 148 views, refit each):
  `std(fx_A - fx_B)` = 0.64 / 1.07 / 0.77 px for left/right/tex against a
  predicted `sqrt(sigma_A^2 + sigma_B^2)` of ~0.48 px, i.e. the analytic sigma
  is optimistic by a factor 1.2-2.3 (median ~2).  This is expected: the 297
  accepted views are bursts of a slow-moving plate, not independent samples, so
  the covariance understates the true reproducibility.  Empirical sigma_fx at
  297 views is therefore ~0.3-0.4 px, not 0.20 px.
- **epipolar / relative pose** (independent of the reprojection objective):
  Sampson distance RMS over the IR correspondences is **0.333 px for the fitted
  rig vs 0.465 px for the factory rig** on the same points.  On the colour pair
  the fitted rig gives 0.63 px while the factory colour rig gives **31.3 px** —
  the colour extrinsic in the factory profile cannot explain these images.  The
  fitted rig is better than the profile it started from, on a measure that was
  never optimised.
- **baseline vs factory**: 160.014 mm fitted vs 158.557 mm factory, i.e.
  **+1.457 mm (+0.92 %)**, sigma 0.018 mm — an 80-sigma discrepancy, so it is
  systematic, not noise.  The same session gives an IR relative rotation of
  22.130 deg vs 22.138 deg factory (0.008 deg) and a colour translation of
  122.878 mm vs 125.318 mm (-2.44 mm, the known stale colour extrinsic).
  A 0.94 % scale error is exactly the size of the plate-pitch ambiguity
  (28.0 mm nominal vs 27.72 mm CCF-effective, 1.0 %): the pixel data cannot
  separate "the baseline is 1.4 mm longer than the factory says" from "the
  factory profile was built with a slightly different effective pitch".  This
  is a physical/factory-pitch question that this session cannot settle.
- **absolute scale is unvalidated.**  The metric scale is the plate pitch
  (28.0 mm nominal, 27.72 mm CCF-effective) and no length artefact was
  measured.  A 0.1 % pitch error is 0.45 mm at 450 mm and is completely
  invisible to the pixel covariance.

### Corrected gate output for this candidate

```
counts        : 17 constrained, 1 weak, 0 unobservable
condition     : 1804 (scaled, threshold 1e7)      rank: 18/18 (18 free rig params)
principal pt  : hard-fixed at factory, delta = 0.0 px for all six coordinates
weak          : tex.tz (ratio ~1.07)
gates         : freed_parameters pass, stereo_rotation pass, baseline pass,
                colour n/a (no colour coverage measurement), model_sensitivity PASS
                (distortion held, held-out gain from freeing it -0.01 %)
```

The gate passes on the corrected covariance *and* the corrected
`model_sensitivity` semantics.  Note the weak `tex.tz` (0.064 mm vs 0.06 mm
budget) and the texture camera's held-out residual (1.1-1.2 px vs 0.41-0.47 for
IR): the colour camera is the weakest part of this candidate.  Two interface notes for the owner of the other
files:

1. `tools/wizard/controller._external_observability` still contains a manual
   override that flips `model_sensitivity` to pass whenever the staged release
   held the distortion.  With the gate now judging the candidate's own policy
   in both directions, that override is redundant (it cannot change a passing
   gate; it would only mask an "under-modelled" failure).  It should be removed
   to avoid two sources of truth.
2. `tests/test_wizard_failures.py::test_observability_refuses_overparameterised_distortion`
   asserts `counts["unobservable"] >= 10`, a number produced by the inflated
   sigma.  With the corrected sigma the same scene gives 4 unobservable and 19
   weak (the gate still fails on `freed_parameters`).  The assertion should
   become `unobservable + weak >= 10` (or `unobservable >= 4`); it is a test of
   the old scale, not of the gate's behaviour.

**Honest verdict.**  The calibration is internally consistent and the model is
defensible in structure (parsimonious: 18 free rig parameters, none of them
unobservable; held-out error only 18 % above training error and dominated by
the texture camera).  But the *precision* claim at target `high`
(sigma_fx <= 0.35 px) is not supported by the split-half evidence: the empirical
reproducibility is ~0.3-0.4 px, at or just over the budget.  A candidate at
target `standard` (0.75 px) is defensible; a `high`-target candidate is not, on
this data.

**The candidate is analysis-only and is not defensible for activation** until
(a) the raw/corner-evidence question is resolved (does the field-edge data
exist, and was D4 unsatisfiable by design?) and (b) an independent scale
artefact pins the metric scale, because the +0.94 % baseline discrepancy is
currently indistinguishable from a pitch/scale difference.

## 7. Remaining unknowns

1. **Absolute scale**: plate pitch is nominal/CCF-effective, no artefact
   measurement.  A pitch error of 0.1 % is 0.45 mm at 450 mm and is invisible to
   the pixel covariance.
2. **Factory principal point**: held, not measured.  Needs either corner
   coverage (D4) or an independent measurement (e.g. a target at the field edge,
   or a vendor CCF re-issue) to free it.
3. **Texture camera systematic**: texture residuals are 1.5-4 px on many views
   while IR is 0.4 px, consistent with the known stale colour extrinsic (~18 mm
   per the controller notes).  The colour extrinsic fitted here is data-driven
   but the colour model (distortion, principal point) is factory-held.
4. **View independence**: the effective number of independent views is much
   smaller than 297; the covariance does not model the burst correlation, which
   is why split-half disagrees with the analytic sigma.  A cluster/block
   covariance (per burst) would be the right estimator.
5. **Board-pose branch**: 4-fold discrete ambiguity per view (§3); harmless for
   the rig, but pose-derived coverage/motion numbers must be branch-consistent.


## 8. Files changed and tests

Code (all inside this solver's ownership):

- `calib/solvers.py`: marginal information = inverse of the rig block of the
  published covariance; unit-free `condition_scaled`/`rank_scaled` in the
  diagnostics; `full_free_mask()` + an explicit `free=` mask for per-parameter
  model selection; and a held parameter now also *starts* at the caller's
  `init_cameras` value (`Problem.start(free=...)`).  Without that last change
  OpenCV's initialisation estimated the whole intrinsic block and a "held"
  principal point silently kept the OpenCV value instead of the factory one —
  which is how the soft-prior candidate came to be mislabelled as held.
- `calib/calibrate.py`: same marginal-information correction and the same
  `condition_scaled` diagnostics on the SciPy reference path.
- `tools/wizard/observability.py`: fast, converged analytic-LM refit for
  `model_sensitivity_analysis` (with a `scipy-engine` fallback and an `ok=False`
  when the refit does not converge); the `model_sensitivity` gate now judges
  the candidate's policy in both directions; the gauge camera is no longer
  downgraded by `_pose_absorption`.
- `calib/objective.py`: design covariance/information now come from the stored
  Gauss-Newton normal matrix (`gn_marginal_information`) instead of the
  published-covariance marginal information, so view scoring keeps its Fisher
  meaning.
- `tests/test_solver_equivalence.py`: marginal-information/covariance identity,
  gate sigma == `param_std`, scale-invariant condition number,
  `full_free_mask` semantics, and "a held intrinsic starts at the caller's
  value and does not move" (the defect behind the mislabelled soft-prior
  candidate).
- `tests/test_calibration_audit.py`: the marginal-information identity (updated
  from the old `* sigma_px^2` expectation, which encoded the bug), gate sigma
  provenance, converged model-sensitivity refit, and the four policy branches of
  the model-sensitivity gate.

Test status for the owned files: `test_solver_equivalence.py` 7 passed,
`test_calibration_audit.py` 15 passed, `test_objective.py` 5 passed,
`test_calib_engine.py` + `test_calib_model.py` 28 passed,
`test_wizard_observability.py` 15 passed.  One test outside this ownership,
`tests/test_wizard_failures.py::test_observability_refuses_overparameterised_distortion`,
fails on `unobservable >= 10` because that count was produced by the inflated
sigma; the scene now gives 4 unobservable + 19 weak and the gate still fails on
`freed_parameters` (see §6).
