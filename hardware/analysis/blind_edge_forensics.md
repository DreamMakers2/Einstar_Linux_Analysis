# Blind edge forensics — strip below CYUSB3014-BZX

Target: `hardware/photos/P1_top_side.webp` (2341×1199, RGB, 176 kB WebP).
Region of interest: x 1150–1600, y 985–1160 (board edge at y ≈ 1156).
No prior conclusion was assumed; every number below is measured from the file.

Method: luminance `L = 0.299R+0.587G+0.114B`; thin-ridge detector
`D = L − boxblur(21×21)`, sub-pixel peak by 3-point parabola, per-row and
per-column scans, plus a shear/Radon projection for 45° structure.
Crops (all NEAREST except where noted) are in `hardware/crops/blind_*.png`:

| crop | shows |
|---|---|
| `blind_full_half.png` | whole board, orientation |
| `blind_pkg_x1120-1620_y580-1040_nearest2x.png` | package + courtyard silk |
| `blind_roi_x1150-1600_y940-1170_nearest3x.png` | the strip |
| `blind_raw_stretch_y1036-1160_5x.png` | tone-stretched strip (thin copper visible) |
| `blind_hp_x1180-1520_y960-1140_5x.png`, `blind_hp_x1150-1600_y960-1140_3x.png` | high-pass, `L − boxblur(21)` |
| `blind_diag_zoom_x1270-1400_y1020-1100_10x.png` | the two 45° lines |
| `blind_diagtop_x1280-1420_y1010-1060_12x.png` | upper termination at silk S1 |
| `blind_diagbot_x1270-1360_y1055-1110_12x.png` | lower 45°→horizontal bends |
| `blind_c5_x1140-1360_y1034-1105_8x.png` | C5/C3/C4 with the three vias |
| `blind_c5end_x1275-1350_y1028-1062_18x.png` | C5 bend at (1301,1041) |
| `blind_bottom_x1100-1620_y1075-1199_4x.png` | pour, pour chamfer, board edge |
| `blind_ctrl_copper_and_silk_x1080-1300_y430-620_4x.png` | **control**: copper 45° pair + silk strokes |
| `blind_ctrl_traces_x1600-1760_y180-360_5x.png` | control: faint traces beside silk text |

---

## 1. Calibrated classifier (measured, not assumed)

### Control A — known silkscreen (`x 1129–1142`, `y 476–512`, left of package)

| quantity | measured |
|---|---|
| plateau luminance | **185.3 – 199.1** |
| background | 52.4 – 58.0 |
| contrast Δ | **+131 … +141** |
| FWHM | **8.9 – 11.0 px** |
| saturation | flat-topped, no sub-peak structure |
| ends | rounded / blunt (1-D column profile holds ≈190 for 30 px then falls 181→55 in 5 px) |

### Control B — known copper traces (`x 1080–1260`, `y 505–565`, two 45° traces)

| quantity | measured |
|---|---|
| peak luminance | **94.0 – 107.1** |
| background | 55.9 – 60.2 |
| contrast Δ | **+37 … +49** |
| FWHM (horizontal cut) | 6.2 – 7.8 px → **4.4–5.5 px perpendicular** |
| shape | single smooth Gaussian-like peak, never saturating |
| routing | straight 45° runs that **bend into horizontal runs** (same topology as the ROI features) |

### Derived thresholds

* **SILKSCREEN** — peak L ≥ 180 (Δ ≥ +120), FWHM ≥ 8 px, flat top, rounded ends.
* **COPPER** — peak L 85–120 (Δ +28 … +55), FWHM 5–8 px, smooth single peak.
* **COPPER POUR** — broad uniform plateau 100–114 (Δ +40 … +55) with sharp straight
  boundary and 45° chamfered corners; *no ridge* at the boundary itself
  (verified at control `x 880–1060, y 180–400`: pour edge is a clean step 107→60).
* **COMPRESSION RINGING / BLOOM** — Δ ≤ +10, must be within ≤6 px of a bright edge,
  and must reproduce along the whole edge.

---

## 2. Feature inventory

Distances in source px. “Δ” = peak minus local background.

### Class S — silkscreen (bright, wide, flat-topped)

| id | description | coords | width | peak L | continuity |
|---|---|---|---|---|---|
| S1 | package courtyard **bottom line** | y 1025–1036, x 1150→1584 | 10–11 | 180–186 | continuous |
| S2 | pin-1 **arrow shaft** | y 1000–1012, x 1225→1496 | 7–8 | ~185 | continuous |
| S3 | arrow **head** (chevron) | x 1215–1250, y 995–1025 | — | ~185 | continuous |
| S4 | bracket **verticals** | x 1183–1190 and x 1493–1505, y 985→1030 | 7–9 | ~185 | continuous |

### Class C — copper under mask (thin, dim, smooth)

| id | description | coords | perpendicular width | peak L (Δ) | continuity |
|---|---|---|---|---|---|
| **C5** | horizontal trace | y 1041–1042, x ≤700 → **1301**, then 45° up at (1301,1041) | 5.5 | 103–118 (+45…+52 at x≈900–1000; +30 at x 1240–1290) | continuous for >600 px |
| **C3** | horizontal trace | y 1085–1086, x ≤700 → **1301**, then 45° up | 6.0 | 99–114 (+41…+48; +33 at x 1280) | continuous >600 px |
| **C4** | horizontal trace | y 1096, x ≤700 → **1306**, then 45° up | 6.0 | 95–106 (+28…+42) | continuous >600 px |
| **D1** | 45° diagonal, `x+y = 2386` | (1301,1085) → (1344,1042) | 5.6 | 91.9 (+32) | continuous 47 rows; stops at y 1042 |
| **D2** | 45° diagonal, `x+y = 2402` | (1306,1096) → (1356,1044) | 5.6 | 91.3 (+31) | continuous 52 rows; stops at y 1044 |

### Class P — copper pour / boundaries (steps, not ridges)

| id | description | coords | L | note |
|---|---|---|---|---|
| P1 | pour top edge, left | y 1114–1116, x <1300 | 114 vs 60 | step, not ridge |
| P2 | pour top edge, right | y 1089–1090, x >1450 | 107 vs 58 | step |
| P3 | pour edge **45° chamfer** | (1312,1117) → (1339,1090), `x+y = 2429` | — | *a diagonal, but a boundary* |
| P4 | pour bottom edge | y 1133–1135 | 105 → 85 | step |
| P5 | board edge | y 1155–1159 | 85 → 15 | end of board |

### Class R — rejected as compression artefact

| id | description | evidence |
|---|---|---|
| R1 | the +7 DN shelf at y 1039–1045 under S1 (x 1150–1300) | This is **not** a separate ridge: it is the merged wing of C5 (see §5). At x = 1320 / 1520 / 1560 the same bright silk edge produces **flat 54.5–57.2 with no bump at all**. |

---

## 3. The diagonal features

### Fit (least squares on sub-pixel ridge peaks, rows 1044–1091)

| | D1 | D2 |
|---|---|---|
| model | `x = 2385.8 − y` | `x = 2401.9 − y` |
| slope dx/dy | **−0.995** | **−0.997** |
| i.e. angle | 45.1° / 44.9° (from vertical) | 45.0° |
| peak L | 91.9 | 91.3 |
| FWHM (horizontal) | 7.9 px | 7.9 px |
| width ⟂ | **5.6 px** | **5.6 px** |
| run | (1301,1085) → (1344,1042) | (1306,1096) → (1356,1044) |

**Perpendicular spacing: Δc = 16.1 px ÷ √2 = 11.4 px.**

### Independent orientation test (Radon, rows 1036–1135, residual after box-blur)

| angle from vertical | significance |
|---|---|
| 30° | 13.8 |
| 35° | 13.8 |
| 40° | 16.8 |
| **44°** | **20.6** |
| **45°** | **20.7** |
| **46°** | **21.0** ← peak |
| 50° | 17.3 |
| 60° | 16.8 |

A single sharp maximum at 45–46°, i.e. true 45° routing, not a projection artefact.

### Count: **exactly 2 genuine 45° copper traces**

A shear-projection search over the whole strip (both diagonals, silk masked,
minimum run 30 rows, threshold lowered to Δ +4.5) returns exactly:

1. `c = 2386` amp 17.9 — **D1 (copper)**
2. `c = 2402` amp 18.6 — **D2 (copper)**
3. `c = 2429` amp 14.9 — **the pour-edge chamfer P3 (not a trace)**
4. `c = 2434` amp 8.5 — pour edge again (shoulder)

**Zero down-right 45° segments** (`x−y = c`) exist anywhere in the strip.
So the answer to “2, 4 or 6?” is **2 copper diagonals** (+1 non-copper 45° pour chamfer).

---

## 4. Where the diagonals go

Both ends were followed with the per-row ridge tracker.

* **Lower-left end — bends 45° → horizontal.**
  * D1 meets C3 at **(1301, 1085)**; C3 then runs left at y ≈ 1085–1086.
  * D2 meets C4 at **(1306, 1096)**; C4 then runs left at y ≈ 1096.
  * Geometry is self-consistent: horizontal pitch 11–12 px, diagonal perpendicular
    pitch 11.4 px — the pair stays parallel through the bend.
* **Upper-right end — lost under the silkscreen.**
  * D1 last unambiguous ridge **(1344, 1042)**; D2 **(1356, 1044)**.
  * S1 (courtyard silk) bottom edge is y 1036. The traces stop **6–8 px short** of it.
  * **They do not reappear above S1.** Rows y 1013–1022 are a clean dark gap
    (L 54–58) and contain no ridge at the predicted positions x 1362–1373
    (D1) / x 1378–1389 (D2); the per-row peak scan returns only the vertical
    silk bar at x ≈ 1500 there.
  * C5 likewise bends up at **(1301, 1041)** and is swallowed by S1 after ~5 px.

**Not determined:** whether the three traces end at vias/pads hidden by S1, or jog
horizontally under S1. Nothing below y 1013 supports either option, and S1 is
opaque, so the connection to the BGA balls cannot be established from this image.

---

## 5. The horizontal lines lower in the strip

Five distinct classes, separated by *measured* criteria, not by assumption:

| y | peak L | Δ | FWHM | extent | verdict |
|---|---|---|---|---|---|
| 1041–1042 | 103–118 | **+45…+52** | 5.5 | x ≤700 → 1301 | **copper trace C5** |
| 1085–1086 | 99–114 | **+41…+48** | 6.0 | x ≤700 → 1301 | **copper trace C3** |
| 1096 | 95–106 | **+28…+42** | 6.0 | x ≤700 → 1306 | **copper trace C4** |
| 1114–1116 (left) / 1089 (right) | 114 / 107 | **+45…+55** | step, no ridge | full width | **copper-pour boundary P1/P2** |
| 1133–1135 | 105→85 | step | — | full width | **pour bottom edge P4** |
| 1155–1159 | 85→15 | step | — | full width | **board outline** |

Discriminating criteria actually used:
1. **Ridge vs step.** A trace has a positive lobe *and* returns to the same
   background on both sides. The pour edges rise and never come back down
   (they are a level change). Measured at x = 900/1000/1100: y 1110–1133 goes
   66 → 114 and stays ≥ 98 until y 1133. Not a ridge.
2. **Amplitude.** Copper ridges are Δ +28…+52. The only near-silk feature with
   Δ ≤ +10 (R1) fails.
3. **Width.** 5.5–6.0 px for traces; 10–11 px for silk; “infinite” for pour.
4. **Silk test.** Nothing at y 1035–1155 reaches L 180, so **none of these
   horizontal lines is silkscreen**.

**Connectivity:** C3 and C4 *are* the horizontal continuations of D1 and D2
(bend points above). C5 is a third, independent trace that bends up on its own.
None of the three touches the pour boundary or the board outline.

---

## 6. Controls (recap of §1)

* **Known copper** — `x 1080–1260, y 505–565` (no package): two 45° traces,
  peak L 94.0 / 107.1, Δ +37 / +49, FWHM 7.8 / 6.2 px, bending into horizontal
  exactly like the ROI pair.
* **Known silkscreen** — `x 1129–1142, y 476–512` (no package): peak L 185–199,
  Δ +131…+141, FWHM 8.9–11.0, rounded ends.

The ROI copper features (peak 91–92, Δ +31…+32, width 5.6 px) sit inside the
control copper envelope and far outside the silkscreen envelope
(Δ +131 minimum, i.e. 4× brighter).

---

## 7. Artefact check — could the diagonals be compression ringing?

Four independent tests, all negative:

1. **Geometry.** Ringing is parallel to the edge that causes it. The only bright
   edges here are horizontal (S1 at y 1025–1036, S2 at y 1000–1012). Ringing
   would therefore be horizontal. The features are at **45.0° ± 0.5°**.
2. **Direction selectivity.** A compression artefact is not directional: it would
   appear along both diagonals. A shear scan of the whole strip finds
   **0** down-right 45° segments and only the two up-right ones. Pure
   compression noise cannot be chiral.
3. **Absence at other silk edges (the decisive test).** The same S1 line is
   present at x = 1320, 1520, 1560 with equal or greater brightness
   (L 183.7 at x 1320; 181–186 at x 1520/1560). Vertical profiles there are
   **flat**: at x 1520, y 1037–1050 reads 65.3, 54.5, 54.7, 56.1, 55.9, 55.9,
   55.9, 56.9, 56.9, 56.9, 57.2, 56.8 — no bump whatsoever. The features near
   y 1042 exist only at x 1150–1300, i.e. they are **localised objects**, not
   edge-locked ringing.
4. **Amplitude and width.** The diagonals reach Δ **+32 DN** with FWHM 5.6 px,
   which is the same amplitude/width as the *bona fide* copper control traces
   (Δ +37…+49) and 4× weaker than silk. WebP ringing measured in this file
   (R1, the only candidate) is Δ ≤ +10 and hugs the edge within 6 px.

**Conclusion: the diagonals are not compression artefacts.**

---

## 8. Confidence per feature

| feature | type | confidence | basis |
|---|---|---|---|
| C3, C4 | copper traces | **high (0.95)** | width/amplitude match control copper; 45°→horizontal bend preserved through the corner; parallel pair with consistent pitch |
| C5 | copper trace | **high (0.92)** | same signature, >600 px continuous, independent of the pair; only weakness is its disappearance under S1 |
| D1, D2 | copper traces | **high (0.95)** | slope −1.00, Δ +32, 5.6 px ⟂, terminate at silk, no down-right counterpart |
| S1–S4 | silkscreen | **certain (>0.99)** | peak L 180–186 (Δ +130), FWHM 10–11, flat top, forms the courtyard bracket + pin-1 arrow |
| P1–P4 | copper pour + edges | **high (0.90)** | uniform 105–114 plateau, sharp step boundaries, 45° chamfers, vias stitched into it |
| R1 | compression/merged wing | **medium (0.75)** | Δ ≤ +10; absent at identical silk edges elsewhere |
| Board outline y 1155–1159 | board edge | **high (0.95)** | monotone drop to L 12–15, persists across x 1100–2300 |

## 9. What could not be determined

* Where C3/C4/C5 connect at their **upper** ends — hidden by the courtyard
  silkscreen S1. No vias are visible in the clear gap y 1013–1022.
* Whether C3/C4 are an **electrical differential pair** or two unrelated
  signals. The geometry (equal width, constant 11.4 px pitch, common bend,
  common routing) is consistent with a coupled pair, but nothing in the image
  proves it.
* The exact **layer** of any trace (only the top layer is visible).
* Absolute **mm** calibration: everything above uses 38.4 px/mm derived from the
  stated 10×10 mm package (384 px). If that dimension is a nominal rather than a
  measured body size, all mm values scale accordingly; px values do not change.

## 10. Metric summary (at 38.4 px/mm)

* trace width 5.6 px ⟂ = **0.146 mm** (≈ 6 mil)
* pair pitch 11.4 px = **0.297 mm**
* edge-to-edge gap 5.8 px = **0.151 mm**
* i.e. a 0.15 mm / 0.15 mm design rule — an ordinary high-speed routing geometry.
