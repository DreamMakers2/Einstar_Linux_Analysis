# White balance — what EXStar's stage does, and the layer we now have

Status: 2026-11-14.  Written against the EXStar calibration run of
**2026-11-14 00:09–00:14** (geometry written 00:13:11, white balance 00:14:54)
on the read-only Windows mount, plus the Windows-side logs of that run and a
decompilation of the shipped algorithm.

This document replaces the sentence "device has auto exposure, and white balance
is missing/unverified" in `docs/reconstruction/status.md` with a measured
answer, an implementation, and an explicit list of what is still unknown.

Labels used throughout: **PROVEN** (observed in shipped bytes, a log, or
code that was decompiled), **PLAUSIBLE** (fits the data, not proven),
**UNKNOWN** (no evidence either way).

---

## 0. Claims at a glance

| claim | label | where |
|---|---|---|
| `wbdata.txt` / `wbdata_noled.txt` are `diag(g0, 1, g2)` in **BGR order**, written by a single call to `SNColorAlgorithm::snWhiteBalanceCali` | **PROVEN** | §3, decompiled code + logged `MatrixWB[*]` |
| each file is the estimate from **one** colour frame; the two files are the illuminator-on and illuminator-off conditions | **PROVEN** | §2, logged `setLedBrightness`/`setCameraGain` pairs |
| the estimator is grey-world over a **centred window** of the frame, dark-subtracted, clipped pixels rejected, each channel normalised to green | **PROVEN** | §3 |
| the central window, dark subtraction and the slot order are each **necessary**: dropping any one moves the gains by 11–16 % | **PROVEN** | §4 table |
| this reproduces both stored numbers to **≤ 1.6 %** (best 0.4 %/0.8 %) | **PROVEN (measurement)** | §4, §8 |
| the exact window divisor (3…16) | **UNKNOWN** | §10 |
| which mosaic phase is physically red and which is blue | **UNKNOWN** | §10 |
| the stored CCM is consistent with the stored gains | **NO** — a "WB then CCM" chain leaves the plate 145 % from neutral | §4 (b) |
| EXStar's own numeric acceptance threshold ("standard rate") | **UNKNOWN** | §10 |
| absolute colour accuracy of the result | **NOT CLAIMED** — a white ceramic plate of unknown spectral reflectance, no chart, no measured illuminant spectrum | §8, §10 |

---

## 1. What the six white-balance frames are

`res/Einscan-E10/calibrate_image_wb/` holds six uncompressed 1280×1024 8-bit
BMPs (1 311 798 bytes each, 54-byte header):

| file | kind | mean DN | p99 DN | ≥250 DN | role |
|---|---|---:|---:|---:|---|
| `imageTex1.bmp` | **Bayer mosaic** (phase spread 29.1 %) | 24.14 | 76 | 0.33 % | colour camera, **illuminator on**, gain 200 |
| `imageTex2.bmp` | **Bayer mosaic** (phase spread 35.2 %) | 29.09 | 119 | 0.56 % | colour camera, **illuminator off**, gain 400 |
| `imageLeft1.bmp` | **mono** (phase spread 0.20 %) | 144.77 | 255 | 21.2 % | IR camera, illuminator on, **blown** |
| `imageLeft2.bmp` | mono (1.5 %) | 11.45 | 16 | 0.00 % | IR camera, dark |
| `imageRight1.bmp` | mono (0.22 %) | 155.58 | 255 | 26.9 % | IR camera, illuminator on, **blown** |
| `imageRight2.bmp` | mono (0.91 %) | 11.53 | 17 | 0.00 % | IR camera, dark |

Facts that follow, all **PROVEN** from the pixels: the two IR frames of a pair
are *mono* (the four Bayer phases agree to 0.2 %), so they carry **no colour
information at all**; and the "on" IR frames are saturated (the plate centre
measures 253 DN, i.e. clipped).  The IR pair is a by-product of the
three-camera synchronised capture — EXStar never changes the IR cameras'
exposure or gain in this stage (§2), so they cannot be the source of a
red/blue gain.  The only frame that can produce `wbdata.txt` is
`imageTex1/2.bmp`.

The colour frames are **dim**: a whole-frame mean of 24–29 DN.  The plate itself
measures 45/65/47 DN (p00/g/p11) in the "on" frame, and the centred estimation
window 26/46/29 DN — note that the window is *darker* than the plate average,
which is part of why the choice of ROI moves the answer (§4).  **PLAUSIBLE**:
EXStar does not expose this stage for brightness, because grey-world gains are
invariant to a common scale (and after dark subtraction, nearly invariant to
exposure — §4 d).

The six frames were written at 00:14:54, the same second as
`[calcWhiteBalance]error = 0` in `log/scanservice_*.log`.

---

## 2. The capture protocol EXStar actually ran

**PROVEN** from `log/scanservice_20261114_000948_049.log` (Windows-side, plain
text) and the mirrored QML trace in `log/EXStar_20261114_000944_607.log`.
Two captures, and **only camera 2 (colour) is reconfigured**:

```
condition 1   setCameraExp  cameraID=2 value=5000      (5000 us)
              setCameraGain cameraID=2 value=200
              setLaserBrightness 0
              setLedBrightness ledID=0 value=0
              setLedBrightness ledID=1 value=400
              → capture
condition 2   setCameraExp  cameraID=2 value=5000      (same exposure)
              setCameraGain cameraID=2 value=400       (twice the gain)
              setLaserBrightness 0
              setLedBrightness ledID=0 value=0
              setLedBrightness ledID=1 value=0
              → capture
then          {"WhiteBalanceAlg":"calcWhiteBalance", "saveDir":"./res/Einscan-E10"}
```

The property read-backs pair the two namings: `setLedBrightness ledID=1 400`
is answered by `StrobeLuminance {"LUMINANCE":400,"ROUTEINDEX":1}`, so
**illuminator `ledID` n is strobe route n**.  Route 0 was driven at 1000 during
the earlier plate-pose step and at 0 here; route 1 is the one this stage uses.

Algorithm output, logged by the wrapper
(`EAWhiteBalanceAlgManagerPrivate.cpp:63,154,157`):

```
whiteBalance true.
[EA 2023]*****wb path = res/Einscan-E10/200x150/wbdata.txt
[EA 2023]MatrixWB[0]=1.60347 … [8]=1.79405
[EA 2023]*****wb path = res/Einscan-E10/200x150/wbdata_noled.txt
[EA 2023]MatrixWB[0]=1.60524 … [8]=1.79638
[calcWhiteBalance]error = 0
```

and there is an explicit failure path in the binary:
`whiteBalance false.write default WB.` — when the algorithm refuses, EXStar
writes a **default** white balance.  Our layer keeps a refusal as a refusal
instead (§7).

**Consequence for us.** The condition that matters for texture capture is the
illuminator-on one, and it needs three things our capture path already has:
`--strobe 1:400` (route 1), a frozen exposure/gain, and the colour stream.
The point cloud produced from *illuminator-off* frames needs the `_noled` set.

### What else that run changed (and why it matters)

| file | changed by the run | what it is |
|---|---|---|
| `wbdata.txt`, `wbdata_noled.txt` | **yes** | the white balance, this document |
| `LeftCCF.txt`, `RightCCF.txt`, `TexCCF.txt` | **yes** | **geometry**: fx/fy/cx/cy/k1 + a 3×4 extrinsic block (Left: fx 1166.44, fy 1154.98, cx 617.09, cy 533.42, k1 −1.9998). The run also re-fitted the rig (`caliInfoChanged calibrateTime 2026-11-14T00:13:11.940`) |
| `ParaCCM_0.txt`, `ParaCCM_noled_0.txt` | **no** | 15 float32 = 3×3 CCM + 3 offsets + 3 reference gains; SHA-256 identical to the pre-run backup and to the 2024 factory file despite the session mtime |
| `ParaDark_0.txt`, `_noled_0.txt` | **no** | 3 float32 dark current (0.045021, 0.045548, 0.045049) |
| `activeBox.dat` | yes | 6 float64 depth working volume — unrelated to colour |

So the run re-derived **only** the diagonal gains.  The colour-correction
matrix that is supposed to consume them was left at its 2024 value, and §4(b)
shows the two are no longer mutually consistent.  Any pipeline that applies
both should treat them as a **pair** and validate them together.

---

## 3. The algorithm, recovered

### 3.1 Where it lives

**PROVEN.** `Sn3DCalibrateAlgorithmWrapper.dll` (the Qt side) calls
`SNColorAlgorithm::snWhiteBalanceCali`, exported from
`Sn3DColorCorrect.dll` at RVA `0x747b0`.  That export is a thin wrapper; the
work is in `FUN_18007ace0` (1 365 bytes), decompiled with Ghidra 12.1.4.  The
function stores the literal `0x3f800000` into matrix entry 4, which is exactly
the `1.0` in the stored files — the decompilation and the files agree on
identity, not just on shape.

### 3.2 What it does

```c
rows = H / divisor;  cols = W / divisor;
r0 = H/2 - rows/2;   c0 = W/2 - cols/2;          /* centred window */
for (row = r0; row < r0+rows; ++row)
  for (col = c0; col < c0+cols; ++col) {
    v0 = px[0]/255; v1 = px[1]/255; v2 = px[2]/255;
    if (v0 > clip || v1 > clip || v2 > clip) { ++n_bad; continue; }
    sums[0] += v0 - dark[0];
    sums[1] += v1 - dark[1];
    sums[2] += v2 - dark[2];
  }
if (n_bad / (rows*cols) > max_bad_rate) {            /* refusal */
    log("ERROR! Out of Exposure, Illegal Nums Rate is:%.4f", rate);
    return false;                                    /* → default WB */
}
matrix[0] = sums[1] / sums[0];
matrix[4] = 1.0;
matrix[8] = sums[1] / sums[2];
```

Consequences that are easy to get wrong:

* the ROI is a **centred window** = (1/divisor) of each dimension, not the
  plate, not the whole frame;
* the rejection is **over-exposure only** (brightest channel above a clip
  level) — dark pixels are kept, so a dark background inside the window is
  counted;
* the dark current is **subtracted before summing**, and the gains are
  therefore defined on dark-subtracted values;
* only channels 0 and 2 are free — green is pinned to 1.0 — so the matrix is
  strictly diagonal and cannot express a tint;
* there is an explicit **refusal** (illegal-pixel rate), and the caller
  substitutes a default in that case.  This is the precedent for our own
  refusal path.

### 3.3 The parameters we cannot read off the files

`divisor`, `clip` and `max_bad_rate` are caller arguments.  Fitting them against
the two stored numbers (§4) constrains `divisor` to a plateau but cannot pin it;
`clip` is inactive on this data (**PROVEN**: 0.00 % of the window exceeds 0.90
full scale, so the clip level does not change the result between 0.80 and 1.00).

---

## 4. Hypothesis table, with residuals against EXStar's stored numbers

Target (illuminator on): `wbdata.txt` = `diag(1.603466, 1.000000, 1.794047)`.
Everything below is computed on `imageTex1.bmp` (and `imageTex2.bmp` where the
pair is compared), and expressed in **EXStar's slot order** so it can be
compared with the file directly.

| # | hypothesis | fitted gains | error vs stored | verdict |
|---|---|---|---|---|
| a | plate-ROI grey-world, no dark subtraction | 1.4571 / 1.3839 | −9.1 % / −22.9 % | **rejected** |
| a+e | plate-ROI grey-world, dark subtracted | 1.6117 / 1.5048 | +0.5 % / −16.1 % | **rejected** — one channel matches by accident, the other is 16 % out |
| a+e | **centred-window grey-world, dark subtracted** | **1.5854 / 1.7667** | **−1.13 % / −1.52 %** | **accepted** |
| a | centred window, **no** dark subtraction | 1.4238 / 1.5374 | −11.2 % / −14.3 % | rejected |
| b | gains that make the plate neutral **after the 3×3 CCM** | — | applying the stored gains then the stored CCM gives (1.6996, 0.1570, 1.3299): a **145 %** spread | **rejected** |
| c | gains including a lens / colour-shading term | — | per-region gains span 5.9 % (slot 0) and 8.7 % (slot 2) over 29 well-lit plate cells | **partly present, not modelled** |
| d | exposure / gain differences between the frames | — | the two frames differ in gain (200 vs 400) and illuminator state; their dark-subtracted estimates differ by **0.82 %**, EXStar's two files by **0.11 %** | **real effect, handled by dark subtraction** |
| e | black-level handling before the gain | — | required: see row 4 | **accepted as a component** |

Reading of the table:

* **The window is not optional.**  The discriminating number is the slot-2 gain:
  a plate-ROI mean gives 1.505 (−16 %), the centred window gives 1.767 (−1.5 %).
  The window sits on the brighter, more central part of the plate, where the
  sensor's colour balance differs from the plate average by 16 % — this is the
  colour shading measured under (c).
* **The dark subtraction is not optional.**  Without it the same window yields
  1.424 / 1.537, i.e. 11–14 % low.  The values EXStar uses are its own stored
  `ParaDark_0.txt`; because the three channels agree to 0.5 % of each other,
  their order is not identifiable and does not matter.
* **(b) is dead.**  Under a "WB then CCM" model the white plate must land
  neutral; it lands at (1.70, 0.16, 1.33).  The CCM's own implied neutral input
  is (0.561, 1.000, 0.755) in *its* slot order while the WB chain actually
  delivers (0.665, 1.000, 0.620) — a 10–22 % mismatch, far beyond the 1.5 %
  session drift of the gains.  Either the CCM was fitted in another domain or
  on another white; we do not use it (§10).
* **(c) exists but is small.**  Across the well-lit plate the per-cell gains
  span 5.9 % / 8.7 %; a single diagonal gain fitted on the centre leaves
  **1.6–1.9 %** cast over the rest of the plate (§8).  That is the honest
  ceiling on what one diagonal matrix can do here, and it is why this layer
  claims cast removal, not colorimetry.

### Window divisor

| divisor | window (full-res px) | on: err slot0 / slot2 | off: err slot0 / slot2 | max abs |
|---|---|---|---|---|
| 1 | 1280×1024 | +5.08 / −12.29 | +7.22 / −11.18 | 12.29 % |
| 2 | 640×512 | +1.79 / −5.46 | +2.57 / −4.71 | 5.46 % |
| 3 | 426×341 | −0.37 / −2.17 | +0.18 / −1.53 | 2.17 % |
| 4 | 320×256 | −0.82 / −1.76 | −0.23 / −1.23 | 1.76 % |
| 6 | 213×170 | −1.02 / −1.51 | −0.32 / −0.90 | 1.51 % |
| **8** | **160×128** | **−1.13 / −1.52** | **−0.43 / −0.84** | **1.52 %** |
| 10 | 128×102 | −1.23 / −1.57 | −0.53 / −0.74 | 1.57 % |
| 16 | 80×64 | −0.90 / −1.38 | −0.68 / −0.97 | 1.38 % |

The plateau is flat from 4 to 16 (1.4–1.8 %), so the divisor is **UNKNOWN**;
divisor 8 is our default because it is a clean power of two, not because it was
measured.  Divisor 1 (the whole frame) is ruled out.

The residual is systematically **negative on both channels** (≈ −1 %).  It is
about 0.014 in gain units, which is larger than EXStar's own 0.11 % frame-to-frame
difference (0.002) and smaller than any wrong hypothesis (0.11–0.35).  The
candidates are the demosaic difference (this analysis sums phase means; EXStar
sums a bilinearly debayered image), the possibility that the saved BMP is not
exactly the buffer handed to the algorithm (`vecImgT` contents are not logged),
and the unknown divisor.  We did not resolve it; it bounds the claim at "≤ 1.6 %".

---

## 5. The slot-order trap (this is the part that can silently ruin colour)

**PROVEN.** `cv2.cvtColor(mosaic, COLOR_BayerRG2BGR)` puts the Bayer phase at
**(even row, even column)** into output slot 0 — verified directly with a
synthetic mosaic whose four phases hold distinct values (100/200/200/150 → the
centre pixel of the debayered image is `[100, 200, 150]`).  EXStar's slot 0 is
the phase at **(odd row, odd column)**: with slot 0 = (even, even) the fit is
+11 %/−11 % wrong, with slot 0 = (odd, odd) it is −1.1 %/−1.5 % right.

Therefore the gains are **reversed** between EXStar's file and our BGR buffer:

| | slot 0 gain | slot 2 gain |
|---|---:|---:|
| EXStar `wbdata.txt` | **1.603466** | **1.794047** |
| our buffer, same frame | **1.766746** | **1.585387** |

Copying the stored `diag(...)` straight onto our buffer attaches each gain to the
wrong phase; on a real frame the measured cast is **11.28 % / 11.11 %** with the
gains placed literally, falling to **3.02 % / 2.08 %** once the documented
reversal is applied (the residual is the plate's own shading, §8) — a plausible-looking number
that would be wrong.  `calib/white_balance.py` therefore stores the slot
convention in the profile (`channel_order: "BGR"`, `slot0_phase: "p00"`,
`exstar_slot0_phase: "p11"`), refuses to load a profile whose convention does
not match, and does the swap in `to_exstar_matrix()` / `from_exstar_matrix()`
with a round-trip test.

What is **UNKNOWN** is which phase is physically red.  Nothing in these files
establishes it, and it does not affect the estimate — every quantity here is
defined against phase position and the engine's slot order.  It would only
matter for an absolute-colour claim.

---

## 6. What this implies for our Linux pipeline

Our engine (`recon/src/depth.cpp`, documented in
`docs/reconstruction/real_data_report.md` §14 and `docs/calibration/
colour_registration.md`) debayers the colour stream and applies **no** white
balance; the raw cast measured on real recordings is `R/G 0.72, B/G 0.65` on the
highlight set, and `tools/scan/color.py` removes it with a grey-world gain on
the highlight set of the *sampled points*.

### Is a diagonal matrix the right model?

**Yes for cast removal, and it is exactly what the vendor uses** — the recovered
algorithm produces a strictly diagonal matrix with green pinned at 1.0, so the
hardware/vendor pipeline itself has no cross-channel term at this stage.  Two
caveats, both measured here:

1. a single diagonal gain cannot remove the **1.6–1.9 % residual cast** that the
   colour shading leaves across the plate (§8);
2. it is a *grey-world* estimate against a white ceramic plate, so it inherits
   whatever spectral reflectance that plate has.  It is not a colorimetric
   white point.

Our existing `tools/scan/color.py` estimate (grey-world on the highlight set of
a finished point cloud) is a **different estimator on a different population**;
it is a display-time correction and should not be confused with this layer.  The
two are complementary: `calib/white_balance.py` produces a *device* profile from
a *capture-time* protocol; `tools/scan/color.py` remains a per-scan fallback.

### What has to be measured to set the gains

1. **Illuminator state.**  The gains are only valid for the illumination they
   were measured under; EXStar keeps two sets (`led`/`noled`) for that reason.
   Our capture tool can already drive `--strobe ROUTE:VALUE` (routes 0/1) and
   reports `strobe route0/route1` in `--probe`, so the hardware path exists.
2. **A frozen exposure and gain, recorded.**  The two conditions above use
   5000 µs with gain 200 and 400.  Auto-exposure must be *off* for a WB capture
   (otherwise the two conditions are not comparable and the setting is not
   reproducible).
3. **The plate, filling the central window.**  Not a colour chart, not a grey
   card: the scanner's own white plate, because that is the reference the
   vendor's numbers are relative to.
4. **The dark current.**  Already available from the factory artefacts in
   `sources/calibration/res_Einscan-E10_200x150/ParaDark_0.txt` (0.0450/0.0455/
   0.0450) — we do not need to measure it, we need to *apply* it.
5. **A second frame of the same condition at a different exposure** (not
   needed for the point estimate, needed to *prove* the invariance and to give
   the operator a reproducibility number).

---

## 7. The implemented layer

New file `calib/white_balance.py` (no existing module changed).

### 7.1 Estimator

`estimate(mosaic, EstimatorConfig)` reproduces §3 exactly: centred window,
per-slot sums, clip rejection, dark subtraction, normalisation to green.  It
returns a `WbEstimate` carrying the gains, the raw sums/means/counts, the window
bounds, and a `refused` flag with reasons.  `require()` raises
`RefusedCalibration` instead.

Refusal conditions (all tested):

| condition | why |
|---|---|
| `n_valid < min_samples` | not enough pixels to average |
| `clipped_fraction > max_clipped_fraction` | over-exposed: the window has no usable signal |
| a slot mean below `min_channel_dn` | channel at the dark/noise floor |
| dark ≥ signal in any slot | the model does not apply |
| a gain outside `[0.25, 4.0]` | physically implausible; refuse rather than publish |
| `validate_profile` finds no measurable condition | a report that measures nothing never passes |

### 7.2 The chain

`correct(bgr, gains, dark)` = subtract dark, then multiply.  **This matters**:
the gains are solved on dark-subtracted data, so applying them to raw 8-bit
values over-corrects — measured on the EXStar frames that is a **+11 % to
+15 %** cast, and it is exactly the sort of error that looks like "white balance
did not help".  `tests/test_white_balance.py` pins this down
(`test_gains_solved_on_dark_subtracted_data_need_the_same_dark_to_apply`).
`apply_gains` / `revert_gains` are an exact inverse pair and never clip, so the
operation is reversible by construction.

### 7.3 Profile schema

A separate, self-describing JSON layer — never a geometric profile, never
activated:

```json
{
  "schema": "einstar.white_balance.profile",
  "schema_version": 1,
  "layer": "photometric",
  "channel_order": "BGR",
  "slot0_phase": "p00",
  "exstar_slot0_phase": "p11",
  "estimator": {"id": "exstar-central-window-greyworld",
                "config": {"window_divisor": 8, "clip_level": 0.95,
                           "subtract_dark": true,
                           "dark_current_slot_order": [0.045, 0.0455, 0.045],
                           "min_gain": 0.25, "max_gain": 4.0,
                           "max_clipped_fraction": 0.2, "min_samples": 1024,
                           "min_channel_dn": 2.0, "max_holdout_cast": 0.05}},
  "geometry_profile_digest": null,
  "status": "estimated | validated | refused",
  "activation": {"activated": false, "auto_activate": false, "note": "..."},
  "conditions": {
    "led_on":  {"gains_bgr": [...], "exstar_matrix": [...9 floats...],
                "refused": false, "reasons": [], "measurement": {...},
                "holdout": {...}},
    "led_off": {...}
  },
  "refusal_reasons": [],
  "holdout_worst_max_cast": 0.0193,
  "capture_settings": {...}, "source": {"frames": {..., "sha256": ...}},
  "created_utc": "...", "digest": "sha256 over the profile minus this field"
}
```

Safety properties, each with a test:

* `layer` must be `photometric`; `geometry_profile_digest` is always `null` —
  a colour record cannot be merged into a geometry record;
* `activation.activated` is always `false`, `write_profile` **refuses** to write
  an activated profile, and `load_profile` refuses to load one;
* `write_profile` refuses any path containing a factory marker
  (`res/Einscan-E10`, `res/Einscan-EA`, `einstar-calibration-03`, `/factory/`);
* `load_profile` refuses a mismatched `channel_order` or `slot0_phase` — the
  red/blue swap is caught at load, not in the output;
* a refused estimate produces `status: "refused"` and `gains_bgr: null`; no
  default matrix is substituted (unlike EXStar, §2);
* `status` is only `"validated"` when an independent holdout cast is below
  `max_holdout_cast`; otherwise it stays `"estimated"`.

### 7.4 CLI

```
python -m calib.white_balance estimate --led-on ON.bmp --led-off OFF.bmp \
    --out wb.json --divisor 8 --dark 0.04502069 0.0455484 0.04504853 \
    --intervention "..."            # recorded verbatim in the profile
python -m calib.white_balance show wb.json
python -m calib.white_balance validate wb.json --led-on ON.bmp --led-off OFF.bmp
python -m calib.white_balance apply wb.json --condition led_on --image ON.bmp --out preview.png
```

`validate` exits non-zero when the holdout cast exceeds the limit, so it can be
used as a gate.

### 7.5 Tests

`tests/test_white_balance.py` — **35 tests, all passing** (see §9 for the run).
They cover: recovery of known synthetic gains; the slot-0 = (even, even)
convention; the reversal in `to_exstar_matrix`/`from_exstar_matrix`; window
centring and its insensitivity to changes outside the window; clip rejection;
the dark-subtraction direction and the "gains need the same dark to apply"
invariant; every refusal branch; exact reversibility; the cast metric; profile
construction, digest stability, schema/layer/channel checks, factory-path
refusal, activation refusal, status promotion; and the validation report
(including that a report measuring nothing never passes).  Two tests run on the
**real EXStar frames** and skip when the private fixture is absent; they assert
the ≤ 2 % reproduction of both stored files and the < 2 % outside-window cast.

---

## 8. Validation on the real EXStar frames

Estimator: divisor 8, dark subtracted, clip 0.95, profile `status: "estimated"`.
Measured with `validate_profile`: the estimation window and the **disjoint** lit-plate
annulus outside it, before (raw sensor) and after (dark-subtracted + gain).

| frame | region | px | R/G before | B/G before | cast before | R/G after | B/G after | **cast after** |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| `imageTex1` (on) | window | 5 120 | 0.7024 | 0.6504 | 34.96 % | 1.0000 | 1.0000 | 0.00 % |
| `imageTex1` (on) | outside | 27 891 | 0.6858 | 0.6505 | 34.95 % | 0.9807 | 1.0170 | **1.93 %** |
| `imageTex2` (off) | window | 5 120 | 0.6777 | 0.6226 | 37.74 % | 1.0000 | 1.0000 | 0.00 % |
| `imageTex2` (off) | outside | 26 398 | 0.6677 | 0.6262 | 37.38 % | 0.9881 | 1.0159 | **1.59 %** |

Reading:

* the in-window result is neutral **by construction** — it is a consistency
  check on the applied chain (a wrong slot order or a missing dark subtraction
  both break it), not evidence;
* the outside number is the evidence: **1.6–1.9 %** residual cast on 26–28 k
  pixels that the fit never saw, from a starting cast of 35–38 %;
* before the correction the sensor shows `R/G 0.68–0.70, B/G 0.62–0.65`, which
  agrees with the independent measurement in `docs/calibration/
  colour_registration.md` (`0.72 / 0.65` on a different recording), so these
  frames and that recording see the same sensor behaviour.

Reproduction of EXStar's own numbers (the point of §4):

| | EXStar stored | our estimate (EXStar slot order) | error |
|---|---:|---:|---:|
| `wbdata.txt` slot 0 | 1.603466 | 1.585387 | **−1.13 %** |
| `wbdata.txt` slot 2 | 1.794047 | 1.766746 | **−1.52 %** |
| `wbdata_noled.txt` slot 0 | 1.605239 | 1.598342 | **−0.43 %** |
| `wbdata_noled.txt` slot 2 | 1.796383 | 1.781225 | **−0.84 %** |

Session drift, for context (previous calibration → this one): slot 0
1.622664 → 1.603466 (−1.2 %), slot 2 1.766063 → 1.794047 (+1.6 %).  Our
residual is the same size as a recalibration-to-recalibration difference, which
is the right scale for it.

**What is not claimed.**  No colorimetric accuracy: the reference is a white
ceramic plate of unknown spectral reflectance under an unmeasured illuminant,
with no colour chart and no spectrophotometer.  What is claimed is that the cast
is removed to ~2 % on the plate, reproducibly, with a stored, reversible,
auditable gain set — and that the recipe matches the vendor's.

---

## 9. Test run

```
$ .venv/bin/python -m pytest tests/test_white_balance.py -q
...................................                                      [100%]
35 passed in 0.29s
```

Both real-data tests ran (the private fixture is present on this machine) and
passed.  The wider suite is unaffected: this change only adds files (§11 lists
what existing files would need to change and has *not* been changed).

---

## 10. What remains unknown, and what would settle it

| unknown | why it matters | evidence that would settle it |
|---|---|---|
| the window **divisor** (3…16) | 1.4–1.8 % on the gains | one more calibration pair, or a debug build / a `divisor` value visible in a caller |
| which mosaic phase is **physically red** | nothing for cast removal; everything for an absolute-colour or matrix claim | a frame of a known-colour object under the same illuminant, or the vendor's saved `res/texcamera/rgb_%d.bmp` (the directory exists but is **empty** for this run) |
| why our residual is −1 % rather than 0 | bounds the claim at ≤1.6 % | the exact buffer handed to `snWhiteBalanceCali` (not logged), or a repeat run |
| EXStar's numeric **acceptance threshold** (`standard rate` in `snTestColorMatrixWB`) | it is the vendor's own "insufficient evidence" line | decompiling `snTestColorMatrixWB` further, or a log from a *failed* WB run |
| why the **CCM** disagrees with the gains | if we ever want the CCM, the pair must be re-fitted together | a WB+CCM capture with a known white point, or the vendor's CCM-fitting protocol |
| the plate's spectral reflectance and the illuminant's spectrum | colorimetry | a reflectance measurement of the plate and a spectrometer on the LED — i.e. it is out of reach from these files |

**Explicitly out of scope and not attempted:** a colour-correction matrix, any
claim about hue accuracy, and any use of a third-party colour target.

---

## 11. Files this work did **not** change (and what should change, by their owners)

Per the task constraints nothing outside the four new files was touched.  The
following are real findings that belong to other owners:

1. **`recon/src/depth.cpp` / the colour sampling path** — the engine applies no
   white balance.  `calib/white_balance.py` now provides a profile that could be
   applied *before* the 180° rotation and debayer, but wiring it in is a
   capture/engine decision, and the profile must stay opt-in (`--profile`), not
   a default.  Note the slot-order trap (§5): the matrix to apply to our BGR
   buffer is `diag(1.7667, 1, 1.5854)` for this run, *not* the file's
   `diag(1.6035, 1, 1.7940)`.
2. **`docs/reconstruction/status.md`** — the row "real colour photometry/white
   balance **unverified**" can now cite this document for the WB *stage*; the
   colorimetry part stays unverified.
3. **`tools/scan/color.py`** — unchanged and still the right per-scan fallback;
   its docstring says "without a colour chart or a measured white reference it
   cannot claim absolute colour accuracy", which remains exactly true.
4. **The capture path** — a WB capture needs `--strobe 1:400` plus frozen
   exposure/gain with those values recorded in the manifest.  The CLI flags
   already exist (`--strobe ROUTE:VALUE`); the *recording* of "this group is a
   WB condition" does not.
5. **The factory dark current** (`sources/calibration/res_Einscan-E10_200x150/
   ParaDark_0.txt`) is needed by the chain and is already in the repo — good, but
   nothing currently reads it; `calib/white_balance.py` takes it as a parameter
   rather than loading it, so the caller must pass it.
6. **`docs/calibration/exstar_vs_factory_vs_linux.md` §7** labels the stored
   entries "`wbdata` R gain" and "`wbdata` B gain" (1.603466 / 1.794047).  That
   labelling assumes slot 0 is red, whereas §5 here shows slot 0 is the mosaic
   phase at (odd, odd) — which is *not* the slot our engine's
   `COLOR_BayerRG2BGR` puts there.  The two documents therefore use **opposite
   channel labels for the same two numbers**.  Neither is provably wrong (the
   physical CFA is not established, §10), but they must be reconciled before
   anyone quotes a "red gain" across them.  This document deliberately says
   slot 0 / slot 2.
7. **`calib/__init__.py`** lists the package's public modules in its docstring;
   `white_balance` is not in that list, because adding it would mean editing an
   existing file.  A one-line addition (`white_balance  photometric (white
   balance) layer, separate from geometry`) would keep the package overview
   accurate.

---

## Reproduction

```sh
# estimate from the two EXStar frames (private fixture, not in the repo)
.venv/bin/python -m calib.white_balance estimate \
  --led-on  ~/einstar-private/exstar-20261114/wb/imageTex1.bmp \
  --led-off ~/einstar-private/exstar-20261114/wb/imageTex2.bmp \
  --out /tmp/wb.json --divisor 8 \
  --dark 0.04502069 0.0455484 0.04504853 \
  --intervention "EXStar wb stage 2026-11-14 00:14:54"
.venv/bin/python -m calib.white_balance validate /tmp/wb.json \
  --led-on  ~/einstar-private/exstar-20261114/wb/imageTex1.bmp \
  --led-off ~/einstar-private/exstar-20261114/wb/imageTex2.bmp
.venv/bin/python -m pytest tests/test_white_balance.py -q
```

Machine-readable facts and every number above:
`analysis/exstar_reference/white_balance.json`.
