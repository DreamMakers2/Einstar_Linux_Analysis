# Partial-board observations: accuracy and conditioning on fresh-01

**Question (objective §9).** Does enabling partial-board observations change
calibration accuracy or conditioning? Three observation policies are compared
under one independent validation protocol:

* **(a) full-board only** — views in which all three cameras saw the whole 8×5 field,
* **(b) all accepted views** — partial boards included,
* **(c) hybrid** — the full-board core plus a bounded number `K` of partial views.

Data: the real `<calibration-session>` accepted set, 162 views (80 full-board / 82 partial). All fits use the production path `calib.calibrate.calibrate(..., pitch=28.0, backend='fast')` with the `high` policy (`free_kc` once `n≥8`, Huber, `f_scale=0.8`).

## Verdict

* **All accepted views vs full-board only:** held-out cross-camera median 0.860 px vs 0.891 px, p95 2.182 vs 2.248 px — **better** than full-board only by 0.030 px (paired 95 % CI [-0.043, -0.019] px).
* **Hybrid, +10 best partial views:** median 0.906 px, p95 2.228 px — no change beyond noise (paired 95 % CI [-0.006, +0.004] px includes 0).
* **Hybrid, +20 best partial views:** median 0.872 px, p95 2.219 px — **better** than full-board only by 0.015 px (paired 95 % CI [-0.021, -0.009] px).
* **Matched view count** (same number of training views as full-board only): a random mix gives no change beyond noise (paired 95 % CI [-0.034, +0.003] px includes 0).
  With 2/3 full + 1/3 partial: no change beyond noise (paired 95 % CI [-0.014, +0.010] px includes 0); with 1/3 full + 2/3 partial: no change beyond noise (paired 95 % CI [-0.032, +0.004] px includes 0).
* **Conditioning:** stereo baseline sigma 0.039 mm (all views) vs 0.059 mm (full-board only), i.e. -0.021 mm (significant); stereo rotation sigma -0.026 deg (significant); unit-free conditioning -64.159 (significant).
* Including partial views does **not** make accuracy or conditioning worse: no
  arm is above full-board only on held-out error beyond noise, and every arm's
  conditioning sigmas shrink. No claim is made about EXStar or any vendor.

## Method

### Recovering labels for partial views

The store keeps `observation['image_points']` as a list per camera. That list is
row-major `(v,u)` — canonical label `(i%8, i//8)` — **only** when all 40 markers were
detected; for a partial view it keeps the detector's assignment order and the JSON
alone cannot say which lattice site each row is. The stored PNGs are lossless, so every
camera image was re-detected with `calib.detect.detect_grid_auto`, whose result is keyed
by `(u,v)`; a detection is used only when its identity witness verified (three chiral
on-lattice large markers plus the off-lattice chiral marker).

The 80 views whose stored lists *are* label-ordered
validate that reconstruction: of 14273 recovered `(u,v)→pixel` rows, 14233 matched the stored label order (99.72 %), at a median pixel agreement of 0.0004 px (p95 0.0006 px).
The 40 exceptions are all in one view's colour camera
(`view 103, tex`), where the stored 40-row list is mirror-`u` relative to the
witness-verified detection; a planar lattice makes that mirror a 180° flip of the
board plane with identical reprojection, so it is unresolvable from the JSON alone
and the chiral witness is the tie-breaker. The re-detected labelling is used.

### Protocol

* 3 repeats × 4-fold split over all 162 pool views, fixed seed 20261114.
* **Held-out scoring never refits the view.** Its board pose is PnP-solved from its
  left observation with the fitted left model; the right and colour pixels are then
  *predicted* through the fitted rig (`X_c = R_c·X_left + t_c`) and that camera's
  fitted intrinsics. `rms_cross` (right+tex, primary) therefore tests the extrinsics
  and the other cameras' intrinsics; `rms_left` is the pose-fit residual.
* Identical test folds for every arm inside a (repeat, fold), so arm differences are
  paired view-by-view.
* Every fit is the production call followed by the wizard's finish-time prune
  (`_prune_outliers`, 3 px on in-sample per-camera-view residuals) and one refit —
  applied identically to all arms; the number dropped is reported per arm.
* `free_kc` is on for every arm here (all training sets exceed `kc_views = 8`),
  `robust_loss='huber'`, `f_scale=0.8`, `backend='fast'`.
* BLAS pinned to one thread (`OPENBLAS_NUM_THREADS=1`, `OMP_NUM_THREADS=1`, `MKL_NUM_THREADS=1`).

## Arms actually fitted

| arm | full views | partial views | train views | markers (mean) | min markers (mean) | obs pruned |
|---|---|---|---|---|---|---|
| a) full-board only | 60 | 0 | 60 | 7200 | 40 | 0.4 |
| b) all accepted views | 60 | 62 | 122 | 14182 | 26 | 3.2 |
| c) matched count, random mix | 29 | 31 | 60 | 7002 | 27 | 1.3 |
| c) matched count, 2/3 full + 1/3 partial | 40 | 20 | 60 | 7071 | 29 | 1.1 |
| c) matched count, 1/3 full + 2/3 partial | 20 | 40 | 60 | 6950 | 27 | 1.8 |
| c) hybrid: core + 10 best partial | 60 | 10 | 70 | 8388 | 39 | 0.8 |
| c) hybrid: core + 20 best partial | 60 | 20 | 80 | 9572 | 38 | 0.5 |
| c) hybrid: core + 10 random partial | 60 | 10 | 70 | 8340 | 30 | 0.8 |
| c) hybrid: core + 10 most-clipped partial | 60 | 10 | 70 | 8341 | 35 | 0.7 |

Hybrid arms add the `K` partial views with the most markers (the ones a deployment
would admit first), plus random and most-clipped variants for robustness. The
matched-count arms hold the training-view count equal to the full-board arm's, so
they answer “is a partial view worth as much as a full-board view at a fixed budget?”
instead of “is more data better?”.

## Coverage of each arm

| set | views | camera-obs | min markers | median markers | markers total | distance mm | tilt_x +/− % | tilt_y +/− % | roll sectors 0/1/2/3 | image quadrants |
|---|---|---|---|---|---|---|---|---|---|---|
| FULL (80 full-board views) | 80 | 240 | 40 | 40 | 9600 | 341–589 | 8/92 | 51/46 | 0/4/54/22 | 4 |
| PARTIAL (82 partial views) | 82 | 246 | 26 | 36 | 9309 | 314–593 | 13/83 | 49/48 | 0/4/62/16 | 4 |
| ALL (162 views) | 162 | 486 | 26 | 39 | 18909 | 314–593 | 10/88 | 50/47 | 0/8/116/38 | 4 |
| HYBRID +10 best | 90 | 270 | 39 | 40 | 10788 | 314–589 | 8/92 | 49/48 | 0/5/62/23 | 4 |
| HYBRID +20 best | 100 | 300 | 39 | 40 | 11977 | 314–589 | 7/93 | 47/49 | 0/5/67/28 | 4 |
| HYBRID +10 clipped | 90 | 270 | 35 | 40 | 10749 | 341–589 | 8/92 | 56/42 | 0/4/63/23 | 4 |

`tilt_x/tilt_y` are the board-normal tilts in the left camera; `roll` is the in-plane
board roll; `image quadrants` counts occupied image quadrants of the plate centre.
The partial views add tilt/roll/distance diversity rather than novel image regions:
they are the same capture session, so enabling them buys poses, not scene variety.

## Held-out accuracy (primary)

`rms_cross` per held-out view (median over the repeats in which it was held out), then
median / p95 over views. Per-camera columns are the same statistic for a single camera.

| arm | median | p95 | p99 | left | right | colour |
|---|---|---|---|---|---|---|
| a) full-board only | 0.891 | 2.248 | 3.142 | 0.397 | 0.442 | 1.166 |
| b) all accepted views | 0.860 | 2.182 | 3.186 | 0.394 | 0.437 | 1.101 |
| c) matched count, random mix | 0.876 | 2.232 | 3.256 | 0.395 | 0.440 | 1.114 |
| c) matched count, 2/3 full + 1/3 partial | 0.904 | 2.310 | 3.267 | 0.396 | 0.444 | 1.170 |
| c) matched count, 1/3 full + 2/3 partial | 0.858 | 2.280 | 3.246 | 0.396 | 0.448 | 1.091 |
| c) hybrid: core + 10 best partial | 0.906 | 2.228 | 3.170 | 0.395 | 0.440 | 1.171 |
| c) hybrid: core + 20 best partial | 0.872 | 2.219 | 3.186 | 0.398 | 0.438 | 1.150 |
| c) hybrid: core + 10 random partial | 0.888 | 2.231 | 3.216 | 0.398 | 0.441 | 1.148 |
| c) hybrid: core + 10 most-clipped partial | 0.852 | 2.249 | 3.171 | 0.399 | 0.441 | 1.120 |

### Paired against full-board only (same test views)

Δ = arm − full-board, in px on `rms_cross` (negative is better). The block column is
mean ± 95 % bootstrap CI over the (repeat, fold) blocks; the view column is the median
per-view Δ and the share of views that improved.

| arm | Δ block mean | Δ block 95 % CI | Δ view median | views improved | sign-test p |
|---|---|---|---|---|---|
| b) all accepted views | -0.030 | [-0.043, -0.019] | -0.027 | 63% | 3.5e-08 |
| c) matched count, random mix | -0.017 | [-0.034, +0.003] | -0.010 | 54% | 0.11 |
| c) matched count, 2/3 full + 1/3 partial | -0.002 | [-0.014, +0.010] | -0.001 | 50% | 0.96 |
| c) matched count, 1/3 full + 2/3 partial | -0.013 | [-0.032, +0.004] | -0.013 | 53% | 0.16 |
| c) hybrid: core + 10 best partial | -0.001 | [-0.006, +0.004] | -0.001 | 51% | 0.82 |
| c) hybrid: core + 20 best partial | -0.015 | [-0.021, -0.009] | -0.014 | 60% | 1e-05 |
| c) hybrid: core + 10 random partial | -0.010 | [-0.016, -0.005] | -0.007 | 56% | 0.013 |
| c) hybrid: core + 10 most-clipped partial | -0.007 | [-0.013, -0.000] | -0.005 | 55% | 0.021 |

### Held-out accuracy by test-view type

The same models, scored separately on held-out full-board views and held-out partial
views (median `rms_cross` over views).

| arm | full test views | median | partial test views | median |
|---|---|---|---|---|
| a) full-board only | 80 | 0.851 | 82 | 0.927 |
| b) all accepted views | 80 | 0.839 | 82 | 0.870 |
| c) matched count, random mix | 80 | 0.834 | 82 | 0.895 |
| c) matched count, 2/3 full + 1/3 partial | 80 | 0.863 | 82 | 0.953 |
| c) matched count, 1/3 full + 2/3 partial | 80 | 0.830 | 82 | 0.884 |
| c) hybrid: core + 10 best partial | 80 | 0.829 | 82 | 0.938 |
| c) hybrid: core + 20 best partial | 80 | 0.857 | 82 | 0.893 |
| c) hybrid: core + 10 random partial | 80 | 0.851 | 82 | 0.914 |
| c) hybrid: core + 10 most-clipped partial | 80 | 0.829 | 82 | 0.931 |

### Fitted rig geometry (sanity)

Median fitted stereo and colour baselines over the blocks; the factory profile values are 158.56 mm and 125.32 mm. Every arm sits +1.69 mm from the factory stereo baseline and -2.13 mm from the colour one, with a ~0.1 mm
spread across arms: that offset is the known data-versus-factory extrinsic
difference, not a partial-board effect.

| arm | stereo baseline (mm) | colour baseline (mm) |
|---|---|---|
| a) full-board only | 160.31 | 123.13 |
| b) all accepted views | 160.25 | 123.19 |
| c) matched count, random mix | 160.24 | 123.24 |
| c) matched count, 2/3 full + 1/3 partial | 160.26 | 123.25 |
| c) matched count, 1/3 full + 2/3 partial | 160.21 | 123.30 |
| c) hybrid: core + 10 best partial | 160.25 | 123.13 |
| c) hybrid: core + 20 best partial | 160.20 | 123.12 |
| c) hybrid: core + 10 random partial | 160.31 | 123.16 |
| c) hybrid: core + 10 most-clipped partial | 160.20 | 123.23 |

## Conditioning

Marginal sigmas come from `result.param_std()` (joint covariance including the board
poses as nuisance parameters), transformed to the rig quantities by `calib.quality`.

| arm | stereo baseline sigma (mm) | stereo rotation sigma (deg) | colour translation sigma (mm) | colour rotation sigma (deg) | colour pixel sigma @400 mm (px) | residual scale sigma (px) | unit-free conditioning | min covariance eigenvalue |
|---|---|---|---|---|---|---|---|---|
| a) full-board only | 0.059 | 0.070 | 0.071 | 0.082 | 0.207 | 0.430 | 583.2 | 3.0e-11 |
| b) all accepted views | 0.039 | 0.044 | 0.040 | 0.050 | 0.117 | 0.433 | 523.0 | 1.6e-11 |
| c) matched count, random mix | 0.055 | 0.064 | 0.057 | 0.072 | 0.165 | 0.431 | 539.5 | 3.1e-11 |
| c) matched count, 2/3 full + 1/3 partial | 0.056 | 0.065 | 0.062 | 0.074 | 0.179 | 0.434 | 549.6 | 3.2e-11 |
| c) matched count, 1/3 full + 2/3 partial | 0.054 | 0.062 | 0.055 | 0.070 | 0.158 | 0.430 | 525.2 | 3.3e-11 |
| c) hybrid: core + 10 best partial | 0.052 | 0.062 | 0.060 | 0.073 | 0.174 | 0.431 | 581.4 | 2.6e-11 |
| c) hybrid: core + 20 best partial | 0.047 | 0.058 | 0.053 | 0.069 | 0.155 | 0.433 | 555.6 | 2.3e-11 |
| c) hybrid: core + 10 random partial | 0.054 | 0.063 | 0.062 | 0.073 | 0.181 | 0.431 | 563.7 | 2.7e-11 |
| c) hybrid: core + 10 most-clipped partial | 0.054 | 0.063 | 0.063 | 0.071 | 0.181 | 0.426 | 564.4 | 2.7e-11 |

### Paired conditioning change vs full-board only

Mean Δ over the (repeat, fold) blocks; `*` marks a 95 % bootstrap CI that excludes 0.

| arm | stereo baseline sigma (mm) | stereo rotation sigma (deg) | colour translation sigma (mm) | colour rotation sigma (deg) | colour pixel sigma @400 mm (px) |
|---|---|---|---|---|---|
| b) all accepted views | -0.021* | -0.026* | -0.031* | -0.032* | -0.090* |
| c) matched count, random mix | -0.005* | -0.007* | -0.014* | -0.010* | -0.040* |
| c) matched count, 2/3 full + 1/3 partial | -0.002* | -0.005* | -0.009* | -0.007* | -0.027* |
| c) matched count, 1/3 full + 2/3 partial | -0.006* | -0.009* | -0.017* | -0.013* | -0.049* |
| c) hybrid: core + 10 best partial | -0.007* | -0.008* | -0.011* | -0.010* | -0.032* |
| c) hybrid: core + 20 best partial | -0.012* | -0.012* | -0.019* | -0.014* | -0.054* |
| c) hybrid: core + 10 random partial | -0.005* | -0.007* | -0.009* | -0.010* | -0.027* |
| c) hybrid: core + 10 most-clipped partial | -0.005* | -0.008* | -0.009* | -0.011* | -0.027* |

## Interpretation

1. **Accuracy at the deployment setting.** b) all accepted views: Δ -0.030 px [-0.043, -0.019], better — 0.860 px median held-out versus 0.891 px for full-board only, with 122 versus 60 training views.
   Numerically that is below the +0.045 px that a 0.02° stereo rotation error produces, i.e. at the
   edge of what this protocol can resolve at all. The extra partial views do
   not poison the fit; what they add is small and lands on partial geometry.
   The gain is concentrated where it should be: on held-out **partial** views the
   all-views model scores 0.927 → 0.870 px (-6.0 %), versus 0.851 → 0.839 px (-1.4 %) on held-out full-board views.
   The cross-camera metric is colour-dominated (left 0.397, right 0.442, colour 1.166 px), which is the known stale
   shipped colour extrinsic; the arm differences are correspondingly colour-carried.
   The same ordering holds on the all-camera metric `rms_all`: Δ -0.024 px [-0.033, -0.015].
2. **Bounded hybrid.** c) hybrid: core + 10 best partial: Δ -0.001 px [-0.006, +0.004], neutral; c) hybrid: core + 20 best partial: Δ -0.015 px [-0.021, -0.009], better;
   c) hybrid: core + 10 random partial: Δ -0.010 px [-0.016, -0.005], better; c) hybrid: core + 10 most-clipped partial: Δ -0.007 px [-0.013, -0.000], better. A bounded
   number of partial views can be admitted beside the full-board core without an
   accuracy penalty, including the most-clipped ones available in this set.
3. **At a matched view budget** (same number of training views, which isolates the
   *quality* of a partial view from the quantity of data):
   c) matched count, random mix: Δ -0.017 px [-0.034, +0.003], neutral;
   c) matched count, 2/3 full + 1/3 partial: Δ -0.002 px [-0.014, +0.010], neutral;
   c) matched count, 1/3 full + 2/3 partial: Δ -0.013 px [-0.032, +0.004], neutral.
   At this clipping level (median partial board 36/40) the substitution
   is within noise: these boards lose 1-4 markers, so their pose constraint is
   nearly that of a full board. The finding does not extend to heavily clipped
   boards, which this accepted set does not contain.
4. **Tail.** Held-out p95: 2.182 px (all views),
   2.248 px (full-board only), 2.219 px (hybrid +20 best). The tail is dominated by
   one accepted view (`view 129`, accepted while moving: 16.6 mm/s,
   `stationary=0`, colour focus 176) whose colour frame disagrees with every rig by
   ~23 px. That is an observation-quality failure, and it is in every arm's test
   set, so it cannot explain an arm difference.
5. **Pruning.** The production 3 px finish prune dropped 247 observations over 204 fits, from
   5 distinct camera-views (v96/tex, v129/tex, v133/tex, v138/tex, v158/tex); by camera {'tex': 247}. No IR observation was ever dropped, and the
   pre-fold screen (identity witness, ≥12 markers, factory-model PnP < 3 px)
   rejected 0 view-camera observations.
6. **Conditioning.** Stereo baseline sigma 0.039 mm vs 0.059 mm (-0.021 mm (significant)); stereo rotation sigma -0.026 deg (significant); colour translation sigma -0.031 mm (significant). More observations of
   the same 39 rig parameters is what shrinks these, so partial views are a
   conditioning asset, not a risk — provided their labels are right, which is what
   the detector's chiral identity witness and the 3 px prune enforce.

### Answer to section 9

Enabling partial-board observations **does not regress** held-out accuracy or
conditioning beyond noise on fresh-01:

* accuracy: all-views Δ -0.030 px, hybrid +10 -0.001 px, hybrid +20 -0.015 px against
  full-board only (95 % block-bootstrap CIs -0.043…-0.019, -0.006…+0.004, -0.021…-0.009 px);
* conditioning: baseline sigma 0.059 → 0.039 mm, i.e. better;
* at a matched view count, partial views are worth slightly less than full-board
  views but are not measurably harmful at the clipping level present here
  (36/40 median).

**Implication for the production policy.** Admitting partial-board views is safe on
this evidence: the accuracy effect is ≤0.03 px either way, conditioning improves
because the pool grows, and the load-bearing safeguards are the chiral identity
witness and the 3 px finish prune (which in this dataset only ever removed colour
observations, never IR). A hybrid cap is therefore a *cost/latency* choice, not an
accuracy requirement; the full-board core should still be present so the fit is
anchored on complete boards.

This says nothing about boards clipped to a third of the field, which are absent
from this accepted set, and nothing about EXStar or any other implementation.

## Metric sensitivity (positive control)

The same held-out statistic after perturbing one fitted rig parameter by a known
amount (one model, 60 full-board training views, 20 held-out views). This says
what a difference of a given size in the tables above actually means.

| injected error | Δ held-out median (px) | held-out median (px) |
|---|---|---|
| stereo rotation +0.01° | 0.011 | 0.775 |
| stereo rotation +0.02° | 0.045 | 0.808 |
| stereo rotation +0.05° | 0.250 | 1.014 |
| stereo rotation +0.10° | 0.775 | 1.538 |
| stereo rotation +0.20° | 2.016 | 2.780 |
| stereo baseline +0.02 mm | 0.001 | 0.765 |
| stereo baseline +0.05 mm | 0.006 | 0.770 |
| stereo baseline +0.10 mm | 0.026 | 0.790 |
| stereo baseline +0.20 mm | 0.101 | 0.864 |
| stereo baseline +0.50 mm | 0.521 | 1.285 |
| colour translation +0.10 mm | -0.033 | 0.730 |
| colour translation +0.50 mm | 0.438 | 1.202 |
| colour translation +1.00 mm | 1.299 | 2.063 |
| colour translation +2.00 mm | 3.416 | 4.179 |

Baseline held-out median for that model: 0.764 px. The
statistic resolves a 0.02° stereo rotation (+0.045 px) or a 0.05 mm baseline shift
(+0.006 px); the negative entry at +0.10 mm of colour translation is the fitted
colour translation being itself ~0.1 mm off on this subset, and it marks the
noise floor of the statistic, ~0.03 px.

Every arm-to-arm difference in this report is at or below the +0.045 px that a
0.02° stereo rotation error produces — an order of magnitude below the accuracy
targets the gates use (0.06 mm baseline sigma, 0.35 px fx sigma, `high` preset).

## Limitations

* **One dataset.** fresh-01 only, 162 accepted views from a single capture session;
  the EXStar 25-view vendor corpus has just three partial views (35/38/38 markers) and
  cannot support a partial arm, so no second partial-vs-full dataset exists locally.
* **Partial here is mostly mild.** The partial views are 26–39 markers with a median
  of 36; only 43 have ≤36 markers in some camera and the arm
  `H_h10C` is the only probe of genuinely clipped boards. Heavily clipped boards
  (≤16 markers, which the `high` policy still admits) are not represented in this
  accepted set, so this report does not bound their effect.
* **Labels for partial views are reconstructed, not stored.** The reconstruction is
  validated on the 80 label-ordered views (99.7 % of rows exact, median 0.0004 px)
  and gated by the detector's chiral identity witness on every view, but it is not
  the production labelling code path.
* **No priors.** The task-specified call is the bare `calibrate(...)`; the wizard's
  live path also applies 1 px principal-point priors through `calib.pipeline`. Those
  priors would shrink `cx/cy` sigmas for every arm equally and do not change the
  comparison, but absolute sigmas here are slightly larger than a live session's.
* **One accepted view is a different kind of outlier.** `view 129` was accepted with
  `motion_warning=1`, `stationary=0`, 16.6 mm/s and colour focus 176; its colour
  observation disagrees with every fitted rig by ~23 px while its own factory-model
  PnP residual is 0.4 px. It is pruned from training by the 3 px rule and shows up as
  a ~23 px held-out error whenever it is in the test fold, for every arm. That is an
  observation-quality problem (stationarity gate), not a partial-board effect.
* **Held-out metric is left-anchored PnP.** Pose noise from the left view propagates
  into the cross-camera prediction, so absolute values are larger than training RMS;
  the comparison is unaffected because the anchor view is identical for all arms.

## Reproduce

```bash
cd <repo>
export OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1
.venv/bin/python analysis/calibration/accuracy/prepare_data.py
OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 \
  .venv/bin/python analysis/calibration/accuracy/experiment.py --folds 4 --repeats 3
OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 \
  .venv/bin/python analysis/calibration/accuracy/report.py
```

Provenance: git `eee5a9a789175eccf621a08884b1dd9dbfe9b409`, numpy 2.5.4, factory profile sha256 `8259f97cc513eea6…`, 204 calibration fits.
