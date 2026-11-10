# Einstar calibration plate — offline audit and strongest supported specification

Status: 2026-11-10. Offline, read-only. No vendor binary was executed, no Ghidra/MCP
was used, and no calibration (CCF baseline, extrinsics, bundle adjustment) was used
to obtain any number below except where explicitly labelled *baseline-derived*.
Only `analysis/calibration/audit/` was written.

Confidence labels: **A** = directly measured and cross-checked here; **B** = one
interpretation step; **C** = hypothesis; **U** = unresolved.

Companion data: `plate_summary.json`, `corpus_metrics.json`.
Scripts: `measure_corpus.py`, `lattice_lib.py`, `ransac_grid.py` (all in this
directory; they read the vendor corpus read-only).

---

---

> **RESOLUTION (2026-11-10, later investigation).** The open questions below
> (grid variant, metric pitch, `200x150`) have since been settled from the
> vendor binaries and the official video — see
> `docs/calibration/plate_spec_recovered.md`.
>
> * The Einstar board is **8 × 5 with 41 markers** (37 small + 4 large at
>   (2,1),(4,1),(5,1),(3.5,3)); the official SHINING 3D calibration video shows
>   the physical board and the 8 × 7 DLL/manual drawing is a shared
>   cross-product illustration (byte-identical PNG in EXStar Hub 2.0.0-44).
> * EXStar's calibration lattice is **17 × 14 at 14.00 mm**, with markers on
>   every second node ⇒ **marker pitch 28.0 mm**. The per-board `AA`/`BB`
>   distances (150.804/√116 = 14.002, 125.071/√80 = 13.983) pin the lattice.
> * `200x150` is a profile/directory name, not a plate dimension, so the
>   25.0 mm hypothesis in §0.3/§6 is **retracted**.
> * The 27.72 mm figure is the **CCF-effective** scale (our solve reproduces the
>   vendor CCF's 158.557 mm baseline at that pitch); it is 1.0 % below the
>   nominal 28.0 mm model. The CCF in the captured install was written by
>   EXStar on the same corpus (2026-02-09 21:34:58), so this is a solver-scale
>   difference, not a board property.
> * Plate outline: 8 × 6 marker pitches = **224 × 168 mm** (measured
>   7.97 × 6.02 pitches).
>
> The measurements in this file (topology, outline, diameters, margins) remain
> valid; only the pitch interpretation and the variant question changed.


## 0. TL;DR

1. **The device corpus is 8 × 5, not 8 × 7.** All 25 left images form an 8-column,
   5-row lattice of **40 lattice nodes plus one off-lattice hole = 41 holes**.
   Three of the lattice nodes are large, and the off-lattice hole is large:
   **37 small + 4 large**. The current docstring's "40 small + 4 large = 44" is
   wrong. **[A]**
2. **The 8 × 7 drawing is the same design with two extra rows.** `board_front`
   asset (first PNG in `CalibrationPage_EA.dll`) is 8 × 7 = 56 lattice + 1
   off-lattice = **57 holes (53 small + 4 large)**, large markers exactly at
   (2,2), (4,2), (5,2), (3.5,4). Dropping rows 0 and 6 gives exactly
   (2,1), (4,1), (5,1), (3.5,3) — the doc's 8 × 5 values. **[A]** for the asset;
   **B** for "same design family".
3. **The 27.72 mm pitch is not established and is probably wrong.** A
   calibration-free measurement of the plate outline in the seven images where all
   four edges are visible gives **7.97 × 6.02 pitches** (margins 0.50/0.46 pitch
   along the 8-axis, 1.01/1.01 along the 5-axis). If the plate is the vendor
   "200 × 150" size, the pitch is **25.00 ± 0.13 mm**, and the plate is
   8 × 6 pitches by design (measured 7.97 × 6.02, 4:3). The baseline-derived 27.72 mm would require a
   **220.9 × 167.0 mm** plate — not a round size and not "200 × 150". **[B]** for
   25.0; the physical value stays **U** until calipers.
4. No board-geometry blob exists in any readable vendor file: the cloud
   `calibBoard` record is present but **empty**, the device dump is CCF/intrinsics,
   `activeBox.dat` is a 48-byte box, and the PLE is activation/licence data. **[A]**
5. The two variants are a one-parameter family (`rows` 5 or 7, same columns, same
   centre-relative large-marker rule), so dual-variant support is cheap — but the
   hole count and the pitch must not be hard-coded to the old 44/27.72 values.

---

## 1. Method (why these numbers are independent of the calibration)

Hole centres were detected as bright saturated blobs in the 1280 × 1024 IR images
(`thr = 180`, area 10–700 px), then fitted with a **projective lattice model**:
the four extreme holes are mapped to the four grid corners, the homography is
refined by least squares, and points are assigned to integer nodes. The fit is
scored by occupied-node coverage and residual in pitch units. Nothing in this
chain uses `calib.geometry`, the CCF, the baseline, or any extrinsic.

The only metric anchor used below is the **plate outline**, measured in pitch
units by sampling brightness profiles outward from the outer holes along the two
grid axes and finding the plate→background step. The 200 × 150 mm plate size is
the vendor's own profile name (`res/Einscan-E10/200x150/`, and the device path
table `"200x150/LeftCCF.txt"`, `"200x150/activeBox.dat"`,
`"200x150/lookupTable.dat"`).

Reproduction: `python analysis/calibration/audit/measure_corpus.py`.

---

## 2. Evidence table — every relevant vendor source

| source | what it is | grid | holes | large markers | type | verdict |
|---|---|---|---|---|---|---|
| `vendor-calib-rapid/imageLeft1..25.bmp` (25 × 1280×1024) | real IR captures, vendor 25-pose corpus | **8 × 5** | 40 lattice + 1 off = **41** (37 small + 4 large) | (2,1),(4,1),(5,1) + (3.5,3) *(canonical frame; see §4)* | photographic | **A** |
| `CalibrationPage_EA.dll` PNG #1 → `img_00.png` (1720×1486) | `board_front`-type board drawing | **8 × 7** | 56 lattice + 1 off = **57** (53 small + 4 large) | (2,2),(4,2),(5,2) + (3.5,4) | schematic (vector-style) | **A** |
| Manual PDF p.35, UI panel (2479×3508 render) | software UI mock-up | **8 × 7** | 56 + 1 = 57 positions (53 visible; the focus overlay hides the rest) | (2,2),(4,2),(5,2) + (3.5,4) | schematic UI | **A** |
| Manual PDF p.35, 3-D scene | same 3-D scene as the video | 8 × 5 in the video frames; page render not independently countable (page white merges with plate) | — | same as video | render | **C** |
| `videos/calib_quik.mp4` (664×546, 2262 fr) | calibration quick-guide animation | **8 × 5** | 41 | (2,1),(4,1),(5,1) + (3.5,3) in the rectified frame | render | **A** for count |
| `img_01.png` (1920×920) | dark gradient/background plate | — | — | — | texture | n/a |
| `img_02..13.png` (650×620) | scanner/board instructional illustrations | board shown in steep perspective, partly occluded; same 3-D asset as the video | — | — | render | **C** (not independently countable) |
| `vec_00..23.svg` | progress-bar and step icons | none | — | — | UI | n/a |
| `rapidCameraCalibrate.xml` | workflow config (5 groups × 5 distances) | none | — | — | config | no geometry |
| `activeBox.dat` (48 B) | 6 little-endian doubles | none | — | — | device config | no geometry |
| device dump `calibration.bin` (6568 B) | `tagDevData`: CCF/intrinsics | none | — | — | binary | no geometry |
| log `calib_window.log` `calibBoard` | cloud response | — | `boarddata=""`, `boarddatacount=0`, `boarddatatype=""`, `file=""`, `sn=""` | — | JSON | **empty** |

Notes on the sources:

* The 25 corpus images are the only *photographic* evidence; every one of them is
  8 × 5. The 8 × 7 appears only in drawings/mock-ups (DLL PNG, manual UI panel).
* Asset order was verified by matching each extracted PNG's IHDR to the DLL
  offsets: `img_00` = offset 45252 (1720 × 1486), `img_01` = 194884 (1920 × 920),
  `img_02` = 1044405 (620 × 650), etc.
* The video is a *product* animation shipped with EXStar 1.2.2.0 and shows the
  8 × 5 board, matching the corpus. This is the strongest non-corpus evidence for
  the physical board. 38 frames were sampled at 1 fps from the 2262-frame clip;
  in every frame where the plate is visible it is the same 8 × 5 board (the
  rectified frame of the clearest pose measures 8 columns × 5 rows with the four
  large markers at (2,1), (4,1), (5,1), (3.5,3)).
* The manual's 3-D scene is the same rendering family as the video (same scanner,
  dashed axis, green focus ellipse); the page render could not be rectified
  cleanly because the page background and the plate are both white.

---

## 3. Variant A — 8 × 5 (the device corpus and the video)

Board coordinates `(u, v)`: `u` = 0…7 along the 8-hole (long) side, `v` = 0…4
along the 5-hole side. Units are pitches.

### 3.1 Hole inventory

| item | count | coordinates |
|---|---|---|
| lattice nodes | 40 | all `(u,v)`, `u=0..7`, `v=0..4` |
| small holes | 37 | lattice nodes minus the three large nodes |
| large, on lattice | 3 | **(2,1), (4,1), (5,1)** |
| large, off lattice | 1 | **(3.5, 3)** — half-way between columns 3 and 4, two rows below the large row |
| **total holes** | **41** | |

The 180°-rotated description of the *same* physical pattern (the frame the
lattice fit happens to choose on most images) is
`(2,3), (3,3), (5,3)` + `(3.5,1)`. Both descriptions were measured; they are
related by `(u,v) → (7−u, 4−v)`, which is a proper rotation, so they are the same
board seen with the plate turned 180°. **[A]**

### 3.2 Invariants (frame-independent)

* The three on-lattice large holes are **collinear in one row**, at columns
  `a, a+2, a+3` (gaps **2 : 1**). Measured gap ratio 2.0–2.2 across the corpus.
* The off-lattice large hole is at `u = a + 1.5`, i.e. the **midpoint of the outer
  two** (`a` and `a+3`), and **two rows away** from the large row. Both properties
  were measured independently in the manual UI panel (see §4) and the corpus.
* The pattern is **chiral and not 180°-symmetric**: rotating it 180° moves the
  three-marker row to the other side and mirrors the gaps. A single view therefore
  fixes the board pose without a 180° ambiguity, as the existing doc claims.

### 3.3 Measured dimensions (corpus, calibration-free)

| quantity | value | n | sd | confidence |
|---|---|---|---|---|
| plate width / pitch | **7.967** | 7 | 0.025 | A |
| plate height / pitch | **6.024** | 7 | 0.023 | A |
| plate aspect | **1.321** (4:3 = 1.333) | 7 | — | A |
| margin left / right | 0.50 / 0.46 pitch | 7 | 0.03 | A |
| margin top / bottom | 1.01 / 1.01 pitch | 7 | 0.03 | A |
| small-hole diameter / pitch | **0.130** | 8 | 0.004 | A |
| large-hole diameter / pitch | **0.255** | 8 | 0.003 | A |
| large / small diameter | **1.96** | 8 | 0.06 | A |
| hole roundness (ellipse minor/major after rectification) | 0.90 | 8 | — | B |

The plate is therefore **8 × 6 pitches** — a 4:3 rectangle — with the hole field
inset by half a pitch on the long sides and a full pitch on the short sides.

### 3.4 Metric values under the 200 × 150 mm anchor

| quantity | at p = 25.0 mm | at p = 27.72 mm |
|---|---|---|
| plate width × height | **199.2 × 150.6 mm** | 220.9 × 167.0 mm |
| small hole diameter | 3.25 mm | 3.60 mm |
| large hole diameter | 6.38 mm | 7.07 mm |
| left/right margin | 12.5 mm | 13.9 mm |
| top/bottom margin | 25.2 mm | 28.0 mm |

The 25.0 mm column reproduces the vendor's "200 × 150" profile to **0.4 %** in
both axes. The 27.72 mm column requires a 221 × 167 mm plate. **[B]** for 25.0,
**U** for the physical value.

---

## 4. Variant B — 8 × 7 (the DLL drawing and the manual UI panel)

### 4.1 The DLL drawing (`img_00.png`, first PNG at file offset 45252)

* Detected 57 outer rings: **56 lattice (8 × 7) + 1 off-lattice**.
* 53 small + 4 large; outer-radius ratio large/small = **1.70** (drawing; the IR
  corpus measures 1.96 because the saturated core blooms).
* Large markers measured at columns **2.04, 3.99, 4.96** in row **2.03**, and at
  column **3.50** in row **3.98** — i.e. exactly `(2,2), (4,2), (5,2), (3.5,4)`.
  **[A]** The centre-relative hypothesis in the task is **confirmed** for 8 × 7.

### 4.2 Manual page 35, UI panel

Pixel measurements in the 748 × 516 panel crop (2× upscale): large markers at
`(192.18, 191.95)`, `(324.09, 191.99)`, `(390.13, 191.91)` and
`(291.19, 328.45)`. Gaps 131.9 px and 66.0 px → **2.0 : 1**; the off-lattice
marker is 136.5 px from the line (2.07 × the short gap) and its `u` is
192.18 + 1.5 × 66.0 = 291.2. The panel is **8 × 7 = 56 lattice positions + 1 off-lattice = 57**, with the same
four large positions as the DLL drawing; 53 dots are visible in the page render
(the translucent focus circle hides the rest), 4 of them large. **[A]**

### 4.3 Relation between the variants

For `rows = r`, `cy = (r−1)/2`, the large markers are

```
(2, cy−1), (4, cy−1), (5, cy−1), (3.5, cy+1)
```

* r = 5 → (2,1), (4,1), (5,1), (3.5,3)   [corpus / video]
* r = 7 → (2,2), (4,2), (5,2), (3.5,4)   [drawing / manual]

The 8 × 5 board is exactly the 8 × 7 board with rows 0 and 6 removed, with the
same columns and the same off-lattice `u = 3.5`. **[B]** That is a designed family,
not a coincidence. The 8 × 7 *plate* size cannot be measured from the shipped
drawing (it is cropped to the dot field); if the same margins held it would be
8 × 8 pitches (200 × 200 mm at 25 mm pitch) — hypothesis only, **[C]**.

---

## 5. Corpus re-measurement (per image)

`hyp` is the automatically selected lattice (8 × 7 wins only as an alias on five
images — never with full occupancy); `cov` = occupied nodes / (cols·rows);
`nodes` = lattice nodes actually occupied; `plate` = outline in pitch units where
all four edges were measurable; `pitch` = 200/plate_W and 150/plate_H averaged;
`diam` = small / large diameter in pitch units.

| image | hyp | cov | nodes | dets | plate (pitch) | pitch (mm) | diam small/large (ratio) |
|---|---|---|---|---|---|---|---|
| L1 | 8x7 alias | 0.77 | 43 | 63 | — | — | 0.131 / 0.251 (1.91) |
| L2 | 8x7 alias | 0.79 | 44 | 64 | — | — | 0.130 / 0.249 (1.92) |
| L3 | 8x5 | 0.97 | 39 | 40 | — | — | — |
| L4 | 8x5 | 0.97 | 39 | 40 | — | — | — |
| L5 | 8x5 | 1.00 | 40 | 41 | 7.94 × 6.02 | 25.05 | — |
| L6 | 8x5 | 1.00 | 40 | 41 | 7.95 × 6.01 | 25.06 | — |
| L7 | 8x5 | 1.00 | 40 | 41 | 7.95 × 6.00 | 25.07 | — |
| L8 | 8x5 | 1.00 | 40 | 41 | 7.96 × 6.02 | 25.01 | 0.133 / 0.252 (1.89) |
| L9 | 8x5 | 1.00 | 40 | 41 | 7.97 × 6.02 | 25.00 | 0.130 / 0.258 (1.98) |
| L10 | 8x7 alias | 0.57 | 32 | 40 | — | — | 0.174 / 0.342 (1.96) |
| L11 | 8x5 | 1.00 | 40 | 41 | — | — | — |
| L12 | 8x5 | 1.00 | 40 | 41 | 7.98 × 6.02 | 24.98 | — |
| L13 | 8x5 | 0.35 | 16 | 44 | — | — | 0.102 / 0.308 (3.01) |
| L14 | 8x5 | 0.42 | 17 | 31 | — | — | 0.166 / 0.377 (2.28) |
| L15 | 8x5 | 0.53 | 21 | 37 | — | — | 0.140 / 0.287 (2.04) |
| L16 | 8x5 | 1.00 | 40 | 41 | 8.01 × 6.07 | 24.83 | 0.129 / 0.250 (1.93) |
| L17 | 8x7 alias | 0.57 | 32 | 40 | — | — | 0.186 / 0.362 (1.94) |
| L18 | 8x7 alias | 0.55 | 31 | 39 | — | — | 0.192 / 0.376 (1.96) |
| L19 | 8x5 | 0.95 | 38 | 39 | — | — | 0.128 / 0.255 (2.00) |
| L20 | 8x5 | 1.00 | 40 | 41 | — | — | 0.126 / 0.258 (2.04) |
| L21 | 8x5 | 1.00 | 40 | 41 | — | — | 0.137 / 0.253 (1.85) |
| L22 | 8x5 | 1.00 | 40 | 41 | — | — | 0.137 / 0.255 (1.86) |
| L23 | 8x5 | 0.57 | 23 | 38 | — | — | 0.190 / 0.388 (2.05) |
| L24 | 8x5 | 1.00 | 40 | 41 | — | — | 0.130 / 0.258 (1.99) |
| L25 | 8x5 | 1.00 | 40 | 41 | — | — | — |

Distribution summary:

* lattice: **20/25 = 8 × 5**, 5/25 = 8 × 7 alias (coverage ≤ 0.79 — these are
  stray reflections or every-other-row aliases, **not** 8 × 7 boards).
* occupied nodes for the 8 × 5 fits: median **40**, min 16 (L13, low-contrast /
  motion-blurred), with 17 images at 38–40 nodes.
* plate width 7.94–8.02 pitches (sd 0.025); height 6.00–6.07 (sd 0.023).
* small diameter 0.126–0.137 pitch in the good images; two low-contrast outliers
  (0.166–0.192) are blurred and were excluded from the medians; large 0.249–0.258.
* pitch ratio u : v after rectification is 1 by construction (the homography is
  fitted to the lattice); the *roundness* of the holes after that rectification
  (0.90 median, 0.93–0.95 in the sharpest images) confirms the square-grid
  assumption at the ~7–10 % level, limited by IR blooming and motion blur rather
  than by a rectangular lattice. A genuinely rectangular grid would show a
  systematic ellipse ratio equal to the pitch ratio; no such systematic is seen.

---

## 6. Pitch — evidence, conflict, uncertainty

| estimator | value | anchor | independent of baseline? | confidence |
|---|---|---|---|---|
| previous triangulation | 27.72 mm | CCF stereo baseline 158.557 mm | **no** | C (suspect) |
| plate outline, width | 200 / 7.967 = **25.10 ± 0.08 mm** | plate = 200 mm | **yes** | B |
| plate outline, height | 150 / 6.024 = **24.90 ± 0.09 mm** | plate = 150 mm | **yes** | B |
| combined | **25.00 ± 0.13 mm** (sem 0.036) | plate = 200 × 150 | yes | B |

Why the conflict matters:

* The two plate-derived estimates agree to **0.8 %**, and the measured plate
  aspect (1.321) matches 200:150 (1.333) to **0.9 %**. That is a strong internal
  consistency check: the plate really is a 4:3 rectangle and the grid really is
  square.
* 25.00 mm is a **round design value**; 27.72 mm is not. At 25.00 mm the plate is
  exactly 8 × 6 pitches with margins of 0.5 and 1.0 pitch.
* For 27.72 mm to be right, the plate would have to be **220.9 × 167.0 mm**, which
  matches neither the vendor "200 × 150" profile nor a round design size.
* Therefore **at least one of {plate size = 200 × 150, CCF baseline = 158.557} is
  wrong**, or the earlier triangulation had a scale error. The baseline-derived
  27.72 must not be presented as independent validation.

Caveat on the anchor: `200x150` is the vendor profile directory and the device
path-table key. It could in principle denote the scan volume rather than the plate.
If it does, the plate-anchored pitch collapses and the 27.72 (or a caliper value)
must be used. This is the single most important thing to settle physically.

---

## 7. Board descriptors — what exists and what does not

| artefact | content | board geometry? |
|---|---|---|
| cloud `calibBoard` (log line, `parseScanPackage` response) | `aa0/aa1/bb0/bb1 = 0`, `boarddata = ""`, `boarddatacount = 0`, `boarddatatype = ""`, `file = ""`, `sn = ""`, `version = "1.0.0"` | **no — present but empty** |
| `ple` block (`<redacted-part-number>.ple`, dfsID `ple/680eb84416e461004fa2074934078b98`) | activation fields: `AA = 150.804`, `BB = 125.071`, `authDays = 0`, `authorizationTime = 2023-07-20`, `fitPleName`, errorCode 0. The `plefiledata` bytes in the log are **lossy** (non-printables replaced by `?`, ~11.6 kB as rendered) and are not a reliable binary. | **no** — licence/activation, not geometry |
| device dump `calibration.bin` (6568 B) | `tagDevData`; offset 12 parses as doubles 1158.76, 1158.68, 624.19, 534.60, −0.162, 0.173, 2e−4, 1e−4 … = intrinsics/distortion and the 3873-byte CCF block at 0x39B | **no** |
| `activeBox.dat` (48 B) | six little-endian doubles, four ≈ −12000 and two = 200 | **no** — scan box |
| `200x150/` folder | `ParaCCM_*.txt`, `ParaDark_*.txt` only | **no** |
| `lookupTable.dat` | referenced by the device path table, **not present** in the vendor tree | unknown |
| `rapidCameraCalibrate.xml` | workflow only (5 groups × 5 distances, angle gates, gain/exposure) | **no** |
| algorithm DLL strings | `Sn3D_calibTable_recognition`, `CalibrationTableParam`, `bigCircleNum`, `smallNumThreshold`, `matrix6X2`, `matrix10X2`, `boardData`, `iSpace`, `six_big_point_calibTable` | board *templates* exist in code, but no readable blob |
| `iX=17, iY=14, iSpace=14` (log `initComputeAlg`) | do not match 8 × 5 or 8 × 7; `isUseLookup = false` and `srcCalParam = null` | **probably LUT/grid parameters, not board counts** (C) |

Identifiers found: board SN **<redacted-part-number>**, camera SN
**<redacted-device-serial>**, device SN **<redacted-serial>**, device series 4 /
sub-type 2, firmware `EinScan10_01_SC130_FX3_V2.10_FPGA_V3.7_EN`, activation
<redacted-production-date>, authorization 2023-07-20, `aa/bb` 150.804 / 125.071.

Conclusion: **there is no readable board-geometry blob anywhere in the vendor
files.** The geometry is either hard-coded in the algorithm DLL templates or
supplied by parameters we have not decoded; the plate must be specified from
measurement, not from a vendor descriptor.

---

## 8. Strongest supported specification

### A. Measured from vendor images (high confidence)

* Grid: 8 columns × 5 rows, square lattice.
* 40 lattice nodes; 3 of them are large holes at (2,1), (4,1), (5,1).
* One additional large hole **off the lattice** at (3.5, 3).
* Total 41 holes: **37 small + 4 large**.
* Small hole diameter ≈ 0.130 pitch; large ≈ 0.255 pitch; ratio ≈ 1.96.
* Plate outline = 7.97 × 6.02 pitches, margins 0.50/0.46 and 1.01/1.01 pitch.
* Grid is square (rectified hole roundness 0.90, limited by blur).

### B. Inferred from UI/manual/vendor naming (one interpretation step)

* The 8 × 7 (57 holes, (2,2),(4,2),(5,2),(3.5,4)) is the **same design** with two
  extra rows; it is the documented/UI variant, not what the device corpus shows.
* The device's physical board is the **8 × 5** variant (corpus + video agree).
* Plate is **200 × 150 mm**, pitch **25.00 mm**; designed as 8 × 6 pitches
  (measured 7.97 × 6.02).
* Physical hole diameters ≈ 3.25 mm small / 6.4 mm large (design values probably
  3.2 / 6.4 mm; IR blooming inflates the measured core by ~5 %).
* Plate material is white with through-holes (bright in IR from behind, dark in
  visible light), not printed black dots.

### C. Hypotheses (not established)

* The 8 × 7 board belongs to a different model or is generic UI art.
* `200x150` denotes the plate, not the scan volume.
* `iX/iY/iSpace` are LUT parameters.
* The CCF baseline is ~11 % off, or the earlier triangulation had a scale bug.

### U. Requires physical measurement

* Which variant the physical plate is (41 vs 57 holes).
* Absolute pitch (25.0 vs 27.72 mm).
* Whether the plate is exactly 200 × 150 mm.
* Hole diameters, plate thickness, chamfer, and whether the large holes are
  through-holes or blind.

---

## 9. Recommended code changes for dual-variant compatibility

The current `calib/plate.py` already computes `n_small`/`n_holes` from the rule
and carries a note about the pitch conflict — good. Remaining changes:

1. **Fix the hole inventory in `PlateSpec`.** `n_small` is 37 (not 40) for 8 × 5
   and 53 for 8 × 7; `n_holes` is 41 / 57. Add `n_off_lattice = 1` and assert
   `n_holes == cols*rows + 1`. Update the module docstring ("40 small + 4 large"
   is wrong) and the `calib/__init__.py` line "40 small circular holes and 4 large
   holes".
2. **Make the variants explicit.** Add two frozen constants:
   `EINSTAR_8x5 = PlateSpec(8, 5, pitch_mm=25.0)` and
   `MANUAL_8x7 = PlateSpec(8, 7, pitch_mm=25.0)`, both generated by the
   centre-relative rule, plus `variant_id` and `provenance` fields. Do not let the
   default constructor silently pick 27.72; require a pitch source.
3. **Pitch provenance.** Add `pitch_source: Literal["caliper","plate_size","baseline"]`
   and `pitch_sigma_mm`. Default to `plate_size` (25.0 ± 0.13) with a warning that
   the baseline-derived 27.72 is not independent; fail closed if a metric pipeline
   requests "calibrated scale" while the source is `plate_size`.
4. **Detector (`calib/detect.py`).**
   * `_pattern_candidates` is variant-agnostic (it uses relative t/off ratios and
     `plate.big_points_uv()`); keep it, but replace the docstring's hard-coded
     (2,4,5)/(3.5,3) with the `PlateSpec` rule.
   * Keep the four large anchors in `GridObservation.big_markers` — currently
     `bm = np.array([src for src in []])` discards them, so downstream code cannot
     verify the chirality or the off-lattice marker.
   * Replace `if len(blobs) < 30: return None` with a fraction of
     `cols*rows` (e.g. 0.55) so 8 × 7 (56 nodes) and cropped 8 × 5 views both work.
   * The `inl.sum() < 30` early break likewise should scale with `cols*rows`.
5. **Board-frame validation.** After fitting, check the four large markers in the
   *image* frame: three collinear with a 2:1 gap, one off-line at the midpoint two
   rows away. Reject a fit that only satisfies the lattice (this is what currently
   lets an 8 × 7 alias win on five images).
6. **Tests** (`tests/`):
   * synthetic render of 8 × 5 with 41 holes and of 8 × 7 with 57, both polarities,
     and assert the recovered variant, hole count, and large-marker coordinates;
   * a 180°-rotated render must be resolved to the correct board frame (the
     chirality is the discriminator), and a mirrored render must **not** be
     accepted as the same variant;
   * a scale test: plate outline = 8 × 6 pitches within 1 %, with the pitch
     recovered from the plate size to 25.0 ± 0.2 mm on synthetic data;
   * a regression test that the old 44-hole / 27.72 mm constants are gone.
7. **Docs/config.** Update `docs/calibration/workflow_and_replacement.md` §5/§12.1
   and `docs/calibration/model.md` to state 41 holes, the 8 × 7 documented variant,
   and the pitch conflict. Add a `plate_variant` field to the calibration profile
   written by `calib/profiles.py` so a scan can be traced to the board used.

---

## 10. What physical measurements settle it (in priority order)

1. **Count the holes.** 41 (8 × 5 + 1) vs 57 (8 × 7 + 1). This is unambiguous and
   settles the variant. Also confirm 37 small + 4 large and that three large holes
   sit *on* the lattice and one is *between* columns, two rows away.
2. **Caliper the pitch.** Centre-to-centre of adjacent small holes, 10 pitches
   averaged: expect **25.0 mm** (plate anchor) or **27.7 mm** (baseline). Also
   measure the outer-hole span (7 pitches) and the 4-row span.
3. **Caliper the plate.** Overall width × height and thickness; check 200 × 150 mm
   and the margins (12.5 mm long sides, 25 mm short sides). If the plate is not
   200 × 150, the pitch anchor is invalid.
4. **Hole diameters.** Pin gauges or a close-up photo with a scale: expect
   ~3.2 mm small and ~6.4 mm large (ratio 2:1). This also tests whether the IR
   blob is the hole or a printed dot.
5. **One photograph of the front face**, flat, with a ruler in frame. That single
   image resolves the variant, the large-marker chirality, and the plate size at
   once, and can be added to the audit as ground truth.
6. Optional: read the plate's label/serial and the box; the vendor may print
   "200 × 150" and a part number on the back.

Until 1–3 are done, the honest state is: **8 × 5 is the device variant [B];
8 × 7 is the documented variant [B]; the pitch is 25.0 ± 0.13 mm if the plate is
200 × 150 mm [B], conflicting with the baseline-derived 27.72 mm [C]; the physical
values remain [U].**
