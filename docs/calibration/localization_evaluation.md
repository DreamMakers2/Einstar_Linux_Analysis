# Sub-pixel marker localisation for the Einstar plate — methods, benchmarks, recommendation

Agent: `localize` (detector/localisation specialist), round 2.
Repo files added (no existing file modified): `calib/localize.py`, `tests/test_localize.py`.
Scratch, benchmark scripts and JSON results: `/tmp/einstar_cal2/agents/localize/`.

Confidence tags used throughout: **[P]** measured/directly observed on the shipped
artefacts or on rendered scenes with known truth (proof for the stated protocol),
**[I]** inferred from a model or from the established plate geometry, **[U]** unknown /
not measurable with the available data.

---

## 0. TL;DR

**Ship `auto` (or `conic_perspective`) for offline calibration and `centroid_bg` for
live guidance, with one `auto` pass on the frozen dwell frame.** Numbers that
justify it (all measured here): real-corpus undistorted leave-one-out residual
**0.247–0.249 px** for `conic`/`conic_perspective`/`auto` vs 0.281 (`centroid`) and
0.275 (`ellipse`); split-half repeatability **0.0255 px** (`conic`) vs 0.094
(`ellipse`); per-marker disagreement against the ensemble **0.009–0.011 px** vs
0.082–0.085; synthetic ground-truth |bias| **≤ 0.006 px** for `auto`/`conic_perspective`
over 0–45° tilt and RMS 0.021–0.028 px, vs 0.105–0.143 px for `ellipse`; stereo 3-D
error after a rigid fit **24–36 µm** vs 112–162 µm (`ellipse`); cost **1.30 ms/marker**
(`auto`) and **0.36 ms/marker** (`centroid_bg`, 26 % of one core for 3 cameras ×
12 markers at 20 fps). The perspective correction removes a 0.39 px bias when the
marker is large (synthetic validation) but on the *shipped* plate it is only
0.004–0.016 px — below the noise — so it is kept for correctness, not because it
moves the corpus numbers. The through-hole aperture bias (0.5–0.83 px at 20–35°)
is physical, shared by all localisers and pose-absorbed; it is documented, not fixed.

## 1. What was asked, what was built

The production detector localises the 41 circular markers with a thresholded intensity
centroid (`calib/detect._weighted_centroid`) or a guarded ellipse fit
(`_ellipse_centre`). This work adds a self-contained refinement module with seven
localisers, a per-marker uncertainty and quality, an explicit failure reason, both
polarities, and a grid-level API.

| method | what it does | needs | cost / marker [P] |
|---|---|---|---:|
| `centroid` | production centroid, bit-identical to `detect._weighted_centroid` (asserted in the tests) | — | 0.21 ms |
| `centroid_bg` | circular-window centroid after a **robust local plane** background fitted on an annulus outside the PSF shoulder; saturated pixels flagged | — | 0.36 ms |
| `ellipse` | adaptive 50 % threshold → contour → `cv2.fitEllipse` (legacy outline centre, distorted pixel space) | — | 0.17 ms |
| `gradient` | orthogonal-gradient edge fit at a circle model: radial derivative peak per ray (0.25 px, parabolic), robust circle (Kasa + IRLS) | — | 0.46 ms |
| `conic` | blur → adaptive Canny → parabolic sub-pixel edges → robust direct conic (Halir–Flusser) **in undistorted normalised coordinates** → algebraic centre reprojected | `K`, `kc` | 0.83 ms |
| `conic_perspective` | `conic` + back-projection of the fitted conic onto the marker plane (normal from the local homography or a supplied pose), 3-D circle fit, projection of the circle centre | `K`, `kc`, plane | 1.05 ms |
| `auto` | per-marker selection/fusion: corrected centroid vs the best edge estimate, inverse-variance fusion only on tight agreement, explicit fallback | `K`, `kc` (plane optional) | 1.30 ms |

Costs are median per-call times on the vendor corpus (n = 2786 markers per method,
one core, Opencv 5.0.0, NumPy 2.5.4). `refine_grid(..., iterations=2)` with the
leave-one-out residual costs 20 ms (centroid) to 110 ms (auto) per 1280×1024 image [P].

API (see the module docstring for the full contract):

```python
r = localize_marker(gray, x, y, r_px, method="auto", K=K, kc=kc,
                    plane={"normal": n})       # or plane={"R": R, "t": t}
# -> {u, v, sigma_px, method, quality, n_edges, ok, reason, polarity,
#     radius_px, contrast, saturated, partial, coverage, correction,
#     circularity, runtime_ms, ...}
ref = refine_grid(gray, obs_or_xy, method="conic_perspective", K=K, kc=kc)
# -> GridRefinement(points {uv: pixel}, records {uv: dict}, homography, rms,
#                    plane_normal, stats)
new_obs = refine_observation(gray, obs, method="auto", K=K, kc=kc)  # GridObservation
```

Every returned dict carries `sigma_px` (predicted 1-σ **random** error), `quality` in
[0, 1], `n_edges`, `ok` and a machine-readable `reason`
(`ok`, `low_contrast`, `outside_image`, `few_edges`, `conic_rejected`,
`no_plane`, `bad_circularity`, `fused`, `disagree_edge`, …).

### 1.1 Physics that the methods exploit or must fight [I]

* The projection of a 3-D circle is a conic whose **algebraic centre is not the
  projection of the circle centre**; the offset is
  `~(f/2)(r/d)^2 sin t cos t`. For the *measured* plate geometry
  (r = 1.77 mm, d = 240–610 mm, r_px = 3.3–7.1 px) this is **0.004–0.016 px** [I].
  It reaches 0.39 px only for a 5.9 mm-radius marker at 250 mm (a synthetic stress
  case, §3.5). `conic_perspective` removes it exactly.
* The **measured marker radius is 1.77 mm, not 3.4 mm** [I]: `r_px·Z/f` over the
  corpus gives 1.77 mm at the established 27.72–28 mm pitch (r/pitch = 0.064),
  consistent across 25 poses and 3 cameras. The earlier audit's "r = 3.4 mm" appears
  to be the *diameter*; with r = 3.4 mm radius every perspective effect would be
  4× larger than what the data support.
* The markers are **through-holes** in the plate: the visible bright region is the
  intersection of the top and bottom aperture projections, biased along the tilt
  direction by `≈ t·tan(t)/pixel_scale`. A 1 mm thickness gives 0.48 px (20°) and
  0.83 px (35°) of shared bias that **no localiser can remove** (§3.4) [P for the
  rendered model, I for the real plate thickness, which is unknown].
* Fitting in *distorted* pixel space biases the centre at large image radius:
  the centroid's measured offset against the undistorted fit grows to
  −0.12 px at r > 480 px on the real corpus (§4.3) [P].

---

## 2. Protocol and exact commands

All benchmarks run in the repo venv, CPU only, no vendor binary executed:

```bash
cd <repo>
PY=".venv/bin/python"; export HOME=/tmp OPENBLAS_NUM_THREADS=1

# unit/API/synthetic-ground-truth tests (16 tests, ~1.5 s)
$PY -m pytest -q tests/test_localize.py

# real corpus: shared detector front-end -> refine with each method -> metrics
$PY /tmp/einstar_cal2/agents/localize/bench_real.py        # -> bench_real.json
$PY /tmp/einstar_cal2/agents/localize/bench_repeat.py      # -> repeat_real.json (split-half)
$PY /tmp/einstar_cal2/agents/localize/joint_cal.py         # -> joint_cal_<method>.json (gated BA)

# synthetic: 41-marker scenes with known truth (analytic renderer)
$PY /tmp/einstar_cal2/agents/localize/bench_synth.py       # -> bench_synth.json

# tables used below
$PY /tmp/einstar_cal2/agents/localize/mkreport.py          # -> tables.md
```

Real-corpus protocol: one **shared front-end for every method** — the production blob
detector with `refine=False` and `assign_grid` (both polarities tried, best grid kept),
so the assignment never depends on the refinement under test; then
`refine_grid(method, K, kc, iterations=2, loo=True)` with the factory CCF intrinsics
and distortion. 75 images, **74 usable grids** (one image has < 30 blobs), 2786
markers per method.

---

## 3. Synthetic results with known ground truth

Renderer: the 41-marker plate is drawn as the exact projection of each 3-D circle
(97-point polygon, 4× supersampled coverage), then Gaussian PSF, illumination,
saturation clip, blooming/smear, read noise, optional through-hole aperture and
occluder. The ground truth is the projection of each circle centre on the plate
front face. Every case is repeated over 4 sub-pixel phases (and 3 for the stereo
cases), which averages the pixelation error (~0.01 px per phase) [P].

### Signed-bias magnitude |bias| (px), 41 markers, 4 phases

| case | centroid | centroid_bg | ellipse | gradient | conic | conic_persp | auto |
|:---|---:|---:|---:|---:|---:|---:|---:|
| tilt0 | 0.005 | 0.006 | 0.006 | 0.025 | 0.005 | 0.005 | 0.005 |
| tilt5 | 0.002 | 0.002 | 0.012 | 0.008 | 0.004 | 0.004 | 0.004 |
| tilt10 | 0.002 | 0.004 | 0.004 | 0.008 | 0.002 | 0.005 | 0.004 |
| tilt15 | 0.003 | 0.004 | 0.013 | 0.006 | 0.004 | 0.003 | 0.003 |
| tilt20 | 0.003 | 0.003 | 0.026 | 0.015 | 0.004 | 0.003 | 0.002 |
| tilt25 | 0.006 | 0.007 | 0.016 | 0.022 | 0.009 | 0.002 | 0.001 |
| tilt30 | 0.012 | 0.011 | 0.036 | 0.026 | 0.014 | 0.006 | 0.006 |
| tilt40 | 0.009 | 0.006 | 0.012 | 0.004 | 0.012 | 0.002 | 0.002 |
| tilt45 | 0.011 | 0.009 | 0.029 | 0.032 | 0.012 | 0.003 | 0.003 |
| radius3 | 0.007 | 0.008 | 0.014 | 0.006 | 0.009 | 0.004 | 0.004 |
| radius4 | 0.010 | 0.013 | 0.022 | 0.008 | 0.010 | 0.002 | 0.003 |
| radius5 | 0.013 | 0.014 | 0.025 | 0.010 | 0.012 | 0.003 | 0.003 |
| radius6 | 0.021 | 0.021 | 0.007 | 0.011 | 0.021 | 0.004 | 0.004 |
| radius8 | 0.033 | 0.033 | 0.024 | 0.023 | 0.032 | 0.002 | 0.002 |
| radius10 | 0.054 | 0.054 | 0.065 | 0.062 | 0.055 | 0.010 | 0.010 |
| radius12 | 0.077 | 0.077 | 0.088 | 0.075 | 0.076 | 0.005 | 0.004 |
| contrast6 | 0.018 | 0.018 | 0.018 | 0.023 | 0.232 | 0.225 | 0.016 |
| contrast10 | 0.009 | 0.013 | 0.011 | 0.014 | 0.279 | 0.272 | 0.012 |
| contrast20 | 0.004 | 0.013 | 0.020 | 0.007 | 0.027 | 0.019 | 0.014 |
| contrast50 | 0.009 | 0.013 | 0.030 | 0.008 | 0.017 | 0.010 | 0.009 |
| contrast100 | 0.011 | 0.012 | 0.038 | 0.023 | 0.015 | 0.007 | 0.007 |
| contrast200 | 0.012 | 0.011 | 0.036 | 0.026 | 0.014 | 0.006 | 0.006 |
| blur0.5 | 0.012 | 0.014 | 0.023 | 0.022 | 0.013 | 0.005 | 0.005 |
| blur1 | 0.012 | 0.014 | 0.036 | 0.024 | 0.016 | 0.008 | 0.008 |
| blur1.5 | 0.013 | 0.008 | 0.036 | 0.026 | 0.014 | 0.006 | 0.005 |
| blur2 | 0.013 | 0.011 | 0.022 | 0.023 | 0.014 | 0.006 | 0.005 |
| blur3 | 0.012 | 0.014 | 0.016 | 0.012 | 0.014 | 0.006 | 0.006 |
| sat120 | 0.012 | 0.013 | 0.015 | 0.010 | 0.013 | 0.001 | 0.001 |
| sat180 | 0.012 | 0.012 | 0.011 | 0.013 | 0.011 | 0.001 | 0.001 |
| sat255 | 0.012 | 0.011 | 0.036 | 0.026 | 0.014 | 0.006 | 0.006 |
| bloom0.3 | 0.012 | 0.012 | 0.037 | 0.028 | 0.014 | 0.006 | 0.006 |
| bloom1 | 0.012 | 0.012 | 0.035 | 0.027 | 0.014 | 0.006 | 0.006 |
| smear0.3 | 0.012 | 0.013 | 0.035 | 0.028 | 0.014 | 0.006 | 0.006 |
| occluded | 0.012 | 0.011 | 0.036 | 0.026 | 0.014 | 0.006 | 0.006 |
| border | 0.040 | 0.045 | 0.047 | 0.036 | 0.039 | 0.040 | 0.043 |
| through1mm_tilt0 | 0.007 | 0.005 | 0.012 | 0.024 | 0.010 | 0.010 | 0.010 |
| through1mm_tilt20 | 0.497 | 0.490 | 0.489 | 0.494 | 0.497 | 0.491 | 0.490 |
| through1mm_tilt35 | 0.830 | 0.814 | 0.819 | 0.808 | 0.830 | 0.821 | 0.820 |
| persp_Z250_r16 | 0.166 | 0.169 | 0.153 | 0.165 | 0.172 | 0.003 | 0.003 |
| persp_Z250_r24 | 0.150 | 0.378 | 0.406 | 0.375 | 0.387 | 0.003 | 0.003 |
| persp_Z150_r32 | 0.422 | 0.472 | 1.296 | 0.111 | 0.681 | 0.252 | 0.252 |
| persp_Z180_r40 | 0.698 | 0.960 | 1.021 | 0.800 | 1.077 | 0.078 | 0.068 |

### RMS error (px)

| case | centroid | centroid_bg | ellipse | gradient | conic | conic_persp | auto |
|:---|---:|---:|---:|---:|---:|---:|---:|
| tilt0 | 0.026 | 0.025 | 0.127 | 0.106 | 0.028 | 0.028 | 0.027 |
| tilt5 | 0.023 | 0.024 | 0.118 | 0.091 | 0.026 | 0.026 | 0.024 |
| tilt10 | 0.020 | 0.023 | 0.118 | 0.092 | 0.026 | 0.026 | 0.023 |
| tilt15 | 0.021 | 0.022 | 0.115 | 0.087 | 0.026 | 0.026 | 0.023 |
| tilt20 | 0.020 | 0.027 | 0.118 | 0.090 | 0.021 | 0.022 | 0.021 |
| tilt25 | 0.021 | 0.028 | 0.105 | 0.091 | 0.027 | 0.025 | 0.022 |
| tilt30 | 0.022 | 0.028 | 0.110 | 0.094 | 0.027 | 0.023 | 0.022 |
| tilt40 | 0.021 | 0.031 | 0.143 | 0.097 | 0.025 | 0.023 | 0.020 |
| tilt45 | 0.023 | 0.042 | 0.117 | 0.100 | 0.028 | 0.025 | 0.024 |
| radius3 | 0.022 | 0.055 | 0.140 | 0.126 | 0.028 | 0.026 | 0.026 |
| radius4 | 0.021 | 0.031 | 0.115 | 0.085 | 0.026 | 0.023 | 0.022 |
| radius5 | 0.022 | 0.026 | 0.097 | 0.078 | 0.026 | 0.024 | 0.022 |
| radius6 | 0.029 | 0.029 | 0.094 | 0.077 | 0.032 | 0.023 | 0.022 |
| radius8 | 0.044 | 0.041 | 0.089 | 0.080 | 0.042 | 0.022 | 0.021 |
| radius10 | 1.936 | 0.078 | 0.093 | 0.091 | 0.063 | 0.024 | 0.023 |
| radius12 | 3.631 | 0.093 | 4.864 | 0.110 | 0.085 | 0.018 | 0.018 |
| contrast6 | 0.288 | 0.141 | 0.236 | 0.489 | 0.569 | 0.565 | 0.144 |
| contrast10 | 0.176 | 0.088 | 0.168 | 0.361 | 0.556 | 0.552 | 0.095 |
| contrast20 | 0.095 | 0.052 | 0.127 | 0.243 | 0.074 | 0.072 | 0.062 |
| contrast50 | 0.042 | 0.033 | 0.115 | 0.151 | 0.034 | 0.032 | 0.030 |
| contrast100 | 0.027 | 0.030 | 0.113 | 0.110 | 0.029 | 0.026 | 0.024 |
| contrast200 | 0.022 | 0.028 | 0.110 | 0.094 | 0.027 | 0.023 | 0.022 |
| blur0.5 | 0.022 | 0.021 | 0.116 | 0.078 | 0.026 | 0.023 | 0.021 |
| blur1 | 0.022 | 0.026 | 0.113 | 0.086 | 0.027 | 0.022 | 0.021 |
| blur1.5 | 0.022 | 0.036 | 0.111 | 0.104 | 0.029 | 0.026 | 0.024 |
| blur2 | 0.023 | 0.046 | 0.109 | 0.151 | 0.028 | 0.025 | 0.024 |
| blur3 | 0.024 | 0.057 | 0.101 | 0.271 | 0.031 | 0.028 | 0.027 |
| sat120 | 0.035 | 0.026 | 0.085 | 0.064 | 0.029 | 0.026 | 0.024 |
| sat180 | 0.024 | 0.022 | 0.125 | 0.081 | 0.025 | 0.022 | 0.021 |
| sat255 | 0.022 | 0.028 | 0.110 | 0.094 | 0.027 | 0.023 | 0.022 |
| bloom0.3 | 0.021 | 0.029 | 0.109 | 0.093 | 0.027 | 0.024 | 0.022 |
| bloom1 | 0.021 | 0.029 | 0.110 | 0.096 | 0.027 | 0.023 | 0.022 |
| smear0.3 | 0.022 | 0.029 | 0.109 | 0.093 | 0.028 | 0.025 | 0.023 |
| occluded | 0.022 | 0.028 | 0.110 | 0.094 | 0.027 | 0.023 | 0.022 |
| border | 0.276 | 0.268 | 0.268 | 0.173 | 0.256 | 0.255 | 0.268 |
| through1mm_tilt0 | 0.275 | 0.272 | 0.293 | 0.296 | 0.279 | 0.279 | 0.279 |
| through1mm_tilt20 | 0.553 | 0.544 | 0.559 | 0.557 | 0.553 | 0.547 | 0.547 |
| through1mm_tilt35 | 0.852 | 0.835 | 0.853 | 0.835 | 0.852 | 0.844 | 0.843 |
| persp_Z250_r16 | 0.689 | 0.196 | 0.186 | 0.199 | 0.196 | 0.026 | 0.026 |
| persp_Z250_r24 | 7.637 | 0.660 | 9.597 | 0.607 | 0.440 | 0.023 | 0.023 |
| persp_Z150_r32 | 12.142 | 7.966 | 6.440 | 4.226 | 1.907 | 1.731 | 1.731 |
| persp_Z180_r40 | 19.794 | 6.169 | 17.797 | 3.307 | 1.250 | 0.511 | 0.447 |

### Reported sigma / actual random error (1.0 = calibrated, >1 conservative)

| case | centroid | centroid_bg | ellipse | gradient | conic | conic_persp | auto |
|:---|---:|---:|---:|---:|---:|---:|---:|
| tilt0 | 1.29 | 1.27 | 0.58 | 0.64 | 0.76 | 0.61 | 0.61 |
| tilt5 | 1.45 | 1.27 | 0.62 | 0.73 | 0.77 | 0.62 | 0.64 |
| tilt10 | 1.79 | 1.34 | 0.62 | 0.74 | 0.81 | 0.65 | 0.71 |
| tilt15 | 1.92 | 1.39 | 0.64 | 0.87 | 0.77 | 0.62 | 0.67 |
| tilt20 | 2.35 | 1.14 | 0.65 | 0.87 | 1.00 | 0.78 | 0.77 |
| tilt25 | 2.89 | 1.14 | 0.70 | 0.94 | 0.85 | 0.69 | 0.76 |
| tilt30 | 4.21 | 1.17 | 0.72 | 1.00 | 0.93 | 0.76 | 0.80 |
| tilt40 | 6.44 | 1.00 | 0.54 | 1.32 | 1.01 | 0.81 | 0.87 |
| tilt45 | 7.64 | 0.74 | 0.69 | 1.54 | 0.99 | 0.81 | 0.80 |
| radius3 | 3.29 | 0.56 | 0.65 | 0.70 | 1.02 | 0.83 | 0.84 |
| radius4 | 4.08 | 1.06 | 0.68 | 1.13 | 0.94 | 0.78 | 0.79 |
| radius5 | 4.57 | 1.42 | 0.73 | 1.37 | 0.82 | 0.66 | 0.67 |
| radius6 | 4.17 | 1.49 | 0.66 | 1.54 | 0.75 | 0.65 | 0.64 |
| radius8 | 3.12 | 1.25 | 0.62 | 2.00 | 0.59 | 0.59 | 0.60 |
| radius10 | 0.05 | 0.53 | 0.72 | 2.66 | 0.45 | 0.52 | 0.52 |
| radius12 | 0.03 | 0.58 | 0.01 | 2.60 | 0.32 | 0.57 | 0.57 |
| contrast6 | 0.61 | 0.57 | 0.29 | 0.48 | 0.54 | 0.47 | 0.69 |
| contrast10 | 0.76 | 0.63 | 0.43 | 0.56 | 0.34 | 0.28 | 0.62 |
| contrast20 | 1.02 | 0.76 | 0.60 | 0.65 | 0.46 | 0.36 | 0.40 |
| contrast50 | 1.93 | 1.02 | 0.67 | 0.73 | 0.74 | 0.58 | 0.63 |
| contrast100 | 3.08 | 1.12 | 0.70 | 0.88 | 0.86 | 0.69 | 0.73 |
| contrast200 | 4.21 | 1.17 | 0.72 | 1.00 | 0.93 | 0.76 | 0.80 |
| blur0.5 | 6.53 | 1.93 | 0.64 | 1.00 | 0.96 | 0.79 | 0.81 |
| blur1 | 4.85 | 1.39 | 0.69 | 1.00 | 1.01 | 0.84 | 0.84 |
| blur1.5 | 3.75 | 0.86 | 0.71 | 1.00 | 0.84 | 0.67 | 0.72 |
| blur2 | 2.96 | 0.68 | 0.70 | 0.82 | 0.86 | 0.69 | 0.70 |
| blur3 | 2.00 | 0.55 | 0.71 | 0.62 | 0.70 | 0.61 | 0.68 |
| sat120 | 2.62 | 1.39 | 0.78 | 1.16 | 0.72 | 0.59 | 0.59 |
| sat180 | 3.89 | 1.66 | 0.55 | 1.01 | 0.90 | 0.74 | 0.74 |
| sat255 | 4.21 | 1.17 | 0.72 | 1.00 | 0.93 | 0.76 | 0.80 |
| bloom0.3 | 4.30 | 1.16 | 0.73 | 1.02 | 0.92 | 0.75 | 0.79 |
| bloom1 | 4.31 | 1.16 | 0.71 | 0.97 | 0.93 | 0.74 | 0.78 |
| smear0.3 | 4.50 | 1.17 | 0.73 | 1.03 | 0.86 | 0.71 | 0.77 |
| occluded | 4.21 | 1.17 | 0.72 | 1.00 | 0.93 | 0.76 | 0.80 |
| border | 0.39 | 0.12 | 0.29 | 0.67 | 0.09 | 0.07 | 0.06 |
| through1mm_tilt0 | 0.15 | 0.11 | 0.24 | 0.23 | 0.07 | 0.06 | 0.05 |
| through1mm_tilt20 | 0.37 | 0.13 | 0.26 | 0.39 | 0.09 | 0.07 | 0.06 |
| through1mm_tilt35 | 0.89 | 0.16 | 0.32 | 0.75 | 0.12 | 0.10 | 0.10 |
| persp_Z250_r16 | 0.31 | 0.31 | 0.35 | 5.07 | 0.12 | 0.36 | 0.51 |
| persp_Z250_r24 | 0.03 | 0.06 | 0.00 | 1.78 | 0.05 | 0.35 | 0.38 |
| persp_Z150_r32 | 0.02 | 0.00 | 0.00 | 0.28 | 0.01 | 0.00 | 0.00 |
| persp_Z180_r40 | 0.01 | 0.00 | 0.00 | 0.46 | 0.01 | 0.01 | 0.02 |

### Stereo 3-D error after a rigid board fit (um)

| case | centroid | centroid_bg | ellipse | gradient | conic | conic_persp | auto |
|:---|---:|---:|---:|---:|---:|---:|---:|
| tilt0 | 23.3 | 23.8 | 120.0 | 80.7 | 31.0 | 29.5 | 26.9 |
| tilt20 | 25.2 | 31.3 | 112.4 | 86.8 | 25.6 | 25.4 | 23.7 |
| tilt40 | 20.3 | 45.6 | 162.3 | 100.2 | 35.9 | 35.7 | 32.2 |
| persp_Z250_r16 | 20.9 | 25.3 | 35.4 | 51.7 | 23.9 | 13.6 | 13.6 |
| persp_Z250_r24 | 22.3 | 30.2 | 44.8 | 66.8 | 47.8 | 14.8 | 14.8 |
| persp_Z150_r32 | 927.0 | 892.7 | 874.7 | 221.0 | 51.6 | 10.4 | 10.5 |
| persp_Z180_r40 | 5201.4 | 62.0 | 89.3 | 271.2 | 85.9 | 8.3 | 17.0 |

### 3.1 Reading the synthetic tables [P]

* **Tilt 0–45°, r = 4.5 px, 450 mm:** `conic` |bias| ≤ 0.014 px / RMS 0.021–0.028 px,
  `conic_perspective` ≤ 0.006 px, `auto` ≤ 0.006 px; `centroid`/`centroid_bg`
  0.002–0.012 px; `gradient` 0.004–0.032 px (circle model on a projected ellipse);
  `ellipse` 0.004–0.036 px with RMS 0.105–0.143 px — the worst method, as in the
  prior audit.
* **Contrast:** down to 20 DN everything works (|bias| ≤ 0.027 px). At 6–10 DN the
  Canny-based `conic` degrades (0.23–0.28 px bias, RMS 0.55) while `auto` (which
  detects the weak edge support and falls back) stays at 0.012–0.016 px and the
  centroid at 0.009–0.018 px [P]. This is the one regime where the production
  centroid beats the conic; the real corpus (saturated 200 DN markers) is far from it.
* **Defocus (0.5–3 px), saturation (120–255 DN), symmetric blooming, directional
  smear, occlusion:** no method loses accuracy (≤ 0.031 px except `centroid_bg`
  0.057 px at σ = 3); `gradient`'s RMS grows with blur (0.27 px at σ = 3) because a
  circle fit on a heavily blurred ellipse is ill-conditioned [P].
* **Border clipping:** |bias| 0.036–0.047 px and RMS 0.17–0.28 px for all methods;
  markers cut by the frame are flagged `partial` and get an inflated σ [P].
* **Radius sweep (3–12 px):** the localisers are scale-stable up to r ≈ 8 px. Above
  that the *synthetic* marker radius (up to 5.0 mm) makes the off-lattice large
  marker touch its neighbours at the half-pitch (14 mm) — a geometry that the real
  plate (r/pitch = 0.064, neighbour at 7.9 r) never has. Those rows are a stress
  test, not a plate statement; the centroid's failure there is the known
  neighbour-contamination mode [P for the render, I for the extrapolation].

### 3.2 Stereo 3-D error [P]

The synthetic Left/Right pair uses the factory rig (baseline 158.557 mm, 22.1°) and
the *true* rig for triangulation, so the 3-D error isolates the localiser:

* At 450 mm, tilt 0/20/40°: rigid-fit 3-D RMS **20–36 µm** for centroid,
  centroid_bg, conic, conic_perspective and auto (24–36 µm for the edge/conic
  family, 20–25 µm for the centroid family whose random noise is smallest) [P].
  `ellipse` 112–162 µm and `gradient` 81–100 µm are 3–5× worse.
* At close range with large markers the common visible set shrinks (the plate
  leaves the field of view) and the stereo test becomes inconclusive — reported as
  `fail`/large values rather than hidden [U].

### 3.3 Perspective-eccentricity validation [P]

With a **5.86 mm radius** marker at 250 mm / 40° tilt (r = 24 px), the conic centre
is biased by **−0.386 px** (RMS 0.44 px); `conic_perspective` returns **+0.003 px**
bias and 0.023 px RMS. With r = 16 px the conic bias is 0.172 px and the corrected
bias 0.003 px. The correction is therefore *implemented correctly and exactly*; whether
it is *worth using* on the real plate is a separate question (§4.4).

### 3.4 Through-hole aperture [P for the model]

A 1 mm through-hole biases **every** localiser by 0.49 px (20°) and 0.81–0.83 px
(35°), with the methods agreeing with each other to ~0.01 px. This is a physical
bias of the observed aperture, not a localiser defect; a per-view board pose absorbs
99.7–100 % of it (prior audit), so it does not enter the calibration residuals in
proportion to its size. It matters for *absolute* point coordinates. The real plate
thickness is **[U]**.

### 3.5 Uncertainty calibration [P]

`sigma_px` vs the actual random error (`sqrt(RMS² − bias²)`) is 0.76–0.99 for
`conic` (honest, slightly conservative), 0.61–0.81 for `conic_perspective`,
0.61–0.80 for `auto` and 0.74–1.27 for `centroid_bg`; the raw `centroid` is off by
up to 7× because its σ carries a 0.03 px systematic floor and ignores pixelation.
The uncertainty is therefore usable as a BA weight for the edge methods; for the
centroid family it is only an order-of-magnitude indicator.

---

## 4. Real vendor corpus (75 images, 74 grids)

No physical ground truth exists on this corpus, so every number below is a
*consistency*, *repeatability* or *disagreement* measurement; absolute accuracy is
**[U]**.

### Real corpus: per-image localisation metrics (74 grids)

| method | LOO med px | LOO p90 px | sigma med px | quality | rejected/img | partial/img | ms/img | ms/marker |
|:---|---:|---:|---:|---:|---:|---:|---:|---:|
| centroid | 0.281 | 0.578 | 0.104 | 0.30 | 0.00 | 0.00 | 20 | 0.52 |
| centroid_bg | 0.260 | 0.532 | 0.031 | 0.75 | 0.00 | 0.00 | 32 | 0.81 |
| ellipse | 0.275 | 0.543 | 0.071 | 0.40 | 0.08 | 0.00 | 17 | 0.44 |
| gradient | 0.263 | 0.543 | 0.070 | 0.93 | 0.07 | 0.04 | 40 | 1.02 |
| conic | 0.247 | 0.513 | 0.024 | 0.98 | 0.12 | 0.03 | 71 | 1.78 |
| conic_perspective | 0.247 | 0.513 | 0.020 | 0.98 | 0.12 | 0.03 | 89 | 2.24 |
| auto | 0.249 | 0.518 | 0.020 | 0.96 | 0.01 | 0.03 | 110 | 2.80 |

### Real corpus: cross-camera consistency (Left/Right, factory CCF rig; 24 poses)

| method | Sampson med px | rigid RMS mm | rigid RMS px |
|:---|---:|---:|---:|
| centroid | 0.326 | 0.7667 | 2.760 |
| centroid_bg | 0.311 | 0.7431 | 2.662 |
| ellipse | 0.336 | 0.7501 | 2.678 |
| gradient | 0.331 | 0.7467 | 2.640 |
| conic | 0.307 | 0.7361 | 2.624 |
| conic_perspective | 0.306 | 0.7375 | 2.629 |
| auto | 0.309 | 0.7384 | 2.630 |

### Real corpus: paired differences vs conic_perspective (bootstrap 95 % CI)

| comparison | mean | CI95 | n |
|:---|---:|---:|---:|
| centroid - conic_persp_rigid_mm | -0.00197 | [-0.02782, +0.02129] | 24 |
| centroid - conic_persp_sampson | -0.00856 | [-0.03770, +0.02022] | 24 |
| centroid_bg - conic_persp_rigid_mm | +0.00142 | [-0.01477, +0.01830] | 24 |
| centroid_bg - conic_persp_sampson | -0.00597 | [-0.01990, +0.00906] | 24 |
| ellipse - conic_persp_rigid_mm | +0.00955 | [-0.00803, +0.02846] | 24 |
| ellipse - conic_persp_sampson | +0.00199 | [-0.01523, +0.01753] | 24 |
| gradient - conic_persp_rigid_mm | -0.01722 | [-0.04197, +0.00331] | 24 |
| gradient - conic_persp_sampson | +0.00775 | [-0.00801, +0.02383] | 24 |
| conic - conic_persp_rigid_mm | +0.00076 | [-0.00070, +0.00249] | 24 |
| conic - conic_persp_sampson | +0.00069 | [-0.00163, +0.00317] | 24 |
| auto - conic_persp_rigid_mm | +0.03210 | [-0.00272, +0.09893] | 24 |
| auto - conic_persp_sampson | -0.00378 | [-0.00998, +0.00008] | 24 |

### Real corpus: disagreement against the per-marker median over methods

| method | median px | p90 px | >0.1 px | >0.2 px |
|:---|---:|---:|---:|---:|
| centroid | 0.082 | 0.239 | 0.418 | 0.147 |
| centroid_bg | 0.027 | 0.062 | 0.018 | 0.002 |
| ellipse | 0.085 | 0.168 | 0.399 | 0.043 |
| gradient | 0.064 | 0.128 | 0.216 | 0.009 |
| conic | 0.011 | 0.042 | 0.002 | 0.000 |
| conic_perspective | 0.011 | 0.045 | 0.002 | 0.000 |
| auto | 0.009 | 0.040 | 0.002 | 0.000 |

### Perspective eccentricity on real images (Left camera), by tilt

| tilt deg | n | predicted |e| px | applied |e| px | ratio (med) | cos (med) | frac cos>0 |
|:---|---:|---:|---:|---:|---:|---:|
| 0-15 | 200 | 0.0074 | 0.0044 | 0.58 | -0.60 | 0.48 |
| 15-25 | 157 | 0.0119 | 0.0125 | 0.94 | -0.44 | 0.46 |
| 25-35 | 155 | 0.0083 | 0.0101 | 0.98 | +0.00 | 0.43 |
| 35-50 | 356 | 0.0075 | 0.0088 | 0.83 | +0.00 | 0.27 |

### Real corpus: method - conic_perspective vs image radius (px)

| method | radius px | n | mean du | mean dv | rms |
|:---|---:|---:|---:|---:|---:|
| centroid | 0-200 | 517 | -0.014 | -0.062 | 0.194 |
| centroid | 200-350 | 312 | -0.014 | -0.051 | 0.153 |
| centroid | 350-480 | 73 | -0.036 | -0.060 | 0.193 |
| centroid | 480-620 | 24 | -0.117 | -0.017 | 0.234 |
| centroid_bg | 0-200 | 517 | -0.004 | -0.023 | 0.140 |
| centroid_bg | 200-350 | 312 | -0.003 | -0.020 | 0.052 |
| centroid_bg | 350-480 | 73 | -0.001 | -0.015 | 0.048 |
| centroid_bg | 480-620 | 24 | +0.000 | -0.017 | 0.059 |
| ellipse | 0-200 | 516 | -0.001 | -0.009 | 0.146 |
| ellipse | 200-350 | 312 | -0.004 | -0.011 | 0.114 |
| ellipse | 350-480 | 73 | +0.002 | -0.001 | 0.093 |
| ellipse | 480-620 | 24 | +0.013 | -0.015 | 0.119 |
| gradient | 0-200 | 517 | +0.002 | -0.002 | 0.148 |
| gradient | 200-350 | 312 | +0.010 | +0.005 | 0.080 |
| gradient | 350-480 | 73 | +0.012 | -0.009 | 0.072 |
| gradient | 480-620 | 24 | +0.037 | -0.012 | 0.087 |
| conic | 0-200 | 517 | -0.005 | +0.000 | 0.014 |
| conic | 200-350 | 312 | -0.006 | +0.001 | 0.012 |
| conic | 350-480 | 73 | -0.007 | +0.002 | 0.014 |
| conic | 480-620 | 24 | -0.008 | +0.002 | 0.014 |
| auto | 0-200 | 516 | -0.000 | -0.002 | 0.030 |
| auto | 200-350 | 312 | +0.001 | -0.002 | 0.007 |
| auto | 350-480 | 73 | +0.001 | -0.001 | 0.006 |
| auto | 480-620 | 24 | +0.001 | -0.002 | 0.006 |

### 4.1 Localisation noise and repeatability [P]

`loo` is the leave-one-out residual of the undistorted grid homography: it is
insensitive to the calibration but contains the distortion-model error, which is
common to all methods. Its method-to-method differences therefore isolate the
localiser: **conic/conic_perspective 0.247 px, auto 0.249 px** vs centroid 0.281,
ellipse 0.275, gradient 0.263, centroid_bg 0.260.

Split-half repeatability (two independent half-sets of edge samples, or two
checkerboard sub-grids for the centroid family) confirms it: **conic 0.0255 px**,
gradient 0.032, ellipse 0.094. The reported σ is calibrated against this
(ratio 0.91 for conic, 2.36 conservative for gradient, 0.73 for ellipse).
The centroid family cannot be split this way — a checkerboard split of a 4-px
saturated disc measures a ~0.5 px *subsampling* bias, not noise — so its random
error is taken from the synthetic Monte Carlo (0.005–0.02 px) and a 0.03 px
systematic floor is added to its σ [P for the negative result, I for the floor].

### 4.2 Cross-camera consistency is CCF-limited [P]

Triangulating the Left/Right correspondences with the **factory CCF rig** and
fitting the known rigid plate gives a 3-D residual of **0.735–0.767 mm**
(≈ 2.6 px equivalent) for every method, and Sampson residuals of 0.307–0.336 px.
The paired bootstrap differences against `conic_perspective` are all smaller than
their 95 % CI and change sign between metrics (e.g. centroid −0.0012 ± 0.028 mm;
gradient −0.0165 ± 0.021 mm). **Conclusion: the factory CCF's own rig error
dominates this metric by an order of magnitude; it cannot rank localisers at the
0.02–0.05 px level.** The prior audit's epipolar reference (0.18 px) and this
0.74 mm rigid residual are consistent with a CCF relative-pose error of ~2 px
equivalent, not with a localiser problem.

### 4.3 Bias structure that is measurable [P]

Because all methods see the same images, the *difference field* against
`conic_perspective` is a valid bias proxy:

* `centroid` drifts with image radius: mean (−0.016, −0.063) px at r < 200 px,
  (−0.118, −0.019) px at 480–620 px, RMS 0.16–0.24 px. That is the
  distortion-space centroid bias plus the local background model.
* `centroid_bg` removes most of it (mean ≤ 0.023 px, RMS 0.04–0.09 px) — the
  local-plane background outside the PSF shoulder is what fixes the
  illumination-gradient/neighbour bias.
* `conic` differs from `conic_perspective` by ≤ 0.008 px at every radius (the
  perspective term on this plate, as predicted).
* `auto` differs by ≤ 0.004 px, i.e. it tracks the best estimate.
* **Intensity** (marker contrast quartiles, 3–233 DN; the corpus markers are
  saturated, so the range is really 175–233 DN): the conic family is flat
  (offset RMS 0.008–0.017 px across quartiles) while the centroid degrades at the
  low-contrast end (RMS 0.31 px below 190 DN → 0.08 px above 219 DN) — the same
  trend as the synthetic contrast sweep, and the reason `auto` falls back there.

### 4.4 Perspective eccentricity on real oblique images [P]

Predicted from the fitted pose and the measured marker radius, the eccentricity is
**0.006–0.012 px** on this corpus (240–610 mm, r_px 3.3–7.1). The applied
correction (`conic_perspective − conic`) has the same magnitude (0.004–0.013 px,
ratio median 0.58–0.98 by tilt bin), but its **direction is noise-dominated**
(median cos −0.60…0.0, 27–49 % of markers with cos > 0): the localiser noise
(0.02 px) is 2–4× larger than the effect [P]. The paired cross-camera test agrees:
switching the correction on changes the rigid residual by
**+0.0008 mm [−0.0007, +0.0025]**, i.e. < 3 µm, consistent with zero [P].
So on the *shipped plate* the perspective correction is not a lever for accuracy —
but it costs 0.02 ms/marker and is exact when the effect is large (§3.3), so it is
kept on. This *corrects* the prior audit's framing: at 240 mm with r = 3.4 mm
*radius* the effect would be 0.06 px; with the measured 1.77 mm radius it is 0.016 px.

### 4.5 Joint calibration residual [I/P]

Protocol: factory CCF as the initial guess, distortion fixed (`free_kc=False`),
fx/fy/cx/cy + rig + 25 board poses free, Huber loss. A first pass drops views whose
factory-intrinsics PnP residual exceeds max(1.5 px, 3× median) (3 of 72 poses have a
partially visible plate); a second pass drops the views whose *fitted* residual is a
gross outlier (3 more, same cause) and refits, so the rig is not contaminated by
them. The resulting residual is **model-limited** — 0.33–0.39 px, the same
0.3–0.4 px the prior audit measured with every localiser — so the localiser ranking
is barely resolvable from it (conic/auto 0.332/0.340 vs centroid 0.385 px, but at
the cost of dropping two more views). As a protocol check, the fitted fx is
1156.8–1157.0 px (factory CCF 1159.1, −0.2 %) and the fitted baseline is
160.08–160.24 mm at the **nominal 28.0 mm pitch**; rescaled to the CCF-effective
27.72 mm pitch this is 158.48–158.63 mm against the factory 158.557 mm (+0.04 %) —
i.e. the fit reproduces the documented 1 % vendor-vs-nominal scale difference. That
is a consistency check, not an accuracy claim.

| method | views (1st) | views (refit) | gated RMS (1st) | refit RMS | view RMS med | fx left (refit) | baseline (refit) mm | CCF baseline mm | s |
|:---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| centroid | 69 | 66 | 0.395 | 0.385 | 0.355 | 1156.98 | 160.242 | 158.557 | 44 |
| conic | 69 | 64 | 0.343 | 0.332 | 0.325 | 1156.92 | 160.084 | 158.557 | 38 |
| auto | 69 | 64 | 0.351 | 0.340 | 0.328 | 1156.80 | 160.094 | 158.557 | 51 |

| method | n | split-half med px | reported sigma med px | ratio | valid |
|:---|---:|---:|---:|---:|---:|
| centroid | 2786 | 0.5030 | 0.1044 | 0.21 | no |
| centroid_bg | 2786 | 0.5023 | 0.0307 | 0.06 | no |
| ellipse | 2780 | 0.0941 | 0.0714 | 0.73 | yes |
| gradient | 2779 | 0.0322 | 0.0699 | 2.36 | yes |
| conic | 2772 | 0.0255 | 0.0239 | 0.91 | yes |

---

## 5. Recommendation

### 5.1 Default (offline calibration, final dwell frame) — `auto`, or `conic_perspective` for auditability

`auto` and `conic_perspective` are statistically indistinguishable on every metric
that is not CCF-limited (LOO 0.249 vs 0.247 px; disagreement 0.009 vs 0.011 px;
split-half 0.0255 px; synthetic |bias| ≤ 0.006 px tilt 0–45°; stereo 3-D 24–36 µm).
Choose:

* **`auto`** when the frames are uncontrolled: it fuses the (perspective-corrected)
  centroid with the conic only when they agree within 0.05 px, prefers the conic
  otherwise, falls back to the centroid family on low contrast, and reports `reason`
  for every rejection. Cost 1.30 ms/marker, 53 ms for 41 markers — irrelevant for a
  stationary frame.
* **`conic_perspective`** when determinism and auditability matter (no data-dependent
  selection): 1.05 ms/marker, same accuracy.
* **Never** ship `ellipse` (worst split-half 0.094 px, 0.085 px disagreement,
  highest share of gross outline failures) or the raw `centroid` (radial bias up to
  0.12 px); keep `centroid` only as a last-resort fallback for contrast < 6 DN,
  where it is the best of the seven. Rejection rates are low for every method
  (0.08 markers/image for `ellipse`, 0.12 for `conic`, ≤ 0.01 for `auto`, i.e.
  ≤ 0.3 % of the 2786 markers); the strict conic guards reject a marker rather
  than return a biased one, and `auto` converts those into a centroid fallback.

### 5.2 Live guidance (3 cameras, ≥ 20 fps, ROI) — `centroid_bg` with `auto` on the dwell frame

| live option | ms/marker | 3 cams × 12 markers | % of one core at 20 fps (50 ms budget) |
|---|---:|---:|---:|
| `centroid_bg` | 0.36 | 13 ms | 26 % |
| `gradient` | 0.46 | 17 ms | 33 % |
| `conic_perspective` | 1.05 | 38 ms | 76 % |
| `auto` | 1.30 | 47 ms | 94 % |

Measured per-call medians [P]; the ROI count is the number of markers expected in
the guided region. Recommendation: **`centroid_bg` while guiding** (0.36 ms/marker, no intrinsics
needed, no bias growth with image radius — ≤ 0.023 px measured over the whole
corpus — because its background model is local and its window is symmetric), then
**one `auto`/`conic_perspective` pass on the frozen dwell frame**
before the observation is committed to the calibration. This keeps the live loop at
~26 % of one core and gives the calibration the best localiser on the frame that
actually counts. If the ROI is the whole plate (41 markers), `auto` is 160 ms
per frame set — more than 3× the 50 ms budget — so the two-stage scheme is required,
not optional. Whole-plate `centroid_bg` (41 markers) is 15 ms per camera,
i.e. 44 ms for three cameras, so even the full plate stays inside 20 fps with that
method (88 % of one core) — the ROI is needed only for the conic-based methods.

Runtime notes: `refine_grid(iterations=1)` halves the per-image cost (one probe +
one localisation per marker); the plane normal from the previous accepted frame can
be passed as `plane={"normal": n}` to skip the homography/plane re-estimation.

---

## 6. Integration notes (no existing file modified)

1. `refine_observation(gray, obs, method="auto", K=K, kc=kc)` returns a
   `calib.detect.GridObservation` with the same fields (`points`, `homography`,
   `n_lattice`, `rms`, `max_residual`, `big_markers`, `detector="localize-v1"`), so
   it can replace the refinement step wherever a `GridObservation` is produced
   (e.g. `pipeline`/`detect` call sites) without touching those files.
2. Per-marker weighting: pass `sigma_px` through to the BA as the observation
   sigma. `conic`/`conic_perspective`/`auto` σ is calibrated against split-half
   (ratio 0.95); the centroid family includes a 0.03 px systematic floor.
3. **`K`/`kc` must be the intrinsics of the camera that produced the frame.** A K
   from the wrong camera (or a patch-local K with the wrong principal point)
   silently degrades `conic`/`conic_perspective` — they fit in undistorted
   coordinates. `refine_grid` estimates the plane normal from the grid homography,
   so no external pose is needed for `conic_perspective`.
4. The 41st (off-lattice) marker: `refine_grid(..., include_off_lattice=True)`
   localises it at `(3.5, 3)`; note that `calib.calibrate._uv_index()` indexes only
   the 40 lattice points, so the extra point is currently ignored by the BA. Wiring
   it in would require a change in `calibrate.py` (deliberately not made here).
5. Polarity: measured per marker from the radial profile, so bright IR frames and
   dark photographic frames both work; `invert=` overrides.
6. Failure handling: `ok=False` with `reason` — the caller should drop the marker
   (or down-weight by `sigma_px=inf`). `partial=True` marks markers cut by the
   frame border; their σ is inflated but they are still usable.
7. Suggested production default: `refine_mode="auto"` in `detect.detect_blobs`
   would require editing `detect.py`; as a non-invasive alternative, call
   `localize.refine_observation` after detection in the caller.

## 7. Limitations and open questions

* **Absolute accuracy on the real plate is [U].** No physical ground truth exists;
  all real-image claims are consistency/repeatability statements. The synthetic
  claims are exact for the rendered model but the renderer is an idealisation
  (flat disc or simple aperture, Gaussian PSF, white noise, no motion blur, no lens
  flare, no IR speckle). Directional blooming is tested as a stress case only.
* **Cross-camera metrics are CCF-limited** (0.74 mm rigid residual), so the real
  data cannot resolve localiser differences below ~0.05 px. A self-calibrated rig
  (or the joint BA with free distortion) would be needed to do better.
* **Through-hole aperture bias** (0.5–0.85 px at 20–35° for 1 mm thickness) is
  shared by all localisers; the real thickness is [U]. A pose-absorbed bias model
  (prior audit: `correct_aperture`) is the only way to remove it, and it is
  second-order for calibration.
* **Split-half is not valid for the centroid family** at 4 px marker scale
  (subsampling bias); its random error is taken from synthetic Monte Carlo.
* **The marker radius (1.77 mm) is [I]**, derived from `r_px·Z/f` with the
  established pitch; a pitch error would scale it.
* **Stereo 3-D tests at close range/large markers are inconclusive** because the
  plate leaves the field of view; they are reported as failures, not hidden.
* **Timing** is one core of this host; the ROI live estimates assume the per-marker
  cost is unchanged by the ROI (it is: the cost depends on the patch, not the frame).
* The joint calibration used a PnP pre-gate (drop views with a factory-intrinsics
  PnP residual > max(1.5 px, 3× median)); the gate keeps 69 of 72 poses and is
  identical for every method. Without it a few vendor frames with a partially
  visible plate dominate the robust loss. The BA itself is run with the repo's
  dense finite-difference Jacobian and `max_nfev=2500`; fewer evaluations leave it
  visibly unconverged (fx drifting by > 50 px), which is a solver-budget artefact,
  not a localiser property.

## 8. Artefacts

| file | content |
|---|---|
| `calib/localize.py` | the module (methods, `auto`, `refine_grid`, `refine_observation`) |
| `tests/test_localize.py` | 16 tests: API, bit-identical centroid, plane normal, GT bias bounds (tilt 0/20/40), perspective correction, polarity, low contrast/saturation, border, through-hole, grid recovery, input forms, `GridObservation` round trip, off-lattice marker |
| `bench_real.json` | per-method per-image refined points, records, cross-camera metrics, disagreement, perspective analysis, bias-vs-radius |
| `bench_synth.json` | 40 synthetic cases × 7 methods: per-marker errors, summary, stereo |
| `repeat_real.json` | split-half repeatability and σ calibration |
| `joint_cal_{centroid,conic,auto}.json` | two-pass gated joint 3-camera BA per method |
| `cache_real.pkl`, `refined_real.pkl` | detection cache and refined grids (re-runnable) |
| `synth.py`, `bench_real.py`, `bench_synth.py`, `bench_repeat.py`, `joint_cal.py`, `mkreport.py` | benchmark code |
| `tables.md` | the generated tables used above |
