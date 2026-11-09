# Einstar three-camera calibration model

Status: **operational model decoded and validated for geometric consistency; absolute physical
accuracy not yet measured.** The scanner is not attached. This document states exactly which
numbers are proved, which are bounded, and what must be measured on hardware.

Code: `tools/calib/` (`calib_model.py`, `rectify.py`, `triangulate.py`, `synth.py`,
`validate_model.py`, `key_analysis.py`, `real_capture_check.py`).
Tests: `tests/test_calib_model.py` (20 tests). Invariants + numerical round-trip:
`python3 tools/calib/validate_model.py` → **42/42 checks**.

## 1. Two calibration sources (and which one is operational)

| Source | What it is | Key | Role |
|---|---|---|---|
| `res_Einscan-E10_200x150/{Left,Right,Tex}CCF.txt` | the vendor's per-unit calibration records shipped with EXStar and loaded by the plugin | IR records are **obfuscated** by a per-field integer offset; rotation and distortion offsets are proved, the four intrinsics are bounded | **operational model** — it is what the vendor stack used at capture time |
| device dump, records at `0x00C/0x104/0x1FC` | 22 unkeyed doubles per camera, read from the device with `ReadData(0, 6568)` | none | independent check; proves the distortion key and bounds the intrinsic key |

They are **not the same calibration**. They share the distortion block bit-exactly, but their
extrinsics and intrinsics come from different runs (dump translations are ~550 mm, CCF ~270 mm;
different rotations). The real captures settle which one is operational: rectifying
`cam0`/`cam1` with the **CCF** model aligns the epipolar lines (median |vertical offset| ≈ 1 px
integer, **0.90 px** with sub-pixel refinement), while the **dump** model produces no accepted
matches with the strict matcher and only window-edge matches (|dy| = 2.0 px) with a looser one. `Calibration.load("auto")` therefore prefers CCF and falls back to the dump.

**Dump record field order (proved).** The 22 doubles are
`fx fy cx cy kc[5] d9 R[9] T[3]` — **kc first**, not alpha-first. Proof: `d[4..8]` is
*bit-identical* to the colour camera's `kc`, and for the IR records
`stored_kc − dump_kc = (0, 6, 2, 7, 13)` exactly (residual < 1e-15). The competing
alpha-first order would put `alpha ≈ −9.5e-5` at `d[4]`, but `d[4]` is `k1 ≈ −0.151`.
`d[9]` is **not** the CCF `alpha`: it is bit-identical for the Left and Tex records
(`+9.837374e-05`; Right `+2.056928e-04`), which no per-camera skew fit can be. Whether
it is a skew (≤ 9e-5 px) or a sixth radial term (≤ 0.033 px), its effect is negligible,
so it is carried as `CameraModel.extra` and never applied.

## 2. Conventions

* Pinhole + Brown–Conrady 5-term distortion `(k1, k2, p1, p2, k3)`, OpenCV convention.
* `K = [[fx, skew, cx], [0, fy, cy], [0, 0, 1]]`; distortion applied in normalised
  coordinates. The CCF stores the CC-toolbox `alpha_c` (dimensionless); the model sets
  `skew = alpha_c·fx` (≤ 0.22 px over the whole image; setting it to 0 changes nothing
  measurable).
* Extrinsics `X_cam = R · X_world + T` (world→camera), OpenCV convention; camera centre
  `C = −RᵀT`.
* Units: millimetres; pixels for intrinsics. Images are 1280 × 1024, 8-bit.
* Cameras: `left` (IR), `right` (IR), `tex` (colour). All three are 1280 × 1024.

## 3. CCF record layout (33 × float64, little-endian)

```
[0] fx   [1] fy   [2..3] pad   [4] cx   [5] cy   [6..7] pad   [8] alpha   [9] pad
[10..14] k1 k2 p1 p2 k3        [15..19] pad
[20..22] T                     [23..31] R row-major               [32] error
```

IR records append a 256-byte trailer (64 int32); the colour record does not. The obfuscation is
`stored = true + key`, with an integer key per field.

**Proved keys.** Rotation `(-2,-4,-11,-9,0,5,-11,-12,0)` — exhaustive search over 2⁹, uniquely
selected by orthonormality (det = 1 to 1e-15). Distortion `(0,6,2,7,13)` — the unkeyed dump
record reproduces the same five coefficients exactly. `alpha` key −2 (only physical integer).

## 4. Operational parameters (CCF, `key_mode="best"`)

| | left (IR) | right (IR) | tex (colour) |
|---|---:|---:|---:|
| fx (px) | 1159.115416 | 1158.086917 | 1141.077627 |
| fy (px) | 1159.021303 | 1157.718563 | 1141.087278 |
| cx (px) | 623.912232 | 635.501437 | 633.102385 |
| cy (px) | 534.562945 | 494.509060 | 521.124979 |
| k1 | −0.162025508 | −0.162050120 | −0.151009834 |
| k2 | 0.173297958 | 0.177924663 | 0.166435784 |
| p1 | 0.000186725 | 0.000837870 | 0.000575640 |
| p2 | 0.000146796 | 0.000024227 | −0.000073757 |
| k3 | 0.018457014 | 0.012359554 | 0.031132637 |
| T (mm) | 48.2022, −37.7250, 273.1451 | −7.8186, −37.6040, 265.9959 | 3.0413, −20.9153, 264.2020 |

Relative IR geometry: baseline **158.557 mm**, relative rotation **22.138°**, epipole at
x ≈ **6693 px** — the pair is strongly converged and **must be rectified**. Rig triangle:
left–right 158.557, left–tex 125.318, right–tex 38.628 mm. All rotations are orthonormal to
≤ 2e-15, all determinants +1.

## 5. What is proved, what is bounded

| Parameter | Status | Bound |
|---|---|---|
| rotation key | **proved** (unique) | exact |
| distortion key | **proved** (exact match against the unkeyed record) | exact |
| `alpha` key | proved up to the only physical integer | exact in practice |
| `fx`, `fy`, `cx`, `cy` keys | **bounded, not proved** | feasible integer keys: fx ∈ {8, 9}, fy ∈ {−4, −3}, cx ∈ {−7, −6}, cy ∈ {−2, −1} (left) but {3, 4} (right) — **no single cy key** fits both IR records within τ = 1.03 px, so either the key is per-record for cy or the Right camera moved ≈ 4 px between the two runs; the square-pixel constraint fixes key(fx) − key(fy) = 12 |
| translation `T` | assumed key 0 (a ±1 mm integer shift does not improve the 2.4 mm rig-triangle agreement); not proved | rig spread 1.53 mm in baseline |

Why the intrinsics cannot be pinned further:

* A **common** integer shift of `fx, fy, cx, cy` leaves every intrinsic invariant intact, and
  the colour camera (unkeyed in both encodings) shows that a re-calibration moves these
  parameters by up to **τ = 1.03 px** — the same order as the key step.
* **Epipolar geometry does not constrain the focal key**: rectification collapses the row
  residual to ~1e-12 px for *every* candidate key; only the epipole moves (6693 → 6651 px for
  keys 8 → 16). This is a clean negative result, not a gap in the search.
* A 1 px focal-key error is **0.26 mm in depth at 300 mm**; the baseline spread (1.53 mm) is
  **2.9 mm** — the extrinsic uncertainty dominates, so the intrinsic ambiguity is acceptable
  for a first implementation but must be removed by a hardware calibration check.

## 6. Validation

**Invariants and numerical round-trip** (`validate_model.py`, 42/42 for both sources):

* `K` upper triangular, `K[2,2] = 1`, positive finite focal lengths, principal point inside the
  image, square-ish pixels (|fx/fy − 1| < 1e-2), distortion in physical range.
* `undistort(distort(x)) == x` to 9e-16.
* R orthonormal det = +1; `|t|` equals the baseline; camera centre from the relative pose
  matches the world-frame centre to 4.4e-13 mm.
* Rectified epipolar row residual **1.02e-12 px** (CCF) / 3.4e-13 px (dump); `Q` triangulation agrees with the DLT to
  1.3e-11 mm; synthetic scene round trip (project → distort → rectify → disparity → triangulate)
  at the numerical floor: **3.5e-12 mm**, reprojection **3e-12 px**; map inversion 3.7e-05 px
  (float32 storage).
* JSON round trip preserves `K, R, T` for all three cameras.

**Real captures** (10 PGM files = three complete triples, frames 0–2, plus one extra
`cam0` frame; 1280 × 1024, captured over USB by the working Linux driver):

| Check | Result |
|---|---|
| CCF rectification, `cam0`/`cam1`, frame 0 | valid canvas 79 %; 16 accepted matches; median \|vertical offset\| **1.0 px**, p90 2.0 px (integer search, quantisation-limited) |
| CCF, sub-pixel refinement (7×7 patch, ±2 px window, ZNCC ≥ 0.85) | median \|vertical offset\| **0.895 px**, p90 1.69 px (frame 0, 4 accepted matches); frames 1/2 give 1 match each (0.58 / 2.00 px) |
| disparity | median **290 px**, p10–p90 97–415 px |
| implied depth (f = 1158.5, B = 158.557) | p10/50/90 = **443 / 635 / 1910 mm** |
| control: same corners against `cam2` under the IR rectification | **no accepted matches** |
| dump-model rectification of the same pair | no accepted matches with the strict matcher; with a looser protocol the survivors sit on the ±2 px window edge (median \|dy\| = 2.00 px, vs 0.90 px for CCF) |

This proves the model is **geometrically consistent** with the real device and that the CCF is
the operational calibration. It does **not** prove physical accuracy: there is no ground-truth
artefact, the sub-pixel residual rests on only 4–16 correspondences, and scale errors from
`T`/`f` are not observable this way.

## 7. Operational recommendation

1. Use `Calibration.load("ccf")` for rectification and triangulation (the default `auto` now
   selects it).
2. Treat the intrinsics as ±1 px uncertain and the baseline as ±1.5 mm; report uncertainty with
   any depth value instead of a single number.
3. Use the dump records only as an independent check (distortion equality, key bounding).
4. Before claiming accuracy: run the hardware checks below and, ideally, scan a calibrated
   artefact (sphere spacing or a flat plane at known distance).

## 8. What must be measured on hardware

1. A planar target at several known distances → residual depth bias and scale (tests `f`, `B`).
2. Vertical-disparity residual after rectification with a **sub-pixel** matcher on a textured
   target (target < 0.5 px median). The offline sub-pixel run already gives 0.90 px median on
   frame 0, but with only 4–16 correspondences; a target is needed to turn that into a
   measurement (and to separate residual rotation from distortion error).
3. Which camera is `left`/`right` in the stream: confirmed indirectly by the successful
   rectification of `cam0`/`cam1`; confirm against a known asymmetric target.
4. Whether the vendor applies an additional rectified-frame scale or ROI (the decoded
   calibration has none, but the plugin's `P1/P2` construction was only inferred statically).
5. Repeatability: the same target scanned twice; the spread sets the noise floor for any
   "better than vendor" claim.

## 9. Reproduce

```bash
python3 tools/calib/validate_model.py --source ccf      # 42/42 invariants + round trip
python3 tools/calib/key_analysis.py                     # feasible key sets, uncertainty
python3 tools/calib/real_capture_check.py --contrast    # needs private captures; CCF vs dump
python3 -m pytest -q tests/test_calib_model.py          # 20 tests
```

---

## 9. EXStar's own calibration procedure (2026-11-09)

This section connects the operational model above to the workflow that
regenerates those numbers. The full derivation is in
`docs/calibration/workflow_and_replacement.md`.

* **What it is**: a planar-marker calibration of the three cameras (left/right
  IR + colour). It re-estimates `fx, fy, cx, cy, alpha, R, T` for all three
  cameras and **preserves `kc` bit-exactly**; the projector's factory geometry
  is not part of it.
* **Workflow**: 5 groups (0°, x∓18°, y±18°) × 5 distance bands
  (240/320/400/480/560 mm ±40) × 1 frame × 3 cameras = 75 images. Only the
  vertical group has an enforced angle gate (±8°). There is no per-step retry;
  “redo” is a full re-entry. A real session took **2 min 56 s**, of which the
  solve was ~1.3 s and the device write ~0.34 s.
* **Storage**: the result replaces `res/Einscan-E10/200x150/{Left,Right,Tex}CCF.txt`
  and the same 3873-byte block is written to the device at `tagDevData+0x39B`.
  No backup is kept. `calib/profiles.py` implements the safe host-side
  alternative (immutable factory snapshot + versioned revisions + rollback).
* **Plate**: the vendor corpus shows an 8 × 5 grid with four large markers at
  (2,1), (4,1), (5,1), (3.5,3); the measured pitch is **27.72 mm**. The manual
  figure draws 8 × 7, so the physical variant must be counted before hardware
  use (`calib.plate.PlateSpec` supports both).
* **Reproduction**: a free re-optimisation of the 75 vendor images reaches
  0.367 px RMS, reproduces the IR baseline to 0.014 mm (158.543 vs 158.557)
  and the IR relative rotation to 0.20°. The shipped CCF's colour record is
  **not co-registered** with the IR pair (17 mm y offset), so the colour
  extrinsic should be re-estimated on the user's unit.
* **New engine**: `calib/` implements detection, joint robust bundle
  adjustment, covariance, Fisher-information view selection and profiles;
  `python -m calib.cli calibrate ...` produces a versioned host-side revision
  without touching the device. The covariance is NEES-calibrated (1.06 × n).

### 9.1 What is still needed before claiming physical accuracy

1. Count the holes and measure the pitch of the **actual** board (8×5 vs 8×7).
2. Anchor the scale with a physical length (factory baseline is a proxy).
3. Calibrate the user's own unit and compare the colour-to-IR transform.
4. Measure a known artefact (plane/sphere/step gauge) to turn reprojection
   residuals into a physical error number.
