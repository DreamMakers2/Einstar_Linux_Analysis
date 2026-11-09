# Held-out capture-count audit (view count vs accuracy)

Status: 2026-11-10. Reduced design (10 synthetic scenes × 5 seeds; real 5-fold,
20 training / 5 held-out views), engine `f91efb1e` (free_kc=False path
numerically identical to frozen `05a8a60b`; post-freeze float32 init only
affects free_kc=True). Raw records: `viewcount_results.json` (743 fits, 0 job
errors); the tables below are recomputed directly from `records`. The public
export omits the raw 9 MB file to keep the repository light; `calib/viewcount.py`
regenerates it. The compact `viewcount_summary.json` is included.

**No held-out view is ever used for selection or fitting.** Synthetic
validation views are generated independently and never enter the fit; real
folds hold out one view per orientation (5 views) and train on the other 20.

## 1. Synthetic ground truth (10 scenes × 5 seeds)

fx error, mean / UCB95 (px):

| selection | k=8 | k=12 | k=20 | k=25 |
|---|---:|---:|---:|---:|
| fixed EXStar | 1.44 / 1.83 | 0.92 / 1.12 | 0.69 / 0.83 | 0.49 / 0.65 |
| uniform | **0.78 / 0.99** | 0.61 / 0.75 | 0.45 / 0.61 | 0.49 / 0.65 |
| random | 1.36 / 1.64 | 0.85 / 0.97 | 0.58 / 0.73 | 0.49 / 0.65 |
| adaptive | 0.89 / 1.14 | **0.72 / 0.85** | 0.47 / 0.61 | 0.49 / 0.65 |
| oracle (diagnostic) | 0.35 / 0.45 | 0.28 / 0.35 | 0.24 / 0.36 | 0.49 / 0.65 |

Baseline error (mean) is 0.012–0.037 mm at every k; held-out reprojection is
0.47 px at every k (the held-out pose is solved from the left camera, so it is
insensitive to the intrinsic count). **Intrinsics dominate the count.**

Minimum k whose UCB95 meets the target:

| target | fixed | uniform | random | adaptive | oracle |
|---|---:|---:|---:|---:|---:|
| fx ≤ 1.0 px | 15 | 8 | 12 | 12 | 8 |
| fx ≤ 0.5 px | not at 25 | not at 25 | not at 25 | not at 25 | 8 |
| baseline ≤ 0.05 mm | 8 | 8 | 8 | 8 | 8 |
| held-out ≤ 0.5 px | 8 | 8 | 8 | 8 | 8 |

The oracle selects views using ground-truth parameter error; it is a diagnostic
upper bound, not deployable. It shows that **selection matters more than count**:
8 oracle views beat 25 fixed views for fx.

## 2. Real corpus (CCF-relative, 5 folds)

| selection | k | mean max|Δfx| px | σfx px | held-out px | baseline Δ mm |
|---|---:|---:|---:|---:|---:|
| fixed | 8 | 17.98* | 3.04 | 0.605 | 0.108 |
| fixed | 12 | 3.23 | 1.29 | 0.466 | 0.035 |
| fixed | 15 | 4.21 | 1.01 | 0.460 | 0.030 |
| uniform | 8 | 5.92 | 0.76 | 0.444 | 0.109 |
| uniform | 12 | 4.32 | 0.72 | 0.440 | 0.062 |
| random | 8 | 4.41 | 0.85 | 0.461 | 0.096 |
| random | 12 | 4.75 | 0.73 | 0.446 | 0.041 |
| adaptive | 8 | 5.06 | 0.89 | 0.486 | 0.050 |
| adaptive | 12 | 4.57 | 0.72 | 0.448 | 0.033 |
| full | 25 | 5.12 | 0.52 | 0.456 | 0.014 |

*fixed k=8 has one fold with a gross failure; the mean is not representative of
a good capture set. Δfx vs the CCF is 4–6 px for every non-fixed method and does
**not** converge to zero with k: the CCF is a reference, not ground truth.

## 3. Conclusion

* **8–12 is withdrawn as a universal replacement for 25.** 8–12 can meet a
  **1 px fx** target with good selection (uniform 8, adaptive 12) and they meet
  baseline/held-out targets, but they do **not** meet a **0.5 px** intrinsic
  target: no non-oracle method reaches the UCB95 target even at 25 (mean
  20–25; only oracle 8 does).
* For a **standard 0.75 px** target, 12–20 views are needed depending on
  selection; for a **high 0.35 px** target the fixed design is insufficient at
  25.
* Adaptive selection is consistently better than fixed at equal k (8: 0.89 vs
  1.44 px mean; 12: 0.72 vs 0.92) but does not reach the oracle. A
  coverage-constrained planner is worth keeping; pure A-optimality without
  diversity is not.
* Baseline and held-out reprojection saturate by k≈8; **do not quote a capture
  count without the target, the selection rule and the held-out protocol**.

## 4. Caveats

* The reduced design has 10 scenes/5 seeds; UCB95 is correspondingly wide.
  Random/adaptive differences of <0.2 px are not resolved.
* Synthetic truth is a renderer, not the physical scanner; physical accuracy
  still requires the artefact test (roadmap calibration gates).
* The real CCF-relative numbers cannot rank physical accuracy; they only check
  consistency with the vendor record.
* The full run used the frozen engine; post-freeze fixes affect free_kc=True
  (float32 init) and quality-preset boundaries, not the free_kc=False path used
  for these rows.
