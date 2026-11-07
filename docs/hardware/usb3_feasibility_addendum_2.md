# Addendum 2 — Two SuperSpeed pairs, not one

**Reopens and corrects the hardware feasibility study of 2026-11-07.**
Read-only photographic re-analysis, 2026-11-08. No hardware was powered, probed, soldered or
modified. This document supersedes `usb3_feasibility_addendum_1.md` §A1.5–A1.8 and corrects
`usb3_mainboard_feasibility.md` §3.6, §4 row 1 and the verdict.

> **Status: the NO-GO for a *passive external adapter* stands. The NO-GO for a *custom
> daughterboard* is withdrawn and replaced by a conditional GO.**
> The condition is a set of safe, unpowered continuity measurements (§A2.7), not a re-spin.

---

## A2.0 Why this addendum exists

A new physical inspection reports that the traces under the CYUSB3014 may connect **directly to
balls A4 and A6**, contradicting the earlier photographic conclusion. That report is treated here
as **unverified evidence**, not as a premise. The photographs were re-measured from scratch, with
no prior coordinate or conclusion assumed. The result is not the one the earlier study reached, and
it is not exactly the one the new inspection reports either:

* the package lower edge carries **four** conductors in **two coupled pairs**, not two conductors
  and not a single pair;
* the four conductors extrapolate, in order and with a symmetric inward convergence, to **A6, A5,
  A4 and A3** — all four SuperSpeed balls;
* the pair that the first addendum found (crossings at x = 1396.0 / 1411.4) is the **RX pair**;
  the **TX pair** at x ≈ 1331 / 1347 was missed;
* the TX pair passes a **two-pad footprint** that sits **across** the pair, not in series with
  either line: both traces are continuous on both sides. It is an unpopulated footprint or a
  differential shunt, **not** an AC-coupling capacitor. No on-board series AC coupling is proved;
* all four conductors route left and up into the via field beside the component-side 40-pin
  connector.

The last 1 mm under the package is still invisible, so the ball-to-trace assignment remains an
inference from geometry, not a direct observation. §A2.8 states exactly what would falsify it.

---

## A2.1 The source is weaker than the earlier reports implied

`hardware/photos/P1_top_side.webp` is a **VP8 lossy** WebP, 2341 × 1199 px, 176,196 bytes.

| Quantity | Value | Consequence |
|---|---|---|
| Pixels | 2.81 M | — |
| Bit rate | **0.50 bit/pixel** | heavy compression for a PCB image |
| Chroma | **4:2:0** (VP8) | colour edges have half resolution |
| Transform | 4 × 4 (VP8) | ringing is local, not purely 8 × 8 |
| Scale | 37.01 px/mm | 0.1 mm = 3.7 px; 0.15 mm = 5.6 px |

The earlier reports quoted sub-pixel positions (0.1 px r.m.s. on edges, 45.0° ± 0.5° on traces) and
widths to 0.1 px. Those numbers are **statistical fits to compressed pixels**, not physical
accuracy. A realistic per-feature position uncertainty in this file is **±1–2 px (0.03–0.05 mm)**,
and a 5–6 px trace is at the edge of what the codec preserves.

This does **not** invalidate the new finding, because the new finding is a *large-scale* feature:
four parallel lines with 16 px and 43.5 px spacing, stable over 18 rows, which is far above the
codec's noise floor. It does invalidate any claim that depends on 0.1 px.

A second, independent limit: **there is only one top-side photograph**. The bottom-side images
(P2–P4) cannot corroborate the top-side copper, so the four-trace result rests on one lossy frame.
That is why §A2.7 starts with an electrical test, not another image measurement.

---

## A2.2 The four conductors, measured

Method: luminance `L = 0.299R + 0.587G + 0.114B`; per-row ridge peaks against a rolling median
background (±15 px); sub-pixel peak by three-point parabola; Hough transform over the gap between
the two silkscreen lines (rows y = 1008–1026), where the package shadow does not reach.

The Hough transform finds **four** lines at slope `dx/dy = −0.85`:

| line | c = x + 0.85·y | x at y = 992 (package edge) | x at y = 955 (row A) | nearest ball | offset |
|---|---|---|---|---|---|
| T1 | 2174.1 | **1330.9** | **1362.4** | **A6** = 1356.5 | +5.9 px |
| T2 | 2190.6 | **1347.4** | **1378.9** | **A5** = 1386.1 | −7.2 px |
| T3 | 2234.1 | **1390.9** | **1422.4** | **A4** = 1415.7 | +6.7 px |
| T4 | 2249.1 | **1405.9** | **1437.4** | **A3** = 1445.3 | −7.9 px |

Per-line fit residual: **0.63–1.01 px r.m.s.** over 10–18 rows. The four lines are parallel within
0.01 in slope. **Caveat on the assignment:** the table uses the local gap slope, −0.85. If the traces
are drawn at exactly 45° (slope −1.00, as agents B and C measure), the straight-line extrapolation
puts the inner traces on A5 and A3 and the outer traces ~0.5 mm to the right of A6 and A4; the outer
traces would then need an inward dogleg under the package to reach A6/A4. The pair centres and the
one-to-one order are unchanged, but the last 1 mm is hidden, so the **ball-to-trace assignment is an
inference, not a measurement**. An independent fit at slope −1.00 (agents B and C) gives the same four lines at
x(y=992) = 1335.3 / 1351.0 / 1395.3 / 1410.3 and c = x + y = 2327.3 / 2342.4 / 2387.1 / 2402.8;
the ~4 px difference from the values above is the slope convention, not a disagreement about the
objects. The **perpendicular** within-pair spacing is **10.7 and 11.1 px = 0.29–0.30 mm**; the
between-pair perpendicular spacing is **31.6 px = 0.85 mm**. (The earlier 0.42 mm figure was the
spacing measured along x, i.e. a √2 overestimate.)

An independent Radon projection along the same slope (mean residual over rows y = 1008–1026)
confirms the same four peaks at c = 2174.25, 2190.25, 2234.25 and 2249.50, at **5.3–6.5σ** over a
3.6 DN MAD baseline. The only other peaks in the window are the silkscreen/corner features at
c ≈ 2071 and 2098–2106, outside the four-line group.

A second independent pass (agent C, [`C_forensics.md`](../../hardware/analysis/reopen/agents/C_forensics.md)) re-encoded the image at qualities 100,
95, 90, 87, 85, 83, 80, 75, 70, 60, 50, 40 and 30 and re-ran the line fit each time. Using a
slope-1.0 convention (c = 2327.3 / 2342.4 / 2387.1 / 2402.8, the same four lines), every line stays
within **±0.5 px** of its quality-100 position down to q30, with amplitudes of 14–20 DN. The four
lines are therefore **not codec artefacts**; they survive 3.3× more compression than the source
file already carries.

### The convergence is the decisive signature

At the balls the four sites are equally spaced at 0.80 mm. For 45° escapes from adjacent balls the
perpendicular spacing would be 0.80/√2 = **0.57 mm**; at the package edge it is **0.29–0.30 mm**.
Each pair has therefore converged by about 2:1 over the 1 mm from the ball row to the package edge,
and the two pairs are 0.85 mm apart (perpendicular). The offsets in the table are **symmetric**:
each trace is displaced *toward the centre of its own pair*. That is exactly a differential-pair
fan-out.

A set of four unrelated traces would not (a) land within 8 px of four adjacent balls, (b) preserve
their order, (c) converge symmetrically in pairs, and (d) split into the two spacings that the two
SuperSpeed pairs require. The probability of that by chance is small; the finding is rated **high
confidence (≈0.9)** as *four routed conductors in two pairs aligned with A3–A6*.

### The symmetry that makes it difficult to dismiss

The **centre of each pair** is displaced from the **centre of its ball pair** by the *same* amount:

| pair | pair centre at edge (px) | ball-pair centre (px) | displacement |
|---|---|---|---|
| TX (A6/A5) | (1330.9 + 1347.4)/2 = **1339.2** | (1356.5 + 1386.1)/2 = **1371.3** | **−32.1 px** |
| RX (A4/A3) | (1390.9 + 1405.9)/2 = **1398.4** | (1415.7 + 1445.3)/2 = **1430.5** | **−32.1 px** |

Two independent pairs, shifted by the *same* 32.1 px (0.87 mm), with the same 16/15 px intra-pair
convergence. (The independent slope-1.0 fit gives −29.3 and −28.2 px instead, still symmetric within
1.1 px; the symmetry does not depend on the slope convention.) That is what a common 45° escape from a common ball row produces; it is not what
unrelated traces, hatched copper or codec artefacts produce. This symmetry is the single strongest
quantitative argument in this addendum.

The ball coordinates were re-derived independently (agent A, [`A_geometry.md`](../../hardware/analysis/reopen/agents/A_geometry.md)): A6 = 1357.8,
A5 = 1387.1, A4 = 1416.4, A3 = 1445.7 at row y = 952.1, within **1.3 px in x** of the values used
above (the 2.4 px y difference is a body-edge-definition systematic, not a disagreement about the
grid). The four-line assignment is unchanged by using either calibration.

### What the first addendum measured

`usb3_feasibility_addendum_1.md` reported direct crossings at **x = 1396.0 and 1411.4**. Those are
T3/T4 (1390.9 / 1405.9) within 5 px — i.e. the **RX pair (A4/A3)**. The addendum sampled the two
predicted traces and did not perform a full-row scan, so T1/T2 (the **TX pair, A6/A5**) were never
seen. The blind pass missed them for the same reason: its ROI/threshold was tuned to the two lines
it had already found.

The original report's sentence "there is no bundle of four" is therefore **wrong**, and the
addendum's "only one pair exists where two are required" is **wrong in the same way**.

---

## A2.3 Where the four conductors go

### A2.3.1 The two pairs become two horizontal runs

Following each line down-left out of the package shadow:

| pair | balls | horizontal run | evidence |
|---|---|---|---|
| TX | A6 / A5 | **y = 1029 and 1041** | continuous x ≈ 460 → 1300, contrast +25…+45 DN, width 5–6 px |
| RX | A4 / A3 | **y = 1084 and 1096** | continuous x ≈ 400 → 1300, contrast +25…+40 DN |

The two runs are 12 px apart within a pair and 55 px apart between pairs — the same two-pitch
structure as at the package edge. At their right ends the runs bend 45° up into T1–T4.

This is the corrected form of addendum 1's "C3/C4/C5" observation. It saw the RX pair and one
extra trace (C5 at y = 1041); it did not resolve the TX pair's second line.

### A2.3.2 The TX pair passes a two-pad footprint — but not in series

At **x ≈ 376–470, y ≈ 988–1084** there is a silk-outlined **two-pad footprint**. The pads are
x ≈ 394–452; the upper pad spans y ≈ 1005–1028, the lower pad y ≈ 1040–1064. The pad axis is
**vertical**, i.e. perpendicular to the two horizontal TX traces at y = 1029.5 and 1040.5, and the
pads' inner edges sit on those traces (upper pad lower edge 1.5 px from the y = 1029.5 trace; lower
pad upper edge 0.5 px from the y = 1040.5 trace).

The decisive measurement is **continuity**: both traces are present on **both** sides of the
footprint (ridge peaks at x = 370–390 and x = 470–520 at the same y), and no trace enters either pad
from left, right, top or bottom. The footprint therefore sits **across** the pair, not in series with
either line. It is either an **unpopulated two-terminal footprint** (the gap between the pads shows
soldermask, not a moulded body) or a **differential shunt**; it is **not** an AC-coupling capacitor.

This corrects this addendum's own first reading. The initial pass saw two bright pads and a silk
frame and inferred two series 0805 parts; the higher-resolution continuity check (agent B,
[`B_traces.md`](../../hardware/analysis/reopen/agents/B_traces.md) §5) shows the traces pass through, with the pads tapped off the two lines. The
series interpretation would require each trace to be interrupted by a pad, and neither is.

Consequences:

* the earlier claim "no 0.1 µF series pair exists" was a **correct observation**. The original
  report's error was the inference drawn from it, not the observation;
* the TX pair is **DC-continuous** from the FX3 to the connector-side via field (subject to the
  inner-layer routing). If this board is to carry a compliant SS link, the mandatory AC coupling
  must live on the **daughterboard**. The USB 3.1 ECN permits coupling "within the media or within
  the receiving component", and AN70707's placement rule is topology-dependent, so that is legal —
  but it is off Infineon's recommended placement and it is SI-marginal (agent D §3.5);
* the two pads are a **convenient, accessible probe point** on the TX pair for M1: they are exposed
  copper on the two lines and large enough for a fine probe.

The footprint's function is not established. A common-mode choke, a differential termination, an
ESD device or a test-pad pair would all look similar; only an electrical check (M1) can type it.

### A2.3.3 The four lines reach the connector-side via field

From the footprint and the RX bends, the four lines run up the left corridor as four parallel traces at
**x ≈ 282 / 294 / 324 / 335**, 11–12 px within each pair and 30 px between pairs. They continue
from y ≈ 1000 to y ≈ 620–700, where they bend left into a field of vias at **x ≈ 240–270**.

The via field sits immediately left of the **component-side 40-pin board-to-board connector**
(body x ≈ 365–595, y ≈ 215–880). The final connection from those vias to specific connector pins is
on inner layers and is **not visible**. The routing direction, the proximity and the four-trace
bundle make the connector the most probable destination, but it is **not proved** by the photograph.
This is the single largest remaining uncertainty in the optical case, and §A2.7 M1/M2 is designed to
settle it with a multimeter.

> **A note on which connector.** The board has a component-side 40-pin board-to-board connector
> (the one the SS pairs approach) and a solder-side 40-pin FPC connector labelled **J4** (P5). The
> earlier report treated them as one connector seen from both sides; the photographs show
> different bodies at different positions, so they **appear to be two different parts**. The SS pairs
> route to the **component-side** connector. Whether that connector is the host
> interface or the sensor-head interface must be confirmed from the product harness, not from these
> photographs.

---

## A2.4 B3, the PHY supplies, and the 128 Ω paths

### A2.4.1 B3 / R_USB3

B3 (`R_USB3`, 200 Ω ±1 % to GND) is at image (1445.4, 925.3), 1.0 mm inside the package's lower
edge, in the same column as A3. Its escape is invisible under the package.

One candidate is visible: a **0.4 mm via at (1428, 1040)**, just outside the package corner, with a
faint trace approaching from the package edge above the courtyard silk. It is 0.47 mm left and
3.1 mm below B3, and is the only via in the immediate bottom-right corner. It is **not proved** to
be B3:

* a straight-down escape from B3 would cross the edge near x = 1445, 17 px right of the via;
* a 45° down-left escape would cross near x = 1378, inside the RX-pair region;
* a near-vertical escape with an 8° dog-leg under the package would reach the via exactly.

The decisive test is electrical (§A2.7 M3): measure the via to ground. **≈200 Ω** means the SS PHY
reference is fitted; **open** means it is not, and no amount of routing can make the PHY train.

### A2.4.2 PHY supplies

The four SS balls are powered by **A2 (U3RXVDDQ)** and **B5 (U3TXVDDQ)**, both 1.15–1.25 V, plus
**A1 (U3VSSQ)** ground. AN70707 Appendix C requires both rails to be powered **even in a USB-2.0-only
design**; they are not optional. Since the FX3 is running and enumerating as a USB 2.0 device, the
1.2 V core/PHY rails are necessarily present at the device. "The SS PHY has no supply" is therefore
not a plausible blocker. The remaining supply question is only whether the *SS-specific* filter
components (ferrite bead + 22 µF/0.1 µF per rail) are fitted, which affects signal quality, not
whether the PHY can be biased.

### A2.4.3 The 128 Ω paths

A prior unpowered measurement reportedly gave **≈128 Ω** on some paths; the exact probe points are
not recorded here. 128 Ω is **not** direct copper continuity and it is **not** a series AC-coupling
capacitor (which is open at DC). Plausible causes and their signatures:

| cause | 2-wire | 4-wire | reverse polarity | diode mode | each line to GND | capacitance |
|---|---|---|---|---|---|---|
| direct trace | < 1 Ω | < 1 Ω | same | 0 V | open / other net | open |
| 0 Ω link, CMC winding | < 1 Ω | < 1 Ω | same | 0 V | open | open |
| ferrite bead | DCR 0.05–0.5 Ω | same | same | 0 V | open | open |
| 100 Ω differential termination | ≈100 Ω | ≈100 Ω | same | 0 V | ≈50 Ω each | few pF |
| 130 Ω resistor (E24) | ≈130 Ω | ≈130 Ω | same | 0 V | open | open |
| series 0.1 µF cap | open / charging | open | open | 0 V | open | ≈0.1 µF |
| ESD diode / IC clamp | varies | varies | **asymmetric** | 0.4–0.7 V | diode | few pF |
| ground-network parallel path | low | low | same | 0 V | < 1 Ω | open |
| meter/contact/probe error | high, unstable | lower | same | 0 V | — | — |

**Reading:** 128 Ω is closest to one of three things:

* the **DC differential termination of a connected SSRX pair** — the USB 3.x receiver's
  `R_RX-DIFF-DC` window is 72–120 Ω, and 120 Ω plus a few ohms of trace/probe lands at ≈128 Ω.
  **If the probes were across the RX pair, this reading is evidence that the pair reaches the
  PHY** — the opposite of the earlier interpretation;
* a **130 Ω (E24/E96) resistor**, or a 127/129 Ω E192 part, with lead error;
* **200 Ω ∥ ~350 Ω** — e.g. R_USB3 (200 Ω) with the unpowered die shunting it.

It is *not* consistent with a direct ball escape (< 1 Ω) and *not* with the series-capacitor TX pair
(open at DC). So the 128 Ω paths do **not** establish a direct copper connection to A4/A6; they are
either an RX termination (good news) or a resistor/semiconductor/ground path (neutral). Without the
probe points the number cannot be assigned. §A2.7 M4 is the disambiguation procedure; the full
hypothesis/decision table is in [`D_electronics.md`](../../hardware/analysis/reopen/agents/D_electronics.md) §6.

The new physical claim ("A4 and A6 direct") and the 128 Ω measurement are therefore **in tension**:
the optical evidence supports *four routed conductors aligned with A3–A6*, while 128 Ω is too high
for any of them to be a direct DC short. Both cannot be true of the same two probe points. The
resolution is either that the 128 Ω points were not on the SS traces, or that the reading passes
through the two-pad footprint (TX) / the FX3's internal network (RX).

---

## A2.5 Net-by-net connectivity map

Figures: [`N1_edge_netmap.png`](../../hardware/analysis/reopen/figs/N1_edge_netmap.png) (edge close-up) and
[`N2_route_netmap.png`](../../hardware/analysis/reopen/figs/N2_route_netmap.png) (full route). Coordinates are source-photo pixels.

| net (provisional) | ball | edge crossing y=992 | gap line c | horizontal run | footprint across TX | left corridor | destination | confidence |
|---|---|---|---|---|---|---|---|---|
| SSTX− | **A6** 1356.5 | 1330.9 | 2174.1 | y = 1029 | upper pad (across pair) | x ≈ 282 | connector via field | high / pin unconfirmed |
| SSTX+ | **A5** 1386.1 | 1347.4 | 2190.6 | y = 1041 | lower pad (across pair) | x ≈ 294 | connector via field | high / pin unconfirmed |
| SSRX+ | **A4** 1415.7 | 1405.9 | 2249.1 | y = 1084 | none | x ≈ 324 | connector via field | high / pin unconfirmed |
| SSRX− | **A3** 1445.3 | 1390.9 | 2234.1 | y = 1096 | none | x ≈ 335 | connector via field | high / pin unconfirmed |
| R_USB3 | **B3** 1445.4 | via (1428,1040)? | — | — | 200 Ω? | — | GND | **unknown** |

Two entries in this table are **provisional** and must not be used as a pin map without the
measurements of §A2.7:

* **polarity** — which of A5/A6 reaches y = 1029 vs 1041, and A3/A4 y = 1084 vs 1096, is not
  resolved to better than one trace pitch;
* **corridor x** — the four traces at x ≈ 282/294/324/335 are a bundle that includes the SS pairs
  and at least one other signal; the via-to-pair mapping through (325,1020)/(365,1060) is not
  one-to-one in the image. The pairs certainly enter the corridor; which corridor trace is which
  is inner-layer routing and is not visible.

Neither affects the feasibility question — both members of a pair are the same signal pair — but
both must be fixed by continuity before a daughterboard pin map is committed.

### A2.5.1 The operator's six-point continuity report

After the optical analysis, an annotated inspection photograph was supplied
([`user_continuity_overlay.png`](../../hardware/analysis/reopen/evidence/user_continuity_overlay.png)) reporting **six connector-side continuity points**,
arranged as **three pairs**, that "beep to suspected USB3 pins" on the FX3 side. It also marks the
two-pad footprint as a "series component in lower path" and the corridor as a "suspected six-trace
corridor".

**What it corroborates.**

* the route from the FX3 lower edge, left along the strip, to the component-side connector;
* **six** conductors in the corridor, not four — the optical pass already found four SS traces plus
  at least one more in the same bundle (agent B: 282.8 / 293.8 / 323.8 / 334.9 / 358.7 px);
* the connector as the destination: the six points are on the connector's pin row.

**What it does not establish.**

* the exact pin numbers or net names (the annotation itself says so);
* that the six points are six SS conductors. The SS interface needs **four**; six is two more, so
  the set is more likely **SSTX + SSRX + the USB 2.0 DP/DM pair** (or a third pair), not six
  SuperSpeed lines;
* that the two-pad footprint is **in series**. The annotation labels it "series component", but the
  continuity check in §A2.3.2 shows both traces continuous on both sides; a beep from the connector
  to the FX3 side passes along the trace, not necessarily through the part. "Series" is an
  interpretation, not a measurement.

**How it changes the confidence.** The optical case for "the SS pairs reach the connector" was
**probable (≈0.6–0.7)**. The six-point report raises it to **likely (≈0.75–0.85)**: an independent
electrical observation found six connector-side points that beep toward the FX3 region. It does not
raise it to proof, because the points' nets are unidentified and a beep can pass through a component
or a ground path. It does not change the B3 gate: a continuity beep says nothing about whether the
200 Ω reference is fitted.

---

## A2.6 Revised feasibility

### What is now established (optical, high confidence)

1. Four conductors, not two, leave the FX3's lower edge in two coupled pairs.
2. Their geometry maps them, in order and with symmetric pair convergence, to **A3–A6**.
3. The two pairs route left across the board as two horizontal runs.
4. The TX pair passes a two-pad footprint **across** the pair (not in series); the RX pair has no
   equivalent footprint. **No on-board series AC coupling is proved.**
5. All four route into the via field beside the component-side 40-pin connector.
6. An operator continuity report finds **six connector-side points in three pairs** that beep toward
   the FX3 region — consistent with **SSTX + SSRX + a third pair** (most likely USB 2.0 DP/DM), and
   with the connector being the interface.

### What is not established

* the last 1 mm under the package (ball-to-trace);
* the specific connector pins (inner-layer routing);
* the type/value of the two-pad footprint (shunt, CMC, termination or unpopulated);
* whether B3 is fitted;
* the board stack-up / differential impedance;
* whether the component-side connector is the host interface.

### Verdict

| option | old verdict | new verdict | reason |
|---|---|---|---|
| A — passive tap on the existing 7-pin cable | no-go | **no-go** | one spare conductor against four; unchanged |
| C — flying-lead tap at the FX3 | no-go | **no-go** | SI budget; no accessible ball |
| **E — custom daughterboard on the component-side 40-pin connector** | ~15–20 % | **conditional GO — materially higher, not yet quantified** | the SS pairs and the connector-side via field are present, and the TX pair is DC-continuous; the daughterboard must add the AC coupling and ESD; only continuity and B3 remain as gates |
| F — board re-spin | only clean path | **no longer the only clean path** | the mainboard may already carry the channel |

The conditional GO means: **practical without BGA rework, if and only if** M1/M2 identify two
connector pins per pair with < 1 Ω continuity and M3 finds the 200 Ω B3 reference. The operator's
six-point report (§A2.5.1) raises the probability of the first gate; it says nothing about the
second. If either fails, the NO-GO becomes final for this board.

The 7-pin external connector remains a hard limit for *re-using the stock cable*. A custom
daughterboard would replace the stock harness, not re-pin it.

---

## A2.7 The decisive safe unpowered measurements

All are 0 V, no soldering, no firmware. Ranked by information per risk.

### M1 — TX pair continuity and the two-pad footprint (highest value)

Probe each of the two exposed pads — upper pad at image (394–452, 1005–1028), lower pad at
(394–452, 1040–1064) — against each of the 40 connector pins. Each pad is tapped off one line, so the
two pins that read **< 1 Ω** are SSTX+ and SSTX−. Then measure **pad to pad**: **open / few pF** ⇒
unpopulated footprint; **≈100 nF** ⇒ a capacitor is fitted across the pair (a differential shunt, not
compliant AC coupling); **< 1 Ω** ⇒ a 0 Ω link or CMC across the pair. This single test both maps the
two lines to the connector and types the footprint.

### M2 — RX pair to connector continuity

Probe the exposed RX runs at x ≈ 700, y = 1084 / 1096 (fine needle), or the vias at (1348, 1085)
and (1420, 1090), against each connector pin. The two pins reading **< 1 Ω** are SSRX+ and SSRX−.
There should be no series part: a direct trace reads < 1 Ω end to end.

### M3 — B3 sweep

Measure every 0402/0603 resistor within ~10 mm of the FX3 to the ground ring. A **200 Ω ±1 %**
reading identifies R_USB3. Also measure the via at (1428, 1040) to ground: **≈200 Ω** is strong
evidence that this via is B3's escape. **No 200 Ω part anywhere** ⇒ the SS PHY has no reference;
the conditional GO fails.

### M4 — the 128 Ω disambiguation

For each previously measured 128 Ω point: record the exact points; repeat 4-wire; reverse polarity;
diode mode; measure each point to three different ground points; measure capacitance across the
pair. Use the table in §A2.4.3. A reading that is asymmetric in polarity or that collapses to < 1 Ω
against a ground point is a semiconductor/ground path, not a signal trace.

### M5 — SS pin identity by diode mode

On the pins found in M1/M2, compare diode-mode readings to ground with a known USB 2.0 pin (DP/DM)
and a known unconnected pin. An IC-connected SS pin shows the FX3's ESD clamp (≈0.4–0.7 V one way,
open the other); a purely passive/ground pin does not.

### M6 — only if M1–M3 pass

TDR / impedance test of the identified connector pins, and a 2D X-ray of the FX3 lower edge to
confirm the four ball escapes and B3. These describe and qualify; they do not decide.

---

## A2.8 What would falsify this addendum

* A full-row re-scan of P1 at a lower threshold that finds the four lines are **not** independent
  (e.g. two edges of two wide traces) — then the "two pairs" reading collapses.
* A second top-side photograph at higher resolution or lossless format that shows the lines do not
  reach A3–A6.
* M1/M2 returning **no** < 1 Ω pin for a pair — then the pairs do not reach the connector, and the
  conditional GO fails.
* M3 returning **no** 200 Ω reference — then the SS PHY cannot calibrate, and the NO-GO is final
  regardless of routing.
* The two-pad footprint measuring **≈100 nF** across the pair — then it is a differential shunt, not
  series AC coupling, and the daughterboard still needs the two series caps (still feasible, but a
  different BOM).

---

## A2.9 Corrections to the earlier documents

| earlier statement | where | status |
|---|---|---|
| "There is no bundle of four" | main report §3.6 | **wrong** — four conductors in two pairs |
| "no 4-trace escape on row A" | main report §4 row 1 | **wrong** |
| "a coupled pair crosses at 1396.0 / 1411.4" | addendum 1 §A1.3 | **correct but incomplete** — that is the RX pair only |
| "only one pair exists where two are required" | addendum 1 §A1.6 | **wrong** — two pairs |
| "the pair fits no valid SS ball pair (27–32 px off)" | addendum 1 §A1.5 | **wrong** — each pair maps to A3/A4 and A5/A6 within 8 px after pair convergence |
| "no 0.1 µF series pair" | addendum 1 §A1.6, FINDINGS F65 | **correct observation, wrong inference** — the TX pair is DC-continuous through a two-pad footprint; the AC coupling must be on the daughterboard, which does not prove "no SS path" |
| "A1 is the top-left corner" | `fx3_ballout.md` §8 table | **wrong** — A1 is bottom-right (laser dot, triangle, arrows, marking) |
| "the four capacitors beside J4 are shunt decoupling" | FINDINGS F66 | **unchanged** — that bank is on the solder side and is still shunt |
| "the 7-pin connector has one spare contact" | FINDINGS F67 | **unchanged** |
| "B3 may be open in USB-2.0-only designs" | FINDINGS F68 | **unchanged, now decisive** |
| *this addendum's first reading:* "two series AC-coupling parts on TX" | §A2.3.2 (first draft) | **withdrawn** — the footprint is shunt/unpopulated; both traces are continuous. Corrected in §A2.3.2 |

The main report's §3.3 orientation proof, §3.4 shunt-bank proof, §6 connector arithmetic and the
firmware evidence are **not** affected.

---

## A2.10 Independent passes — what they agree and disagree on

Four independent passes were run against the same photograph without being given the earlier
conclusion. Their reports are in [`hardware/analysis/reopen/agents/`](../../hardware/analysis/reopen/agents/). This table is the audit trail, including where they
contradict this addendum.

| pass | agrees | disagrees / limits |
|---|---|---|
| **A — geometry** | A1 bottom-right (laser dot, both arrows, upside-down marking); rows A/B with A3–A6 and B3 within **1.3 px in x** of the values used here | body edge ~2.4 px higher in y; scale **36.6 px/mm** vs 37.0 (cross-checks 35.8–36.4); Pango/DDR3 dimensions partly inferred; ball pitch is a model because the balls are hidden |
| **B — trace topology** | **exactly four** conductors cross the edge, in two pairs (15.7 / 44.3 / 15.0 px); the left-corridor bundle and connector-side via field | slope **−1.00** vs this addendum's −0.85; **the two-pad footprint is shunt/unpopulated, not series** — this addendum's first reading was wrong and has been corrected in §A2.3.2 |
| **C — forensics** | four 45° ridges at the same `c` values; positions stable to **σ 0.15–0.2 px** across q30–100 and 10 re-encodes; exceed a 120-trial phase-randomised null; multi-image noise 0.4–0.5 px | the **0.42 mm** pitch was a √2 overestimate (perpendicular **0.29–0.30 mm**); widths 4.9–5.7 px, not 5.5–6.0; sub-0.1 px claims unsupported |
| **D — electronics** | A1 is **ground, not a rail**; AC coupling may legally live on the daughterboard; 128 Ω is not a trace and may be an RX termination; B3 sweep is decisive | did not examine the exact footprint at x 376–470 (its "decoupling-bank-shaped" remark refers to a different cluster) |

The one substantive contradiction — **series vs shunt** — was resolved in favour of **shunt** by a
direct continuity check: both traces are present on both sides of the footprint, and no trace enters
either pad from any other side. The four-conductor result is independent of that question and is
confirmed by both B and C. The 128 Ω interpretation and the B3 status remain open until measured.

---

## A2.11 Reproduce

```bash
python3 hardware/analysis/reopen/scripts/four_tracks.py     # four-line Hough + ball comparison
python3 hardware/analysis/reopen/scripts/netmap.py          # N1/N2 overlays
python3 hardware/analysis/reopen/scripts/enhance.py         # raw/high-pass/green/edge ROI
```

Independent passes: [`A_geometry.md`](../../hardware/analysis/reopen/agents/A_geometry.md) (ball grid),
[`B_traces.md`](../../hardware/analysis/reopen/agents/B_traces.md) (trace topology),
[`C_forensics.md`](../../hardware/analysis/reopen/agents/C_forensics.md) (compression envelope),
[`D_electronics.md`](../../hardware/analysis/reopen/agents/D_electronics.md) (SS front-end and measurement plan).

Figure files: [`N1_edge_netmap.png`](../../hardware/analysis/reopen/figs/N1_edge_netmap.png),
[`N2_route_netmap.png`](../../hardware/analysis/reopen/figs/N2_route_netmap.png), [`comp_deconv.png`](../../hardware/analysis/reopen/figs/comp_deconv.png), [`col_labels.png`](../../hardware/analysis/reopen/figs/col_labels.png).

---

## A2.12 One-paragraph answer to the original question

The Einstar mainboard is **not** a USB-2.0-only design around a SuperSpeed-capable FX3. It carries
two coupled SuperSpeed pairs from balls A3–A6, a two-pad footprint across the TX pair (not in
series), and a route to the component-side 40-pin connector. That is enough to make a **custom internal USB 3.0 interface
practical without BGA rework**, provided two safe continuity measurements identify the connector
pins and one resistor sweep finds the 200 Ω B3 reference. The stock 7-pin cable still cannot carry
SuperSpeed; the daughterboard, not the mainboard, is the thing to replace.
