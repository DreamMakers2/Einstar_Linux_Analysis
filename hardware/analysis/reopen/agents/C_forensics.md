# C — Image forensics: what the P1/P2/P3/P4 WebP files actually preserve

**Scope.** Read-only analysis of the four supplied photographs. No hardware access.
All measurements are reproducible from the scripts in
`hardware/analysis/reopen/agents/C_work/scripts/` (numpy/PIL only; no scipy/cv2) and the
raw intermediate data in `C_work/out/*.json`.

| file | size | dims | bits/px | format |
|---|---|---|---|---|
| `P1_top_side.webp` | 176 196 B | 2341×1199 | 0.502 | RIFF/WEBP, single `VP8 ` chunk |
| `P2_bottom_side.webp` | 272 028 B | 2283×1203 | 0.792 | same |
| `P3_bottom_side_2.webp` | 295 100 B | 2312×1180 | 0.865 | same |
| `P4_bottom_side_3.webp` | 289 166 B | 2260×1207 | 0.848 | same |

Figures: `figs/C_forensics_block_artefacts.png`, `figs/C_forensics_feature_stability.png`,
`figs/C_forensics_multi_image.png`.

---

## 1. Encoding facts (verified from the bitstream, not assumed)

Parsed with a hand-written RIFF + VP8 key-frame header parser (`vp8parse.py`); the parse
consumes exactly 137 header bits and reproduces the frame dimensions and start code, so
the field alignment is self-checking.

| property | P1 value | note |
|---|---|---|
| container | `RIFF....WEBP`, one `VP8 ` chunk, no `VP8X/ALPH/ICCP/EXIF/XMP` | no alpha, no metadata, no colour profile |
| codec | VP8 **lossy** key frame (`9d 01 2a` start code) | not VP8L |
| frame tag | `frame_type=key`, `version=0`, `show_frame=1`, first partition 16 699 B | version 0 = bicubic reconstruction, normal loop |
| dimensions | 2341×1199, h_scale=0, v_scale=0 | no internal scaling |
| chroma | **4:2:0**. `ffmpeg -pix_fmt yuv420p` yields exactly 2341·1199 + 2·1171·600 bytes; chroma planes are 1171×600 = 0.5002×0.5004 of luma | 25.0 % of the luma sample count |
| segmentation | enabled, 4 segments, `segment_feature_mode=1` | quantizer indices **20 / 20 / 16 / 12** |
| base quantizer | `y_ac_qi=40`, AC quantizer **44**, DC **37** | Y2-DC delta 0, Y2-AC −2, UV-DC −3 |
| loop filter | normal filter, level **7**, sharpness 0, no per-MB deltas | weak deblocking |
| token partitions | 1 (`log2=0`) | |
| block structure | 16×16 macroblocks, 4×4 DCT, 4:2:0 chroma | |
| crop | full frame; the 4-px transform grid is phase-locked at x ≡ 0 (mod 4) in every one of 8 vertical strips | no post-encode resize |

**Quality proxy.** A libwebp 1.6.0 re-encode of the decoded P1 at `quality=85`, `segments=4`
writes almost the same header (base QP 40; segment indices 20/18/12/8; 1 partition), so P1 is
approximately **libwebp q ≈ 83–85** (`q=83` gives base 46 / segments 23,21,16,11; `q=85` gives
40 / 20,18,12,8). The absolute quantizer is therefore *not* extreme: AC step 44 at the base
QP, 16–24 in the four segments. The small file size (0.50 bpp) comes from the smooth PCB
content, not from coarse quantization.

> **Caveat (stated explicitly).** The per-segment absolute-vs-delta semantics of VP8's
> segment quantizer fields cannot be resolved from the bitstream alone, and a controlled
> libwebp experiment was ambiguous (a 4-segment encode at q=85 has RMSE 2.02 DN against its
> input, between a 1-segment QP 38 encode, RMSE 2.11, and QP 34, 1.97). All conclusions below
> therefore rest on **measured** quantities (noise floor, block-edge energy, edge profile,
> re-encode stability), not on the nominal QP.

---

## 2. Compression artefacts — measured

All numbers in 8-bit DN. Luma = BT.601 (0.299/0.587/0.114); chroma from the decoder's
U/V planes and from RGB.

### 2.1 Block-boundary energy (fig. 1a,b)

Mean |ΔY| on each boundary position of the 16-px period, high-passed image, whole frame:

| grid | excess at boundary | % over median | z (vs other offsets) |
|---|---|---|---|
| **4-px transform grid** (x=4,8,12,16), vertical edges | **+0.38 DN** | **+36.7 %** | **10.8** |
| same, horizontal edges | +0.42 DN | +36.8 % | 30.5 |
| 16-px macroblock edge, luma (vertical) | +0.40 DN | +39.3 % | 2.0 (no *extra* step over the 4-px grid) |
| 16-px macroblock edge, chroma U | +0.21 DN | +53.7 % | 2.8 |
| 16-px macroblock edge, chroma V | +0.27 DN | +51.2 % | 2.2 |

The 4-px grid is the dominant, unambiguous signature (peaks at x = 4, 8, 12 and 16 in
fig. 1a); the 16-px macroblock boundary adds nothing measurable in luma but is visible in
chroma. Mean DC step across a macroblock boundary: **+0.10 DN** (mean high-pass luma,
fig. 1b). Grid phase is identical across the frame (no resampling drift), so the image was
encoded at its present resolution.

### 2.2 Noise floor

Robust σ of a σ=1.5 DN high-pass, per 32×32 tile, over the whole image:

| statistic | value |
|---|---|
| median tile σ | **0.67 DN** |
| p10 / p90 / p99 | 0.12 / 4.48 / 10.05 DN |
| ROI (x 1100–1700, y 940–1160) median σ | **0.90 DN** |

The random noise floor is far below the +14…+21 DN ridge contrasts discussed below.
Structured (edge-adjacent) artefacts, not σ, are the limiting factor near bright features.

### 2.3 Edge spread, overshoot and "ringing" (fig. 1d)

Averaged, step-normalised profile over 28 782 strong edges (|∇Y| > 20 DN/px, all
orientations, each profile divided by its own step before averaging):

| quantity | value |
|---|---|
| 10–90 % edge width | **2.68 px** (Gaussian-equivalent σ ≈ 1.05 px) |
| maximum slope | ≈ 0.42 step/px |
| overshoot (bright side) | **+6.0 %** at **+4 px** (≈ +5.7 DN for a 95 DN step) |
| undershoot (dark side) | **−6.0 %** at **−3 px** (≈ −5.7 DN for a 95 DN step) |

The profile is monotone with a weak symmetric overshoot/undershoot pair at ±3–4 px. This is
the *combined* optical + codec response; it cannot be separated into "lens blur" and "codec
ringing" without a controlled injection test (not run — see §7). What matters for feature
detection: **within 3–4 px of a 100 DN edge the local systematic floor is ≈ 6 DN**, 7× the
random tile noise.

Column profiles next to the 130 DN courtyard line (x = 1200 and 1250) confirm this: after the
edge the luma falls 173 → 70 and the minimum reached is 70 against a 67–69 DN background,
i.e. an undershoot of **≤ 3 DN**, with no oscillatory lobe beyond 2 px.

### 2.4 Chroma resolution and bleeding

* Chroma is stored at half resolution in **both** axes (4:2:0), so its Nyquist is 0.25 cyc/luma-px.
* Radially averaged luma PSD falls to 10× its noise floor at **0.229 cyc/px** and to 3× at
  0.279 cyc/px; the chroma PSD reaches 10× floor at **0.110 cyc/px** (in luma units) and is at
  the floor by 0.20 cyc/px. Luma PSD: 1.51 × 10⁻⁴ at 0.20–0.25 cyc/px vs a floor of
  9.9 × 10⁻⁶; the 0.25–0.30 band is 3.7 × 10⁻⁵ (4× the floor).
* **Consequence:** the image carries essentially no independent detail above
  **≈ 0.23–0.25 cyc/px** (period 4.0–4.3 px). Everything narrower than ~2 px is gone; the
  usable information band is ~0–0.25 cyc/px.
* Edge widths at the same strong colour edges: **Y 3.69 px, Cb 4.06 px, Cr 6.80 px**
  (10–90 %, step-normalised). Chroma transitions are 1.1–1.8× wider and smear across the
  luma edge.

### 2.5 The green channel as a "copper" discriminator

* 93.1 % of the variance of the green channel is explained by luma (G is luma-dominated and
  therefore full-resolution in the luma plane).
* Any *colour* discriminant (e.g. green-excess `G − (R+B)/2`) inherits the 4:2:0 chroma: its
  high-frequency share (>0.25 cyc/px) is 0.0055 of the total, versus 0.0016 for Y — i.e. it
  carries some extra high-frequency energy but that energy is chroma *interpolation* noise,
  not independent colour detail. A green/copper discriminator is therefore limited to
  ≈ 0.25 cyc/px (2-px Nyquist) and bleeds over 4–7 px. Colour is **not** a sub-pixel
  discriminator in these files.

---

## 3. The four 45° ridges: what they are and how stable they are

### 3.1 Census

Projection along `c = x + y` over x 1150–1620, y 990–1035 (the band where all four are
present), sub-pixel peak on a 1-px c grid, background = 61-px running median.
45° ridge direction confirmed by a slope scan: best `m = x + m·y` fit **m = 0.990 →
44.71°** from the y-axis (scan step 0.005 ⇒ ±0.29°).

| ridge | c (px) | peak contrast (DN) | FWHM in c (px) | perpendicular FWHM (px) | perpendicular spacing to next (px) | mm at 37.01 px/mm |
|---|---|---|---|---|---|---|
| **R1** | **2327.3** | **+16.7** | 7.18 | 5.08 | 10.7 | 0.289 |
| **R2** | **2342.4** | **+15.3** | 8.12 | 5.74 | 31.6 | 0.854 |
| **R3** | **2387.1** | **+13.8** | 6.95 | 4.91 | 11.1 | 0.300 |
| **R4** | **2402.8** | **+14.5** | 7.03 | 4.97 | — | — |

Spacings along c: **15.10 / 44.67 / 15.69 px** — i.e. the "16 / 43.5 px" family reported by the
parent is confirmed to ≈ 1 px (the band choice and peak-picking shift the ends by ~0.5 px).
The perpendicular spacings are c-spacing/√2 = **10.7 / 31.6 / 11.1 px** (0.289 / 0.854 /
0.300 mm).

The ridges extend from y ≈ 985 (above the bright courtyard line S1 at y 1025–1036) down to
y ≈ 1090 (R3/R4) or y ≈ 1045 (R1/R2); R1/R2 terminate at the silk line, R3/R4 continue to the
two vias. They are visible *through* the silk line at the same c, which is why a single
fixed band under the silk under-reports R1/R2 (a narrow-band measurement at y 1035–1100 sees
only +2.7/+3.7 DN for them; at y 1010–1030 they are +23/+21 DN).

### 3.2 Stability under re-encoding (fig. 2)

P1 re-encoded with libwebp at q = 100, 95, 90, 87, 85, 83, 80, 75, 70, 60, 50, 40, 30
(method 4), decoded, and the same four peaks re-measured:

| ridge | c range over q=30…100 | σ(c) | amplitude range (DN) | FWHM range (c px) |
|---|---|---|---|---|
| R1 | 2327.0 – 2327.5 (**0.5 px**) | 0.15 | 17.9 – 20.7 | 7.1 – 8.5 |
| R2 | 2341.9 – 2342.6 (**0.7 px**) | 0.21 | 16.8 – 18.5 | 8.0 – 9.5 |
| R3 | 2386.7 – 2387.2 (**0.5 px**) | 0.17 | 14.2 – 17.8 | 6.8 – 7.9 |
| R4 | 2402.4 – 2402.9 (**0.5 px**) | 0.15 | 14.3 – 17.3 | 6.5 – 9.5 |

Re-encoding raises the measured contrast by ≈ 3 DN relative to the original (19.6–20.7 vs
16.7 for R1) — the first encode has already smoothed the profile, and a second encode at
q ≥ 95 barely touches it; the *position* does not move. Additional tests (R3/R4):
10 successive q=83 re-encodes → c = 2386.05–2386.19 / 2401.73–2401.88 (**0.14 px** drift);
Gaussian σ = 1.0 px blur → c unchanged (2386.15), contrast 20.9 vs 22.5 DN; 3×3 median →
c 2386.16, contrast 21.8 DN. The ridges survive everything that preserves 4–6 px structure.

### 3.3 Null test: can noise/artefacts produce this?

120 phase-randomised surrogates of the same ROI (Fourier amplitudes preserved, phases
randomised — this keeps the block-artefact and texture power spectrum but destroys spatial
structure). Radon max over the same c-band (2370–2470):

| statistic | max ridge amplitude (DN) |
|---|---|
| surrogate mean ± σ | 3.30 ± 3.88 |
| p90 / p99 / max | 8.58 / 10.87 / **12.68** |
| observed (wide band y 960–1140) | 12.4 (R3) / 15.7 (R4) |
| observed (band y 990–1035) | 13.8 – 16.7 |

Two of the four exceed the surrogate maximum (p < 1/120), and the family as a whole sits at
the extreme tail of the null. Combined with the 0.15–0.2 px positional stability across
q = 30…100 and survival through 10 generations, a codec origin is **excluded** for all four.

> Incomplete: the full blind test — synthetic ridges injected at controlled width/contrast
> into a masked/phase-randomised ROI, to measure the detector's false-positive rate against
> the stated thresholds (contrast +20…+50 DN, width 4–8 px, run ≥ 10 px) — was designed but
> **not executed**. The phase-randomised null above is the substitute and only bounds the
> *Radon projection* statistic, not the row-scan+Hough detector.

---

## 4. Horizontal traces (sanity check on the same measurement chain)

Perpendicular centroid over columns x = 1200…1290, tight ±5.5 px windows (the broad
"trace" at y 1041 is a real, single-peaked +20…+30 DN ridge of FWHM ≈ 5 px):

| feature | centroid y (px) | column-to-column scatter |
|---|---|---|
| H1041 | **1041.47** | 0.18 px |
| H1085 | **1085.74** | 0.24 px |
| H1096 | **1096.22** | 0.29 px |

The earlier `y = 1029` entry is the peak of the bright silkscreen line S1 (172 DN, 10–11 px
wide), not a copper trace. H1041/H1085/H1096 are consistent with the "C5/C3/C4" inventory of
`blind_edge_forensics.md`.

---

## 5. Multi-image position noise (P2, P3, P4)

Three independent shots of the same bottom side. Coarse similarity by scale/rotation search
+ FFT phase correlation, then 280–400 high-gradient patches matched by normalised
cross-correlation, then a robust quadratic (12-parameter) mapping that absorbs lens
distortion; residual = per-patch position scatter after the best global model.

| pair | coarse scale | coarse translation | patches | median NCC | residual RMS | median | p95 |
|---|---|---|---|---|---|---|---|
| P3 → P2 | 1.002 | (11.1, 29.9) px | 276 | 0.837 | **0.504 px** | 0.389 | 0.918 |
| P4 → P2 | 1.000 | (15.7, 11.5) px | 273 | 0.852 | **0.476 px** | 0.375 | 0.916 |

**Per-image position noise for generic features: 0.4–0.5 px RMS (median 0.38 px).** This is
the irreducible disagreement between two photographs of the same rigid board after removing
all smooth geometric terms; it is dominated by localisation of finite-SNR, differently lit,
independently compressed texture, not by board deformation.

For comparison, a long, high-SNR, straight ridge measured 13 times under different
quantisation states agrees to **0.15–0.2 px** (§3.2). So the achievable sub-pixel precision
ranges from ≈ 0.15 px (best case: long, straight, +15 DN ridge) to ≈ 0.5 px (generic
feature), and **never** 0.05–0.1 px.

---

## 6. Confidence envelope for P1 and verdict on the prior claims

### 6.1 Envelope

| quantity | trustworthy limit | basis |
|---|---|---|
| **contrast** of a ridge on locally flat mask | **≥ 3–4 DN** (5σ of 0.7–0.9 DN tile noise) | §2.2 |
| contrast within 3–4 px of a >100 DN edge | **≥ 6 DN** (overshoot/undershoot floor) | §2.3 |
| **width**: resolvable | FWHM ≥ **4–5 px**; below ~3 px the measured width is the PSF, not the feature | 10–90 % = 2.68 px, no information above 0.25 cyc/px, §2.3–2.4 |
| width uncertainty at FWHM ≈ 5 px | **±0.3 px** statistical, **±0.5 px** systematic (background/PSF model choice) | §3.2 + profile fits |
| **position** of a long straight ridge (high SNR) | **±0.2 px** (σ), 0.5–0.7 px peak-to-peak over q = 30…100 | §3.2 |
| position of a generic feature | **±0.5 px** RMS | §5 |
| **angle** from a 45–100 px run | **±0.5°** (0.2 px end error over 45 px ⇒ 0.36°) | §3.1 |
| **colour** discriminator | 2-px Nyquist, 4–7 px bleed; unusable for sub-pixel geometry | §2.4–2.5 |

### 6.2 Prior claims

| prior claim | measured | verdict |
|---|---|---|
| four 45° ridges, spacing 16 / 43.5 px | **15.10 / 44.67 / 15.69 px** along c; 10.7 / 31.6 / 11.1 px perpendicular | **inside** (≈ 1 px agreement) |
| ridge slopes 45.0° | **44.71° ± 0.29°** (Radon scan) | **inside** (±0.3°) |
| ridge widths 5.5–6.0 px | **5.08 / 5.74 / 4.91 / 4.97 px** perpendicular FWHM | **borderline**: 5.5–5.7 are inside; 6.0 is 1.0 px (2σ systematic) above R3/R4 — the claim is not reproducible as stated |
| pair pitch 0.42 mm | perpendicular spacings **0.289 / 0.854 / 0.300 mm** | **outside**: 0.42 mm equals 15.5 px *along c* (or along x) ÷ 37.01 px/mm. That is a **√2 overestimate** of the perpendicular pitch (16.0/√2 = 11.3 px = 0.306 mm) |
| "faint diagonal ridges are real copper" | +14…+17 DN, 5 px FWHM, position σ 0.15–0.2 px over q = 30…100, survives 10 generations and σ=1 px blur, exceeds the phase-randomised surrogate maximum | **inside** for "real image content, not a compression artefact". Their *physical* identity (copper under mask vs. some other surface feature) cannot be proven from a single top-side photograph; the 45° geometry, the 16/44/16 regular spacing and the termination at the vias are consistent with routed copper |
| sub-pixel measurements at 0.1 px | best achievable 0.15–0.2 px; generic 0.4–0.5 px | **outside** |

### 6.3 Bottom line

P1 is **not** a heavily quantised file: 0.50 bpp but a measured tile noise of 0.67 DN, a
4-px block-edge excess of only 0.4 DN, and a 2.7 px edge spread dominated by optics, not
quantisation. It preserves structure down to ≈ 4 px (0.11 mm at 37.01 px/mm) and position to
≈ 0.2 px for long straight high-contrast features. The four 45° ridges are real, reproducible
image content at 14–21 DN contrast and 4.9–5.7 px perpendicular width, and they are the most
robust sub-pixel features in the ROI. What the file does **not** support is: any claim at
0.1 px, any width claim finer than ±0.3–0.5 px, any colour-based sub-pixel geometry, and the
0.42 mm pair pitch (a √2 coordinate error).

---

## 7. Limitations and tests not completed

1. **Codec ringing vs optical blur** are not separated. The ±6 % overshoot/undershoot of the
   mean edge profile is the combined system response. A controlled injection experiment
   (synthetic steps/ridges encoded at the file's measured quantiser) was designed but not run.
2. **Blind false-positive-rate test not executed.** No synthetic-ridge injection, no masked
   ROI, no detector ROC at the stated thresholds. Only the phase-randomised Radon null
   (§3.3) was completed.
3. **Per-segment quantiser semantics unresolved** (§1 caveat). The header values are exact;
   their absolute-vs-delta application is not, and one libwebp experiment was ambiguous.
4. **Multi-image test is bottom-side only.** P2–P4 give a 0.4–0.5 px noise floor for generic
   features, but no bottom-side feature was tracked at the ridge level, and P1 has no second
   top-side shot — the top-side position noise is inferred from the re-encode test, not from
   an independent image.
5. **The 37.01 px/mm scale** is taken from `ball_grid_calibration.md` (10.00 mm body =
   370.11 px, 0.80 mm ball pitch = 29.61 px). It is consistent with the same image, but it is
   a prior measurement, not one re-derived here.
6. **Registration model** is a global quadratic; local board warp and lighting differences
   are inside the 0.4–0.5 px residual, so that figure is an upper bound on true image noise.

## 8. Reproducibility

```
agents/C_work/scripts/vp8parse.py      RIFF/VP8 header parser (quantiser, filter, partitions)
agents/C_work/scripts/fx.py            numpy filters, PSD, phase correlation, sampling
agents/C_work/scripts/s1_encoding.py   container/plane facts, spectra, chroma metrics
agents/C_work/scripts/s2b_edges.py     tile noise, edge profile, chroma edge widths
agents/C_work/scripts/s2c_blocks2.py   block-boundary statistics (4/8/16/32), phase strips
agents/C_work/scripts/s4_register.py   P3/P4→P2 registration, quadratic fit, residuals
agents/C_work/scripts/s5_final.py      ridge census, slope scan, phase-randomised null
agents/C_work/scripts/s6_stability.py  re-encode/generation/denoise stability
agents/C_work/scripts/s7_quad.py       four-ridge measurement across the quality ladder
agents/C_work/scripts/s8_figs.py       figures
```
Raw data: `C_work/out/s1_encoding.json`, `s2b_edges.json`, `s2c_blocks2.json`,
`s4_register.json`, `s5_final.json`, `s6_stability.json`, `s7_quad*.json`;
decoded planes `C_work/img/p1_yuv420.yuv`; re-encodes `C_work/img/p1_rc_q*.webp`.
