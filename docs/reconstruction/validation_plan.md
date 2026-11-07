# Validation plan — offline experiments and hardware benchmark

Status: executable plan. Every experiment below is runnable **without the scanner** until the
hardware section; the scanner is only needed for E12/H-series. Metrics are defined precisely so
they can become CI gates.

Tooling constraint for the offline phase: Python 3.14 + NumPy + Matplotlib only (no OpenCV, no
PyTorch, no CUDA). Reference implementations may be slow; they exist to produce ground truth and
catch regressions, not to hit real-time. C++/CUDA stage tests use `ctest` and the same data files.

---

## 0. Test pyramid

| level | what | runtime | gate |
|---|---|---|---|
| L0 unit | calibration math, rectification maps, disparity→depth, TSDF update, MC on an analytic SDF | <10 s | every commit |
| L1 synthetic | analytic scenes + simulated speckle, full stage chain | 1–3 min | every PR |
| L2 dataset | ETH3D / Middlebury / TUM / DTU regression subsets | 10–30 min | nightly |
| L3 hardware | real scanner, gauge/metrology targets | hours | milestone |

L0/L1 are deterministic (fixed seeds); L2 is pinned by file SHA-256; L3 records scanner firmware,
temperature and calibration file hashes.

---

## 1. Synthetic data generator

Location: `tests/synth/` (Python reference) and `tests/data/synth/` (generated, git-ignored).
The C++ tests consume the same `.npz`/`.bin` files.

### 1.1 Scene model

Analytic primitives with closed-form ray intersection:

| primitive | parameters | use |
|---|---|---|
| plane | point, normal | flatness, gauge faces |
| sphere | centre, radius | diameter/form error |
| step block | 2 boxes | step height, edge behaviour |
| cylinder | axis, radius, height | curvature, grazing angles |
| corner | 3 planes | occlusion/disocclusion |
| random height field | sum of Gaussians | natural-ish surface |

Each scene has an albedo texture (procedural Perlin/value noise) and a material class:
`lambertian`, `low_texture` (albedo contrast < 2%), `specular` (GGX-ish lobe), `dark` (albedo 0.02).
This lets us test the failure modes the vendor’s SGM will hit.

### 1.2 Rendering

For a pinhole camera `K`, pose `T_wc`:

```
for each pixel (u,v):
    ray d_c = normalize(K^-1 [u,v,1])
    ray in world: o = T_wc^-1, d = R_wc^T d_c
    t = intersect(scene, o, d)
    z = t * d_c.z
    P = o + t d
    colour = albedo(P) * shading(P, n, light)
```

Right camera: `P_r = T_rc P`, `u_r = project(K_r, P_r)`, splat with a z-buffer. Ground-truth
disparity: `d_gt = u_l - u_r` at pixels where both projections are visible and the surface is
front-most. Occlusions are explicit; no “hole filling” in GT.

Outputs per frame:

```
left.png/right.png (16-bit), left_depth.exr/npy (float32), disp_gt.npy (float32),
valid_gt.npy (uint8), normal_gt.npy (float32), pose_gt.npy (4x4), K.npy, T_rc.npy
```

### 1.3 Simulated speckle projector

The Einstar’s IR projector is a random-dot pattern. Simulate it physically:

1. Generate a binary/grayscale dot pattern `P(u_p,v_p)` (density 20–40%, dot radius 0.5–1.5 px,
   Gaussian profile).
2. Projector pose `T_pw` and intrinsics `K_p` (from a nominal design; the exact vendor values are
   unknown).
3. For each visible 3D point, project into the projector, sample `P`, multiply by albedo and
   `cos` falloff, add ambient.
4. Blur with the camera PSF (`sigma` 0.6–1.2 px), apply gamma, quantise to 10/12 bit, add shot +
   read noise.

This produces images with **known correspondence** (the 3D point that generated each dot), so the
speckle backend can be scored on centre localisation and matching, not just end-to-end depth.

### 1.4 Noise and acquisition model

```
I = clamp( (g * (I_ideal + dark) + read_noise) * exposure_gain , 0, full_scale )
```

* shot noise: Poisson(`I_ideal * qe`);
* read noise: Gaussian(`sigma_read` 1–3 e-);
* PRNU: multiplicative 0.5–2%;
* exposure/gain per camera from a config (the vendor XML uses gain 400/exp 1500 for mono,
  180/1000 for colour — use those as one profile);
* optional projector flicker (5% sinusoidal) and ambient IR (0–1000 lux equivalent).

### 1.5 Motion sequences

For SLAM/tracking, generate a smooth hand-held trajectory (minimum-jerk segments + 0.5° jitter)
around the object, 150–400 frames, with ground-truth poses. Add motion blur proportional to
velocity and exposure. This is the only way to test tracking offline with GT.

---

## 2. Public datasets

| dataset | use | why | licence/notes |
|---|---|---|---|
| **ETH3D stereo** (low-res + full) | rectified stereo, confidence, occlusion | 1 MP laser-scanned GT, real sensors, indoor/outdoor | academic licence; download once, pin SHA-256 |
| **Middlebury 2014** | sub-pixel accuracy, high-res | very accurate GT, challenging lighting | academic licence |
| **KITTI 2015/2012** | learned-stereo sanity, outliers | real automotive, large baselines | CC BY-NC-SA |
| **DTU MVS** | MVS/mesh, TSDF/Poisson | 1600×1200, structured-light GT | academic |
| **TUM RGB-D** | SLAM ATE/RPE | standard benchmark, ground-truth motion capture | CC BY 4.0 |
| **EuRoC MAV** | stereo-inertial SLAM (we are IMU-less; use stereo only) | real drone, fast motion | CC BY 4.0 |
| **Replica** | synthetic SLAM + mesh | perfect GT geometry/poses | research licence |
| **BlendedMVS / ETH3D MVS** | texture/MVS | multi-view | academic |

Offline workflow: a `datasets/manifest.json` with URL, SHA-256, licence, and the exact subset used
(e.g. ETH3D `delivery_area_1l`, `electro_1l`, `forest_1l`; TUM `fr1/xyz`, `fr2/desk`; DTU scans
1, 24, 65). No dataset is committed; CI downloads only if the cache is empty.

---

## 3. Metrics and acceptance criteria

### 3.1 Rectification

| metric | definition | pass |
|---|---|---|
| epipolar RMS | RMS of `y_l - y_r` for GT correspondences after rectification | < 0.1 px |
| reprojection RMS | back-project rectified pixel, reproject with original K/D/R/T | < 0.1 px |
| LUT consistency | `remap` vs analytic projection | < 0.05 px |
| fill rate | valid pixels after rectify | > 99% |

### 3.2 Stereo / disparity

Let `V` be GT-valid pixels, `d` predicted, `d*` GT. Exclude occlusions and `|d*|>D_max`.

```
EPE      = mean_{p in V} |d(p) - d*(p)|
bad1     = 100 * mean( |d-d*| > 1.0 )
bad3     = 100 * mean( |d-d*| > 3.0 )
RMSE     = sqrt(mean((d-d*)^2))
completeness = |{p in V : d valid}| / |V|
```

| metric | synthetic L1 | ETH3D L2 | Middlebury |
|---|---:|---:|---:|
| EPE | < 0.25 px | < 1.0 px | < 0.5 px |
| bad1 | < 2% | < 10% | < 5% |
| completeness | > 95% | > 85% | > 90% |
| LR consistency rate | > 95% of valid | > 90% | > 90% |

### 3.3 Confidence / uncertainty

A confidence map is only useful if it predicts error. Test:

| metric | definition | pass |
|---|---|---|
| AUROC | confidence vs `|error| > 1 px` | > 0.85 |
| ECE | expected calibration error of `P(|error|>1px)` in 10 bins | < 0.10 |
| sigma ratio | median(`actual_error / sigma_z`) | 0.7–1.4 |
| coverage | fraction of `|error| < 2 sigma_z` | 0.90–0.98 |

A confidence that is monotonically related to error but mis-scaled is still useful for fusion
weights; the sigma ratio catches gross mis-scaling.

### 3.4 Depth / point cloud

| metric | definition | pass |
|---|---|---|
| depth RMSE | on GT-valid, metres | < 0.3 mm at 0.4 m synthetic |
| depth bias | mean signed error | < 0.1 mm |
| planar RMS | fit plane to a flat region | < 0.1 mm |
| sphere form error | fit sphere, RMS radial residual | < 0.1 mm |
| normal angular error | median angle vs GT | < 2° |

### 3.5 Tracking / SLAM

| metric | definition | pass |
|---|---|---|
| ATE (RMSE) | aligned trajectory vs GT, translation | < 1 mm synthetic; < 2 cm TUM |
| RPE | relative pose error, 1 m segments | < 0.5 mm / 0.2° per frame synthetic |
| drift | ATE / path length | < 0.1% synthetic; < 1% TUM |
| relocalisation | success within 5 frames after occlusion | > 90% |
| loop precision | true loops / accepted loops | > 95% |
| loop recall | accepted / GT loops | > 70% |

### 3.6 Fusion / mesh

| metric | definition | pass |
|---|---|---|
| Chamfer | symmetric mean nearest-neighbour distance | < 0.2 mm sphere/step |
| Hausdorff 95% | 95th percentile | < 0.5 mm |
| completeness | GT surface covered within 0.5 mm | > 98% |
| watertight | no boundary edges | true for closed objects |
| normal consistency | mean `1 - |n·n*|` | < 0.05 |
| triangle count | after decimation | 1–5 M for a 0.5 m object |

### 3.7 Texture

| metric | definition | pass |
|---|---|---|
| PSNR | rendered texture vs GT albedo, masked | > 30 dB |
| SSIM | 11×11 Gaussian, standard formula | > 0.90 |
| colour ΔE00 | mean CIEDE2000 vs GT | < 3 |
| seam error | gradient discontinuity at atlas seams | < 2% of mean gradient |
| exposure uniformity | std/mean over a flat chart | < 3% |

### 3.8 Performance

| metric | definition | pass |
|---|---|---|
| stage latency | CUDA events per stage, p50/p95 | see design budget |
| end-to-end | submit→mesh delta, p95 | < 40 ms live |
| GPU memory | `cudaMemGetInfo` high-water | < 10 GB |
| host memory | RSS high-water | < 8 GB |
| dropped frames | `dropped / submitted` over 10 min | < 1% |
| determinism | same input + seed → same output hash | exact for L0/L1 |

---

## 4. Experiment catalogue

Each experiment is a script `tests/experiments/E##_*.py` producing `results/E##.json` and a plot.
Seeds and dataset hashes are recorded in the JSON.

### E01 — Rectification correctness

* **Data:** synthetic pinhole pair with known `K, D, R, T`; plus ETH3D calibration.
* **Procedure:** build LUT; remap a dot grid; measure epipolar and reprojection RMS; compare
  analytic vs LUT.
* **Pass:** epipolar RMS < 0.1 px, reprojection < 0.1 px, fill > 99%.
* **Catches:** wrong distortion order, transposed R, pixel-centre convention.

### E02 — Disparity→depth and Q matrix

* **Data:** synthetic plane at 0.2/0.4/0.8 m, known baseline.
* **Procedure:** generate GT disparity, convert with `Z=fB/d`, compare to analytic depth; test
  off-axis (non-parallel) rectified rig.
* **Pass:** relative error < 1e-4; sign conventions correct.

### E03 — SGM on analytic scenes (no noise)

* **Data:** plane, sphere, step, corner; 5 disparities 64–512; census 5/7/9; 4 paths.
* **Procedure:** run `SgmBackend`; sweep `P1/P2`, window, sub-pixel method.
* **Metrics:** EPE, bad1, completeness, runtime.
* **Pass:** EPE < 0.15 px on fronto-parallel; < 0.35 px at 60° slant.
* **Output:** parameter sensitivity table (this becomes the default config).

### E04 — Noise / exposure sweep

* **Data:** E03 scenes with shot+read noise, exposure 0.5–8 ms, gain 0–24 dB, projector on/off.
* **Procedure:** 5 seeds per point; measure EPE vs SNR; fit `sigma_d(SNR)`.
* **Pass:** EPE < 0.3 px at SNR > 20 dB; graceful degradation (no catastrophic outliers).
* **Catches:** census instability at low contrast; sub-pixel bias.

### E05 — Speckle projector simulation

* **Data:** dot pattern, projector pose, 3D scene; render left/right with known dot correspondence.
* **Procedure:** run `SpeckleBackend`; measure centre localisation (px), match precision/recall,
  triangulation error; compare with SGM.
* **Pass:** centre error < 0.2 px; match precision > 95%; depth RMSE < 0.25 mm at 0.4 m.
* **Catches:** dot density, projector defocus, ambient IR.

### E06 — Left/right consistency and occlusion

* **Data:** scenes with thin structures and disocclusion (corner, cylinder, foliage-like).
* **Procedure:** compare no-LR, LR, LR+sub-pixel, LR+median+CCL.
* **Metrics:** bad1 in occluded vs visible regions; flying pixels at edges.
* **Pass:** occluded-region bad1 < 5%; edge flying pixels reduced > 50% vs no-LR.

### E07 — Confidence calibration

* **Data:** E04/E06 outputs.
* **Procedure:** compute cost curvature, LR margin, texture density, learned variance; fit a
  monotone map to `|error|`; compute AUROC/ECE/sigma ratio.
* **Pass:** AUROC > 0.85, ECE < 0.10, sigma ratio 0.7–1.4.
* **Catches:** over-confident cost curvature on repetitive texture.

### E08 — TSDF vs analytic SDF

* **Data:** sphere/plane/step; synthetic depth + poses; known SDF.
* **Procedure:** integrate 1/10/100 frames with inverse-variance weights; extract MC; compare
  voxel SDF and mesh to analytic.
* **Metrics:** SDF RMSE, Chamfer, bias vs frame count, memory.
* **Pass:** SDF RMSE < 0.5 voxel; Chamfer < 0.2 mm at 0.5 mm voxel; bias decreases with frames.

### E09 — Pose-aware fusion / drift sensitivity

* **Data:** synthetic sequence with GT poses; inject pose noise (0.1/0.5/1 mm, 0.05/0.2°).
* **Procedure:** fuse with GT, noisy, and estimated poses; measure surface thickness/double layers.
* **Pass:** 0.5 mm pose noise → surface thickness < 2 voxels; no double surfaces at 1 mm.
* **Catches:** fusion without pose covariance; stale poses.

### E10 — Tracking on synthetic + TUM

* **Data:** synthetic motion sequences; TUM `fr1/xyz`, `fr2/desk`; EuRoC stereo.
* **Procedure:** ORB + ICP + iSAM2; ablate motion model, loop closure, robust kernel.
* **Metrics:** ATE, RPE, drift, relocalisation, loop precision/recall.
* **Pass:** synthetic ATE < 1 mm; TUM < 2 cm; loop precision > 95%.

### E11 — Mesh and texture end-to-end

* **Data:** DTU scans + synthetic textured objects.
* **Procedure:** MVS/TSDF → MC/Poisson → atlas → colour; compare to GT.
* **Metrics:** Chamfer, completeness, PSNR, SSIM, ΔE00, seams.
* **Pass:** Chamfer < 0.3 mm DTU; PSNR > 30 dB; ΔE00 < 3.

### E12 — Hardware bring-up (scanner required)

See §6.

### E13 — Learned stereo evaluation (optional)

* **Data:** ETH3D/KITTI zero-shot; then 200–500 hand-labelled Einstar pairs (or GT from a
  structured-light scan of the same object).
* **Procedure:** ONNX→TensorRT FP16; calibration-aware refinement; compare to SGM.
* **Metrics:** EPE/bad1, temporal flicker (frame-to-frame disparity std on static scene), GPU
  memory, latency.
* **Pass:** beats SGM by >20% bad1 on low-texture, no regression on textured, < 50 ms, < 4 GB.
* **Gate:** only promote to default if it also passes E07 confidence calibration.

---

## 5. CI and reporting

* `ctest -L l0` on every commit; `-L l1` on PR; `-L l2` nightly.
* Results are JSON with `{git_sha, cuda_version, gpu, dataset_sha, seed, metrics, pass}`.
* Regression gate: a metric may not worsen by more than 5% relative without an explicit waiver.
* Plots: disparity error heat-map, confidence reliability diagram, ATE trajectory, Chamfer
  histogram, latency percentile.
* Artifacts: keep the smallest failing frame per stage for debugging (not the whole dataset).

---

## 6. Hardware benchmark plan (when the scanner is available)

### 6.1 Instruments and targets

| item | purpose | notes |
|---|---|---|
| calibrated sphere Ø50 mm (λ/10) | form error, diameter | known certificate |
| step gauge (0.1/0.5/1/5 mm) | step height, edge behaviour | |
| flat granite plate | flatness, noise floor | |
| calibrated cube / gauge block | scale, orthogonal axes | |
| NIST-traceable caliper | independent check | |
| thermo-hygrometer | thermal drift | log every 10 s |
| lux meter | ambient light | 0/300/1000 lux |
| tripod + rotary stage | repeatability, angular coverage | |

### 6.2 Scan protocols

| protocol | procedure | metric |
|---|---|---|
| **P1 repeatability** | 10 scans of the sphere, same pose, scanner restarted between | diameter mean/std, form error |
| **P2 pose repeatability** | 10 scans, re-mounted each time | sphere centre scatter |
| **P3 distance** | sphere at 250/350/450/550 mm | scale error, noise vs distance |
| **P4 angular** | sphere at 0/30/45/60/75° incidence | dropout, bias |
| **P5 material** | matte white, black (albedo 0.02), shiny steel, transparent | coverage, outliers |
| **P6 ambient** | 0/300/1000 lux, projector on/off | depth noise, holes |
| **P7 thermal** | 30 min continuous scan of granite | drift (µm/°C, µm/min) |
| **P8 motion** | 5/15/25 fps equivalent speeds, fast rotation | tracking loss, blur, double surfaces |
| **P9 complexity** | turbine blade / dental model / black plastic | coverage, mesh completeness |
| **P10 comparative** | same object, vendor EXStar vs new pipeline, same calibration | paired difference |

### 6.3 Acceptance targets

| metric | target | stretch |
|---|---:|---:|
| sphere diameter error | < 0.1 mm | < 0.05 mm |
| form error (RMS) | < 0.1 mm | < 0.05 mm |
| flatness (granite) | < 0.05 mm | < 0.03 mm |
| step height error (1 mm) | < 0.05 mm | < 0.02 mm |
| repeatability (1σ) | < 0.05 mm | < 0.02 mm |
| thermal drift | < 0.1 mm over 30 min | < 0.05 mm |
| coverage (complex object) | > 95% | > 99% |
| tracking loss | < 1 per 5-min scan | 0 |
| live latency p95 | < 40 ms | < 25 ms |
| dropped frames | < 1% | < 0.1% |
| GPU memory | < 10 GB | < 8 GB |

### 6.4 Comparative study design

* 3 objects × 3 distances × 3 materials × 5 repeats = 135 scans per system.
* Randomise order; same operator; same calibration target; log temperature.
* Metrics: bias (paired difference), repeatability (variance ratio), coverage, time.
* Statistics: paired Wilcoxon signed-rank; report median and 95% CI; a 0.02 mm difference needs
  ~30 paired scans to detect at 80% power (estimate; verify with pilot).
* Blind evaluation: an independent operator measures the exported meshes in GOM/CloudCompare.

### 6.5 What to instrument in the new pipeline

* per-stage CUDA events, `cudaMemGetInfo`, `nvidia-smi` power/clock, CPU per-thread time;
* frame drop reason (`usb_timeout`, `queue_full`, `gpu_busy`, `projector_miss`);
* pose covariance and ICP inlier ratio per frame;
* TSDF active voxel count, hash collisions, mesh delta size;
* calibration file hash, firmware version, temperature.

---

## 7. Reproducibility checklist

- [ ] seeds fixed in every generator (`numpy.random.default_rng(seed)`);
- [ ] dataset files pinned by SHA-256 in `datasets/manifest.json`;
- [ ] CUDA / driver / compiler versions in results JSON;
- [ ] synthetic scene parameters serialised next to the images;
- [ ] CI stores the smallest failing artifact;
- [ ] hardware runs record scanner serial, firmware (`docs/firmware/` versions), calibration hash,
      ambient temperature, and operator;
- [ ] no result is reported without the git SHA and the exact command line.

---

## 8. Minimum viable validation (first two weeks)

1. E01 rectification on synthetic + one ETH3D pair.
2. E02 disparity→depth on a synthetic plane.
3. E03 SGM on plane/sphere/step at D=128 and D=256.
4. E04 noise sweep at 3 exposures.
5. E08 TSDF on a synthetic sphere with GT poses.
6. E10 tracking on 100 synthetic frames.

If these pass, the core geometry is trustworthy enough to start hardware integration. If E03 or
E08 fails, do not add texture or learned stereo — fix the geometry first.
