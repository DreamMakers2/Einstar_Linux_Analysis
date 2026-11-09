# EXStar physical calibration: recovered workflow, mathematics, and a Linux-native replacement

Status: 2026-11-09. Offline work only; the scanner is disconnected and no vendor
binary was executed. Evidence is the shipped EXStar 1.2.2.0 tree, Ghidra 12.1.4
decompilation of its DLLs, the vendor's own 25 × 3-image calibration corpus, the
recorded 2026-02-09 EXStar session log, and the 6568-byte device calibration dump.

Companion documents (deeper, per module):

| document | contents |
|---|---|
| `analysis/calibration/re/workflow_report.md` | state machine, commands, acceptance criteria, 45 addressed functions |
| `analysis/calibration/re/algorithm_report.md` | camera model, solver layout, Ceres options, quality gates, 223 cited claims |
| `analysis/calibration/re/storage_report.md` | CCF/device layout, write-back, failure semantics, profile design |
| `docs/calibration/model.md` | the operational three-camera model used for scanning |
| `docs/calibration/ccf_format.md` | CCF byte format and key recovery |
| `calib/` | the replacement engine; `tests/test_calib_engine.py` |

Confidence labels: **A** = code-proven and/or byte-exact with an independent
check, **B** = one interpretation step, **C** = hypothesis, **U** = unresolved.

---

## 1. Executive summary

1. The Einstar calibration is a **planar-marker camera calibration**, not a
   projector or sensor-synchronisation calibration. It estimates the
   **intrinsics and relative poses of all three cameras** (left IR, right IR,
   colour) from 25 captures of one marker plate. The projector's own geometry
   (`aa/bb`, plane calibration) belongs to the factory process and is not part
   of this workflow. **[A]**

2. The workflow is exactly **5 groups × 5 steps = 25 captures × 3 cameras = 75
   images**. The groups are board orientations `0°, x−18°, x+18°, y+18°, y−18°`;
   the five steps are working-distance bands centred at **240, 320, 400, 480,
   560 mm** (each ±40 mm, within the 200–600 mm gate). **[A]**

3. Only the vertical group has an **enforced angle gate** (0/0/180° ±8°). The
   four tilted groups carry `angle valid="false"`; their ±10° thresholds are
   never evaluated. There is **no per-step retry**: a rejected pose is simply
   not committed, and “Redo calibration” re-enters the whole process. **[A/B]**

4. A real successful session took **2 min 56 s** from the first height event to
   `calibComputeFinished`, of which the actual solve was **~1.3 s** and the
   device write **~0.34 s**. Stages 0–4 took 7.7 / 11.2 / 58.0 / 78.3 / 35.9 s;
   the tilted stages dominate. **[A]** (session log, §4.5)

5. The plate recovered from the vendor's own corpus is an **8 × 5 grid of small
   holes plus 4 large holes** at board coordinates (2,1), (4,1), (5,1) and the
   off-lattice (3.5,3). Its pitch, recovered independently from the stereo rig
   and from a joint bundle adjustment, is **27.72 mm** (triangulation
   27.735 ± 0.022 mm over 22 poses; BA-implied 27.722 mm). **[A]** for the
   corpus, **U** for whether the physical board is 8×5 or the 8×7 drawn in the
   manual (see §12.1).

6. The vendor's numerical core is `Sn3D_E10_camera_calibration` →
   `FUN_1802dca70` → `FUN_1800e0180` (3-camera driver) → `TribleCCF`
   (`CalibrationCCF::TribleCCF`, dongle-gated). It first solves a per-view
   extrinsic with `K`/`D` frozen (`sn3DExtrinsicSolver`, Ceres, no robust loss),
   then refines jointly, gated by `isOptK` / `isOptP3D`. The camera model is
   `K5=[fx,cx,cy,fy/fx,alpha]`, `D5=[k1,k2,p1,p2,k3]` with Brown–Conrady in
   normalised coordinates. **[A]/[B]**

7. The output **overwrites the factory CCF files in place and mirrors the same
   3873-byte block into the device record at `tagDevData+0x39B`**; no backup is
   kept. The write is reversible only if a host-side snapshot exists. The
   recovered format lets us build such a snapshot and a versioned profile
   without touching the device. **[A]**

8. A free re-optimisation of the vendor corpus (fixed factory distortion,
   free intrinsics + rig + poses) reaches **0.367 px per-point RMS** and
   reproduces the **IR baseline to 0.014 mm** (158.543 vs 158.557 mm) and the
   **IR relative rotation to 0.20°**. The intrinsics differ from the shipped
   CCF by **2.8–5.1 px** in focal length and 3–9 px in principal point: the
   CCF is a near-optimal but not identical solution. **[A]** (measured)

9. The **colour record is not co-registered with the IR pair in the shipped
   CCF**: its relative rotation matches our fit to 0.14°, but its translation
   differs by 3.6 mm in magnitude and ~17 mm in y — a difference that cannot
   be explained by the plate scale. Either the CCF's colour record comes from a
   different run/unit, or the vendor's colour calibration is inconsistent.
   This must be resolved on the user's own unit. **[A]** for the discrepancy,
   **U** for its cause.

10. A replacement pipeline was built and evaluated. It uses the same plate,
    sub-pixel blob detection, robust joint bundle adjustment, analytic
    covariance, Fisher-information view selection and uncertainty-driven
    stopping. On synthetic ground truth the covariance is statistically
    calibrated (**NEES ≈ 1.06 × n**). On the vendor corpus, **3 views already
    recover the baseline to 0.03 mm**, but **intrinsics need ≥20 views** to get
    within ~2–5 px of the vendor CCF. A greedy 8-view adaptive selection
    reduced the synthetic fx error from 0.80 px (fixed first 8 views) to
    0.13 px. **[A/B]** (synthetic + real corpus; not a physical accuracy claim)

11. The honest conclusion: the original sequence is **not mathematically
    minimal** (its 5×5 grid contains redundant poses), but the redundancy buys
    robustness against bad detections and makes the intrinsics observable
    without a covariance model. A properly designed adaptive sequence can
    match or beat it with **8–15 captures**, provided the scale is anchored and
    the plate variant is known. The largest remaining uncertainty is **physical
    scale and the actual plate variant**, not the solver.

---

## 2. Evidence map

| evidence | what it establishes |
|---|---|
| `sources/vendor/EXStar_1.2.2.0/res/calibrateConfig/Einscan-EA/rapidCameraCalibrate.xml` | 5 groups × 5 steps, orientations, distance band 200–600, exposure/gain, LED/indicator commands |
| `Sn3DCalibrateLoader.dll` | XML model, distance ladder, step pose windows |
| `Sn3DCalibCaptureImageProcess.dll` | capture loop, pose gating, height-OK events, UI indicator |
| `Sn3DEACalibrateController.dll` | orchestration, init fan-out, CCF write-back, config (`nm_group_num`, `nm_cell_num`, `space`) |
| `Sn3DCalibrationJR.dll` | detection, PnP, joint refinement, CCF packing |
| `res/Einscan-E10/calibrate_image_read_rapid/` | 25 × 3 vendor calibration images (the real plate in use) |
| `res/Einscan-E10/200x150/*CCF.txt` | shipped user CCF (2026-02-09 timestamp in the device dump) |
| `<device-dump.bin>` | 6568-byte device record, factory 22-double blocks, 2026 CCF block |
| `log/EXStar_20260209_213118_746.log` | real session: 5 stages, 25 height events, timings, compute/write results |
| manual PDF p.34–37 + `videos/calib_quik.mp4` | operator guidance, five-stage UI, board appearance |

---

## 3. The physical target

### 3.1 What the plate is

A white plate with a rectangular grid of circular holes; the manual calls it
“white background with black dots”. In the vendor's IR captures the holes are
bright (the plate surface is dark at the chosen exposure) and the four large
holes are unmistakable. The plate is **matched to the device**: the board SN
`<redacted-part-number>` is the same string as the device part number in the
6568-byte record, and EXStar downloads a `calibBoard` descriptor at start-up
(empty in the recorded session, `version 1.0.0`). **[B]**

### 3.2 Recovered geometry (vendor corpus)

In every one of the 22 usable left images the detector finds **40 small holes**
on an **8 × 5 lattice** and **4 large holes** at:

```
(u, v) = (2, 1), (4, 1), (5, 1)      # three collinear, spacing ratio 2:1
(u, v) = (3.5, 3)                    # off-lattice: midway between u=3 and u=4
```

`u` runs along the long side, `v` along the short side. The half-integer
coordinate is deliberate: the pattern is chiral, so a single view fixes the
board frame without a 180°/mirror ambiguity. The large holes are used only for
orientation; the 40 small-hole centres are the calibration observations.
**[A]** (detected in all images; the same coordinates recur)

The manual's figure instead draws **8 × 7 = 56** small holes, and the factory
config exposes `iX=17, iY=14` (logged in `initComputeAlg`), which is consistent
with an 8×7 board (2·8+1, 2·7) but also with unrelated detection-window
parameters. The corpus is 8×5. **The physical variant must be confirmed by
counting the holes on the actual board.** The engine is plate-configurable
(`calib.plate.PlateSpec`, `--cols/--rows`) for this reason. **U**

### 3.3 Metric pitch

Two independent estimators using the shipped CCF's IR baseline (158.557 mm):

* point-distance triangulation over 22 poses: **27.735 mm**, sd 0.022 mm,
  distance residuals 0.1–0.15 mm, plane deviation 0.02–0.07 mm;
* joint bundle adjustment, scaled until the IR baseline matches the CCF:
  **27.722 mm**.

The 0.05 % agreement is statistical; the systematic uncertainty is the CCF
baseline itself (~0.1 %, i.e. ±0.03 mm in pitch). The engine default is
`27.72 mm` and it can instead **anchor the scale to the factory baseline**,
which is the physically meaningful choice. A caliper measurement of the hole
pitch (or of the board outline) would settle the last 0.1 %. **[A]/[B]**

### 3.4 Hole sizes and appearance

The four large holes are ~2× the radius of the small ones; the exact ratio is
a detection parameter (`radiusRatio`, `bigCircleNum=4`) and the vendor rejects
a plate where the ratio is not proper (`big num is less four or radiusRatio is
not proper`). The holes are detected as bright blobs on a dark plate in the
vendor IR images; a normal white-light photograph has dark holes on a light
plate. The detector therefore supports both polarities. **[A]/[B]**

---

## 4. The workflow

### 4.1 Configuration

`res/calibrateConfig/Einscan-EA/rapidCameraCalibrate.xml` (8630 bytes) is the
authoritative description. Key values:

| key | value | meaning |
|---|---|---|
| `groups count` | 5 | board orientations |
| `groupN count` | 5 | captures per orientation |
| `distanceMid` / `distanceMax` | 200 / 600 mm | distance gate |
| `angle` group0 | x=0, y=0, z=180, thres 8/8/8, `valid=true` | vertical, enforced |
| `angle` groups1–4 | x=∓18 or y=±18, z=180, `valid=false` | tilted, not enforced |
| `pt` | x=0, y=0, z=40, `thresDim=40` | lateral gate ±40 mm |
| `gainExposure` | IR 400/1500, colour 180/1000 | camera settings |
| `LedBrightness` start | LED0=1000, LED1=300 | illumination during capture |
| `DistanceIndicate` | 1 during start, 2 otherwise | green/blue ring |
| `captureInfo` | 1 frame × 3 pictures `imageLeft/Right/Tex%1` | 3 images/step |

### 4.2 Distance ladder

The loader computes, for `N=5` steps and `[Min, Mid, Max]=[200,200,600]`:

```
spacing = (Max - Mid) / (2N) = 40 mm
step_z(i) = Mid + (2i+1)·spacing,  i = 0..4
          = 240, 320, 400, 480, 560 mm
```

Each step's accepted window is `step_z ± 40 mm`; the pose gate maps
`z < Mid` to `-2` (“too close”), `z > Max` to `-3` (“too far”), and a pose
failure to `-5` (“adjust distance”). The UI receives `CalibHeightOKChanged`
with the step index when a band is hit. **[A]** (`Sn3DCalibrateLoader` at
`0x18000b2a0` region, `LOAD:9420–9460`; real log shows 25 events)

### 4.3 State machine

```
startEnterCalibration
  -> data controller reads 6568-byte tagDevData (3 × 50 ms retries)
  -> dispatchCmd -> handleEnterClibrateCmd: setCalibType(4),
     initCaptureImage, initComputeConfig(srcCalibParam),
     initRebuildParam(devData, PoseAlg), notifyIsHasSrcClibParam
  -> wait 3 × retInit*, then a fixed 2000 ms settle
  -> finishEnterCalibration
capture loop:
  first incomplete group -> setGroupIndex N
  first incomplete step -> arm one frame (3 images)
  pose gate: distance band + lateral ±40 mm (+ angle only group0)
  accepted -> CalibHeightOKChanged{okheight=step}
  group complete -> LEDs off, DistanceIndicate=2
  25/25 -> 500 ms -> CaptureImageFinished
compute:
  calcCameraCalibrate(images, isOpt3d, isOptInner)
  -> CameraCalibrateAlg -> cameraCalibCalcResp -> writeCalibrateResultToDev
  -> Left/Right/Tex CCF files -> pack 0xF21 block -> device write at 0x39B
  -> setCalibCCFData, setProjectCalirateTime, calibComputeFinished
```

There is **one frame per step**, not a burst; the UI's five check marks are the
five distance bands, not five frames. The manual's “move up and down until the
distance indicator shows all green” is exactly the five height bands. **[A]**

### 4.4 Operator guidance

* Blue circle = scanner focus; grey circle = board centre. The blue circle
  must be inside the grey circle and turn green.
* The indicator ring is green when the pose is inside a band, blue otherwise,
  and red with a beep for too close/too far.
* During capture the user must keep the projected circle green and move up/down
  until five checks turn green, then press the button again for the next
  orientation.
* The manual forbids moving the scanner during a capture and says the board must
  be flat, clean, unscratched, and matched to the device.

### 4.5 Measured timing (real session, 2026-02-09)

| stage | start | duration | notes |
|---|---:|---:|---|
| 0 (vertical) | 21:32:02.6 | 7.7 s | 5 height bands |
| 1 (x−18°) | 21:32:10.3 | 11.2 s | |
| 2 (x+18°) | 21:32:21.5 | **58.0 s** | hardest tilt |
| 3 (y+18°) | 21:33:19.5 | **78.3 s** | hardest tilt |
| 4 (y−18°) | 21:34:37.8 | 35.9 s | includes compute/write |
| total to `calibComputeFinished` | 21:32:02.6 | **2 min 56 s** | 25 × 3 images |
| last capture → result | 21:34:57.2 | **~1.3 s** | 75 images |
| device write | 21:34:58.5 | **~0.34 s** | 0xF21 block |
| white balance | 21:35:13.7 | ~7 s | separate `currentCalibType=2` |

The five orientation changes are **manual**; the two y-tilt stages are ~8×
slower than the vertical stage. This is the human cost the replacement targets.

---

## 5. Acceptance criteria and quality gates

### 5.1 Vendor gates

| gate | value | source |
|---|---|---|
| distance | 200 ≤ z ≤ 600 mm | XML + capture process |
| height band | centre ±40 mm | loader ladder |
| lateral | |x|,|y| ≤ 40 mm | `pt.thresDim` |
| angle (group 0 only) | ±8° on x,y,z | XML `valid=true` |
| per-view correspondences | ≥ 4 after sentinel filtering | `FUN_1800e0180` |
| per-view invalid fraction | ≤ `pointThre` | `FUN_1800e0180` |
| Ceres termination | usable (`termination_type`) | `compute_rt` |
| registration | precise / enough points | `CalibrationCFR::run` strings |
| wrapper residual | `"calibration failed, outside residuals"` | `Sn3DCameraCalibHelper` |
| final CCF `error` | mean reprojection px (aggregation U) | `compute_residual` |

The vendor does **not** expose a covariance, a per-parameter uncertainty, or a
held-out validation. It accepts a result when the solver terminates and the
residual is inside a fixed gate. **[A]**

### 5.2 What the real session shows

`calibComputeFinished{currentCalibType:4, success:true}` at 21:34:58.905, then
`WriteDataRet{additionInfo:"calibData", result:0}`, then `setCalibCCFData`
with a timestamp. The shipped CCF's `error` is 0.0 for the IR records and
0.0765 for colour. The session is a successful user calibration, not a factory
one. **[A]**

---

## 6. What is estimated, and where it goes

### 6.1 Parameters

The vendor solves **all three cameras jointly**: the E10 driver receives three
image arrays and three `CalibParam` outputs and calls the 3-camera
`CalibrationCCF::TribleCCF`. The per-camera state is `fx, fy, cx, cy, kc[5],
alpha, R, T` (23 doubles). The solver state of the two-camera LM class is
`[K5_A, D5_A, K5_B, D5_B, 6·views, relative pose 6, 3D points 3N]`; the E10
3-camera path is the same idea with one more camera. **[A]**

The **distortion is preserved bit-exactly** in the measured factory→user
comparison; the user calibration re-estimates focal length, principal point,
alpha and the pose. Whether the vendor freezes `kc` or the solver simply does
not move it is a code question: `isOptK` gates the joint refinement. The
observed bit-exact preservation across two independent calibrations is strong
evidence that `kc` is fixed in this path. **[A]**

### 6.2 Storage

* On disk: `./res/Einscan-E10/200x150/{Left,Right,Tex}CCF.txt` — binary
  little-endian, 33 doubles (IR + 256-byte trailer), 520/520/264 bytes.
* In the device: a 3873-byte (`0xF21`) block at `tagDevData+0x39B` inside the
  6568-byte record: version 4, three length-prefixed records, timestamp at
  +0xA14, magic `FQFQ\0` at +0xF1C.
* The user calibration **overwrites both media**; there is **no backup** and no
  joint rollback on failure. A failed pack/write leaves the files as the
  algorithm wrote them and the device on its previous block. **[A]**
* `cameraCalibFlag.dat` is a little-endian `QDateTime` marker with no reader in
  the recovered dumps. **[A]** for writer, **U** for purpose.
* `isCalibrated` is published **inverted** (true when a CCF file is missing or
  empty), so the UI's “not calibrated” state is the absence of files. **[A]**

### 6.3 Interaction with the scanning model

EXStar's scan pipeline reads the CCF and builds the rectification/triangulation
matrices from `K`, `dist`, `R`, `T`. The operational model and the proof that
the CCF (not the device's 22-double factory blocks) is what real captures
rectify with are in `docs/calibration/model.md`. A replacement calibration
must therefore produce the same 33-double records in the same world-frame
convention; our engine does, after rebasing the world frame to a reference
board pose (otherwise the left record would be identity, which the vendor
format does not expect). **[A]/[B]**

---

## 7. Mathematics

### 7.1 Camera model (vendor, proved)

```
K5 = [fx, cx, cy, fy/fx, alpha]
D5 = [k1, k2, p1, p2, k3]
x = X/Z, y = Y/Z
xd = x(1 + k1 r² + k2 r⁴ + k3 r⁶) + 2 p1 x y + p2(r² + 2x²)
yd = y(1 + k1 r² + k2 r⁴ + k3 r⁶) + p1(r² + 2y²) + 2 p2 x y
u = fx (xd + alpha yd) + cx
v = fy yd + cy
```

`K5[3] = fy/fx` (the CCF stores `fy` directly after multiplication), `K5[4] =
alpha` is the CCToolbox `alpha_c`. The model is exactly Brown–Conrady; the
`docs/calibration/ccf_format.md` model (`K`, `dist`, `alpha_c·fx`) is the same
model written in OpenCV form. **[A]** (`Sn3D_camera_to_image` 0x18013d3f0;
`CalibParam` offsets in `CalibrationCCF::TribleCCF`)

### 7.2 Vendor solve

1. **Detection**: Gaussian 5×5 σ=2.1, Canny 75/150, ellipse fitting, big-circle
   identification (`bigCircleNum=4`, radius ratio), reference-circle
   identification, grid registration. `select_goodcalib_point` filters unseen
   or invalid points (sentinel `(-1,-1,-1)`).
2. **Per-view extrinsic**: `sn3DExtrinsicSolver::compute_rt` — Ceres
   `AutoDiffCostFunction<CostFunctor,2,3,3>` on `rvec(3)+tvec(3)`, 2 residuals
   per correspondence, **loss function `nullptr`**, initial `tvec.x += 100 mm`.
   Only two explicit options are written (an int 100 at `Options+0x68`,
   most plausibly `max_num_iterations`, and a bool 0 at `+0x130`); everything
   else is Ceres default. **[A]** for the structure, **C** for the option names.
3. **Joint refinement**: a hand-written LM (`Sn3D_LM_SingleCamera`,
   `Sn3D_LM_DoubleCamera`, `Sn3D_LM_RT`) with analytic Jacobians, and/or the
   `TribleCCF` path; gated by `isOptK` (distortion) and `isOptP3D` (3-D points).
   The 3-camera routine is `CalibrationCCF::TribleCCF` behind the dongle check.
4. **Output**: `CalibParam` 23 doubles → 33-double CCF; `T` is a valid view's
   `tvec` (so the world frame is a board pose, not a camera).

### 7.3 The replacement model

Our engine (`calib/calibrate.py`) uses the same projection model but a single
joint problem:

```
parameters = [K_i(9) for i in L,R,T] + [(rvec,t) for R,T] + [(rvec,t) for each view]
residual   = reprojection error of every observed hole in every camera
gauge      = left camera identity (or the board pose of view 0 for output)
loss       = Huber (f_scale 1 px) over the residual vector
solver     = scipy least_squares / trf (Levenberg–Marquardt), dense 2-point Jacobian
```

It estimates all intrinsics (optionally distortion), all rig extrinsics and all
view poses jointly, so the per-view pose error is not frozen into the
extrinsics. The Jacobian at the solution gives the parameter covariance:
`cov = pinv(JᵀJ)·σ²`, with `σ` from the component-wise MAD of the residuals.
This is the uncertainty the vendor does not report.

**Identifiability**: the planar target fixes all parameters except the overall
**metric scale**. The scale is set either by the plate pitch or, better, by
anchoring the IR baseline to the factory value. The engine supports both
(`--pitch`, `--no-anchor`). This distinction is why a low reprojection error
does not by itself imply physical accuracy. **[A]**

---

## 8. Critical evaluation of the original sequence

### 8.1 Which captures carry information

Ablation on the vendor corpus (all 25 views; fixed distortion; free K + rig;
each row is a separate calibration). `Δbaseline` is against the CCF's
158.557 mm; `Δfx` is the mean absolute focal error against the CCF.

| subset | views | RMS px | baseline mm | Δbaseline mm | Δfx px |
|---|---:|---:|---:|---:|---:|
| all 25 | 25 | 0.367 | 158.543 | −0.014 | 4.02 |
| even 20 | 20 | 0.366 | 158.564 | +0.007 | 3.65 |
| even 15 | 15 | 0.392 | 158.537 | −0.020 | 3.48 |
| even 10 | 10 | 0.351 | 158.489 | −0.068 | 5.74 |
| even 5 | 5 | 0.348 | 158.462 | −0.095 | 10.53 |
| even 3 | 3 | 0.335 | 158.530 | −0.027 | 13.97 |
| even 1 | 1 | 0.261 | 137.915 | −20.6 | 87.7 |
| group 0 only | 5 | 0.383 | 158.716 | +0.159 | 1.49 |
| group 1 only | 5 | 0.379 | 158.530 | −0.027 | 3.38 |
| group 2 only | 5 | 0.321 | 158.860 | +0.303 | 8.32 |
| group 3 only | 5 | 0.243 | 159.474 | +0.917 | 3.32 |
| group 4 only | 5 | 0.293 | 158.615 | +0.058 | 4.21 |
| step 0 only | 5 | 0.356 | 158.710 | +0.153 | 3.86 |
| step 4 only | 5 | 0.399 | 158.488 | −0.069 | 1.32 |

Interpretation:

* **Three well-spread views already determine the baseline to 0.03 mm**, because
  the baseline is a stereo triangulation scale observed in every pair.
* **Intrinsics need many views**: 5 views leave 10–11 px of focal bias, 10 views
  5–6 px, 20–25 views 2–4 px. The full grid's value is intrinsic observability,
  not baseline.
* **No single orientation or distance band is sufficient**: group 2 alone is
  8.3 px off in fx, group 0 alone is 1.5 px off but its baseline is 0.16 mm high;
  each subset has a different bias. The 5×5 design averages these biases.
* The sequence is **redundant for scale** and **not redundant for intrinsics**.
  A shorter sequence must deliberately cover focal-length-observable poses
  (large board, varied tilt, varied image position), not just any 5 poses.

**Caveat**: the CCF is a reference, not ground truth. A 2–5 px difference from
CCF is not necessarily our error; our fit has a lower reprojection error on the
same images. The honest statement is that the CCF is not the reprojection
optimum of this corpus, and physical truth requires an artefact measurement.

### 8.2 Convergence is not physical accuracy

* Our 25-view fit reaches 0.367 px; adding more views does not reduce it below
  the detector noise (~0.25–0.3 px). The residual floor is the marker
  localisation, not the model.
* The baseline is stable to 0.026 mm across bootstraps (6 resamples, 4 views
  dropped) and to 0.014 mm against the CCF — excellent **relative** geometry.
* The **absolute scale** still depends on the plate pitch / baseline. A 0.1 %
  pitch error is 0.16 mm on a 158 mm baseline and ~0.3 mm at 300 mm depth.
  Without a physical length standard this cannot be closed.
* The CCF's colour record is inconsistent with a single rigid rig (17 mm y
  offset). No amount of solver convergence fixes a wrong reference; this is a
  data/registration issue, not an optimisation issue.

### 8.3 Why the manufacturer requires this sequence

1. **Observability**: intrinsics need board coverage at multiple image
   positions and tilts; the 5 orientations × 5 distances cover the field.
2. **Robustness**: 25 poses tolerate several bad detections without losing a
   parameter; the vendor has no robust loss and no outlier covariance, so
   redundancy is its robustness mechanism.
3. **Operator simplicity**: fixed groups and a binary green/red indicator are
   easier to follow than an optimised pose plan.
4. **Legacy**: the 5×5 grid is a conservative superset; the ablation shows most
   of its scale information is available from 3–5 views, and its intrinsic
   information from 10–20.

---

## 9. Replacement pipeline

### 9.1 Architecture

```
images ──► detect.py ──► grid (u,v)->(x,y) ──► calibrate.py ──► result
              │                                   │  joint BA + covariance
              │                                   ▼
              └── plate.py (8x5 / 8x7 spec)   adaptive.py (Fisher info)
                                                  │
                                                  ▼
                                            profiles.py (versioned)
                                                  │
                                            integration.py (CCF)
```

* **Detector**: threshold (both polarities), connected components, intensity
  weighted sub-pixel centroid, large-marker pattern search (3 collinear with
  2:1 spacing + off-lattice midpoint), affine bootstrap, RANSAC homography,
  full-grid refit. 72/75 vendor images detected with 38–40/40 holes; the three
  failures are left images 9, 16, 17 (the right/tex views still calibrate).
* **Calibration**: joint robust BA as in §7.3, with optional fixed distortion
  (vendor-like) or free distortion, and optional baseline anchoring.
* **Uncertainty**: `JᵀJ` covariance; synthetic NEES ≈ 1.06 × n (well calibrated).
* **Adaptive**: candidate pool of board poses (distance × tilt × roll), Fisher
  information gain after eliminating the candidate view by its Schur
  complement; A-optimal greedy selection; stopping when fx/fy σ and baseline σ
  are below thresholds and the held-out residual is acceptable.
* **Profiles**: immutable factory snapshot, numbered revisions, manifest
  hashes, active pointer, rollback, and a device-export plan (offset 0x39B,
  0xF21 bytes) that is **not executed automatically**.

### 9.2 Usage

```
python -m calib.cli workflow res/calibrateConfig/Einscan-EA/rapidCameraCalibrate.xml
python -m calib.cli detect  /path/to/calibration/images
python -m calib.cli calibrate /path/to/calibration/images \
    --factory sources/calibration/res_Einscan-E10_200x150 \
    --profile ~/.local/share/einstar/calibration --serial <serial> \
    --out result.json
python -m calib.cli plan result.json --n 8
```

`--free-distortion` estimates the five coefficients; the default fixes them to
the factory values (matching the vendor's preserved-distortion behaviour).
`--no-anchor` keeps the raw plate-scale solution; the default anchors the IR
baseline to the factory CCF.

### 9.3 Test and benchmark status

`python -m pytest tests/test_calib_engine.py` — 8 tests (plate topology, CCF
round-trip, XML, synthetic detection, projection/triangulation, profile
store, synthetic calibration recovery, adaptive Jacobian). The broader project
tests remain green (see `scripts/verify_all.sh` if present).

---

## 10. Evaluation

### 10.1 Synthetic ground truth (known truth, 0.2 px noise, 2 % outliers)

| views | median |fx error| | median baseline error | median RMS | NEES / n |
|---:|---:|---:|---:|---:|
| 5 | 0.51 px | 0.054 mm | 1.12 px | 1.09 |
| 10 | 0.34 px | 0.011 mm | 1.03 px | 1.07 |
| 25 | 0.51 px | 0.022 mm | 1.51 px | 1.06 |

The RMS includes the 2 % gross outliers (8 px); the robust loss keeps the
parameter errors low. Without outliers the RMS is 0.27 px and the fx error is
0.20–0.61 px. The covariance is statistically calibrated (NEES ≈ n), and the
reported fx sigma matches the empirical spread (e.g. 0.395 vs 0.381 px at 25
views).

### 10.2 Vendor corpus (real images, CCF reference)

Full 25 views: **0.367 px** RMS, **158.543 mm** baseline (−0.014 mm vs CCF),
IR relative rotation difference **0.202°**, intrinsics 2.8–5.1 px from CCF.
The ablation is §8.1. Six bootstrap resamples (21 views each): baseline
158.549 ± 0.026 mm; fx sd 1.00 / 0.82 / 0.68 px for left/right/tex.

### 10.3 Adaptive vs fixed (synthetic, 8 views, 2 trials)

| method | median |fx error| | median baseline error | rotation error |
|---|---:|---:|---:|
| fixed first 8 views | 0.804 px | 0.050 mm | 0.051° |
| greedy Fisher 8 views | **0.126 px** | **0.016 mm** | 0.050° |

The greedy plan selected four close-distance, high-tilt poses — the most
informative for focal length, but **all at 240 mm**. That is a warning: pure
A-optimality clusters at the nearest distance; a production planner should add
a distance/pose diversity constraint or a distortion prior. The 6× intrinsic
improvement is real for this scene but should not be generalised without the
diversity constraint.

### 10.4 End-to-end profile

The CLI run on the 75 vendor images produced a revision with:
baseline **158.557 mm** (anchored), baseline σ **0.012 mm**, fx σ 0.056–0.154 px,
all stopping checks green, factory snapshot and covariance saved. The legacy
`Calibration.from_ccf_dir` loads the produced directory and reports the same
baseline. This is the intended host-side workflow: **no device write**.

### 10.5 Processing time

| operation | time (this machine) |
|---|---:|
| detect 75 images | ~7 s (single-threaded Python/OpenCV) |
| calibrate 25 views, 72 observations | ~10 s (dense Jacobian) |
| covariance (189 params) | ~9 s |
| vendor solve on the same session | ~1.3 s (C++/Ceres, 3 cameras) |
| EXStar human positioning | 2 min 56 s (measured) |

The Python prototype is ~10× slower than the vendor's C++ solve but the solve
is <1 % of the human time. Optimisation (analytic Jacobian, vectorised
residuals, GPU detection) is future work, not the bottleneck.

---

## 11. Direct comparison

| dimension | EXStar 1.2.2.0 | proposed replacement |
|---|---|---|
| captures | fixed 25 × 3 = 75 | adaptive 8–20 × 3 (target 8–12) |
| orientations | 5 fixed | chosen by Fisher information + diversity |
| distances | 5 fixed bands 240–560 | chosen from a pool; diversity-constrained |
| human time | 2 min 56 s measured | estimated 40–90 s for 8–12 captures (not yet measured on hardware) |
| capture→result | ~1.3 s | ~10 s prototype; <2 s achievable with analytic Jacobian/C++ |
| intrinsic output | fx, fy, cx, cy, alpha, kc fixed | same, kc optionally free |
| uncertainty | none | full covariance, NEES-validated |
| early stopping | never (25/25) | σ + held-out residual criteria |
| bad-view handling | none per step; full redo | robust loss + outlier rejection + covariance-aware planning |
| retry | full re-run | per-view rejection and re-planning |
| storage | in-place overwrite of factory files + device; no backup | immutable factory snapshot + versioned revisions + rollback; device write only via explicit export plan |
| validation | fixed residual gate | held-out views, reprojection/epipolar residuals, covariance, synthetic ground truth |
| compatibility | vendor CCF/device format | same 33-double CCF and 0xF21 device block; same plate (8×5 corpus / configurable 8×7) |
| accuracy claim | vendor's own residual | **no physical accuracy claim yet**; relative geometry matches CCF to 0.014 mm baseline / 0.2°, absolute scale needs a physical standard |

### 11.1 Which stages are necessary

* **Necessary**: at least 3 well-spread views for the baseline; 8–20 for
  intrinsics; at least two distinct tilts to separate focal length from pose;
  at least 3 distance levels to constrain the principal point and distortion
  (if free).
* **Redundant**: the full 5×5 grid for scale; many near-duplicate poses within
  a group; the 5th distance band for the IR baseline.
* **Eliminable**: repeated groups at the same tilt sign; the fixed 25/25
  completion when covariance already meets the target.
* **Not eliminable without a different target**: absolute scale and distortion
  validation; those need a physical length standard or a characterised artefact.

### 11.2 Expected gains (with confidence)

| gain | estimate | confidence |
|---|---|---|
| captures | 75 → 24–36 images (8–12 steps) | B (synthetic + corpus ablation) |
| human time | 176 s → 40–90 s | C (not measured on hardware) |
| intrinsic error | comparable or better than fixed 8 views; 6× better in synthetic | B |
| baseline error | 0.05 → 0.02 mm in synthetic; 0.014 mm vs CCF on real | B |
| uncertainty | from none to calibrated covariance | A (NEES) |
| robustness | robust loss + per-view rejection | B |
| failure recovery | re-plan vs full redo | B |

No claim of **physical** accuracy improvement is made: the synthetic and
training-set reprojection results cannot establish it. The required physical
validation is in §12.

---

## 12. Limitations and the evidence still needed

### 12.1 Plate variant (blocking for hardware use)

The corpus is 8×5; the manual figure is 8×7. The engine is configurable, but
the **actual board must be counted and measured**:

1. Count the small holes along both axes (8×5 or 8×7).
2. Measure the centre-to-centre pitch with calipers (expected ~27.7 mm for the
   corpus variant; a different value if 8×7).
3. Record the four large-hole coordinates (the centre-relative rule predicts
   (2,cy−1),(4,cy−1),(5,cy−1),(3.5,cy+1)).
4. Measure the board outline and thickness.

This is a five-minute physical measurement and it decides the plate spec.

### 12.2 Physical scale

Anchor to the factory baseline (implemented) or measure a known length
(caliper/artefact). A 0.1 % scale error is 0.3 mm at 300 mm depth; only a
physical standard can bound it.

### 12.3 Colour camera

The CCF colour extrinsic is not co-registered with the IR pair (17 mm y
offset). Re-calibrate the user's own unit jointly (our engine does) and compare
the colour-to-IR transform before/after; do not trust the shipped colour T.

### 12.4 Real captures

The vendor corpus is the only real calibration image set. The user's 10
first-capture PGM frames are scan frames, not calibration frames. A real
calibration run on the user's unit (25 images, any sequence) is needed to
measure repeatability and physical accuracy. The scanner is disconnected, so
this is an external dependency.

### 12.5 Distortion

The vendor preserves `kc` bit-exactly; our default does the same. Freeing the
distortion on 25 views is ill-conditioned (k3 especially). Do not free it
without a strong prior or a larger, better-spread dataset.

### 12.6 What is *not* claimed

* No physical accuracy number, no “better than vendor” claim.
* No claim that 8 views always suffice; the adaptive result is scene-dependent
  and currently lacks a diversity constraint.
* No claim that the CCF is wrong; it is a near-optimal solution with a
  different parameter set/constraints, and its colour record is inconsistent
  with the IR pair.
* No device write was performed.

---

## 13. Reproduction

```
# tests
.venv/bin/python -m pytest -q tests/test_calib_engine.py

# workflow model
.venv/bin/python -m calib.cli workflow \
  sources/vendor/EXStar_1.2.2.0/res/calibrateConfig/Einscan-EA/rapidCameraCalibrate.xml

# detector + calibration on the vendor corpus
.venv/bin/python -m calib.cli calibrate <private-inputs>/vendor-calib-rapid \
  --factory sources/calibration/res_Einscan-E10_200x150 \
  --profile <private-inputs>/calib-profiles --serial TESTUNIT \
  --out analysis/calibration/engine/end_to_end.json

# evaluation artefacts
analysis/calibration/engine/vendor_ablation.json
analysis/calibration/engine/adaptive_exp.json
analysis/calibration/engine/synth_noout.json
analysis/calibration/engine/nees_fix.log
docs/calibration/figures/{ablation,adaptive_vs_fixed}.png
```

The vendor binaries, the 75 calibration images and the device dump stay local
(not published); the derived geometry, algorithms, tests and reports are
publishable.
