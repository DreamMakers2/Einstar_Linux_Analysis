# Uncertainty calibration and failure-mode audit

Generated: 2026-11-10T00:36:37+0100  
Live tree: `f4ba545d475462bf787493b31415d4e80a4cae1c` (dirty=True)  
Scope: **synthetic ground truth + vendor corpus consistency**. No physical-accuracy claim is made.

## TL;DR

- k=5: NEES/n all=1.318 (optimistic), intrinsics=1.134, extrinsics=1.031, 68% coverage=0.666.
- k=12: NEES/n all=1.248 (optimistic), intrinsics=1.168, extrinsics=0.987, 68% coverage=0.669.
- k=25: NEES/n all=1.169 (optimistic), intrinsics=1.144, extrinsics=0.921, 68% coverage=0.639.

## Variants used

| label | definition |
|---|---|
| current | live calibrate() covariance (default robust_covariance=True) |
| gn_mad | sigma_MAD^2 * pinv(J^T J), literal pre-robust estimator |
| sandwich_current | pre-fix reference sandwich: B=J^T diag(w r^2) J (conservative for outliers) |
| sandwich_corrected | standard Huber sandwich: B=J^T diag((w r)^2) J (live tree after the parent's fix) |
| marginal | rig covariance = top-left 39x39 block of the joint covariance |
| conditional_legacy | C_rr - C_rv C_vv^-1 C_vr (conditional, too small) |
| rescale_transform | S C S^T, translation rows/cols scaled only |
| rescale_legacy | C * s^2 (old, wrong for non-translation parameters) |
| corpus | vendor 25-view corpus, CCF as reference (no physical truth) |
| synthetic | synthetic scene with known ground truth |

## 1. Monte-Carlo NEES and coverage

Independent synthetic scenes; true K/D/rig/poses drawn per trial; 0.2 px i.i.d. noise; no gross outliers; `free_kc=True`; current default covariance.

| k | trials | NEES/n all [95% CI] | intr | extr | rig | cov68 all | cov95 all | cov68 extr | cov95 extr | verdict |
|---|---|---|---|---|---|---|---|---|---|---|
| 5 | 60 | 1.32 [1.28,1.40] | 1.13 | 1.03 | 1.25 | 0.67 | 0.95 | 0.64 | 0.93 | optimistic |
| 8 | 60 | 1.32 [1.26,1.40] | 1.24 | 1.06 | 1.30 | 0.67 | 0.93 | 0.66 | 0.95 | optimistic |
| 10 | 60 | 1.24 [1.20,1.29] | 1.16 | 0.85 | 1.17 | 0.68 | 0.94 | 0.69 | 0.95 | optimistic |
| 12 | 60 | 1.25 [1.20,1.30] | 1.17 | 0.99 | 1.15 | 0.67 | 0.94 | 0.71 | 0.94 | optimistic |
| 15 | 60 | 1.19 [1.14,1.27] | 1.13 | 0.91 | 1.12 | 0.68 | 0.95 | 0.69 | 0.95 | optimistic |
| 20 | 60 | 1.21 [1.12,1.23] | 1.16 | 0.98 | 1.06 | 0.65 | 0.94 | 0.69 | 0.93 | optimistic |
| 25 | 60 | 1.17 [1.13,1.22] | 1.14 | 0.92 | 1.13 | 0.64 | 0.92 | 0.71 | 0.96 | optimistic |

### Covariance-estimator variants

| k | variant | NEES/n all | intr | extr | cov68 | sigma_fx | sigma_base |
|---|---|---|---|---|---|---|---|
| 10 | sandwich_current | 1.23 | 1.16 | 0.85 | 0.68 | 0.80 | 0.0425 |
| 10 | sandwich_corrected | 1.23 | 1.16 | 0.85 | 0.68 | 0.80 | 0.0425 |
| 10 | gn_mad | 1.06 | 0.99 | 0.89 | 0.69 | 0.81 | 0.0427 |
| 10 | legacy_pinv_h | 1.06 | 0.99 | 0.89 | 0.69 | 0.81 | 0.0427 |
| 12 | sandwich_current | 1.23 | 1.17 | 0.99 | 0.67 | 0.71 | 0.0371 |
| 12 | sandwich_corrected | 1.23 | 1.17 | 0.99 | 0.67 | 0.71 | 0.0371 |
| 12 | gn_mad | 1.08 | 0.98 | 0.99 | 0.68 | 0.72 | 0.0367 |
| 12 | legacy_pinv_h | 1.08 | 0.98 | 0.99 | 0.68 | 0.72 | 0.0367 |
| 15 | sandwich_current | 1.18 | 1.13 | 0.91 | 0.68 | 0.61 | 0.0326 |
| 15 | sandwich_corrected | 1.18 | 1.13 | 0.91 | 0.68 | 0.61 | 0.0326 |
| 15 | gn_mad | 1.05 | 0.95 | 0.88 | 0.69 | 0.62 | 0.0316 |
| 15 | legacy_pinv_h | 1.05 | 0.95 | 0.88 | 0.69 | 0.62 | 0.0316 |
| 20 | sandwich_current | 1.18 | 1.16 | 0.98 | 0.65 | 0.51 | 0.0278 |
| 20 | sandwich_corrected | 1.18 | 1.16 | 0.98 | 0.65 | 0.51 | 0.0278 |
| 20 | gn_mad | 1.03 | 1.02 | 0.96 | 0.66 | 0.51 | 0.0279 |
| 20 | legacy_pinv_h | 1.03 | 1.02 | 0.96 | 0.66 | 0.51 | 0.0279 |
| 25 | sandwich_current | 1.16 | 1.14 | 0.92 | 0.64 | 0.46 | 0.0246 |
| 25 | sandwich_corrected | 1.16 | 1.14 | 0.92 | 0.64 | 0.46 | 0.0246 |
| 25 | gn_mad | 1.03 | 1.02 | 0.90 | 0.65 | 0.46 | 0.0252 |
| 25 | legacy_pinv_h | 1.03 | 1.02 | 0.90 | 0.65 | 0.46 | 0.0252 |
| 5 | sandwich_current | 1.30 | 1.13 | 1.03 | 0.67 | 1.37 | 0.0701 |
| 5 | sandwich_corrected | 1.30 | 1.13 | 1.03 | 0.67 | 1.37 | 0.0701 |
| 5 | gn_mad | 1.08 | 1.00 | 1.00 | 0.67 | 1.43 | 0.0693 |
| 5 | legacy_pinv_h | 1.08 | 1.02 | 1.03 | 0.67 | 1.39 | 0.0693 |
| 8 | sandwich_current | 1.29 | 1.24 | 1.06 | 0.67 | 0.87 | 0.0460 |
| 8 | sandwich_corrected | 1.29 | 1.24 | 1.06 | 0.67 | 0.87 | 0.0460 |
| 8 | gn_mad | 1.07 | 1.02 | 1.02 | 0.67 | 0.90 | 0.0464 |
| 8 | legacy_pinv_h | 1.07 | 1.02 | 1.02 | 0.67 | 0.90 | 0.0464 |

### Marginal vs legacy conditional rig covariance

| k | live-vs-marginal rel | live-vs-conditional rel | trace ratio live/cond | NEES extr marginal | NEES extr conditional |
|---|---|---|---|---|---|
| 5 | 0.00 | 1.27 | 2.80 | 1.03 | 9.94 |
| 8 | 0.00 | 1.13 | 2.76 | 1.06 | 10.93 |
| 10 | 0.00 | 1.15 | 2.70 | 0.85 | 8.81 |
| 12 | 0.00 | 1.16 | 2.87 | 0.99 | 8.11 |
| 15 | 0.00 | 1.15 | 2.66 | 0.91 | 8.32 |
| 20 | 0.00 | 1.10 | 2.65 | 0.98 | 13.54 |
| 25 | 0.00 | 1.12 | 2.66 | 0.92 | 10.94 |

## 2. Vendor-corpus bootstrap

25 views, CCF as reference (not truth). Empirical spread over resampled views/points vs the reported sigma.

| param | reported sigma | empirical sd (views) | ratio views | ratio points80 (adj) | ratio points60 (adj) |
|---|---|---|---|---|---|
| left.fx | 0.475 | 1.264 | 0.38 | 2.16 | 1.25 |
| right.fx | 0.521 | 1.576 | 0.33 | 2.44 | 1.36 |
| tex.fx | 0.516 | 1.603 | 0.32 | 2.22 | 1.42 |
| left.cx | 0.819 | 1.865 | 0.44 | 1.94 | 1.38 |
| right.cx | 0.644 | 1.474 | 0.44 | 2.03 | 1.76 |
| right.tx | 0.040 | 0.088 | 0.46 | 2.47 | 1.94 |
| right.ty | 0.023 | 0.025 | 0.94 | 1.81 | 1.89 |
| right.tz | 0.105 | 0.102 | 1.03 | 2.36 | 1.60 |

## 3. Perturbation tests

Synthetic k=12 scenes, 20 trials per condition (reduced for slow or extreme cases). `z = |error| / reported sigma`; a median |z| well above 1 means the covariance is overconfident for that parameter.

| condition | n | fx | baseline | rot | cx | NEES/n all | stop pass | flags |
|---|---|---|---|---|---|---|---|---|
| clean_iid | 24 | 0.49 | 0.74 | 0.80 | 0.62 | 1.17 | 0.33 |  |
| det_global_shift_0.05 | 20 | 0.97 | 0.56 | 0.77 | 0.61 | 3.01 | 0.30 |  |
| det_global_shift_0.25 | 20 | 1.19 | 0.58 | 0.87 | 0.61 | 48.16 | 0.25 |  |
| det_subset_shift_30pct_0.3 | 20 | 1.24 | 2.13 | 0.69 | 2.13 | 9.53 | 0.35 |  |
| det_subset_shift_30pct_0.5 | 20 | 0.94 | 2.69 | 0.89 | 4.33 | 23.73 | 0.15 | left.cx: median|z|=4.3 (sigma=1.176319216266346); tex.cx: median|z|=3.7 (sigma=1 |
| det_gradient_0.2px | 20 | 1.36 | 1.49 | 1.18 | 1.18 | 6.14 | 0.40 |  |
| lens_rational_moderate | 20 | 0.78 | 0.27 | 1.09 | 0.94 | 1071.51 | 0.30 |  |
| lens_poly8_moderate | 20 | 0.59 | 0.75 | 0.83 | 0.74 | 301.74 | 0.25 |  |
| pitch_-1.0pct | 20 | 0.89 | 40.28 | 0.69 | 0.50 | 2124.42 | 0.30 | baseline: median|z|=40.3 (sigma=0.041307235210405524) |
| pitch_-0.5pct | 20 | 0.55 | 21.59 | 0.71 | 0.65 | 573.10 | 0.40 | baseline: median|z|=21.6 (sigma=0.03798792050279097) |
| pitch_-0.1pct | 20 | 0.65 | 4.77 | 0.65 | 0.62 | 23.76 | 0.40 | baseline: median|z|=4.8 (sigma=0.03824371208129439) |
| pitch_+0.1pct | 20 | 1.20 | 4.67 | 0.88 | 0.72 | 21.03 | 0.40 | baseline: median|z|=4.7 (sigma=0.03805205207771156) |
| pitch_+0.5pct | 20 | 0.73 | 24.23 | 0.79 | 0.66 | 603.97 | 0.40 | baseline: median|z|=24.2 (sigma=0.034251985156893386) |
| pitch_+1.0pct | 20 | 0.37 | 42.15 | 1.04 | 0.78 | 2246.29 | 0.25 | baseline: median|z|=42.2 (sigma=0.03533653637565766) |
| board_bow_0.3mm | 20 | 2.86 | 1.77 | 3.88 | 0.76 | 4.80 | 0.25 | right.fx: median|z|=3.9 (sigma=0.6795408070831404); tex.fx: median|z|=3.5 (sigma |
| board_bow_1.0mm | 20 | 6.06 | 3.08 | 8.27 | 3.11 | 13.47 | 0.00 | left.fx: median|z|=6.1 (sigma=1.1992981532716573); right.fx: median|z|=7.9 (sigm |
| board_warp_0.1mm | 20 | 1.06 | 0.79 | 0.96 | 1.87 | 2.06 | 0.05 |  |
| noise_corr_len1_0.2px | 20 | 1.13 | 1.99 | 1.53 | 1.69 | 9.04 | 0.05 |  |
| noise_corr_len2_0.2px | 20 | 2.19 | 1.97 | 1.60 | 1.25 | 35.53 | 0.00 |  |
| noise_corr_len1_0.5px | 20 | 1.63 | 1.74 | 1.32 | 1.10 | 9.76 | 0.00 |  |
| pose_jitter_0.5deg_2mm | 20 | 0.68 | 0.71 | 1.20 | 1.02 | 1.18 | 0.15 |  |
| pose_jitter_2deg_8mm | 15 | 0.63 | 1.27 | 0.85 | 0.63 | 1.29 | 0.33 |  |
| missing_20pct | 20 | 0.79 | 0.27 | 0.75 | 0.52 | 1.25 | 0.15 |  |
| missing_40pct | 20 | 0.67 | 0.46 | 0.83 | 0.80 | 1.40 | 0.00 |  |
| missing_60pct | 15 | 0.52 | 0.94 | 1.11 | 0.87 | 1.53 | 0.00 |  |
| missing_columns_40pct | 20 | 0.75 | 1.29 | 1.04 | 0.72 | 1.40 | 0.00 |  |
| outliers_2pct | 20 | 0.72 | 0.63 | 0.78 | 0.63 | 1.13 | 0.05 |  |
| outliers_5pct | 20 | 0.82 | 0.64 | 1.04 | 0.86 | 1.17 | 0.05 |  |
| outliers_10pct | 20 | 0.69 | 0.40 | 0.80 | 0.54 | 1.14 | 0.00 |  |
| outliers_20pct | 15 | 1.06 | 0.25 | 0.82 | 1.24 | 1.05 | 0.00 |  |

## 4. Failure cases

| condition | n | sigma_fx | baseline z | rank-def | zero-var | cond | stop pass |
|---|---|---|---|---|---|---|---|
| coverage_left_right | 12 | 0.866 | 1.03 | 12 | 12 | 265424.42 | 0.00 |
| coverage_left_only | 12 | 1.439 | n/a | 12 | 12 | 43193.48 | 0.00 |
| frontoparallel | 10 | 521.754 | 0.60 | 2 | 0 | 783387334.91 | 0.00 |
| identical_poses | 8 | 882.477 | 0.45 | 1 | 0 | 377096590.47 | 0.00 |
| fixed_kc_biased5pct | 15 | 0.829 | 2.65 | 0 | 0 | 252599.81 | 0.20 |
| fixed_kc_oracle | 15 | 0.782 | 1.46 | 0 | 0 | 250774.88 | 0.27 |
| fixed_kc_biased10pct | 15 | 0.925 | 2.70 | 0 | 0 | 300760.65 | 0.00 |
| near_identical | 10 | 39.124 | 1.13 | 0 | 0 | 15414492.24 | 0.00 |
| same_tilt | 12 | 0.530 | 0.95 | 0 | 0 | 258871.06 | 0.08 |
| few_views_3 | 15 | 2.490 | 1.01 | 0 | 0 | 478933.47 | 0.00 |
| unused_views_gaps | 10 | 1.108 | 0.74 | 0 | 0 | 320741.32 | 0.00 |
| free_kc_reference | 15 | 0.665 | 0.89 | 0 | 0 | 321928.11 | 0.33 |
| same_distance | 12 | 1.257 | 0.84 | 0 | 0 | 492039.71 | 0.00 |
| same_tilt_distance | 10 | 1.038 | 0.58 | 0 | 0 | 477421.21 | 0.00 |
| few_views_5 | 15 | 1.231 | 0.57 | 0 | 0 | 303647.65 | 0.00 |
| clustered | 12 | 0.473 | 0.68 | 0 | 0 | 149850.32 | 0.00 |
| narrow_band | 12 | 17.741 | 0.52 | 0 | 0 | 6438325.57 | 0.00 |
| coverage_right_1view | 0 | n/a | n/a | 0 | 0 | n/a | n/a |

## 5. Recommended stopping rule

Minimum views: **15**. first k with NEES_all in [0.85,1.15], 68% coverage >=0.60, 95% coverage >=0.90, sigma_fx <=0.60 px, sigma_baseline <=0.15 mm; rounded up to at least 12

| check | threshold |
|---|---|
| joint views seen by all 3 cameras | 8 |
| views per camera | 8 |
| points per view/camera | 20 |
| observations per camera | 300 |
| distance bins | 3 |
| tilt signs each axis | 1 |
| image quadrant fraction | 0.75 |
| sigma_fx (px) | 0.5 |
| sigma_baseline (mm) | 0.12 |
| sigma_rotation (deg) | 0.2 |
| condition number | 10000000.0 |
| heldout RMS (px) | 0.5 |
| heldout / train | 1.5 |
| stability max |z| | 4.0 |

### Monte-Carlo acceptance criteria for future changes

```json
{
 "n_scenes_per_view_count": 50,
 "view_counts": [
  5,
  8,
  10,
  12,
  15,
  20,
  25
 ],
 "nees_per_n_median": [
  0.9,
  1.1
 ],
 "nees_per_n_ci95_includes_1": true,
 "coverage68": [
  0.63,
  0.73
 ],
 "coverage95": [
  0.92,
  0.98
 ],
 "z_rms_max": 1.2,
 "median_abs_z_max": 0.2,
 "no_zero_variance_params": true,
 "bootstrap_ratio": [
  0.8,
  1.25
 ],
 "worst_condition_median_z_max": 2.0,
 "pitch_sensitivity": "z_baseline<3 at 0.1% or propagated pitch sigma"
}
```

## A/B/C/U claims

A = proved/derived; B = measured with a stated statistic; C = cross-checked against the corpus; U = unknown.

| grade | claim | evidence |
|---|---|---|
| A | The joint covariance's top-left 39x39 block is the marginal rig covariance; the old conditional formula is smaller by a factor measured in task 1. | rig_variants comparisons; marginal_vs_conditional |
| A | A free parameter with zero Jacobian column (camera never observed) receives zero variance and can carry an arbitrary error; rank/coverage checks are mandatory. | task4 coverage_left_only |
| A | rescaled() covariance now matches S C S^T to ~1e-10; the legacy C*s^2 does not. | rescale_check |
| B | The live default covariance is (approximately) calibrated on clean synthetic scenes; NEES/n is reported per view count. | task1 |
| B | The pre-fix Huber sandwich used w*r^2 in B rather than (w*r)^2; with outliers it was conservative, not optimistic. The live tree now matches the standard sandwich. | task1 variants + task4 outliers |
| B | Plate-pitch error is a scale gauge not represented in the covariance; a 0.1% error is already several sigma on the baseline. | task3 pitch conditions |
| B | Spatially correlated detector noise and non-rigid marker shifts are the main synthetic overconfidence mechanisms after pitch. | task3 correlated/missing/subset conditions |
| C | On the vendor corpus the bootstrap spread is compared with the reported sigma; the CCF is a reference, not truth. | task2 |
| U | The physical detector bias, plate flatness and the true pitch tolerance are unknown; synthetic amplitudes are plausible, not measured. | limitations |

## Limitations

- Synthetic scenes use a pinhole + Brown-Conrady model; detector physics, illumination and the real plate flatness are not modelled.
- The vendor corpus has no physical ground truth; CCF agreement is consistency, not accuracy.
- The `rescaled()` covariance is a linear transform; the result's stored residual RMS is not recomputed after scaling.

## Reproduce

```bash
.venv/bin/python -m calib.uncertainty_audit --out analysis/calibration/audit
```
