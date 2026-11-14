# EXStar 2026-11-14 vendor corpus: refit, scale stability and model selection

Agent C (calibration mathematics), 2026-11-14.  Analysis only: **no profile was
activated, no session file and no factory file was modified, nothing was written
to the Windows mount.**

Inputs (read-only NTFS mount, `<EXStar-install>/Shining3d/EXStar/res/Einscan-E10/`):

| input | files | note |
|---|---|---|
| `calibrate_image_read_rapid/` | 75 BMP, 1280x1024 u8 | 25 triples `imageLeft/Right/Tex1..25`, written 00:09-00:14 |
| `200x150/{Left,Right,Tex}CCF.txt` | 3 | the **NEW** calibration, written 00:13 |
| `backup/bak/*CCF.txt` | 3 | the pre-calibration copy; **byte-identical** to `sources/calibration/res_Einscan-E10_200x150` (SHA-256 checked in `inflash.py`) |

The 75 images were copied to `~/einstar-private/exstar-20261114/` (private) and
every copy was verified byte-identical to the mount (SHA-256 recorded in
`analysis/exstar_reference/vendor_20261114_detections.json`).

All numbers below are produced by the scripts in `analysis/exstar_reference/`
and stored in `summary.json` (index) plus one file per stage.  Every script can
be re-run with the project virtualenv, e.g.

```
.venv/bin/python analysis/exstar_reference/detect_vendor.py
.venv/bin/python analysis/exstar_reference/rescue_views.py
.venv/bin/python analysis/exstar_reference/inflash.py
.venv/bin/python analysis/exstar_reference/fit_vendor.py
.venv/bin/python analysis/exstar_reference/eval_vendor_model.py
.venv/bin/python analysis/exstar_reference/part_b.py
for c in held_kc_ppfixed pp_free kc_free both_free vendor_anchored_inflash vendor_anchored_prior; do
  .venv/bin/python analysis/exstar_reference/model_selection_297.py --config $c; done
.venv/bin/python analysis/exstar_reference/make_summary.py
```

---

## 0. Three vendor datasets, not two

The device info dump (`~/einstar-private/calibration.bin`, 6568 B, read twice on
2026-11-07, SHA-256 `557a79926fbd8459…`) contains **two** calibration datasets
(decoded independently in `inflash.py`, records located by structure, not by
offset):

* a **CCF block** at 1191/2479/3767, byte-identical to set A (= the
  pre-recalibration host copy).  The dump carries the ASCII timestamp
  `2026-02-09 21:34` and the NEW 2026-11 CCF is **not** in it (the dump predates
  the recalibration by a week);
* three **unkeyed 22-double camera records** at offsets 12 / 260 / 508
  (`fx fy cx cy dist6 R9 T3`), followed by the ASCII date `<redacted-production-date>`.

IR/IR-tex geometry of the three datasets (mm, degrees):

| dataset | LR baseline | L-tex | R-tex | LR rot | L-tex rot |
|---|---|---|---|---|---|
| device dump 22-double (<redacted-production-date>) | **160.0916** | 122.8774 | 37.2149 | 22.0859 | 16.9983 |
| host CCF set A (2026-02-09) | 158.5570 | 125.3176 | 38.6281 | 22.1383 | 17.0049 |
| host CCF NEW (2026-11-14) | 158.6128 | 125.3362 | 38.6443 | 22.1290 | 17.0004 |

The three records' distortion coefficients are **bit-identical** to the CCFs'
(all three cameras; max |Δ| = 6.3e-16, i.e. double-precision noise).  EXStar has
not estimated distortion in any of these calibrations -- 2024, 2026-02 and
2026-11 all carry the same five coefficients.

Decode note: the IR records are obfuscated per field.  `key_mode="proven"`
leaves `fx/fy/cx/cy` keyed at zero, which yields `fx/fy = 1.0105` -- a 1 %
astigmatism no real lens has -- and contradicts the unkeyed 22-double record
(`fx/fy = 1.0001`).  Everything below uses `key_mode="best"`
(key `fx=+8, fy=-4, cx=-6, cy=-1`), which is also what the repo's factory
profile uses.  The two decodes shift fx by exactly 8 px and cx by 6 px; the
baseline and rotation are unaffected (their keys are 0).

---

## 1. (A) Our estimator applied to the vendor's own fresh images

### 1.1 Detection inventory (`detect_vendor.py`, `rescue_views.py`)

Repo detector `calib.detect.detect_grid_auto`, no tuning.

| camera | images | lattice points / view (mean) | 40/40 | below 40 |
|---|---|---|---|---|
| left | 25 | 39.64 | 22 | view 5: 35, view 10: 38, view 14: 38 |
| right | 25 | 39.80 | 24 | view 5: 35 |
| tex | 25 | 39.76 | 24 | view 5: 35 |

View 5 is genuinely cropped (the plate leaves the frame in all three cameras);
35/40 is the correct answer there.  Three view/camera combinations failed and
were repaired by a *documented* guided rescue (PnP from the healthy camera of the
same view, mapped through the factory rig, matched to blobs; the rig is used for
correspondence only, the measurements are blob centroids in the failing image):

| failure | symptom | rescue |
|---|---|---|
| `imageLeft19` | 15/40 assigned, homography rms 11.1 px | 40/40 matched from right, prediction median 4.7 px, homography rms 1.31 px |
| `imageLeft24` | 0/40 assigned (pattern bootstrap found no candidate) | 40/40, prediction 5.5 px, homography rms 1.40 px |
| `imageRight15` | **degenerate homography**: 40 "assignments" collapsing onto 8 blobs, reported rms 0.257 px | 40/40, prediction 3.2 px, homography rms 0.33 px |

The `imageRight15` case is a real detector failure mode worth fixing in
`calib.detect`: `assign_grid` accepts a rank-deficient homography (all five rows
of a column land on the same pixel) and its rms test *rewards* it.  A
`len(unique pixels) == len(points)` check catches it.  (The 297-view session is
clean: 0 of 891 groups have duplicate pixels.)

### 1.2 Fits of our model to the 25 vendor triples (pitch 28.0 mm)

`fit_vendor.py`; factory distortion **fixed** in every fit except `ours_freekc`;
initialised from the factory CCF; the optimum is initialisation-independent
(`ours_free_initproven`, started from the raw stored decode, converges to the
same solution to 1e-4 px).

| fit | model | RMS px | fx L / R / T | cx L / R / T | LR baseline mm | IR rot deg |
|---|---|---|---|---|---|---|
| `ours_free` | kc fixed, fx,fy,cx,cy free | **0.3138** | 1156.409 / 1155.393 / 1139.357 | 624.93 / 638.60 / 637.09 | **160.2097** | 22.0843 |
| `ours_prod` | kc fixed, cx,cy fixed at factory CCF | 0.3184 | 1156.778 / 1154.920 / 1139.100 | 623.91 / 635.50 / 633.10 | 160.1770 | 22.1861 |
| `ours_prod_ip` | as `ours_prod`, PP fixed at the device record | 0.3256 | 1156.936 / 1155.382 / 1139.391 | 624.19 / 635.78 / 633.63 | 160.1976 | 22.1826 |
| `ours_freekc` | kc free (5/cam), fx,fy,cx,cy free | 0.2960 | 1156.445 / 1155.702 / 1139.562 | free (see JSON) | 160.2124 | 22.0413 |

Effective pitch that would reproduce each vendor dataset from these fits:

| fit | to reproduce CCF A (158.5570) | CCF NEW (158.6128) | device record (160.0916) |
|---|---|---|---|
| `ours_free` | **27.711 mm** | **27.721 mm** | **27.979 mm** |
| `ours_prod` | 27.717 | 27.727 | 27.985 |
| `ours_freekc` | 27.711 | 27.720 | 27.979 |

**The +1 % reproduces on the vendor's own fresh images.**  Our fit on the 25
vendor images lands 1.04 % above the CCF set A and 1.01 % above the NEW CCF --
the same sign and magnitude as on our 297-view session (+0.92 % vs set A), and
it lands **+0.074 %** above the 2024 device record.  Freeing distortion changes
the baseline by 0.002 mm (0.001 %), so this is not a distortion-model effect.

Note the older 2026-02 corpus result is reproduced exactly: it required 27.72 mm
to match set A; the NEW corpus requires 27.711-27.721 mm.

### 1.3 How well does the manufacturer's calibration explain its own images?

`eval_vendor_model.py`.  Every model is **fixed** (intrinsics, distortion and rig
extrinsics); only one 6-DoF board pose per view is solved, on the IR pair, so the
board-pitch/baseline ratio cannot be absorbed.

| model | IR rms px | left | right | tex | Sampson LR (undist) px | baseline mm |
|---|---|---|---|---|---|---|
| CCF set A (2026-02-09) | 1.395 | 1.499 | 1.283 | **48.03** | 0.229 | 158.557 |
| CCF NEW (2026-11-14) | **1.420** | 1.532 | 1.299 | 48.39 | 0.204 | 158.613 |
| set A, "proven" decode | 1.531 | 1.324 | 1.712 | 48.76 | 0.140 | 158.557 |
| NEW, "proven" decode | 1.527 | 1.336 | 1.696 | 49.00 | 0.120 | 158.613 |
| device 22-double record | 1.843 | 1.836 | 1.849 | **1.150** | 2.520 | 160.092 |
| `ours_free` (this corpus) | **0.314** | 0.300 | 0.328 | 0.322 | 0.033 | 160.210 |
| `ours_prod` | 0.318 | 0.301 | 0.333 | 0.329 | 0.036 | 160.177 |
| 297-view candidate profile | 0.384 | 0.354 | 0.412 | 0.407 | 0.048 | 160.014 |

Three things stand out.

1. **The vendor's new calibration explains its own images no better than the nine
   month-old one** (1.420 vs 1.395 px), and both are 4.5x worse than our fit on
   the same images (0.314 px).
2. **The CCFs' colour extrinsic is broken**: 48 px.  The 2024 device record's
   colour extrinsic is fine (1.15 px).  This is the same defect the repo had
   already measured for the factory CCF (`rgb_factory 31.3 px` vs
   `rgb_fitted 0.63 px` on our own images).
3. **A pure rig-scale change removes almost all of the CCF residual.**  Scaling
   the CCF's camera translations (i.e. its baseline/pitch ratio) by `s` and
   refitting only the per-view poses (`eval_vendor_model.py`, `rig_scale_scan`):

   | model | best s | rms at s | rms at s=1 |
   |---|---|---|---|
   | CCF set A | **1.0100** | 0.445 px | 1.395 px |
   | CCF NEW | **1.0100** | 0.412 px | 1.420 px |
   | device 22-double | 0.9975 | 1.799 px | 1.843 px |

   The images want the CCF baselines multiplied by 1.0100 -- i.e. 160.14 and
   160.20 mm -- and the device record left essentially unchanged.  The residual
   direction agrees: the CCF residual is dominated by its horizontal
   (baseline/parallax) component, which the scale change collapses
   (`part_b.py`, `residual_shape`):

   | model | rms px | rms dx | rms dy |
   |---|---|---|---|
   | CCF set A, s=1 | 1.395 | 1.089 | 0.871 |
   | CCF set A, s=1.01 | 0.445 | 0.270 | 0.354 |
   | device record, s=1 | 1.843 | 0.392 | **1.801** |
   | `ours_free` | 0.314 | 0.233 | 0.211 |

   The device record's residual is almost purely *vertical* (its right `cy`
   differs from the CCF's by 4.2 px), i.e. that record has the right scale but
   stale intrinsics.  Neither vendor dataset is right in every respect; our fit
   is better than both.

---

## 2. (B) What the vendor's 0.035 % scale stability establishes

`part_b.py`.

Vendor movement between its own two calibrations (CCF NEW minus CCF set A):

| quantity | movement |
|---|---|
| fx L / R / T | -0.675 / -1.106 / -0.148 px |
| fy L / R / T | -0.041 / -0.246 / +0.291 px |
| cx L / R / T | -0.822 / -0.227 / -1.518 px |
| cy L / R / T | -0.143 / -0.385 / +0.485 px |
| LR baseline | **+0.0558 mm (+0.0352 %)** |
| IR relative rotation | -0.0093 deg |
| distortion | **0.0** (bit-identical) |

What the *data* ask for, from our free fit on the same 25 views:

| quantity | data-driven change | vendor's own change | ratio |
|---|---|---|---|
| LR baseline | **+1.653 mm (+1.042 %)** | +0.0558 mm | 3.4 % |
| fx L / R / T | -2.71 / -2.69 / -1.72 px | -0.68 / -1.11 / -0.15 px | 25 / 41 / 9 % |
| cx L / R / T | +1.02 / +3.10 / +3.99 px | -0.82 / -0.23 / -1.52 px | opposite sign |

Leave-one-distance-group-out jackknife of the same 25-view fit (5 refits of 20
views) gives the estimator's repeatability: fx sd 0.35 / 0.24 / 0.36 px, cx sd
0.13 / 0.37 / 0.46 px, **baseline sd 0.028 mm (0.018 %), range 160.167-160.232 mm**.

So:

* the vendor's recalibration is *internally* very repeatable (0.035 % between two
  runs is ~2 jackknife sigma of a 25-view fit);
* but it moves the baseline by only **3.4 % of the correction the images
  support**, and in the same direction.  That is the signature of a strong
  prior/anchoring toward the previous solution (or of a plate-scale bias that is
  reproduced identically on every run);
* the vendor's movement is far too small to be "the data changed their mind":
  the 25-view images are not the limiting factor, the update rule is.

Two hypotheses survive, and this dataset cannot separate them:

1. **anchored MAP** -- EXStar regularises toward the stored solution (distortion
   is demonstrably held, so at least part of the solver is a MAP estimator);
2. **plate-scale bias** -- EXStar's internal plate model is ~1 % smaller than the
   nominal 28.0 mm pitch, in which case *every* run it performs is ~1 % small,
   reproducibly.  This is exactly the repo's old `ccf_effective = 27.72 mm`
   finding.

Distinguishing them needs the vendor's internal plate constant (RE work) or an
independent length artefact.  Neither is available here.  Note that the 2024
device record does **not** show the 1 % bias, so a plate-scale bias would have to
have been introduced between 2024-03 and 2026-02 (or the factory used a different
tool/plate).

What the stability does **not** establish: that 158.56 mm is the true baseline.
Two calibrations agreeing to 0.035 % only show that the estimator is repeatable;
a deterministic scale bias is perfectly repeatable.

---

## 3. (C) Model selection on the existing 297-view session

`model_selection_297.py`.  Observations are the *existing* detections of
`<calibration-session>` from `analysis/session03/session.json`
(891 groups, 34202 points), culled with the session's own list (views 6, 36, 129,
161 -> 293 views), pitch 28.0 mm.  Nothing was re-recorded and nothing was
written to the session.

The first row reproduces the delivered candidate, which validates the harness:
fx 1159.385 vs 1159.346, baseline 160.0182 vs 160.0137, rms 0.6461 vs 0.6514,
held-out 0.757 vs 0.770 px.

| model | train rms | held-out rms | held-out L/R/T | fx L/R/T | cx L/R/T | LR baseline |
|---|---|---|---|---|---|---|
| kc held, PP hard-fixed (candidate) | 0.6461 | 0.7567 | 0.418/0.476/1.146 | 1159.385/1158.600/1141.305 | 623.91/635.50/633.10 | 160.0182 |
| PP free | 0.6448 | **0.7526** | 0.418/0.466/1.142 | 1157.871/1156.451/1139.008 | 621.10/635.04/633.07 | 160.0702 |
| kc free (5/cam) | 0.6449 | **0.7522** | 0.418/0.465/1.142 | 1158.630/1157.180/1140.093 | fixed | 160.0520 |
| PP + kc free | 0.6443 | **0.7512** | 0.418/0.464/1.141 | 1157.635/1156.098/1138.956 | 622.17/636.67/634.16 | 160.0678 |
| PP hard-fixed at device record | 0.6473 | 0.7586 | 0.418/0.486/1.146 | 1159.703/1158.916/1141.534 | 624.19/635.78/633.63 | 160.0095 |
| PP free, 2 px prior at device record | 0.6448 | 0.7527 | 0.417/0.466/1.142 | 1157.976/1156.630/1139.182 | 621.38/635.14/633.17 | 160.0639 |

Epipolar consistency of the same fits (Sampson, distorted coordinates / properly
undistorted / left-tex undistorted, all medians in px): 0.122/0.0485/0.252
(candidate), 0.118/0.0474/0.255 (PP free), 0.124/0.0464/0.253 (kc free),
0.119/0.0473/0.253 (both free).  All models are epipolar-consistent to 0.05 px;
the epipolar metric does not discriminate.

Observability (cached evidence, `analysis/calibration/solver/gate_fixed.json`,
18-parameter model): sigma(fx) = 0.202/0.181/0.196 px, `condition_scaled` 1697.8,
rank 18/18, 15 constrained / 3 weak / 0 unobservable.  fx is the weakly
observable direction only in the sense that it correlates 0.96 with a board pose
-- the same coupling that makes the 1 % scale question hard.

**Verdict.**

* Freeing the principal point and/or distortion is justified by *held-out*
  evidence, not just by lower training error: 0.7567 -> 0.7512 px (-0.7 %), with
  held-out right/tex improving (0.476/1.146 -> 0.464/1.141).  The gain is small
  but consistent across folds.
* It does **not** move the solution toward the vendor CCF.  Freeing the PP moves
  fx **down** by 1.5-2.3 px (1159.4 -> 1157.6-1157.9), i.e. *past* the CCF's
  1158.44 to the other side, and moves the LR baseline **up** (160.018 ->
  160.068-160.070 mm), i.e. *further* from 158.56 and 0.02 mm from the device
  record's 160.09.
* Across all six models the LR baseline stays in **160.010-160.070 mm** (0.04 %
  spread).  No parameterisation approaches 158.56 mm; reaching it would need a
  1 % change that no model produces.  **The scale gap is not a parameterisation
  artefact.**
* The fx spread across models (1157.6-1159.7 px, 0.18 %) comfortably contains the
  vendor CCF (1158.44) and the device record (1158.76): the focal lengths are
  consistent with both vendor datasets.  Only the baseline/board-size ratio
  differs.

The smallest additional evidence that would resolve the remaining scale question:

1. **an independent length artefact** scanned by EXStar (a calibrated sphere or
   gauge block, or a scan of a known-size object) -- this is the only thing that
   breaks the pitch/baseline degeneracy;
2. failing that, **the vendor's plate constant** as actually used by its solver
   (the `.ple`/`CalibrationTableParam` path in the RE work) or a scan of the
   physical plate with a caliper along two orthogonal directions (a plate
   anisotropy of 1 % would also explain part of the fy-fx residual we see);
3. a **firmware-side readback** of which dataset the device uses for scanning
   (the device record or the host CCF) -- that settles which vendor number is
   "the vendor's answer", not which is physically right.

---

## 4. Facts, inferences, unknowns

### Facts (measured here, reproducible by the commands above)

1. The 2026-11-14 corpus contains 25 usable triples; the repo detector assigns
   40/40 lattice points on 22/25 left, 24/25 right and 24/25 tex views; view 5 is
   genuinely cropped (35/40 in all three cameras); three failures were repaired
   (Left19, Left24, Right15), including one *degenerate homography* that the
   detector's own rms test accepts.
2. Our model fits the vendor's fresh images at **0.314 px** RMS (free PP) /
   **0.318 px** (production model), and the vendor's CCF as a fixed model leaves
   **1.395 px** (set A) / **1.420 px** (NEW).
3. Our fit on the vendor images gives LR baseline **160.20-160.21 mm** at pitch
   28.0, i.e. the effective pitch to reproduce the CCFs is **27.71-27.72 mm**;
   to reproduce the 2024 device record it is **27.98 mm**.
4. Scaling a CCF's baseline by **1.0100** and refitting only poses reduces its
   residual from 1.395 to **0.445 px** (set A) and from 1.420 to **0.412 px**
   (NEW); the device record is left essentially unchanged (best s = 0.9975,
   1.843 -> 1.799 px).
5. The vendor's two CCFs differ by 0.035 % in baseline and 0.0 in distortion;
   the 2026-02 CCF differs from the 2024 device record by 0.959 % in baseline,
   with distortion again bit-identical.
6. The 2024 device record's colour extrinsic is consistent with the IR cameras
   (1.15 px) while both CCFs' colour extrinsics are ~48 px off.
7. On the 293-view session, freeing PP and/or distortion improves held-out
   accuracy from 0.7567 to 0.7512 px and moves fx by -1.5 to -2.3 px and the
   baseline by +0.05 mm; no model leaves the 160.010-160.070 mm band.

### Inferences (labelled, not proof)

1. **The 1 % scale difference is a property of the vendor's host CCF, not of our
   estimator.**  Two independent corpora (the vendor's own 2026-11 images and our
   297-view session) give the same ratio to 0.1 %, and a scale-free
   baseline/pitch ratio measured with a fixed model agrees with the free fit.
2. **EXStar's host recalibration is strongly anchored/regularised**: distortion
   is provably held, and the update is 3-9 % of the data-driven correction, in
   the same direction.  Anchoring and a plate-scale bias are both consistent with
   the data; the 2024 record's different scale slightly favours anchoring or a
   changed plate constant.
3. **Our candidate's scale coincides with the device's 2024 factory record**
   (baseline -0.049 %, left-tex -0.0004 %, relative translation components
   <= 0.08 mm).  That is strong evidence that our estimator is not the outlier,
   but it is *not* an absolute length reference: the 2024 record is itself
   unverified.
4. The 0.92 % "discrepancy" should be reported as **a difference between two
   vendor datasets**, and our fit sits on the device-record side, not as "our
   fit is 1 % too big".

### Unknowns (cannot be resolved from this data)

1. Which vendor dataset is physically correct (no independent length artefact).
2. Whether EXStar's 1 % comes from a prior or from its plate constant.
3. Whether the physical plate's pitch is 28.0 mm (the `.ple` distances pin the
   lattice to 13.983-14.002 mm, i.e. 27.97-28.00 mm marker pitch, but those
   numbers come from the same vendor).
4. Which dataset the device firmware actually uses when scanning.
5. The residual fy-fx offset: our free fits give fy-fx = +1.7 px on the vendor
   corpus and -1.3 px on the 297-view session.  At the 0.1 % level our fx/fy are
   not individually trustworthy, so no conclusion should be drawn from a
   sub-pixel focal-length comparison.

---

## 5. What this means for the 0.92 % and for profile activation

* The 0.92 % is **not** an error of the Linux estimator: the same estimator, on
  the manufacturer's own images, reproduces the same ratio (1.01-1.04 % vs the
  CCF) and lands within 0.07 % of the device's factory record.  Any statement
  that "our fit is 0.9 % too big" must be replaced by "the host CCF's IR baseline
  is 0.97 % smaller than the device record's, and the images support the device
  record".
* **Activation:** the candidate is not made unsafe by this analysis -- its
  intrinsics agree with both vendor datasets to ~1 px and its epipolar
  consistency is an order of magnitude better (0.048 px vs 0.2 px).  But the
  absolute scale is a *policy* choice, not a measurement:
  * activating as-is reproduces the device's factory scale (and the physical
    28.0 mm pitch, subject to the `.ple` evidence);
  * scaling the rig to the host CCF's 158.56 mm would make scans agree with
    EXStar-on-Windows to 0.03 %, at the cost of a 1 % dimensional error if the
    device record is right.
  Either way the choice should be explicit in the profile metadata
  (`plate_pitch_mm` / a documented `scale_policy` field), not an accident of the
  fit.
* Before any activation decision, the three cheap checks worth doing are: an
  EXStar scan of a known-length artefact (kills the degeneracy), a caliper
  measurement of the plate (pitch and isotropy), and a firmware-side check of
  which calibration dataset is used at scan time.
