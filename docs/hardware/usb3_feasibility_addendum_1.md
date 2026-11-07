# Addendum 1 — Re-examination of the FX3 lower edge: what is actually there

**Supersedes nothing. Amends §3.6 and §5 of `usb3_mainboard_feasibility.md`.**
Status: read-only photographic re-analysis, 2026-11-07. No hardware was touched.

---

## A1.1 Why this addendum exists

The main report stated that around the FX3's row-A edge "the visible top copper shows the ground
pour, two silkscreen lines, and at most two thin traces that belong to other nets… **There is no
bundle of four**." A challenge was raised with six candidate copper features below the package.

The challenge is **partly correct, and the correction matters**. The earlier statement was based on
crops that were too tight and on a contrast treatment that crushed dim detail. Re-working the region
at native resolution with a calibrated copper-vs-silkscreen classifier shows considerably more copper
than the original report admitted.

What changed, precisely:

| Original claim | Status now |
|---|---|
| "no bundle of four traces" | **Still true** — no four-trace bundle exists |
| "at most two thin traces that belong to other nets" | **Wrong** — there are six long copper traces plus a coupled 45° pair |
| "SS pins are likely left open (App. C)" | **No longer supported** — a coupled pair does escape the package edge |

---

## A1.2 Method (and why the classification is trustworthy)

Everything here is measured on `hardware/photos/P1_top_side.webp` at native resolution. No AI-generated
image was used as evidence; the earlier annotated figures were used only as locators.

A pixel class was accepted as **copper under green soldermask** only if it matched a calibrated
signature measured on this same photograph:

| Material | RGB (sample) | Luminance | Width | Behaviour at silkscreen |
|---|---|---|---|---|
| Board mask (no copper) | 33, 64, 86 | **61** | — | — |
| **Copper under mask** (verified on the diagonal pair) | 61, 95, 112 | **89–94** | 2–3 px | passes under, interrupted |
| Silkscreen | 177, 168, 229 | **191** | 5–8 px | on top, opaque |
| Exposed pad / bare copper | 62, 133, 140 | 110–140 | — | not covered |

The classifier is therefore "green channel elevated by 25–60 over the local median, width ≤ 4 px,
continuous over ≥ 10 px". Silkscreen is excluded by luminance (>150) and by width.

Ridge positions were extracted per row with sub-pixel parabolic interpolation, then linked into tracks.
All coordinates below are source pixels; **37.5 px ≈ 1 mm**.

---

## A1.3 What is actually below the package

### Observation O1 — the strip is a routing channel, not empty board

Six copper traces run **horizontally across the whole strip**, in three pairs:

| Pair | y (px) | intra-pair gap | Extent | Continues right of the FX3? |
|---|---|---|---|---|
| P1 | 978 / 989 | 11 px = 0.29 mm | x 620 → 1180 | **yes — traced to x ≈ 1680**, i.e. past the FPGA's left edge |
| P2 | 1030 / 1041 | 11 px = 0.29 mm | x 620 → 1132 | **yes — to x ≈ 1570**, just past the package's right edge |
| P3 | 1085 / 1096 | 11 px = 0.30 mm | x 620 → 1124 | partially — it enters the copper-pour region and cannot be tracked further |

**This is the single most important new fact.** A trace that continues past the package on *both*
sides — toward J4 on the left and beyond the package on the right — cannot originate at the FX3's
row-A balls. It is a **through-route in the channel between the FPGA and the connector**. P1 is the
clearest case: it is continuous from x 620 to x 1680, spanning the entire package.

The user's hypothesis mapped six traces to A3/A4 + A5/A6 + A9/A10. **That mapping is not supported**:
the six long traces pass the FX3 rather than terminate at it, and they are 40–100 px below the ball row
(y = 948), not at it.

### Observation O2 — one genuine coupled pair *does* escape the package

Independent of the six through-routes, there is a **two-trace, 45° coupled pair** that ascends toward
the package's bottom edge and stops there:

| Property | Measured |
|---|---|
| Slope (least squares, 9 rows each) | **45.2°** and **45.9°** |
| Centre-to-centre spacing | **15–17 px = 0.41–0.46 mm** |
| Trace width | ≈ 3 px = 0.08 mm |
| Contrast over mask | +30 luminance (same as verified copper) |
| Visible extent | (1306, 1080) → (1395, 1005) |
| Extrapolated landing at the package edge (y = 990) | **x = 1403** and **x = 1419** |

It is a real differential pair by geometry (parallel, constant spacing, equal width, 45°) and by
material (copper under mask). See `hardware/annotated/B1_escape_pair_measured.png`.

### Observation O3 — the pair bends into the channel

At ≈ (1300, 1085) both traces turn from 45° to horizontal and join the P3 run. The bend is visible as
a single junction knot. So the path is:

```
FX3 bottom edge  →  45° down-left  →  bend at (1300,1085)  →  horizontal left, toward J4
```

### Observation O4 — nothing else changed

Re-checked at the same time and unchanged from the main report: **no 0.1 µF series pair**, **no
SuperSpeed ESD array**, **no SSTX/SSRX test pads** anywhere on either photographed face.

### Observation O5 — an exposed pad cluster near the left board edge

At x 150–330, y 1060–1120 there is a grid of **exposed (mask-opened) oval pads** on ~0.8 mm pitch,
surrounded by a 1.2 mm-pitch via field. It is not referenced anywhere in the main report. Its function
is **unresolved** — candidates are an unpopulated component footprint (ESD/level-shifter class, 0.8 mm
pitch) or a connector/test-pad array. It is flagged here because it is the kind of site that could be
populated, and because the six channel traces run toward it.

---

## A1.4 Geometry: does the pair match A3–A6?

Ball centres were recomputed from the package body edges (x 1176/1560, y 624/990), the 1.00 mm
outer-ball inset and the 0.80 mm pitch:

| Ball | A10 | A9 | A8 | A7 | A6 | A5 | A4 | A3 |
|---|---|---|---|---|---|---|---|---|
| x (px) | 1249.5 | 1279.5 | 1309.5 | 1339.5 | **1369.5** | **1399.5** | **1429.5** | **1459.5** |

Uncertainty: ±5 px on A1 (edge measurement), ±0.3 px/ball on pitch → **±6.5 px at column 5–6**;
perspective adds ≈ 5 % (≈ 3 px at this radius). Total **±10 px**.

| Test | Result |
|---|---|
| Left trace landing 1403 vs A5 1399.5 | **matches within 3.5 px** |
| Right trace landing 1419 vs A4 1429.5 | 10.5 px off |
| Pair centre 1411 vs **A5/A6 midpoint 1384.5** (the TX pair) | **26.5 px off = 2.6 σ** |
| Pair centre 1411 vs **A3/A4 midpoint 1444.5** (the RX pair) | **33.5 px off = 3.3 σ** |
| Pair centre 1411 vs **A4/A5 midpoint 1414.5** | 3.5 px — but A4/A5 is **not a valid pair** |

**Conclusion: the pair does not align with either valid SuperSpeed pair.** The only ball pairing it
matches is A4/A5, which pairs SSRXP with SSTXP — electrically meaningless. Either the calibration is
worse than measured, or the traces converge asymmetrically under the package, or they do not come from
row A at all.

This is the opposite of the tidy story the hypothesis proposed, and it must be stated plainly.

---

## A1.5 Hypothesis testing

### Hypothesis A — the pairs are routed SuperSpeed, a custom USB 3.0 connection is possible

**Strongest evidence for:**
* a genuine coupled pair, correct 45° geometry, verified copper, terminating at the package edge;
* it is exactly where a row-A escape would be;
* the product family's software expects USB 3.0, so routing it would not be irrational.

**Strongest evidence against:**
* the pair's centre is 2.6 σ from the TX pair and 3.3 σ from the RX pair — it matches no valid ball pair;
* **only one pair exists**, not two. A SuperSpeed device needs TX *and* RX; a lone pair is useless;
* no 0.1 µF series capacitors exist anywhere on either face (mandatory on SSTX, AN70707 §12.1);
* no ESD array exists (recommended, 0.5 pF);
* the six long traces in the same strip are through-routes, so "six escapes" does not survive scrutiny.

### Hypothesis B — unrelated copper, the design remains USB 2.0-only

**Strongest evidence for:**
* the six long traces demonstrably pass the FX3 on both sides (through-routes);
* the one escape pair fits no valid SS ball pair;
* the missing AC caps and ESD array are exactly what a USB-2.0-only design omits.

**Strongest evidence against:**
* the escape pair is *real*, coupled, and terminates at the package edge — it is not silkscreen, not a
  pour boundary, and not compression ringing (tested: ringing is parallel to bright edges, i.e.
  horizontal; the 45° energy is absent beside other silkscreen edges);
* "unrelated" is not an explanation of *what* it is — the pair still has to go somewhere.

**Neither hypothesis is proved.** A is disfavoured on geometry and on the missing mandatory components;
B explains the six traces but not the paired escape.

### Rejected explanations (tested, not assumed)

| Explanation | Test | Result |
|---|---|---|
| Silkscreen | luminance 191, width 5–8 px, rounded ends | **rejected** — features are lum 89–94, 3 px |
| Compression ringing | ringing is parallel to the bright edge (horizontal) and decays with distance | **rejected** — features are at 45°; orientation histogram shows a single 45° family, no 135° counterpart |
| Cross-hatched copper pour | a hatch would show both diagonal families | **rejected** — no 135° family (2 counts vs 127 at 45°) |
| Pour boundary | pour edges follow polygon outlines, not constant-spacing pairs | **rejected** for the pair; **accepted** for the bright region below y ≈ 1100 |
| Wide trace with edge highlights | a wide trace shows two bright edges with an elevated centre | **accepted** for part of the six-trace set — their inter-line level is 78–83 vs 61 background, unlike the pair (67.5), so some of the six may be three wide traces, not six thin ones |

---

## A1.6 Evidence table

| # | Statement | Class |
|---|---|---|
| 1 | Six long copper traces run in the strip below the FX3, in three near-pairs 0.29–0.30 mm apart | **Directly observable** |
| 2 | P1 continues past the package on both sides (x 620 → 1680); P2 reaches x ≈ 1570; P3 is lost in the pour | **Directly observable** |
| 3 | A two-trace 45° coupled pair (0.41–0.46 mm spacing) ascends to the package edge and stops | **Directly observable** |
| 4 | The pair bends into the horizontal channel at ≈(1300,1085) | **Directly observable** |
| 5 | No 0.1 µF series pair, no SS ESD array, no SS test pad on either face | **Directly observable** |
| 6 | The pair is copper under mask, not silkscreen/ringing/hatch | **Strong inference** (calibrated classifier + three rejected alternatives) |
| 7 | The six traces are through-routes, not FX3 escapes | **Strong inference** (continuity on both sides) |
| 8 | The pair's landing is 1403/1419 at the package edge | **Strong inference** (±10 px) |
| 9 | The pair does not match A3/A4 or A5/A6 within uncertainty | **Strong inference** (2.6–3.3 σ) |
| 10 | The pair connects to specific balls A3–A6 | **Requires electrical test / X-ray** — cannot be read optically |
| 11 | Where the pair terminates (J4 pin? via? inner layer?) | **Requires electrical test** |
| 12 | Whether R_USB3 (200 Ω, B3) is fitted | **Requires electrical test** |
| 13 | Whether the exposed pad cluster (O5) is populated with anything | **Impossible from photograph alone** (needs the bare-board artwork or X-ray) |
| 14 | Whether the SS PHY is biased and could ever train | **Requires electrical test** |

---

## A1.7 Revised verdict

> **The NO-GO for a passive modification stands — but the reasoning in the main report was too strong
> in one place and must be corrected.**
>
> * The board is **not** "empty" under the package. There is a real coupled pair escaping the FX3's
>   bottom edge, and six further copper traces in the same channel.
> * That pair **cannot be assigned to A3–A6** on the available evidence, and it fits no valid
>   SuperSpeed pair. Only one pair exists where two are required.
> * The mandatory SuperSpeed components — 0.1 µF SSTX series pair, ≤0.5 pF ESD array — are **still
>   absent**, and the 7-pin connector still has one spare contact against four required.

The practical conclusion therefore survives, but its probability weighting changes:

| Option | Before | After | Why |
|---|---|---|---|
| C — tap vias with a flying lead | no-go | **no-go** | unchanged (SI), and no verified tap point exists |
| E — new daughterboard if SS reaches J4 | ~10–15 % | **~25–35 %** | a coupled pair demonstrably exists and heads toward J4, though it fits no valid ball pair |
| F — board re-spin | the only clean path | **unchanged** | — |

The single sentence to carry forward: *something* is routed from the FX3's lower edge toward the
connector, but it is not demonstrably SuperSpeed, it is not a complete pair set, and it lacks every
mandatory passive component.

---

## A1.8 The three decisive unpowered measurements

All three are on accessible copper, none requires probing under the BGA, none is destructive.

### M-A · The 200 Ω sweep (decisive, unchanged)

Find R_USB3 on B3. Sweep every 0402/0603 resistor within ~12 mm of the FX3 with the black lead on a
ground ring, as in `hardware/annotated/A7_multimeter_procedure.png`.

* **6.04 kΩ found** → method validated (R_USB2 on C8).
* **200 Ω found** → the SS PHY is biased; the routed pair is probably intended as SuperSpeed; proceed
  to M-C.
* **No 200 Ω** → the PHY has no calibration reference. **No routing can make it work.** The NO-GO
  becomes final and no further measurement on this board is worth doing.

This remains the highest-information measurement per minute of effort.

### M-B · Channel continuity: does the pair reach J4?

With the board unpowered, in continuity mode, probe between **J4 pins** and the **exposed pad cluster
at x 150–330, y 1060–1120** (observation O5), and between J4 pins and the ground ring.

* A J4 pin that is **DC-continuous to ground** is a ground/power pin.
* A J4 pin that shows a **0.4–0.7 V diode drop to ground in one direction only** is connected to an IC
  (FX3 or FPGA) — this is the signature of a live signal pin.
* A J4 pin that is **open in both directions** carries nothing, or only goes to a passive.

This maps which of the 40 J4 contacts are live signal pins and which are spare. **What it establishes:**
whether there is any spare conductor at all in the mainboard→daughterboard link, which is the
precondition for option E. **What it does not establish:** which pin is SSTX or SSRX.

### M-C · Differential-pair identification at J4 (only if M-A finds 200 Ω)

Measure **capacitance pin-to-pin** across J4 pairs with a DMM that has a pF range (or an LCR meter).

* A pair reading **≈ 0.05 µF** (two 0.1 µF caps in series from the far side, or one cap plus trace)
  would indicate AC-coupled SuperSpeed TX.
* A pair reading **2–10 pF** is an ordinary signal pair.
* A pair reading **open** is unconnected.

**What it establishes:** whether any J4 pair is AC-coupled — the one electrical signature that
distinguishes an SSTX pair from every other pair on the board.

---

## A1.9 What would settle it definitively

A **2D X-ray of the J4-to-FX3 region**, taken from the component side. It resolves, in one image and
non-destructively:

1. whether traces leave balls A3–A6 (visible as copper converging on those ball pads);
2. whether B3 carries a trace to a 200 Ω resistor;
3. where the diagonal pair terminates — via, J4 pin, or inner layer.

No optical method can substitute for it. The photographs are at 37.5 px/mm with 3 px traces and
compression; the region of interest is 1.6 mm tall and covered by opaque silkscreen exactly where the
traces emerge.

---

## A1.10 Direct answers to the seven questions

**1. Are the four diagonal features actually copper traces?**
**Two of them are, one pair, confirmed.** The pair measures 45.2°/45.9°, 0.41–0.46 mm spacing, 3 px
width, +30 luminance over mask — the calibrated copper signature — and three alternatives (silkscreen,
compression ringing, hatched pour) were tested and rejected. **The other two are not established as
diagonal copper.** The diagonal-looking marks elsewhere in that strip are the through-routes crossing
the silkscreen bands and the bend at (1300,1085); they are horizontal copper, not a second diagonal
pair. Net finding: **one coupled pair, not two.**

**2. Are their locations geometrically consistent with A3–A6?**
**Partially — and not well enough to claim it.** The left trace extrapolates to x = 1403, which is
3.5 px from A5 (1399.5). But the pair's centre (1411) sits 26.5 px from the A5/A6 midpoint and
33.5 px from the A3/A4 midpoint — 2.6 σ and 3.3 σ respectively at ±10 px uncertainty. The only ball
pairing it matches (A4/A5) is not a valid differential pair. **Consistent in position, inconsistent in
pairing.**

**3. Is there evidence of two matched SuperSpeed differential pairs?**
**No.** Exactly one matched pair exists in the region. A SuperSpeed device needs TX *and* RX; a single
pair cannot form a link. This is the strongest single argument against Hypothesis A.

**4. Where could those signals physically terminate?**
Unresolved, and honestly so. The pair bends at ≈(1300,1085) into the horizontal channel and runs left
toward J4 (x 365–595), but the channel carries at least three other pairs, and the region where the
traces would turn up to J4 is obscured by components and by J4's own footprint. **No via termination
could be resolved.** Candidates: a J4 pin, a blind/buried via to an inner layer, or the exposed pad
cluster at x 150–330, y 1060–1120 (observation O5, function unknown).

**5. Does the board appear to have the necessary SuperSpeed PHY support circuitry?**
**No.** Mandatory: 0.1 µF series pair on SSTX — **absent**. Recommended: ≤0.5 pF ESD array —
**absent**. Required: 200 Ω ±1 % on B3 — **not determinable optically** (this is exactly what M-A
settles). The USB 2.0 reference (6.04 kΩ on C8) is necessarily present but was not individually
identified either.

**6. Does the prior NO-GO verdict still hold, or should it be revised?**
**It holds, with an amended rationale.** The verdict was right; the reasoning was partly wrong.
"Nothing escapes the package" is false — a coupled pair does. But a single unattributable pair,
without AC coupling, without ESD, fitting no valid ball pair, cannot make a link. The probability
assigned to option E (new daughterboard, mainboard untouched) rises from ~10–15 % to **~25–35 %**.

**7. What single additional measurement would most decisively change the answer?**
**The 200 Ω sweep (M-A).** It is the only accessible measurement whose outcome is binary and
conclusive:

* **200 Ω present** → the SS PHY is biased, the routed pair is probably intended as SuperSpeed, and an
  X-ray becomes worth commissioning. Option E is alive.
* **200 Ω absent** → the PHY has no calibration reference. *No amount of routing can make it train.*
  The NO-GO becomes final, and every other measurement on this board becomes pointless.

Everything else — continuity mapping, capacitance, even the X-ray — only refines a question that this
one resistor answers outright.

---

## A1.11 Reproduce

```bash
cd <repo>
python3 hardware/make_addendum_figs.py          # B1, B2
```

Figures: `hardware/annotated/B1_escape_pair_measured.png` (pair geometry and landing points),
`hardware/annotated/B2_routing_channel.png` (the six through-routes).
Raw crops used: `hardware/crops/pair_detail.png`, `pair_wide.png`, `bend_precise.png`,
`copper_map.png`, `track_scatter.png`, `six_lines_zoom.png`, `left_edge_structure.png`.
