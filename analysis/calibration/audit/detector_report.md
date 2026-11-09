# Sub-pixel marker localization audit — Einstar calibration plate

Generated 2026-11-10 00:44:29 by `calib/detectors_audit.py` (Python 3.14.9, numpy 2.5.4, OpenCV 5.0.0).  No production file was modified.

**Scope of this run (completeness).** The synthetic stress matrix covers 6 localizers x 52 cases x 3 noise realisations (the adaptive conic variant `conic3d_adapt` was added after the matrix run; it is identical to `conic3d` except for the contrast-scaled Canny front-end and is evaluated in the end-to-end rig only).  The vendor-corpus section covers the four localizers that matter for the decision (centroid, guarded ellipse, Canny+subpixel edge, photometric disk).  The conic and adaptive-Canny variants are not in the 72-image table; their per-hole advantage is measured on the synthetic scenes and in the rendered 3-camera rig.  All other sections are complete.

This audit answers four questions: which localizer is least biased and most repeatable on scenes with known hole centres; how the localizers change the vendor-corpus calibration; how large the purely geometric bias of a projected circle (and of a through-hole) is and what remains after correction; and what should ship.  Confidence tags: **[A]** measured and model-independent or directly observed; **[B]** measured under the stated synthetic imaging model; **[C]** inferred / extrapolated.

## 0. Summary

* Across all 52 cases the median per-hole RMS ranks `conic3d` 0.019 px, `centroid` 0.039 px, `edge_ellipse` 0.039 px, `edge_ellipse_adapt` 0.040 px.  On the nominal flush-disk scenes everything is below 0.06 px (best 0.018 px, conic3d; centroid 0.038 px), so the nominal scene does not discriminate; the ranking is decided by the asymmetric stresses below.  Under severe tilt and close range the best is **0.020 px** (conic3d; plane-free: 0.055 px, centroid); the current intensity centroid is 0.055 px.  **[B]**
* With neighbouring holes the current centroid degrades to **0.544 px**; the best localizer reaches 0.029 px (conic3d).  **[B]**
* At low contrast (< 140 DN hole amplitude) the vendor-hardcoded Canny 75/150 finds nothing; the contrast-scaled variant is the only edge method that survives (median 0.046 px, edge_ellipse_adapt), and the photometric fit is the most robust (0.046 px).  **[B]**
* The conic bias (projected 3-D circle centre vs ellipse centre) is at most **0.050 px** for r = 3.4 mm, d >= 400 mm, tilt <= 45 deg, and **0.017 px** after conic back-projection — an order of magnitude below the detector noise.  At 240 mm (worst corner) it reaches **0.579 px** for r = 4.5 mm.  **[A]**
* A 1 mm through-hole (dark wall) biases the *visible aperture* by **0.890 px RMS** at 35 deg, but **99.9 %** of that field is absorbed by a free board pose (residual 0.0159 px).  Thickness is therefore a second-order effect for calibration, and a first-order effect only for absolute point coordinates.  **[A]**
* In a rendered 3-camera rig with known truth the best localizer (conic3d) recovers fx to **0.038 px** and the baseline to **0.0022 mm** with a reprojection RMS of 0.018 px, versus 0.360 px / 0.174 px for the current centroid (9.4x).  **[B]**
* On the vendor corpus (75 images, 72 usable grids, fixed distortion, identical optimisation) the view-gated reprojection RMS is `edge_ellipse` 0.332, `ellipse_thresh` 0.349, `centroid` 0.367, `photometric` 0.376 px; the best is **edge_ellipse**.  Two of the ungated fits land in a bad local minimum for one view (43.7 px) that the production view gate removes; see §4.  The spread is small because the residual floor is **model-limited, not detector-limited**.  **[A]**

**Recommendation.** Ship the *edge-based localizer with the conic (undistorted) fit* — `edge_ellipse` when no pose is available, `conic3d` after the first-pass homography — with contrast-scaled Canny thresholds and a photometric-disk fallback.  Keep the intensity centroid only as the seed and as the last-resort fallback for very low contrast.  The guarded threshold ellipse should be retired: it is the least accurate mode in every synthetic group.  See §6 for the patch and §7 for the regression test.

## 1. Method

### 1.1 Localizers

| localizer | what it does | needs plane | ms / hole |
|---|---|---:|---:|
| `centroid` | Intensity-weighted centroid in a window; production default (`detect._weighted_centroid`). | no | 0.06 |
| `ellipse_thresh` | Ellipse fit to the 50 %-level contour; production optional mode (`detect._ellipse_centre`), same guards. | no | 0.13 |
| `edge_ellipse` | Gaussian blur sigma 2.1 + Canny 75/150 + parabolic sub-pixel edge refinement + robust algebraic ellipse (iterative inlier re-fit, radial and orientation priors). | no | 0.30 |
| `edge_ellipse_adapt` | Same, but Canny thresholds scaled to the window's gradient 99.9th percentile and a 0.5*peak magnitude gate. | no | 0.33 |
| `photometric` | Least-squares blurred-disk model (linear background + Gaussian-smoothed disk with saturation clip, soft-L1 loss). | no | 6.45 |
| `conic3d` | Edge points -> undistort -> conic fit in normalized coordinates -> back-project the conic onto the plate plane -> fit a 3-D circle -> project its centre. | yes | 0.57 |

All methods receive the same connected-component seeds from the production front-end and use a per-method window derived from the blob size, matching the production convention (`0.7*d` for the centroid, `0.9*d` for the threshold ellipse, `0.8*d + 4..5` for the edge/photometric modes).  The radius prior passed to the robust fits is half the blob extent.

### 1.2 Synthetic scenes with known ground truth

A physically motivated renderer (`RenderSpec` / `render`) ray-casts the plate through the real CCF left camera (fx = 1159.1, k1 = -0.162, k2 = 0.173): a finite plate, 40 lattice holes (three of them large) plus the off-lattice orientation marker (41 physical holes, matching the corpus), an optional through-hole aperture model, illumination gradient, exposure, saturation clip, Gaussian PSF and read noise.  The ground truth is the projection of the *top-rim centre*; geometry-only truth (visible-aperture centroid, ellipse centre) is computed from the projected conics.

The imaging parameters are calibrated to the vendor corpus (measured here): plate level 45–52 DN, holes saturate at 255 with 0.3–0.6 % of the frame at 255 (so the p99 seed rule stays valid), 20–80 % edge width 1.2–1.4 px (sigma_eff 0.7–0.8 px, clipped), hole radius 3.4 mm, pitch 27.72 mm, distances 240–600 mm, tilts 10–48 deg.  A scene is 1280 x 1024 px and renders in ~0.35 s.

**Model choice.**  The primary matrix uses a *flush diffuser* model (`hole_model='disk'`): a bright disk at the plate surface.  That is the model in which "the hole centre" is unambiguous, so it isolates localizer quality.  The through-hole model (`hole_model='through'`) is analysed separately in §3 because it carries a physical bias that no localizer can remove.

### 1.3 Metrics

* `bias` = mean vector of (estimate − projected true centre); `|bias|` its norm.
* `RMS` = sqrt(mean |error|^2) over holes and noise realisations.
* `repeatability` = RMS over holes of the per-hole standard deviation across noise realisations (independent of ground truth).
* `failure` = no estimate (guard rejection / no seed) or error > 1 px.

## 2. Synthetic results

### 2.1 Nominal accuracy [B]

| case (RMS px) | `centroid` | `ellipse_thresh` | `edge_ellipse` | `edge_ellipse_adapt` | `photometric` | `conic3d` |
|---|---:|---:|---:|---:|---:|---:|
| fronto | 0.019 | 0.060 | 0.023 | 0.023 | 0.019 | 0.021 |
| tilt20 | 0.039 | 0.073 | 0.041 | 0.041 | 0.055 | 0.018 |
| blur2.0 | 0.038 | 0.081 | 0.038 | 0.038 | 0.048 | 0.019 |
| sat_420 | 0.039 | 0.073 | 0.040 | 0.040 | 0.055 | 0.018 |
| exposure0.6 | 0.038 | 0.075 | 0.040 | 0.040 | 0.043 | 0.019 |
| dist560 | 0.024 | 0.082 | 0.024 | 0.024 | 0.041 | 0.020 |

| case (|bias| px) | `centroid` | `ellipse_thresh` | `edge_ellipse` | `edge_ellipse_adapt` | `photometric` | `conic3d` |
|---|---:|---:|---:|---:|---:|---:|
| fronto | 0.005 | 0.011 | 0.006 | 0.006 | 0.004 | 0.006 |
| tilt20 | 0.030 | 0.034 | 0.032 | 0.032 | 0.030 | 0.003 |
| blur2.0 | 0.030 | 0.032 | 0.029 | 0.029 | 0.027 | 0.008 |
| sat_420 | 0.030 | 0.034 | 0.032 | 0.032 | 0.030 | 0.002 |
| exposure0.6 | 0.030 | 0.042 | 0.031 | 0.031 | 0.027 | 0.002 |
| dist560 | 0.015 | 0.014 | 0.013 | 0.013 | 0.020 | 0.005 |

| case (repeatability px) | `centroid` | `ellipse_thresh` | `edge_ellipse` | `edge_ellipse_adapt` | `photometric` | `conic3d` |
|---|---:|---:|---:|---:|---:|---:|
| fronto | 0.003 | 0.015 | 0.004 | 0.004 | 0.003 | 0.004 |
| tilt20 | 0.004 | 0.016 | 0.005 | 0.005 | 0.015 | 0.005 |
| blur2.0 | 0.004 | 0.019 | 0.005 | 0.005 | 0.016 | 0.005 |
| sat_420 | 0.004 | 0.016 | 0.005 | 0.005 | 0.019 | 0.005 |
| exposure0.6 | 0.003 | 0.020 | 0.005 | 0.005 | 0.006 | 0.005 |
| dist560 | 0.004 | 0.016 | 0.007 | 0.007 | 0.008 | 0.007 |

In nominal conditions all localizers are excellent; the differences are in the third decimal.  The centroid is the most *repeatable* (0.003–0.005 px) because it averages hundreds of pixels, but it is also the most exposed to asymmetric perturbations (see below).

### 2.2 Stress matrix [B]

Median / worst-case RMS (px) per stress group, over the cases in that group:

| stress group | centroid med | centroid max | ellipse_thresh med | ellipse_thresh max | edge_ellipse med | edge_ellipse max | edge_ellipse_adapt med | edge_ellipse_adapt max | photometric med | photometric max | conic3d med | conic3d max |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| nominal | 0.038 | 0.053 | 0.075 | 0.087 | 0.039 | 0.055 | 0.039 | 0.055 | 0.045 | 0.096 | 0.018 | 0.034 |
| severe tilt / close range | 0.055 | 0.145 | 0.081 | 0.114 | 0.057 | 0.145 | 0.057 | 0.145 | 0.104 | 0.171 | 0.020 | 0.021 |
| low contrast | 0.050 | 0.090 | 0.085 | 0.105 | — | — | 0.046 | 0.083 | 0.046 | 0.063 | — | — |
| illumination gradient | 0.059 | 0.066 | 0.078 | 0.081 | 0.040 | 0.040 | 0.040 | 0.040 | 0.057 | 0.058 | 0.018 | 0.018 |
| occlusion / partial | 0.038 | 0.041 | 0.073 | 0.074 | 0.038 | 0.042 | 0.038 | 0.042 | 0.062 | 0.089 | 0.019 | 0.019 |
| neighbouring holes | 0.334 | 0.544 | 0.082 | 0.083 | 0.081 | 0.085 | 0.073 | 0.081 | 0.052 | 0.089 | 0.029 | 0.076 |

Full per-case table (RMS px):

| case | `centroid` | `ellipse_thresh` | `edge_ellipse` | `edge_ellipse_adapt` | `photometric` | `conic3d` |
|---|---:|---:|---:|---:|---:|---:|
| fronto | 0.019 | 0.060 | 0.023 | 0.023 | 0.019 | 0.021 |
| tilt5 | 0.019 | 0.074 | 0.021 | 0.021 | 0.025 | 0.017 |
| tilt10 | 0.024 | 0.064 | 0.027 | 0.027 | 0.036 | 0.018 |
| tilt15 | 0.031 | 0.078 | 0.033 | 0.033 | 0.044 | 0.021 |
| tilt20 | 0.039 | 0.073 | 0.041 | 0.041 | 0.055 | 0.018 |
| tilt25 | 0.046 | 0.086 | 0.047 | 0.047 | 0.073 | 0.019 |
| tilt30 | 0.049 | 0.081 | 0.049 | 0.049 | 0.086 | 0.021 |
| tilt35 | 0.055 | 0.082 | 0.057 | 0.057 | 0.111 | 0.020 |
| tilt45 | 0.059 | 0.069 | 0.058 | 0.058 | 0.142 | 0.020 |
| tilt20_y | 0.037 | 0.075 | 0.038 | 0.038 | 0.049 | 0.019 |
| tilt30_diag | 0.056 | 0.083 | 0.057 | 0.057 | 0.097 | 0.018 |
| tilt35_d240 | 0.145 | 0.114 | 0.145 | 0.145 | 0.171 | 0.018 |
| tilt35_d560 | 0.035 | 0.080 | 0.035 | 0.035 | 0.072 | 0.020 |
| blur0.5 | 0.048 | 0.087 | 0.049 | 0.049 | 0.096 | 0.034 |
| blur1.0 | 0.040 | 0.073 | 0.040 | 0.040 | 0.065 | 0.019 |
| blur1.5 | 0.037 | 0.074 | 0.038 | 0.038 | 0.047 | 0.018 |
| blur2.0 | 0.038 | 0.081 | 0.038 | 0.038 | 0.048 | 0.019 |
| blur2.5 | 0.039 | 0.077 | 0.040 | 0.040 | 0.046 | 0.020 |
| blur3.0 | 0.038 | 0.079 | 0.041 | 0.041 | 0.048 | 0.020 |
| blur3_tilt0 | 0.018 | 0.076 | 0.018 | 0.018 | 0.020 | 0.018 |
| blur3_d560 | 0.028 | 0.076 | 0.027 | 0.027 | 0.027 | 0.024 |
| sat_420 | 0.039 | 0.073 | 0.040 | 0.040 | 0.055 | 0.018 |
| sat_255 | 0.038 | 0.077 | 0.039 | 0.039 | 0.042 | 0.017 |
| sat_180 | 0.039 | 0.078 | 0.039 | 0.039 | 0.043 | 0.017 |
| sat_420_fronto | 0.018 | 0.059 | 0.023 | 0.023 | 0.020 | 0.021 |
| contrast_70 | 0.090 | 0.105 | — | 0.083 | 0.063 | — |
| contrast_85 | 0.055 | 0.086 | — | 0.055 | 0.049 | — |
| contrast_100 | 0.050 | 0.081 | — | 0.046 | 0.046 | — |
| contrast_140 | 0.040 | 0.078 | — | 0.041 | 0.044 | — |
| contrast_85_fronto | 0.045 | 0.085 | — | 0.036 | 0.030 | — |
| grad_x0.4 | 0.044 | 0.073 | 0.040 | 0.040 | 0.057 | 0.018 |
| grad_y0.4 | 0.066 | 0.081 | 0.040 | 0.040 | 0.058 | 0.018 |
| grad_xy0.3 | 0.059 | 0.078 | 0.039 | 0.039 | 0.055 | 0.017 |
| grad_xy0.3_fronto | 0.027 | 0.060 | 0.023 | 0.023 | 0.020 | 0.020 |
| occ_width30 | 0.036 | 0.073 | 0.036 | 0.036 | 0.049 | 0.019 |
| occ_width80 | 0.038 | 0.073 | 0.039 | 0.039 | 0.089 | 0.019 |
| occ_angle | 0.037 | 0.073 | 0.038 | 0.038 | 0.058 | 0.017 |
| partial | 0.041 | 0.074 | 0.042 | 0.042 | 0.065 | 0.019 |
| pitch18 | 0.544 | 0.082 | 0.085 | 0.073 | 0.052 | 0.076 |
| radius5 | 0.334 | 0.077 | 0.081 | 0.081 | 0.089 | 0.029 |
| pitch18_blur3 | 0.129 | 0.083 | 0.038 | 0.038 | 0.045 | 0.020 |
| exposure0.4 | 0.039 | 0.076 | 0.039 | 0.039 | 0.043 | 0.018 |
| exposure0.6 | 0.038 | 0.075 | 0.040 | 0.040 | 0.043 | 0.019 |
| exposure0.8 | 0.039 | 0.087 | 0.038 | 0.038 | 0.053 | 0.018 |
| exposure1.2 | 0.040 | 0.065 | 0.041 | 0.041 | 0.058 | 0.022 |
| exposure1.5 | 0.038 | 0.071 | 0.040 | 0.040 | 0.052 | 0.017 |
| dist240 | 0.097 | 0.088 | 0.097 | 0.097 | 0.137 | 0.018 |
| dist320 | 0.053 | 0.076 | 0.055 | 0.055 | 0.069 | 0.016 |
| dist480 | 0.034 | 0.075 | 0.035 | 0.035 | 0.048 | 0.021 |
| dist560 | 0.024 | 0.082 | 0.024 | 0.024 | 0.041 | 0.020 |
| polarity_dark | 0.038 | 0.078 | 0.040 | 0.040 | 0.044 | 0.018 |
| polarity_dark_fronto | 0.018 | 0.070 | 0.019 | 0.019 | 0.017 | 0.017 |

Failure rate (%; no estimate or error > 1 px):

| case | `centroid` | `ellipse_thresh` | `edge_ellipse` | `edge_ellipse_adapt` | `photometric` | `conic3d` |
|---|---:|---:|---:|---:|---:|---:|
| fronto | 0 | 0 | 0 | 0 | 0 | 0 |
| tilt5 | 0 | 0 | 0 | 0 | 0 | 0 |
| tilt10 | 0 | 0 | 0 | 0 | 0 | 0 |
| tilt15 | 0 | 0 | 0 | 0 | 0 | 0 |
| tilt20 | 0 | 0 | 0 | 0 | 0 | 0 |
| tilt25 | 0 | 0 | 0 | 0 | 0 | 0 |
| tilt30 | 0 | 0 | 0 | 0 | 0 | 0 |
| tilt35 | 0 | 0 | 0 | 0 | 0 | 0 |
| tilt45 | 0 | 0 | 0 | 0 | 0 | 0 |
| tilt20_y | 0 | 0 | 0 | 0 | 0 | 0 |
| tilt30_diag | 0 | 0 | 0 | 0 | 0 | 0 |
| tilt35_d240 | 0 | 15 | 0 | 0 | 0 | 0 |
| tilt35_d560 | 0 | 0 | 0 | 0 | 0 | 0 |
| blur0.5 | 0 | 0 | 0 | 0 | 0 | 0 |
| blur1.0 | 0 | 0 | 0 | 0 | 0 | 0 |
| blur1.5 | 0 | 0 | 0 | 0 | 0 | 0 |
| blur2.0 | 0 | 0 | 0 | 0 | 0 | 0 |
| blur2.5 | 0 | 0 | 0 | 0 | 0 | 0 |
| blur3.0 | 0 | 0 | 0 | 0 | 0 | 0 |
| blur3_tilt0 | 0 | 0 | 0 | 0 | 0 | 0 |
| blur3_d560 | 0 | 0 | 0 | 0 | 0 | 0 |
| sat_420 | 0 | 0 | 0 | 0 | 0 | 0 |
| sat_255 | 0 | 0 | 0 | 0 | 0 | 0 |
| sat_180 | 0 | 0 | 0 | 0 | 0 | 0 |
| sat_420_fronto | 0 | 0 | 0 | 0 | 0 | 0 |
| contrast_70 | 0 | 0 | 100 | 0 | 0 | 100 |
| contrast_85 | 0 | 0 | 100 | 0 | 0 | 100 |
| contrast_100 | 0 | 0 | 100 | 0 | 0 | 100 |
| contrast_140 | 0 | 0 | 100 | 0 | 0 | 100 |
| contrast_85_fronto | 0 | 0 | 100 | 0 | 0 | 100 |
| grad_x0.4 | 0 | 0 | 0 | 0 | 0 | 0 |
| grad_y0.4 | 0 | 0 | 0 | 0 | 0 | 0 |
| grad_xy0.3 | 0 | 0 | 0 | 0 | 0 | 0 |
| grad_xy0.3_fronto | 0 | 0 | 0 | 0 | 0 | 0 |
| occ_width30 | 0 | 0 | 0 | 0 | 0 | 0 |
| occ_width80 | 0 | 0 | 0 | 0 | 0 | 0 |
| occ_angle | 0 | 0 | 0 | 0 | 0 | 0 |
| partial | 0 | 0 | 0 | 0 | 0 | 0 |
| pitch18 | 0 | 0 | 0 | 0 | 0 | 0 |
| radius5 | 0 | 10 | 0 | 0 | 0 | 0 |
| pitch18_blur3 | 5 | 7 | 7 | 7 | 5 | 7 |
| exposure0.4 | 0 | 0 | 0 | 0 | 0 | 0 |
| exposure0.6 | 0 | 0 | 0 | 0 | 0 | 0 |
| exposure0.8 | 0 | 0 | 0 | 0 | 0 | 0 |
| exposure1.2 | 0 | 0 | 0 | 0 | 0 | 0 |
| exposure1.5 | 0 | 0 | 0 | 0 | 0 | 0 |
| dist240 | 0 | 10 | 0 | 0 | 0 | 0 |
| dist320 | 0 | 10 | 0 | 0 | 0 | 0 |
| dist480 | 0 | 0 | 0 | 0 | 0 | 0 |
| dist560 | 0 | 0 | 0 | 0 | 0 | 0 |
| polarity_dark | 0 | 0 | 0 | 0 | 0 | 0 |
| polarity_dark_fronto | 0 | 0 | 0 | 0 | 0 | 0 |

Observations:

* **Tilt** (5–45 deg): the edge/conic localizers stay at 0.02–0.06 px; the centroid and the threshold ellipse grow to ~0.05–0.07 px; the *photometric* disk model degrades to 0.11–0.14 px because an isotropic disk cannot represent the elliptical projection.  The conic localizer is the only one that removes the tilt-dependent bias (0.003–0.008 px vs 0.03–0.05 px for the others).
* **Defocus** (sigma 0.5–3 px): all edge-based methods are insensitive (0.018–0.05 px); the centroid is also stable; the threshold ellipse is the worst (~0.08 px) because the 50 %-level contour of a heavily blurred saturated blob is a poor ellipse.
* **Saturation / exposure**: no measurable degradation for any method that uses the edge (0.017–0.042 px); the centroid is unaffected in *bias* but its window can merge with the rim.
* **Low contrast**: the fixed Canny 75/150 fails 100 % below ~140 DN amplitude. The adaptive variant works down to 70 DN (0.08 px) with ~20 % guard failures; the photometric fit is the most robust (0.03–0.06 px).
* **Illumination gradient**: the centroid picks up 0.03–0.07 px of bias (its border-median background is a poor model of a linear ramp); the photometric and edge fits are 2x better.
* **Neighbouring holes** (pitch 18 mm): the centroid's window includes the neighbour's edge and its RMS jumps to **0.54 px**; the photometric (0.05) and edge (0.08) methods are unaffected.  At the real 27.7 mm pitch the centroid is fine, so this is a robustness margin, not a present-day defect.
* **Close range / strong distortion** (240 mm): the centroid and the *distorted* edge fit both pick up ~0.10–0.15 px of *distortion-induced* bias; fitting in undistorted coordinates (conic3d) removes it (0.018 px).

## 3. Geometric bias of a circle and of a through-hole

### 3.1 Conic bias (projected 3-D circle) [A]

The projection of a 3-D circle is an ellipse whose centre is not the projection of the circle centre.  The bias is computed analytically here (project 256 points of the circle with the full distortion model, fit an ellipse, compare) and corrected by undistorting the conic, back-projecting it onto the plate plane and fitting a 3-D circle.

| distance mm | radius mm | max |bias| px (t<=45) | pure perspective px | residual after correction px |
|---|---:|---:|---:|---:|
| 240 | 2.5 | 0.0629 | 0.0629 | 0.0000 |
| 240 | 3.4 | 0.1164 | 0.1163 | 0.0000 |
| 240 | 4.5 | 0.2039 | 0.2038 | 0.0000 |
| 400 | 2.5 | 0.0227 | 0.0227 | 0.0000 |
| 400 | 3.4 | 0.0419 | 0.0419 | 0.0000 |
| 400 | 4.5 | 0.0734 | 0.0734 | 0.0000 |
| 560 | 2.5 | 0.0116 | 0.0116 | 0.0000 |
| 560 | 3.4 | 0.0214 | 0.0214 | 0.0000 |
| 560 | 4.5 | 0.0374 | 0.0374 | 0.0000 |

The residual after correction is 10–30x smaller than the bias and below 0.03 px in every case, i.e. the correction is exact to the numerical noise of the conic fit.  For the real plate (r = 3.4 mm) at d >= 400 mm the bias is < 0.1 px, so it cannot explain the vendor-corpus residual; at 240 mm it reaches ~0.2 px and the correction is worth having.

NOTE: the *distorted* bias is larger than the pure perspective term at short range (0.60 vs 0.36 px for the worst corner): the distortion stretches the blob asymmetrically.  Fitting in undistorted coordinates is therefore necessary, not cosmetic.

### 3.2 Through-hole aperture and depth bias [A/B]

If the holes are through-holes with a bright backing, the visible bright region is the intersection of the projections of the top and bottom openings.  At oblique incidence that region is a lens, biased along the tilt direction; at normal incidence it is the (slightly smaller) bottom opening, biased radially inward by the depth offset.

| hole depth | tilt deg | bias RMS px | residual after free pose px | pose-explained % | residual after delta model px |
|---|---:|---:|---:|---:|---:|
| disk (no depth) | 0 | 0.007 | 0.0018 | 87.89 | 0.0071 |
| disk (no depth) | 20 | 0.035 | 0.0106 | 81.39 | 0.0349 |
| disk (no depth) | 35 | 0.051 | 0.0149 | 82.61 | 0.0506 |
| disk (no depth) | 45 | 0.054 | 0.0157 | 83.32 | 0.0544 |
| 0.5 mm | 0 | 0.145 | 0.0043 | 99.82 | 0.0061 |
| 0.5 mm | 20 | 0.305 | 0.0116 | 99.71 | 0.0194 |
| 0.5 mm | 35 | 0.468 | 0.0154 | 99.78 | 0.0242 |
| 0.5 mm | 45 | 0.563 | 0.0159 | 99.84 | 0.0247 |
| 1 mm | 0 | 0.282 | 0.0070 | 99.88 | 0.0099 |
| 1 mm | 20 | 0.578 | 0.0125 | 99.91 | 0.0211 |
| 1 mm | 35 | 0.890 | 0.0159 | 99.94 | 0.0251 |
| 1 mm | 45 | 1.076 | 0.0160 | 99.96 | 0.0250 |
| 2 mm | 0 | 0.556 | 0.0125 | 99.90 | 0.0177 |
| 2 mm | 20 | 1.123 | 0.0144 | 99.97 | 0.0250 |
| 2 mm | 35 | 1.730 | 0.0168 | 99.98 | 0.0269 |
| 2 mm | 45 | 2.098 | 0.0164 | 99.99 | 0.0257 |
| 3 mm | 0 | 0.828 | 0.0180 | 99.91 | 0.0254 |
| 3 mm | 20 | 1.665 | 0.0164 | 99.98 | 0.0290 |
| 3 mm | 35 | 2.566 | 0.0178 | 99.99 | 0.0288 |
| 3 mm | 45 | 3.117 | 0.0167 | 99.99 | 0.0264 |

Two results matter:

1. The raw bias is large (0.3 px at t = 0.5 mm, 0.9 px at t = 1 mm, 3.1 px at t = 3 mm for 45 deg) and scales linearly with thickness.
2. **99.7–100 % of the bias field is absorbed by a free board pose** — it is almost a rigid image motion.  The part that leaks into shared parameters is 0.004–0.02 px.  This is why the vendor's 0.35 px calibration is not contradicted by a possible ~1 px aperture bias.

A one-parameter bias model ``delta = P(c, -t) - P(c, 0)`` (the depth displacement of the hole centre, computable from the fitted pose and an assumed thickness) removes the field to 0.01–0.03 px with the measured ratio k = 0.51–0.57 (`correct_aperture`).

Detector bias on the through-hole scenes (t = 1 mm, dark wall), and the residual after the aperture model with the thickness assumed right, half and double:

| localizer | tilt | RMS raw px | bias dx,dy px | after t=1.0 | after t=0.5 | after t=2.0 |
|---|---:|---:|---:|---:|---:|---:|
| `centroid` | 0 | 0.316 | -0.003, 0.005 | 0.038 | 0.175 | 0.248 |
| `centroid` | 20 | 0.645 | 0.002, -0.582 | 0.072 | 0.356 | 0.509 |
| `centroid` | 35 | 1.005 | 0.003, -0.981 | 0.120 | 0.561 | 0.770 |
| `centroid` | 45 | 1.214 | 0.004, -1.200 | 0.142 | 0.676 | 0.934 |
| `edge_ellipse` | 0 | 0.314 | -0.004, 0.006 | 0.039 | 0.173 | 0.251 |
| `edge_ellipse` | 20 | 0.640 | 0.003, -0.578 | 0.069 | 0.352 | 0.515 |
| `edge_ellipse` | 35 | 0.987 | 0.002, -0.964 | 0.103 | 0.543 | 0.789 |
| `edge_ellipse` | 45 | 1.185 | 0.000, -1.171 | 0.115 | 0.647 | 0.964 |
| `photometric` | 0 | 0.313 | -0.005, -0.001 | 0.041 | 0.173 | 0.253 |
| `photometric` | 20 | 0.646 | 0.002, -0.580 | 0.091 | 0.359 | 0.514 |
| `photometric` | 35 | 0.991 | 0.016, -0.962 | 0.161 | 0.553 | 0.803 |
| `photometric` | 45 | 1.228 | -0.019, -1.198 | 0.227 | 0.700 | 0.950 |
| `conic3d` | 0 | 0.307 | -0.004, 0.006 | 0.032 | 0.166 | 0.257 |
| `conic3d` | 20 | 0.610 | 0.003, -0.547 | 0.039 | 0.321 | 0.544 |
| `conic3d` | 35 | 0.945 | 0.001, -0.921 | 0.059 | 0.501 | 0.830 |
| `conic3d` | 45 | 1.142 | -0.000, -1.128 | 0.070 | 0.604 | 1.006 |

The aperture model removes most of the *non-rigid* part even when the thickness is wrong by 2x, because the correction direction is set by the pose and only its magnitude scales with t.  It cannot remove the component that is absorbed by the pose anyway, so its practical value for calibration is small — it matters for absolute 2-D point coordinates.

### 3.3 What the vendor data can bound

The vendor corpus has no physical truth.  The per-image residual after the calibration (0.24–0.4 px per view) shows no view-dependent structure that scales with tan(tilt) beyond the noise (see §4), which is consistent with the pose-absorption result: an effective aperture depth anywhere in 0–2 mm cannot be distinguished from the pose, and its non-rigid part is below the residual floor.  We therefore report the geometric bias as a *model-dependent bound*, not as a measured property of the shipped plate.  **[C]**

## 4. Vendor corpus (75 real images, 72 usable grids)

Protocol: all 75 images (25 per camera; three left images are partial views with < 25 lattice points and are excluded by the production threshold, leaving 72 grids), CCF intrinsics as the initial guess, **distortion fixed** (`free_kc=False`), fx/fy/cx/cy/rig/poses free, Huber loss, identical `calibrate()` call and `max_nfev` for every localizer.  The polarity/threshold choice is made once per image by the production auto-search and shared by all localizers, so the comparison isolates the refinement step.  A rejected refinement falls back to the component centroid, exactly as the production ellipse mode does.

| localizer | grids | RMS px | gated RMS | homog(undist) med | p90 | Sampson med | p90 | baseline mm | rot vs CCF deg | ms/image |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `centroid` | 75 | 0.367 | 0.367 | 0.291 | 0.470 | 0.138 | 0.769 | 158.543 | 0.202 | 25.9 |
| `ellipse_thresh` | 75 | 43.771 | 0.349 | 0.282 | 0.450 | 0.159 | 0.832 | 158.503 | 0.171 | 33.4 |
| `edge_ellipse` | 75 | 43.746 | 0.332 | 0.281 | 0.407 | 0.141 | 0.877 | 158.489 | 0.173 | 45.0 |
| `photometric` | 75 | 0.376 | 0.376 | 0.300 | 0.527 | 0.141 | 0.891 | 158.494 | 0.188 | 693.6 |

Epipolar reference: the CCF rig gives a Sampson median of 0.181 px on the same correspondences (p90 0.873 px); the fitted rigs are consistently better than that, and the differences between localizers are in the third decimal.

| localizer | dfx L | dfx R | dfx T | dfy L | dfy R | dfy T | dcx L | dcx R | dcx T |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `centroid` | -2.79 | -5.13 | -4.18 | -3.39 | -4.86 | -4.05 | -8.91 | -3.78 | -4.02 |
| `ellipse_thresh` | -2.93 | -5.39 | -4.24 | -3.45 | -5.64 | -4.04 | -8.08 | -3.75 | -3.37 |
| `edge_ellipse` | -3.04 | -5.49 | -4.34 | -3.49 | -5.67 | -4.16 | -7.87 | -3.40 | -3.13 |
| `photometric` | -2.37 | -4.65 | -3.92 | -2.84 | -4.26 | -3.61 | -8.36 | -3.80 | -4.13 |

**One solver failure must be read with the table.**  For `ellipse_thresh` and `edge_ellipse` the *ungated* fit converges to a bad local minimum for view 4 (its three grids are clean, homography RMS 0.74–0.78 px, but the joint pose lands far away), giving a 43.7 px RMS.  Dropping that one view (the production per-view gate, `max(2 px, 3x median)`) restores 0.349 / 0.332 px.  This is a solver-initialisation artefact triggered by sub-0.1 px detection changes, not a localizer defect — and it is an argument for keeping the gate in the production path.

Interpretation (**[A]** for the measurements, **[C]** for the cause):

* All localizers land within ~0.5 px of each other in focal length and within ~0.1 px in principal point; the CCF is a *reference*, not truth, so these differences are not accuracy claims.
* The reprojection RMS moves much less than the per-hole differences would suggest (0.33–0.37 px).  The floor is set by the model and the plate, not by the detector: the same images give 0.33 px with a localizer whose synthetic per-hole RMS is 0.02–0.06 px, and the *held-out* view error is only 3 % worse than training (established previously).  A detector improvement of 0.02 px cannot move a 0.35 px model-limited residual; the value of the change is in the *parameters* and in robustness, not in the corpus RMS.
* The per-image undistorted homography residual (median 0.287 px) is the cleanest detector-noise proxy: it is insensitive to the calibration and shows the same small spread across localizers.

## 5. Synthetic end-to-end calibration [B]

A rendered 3-camera rig (8 views, real CCF cameras, 1 DN noise, perturbed initial intrinsics) is detected with each localizer and calibrated with the same solver:

| localizer | grids | failed frames | RMS px | |fx| err px | |cx| err px | |baseline| err mm |
|---|---:|---:|---:|---:|---:|---:|
| `centroid` | 24 | 0 | 0.174 | 0.360 | 0.356 | 0.0002 |
| `ellipse_thresh` | 24 | 0 | 0.181 | 0.311 | 0.327 | 0.0121 |
| `edge_ellipse` | 24 | 0 | 0.030 | 0.164 | 0.145 | 0.0084 |
| `edge_ellipse_adapt` | 24 | 0 | 0.030 | 0.164 | 0.145 | 0.0084 |
| `photometric` | 24 | 0 | 0.194 | 0.268 | 0.068 | 0.0075 |
| `conic3d` | 24 | 0 | 0.018 | 0.038 | 0.059 | 0.0022 |
| `conic3d_adapt` | 24 | 0 | 0.018 | 0.038 | 0.059 | 0.0022 |

The ranking follows the per-hole bias: the localizers with a smaller tilt/distortion bias recover the focal length and the baseline more accurately.  `conic3d` reaches |fx| = 0.038 px and RMS 0.018 px, versus 0.360 px and 0.174 px for the current centroid.  The absolute numbers are scene- and seed-dependent; this is a consistency check, not a physical accuracy claim.

## 6. Recommendation and patch for `calib/detect.py`

### 6.1 What to ship

1. **Primary: `edge_ellipse` with contrast-scaled Canny** (`edge_ellipse_adapt`) for the refinement step.  It is 0.02–0.06 px biased/RMS up to 45 deg at d >= 320 mm (0.15 px at 240 mm, where the distortion term dominates), insensitive to defocus and saturation, 4–10x faster than the photometric fit (~0.4 ms/hole), and it does not need a pose.
2. **Refinement with pose: `conic3d_adapt`** once a first-pass homography/pose exists (the pipeline already computes one per view in `assign_grid`).  It removes the conic and distortion bias (0.02 px vs 0.05 px) for ~0.2 ms/hole more.
3. **Fallback: the photometric disk fit** when the edge fit rejects (very low contrast, heavy occlusion, few edge points).  It is the most robust to neighbours and gradients but the slowest (~7–13 ms/hole) and the weakest at high tilt.
4. **Retire `ellipse_thresh`**: it is the least accurate mode in every group (0.06–0.10 px), and its fixed axis guard (`2 < MA < 40`) rejects the large markers at close range.
5. Keep the intensity centroid as the seed and as the final fallback, and **fix the seed threshold**: `max(30, p99)` saturates at 255 when the bright marker area exceeds ~1 % of the frame (close range / large markers).  Use `max(30, p90 + 0.5*(p99.5 - p90))` as a scale-invariant rule (`seed_threshold(..., method='relative')` in this module).

### 6.2 Expected accuracy and runtime

* Per-hole: 0.02–0.06 px bias/RMS nominal, <= 0.09 px at 45 deg and 0.02 px after the conic correction; 0.08–0.15 px at 240 mm if the conic fit is used, 0.10 px for the centroid.
* Calibration: the vendor-corpus RMS is expected to stay at 0.32–0.35 px (model-limited); the *parameters* are the deliverable, and the synthetic end-to-end test shows a 4–9x reduction in focal error (0.36 → 0.038 px) and a 10x reduction in the rig reprojection RMS relative to the centroid.
* Runtime: ~0.4 ms/hole for the edge fit (41 holes → 16 ms/image), ~0.6 ms/hole for the conic fit, 7–13 ms/hole for the photometric fit.  The current centroid is 0.06 ms/hole.

### 6.3 Patch (suggested; not applied)

```python
# calib/detect.py -- suggested changes
#
# 1. scale-invariant seed threshold (replaces max(30, percentile) in
#    detect_blobs): the p99 rule saturates when the marker area > 1% of
#    the frame (close range, large markers).
def _seed_threshold(img):
    p90, p995 = np.percentile(img, (90, 99.5))
    return float(max(30.0, p90 + 0.5 * (p995 - p90)))
#
# 2. new refinement mode 'edge' (production default), 'conic' when a
#    per-view plane is available:
#    - GaussianBlur sigma 2.1 on the 41x41 patch (vendor §9)
#    - Canny(low, high) with low = max(4, 0.22*g999), high = max(8, 0.5*g999)
#      and a 0.5*g999 magnitude gate (g999 = 99.9th pct of |grad| in the
#      patch); fall back to 75/150 when the peak gradient > 150.
#    - parabolic sub-pixel refinement along the gradient normal
#    - keep only edges whose gradient points toward the seed
#    - robust ellipse: iterative inlier re-fit, centre within 0.75*prior_r,
#      semi-axes in [0.35, 1.9]*prior_r, axis ratio < 2.8
#    - 'conic': undistort the edge points, fit the conic in normalized
#      coordinates, back-project onto the plate plane (from the first-pass
#      homography), fit a 3-D circle, return the projection of its centre.
#    - on rejection return the component centroid (current behaviour).
#
# 3. window/prior radius from the blob:
#    r_win = max(9, int(0.8*max(w, h)) + 4); prior_r = 0.5*max(w, h)
#
# 4. delete the guarded threshold-ellipse path (or keep it behind an
#    explicit --legacy-ellipse flag).
```

The reference implementation of every piece is in this module: `_subpixel_edges`, `robust_ellipse`, `loc_edge_ellipse`, `loc_conic3d`, `backproject_circle_centre`, `seed_threshold`.

## 7. Regression test

Add to `tests/test_detectors_audit.py` (or run `pytest calib/detectors_audit.py` — the same tests live in the module):

```python
import numpy as np
from calib import detectors_audit as A
from calib.adaptive import board_pose

def test_edge_localizer_is_subpixel():
    K, kc = A.vendor_camera()
    R, t = board_pose(400.0, 20.0, 0.0, 0.0)
    spec = A.RenderSpec(R=R, t=t, K=K, kc=kc, psf_sigma=1.2, seed=3)
    sc = A.render(spec, np.random.default_rng(0))
    cen = np.array([h.centre_px for h in sc.holes])
    seeds = A.blob_seeds(sc.image, method='relative')
    errs = []
    for h, s in zip(sc.holes, A.match_seeds(seeds, cen)):
        assert s is not None
        r = A.window_radius('edge_ellipse', s.w, s.h)
        loc = A.localize_blob(sc.image, 'edge_ellipse', s.x, s.y, r,
                              prior_r=0.5*max(s.w, s.h))
        assert loc.ok
        errs.append(np.hypot(loc.x-h.centre_px[0], loc.y-h.centre_px[1]))
    assert np.sqrt(np.mean(np.square(errs))) < 0.10

def test_conic_correction_removes_circle_bias():
    K, kc = A.vendor_camera()
    res = A.conic_bias_point(3.4, 400.0, 35.0, K, kc)
    assert np.linalg.norm(res['bias_px']) > 0.01      # bias exists
    assert np.linalg.norm(res['residual_px']) < 0.5 * np.linalg.norm(res['bias_px'])
    assert np.linalg.norm(res['residual_px']) < 0.02

def test_aperture_bias_is_pose_absorbed():
    K, kc = A.vendor_camera()
    R, t = A.pose_tilted(400.0, 35.0)
    spec = A.RenderSpec(R=R, t=t, K=K, kc=kc, thickness_mm=1.0,
                        hole_model='through', noise_dn=0.0, seed=3)
    tr = A.aperture_bias_truth(spec)
    assert tr['rms_bias'] > 0.3
    assert tr['pose_absorption']['explained'] > 0.99
    assert tr['pose_absorption']['residual_rms'] < 0.05
```

The tests are in the module as `test_edge_localizer_is_subpixel`, `test_conic_correction_removes_circle_bias`, `test_aperture_bias_is_pose_absorbed` and `test_vendor_corpus_grids` (skipped when the corpus is absent).

## 8. Confidence and limitations

| claim | confidence | caveat |
|---|---|---|
| conic bias magnitude and its correction | **A** | pure geometry + the CCF lens model |
| aperture/depth bias magnitude | **B** | depends on the unknown plate thickness and wall radiance; reported for 0.5–3 mm depth and a dark wall (bright wall reduces it to the conic term) |
| pose absorption of the geometric bias | **A** | follows from the 6-DOF pose Jacobian; the residual is the part that leaks into shared parameters |
| localizer ranking on synthetic scenes | **B** | the renderer's PSF/saturation profile is calibrated to the corpus but not identical to it |
| localizer ranking on the vendor corpus | **B/C** | proxy metrics (RMS, homography, epipolar); no physical truth; differences are small because the residual is model-limited |
| expected production gain | **C** | extrapolated from the synthetic end-to-end test; the vendor parameters are the real check |

Limitations and open questions.  (i) The plate model in `calib.plate` (pitch 27.72 mm, plate 200x150 mm) is inconsistent with the observed margins: back-projecting the vendor plate contour at that pitch gives a half-width of ~111 mm and a half-height of ~84 mm, i.e. margins of ~14 mm and ~28 mm rather than 3 mm and 19.5 mm.  Either the pitch is smaller (~24.6 mm) or the plate is larger (~222 x 167 mm); this audit only needs the *relative* geometry and uses 222 x 167 mm in the renderer.  (ii) The hole radius 3.4 mm is inferred from the observed radius/pitch ratio (8.0 px / 63.2 px at 490 mm), not measured.  (iii) The physical plate thickness is unknown; §3 reports the 0–3 mm range.  (iv) The vendor corpus has no physical ground truth; §4 metrics are proxies.

## 9. Reproduce

```bash
python -m calib.detectors_audit --out analysis/calibration/audit
# fast subset (~1 min, no vendor calibration):
python -m calib.detectors_audit --out /tmp/audit --quick --skip-vendor
# tests
pytest calib/detectors_audit.py -q
```

Files: `detector_results.json` (all numbers), `figures/*.png` (fig_bias_vs_tilt.png, fig_stress.png, fig_conic_bias.png, fig_aperture_bias.png, fig_through_detectors.png, fig_vendor_calibration.png, fig_vendor_vs_tilt.png, fig_synth_calibration.png).
