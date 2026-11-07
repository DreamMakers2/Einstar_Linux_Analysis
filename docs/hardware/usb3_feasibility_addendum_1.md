# Addendum 1 — The FX3 lower edge, re-measured

> **SUPERSEDED IN PART by [Addendum 2](usb3_feasibility_addendum_2.md) (2026-11-08).**
> A full-row re-measurement finds **four** conductors in **two** coupled pairs, not one pair. The
> "no bundle of four" row below is wrong; §A1.5–A1.8 and the verdict are corrected in Addendum 2.
> This file is retained as the historical record of what the first pass measured and why it stopped
> where it did.

**Amends §3.6 and §5 of `usb3_mainboard_feasibility.md`. Does not replace its verdict.**
Read-only photographic re-analysis, 2026-11-07. No hardware touched, no power applied.

Two independent subagents were run against the same photograph without being told the earlier
conclusion: one blind feature-forensics pass, one blind ball-grid calibration. Their results are
folded in below, **including where they disagree with each other and with this analysis**.

---

## A1.1 Why this addendum exists

The main report said that around the FX3's row-A edge "the visible top copper shows the ground pour,
two silkscreen lines, and at most two thin traces that belong to other nets… **There is no bundle of
four**."

A challenge was raised that six copper traces escape the package. The challenge is **partly right**,
and chasing it produced two results that matter:

1. The original analysis **under-counted the copper** in that strip. My "two thin traces" line was
   wrong: there are at least five copper traces there, one of them a coupled pair that reaches the
   package edge.
2. A **second, independent calibration** of the ball grid moved every ball ~15 px (0.4 mm) from where
   I had put it. All the alignment arithmetic in earlier drafts of this addendum was therefore wrong,
   and has been redone.

| Original claim | Status now |
|---|---|
| "no bundle of four traces" | **superseded** — Addendum 2 finds four conductors in two pairs |
| "at most two thin traces that belong to other nets" | **wrong** — a coupled pair plus several through-routes |
| "SS pins probably left open (App. C)" | **no longer supported**, but not replaced by its opposite either |
| ball positions ±1 px | **wrong** — they were 15 px off; see A1.4 |

---

## A1.2 Method and the copper classifier

Everything is measured on `hardware/photos/P1_top_side.webp` at native resolution. No generated image
was used as evidence.

A pixel was accepted as **copper under green soldermask** only if its green channel exceeded a *local*
background (median of the same row, ±6…14 px away) by **≥ 20 counts**, was **2–6 px wide**, and was
**continuous over ≥ 10 px**. That threshold is calibrated on this image:

| Material | Green | Luminance | Width | Signature |
|---|---|---|---|---|
| Board mask, no copper | 64 | 61 | — | baseline |
| **Copper under mask** (control: a known 45° pair at x 1080–1260, y 505–565) | 95–107 | 89–94 | 4.4–5.5 px ⟂ | **+31 … +49** |
| Silkscreen (control: x 1129–1142, y 476–512) | 168–230 | 185–199 | 9–11 px, flat top, round ends | **+131 … +141** |
| Copper pour | 105–114 | 100–110 | plateau | no ridge, sharp step edges |

Copper and silkscreen differ by a factor of four in contrast, so the classifier is not marginal.

Tracks were extracted as per-row ridge peaks with sub-pixel interpolation, linked across rows, and
least-squares fitted. **Scale: 37.01 px/mm** (independent calibration, A1.4).

---

## A1.3 What is below the package

### O1 — a coupled pair really does emerge from under the package

Two parallel 45° traces run up to the package edge and **cross it**. Measured along the fitted
centrelines against a *local* background, at every row:

| y (px) | 988 | **992** | 996 | 1000 | 1004 | 1008 | 1012 | 1016 | 1020 | 1024 | 1028 | 1032 | 1036 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| D1 contrast | −5 | **+38** | +24 | +24 | −12 | −4 | +40 | +36 | +38 | +36 | −6 | −4 | +32 |
| D2 contrast | +2 | **+38** | +27 | +32 | −12 | −6 | +40 | +23 | +31 | +23 | −7 | −1 | +47 |

The negative rows are the two silkscreen lines (y 1002–1010 and 1028–1034), where an opaque white
stroke covers whatever is underneath. Everywhere else contrast is +23…+47. At **y = 988 it collapses
to zero** — four pixels above the package edge at y = 991.9. That is the package shadow: the traces go
under the part.

**The pair crosses the package edge at x = 1396.0 and x = 1411.4**, 15.4 px = **0.416 mm** apart. This
is a direct measurement, not an extrapolation.

> **Correction to the blind subagent's conclusion.** It reported that the traces "stop 6–8 px short of
> the courtyard silkscreen" and "do not reappear in the clean dark gap y 1013–1022". Direct pixel
> sampling contradicts that: at y = 1016 and 1020 the traces read green 100/89 and 107/99 against a
> local background of 64/68, i.e. +23…+38. The subagent's own line equations (D1 x+y = 2386,
> D2 x+y = 2402) predict x = 1366 and 1382 at y = 1020 — exactly where the bright pixels are. Its gap
> check appears to have used a threshold or window that missed them. The results below use the direct
> measurement.

### O2 — the horizontal traces bend into the pair

The blind pass and this analysis agree on the lower half: horizontal copper at y ≈ 1041, 1085 and 1096
runs left from x ≈ 700, and at x ≈ 1301–1306 turns 45° up into the pair. The bend is sharp and common
to both traces — what a routed differential pair looks like at a corner.

### O3 — several more long traces pass through, and they are through-routes

Independent of the pair, three further near-horizontal traces run the width of the strip:

| Pair | y (px) | gap | Extent |
|---|---|---|---|
| P1 | 978 / 989 | 11 px = 0.29 mm | **x 620 → 1680** |
| P2 | 1030 / 1041 | 11 px | x 620 → 1124 |
| P3 | 1085 / 1096 | 11 px | x 620 → 1124 (merges into the pour) |

P1 is continuous from x 620 to x 1680 — it spans the whole package, from the J4 side to past the FPGA.
**It cannot originate at the FX3.** These are through-routes in the channel between the FPGA and the
connector. The six-trace reading of the original challenge is therefore **six traces, but not six FX3
escapes**.

### O4 — unchanged

No 0.1 µF series pair, no SuperSpeed ESD array, no SSTX/SSRX test pads, on either photographed face.

---

## A1.4 The ball grid, independently calibrated

The first calibration in this study placed the package body at x 1176–1560, y 624–990 (≈37.5 px/mm).
An independent pass fitted a matched filter to 230–310 scanlines per edge, separating the **package
bevel** from the **silkscreen courtyard** 40 px further out:

| | Value |
|---|---|
| Body corners | TL (1172.61, 621.05) · TR (1542.41, 622.37) · BR (1541.35, 991.94) · BL (1170.82, 991.57) |
| Side lengths | 369.80 / 370.53 / 370.53 / 369.58 px → **370.11 px = 10.00 mm** |
| Scale | **37.01 px/mm** (0.80 mm = 29.61 px) |
| Perspective | affine residual 0.30 px r.m.s.; homography differs by ≤0.30 px → **affine adequate** |
| Pin-1 laser dot | (1509.2, 955.35), r = 8.7 px; maps to (0.873, 0.987) mm vs A1 (1.000, 1.000) → **0.13 mm off A1**, toward the corner |
| Per-ball uncertainty | **±1.5 px (0.04 mm)** 1σ, worst case ±3 px |

Row A (y = 954.7):

| Ball | A10 | A9 | A8 | A7 | A6 | A5 | A4 | A3 |
|---|---|---|---|---|---|---|---|---|
| x | 1237.8 | 1267.5 | 1297.1 | 1326.8 | **1356.5** | **1386.1** | **1415.7** | **1445.3** |

The pin-1 dot sits 0.13 mm off the A1 ball centre and 0.87 mm from the column-1 edge — consistent with
a corner mould mark rather than a ball-centre mark. The 180° orientation is confirmed independently by
the silkscreen triangle and the two direction arrows emanating from that corner.

---

## A1.5 Geometry: does the pair match A3–A6?

The pair crosses the package edge at **1396.0** and **1411.4** (y = 991.9), separation 0.416 mm.

| Comparison | Offset |
|---|---|
| left trace 1396.0 vs **A5** 1386.1 | **9.9 px = 0.27 mm** |
| right trace 1411.4 vs **A4** 1415.7 | **4.3 px = 0.12 mm** |
| pair centre 1403.7 vs **A5/A6** midpoint 1371.3 (the TX pair) | **32.4 px = 0.88 mm** |
| pair centre 1403.7 vs **A3/A4** midpoint 1430.5 (the RX pair) | **26.8 px = 0.72 mm** |
| pair centre 1403.7 vs A4/A5 midpoint 1400.9 | 2.8 px — but A4/A5 is **not a valid pair** |

Two conclusions follow, and they cut in opposite directions:

* **Against**: the two traces do **not** sit on the two balls of any valid SuperSpeed pair. The
  offsets (27–32 px) are 9–11σ at the calibrated uncertainty — far outside measurement error. And two
  traces emerging straight from adjacent row-A balls would be 29.6 px apart, not 15.4 px.
* **For**: the pair is nonetheless *inside the A3–A6 span* and emerges from the package exactly where
  a row-A escape would. Traces can reach the top layer from vias rather than from balls directly, in
  which case the landing position says nothing about which ball feeds them.

**The photograph cannot decide between these.** What it can do is rule out the tidy version of the
hypothesis: *the pair is not A3/A4 and it is not A5/A6 as a straight ball escape.*

---

## A1.6 Hypothesis testing

### Hypothesis A — SuperSpeed is routed, a custom USB 3.0 connection is possible

**For**: a real coupled pair emerges from the package in the SS region; the product family's software
expects USB 3.0; the geometry (0.42 mm pitch, 45°, common bend) is exactly how such a pair is drawn.

**Against**:
* it matches no valid SS ball pair (9–11σ);
* **only one pair exists where two are required** — no second coupled pair anywhere in the strip;
* **no 0.1 µF series capacitors** exist on either face (mandatory on SSTX, AN70707 §12.1);
* **no SuperSpeed ESD array** exists;
* the six "extra" traces are through-routes, not escapes.

### Hypothesis B — unrelated copper; the design remains USB 2.0-only

**For**: the missing mandatory components; the invalid pairing; the through-routes outnumbering the
escape.

**Against**: it does not explain *what* the pair is. "Unrelated copper" is a label, not an
explanation, and the pair is coupled, 45°, and terminates at the package.

**Neither hypothesis is proved.** A is disfavoured on the electronics, B on the unexplained pair.

### Alternatives tested and rejected

| Explanation | Test | Result |
|---|---|---|
| Silkscreen | luminance 185–199, 9–11 px, flat top, round ends | **rejected** — features are 89–107, 5.6 px |
| Compression ringing | ringing is parallel to the bright edge and decays with distance; here the edges are horizontal | **rejected** — features are at 45°, Radon peak 21.0 at 45–46° vs 13.8 at 30°, and the y≈1042 feature is absent at x = 1320/1520/1560 where the same silkscreen edge is equally bright |
| Hatched copper pour | a cross-hatch has two diagonal families | **rejected** — zero down-right diagonals in the strip |
| Pour boundary | sharp step, no ridge, 45° chamfers | **rejected** for the pair; **accepted** for the region below y ≈ 1100 |
| Wide trace with two bright edges | inter-edge level stays elevated | **partly accepted** for the horizontal set — their inter-line level is 78–83 vs 61 background, unlike the pair (67.5) |

---

## A1.7 Evidence table

| # | Statement | Class |
|---|---|---|
| 1 | A coupled 45° pair crosses the package edge at x = 1396.0 and 1411.4, 0.416 mm apart | **Directly observable** (+38 local contrast at y = 992) |
| 2 | The pair bends at x ≈ 1301–1306 into horizontal traces at y ≈ 1041 / 1085 / 1096 | **Directly observable** |
| 3 | P1 (y 978/989) runs continuously from x 620 to x 1680, past both package edges | **Directly observable** — a through-route |
| 4 | No 0.1 µF series pair, no SS ESD array, no SS test pads on either face | **Directly observable** |
| 5 | Package is 370.11 px = 10.00 mm; scale 37.01 px/mm; affine adequate | **Strong inference** (sub-pixel edge fit, 230–310 scanlines) |
| 6 | Ball centres: A5 = 1386.1, A4 = 1415.7, A3 = 1445.3, A6 = 1356.5 | **Strong inference** (±1.5 px) |
| 7 | The pair matches no valid SS ball pair (27–32 px off, 9–11σ) | **Strong inference** |
| 8 | The pair is copper, not silkscreen / ringing / hatch / pour edge | **Strong inference** (calibrated classifier, three alternatives rejected) |
| 9 | Which balls feed the pair | **Requires X-ray or electrical test** — vias under the package are invisible |
| 10 | Where the pair terminates at its other end | **Requires electrical test** — it runs into a channel carrying at least five other traces |
| 11 | Whether R_USB3 (200 Ω, B3) is fitted | **Requires electrical test** |
| 12 | Whether the SS PHY is biased, and could ever train | **Requires electrical test** |
| 13 | Layer of any trace, and whether the pair reaches J4 on an inner layer | **Impossible from photographs alone** |

---

## A1.8 Revised verdict

> **The NO-GO for a passive modification stands.** The earlier *reasoning* was too strong in one place
> — the strip is not empty, and a coupled pair does emerge from the package — but the conclusion does
> not change, and one of the two supporting arguments is now stronger, not weaker.

What changed and what did not:

* **Changed**: the board has real, coupled, high-speed-looking routing emerging from the FX3 region.
  The statement "nothing is routed" is withdrawn.
* **Unchanged**: there are no AC-coupling capacitors, no SS ESD array, no SS test points, and the
  7-pin external connector has one spare contact against four required.
* **Strengthened**: the pair fits no valid SS ball pair, and there is only one pair where two are
  needed.

| Option | Before | Now | Why |
|---|---|---|---|
| C — flying-lead tap | no-go | **no-go** | SI budget unchanged; and there is still no verified tap point |
| E — new daughterboard if SS reaches J4 | ~10–15 % | **~15–20 %** | a coupled pair exists and heads toward J4, but it is unassignable and incomplete |
| F — board re-spin | only clean path | **unchanged** | — |

---

## A1.9 The three decisive unpowered measurements

### M-A · The 200 Ω sweep — still the decisive one

Sweep every 0402/0603 resistor within ~12 mm of the FX3, black lead on a ground ring
(`hardware/annotated/A7_multimeter_procedure.png`).

* **6.04 kΩ** → R_USB2 (C8) found: the method is valid.
* **200 Ω** → R_USB3 (B3) fitted: the SS PHY is biased, the pair is probably SuperSpeed, and an X-ray
  becomes worth commissioning.
* **Neither** → the PHY has no calibration reference. **No routing can make it train.** The NO-GO
  becomes final.

Why it still wins: it is binary, accessible, non-destructive, and it is the only measurement that can
*disqualify* the whole idea. An X-ray can only describe.

### M-B · J4 continuity and diode map

Unpowered, in continuity/diode mode, from each of the 40 J4 contacts to the ground ring:

* **< 1 Ω** → ground or a power rail;
* **0.4–0.7 V one way, open the other** → connected to an IC (FX3 or FPGA) — a live signal pin;
* **open both ways** → spare, or only reaches a passive.

Establishes whether the mainboard→daughterboard link has *any* spare conductors — the precondition for
option E. Does not identify SSTX/SSRX.

### M-C · Capacitance across J4 pairs (only if M-A finds 200 Ω)

With a pF-capable meter, across candidate J4 pin pairs:

* **≈ 0.05 µF** → two 0.1 µF caps in series: an AC-coupled SuperSpeed TX pair. This is the one
  electrical signature that distinguishes SSTX from every other pair on the board.
* **2–10 pF** → ordinary signal pair.
* **open** → unconnected.

---

## A1.10 What would settle it

A **2D X-ray of the J4-to-FX3 region from the component side**. One image, non-destructive, resolves:

1. whether traces leave balls A3–A6, and whether they pass through vias;
2. whether B3 carries a trace to a 200 Ω resistor;
3. where the coupled pair terminates.

No optical method can substitute. The region of interest is 1.6 mm tall, the traces are 0.15 mm, and
the exact place where they meet the package is covered by opaque silkscreen.

---

## A1.11 Reproduce

```bash
python3 hardware/make_addendum_figs.py     # B1, B2
```

Figures: `hardware/annotated/B1_escape_pair_measured.png`,
`hardware/annotated/B2_routing_channel.png`.
Independent passes: `hardware/analysis/blind_edge_forensics.md`,
`hardware/analysis/ball_grid_calibration.md`, overlay `hardware/crops/calib_overlay.png`.
Crops cited: `pair_detail`, `pair_wide`, `bend_precise`, `copper_map`, `track_scatter`,
`six_lines_zoom`, `left_edge_structure`.
