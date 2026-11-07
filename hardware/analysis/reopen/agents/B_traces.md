# B — Blind copper-trace extraction, `P1_top_side.webp`

**Scope.** Read-only optical extraction of top-side copper in the strip below the CYUSB3014
(FX3) package and westwards to the 40-pin connector. No repository reports were read before
this analysis. All numbers below were produced by the scripts in
`hardware/analysis/reopen/agents/B_work/` (`lib.py`, `linefit.py`, `masks.py`, `along.py`,
`hough2.py`, `figure.py`, …), run against
`hardware/photos/P1_top_side.webp` (2341×1199, RGB, single frame, SHA-256
`0deceaa7…3048f11`, 176 196 bytes = **0.50 bit/pixel**).

**Figure.** `hardware/analysis/reopen/figs/B_trace_map.png` — two panels (x 240–1000 and
x 1000–1720, y 930–1170), every track numbered and colour-coded, with grid in source pixels.

---

## 0. Answers to the three headline questions

| # | Question | Answer | Confidence |
|---|----------|--------|-----------|
| 1 | Conductors crossing the FX3 lower edge, x 1300–1460 | **4** — at x = 1335.3, 1351.0, 1395.3, 1410.3 (y = 992). Two pairs: 1335/1351 (Δ15.7 px) and 1395/1410 (Δ15.0 px), pairs 44.3 px apart. | high (RMS 0.75 px across a 20 px silk-occluded gap) |
| 2 | Two-terminal part at x 376–470, y 988–1084 | **SHUNT across the trace pair T07/T08 (y 1029.5 / 1040.5), not in series.** Pad axis is vertical, i.e. perpendicular to both traces; both traces are continuous on *both* sides of the part, so neither is interrupted by it. | medium — pad-to-trace gap is 0.5–1.5 px, at the PSF limit |
| 3 | Where the horizontal runs go at their left ends | They turn 45° **north** at x ≈ 265–300 into a five-trace vertical bundle at x = 282.8, 293.8, 323.8, 334.9, 358.7 which runs north past y = 880 alongside the *left* edge of the connector. They pass **below** the connector (connector body ends near y ≈ 880), they do not enter its body. | high for the bundle, medium for the connector attachment |

---

## 1. Imaging characterisation

### 1.1 Scale

FX3 mould-compound body measured from the purple colour class:
x 1183–1540, y 628–986 → **358 × 359 px**. The CYUSB3014 body is 10 × 10 mm, so

> **35.8 px/mm** (1 px = 27.9 µm). 0.8 mm ball pitch = 28.6 px.

(The task brief assumed ≈37 px/mm; the difference is 3 %, so all mm figures below carry a
~3 % systematic uncertainty.)

### 1.2 Point-spread function / edge width

Erf-fit (`psf.py`) of `a + b·Φ((t−t0)/σ)` to sub-pixel-aligned average edge profiles, on the
sharpest available high-contrast steps (silkscreen lines at y≈1005 and y≈1029, contrast
≈110 L-units):

| edge | σ (px) | 10–90 % width (px) |
|------|--------|--------------------|
| silk A rising | 1.68 | 4.31 |
| silk A falling | 1.36 | 3.49 |
| silk B rising | 1.80 | 4.61 |
| silk B falling | 1.42 | 3.64 |

> **PSF σ = 1.55 ± 0.2 px (0.043 mm); FWHM 3.6 px (0.10 mm); 10–90 % edge width 4.0 px.**

Consequences, used throughout: a 0.10 mm (3.6 px) trace keeps ≈77 % of its intrinsic
contrast and appears ~5.2 px wide; a 0.05 mm trace keeps ≈45 %. **All fitted widths are
deconvolved box widths** (model `box(w) ⊛ gauss(σ=1.55)`, 4-parameter local least squares),
not raw FWHM.

### 1.3 Noise floor and the lossy-WebP limits

The image is **quantisation-limited, not sensor-noise-limited**. In the 8 % flattest pixels
(|∇L| < 0.035) the robust σ of the high-pass residual is **0.001 L** and >50 % of pixels are
*exactly* equal to their neighbours: libwebp has flattened the flat areas. RMS residual there
is 0.08 L. Measured WebP fingerprints:

* **8×8 blocking**: mean |∂L/∂x| at multiples of 8 px is **1.27×** the interior value
  (R 1.29, G 1.28, B 1.32).
* **4:2:0 chroma**: intra-2×2 std of L = 1.78 vs R 2.09 / G 1.74 / B 1.91 — chroma is
  up-sampled from half resolution, so **colour edges are twice as soft as luminance edges**.
  Colour must not be used for sub-pixel work, only for classifying whole regions.
* Re-encoding the decoded image at q = 60/70/75/80/85/90 gives PSNR 34.4/36.2/37.1/38.0/
  39.1/41.2 dB against the decoded original, i.e. the file's own quantisation error is of the
  same order as a q≈75–80 encode: **±3–5 L-units of low-frequency error, plus ringing within
  ~4 px of strong edges.**

**Sub-pixel claim limit.** Centroid positions are reproducible to **±0.3 px** on strong
ridges (SNR > 10) and **±0.8 px** on weak ones; *widths* to ±1.0–1.5 px; *contrast* to ±15 %.
Anything narrower than ~2 px (0.056 mm) or fainter than 3 L-units cannot be separated from
compression texture. Two conductors closer than ~6 px (0.17 mm) are not resolvable as
separate objects — this matters for the pair separations reported below (10.7 px = 0.30 mm,
safely above the limit).

### 1.4 Colour / luminance classes (calibrated thresholds)

k-means (k = 10) over the ROI x 1100–1700, y 940–1160, plus targeted patch statistics.
The scene has a strong blue cast: "white" silkscreen is lavender and solder is salmon.

| class | RGB (mean) | L | H (°) | S | V | threshold used |
|-------|-----------|---|---|---|---|----------------|
| copper **pour** (mask over copper, large area) | (58,127,134) | 112.7 | 185.7 | 0.57 | 0.53 | copper test **and** L ≥ 105 in a 9×9 neighbourhood |
| copper **trace** (thin, peak value) | (71,107,124) | 100.6 | 199.2 | 0.43 | 0.49 | L ≥ 92 ∧ S ≥ 0.28 ∧ 165 ≤ H ≤ 215 |
| **bare soldermask** (no copper) | (35,69,93) | 63.1 | 204.7 | 0.63 | 0.36 | fails copper test (L < 92 or S < 0.28) |
| darker mask in shadow under package | (34,61,83) | 56.8 | 207.6 | 0.59 | 0.33 | as above |
| **silkscreen** | (186,168,238) | 177.0 | 255.4 | 0.29 | 0.93 | L ≥ 138 ∧ S ≤ 0.36 ∧ 235 ≤ H ≤ 275 |
| **solder / bare pad** | (240,178,155) → pad core (176,·,·) H 311 | 160–190 | 311–346 | 0.22–0.35 | 0.65–0.94 | H ≥ 330 ∨ H ≤ 40, V ≥ 0.50, L ≥ 100 |
| **IC mould compound** | (57,42,67) | 47.3 | 276.4 | 0.36 | 0.26 | body mask, fixed bbox |

Note the trap: pad cores (H ≈ 311, S ≈ 0.22) and silkscreen (H ≈ 255, S ≈ 0.29) have almost
the same luminance (176 vs 177) — **luminance alone cannot separate pads from silkscreen;
hue is required.** All mask logic in this work uses hue, and masks are dilated by 2 px so
that the 4 px-wide edge ramps of silk/pads cannot manufacture false ridges.

Copper-to-mask contrast is **A ≈ 50–60 L-units** where mask is unshadowed, and
**A ≈ 30–40 L-units** inside the package shadow (y 986–1000). All amplitudes below are
fitted peak amplitudes *above the local background*, so they are comparable across the strip.

---

## 2. Method (reproducible)

1. **Decode** with PIL (`RGB`, no EXIF transform present), luma = Rec.709.
2. **Masks**: silk, body, pad/solder as in §1.4, each dilated 2 px; union = exclusion set.
3. **Ridge response**: Hessian of a Gaussian-smoothed luma (σ = 1.1 and 2.0 px); score =
   −λ₁ (most-negative eigenvalue), orientation = eigenvector of λ₁; multi-scale max.
   Non-maximum suppression along the across-ridge direction at ±0.8 px.
4. **Line fitting**: every scan line is fitted with
   `L(t) = b₀ + b₁·t + A·[Φ((t−c+w/2)/σ) − Φ((t−c−w/2)/σ)]`, σ = 1.55 px, grid search over
   `w ∈ [0.8, 9] px` (0.2 px) and `c ∈ c₀±1.5 px` (0.1 px), linear least squares for
   `A, b₀, b₁`. Peak accepted if A > 2.5 L and A/rms > 2.
5. **Counting**: for a family of parallel conductors, a slope–intercept Hough
   (`x = s·(y−y_ref) + b`, s ∈ [−1.3, −0.7] step 0.02; or `y = const` for horizontals) over
   the NMS ridge pixels, excluding masks. Peak positions are then refined by the §2.4 fit.
6. **Cross-checks**: every count is measured in ≥ 2 independent bands and the extracted lines
   are tested for common slope and zero residual across the occluding gaps.

---

## 3. Track table

Coordinates are source pixels; `y=992` is the package lower edge line requested.
`w` = deconvolved width; `A` = fitted peak amplitude in L-units; positions ±1 px unless noted.
Classification: all are **top-layer traces under green mask** unless stated.

| id | class | measured path (x,y) | length | w (px / mm) | A (L) | continuity | conf. |
|----|-------|---------------------|--------|-------------|-------|------------|-------|
| T01 | trace, 45° | (1335.3,992) → (1287,1042) | 71 px | 4.4 / 0.123 | 27–36 | continuous 992–1042, then fades into mask texture | med |
| T02 | trace, 45° | (1351.0,992) → (1303,1042) | 71 px | 4.6 / 0.128 | 27–40 | continuous 992–1042, then fades | med |
| T03 | trace, 45° | (1395.3,992) → (1347,1040) → (1302,1085) → west | 148 px | 3.5 / 0.098 | 33–36 | continuous 992–1090 (occluded by silk at y 1001–1010 and 1025–1034) | high |
| T04 | trace, 45° | (1410.3,992) → (1362,1040) → (1316,1090) → east | 150 px | 3.9 / 0.109 | 33–36 | continuous 992–1092 | high |
| T05 | trace, horizontal | (370,977.4) → (1150,977.4) | 780 px | 3.9 / 0.109 | 45–99 | unbroken where not silk-masked; lost at the package shadow x≈1170 | high |
| T06 | trace, horizontal | (470,988.1) → (1150,988.4) | 680 px | 4.0 / 0.112 | 37–91 | unbroken; brightness peaks at x 700–1050; lost at x≈1170 | high |
| T07 | trace, horizontal | (390,1029.5) → (1110,1029.5) | 720 px | 4.0 / 0.112 | 37–91 | unbroken; passes through the part's pad gap | high |
| T08 | trace, horizontal | (310,1040.5) → (1290,1040.5) | 980 px | 4.2 / 0.117 | 38–74 | unbroken across the whole strip | high |
| T09 | trace, horizontal | (350,1084.6) → (1295,1084.6) | 945 px | 3.8 / 0.106 | 43–110 | unbroken; joins T03 at its east end | high |
| T10 | trace, horizontal | (370,1095.3) → (1290,1095.3) | 920 px | 3.9 / 0.109 | 38–89 | unbroken | high |
| T11 | trace, horizontal | (1330,1089.8) → (1560,1089.8) | 230 px | 4.4 / 0.123 | 12–26 | continuous, weaker (sits on pour edge) | med |
| T12 | trace, vertical | x = 282.8, y 880 → 1010 | 130 px | n/m | 58–76 | continuous in scanned range | med |
| T13 | trace, vertical | x = 293.8, y 880 → 1010 | 130 px | n/m | 58–108 | continuous | med |
| T14 | trace, vertical | x = 323.8, y 880 → 1000 | 120 px | n/m | 57–84 | continuous | med |
| T15 | trace, vertical | x = 334.9, y 880 → 1000 | 120 px | n/m | 42–155 | continuous | med |
| T16 | trace, vertical | x = 358.7, y 880 → 980 | 100 px | n/m | 49–81 | continuous | med |
| P1 | solder pad | x 394–452, y 1005–1028 | 59×24 px = 1.65×0.67 mm | — | L 176, H 311 | — | high |
| P2 | solder pad | x 394–452, y 1040–1064 | 59×25 px | — | L 176, H 311 | — | high |
| V1 | via (ring signature) | centre (1362,1090), outer Ø ≈ 26 px = 0.73 mm | — | — | ring L 52–58, core L 85–90 vs pour 110–120 | on T11 | med |
| V2 | via (ring signature) | centre (1414,1090), same geometry | — | — | as V1 | on T11 | med |

Masked/occluded spans (silkscreen drawn over copper, so the copper is *not visible* there but
is inferred to continue): **y 1001–1010 and y 1025–1034** for every diagonal crossing them.
The package body itself (y < 986) hides everything above the edge.

---

## 4. Question 1 — conductors crossing the FX3 lower edge, x 1300–1460

### 4.1 Result

**Four** distinct conductors cross y = 992 in x ∈ [1300, 1460]:

| # | x at y = 992 | x at y = 1016 | x at y = 1040 | slope dx/dy | pair | Δ within pair | Δ between pairs |
|---|--------------|---------------|---------------|-------------|------|---------------|-----------------|
| 1 | 1335.3 | 1310.8 | 1287 (fit) | −1.00 | **P1** | 15.7 px = 0.44 mm | — |
| 2 | 1351.0 | 1326.8 | 1303 (fit) | −1.00 | **P1** | | 44.3 px = 1.24 mm (2↔3) |
| 3 | 1395.3 | 1372.0 | 1347 | −1.00 | **P2** | 15.0 px = 0.42 mm | — |
| 4 | 1410.3 | 1386.8 | 1362 | −1.00 | **P2** | | — |

Evidence chain:

* **Band C**, y 991–1000 (the only strip between the body edge and the first silk line):
  Hough peaks at x@y996 = 1332.0, 1347.2, 1391.6, 1407.6.
* **Band A**, y 1012–1024 (between the two silk lines): peaks at 1310.8, 1326.8, 1372.0, 1386.8.
* **Band B**, y 1035–1085: strong peaks at x@y1060 = 1326.8 and 1342.8, plus fainter ones at
  1268.8 and 1282.8 (the continuation of conductors 1 and 2, 10–15× weaker).
* **Common-slope test**: forcing slope −1.00, the predicted positions of the four lines in
  band A from the band-C fit have residuals **+1.2, +0.4, −0.4, +0.8 px (RMS 0.75 px)** across
  a 20 px gap that is fully covered by silkscreen. That is the strongest single piece of
  evidence that there are four *continuous* lines and not eight unrelated stubs.
* All four reach y = 1085–1090 and either turn west (3) or east (4); 1 and 2 fade below
  y ≈ 1045 into the mask/clearance texture (they may terminate on vias hidden by the
  poor contrast there — see §7).

### 4.2 What each is — and the alternatives rejected

| interpretation | verdict | evidence |
|---|---|---|
| **trace (top-layer, 0.10–0.13 mm)** | **ACCEPTED** | L 80–92, H 210–216, S 0.41–0.47 = copper-under-mask signature; 3.5–4.6 px deconvolved width; dark clearance on both flanks; run 90–150 px at a constant −1.00 slope. |
| solder-ball fillet | rejected | Balls are *under* the body (body edge y = 986, ball field inside it). These features start 6 px below the edge and continue 100 px away at 45°, with no 0.8 mm (28.6 px) periodicity. |
| silkscreen | rejected | Silk is L > 138, S < 0.34, H 247–275; these are L ≤ 92, H 210–216. They are also *hidden* by the silk lines (visible only in the gaps), i.e. they lie under the ink, not in it. |
| copper pour edge | rejected | No large-area copper there; the pour boundary is 100 px lower (y ≈ 1095–1112) and is a shallow, wide step, not a 4 px ridge with two dark flanks. |
| package bevel / shadow | rejected | The bevel produces a *horizontal* highlight at y 987–990; these features are at 45° and cross that line, and their amplitude is 30–40 L against a 47–52 L shadow. |
| same trace seen twice (down-and-back) | rejected | All four are parallel, monotone, and separated by 15–16 px over 90 px of travel. |

### 4.3 Do they form pairs?

Yes, in the spacing sense: 15.7 / 44.3 / 15.0 px. Within-pair spacing 15–16 px
(0.42–0.45 mm); between-pair 44 px (1.24 mm), i.e. **2.8× the within-pair spacing**. That
two-short-one-long-two-short rhythm is the classic edge-coupled pair pattern. I make **no
claim about what the pairs carry** (that was excluded from scope).

---

## 5. Question 2 — the two-terminal part at x 376–470, y 988–1084

### 5.1 What is physically there (measured)

* Silkscreen frame: rounded rectangle, outer bbox **x 376–470, y 988–1084**, line width ≈ 10 px
  (0.28 mm), L 175, H 247 → definitely silkscreen.
* Two solder pads inside, both **x 394–452 (59 px = 1.65 mm) wide**:
  * **P1** y 1005–1028 (24 px = 0.67 mm), centroid (423, 1016)
  * **P2** y 1040–1064 (25 px), centroid (421, 1053)
* Pad pitch (centre-to-centre) **37 px = 1.03 mm**; inter-pad gap **12 px = 0.34 mm**.
* Pad colour L 176 / H 311 / S 0.22; the small warm cores (H 346, L 160) are the solder
  fillets. There is no visible moulded body between the pads: the 12 px gap has the
  soldermask hue (H 205–210, S 0.6), i.e. the part is either not populated or its body is
  smaller than the gap.

### 5.2 Verdict: **SHUNT across the T07/T08 pair. Not in series.**

Pixel evidence:

1. **Both adjacent traces are continuous past the part on both sides.**
   T07 (y 1029.5) is measured at x = 390, 470, 490, 510, … 1110 — i.e. immediately west and
   east of the pad column. T08 (y 1040.5) is measured at x = 310, 350, 370, 430, 470, 490, …
   1290. A series element would produce a **break** in one trace at the pads; there is none.
2. **The pad axis is perpendicular to the traces.** P1/P2 are stacked vertically (pad normal
   along y) while both traces run along x. A series part must have its two terminals in line
   with the trace it interrupts (pads side-by-side along x). That is not the geometry here.
3. **The pads' inner edges coincide with the two traces.** P1's lower edge is y 1028 and T07's
   ridge centre is y 1029.5 → **1.5 px (0.042 mm)**. P2's upper edge is y 1040 and T08's centre
   is y 1040.5 → **0.5 px (0.014 mm)**. Both are at or below the PSF scale (σ = 1.55 px), so
   each pad sits *on* the adjacent trace. Since T07 and T08 are separate conductors 11 px
   apart, the part bridges them → shunt.
4. **No trace enters either pad from any other side.** Left, right, top and bottom of both
   pads are separated from the silk frame by ≥ 5 px of dark mask (L 55–60). The only
   bright/copper pixels touching a pad are the two horizontal traces in the gap.

### 5.3 Confidence and the honest alternative

**Medium.** The discriminating quantity is a 0.5–1.5 px gap, which is *below* the reliable
separation limit (σ = 1.55 px, and 4:2:0 chroma doubles the colour blur). The alternative
that cannot be excluded optically is: the pads connect **through vias under the pads** to
inner or bottom copper, and the two horizontal traces merely pass through the gap (a normal
thing for traces to do under a chip body). In that case the part would still be *across* the
pair in layout terms but the top-layer evidence would be silent.

What *can* be stated without qualification: **the part is not in series with T07, T08, or any
other visible trace**, because neither trace is interrupted and no other trace touches a pad.

A second two-terminal footprint (same silk+two-pad signature) exists at x 640–700 / 850–900,
y 730–950 — outside this strip and not analysed here.

---

## 6. Question 3 — the horizontal runs westwards (x 250–1300)

### 6.1 Inventory

Seven persistent horizontal conductors, of which six form three pairs with a **constant
10.7–11.0 px (0.30 mm) centre spacing**:

| pair | trace | y (west end → east end) | span (x) | w (px) | A (L) |
|------|-------|--------------------------|----------|--------|-------|
| **H-A** | T05 | 977.4 → 977.6 | 370 → 1110 | 3.9 | 45–99 |
| | T06 | 988.1 → 988.7 | 470 → 1110 | 4.0 | 37–91 |
| **H-B** | T07 | 1029.5 → 1030.5 | 390 → 1110 | 4.0 | 37–91 |
| | T08 | 1040.5 → 1041.2 | 310 → 1290 | 4.2 | 38–74 |
| **H-C** | T09 | 1084.6 → 1085.3 | 350 → 1295 | 3.8 | 43–110 |
| | T10 | 1095.3 → 1096.1 | 370 → 1290 | 3.9 | 38–89 |
| (single) | (weak) | 1114.5 → 1116 | 270 → 1290 | 5–7 | 18–34 |

The +0.6 to +1.0 px drift over 700 px is the board's rotation relative to the sensor
(≈0.08°), not a bend. The 1114.5 line is only 18–34 L and 5–7 px wide; it is most likely the
**edge of the bottom copper pour** rather than a trace, and it is not counted as a conductor.

### 6.2 East ends

* T05/T06 persist to **x ≈ 1150** (measured at x = 1150: y 972.9 and 989.4) and are lost at
  x ≈ 1170–1190, i.e. at the package edge/shadow (body starts x 1183). Whether they terminate
  there (via) or continue under the body cannot be resolved; they are certainly not visible
  east of x 1190.
* T07 is last seen at x ≈ 1110 (present at 1110, absent at 1130).
* T08 continues to x 1290; T09/T10 continue to x 1290–1295, where they meet the diagonal feet.
  **Inferred (not directly resolved):** T09 ↔ T03 and T04 → T11 → V1/V2 — the diagonals and
  the horizontals meet within 5 px of each other at (1302,1085) and (1316,1090), but the
  corners are short and partly under the pour edge.

### 6.3 West ends — where they actually go

Between **x 265 and 300** the whole bundle turns 45° north and becomes a **five-trace vertical
bundle** at

> x = **282.8, 293.8, 323.8, 334.9, 358.7** (again two close pairs: 282.8/293.8 Δ11.0 px,
> 323.8/334.9 Δ11.1 px, plus a singleton at 358.7).

These are present at every sampled row from y = 990 up to y = 880 with constant x (peak
amplitudes 42–108 L), i.e. they run **north along the left side of the connector**.

* **They do not enter the 40-pin connector body.** The connector occupies x 365–595,
  y ≈ 230–880; the horizontal runs are at y 977–1095, i.e. **below** its bottom edge, and the
  vertical bundle is at x 283–359, i.e. **left** of its left edge (365).
* Row scans at y 940/960/970/980 show additional ridges at x 345.6, 358.7, 372.9, 382.0,
  375.9, 391.5 — features that move right as y decreases, consistent with the bundle fanning
  **up-right toward the connector's left pin row (x ≈ 370)** at y ≈ 880–940. I did not resolve
  individual pin attachments; treat that as an open item (§7).
* **Vias.** At least four via-ring signatures sit in the bend region — (342,950), (405,1010),
  (400,1075), (268,1085), each a dark annulus Ø 9–13 px with a brighter core — plus the two
  larger ones on T11 at (1362,1090) and (1414,1090) (Ø 26 px = 0.73 mm). At least one of the
  vertical traces appears to terminate on a via rather than continue; the assignment is not
  certain at this contrast.
* **Series components in the horizontal runs: none.** Between x 470 and x 1290 no pad pair,
  silk footprint or gap interrupts T05–T10. The only footprint in the strip is the part in
  §5, and the traces pass *through* it rather than being cut by it.

---

## 7. What cannot be determined optically (explicit limits)

1. **Anything under the package.** The body is opaque; the four escape traces are visible only
   from y ≥ 991 downward. Which balls they come from, and whether they connect to balls at all
   (they may simply pass under the package), is not determinable from this image.
2. **Pads vs traces at the sub-pixel level** (§5.3): the 0.5–1.5 px pad-to-trace gaps are
   smaller than the PSF. The shunt verdict is a geometric argument, not a resolved gap.
3. **Layer identity.** Nothing here distinguishes top-layer from an inner layer showing
   through; the analysis assumes, on the basis of the mask/copper colour signature, that all
   mapped conductors are top-layer.
4. **Via connectivity.** The ring signatures are clear, but whether a given trace *ends* on a
   via or passes it cannot be resolved where the trace fades (T01/T02 below y ≈ 1045, and the
   vertical bundle near y 950–1010).
5. **Exact left-end termination** of the vertical bundle (connector pin vs via vs inner layer).
6. **Trace widths** carry ±1.0–1.5 px (±0.03–0.04 mm) because they are deconvolution results
   with an assumed σ; absolute widths could be systematically 10–20 % low if the true PSF is
   wider than the silkscreen edges suggest.
7. **No electrical claim of any kind** is made — this is copper geometry only.

---

## 8. Files

| file | contents |
|------|----------|
| `hardware/analysis/reopen/figs/B_trace_map.png` | annotated two-panel map, tracks T01–T16, pads P1/P2, vias |
| `hardware/analysis/reopen/agents/B_traces.md` | this report |
| `hardware/analysis/reopen/agents/B_work/lib.py` | numpy/PIL imaging helpers (filters, peaks, HSV) |
| `…/B_work/charac.py, charac2.py, charac3.py, psf.py` | noise floor, PSF, WebP fingerprint |
| `…/B_work/linefit.py, masks.py, along.py, hough2.py, colscan.py` | sub-pixel line fitting, masks, slope–intercept Hough |
| `…/B_work/figure.py` | figure generator |
