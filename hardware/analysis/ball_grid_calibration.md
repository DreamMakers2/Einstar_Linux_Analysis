# CYUSB3014 (121-FBGA) ball-grid calibration from `P1_top_side.webp`

Independent measurement of the FX3 package in the top-side photograph. All numbers below
were derived from the image; no prior coordinate estimates were used.

Image: `<repo>/hardware/photos/P1_top_side.webp` (2341x1199).

## 1. Package body edges

Two bright/dark lines run parallel to every package edge:

* an outer **silkscreen courtyard** line, ~1.0-1.1 mm outside the body (x=1132 left, x=1582 right, y=580 top, y=1025 bottom);
* the **package bevel**: a bright specular ridge on the lit (left/bottom) sides, a dark shadowed band on the unlit (right/top) sides,
  spanning ~0.1-0.3 mm.

The body outline (JEDEC '10.00 mm' = maximum body size) is the **outer foot** of the bevel. It is measured as the
10 % crossing of the bevel ridge on its outer flank, on a high-SNR template (median of 230-310 scanlines), with the
per-scanline position obtained by matched filtering (sub-pixel by parabolic interpolation of the correlation peak).

| edge | fitted silhouette line | position at package centre | per-scanline r.m.s. | scanlines |
|---|---|---|---|---|
| left   | x = 1175.627 -0.00485·y | x(806) = 1171.72 | 0.10 px | 304 |
| right  | x = 1544.195 -0.00287·y | x(798) = 1541.90 | 0.53 px | 230 |
| top    | y = 616.884 +0.00356·x | y(1357) = 621.71 | 0.14 px | 309 |
| bottom | y = 990.404 +0.00100·x | y(1357) = 991.76 | 0.24 px | 289 |

Uncertainty of each edge: +/-0.2 px (statistical, from the line fit) and +/-0.8 px (systematic,
edge-definition / threshold choice: moving the criterion from 5 % to 35 % shifts each edge by 1.0-1.7 px).

**Corners** (intersections of the fitted silhouette lines, image px):

| corner | x (px) | y (px) |
|---|---|---|
| TL | 1172.61 | 621.05 |
| TR | 1542.41 | 622.37 |
| BR | 1541.35 | 991.94 |
| BL | 1170.82 | 991.57 |

Side lengths: top 369.80, bottom 370.53, left 370.53, right 369.58 px;
mean 370.11 px for 10.00 mm -> **37.01 px/mm** (0.80 mm pitch = 29.61 px),
i.e. 37.15 px/mm if the bevel foot is taken 0.7 px further out on each side.

## 2. Perspective / affine test

* Opposite sides differ by 0.73 px (top vs bottom) and 0.95 px (left vs right).
* Parallelogram error |TL+BR-TR-BL| = 1.20 px.
* Least-squares **affine** fit of the four corners to the 10x10 mm square: r.m.s. residual **0.30 px**, max component 0.24 px.
* The projective (homography) model passes exactly through the four corners (residual 0 by construction); over all
  121 ball sites it differs from the affine model by at most **0.30 px**.

**A simple affine model is adequate** (residual 0.30 px << 3 px threshold); perspective is negligible.
A homography is nevertheless used for the grid so that the result is exact at the corners.

## 3. Ball grid

Model: the measured quad is mapped to the ideal 10 x 10 mm square; the array is 8.00 mm centre-to-centre,
so ball (row i, col j) sits at u = 1.00 + 0.80(j-1) mm, v = 1.00 + 0.80 i mm from the pin-1 corner. The package is
mounted rotated 180 deg, so **A1 is the bottom-right corner** in image coordinates (u measured from the image right edge,
v from the image bottom edge).

| ball | x (px) | +/-(px) | y (px) | +/-(px) |
|---|---|---|---|---|
| **A1** | **1504.49** | 0.99 | **954.87** | 0.82 |
| **A3** | **1445.32** | 0.81 | **954.80** | 0.82 |
| **A4** | **1415.71** | 0.73 | **954.76** | 0.82 |
| **A5** | **1386.09** | 0.68 | **954.73** | 0.82 |
| **A6** | **1356.46** | 0.64 | **954.69** | 0.82 |
| **A9** | **1267.50** | 0.68 | **954.58** | 0.82 |
| **A10** | **1237.82** | 0.75 | **954.54** | 0.82 |

Full grid (121 balls) - x/y in image px, 1-sigma from a 4000-trial Monte-Carlo over the edge uncertainties:

| row | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 | 11 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| A | 1504.5/954.9 | 1474.9/954.8 | 1445.3/954.8 | 1415.7/954.8 | 1386.1/954.7 | 1356.5/954.7 | 1326.8/954.7 | 1297.2/954.6 | 1267.5/954.6 | 1237.8/954.5 | 1208.1/954.5 |
| B | 1504.6/925.3 | 1475.0/925.2 | 1445.4/925.2 | 1415.8/925.1 | 1386.2/925.1 | 1356.6/925.0 | 1326.9/925.0 | 1297.3/925.0 | 1267.6/924.9 | 1238.0/924.9 | 1208.3/924.8 |
| C | 1504.7/895.7 | 1475.1/895.6 | 1445.5/895.6 | 1415.9/895.5 | 1386.3/895.5 | 1356.7/895.4 | 1327.1/895.4 | 1297.4/895.3 | 1267.8/895.3 | 1238.1/895.2 | 1208.4/895.2 |
| D | 1504.8/866.1 | 1475.2/866.0 | 1445.6/865.9 | 1416.0/865.9 | 1386.4/865.8 | 1356.8/865.8 | 1327.2/865.7 | 1297.5/865.7 | 1267.9/865.6 | 1238.2/865.6 | 1208.5/865.5 |
| E | 1504.9/836.5 | 1475.3/836.4 | 1445.7/836.3 | 1416.1/836.3 | 1386.5/836.2 | 1356.9/836.2 | 1327.3/836.1 | 1297.7/836.0 | 1268.0/836.0 | 1238.4/835.9 | 1208.7/835.9 |
| F | 1504.9/806.9 | 1475.4/806.8 | 1445.8/806.8 | 1416.2/806.7 | 1386.6/806.6 | 1357.0/806.6 | 1327.4/806.5 | 1297.8/806.4 | 1268.1/806.3 | 1238.5/806.3 | 1208.8/806.2 |
| G | 1505.0/777.3 | 1475.5/777.2 | 1445.9/777.2 | 1416.3/777.1 | 1386.7/777.0 | 1357.1/777.0 | 1327.5/776.9 | 1297.9/776.8 | 1268.3/776.7 | 1238.6/776.7 | 1209.0/776.6 |
| H | 1505.1/747.8 | 1475.6/747.7 | 1446.0/747.6 | 1416.4/747.5 | 1386.9/747.4 | 1357.3/747.4 | 1327.7/747.3 | 1298.0/747.2 | 1268.4/747.1 | 1238.8/747.0 | 1209.1/747.0 |
| J | 1505.2/718.2 | 1475.7/718.1 | 1446.1/718.0 | 1416.5/718.0 | 1387.0/717.9 | 1357.4/717.8 | 1327.8/717.7 | 1298.2/717.6 | 1268.5/717.5 | 1238.9/717.4 | 1209.2/717.4 |
| K | 1505.3/688.7 | 1475.8/688.6 | 1446.2/688.5 | 1416.7/688.4 | 1387.1/688.3 | 1357.5/688.2 | 1327.9/688.1 | 1298.3/688.0 | 1268.7/687.9 | 1239.0/687.8 | 1209.4/687.8 |
| L | 1505.4/659.1 | 1475.9/659.0 | 1446.3/658.9 | 1416.8/658.8 | 1387.2/658.7 | 1357.6/658.7 | 1328.0/658.6 | 1298.4/658.5 | 1268.8/658.4 | 1239.1/658.3 | 1209.5/658.2 |

Row A lies at y = 954.87 +/- 0.82 px; the pitch measures 29.61-29.68 px between
adjacent columns, consistent with 0.80 mm x 37.01 px/mm = 29.61 px.

## 4. Laser pin-1 dot

A round, flat-topped bright mark (luminance 132-135 on a 47 background) sits inside the package near the bottom-right corner.

| quantity | value |
|---|---|
| centre | (1509.2, 955.35) +/- 0.2 px |
| radius (half-max) | 8.7 px = 0.235 mm (diameter 0.47 mm) |
| position in package frame | u = 0.873 mm, v = 0.987 mm |
| A1 (computed) | u = 1.000 mm, v = 1.000 mm |
| offset from A1 | (-0.127, -0.013) mm, |offset| = **0.128 mm** toward the corner |

The mark is within 0.5 px of the computed A1 row position, i.e. it is **not** centred on A1: it lies
0.13 mm (4.7 px) closer to the column-1 edge, i.e. offset toward the package corner. The offset is ~1.5x the
calibration systematic, so it is marginally significant; it is also consistent with a laser-mark placement tolerance.
Crucially, the mark's presence at the bottom-right corner is independent evidence that **A1 is at the bottom-right**,
confirming the 180 deg rotation.

## 5. Silkscreen pin-1 marks (visual inspection at 4x)

* The courtyard rectangle (x=1132/1582, y=580/1025) is ~1.0-1.1 mm outside the body on all four sides - it is a courtyard, not an orientation mark.
* A **filled triangle** sits outside the bottom-right corner with its apex pointing at that corner.
* A straight silk line runs along the right edge, ending in an arrowhead that points **up** at the top-right;
  a second line runs along the bottom edge and ends in an arrowhead pointing **left** at the bottom-left.
* A small L-shaped bracket lies just outside the top-right corner (meaning unresolved - possibly unrelated to pin 1).

Reading: the two arrows start at the bottom-right corner and run along the row and column directions away from it,
and the filled triangle points at the same corner. Both indicate **pin 1 = bottom-right**, which **agrees** with the
180 deg rotation assumption and with the laser dot.

## 6. Pango FPGA cross-check - NOT COMPLETED

The 484-ball Pango (PGL25G, 22x22) is at x ~1810-2240, y ~190-980; its column numbers 22...1 are printed along its
left edge at x ~1640-1680. The tick spacing was **not measured** before this analysis was cut short, so the
consistency check is still open.

## 7. Residual uncertainty

* Ball position, random (Monte-Carlo, 1 sigma): +/-0.6 to 1.0 px in x, +/-0.8 px in y.
* Ball position, systematic: the body-edge definition (+/-0.8 px per edge) and the 10.00 mm reference give a
  scale uncertainty of +/-0.4 % (+/-0.15 px/mm), which adds +/-0.03 mm at the far corner of the array.
* **Total: +/-1.5 px (0.04 mm) 1 sigma per ball, worst case +/-3 px (0.08 mm).**
* The pitch is 29.61 px; a 0.08 mm error is 0.1 pitch, so ball index assignment (rows A-L, columns 1-11) is unambiguous.

## Files

* overlay: `hardware/crops/calib_overlay.png` (2x crop, green = measured body outline, circles = computed ball centres,
  red = row A, cyan = laser dot).
* scripts: `hardware/analysis/calib_scripts/` (geom.py, model.py, mc.py, overlay.py, report.py).
