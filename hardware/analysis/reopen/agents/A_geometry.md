# A — Independent photogrammetric calibration of `P1_top_side.webp`

**Scope:** geometry only (package body, scale, pin-1 orientation, 121-ball grid). This investigation
did **not** read the repository's existing hardware reports or calibration files before measuring.
All numbers below were measured from the photograph first; datasheet facts were consulted only
afterwards to interpret them. Work was read-only; scripts and crops are under
`hardware/analysis/reopen/agents/A_geom/`, the figure under `hardware/analysis/reopen/figs/`.

* Image: `hardware/photos/P1_top_side.webp`, 2341 x 1199 px, lossy VP8 WebP, RGB.
* The board is photographed **rotated 180 deg** relative to normal text orientation (all package
  markings read upside-down).
* Balls are **under** the package and are not visible on a top-side photograph. The 121-ball grid
  below is therefore a *model* anchored to the measured package edges, not an observation of balls.

---

## 1. Method

The FX3 package is a dark purple moulded body on green soldermask. The discriminative colour
signature is **R − G**: package mould and its lit bevel are positive (R > G, +13…+22 DN), the
soldermask and its shadow are negative (R < G, −10…−25 DN), white silkscreen is near neutral.
Therefore the package silhouette is objectively the **zero crossing of R − G** along scanlines
perpendicular to each edge. This is threshold-free in the usual sense (the threshold is fixed at
zero by physics, not chosen), and it uses both "dark package vs green board" and "bevel vs board".

Procedure (`A_geom/edge_lib.py`, `A_geom/measure_edges.py`):

1. Gaussian smooth sigma = 1.2 px, compute p = R − G.
2. For each scanline, sample p at 0.05 px steps along the edge normal (±7 px window), find the
   p = 0 crossing with the largest local gradient, and interpolate linearly.
3. Robust straight-line fit per edge with iterative 3-sigma clipping.
4. Repeat with the p = 0 threshold moved from −6 to +6 DN to quantify **threshold sensitivity**.

Edges are reported as lines in image coordinates (x right, y down):

| edge | fitted line | RMS (px) | scanline span | points |
|---|---|---|---|---|
| left  (W) | x = −0.000684·y + 1174.780 | 0.53 | y 665–945 | 281 |
| right (E) | x = −0.005158·y + 1545.858 | 0.78 | y 665–935 | 271 |
| top   (N) | y = +0.003352·x + 618.837 | 0.58 | x 1205–1510 | 306 |
| bottom(S) | y = +0.004202·x + 982.924 | 0.48 | x 1205–1490 | 278 |

At the package centre (y = 806.0, x = 1358.0):

| edge | coordinate (px) |
|---|---|
| left  | x = 1174.23 |
| right | x = 1541.70 |
| top   | y = 623.39 |
| bottom| y = 988.63 |

**Body silhouette: 367.47 px (x) × 365.24 px (y)**, ratio 1.0061 (0.6 % non-square — see §5).
The edges are tilted by +0.04 deg…+0.30 deg with respect to the image axes (mean ≈ +0.20 deg);
the left/right tilts are not perfectly parallel (0.26 deg residual), which is one source of
systematic edge bias.

**Threshold sensitivity** (edge position shift at the middle scanline, DN = 8-bit colour units):

| edge | t = −6 | −4 | −2 | 0 | +2 | +4 | +6 | slope (px/DN) |
|---|---|---|---|---|---|---|---|---|
| left  | −1.25 | −0.78 | −0.43 | 0 | +0.42 | +0.79 | +1.18 | +0.20 |
| right | +3.17 | +2.20 | +1.09 | 0 | −0.89 | −1.51 | −2.19 | −0.45 |
| top   | −2.24 | −1.40 | −0.66 | 0 | +0.80 | +1.70 | +2.60 | +0.40 |
| bottom| +0.86 | +0.47 | +0.35 | 0 | −0.26 | −0.54 | −0.90 | −0.15 |

A plausible ±3 DN systematic (colour balance of a lossy WebP) therefore moves the right/top edges
by ±1.3 px and the left/bottom edges by ±0.5 px; this is folded into the uncertainty budget
(§4, §6) rather than into the RMS.

---

## 2. Scale and independent cross-checks

**FX3 scale from the 10.00 mm body:**

* s_x = 367.47 / 10.00 = **36.75 px/mm**
* s_y = 365.24 / 10.00 = **36.52 px/mm**
* adopted mean **s = 36.64 px/mm**

| object | known dimension | measured (px) | scale (px/mm) | Δ vs 36.64 |
|---|---|---|---|---|
| Pango PGL25G body height | 23.00 mm | 827.41 | 35.97 | −1.8 % |
| DDR3L package width | 14.00 mm | 509.81 | 36.42 | −0.6 % |
| DDR3L package height | 8.00 mm | 291.04 | 36.38 | −0.7 % |
| connector row pitch | 0.50 mm | 17.90 | 35.80 | −2.3 % |

Mean of the four cross-checks: **36.14 px/mm**, spread 0.26 px/mm (0.7 %). The FX3-derived value is
+1.4 % above this mean. The most likely explanation is the FX3's visibly rounded/lit mould edge: its
R−G silhouette includes roughly 0.1 mm of bevel on each side, i.e. the nominal 10.00 mm body is
about 0.2 mm (≈7 px) smaller than the measured silhouette. With 358–360 px for the nominal body,
the FX3 scale becomes 35.8–36.0 px/mm, in agreement with the cross-checks. **This 0.1 mm/side
convention ambiguity is carried as a systematic, not hidden** (§6).

Details / caveats of the cross-checks (explicitly incomplete where they are incomplete):

* **Pango PGL25G** — 484-ball FBGA, package attribute `BGA-484_23.0x23.0mm_Layout22x2` from a
  distributor listing (etei.com product page). The right side of the package is cut off by the
  image border, so only the **height** (top y = 198.1 at x = 2025, bottom y = 1025.5) could be used.
  Top/bottom edge fits: RMS 0.77 px and 1.56 px (bottom contaminated by white text below the
  package). The official Pango package drawing (PK02008_PGL25G_FBG484) could not be fetched —
  network DNS/PDF fetches timed out — so the 23.00 mm value rests on the distributor attribute.
* **Micron DDR3L** — marking `DP7K` / `2HK17`. The absolute 8 × 14 mm dimensions are **inferred**
  from the 96-ball DDR3L family, not confirmed from a datasheet that I could fetch. What *is*
  measured is the rectangle 509.81 × 291.04 px, whose aspect ratio is **1.7517**; the 14:8 ratio is
  1.7500, i.e. the pixels are square to within **0.1 %** (anisotropic resampling ruled out).
  The scale *values* for this row depend on the 8 × 14 mm assumption.
* **Connector** — the visible row of contacts/pads between y ≈ 341 and y ≈ 859 has a median pitch of
  **17.90 px** (autocorrelation peak at lag 18 px). If the pitch is the standard 0.50 mm, the scale
  is 35.80 px/mm. **Caveat:** I count ≈30 regularly spaced contacts in that row, not 40, so this
  connector is probably *not* the 40-pin board-to-board connector named in the brief (which may be
  J4, photographed separately as `P5_connector_J4.webp`). This cross-check is therefore the weakest
  of the three, and it is flagged as incomplete.
* **0402/0603 passives were not measured** (time ran out); they were not needed to meet the
  two-object requirement.

Pixel aspect: the DDR3 aspect check (1.7517 vs 1.7500) shows **no measurable anisotropic scaling**
of the WebP, so x and y scales are directly comparable.

---

## 3. Pin-1 orientation

Datasheet convention (checked after measuring): the FX3 121-ball ball map is **Top View**, columns
1→11 left-to-right and rows A→L top-to-bottom (no row I), A1 top-left when the marking is readable.
Since the physical marking is upside down in this image, the package frame is rotated **180 deg**,
so A1 must lie at the **bottom-right** corner in image coordinates.

| cue | image coordinates (px) | what it indicates |
|---|---|---|
| package marking `CYUSB3014-BZX` (plus `CYP 625297`, `A 33 THA`, `1 2237`) | text block x ≈ 1195–1520, y ≈ 760–1010; glyphs rotated 180 deg | marking frame rotated 180 deg ⇒ A1 at **bottom-right** |
| laser/ink dot on mould | centroid **(1508.90, 955.42)**, bbox x 1501–1517, y 947–964 (diameter ≈16 px ≈ 0.44 mm) | **bottom-right** corner (5.3 px = 0.14 mm from predicted A1) |
| vertical silkscreen arrow (open, thin) | shaft x ≈ 1563, y 720–965; head at top, pointing up (image) | tail at bottom-right corner; row index increases **upward** |
| horizontal silkscreen arrow (open, thin) | shaft y ≈ 996, x 1235–1515; head at left, pointing left (image) | tail at bottom-right corner; column index increases **leftward** |
| filled silkscreen triangle | tip ≈ (1588, 928), base x ≈ 1630, y ≈ 900–958, pointing left | just outside the FX3 courtyard; tip points at the right edge near the pin-1 corner — consistent but not independently diagnostic |
| `CC` + small square mould marking | `CC` ≈ (1240, 662), square ≈ (1255, 663) | a mould/marking code, not at a corner; if it were read as a corner marker it would be ambiguous. **Not used.** |
| moulded corner chamfer | all four corners checked: (1174,623), (1542,623), (1174,989), (1542,989) | **no chamfer**; no cue |
| silkscreen labels 1…11 / A…L for the FX3 | **none found** adjacent to the FX3 | — |
| numbers `22 … 1` in a vertical column at x ≈ 1628–1690, y ≈ 200–1045 | immediately left of the Pango body (left edge x ≈ 1697) | **FPGA column labels, not FX3.** They must not be used for the FX3 ball map. |

**Verdict: all usable cues agree.** A1 is the **bottom-right** ball of the array, image position
≈ (1504.3, 952.6). Columns 1→11 run toward image **left**; rows A→L run toward image **top**.
The only thing that could be read as a conflict is the `CC` mould mark, but it is not a corner
marker and is not at a corner. The filled triangle is consistent but lies outside the footprint
courtyard, so I treat it as supporting, not primary, evidence.

---

## 4. 121-ball grid

**Mapping convention (explicit):**

* Origin **A1 = bottom-right** ball of the 11 × 11 array, at image (1504.31, 952.61).
* **Columns 1 → 11** advance toward image **left** (direction u = (−cos θ, −sin θ), θ = 0.20 deg).
* **Rows A → L** (skipping I) advance toward image **top** (v = (+sin θ, −cos θ)).
* Pitch = 0.8 mm × 36.636 px/mm = **29.28 px**.
* A1 is 1.00 mm (36.64 px) inside the measured right and bottom silhouette edges.
* Ball (column i, row r) = A1 + (i−1)·29.28·u + (r−1)·29.28·v.

Rows: A B C D E F G H J K L (no I).

### Rows A and B, columns 1–11 (full table)

| ball | x (px) | y (px) | 1σ x / y (px) | 1σ x / y (mm) |
|---|---|---|---|---|
| A1 | 1504.31 | 952.61 | 1.66 / 1.66 | 0.045 / 0.045 |
| A2 | 1475.00 | 952.51 | 1.74 / 1.66 | 0.047 / 0.045 |
| A3 | 1445.69 | 952.40 | 1.85 / 1.66 | 0.051 / 0.045 |
| A4 | 1416.38 | 952.30 | 2.00 / 1.66 | 0.055 / 0.045 |
| A5 | 1387.07 | 952.20 | 2.18 / 1.67 | 0.059 / 0.045 |
| A6 | 1357.77 | 952.10 | 2.37 / 1.67 | 0.065 / 0.046 |
| A7 | 1328.46 | 952.00 | 2.58 / 1.68 | 0.070 / 0.046 |
| A8 | 1299.15 | 951.89 | 2.80 / 1.68 | 0.076 / 0.046 |
| A9 | 1269.84 | 951.79 | 3.03 / 1.69 | 0.083 / 0.046 |
| A10 | 1240.53 | 951.69 | 3.27 / 1.70 | 0.089 / 0.046 |
| A11 | 1211.22 | 951.59 | 3.51 / 1.71 | 0.096 / 0.047 |
| B1 | 1504.41 | 923.30 | 1.66 / 1.74 | 0.045 / 0.047 |
| B2 | 1475.10 | 923.20 | 1.74 / 1.74 | 0.047 / 0.047 |
| B3 | 1445.79 | 923.10 | 1.85 / 1.74 | 0.051 / 0.047 |
| B4 | 1416.49 | 922.99 | 2.00 / 1.74 | 0.055 / 0.048 |
| B5 | 1387.18 | 922.89 | 2.18 / 1.74 | 0.059 / 0.048 |
| B6 | 1357.87 | 922.79 | 2.37 / 1.75 | 0.065 / 0.048 |
| B7 | 1328.56 | 922.69 | 2.58 / 1.76 | 0.070 / 0.048 |
| B8 | 1299.25 | 922.58 | 2.80 / 1.76 | 0.076 / 0.048 |
| B9 | 1269.94 | 922.48 | 3.03 / 1.77 | 0.083 / 0.048 |
| B10 | 1240.64 | 922.38 | 3.27 / 1.78 | 0.089 / 0.049 |
| B11 | 1211.33 | 922.28 | 3.51 / 1.79 | 0.096 / 0.049 |

### Requested balls

| ball | x (px) | y (px) | 1σ (mm) |
|---|---|---|---|
| **A3** | 1445.69 | 952.40 | 0.051 / 0.045 |
| **A4** | 1416.38 | 952.30 | 0.055 / 0.045 |
| **A5** | 1387.07 | 952.20 | 0.059 / 0.045 |
| **A6** | 1357.77 | 952.10 | 0.065 / 0.046 |
| **B3** | 1445.79 | 923.10 | 0.051 / 0.047 |
| **A9** | 1269.84 | 951.79 | 0.083 / 0.046 |
| **A10** | 1240.53 | 951.69 | 0.089 / 0.046 |

(Full table in `A_geom/geometry.json`.)

**Uncertainty method.** Monte-Carlo, 20 000 trials, sampling: each edge position N(measured, 1.2 px);
scale N(36.64, 0.35) px/mm; package rotation N(0.20 deg, 0.08 deg); 1.00 mm inset N(1.00 mm,
0.03 mm). Per-ball 1σ is 0.045 mm near A1 and grows to ≈0.10 mm at A11 because the scale
uncertainty integrates along the 8 mm array.

**Additional systematic (not in the 1σ above).** The edge-definition/threshold sensitivity
(§1) is ±0.5…1.3 px (±0.015…0.035 mm) per edge, and the scale-convention ambiguity
(silhouette = 10.00 mm vs silhouette = 10.20 mm with a 0.1 mm bevel) moves the balls
**inward by ≈0.1 mm at A1 and ≈0.2–0.25 mm at A11**, mostly along the row/column directions.
Combining the two, a defensible 1σ total is **±0.05 mm (A1) … ±0.15 mm (A11)** for random
errors, plus **up to ±0.25 mm systematic at the far corner** if the cross-check scale is right.
No sub-0.1-mm absolute claim across the whole array is supported by this photograph.

---

## 5. Internal consistency checks

| check | result | verdict |
|---|---|---|
| measured pitch 0.8 mm? | **cannot be measured** — balls are hidden under the package. The grid uses the datasheet 0.8 mm pitch; at s = 36.64 px/mm this is 29.28 px. | model, not measurement |
| array centre-to-centre 8.00 mm? | 10 × 29.28 = 292.8 px = 7.99 mm. The middle column (A6/F6) falls at x = 1357.8 vs package centre x = 1358.0 (0.2 px). | consistent (by construction) |
| ball-array corners 1.00 mm inside the body edges? | A1–right = 37.39 px = 1.021 mm; A1–bottom = 36.02 px = 0.983 mm; **A11–left = 37.00 px = 1.010 mm; L1–top = 36.14 px = 0.986 mm**. The last two are *not* forced by the A1 construction and are the meaningful check. | **yes, 0.98–1.02 mm** |
| body square? | 367.47 × 365.24 px = ratio 1.0061 | 0.6 % non-square, at the edge-bias level |
| ink dot vs predicted A1 | dot (1508.90, 955.42) − A1 (1504.31, 952.61) = (4.6, 2.8) px = 5.3 px (0.14 mm) toward package interior | plausible pin-1 dot placement; supports the 1 mm inset |

The 0.6 % width/height difference and the 0.26 deg non-parallelism of the left/right edges are
larger than the fit RMS; they are evidence of a small uncorrected perspective/lens or
lighting-dependent edge bias, and they justify the systematic budget in §4.

---

## 6. Assumptions and what would falsify them

1. **Part and body size.** The device is a CYUSB3014-BZX in the 121-ball, 10.00 × 10.00 mm,
   0.8 mm-pitch BGA (marking; datasheet `cyusb301x_ds.txt`, consulted after measuring).
   *Falsified by:* a different package variant on the board (e.g. the 131-ball WLCSP), or a body
   that is not 10.00 mm.
2. **Edge definition.** The R−G zero crossing marks the package silhouette (outer bevel included).
   *Falsified by:* a specular/print rim extending outside the physical body. The 1.4 % offset from
   the cross-check objects is direct evidence that a ~0.1 mm/side bias of this kind **is present**;
   the ball table uses the measured silhouette and the bias is carried as a systematic.
3. **Straight, parallel edges / negligible perspective.** The fits show 0.2–0.3 deg tilts and a
   0.6 % squareness error. *Falsified by:* a lens/perspective model with > 1 % local scale change;
   the DDR3/x-check agreement across the field bounds this at ≈1–2 %.
4. **Orientation convention.** The datasheet ball map is Top View and pin 1 is the A1 corner
   marked by the dot; the marking upright frame is rotated 180 deg in the image.
   *Falsified by:* a Cypress package drawing that puts A1 or the pin-1 dot elsewhere (the package
   drawing figure itself could not be extracted from the text datasheet).
5. **Ball array model.** 11 × 11 balls, 0.8 mm pitch, 8.00 mm span, 1.00 mm edge inset (the
   datasheet table lists A9 = DP; the "no ball at A9" note belongs to the 131-ball WLCSP map).
   *Falsified by:* an actual ball map with a different depopulation or pitch.
6. **Cross-check dimensions.** Pango 23.0 × 23.0 mm (distributor attribute, official drawing not
   fetched); DDR3 8 × 14 mm (inferred, not confirmed from a datasheet); connector pitch 0.5 mm
   (assumed standard, and the connector may not be the 40-pin one).
   *Falsified by:* the Pango/PK02008 drawing giving a different body, the Micron package being
   e.g. 9 × 13 mm, or the connector being a different pitch family.
7. **No anisotropic resampling.** Supported by the DDR3 aspect check (1.7517 vs 1.7500).

---

## 7. What the photograph cannot tell us

* **The balls themselves.** They are underneath the package. Their diameter, pitch, coplanarity,
  missing balls (e.g. A9), and solder-joint quality are invisible; the 121-ball grid is a model
  anchored to the package outline, not an observation.
* **The exact pin-1 dot semantics.** A 0.44 mm mould dot in the A1 corner is consistent with a
  pin-1 mark, but the same feature can be a mould ejector mark; the package drawing would be
  needed to decide.
* **Absolute scale better than ~1.5 %.** Lens distortion, edge bevel and the lossy WebP limit any
  sub-0.1 mm absolute claim; array-corner positions carry up to ≈0.25 mm systematic.
* **Package thickness / standoff**, die orientation, underfill, and anything on the bottom side.
* **The part number beyond the printed marking.** CYUSB3011/12/13 share the same 121-ball package.
* **Whether this is the "40-pin board-to-board" connector.** The visible row has ≈30 contacts at
  0.50 mm pitch; the 40-pin connector may be J4 (`P5_connector_J4.webp`).

---

## 8. Reproducibility

Scripts and intermediate data (read-only w.r.t. the photograph):

* `A_geom/edge_lib.py` — loading, Gaussian smoothing, bilinear sampling.
* `A_geom/measure_edges.py` — edge zero-crossings, robust fits, threshold-sensitivity table.
* `A_geom/final_measure.py` — Pango/DDR3 fits, ink-dot centroid.
* `A_geom/build_geometry.py` — ball-grid construction and Monte-Carlo (writes `geometry.json`).
* `A_geom/make_overlay.py` — overlay figure.
* Figure: `hardware/analysis/reopen/figs/A_geometry_overlay.png` (1360 × 1150 px, 2× crop of the
  FX3 region with the fitted body edges, the A/B rows of the ball grid, A1, the ink dot, both
  silkscreen arrows, the filled triangle, the `CC` mark and the FPGA label column marked
  "NOT FX3").
