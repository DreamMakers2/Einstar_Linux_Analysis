# Covariance, gauge transformations and conservative stopping

Status: 2026-11-10. This document derives the corrections made after auditing the
published engine. It is deliberately explicit about what is a covariance
(variance of the estimator) and what is a physical-accuracy claim (which
requires an artefact).

Notation: parameter vector

```
x = [ K_L, K_R, K_T, (r_R,t_R), (r_T,t_T), (r_1,t_1), …, (r_N,t_N) ]
```

with `K = [fx,fy,cx,cy,kc1..kc5]` and rotations stored as Rodrigues vectors. The
left camera is the gauge (`R_L=I, t_L=0`). `n_r=39` is the rig prefix and
`n_v=6N` the board poses. The residual vector `r(x)` stacks two pixel
coordinates per observed hole.

## 1. Marginal vs conditional covariance (the published defect)

For a Gaussian linearisation, the joint covariance is `C = H^{-1}` with
`H = JᵀWJ`. Partition it as

```
C = [ C_rr  C_rv ]
    [ C_vr  C_vv ]
```

The rig parameters are the ones we report; the board poses are **nuisance
parameters**. Their uncertainty must be integrated out, so the required
covariance is the **marginal** block `C_rr` (the top-left block of `C`).

The old code computed

```
C_cond = C_rr − C_rv C_vv^{-1} C_vr ,
```

which is the covariance of `x_r` **conditioned on the board poses**. That is the
wrong estimand: it removes the pose uncertainty and is therefore too small. For
the same data, the audit measured (10 synthetic views, 0.15 px noise, plain
Gauss–Newton):

| quantity | fx sigma (px) | ratio |
|---|---:|---:|
| marginal `C_rr` (correct) | 0.584 | 1.00 |
| conditional `C_cond` (old) | 0.0537 | **0.092** |

So the old stopping rule was ~11× over-confident for the focal length. This is
the direct explanation for the premature stops seen in the first audit.

### 1.1 Schur-complement verification

By the block-inverse identity,

```
(C_rr)^{-1} = H_rr − H_rv H_vv^{+} H_vr  ≡  S .
```

The engine stores `S` (`result.marginal_information`) computed directly from the
weighted Jacobian, using the SVD of `J_v` for `H_vv^{+}`. It does **not** form
`pinv(H_vv)` with an H-scale cutoff: because `H=JᵀJ` has eigenvalues `s²`, a
`1e-10` cutoff on `H` drops directions with `s/s_max` between `1e-5` and
`1e-10` that the Jacobian rank keeps. That mistake produced six zero-variance
directions and a singular rig covariance in the published implementation.

The regression test checks `S = inv(C_rr)·σ²` to `1e-6` relative on a synthetic
10-view problem (plain estimator). For the robust estimator, `S` is the
Huber-weighted expected information and is used for **view selection**; the
reported covariance is the robust sandwich (next section). They are not
inverses of each other, and the code no longer assumes they are.

## 2. Robust covariance

For the Huber M-estimator with weights `w_i = 1` if `|r_i| ≤ δ`, else `δ/|r_i|`:

```
A = Jᵀ W J
B = Jᵀ W² diag(r²) J
C_robust = A^{-1} B A^{-1}
```

The implementation works in the SVD basis of `√W J` (so `A^{-1}` is stable),
clips negative eigenvalues of the Schur complement to zero, and clips the final
matrix to the PSD cone. The plain Gauss–Newton estimator `σ² (JᵀJ)^{-1}` is kept
for comparison and for the `robust_covariance=False` path.

Synthetic NEES/n after the correction. With `free_kc=False` (the standard
profile) it is 0.91 / 1.01 / 0.92 / 1.02 at 5/8/12/20 (3 trials, 0.2 px noise,
2 % outliers), i.e. calibrated. The final 60-trial audit with **`free_kc=True`**
gives 1.32 / 1.32 / 1.25 / 1.19 / 1.17 at 5/8/12/15/25 and 68 % coverage
0.64–0.68 (nominal 0.68): the covariance is **slightly optimistic** when the
distortion is free, and the plain `gn_mad` variant is closer (NEES 1.03–1.08).
This is a known limitation, not hidden by the stopping rule.

Residual scale is the **component-wise MAD** (`1.4826·median|r−median(r)|`), not
the norm of 2-D residuals, which underestimates σ by ~20 % and makes the
covariance optimistic.

## 3. Scaling the calibration (`rescaled`)

A planar target determines the translation only up to the plate's metric scale.
Let `s` be the scale applied to every translation (rig and board poses). The
parameter transformation is

```
x'_K = x_K,   x'_rot = x_rot,   x'_t = s x_t .
```

The Jacobian is diagonal with `s` on translation entries and 1 elsewhere, so

```
C' = J C Jᵀ ,   J = diag(1,…,1,s,…,s,1,…) .
```

The published code used `C' = s² C`, which inflated the focal-length, distortion
and rotation variances by `s²` as well. The audit test asserts:

* intrinsic/rotation sigmas unchanged for `s=2`;
* translation sigmas exactly doubled;
* baseline sigma exactly doubled.

### 3.1 Anchor uncertainty

When the scale is set from an anchor baseline `b0` (e.g. the factory CCF), then

```
s(x) = b0 / b(x),   b(x)=|t_R − R_R t_L|,
```

and the correct first-order transform includes the dependence of `s` on `x`:

```
∂x'_t/∂x_t   = s I
∂x'_t/∂x_other = −(b0/b²) x_t (∂b/∂x_other)
∂x'_t/∂b0    = x_t / b
C' = J C Jᵀ + σ_b0² j_b j_bᵀ .
```

`rescaled(target_baseline=..., anchor_sigma_mm=...)` implements this numerically
(and prints `scale_anchor_sigma_mm`). With an exact anchor (`σ_b0=0`) the
baseline variance is zero **by construction**, which is why the pipeline passes
a non-zero factory-anchor sigma; an exact anchor is a modelling assumption, not
a measurement.

## 4. Rebasing (`rebased`) and the gauge

CCF records are world→camera poses in the board frame of the reference view
(camera 1's first valid view; `CalibrationCCF::QuarCCF` 0x1802e5e70). Rebasing to
view `v` applies

```
R'_c = R_c R_v,          t'_c = R_c t_v + t_c
R'_i = R_vᵀ R_i,         t'_i = R_vᵀ (t_i − t_v)
```

which changes the gauge. Relative poses and their uncertainties are invariant,
but the left camera is no longer the identity and the packed parameter vector
does not contain the six rebased left-camera degrees of freedom. Publishing the
old covariance unchanged (or Jacobian-transformed as if left stayed fixed) would
be wrong. `rebased()` therefore sets `covariance=None`, records
`baseline_sigma_mm` and the relative IR/RGB sigmas in `info`, and documents the
gauge. The CCF export only needs R/T, not the absolute covariance.

## 5. View selection (Fisher information)

The planner uses the marginal rig covariance `C_rr`. For a candidate view with
Jacobian `J = [J_r | J_v]`, the board pose is eliminated:

```
H_add = J_rᵀJ_r − J_rᵀJ_v (J_vᵀJ_v)^{+} J_vᵀJ_r
H_prior = inv(C_rr)
C_after = (H_prior + H_add)^{-1}
gain = tr(C_rr) − tr(C_after)
```

The prior information is `inv(C_rr)`, **not** raw `JᵀJ`: the two differ by the
residual variance (and, for the robust estimator, by the sandwich meat), and
using `JᵀJ` made A-optimal gains negative in the published code. A distance/tilt
diversity filter prevents the greedy plan from clustering all views at 240 mm.

## 6. Conservative stopping

`calib.quality.stopping_report` requires **all** of the following; covariance
thresholds alone can never accept a calibration:

| check | default (standard) | rationale |
|---|---|---|
| views | ≥ 10 | below this the marginal covariance is dominated by pose nuisance |
| distance bins | ≥ 3 (50 mm bins) | constrains principal point / distortion |
| tilt signs and range | ≥1 each side of median, ≥10° range each axis | avoids a single-tilt degenerate set |
| image quadrants | ≥75 % per camera | field coverage |
| observations | ≥300 per camera | enough redundant rays |
| Jacobian rank/condition | full rank, cond ≤ 1e7 | rejects numerically unobservable sets |
| sigma fx/fy | ≤0.75 px (standard) | configurable preset |
| baseline sigma | ≤0.12 mm | |
| IR relative rotation sigma | ≤0.20° | |
| RGB translation sigma | ≤1.5 mm | |
| held-out reprojection | ≤0.5 px, **required** | independent of the training views |
| parameter stability | max |Δ|/σ < 4 when dropping the newest view | detects local minima / bias |

Presets: `fast` (1.25 px / 0.20 mm / 0.8 px held-out), `standard` (0.75 / 0.12 /
0.5), `high` (0.35 / 0.06 / 0.35). The decision is returned as a dictionary with
`value`, `limit`, `pass` and `reasons_failed` for every check, so it is
reproducible and explainable.

### 6.1 Beyond the single-fit covariance

The audit found three failure modes that a single-fit covariance cannot see:

* **View composition.** The 60-trial audit's view resampling changes fx by
  1.26–1.60 px while the reported sigma is 0.475–0.521 px (ratio 0.32–0.44).
  A 6-fit drop-4 bootstrap of the vendor corpus gives fx 0.647 vs 0.504 px
  (ratio 0.78), so the effect is data/selection-dependent: the single-fit sigma
  can understate view-to-view variability, but not always by 2–3×.
  `bootstrap_sigma()` measures it; `--bootstrap` is available and should be
  used for a production profile.
* **Plate pitch.** +0.1 % true pitch gives baseline z≈4.7, +1 % z≈42; the
  baseline is a scale gauge. Anchor with a measured length or pass a pitch
  sigma; the pixel covariance cannot see it.
* **Board bow / fixed distortion.** 1 mm bow gives fx z≈6, rotation z≈8; a 10 %
  biased fixed `kc` gives fx z≈4–5, cx≈12. Check plate flatness and do not fix
  `kc` without verification.

The quality report carries `physical_scale` and bootstrap warnings so these do
not masquerade as pixel-level confidence.

## 7. What the corrected uncertainty means for capture counts

The marginal correction increases the reported focal uncertainty by ~10× on the
same data. The old "8–12 captures" conclusion was based on a stopping rule that
could not see this component. The corrected analysis must therefore re-derive
the minimum count against an explicit target; the view-count audit
(`analysis/calibration/audit/viewcount_report.md`) is the authoritative
comparison. Do not quote a capture count without the target and the held-out
protocol.

## 8. Regression tests

`tests/test_calibration_audit.py`:

1. marginal covariance exceeds conditional by >2× for fx/fy and equals `C_rr`;
2. `S = inv(C_rr)·σ²` (Schur consistency);
3. `rescaled(2)` leaves intrinsic/rotation sigmas unchanged, doubles translation
   and baseline sigmas; anchor propagates `σ_b0`;
4. `rebased` does not publish an invalid absolute covariance, but preserves
   relative sigmas;
5. stopping without coverage/held-out is rejected and names the missing checks;
6. view remapping is compact and `view_pose()` maps original ids;
7. CCF relative poses are invariant under a common world-frame rotation.
