# USB 3.0 SuperSpeed hardware feasibility — SHINING 3D Einstar mainboard (`EinScan10_01`)

**Subject** — the photographed mainboard of the original Einstar: Cypress/Infineon **CYUSB3014-BZX**
(EZ-USB FX3), **Pango Logos PGL25G** FPGA, Micron **D9PTK** DDR3L, Winbond **W25Q64JVSIQ** SPI flash,
one 40-pin board-to-board connector (**J4**), two buck converters.
**Question** — does this mainboard contain a usable SuperSpeed path that could be brought out to a new
USB 3.0 connection, bypassing the existing 7-pin external connector?
**Method** — official Infineon documentation (primary sources), quantitative photographic analysis of
both board sides, and the firmware/protocol evidence already established in this workspace.
**Nothing was soldered, flashed, powered or modified.** All conclusions are static.

---

## 0. Verdict

> ### **NO-GO** for adding USB 3.0 to this mainboard by tapping pads, populating missing parts, or swapping the cable.
>
> ### **GO** only as a board re-spin (new PCB reusing the FX3 + existing firmware), or — with low probability — as a new plug-in daughterboard *if* an X-ray/TDR measurement first proves that the SuperSpeed pairs are routed to J4.

Confidence in the NO-GO for a passive modification: **high (~90 %)**.
The residual 10 % is not "it might work anyway" — it is the possibility that the SS pairs are routed to
J4 for a planned USB 3.0 variant. Even in that case the stock daughterboard and 7-pin cable cannot carry
them (one spare conductor, four needed), so a new daughterboard **and** a new cable would still be
required, and the mainboard would not be modified at all.

**The single most important sentence in this report:** the FX3's four SuperSpeed balls sit on the
*outermost* row of the package, 1.0 mm inside a 10 × 10 mm BGA body, and the USB-2.0-only design
practice that Infineon itself documents (AN70707 App. C) is to leave those four balls **and** the
USB 3.0 reference ball B3 **open**. Everything visible on this board — no AC-coupling pair, no
SuperSpeed ESD array, no SS test pads, no 4-conductor path to any connector — is consistent with
exactly that design practice, and nothing is consistent with a routed SuperSpeed channel.

---

## 1. Scope, evidence, and what a photograph can and cannot prove

### 1.1 Evidence used

| Class | Evidence | Where |
|---|---|---|
| Primary vendor documentation | CYUSB301x datasheet **001-52136 Rev.\*Z**, 2022-09-29; AN70707 **001-70707 Rev.\*Q**, 2021-10-26; package drawing **001-54471 Rev.\*F** | `hardware/analysis/fx3_docs/`, `hardware/analysis/sources/` |
| Ball map (all 121 balls) | extracted, cross-checked against three sources, machine-readable | `hardware/analysis/fx3_ballout.md` / `.json` |
| Layout/SI requirements | 131 confidence-tagged claims from 12 documents | `hardware/analysis/fx3_ss_layout_requirements.md` |
| Product/teardown corroboration | no public teardown exists; official manual says USB 2.0; vendor software expects USB 3.0 | `hardware/analysis/product_teardown_corroboration.md` |
| Photographs | 5 supplied images (P1 top, P2–P4 bottom, P5 J4 close-up) | `hardware/photos/` |
| Firmware (already established) | SS descriptors, BOS, `CyU3PConnectState(1,1)`, `USB3_LNKFAIL` handling | `docs/protocol/usb3_capability.md`, `analysis/usb3/` |

### 1.2 The three hard limits of this evidence

These limits bound every conclusion below, and are stated once here rather than repeated.

1. **The balls are under the package.** A 121-ball BGA has its solder balls under a 10 × 10 mm
   laminate body; the outermost ball centres are 1.0 mm inside the body edge. Nothing about a ball's
   connectivity can be seen from above. What *can* be seen is whether a trace **emerges** from under
   the package edge — and only for signals that escape on the top copper layer rather than through a
   dog-bone via.
2. **Inner layers are invisible.** This is a multi-layer board (the density and the two-plane routing
   around J4 imply at least 6 layers; AN70707 requires ≥ 4). A signal can leave a ball, enter a via
   inside the 1 mm band under the package, and travel on an inner layer without ever appearing on
   either photographed surface. **An unseen connection is not evidence of absence** — this report does
   not treat it as such.
3. **Resolution.** The photographs are 2341 × 1199 px for a board that measures ≈ 63 mm across the
   frame, i.e. **≈ 37 px/mm** (calibrated on the FX3's 10 mm body = 370 px, cross-checked against the
   FPGA's 22 × 22 ball field). A 0.1 mm trace is 3.7 px wide; a 0.8 mm ball pitch is 29.6 px. Traces
   are resolvable as *paths*, not as *measurable widths*. The images are also heavily compressed
   (176–295 kB WebP), which softens exactly the 3–4 px features that matter.

### 1.3 Terminology

* **SS** — SuperSpeed, USB 3.2 Gen 1, 5 Gb/s, 8b/10b, 200 ps unit interval.
* **Ball** — BGA solder ball; named *row letter* + *column number* (A1 … L11, the letter **I** is
  skipped per JEDEC).
* **Escape** — the short trace from a ball to the first via or out of the package shadow.

---

## 2. The controller, exactly identified

| Item | Value | Confidence |
|---|---|---|
| Marking on the package (photo P1) | `CYUSB3014-BZX` / `I 2237` / `A 33 THA` / `CYP 625297` | **observed** |
| Orderable part | **CYUSB3014-BZXI** — industrial temperature, 512 kB SRAM, 32-bit GPIF II | **probable** (marking convention) |
| Package | 121-ball FBGA, **10.00 × 10.00 mm**, **0.8 mm** pitch, 11 × 11 fully populated, ball Ø 0.30 mm, body 1.20 mm max | **confirmed** (001-54471 \*F) |
| Date code | 2237 → 2022, week 37 | **probable** |
| USB capability | USB 3.2 Gen 1 (5 Gb/s) device + USB 2.0 HS/FS | **confirmed** (datasheet) |

**Balls that matter** (datasheet Fig. 6 / Table 7; every ball cross-checked):

| Ball | Signal | Function |
|---|---|---|
| **A1** | `U3VSSQ` | SuperSpeed PHY ground (also the pin-1 corner) |
| **A2** | `U3RXVDDQ` | SuperSpeed RX PHY supply, 1.2 V |
| **A3** | `SSRXM` | SuperSpeed **receive −** |
| **A4** | `SSRXP` | SuperSpeed **receive +** |
| **A5** | `SSTXP` | SuperSpeed **transmit +** |
| **A6** | `SSTXM` | SuperSpeed **transmit −** |
| **B3** | `R_USB3` | **200 Ω ±1 % to GND** — SS PHY calibration reference |
| **B5** | `U3TXVDDQ` | SuperSpeed TX PHY supply, 1.2 V |
| **A9 / A10** | `DP` / `DM` | USB 2.0 D+ / D− |
| **C8** | `R_USB2` | 6.04 kΩ ±1 % to GND — USB 2.0 PHY reference |
| **C6 / C7** | `XTALIN` / `XTALOUT` | 19.2 MHz crystal |

Order along row A is **RX−, RX+, TX+, TX−** (A3 → A6). The four SS balls plus their two supply balls
(A1, A2) and AVDD (A7) form a contiguous run of seven balls on the **outermost row** — the best-case
geometry for a top-layer escape and the worst-case geometry for rework, because the package overhangs
them.

---

## 3. What the photographs actually show

All coordinates below are source-photo pixels; divide by ≈ 37 for millimetres. Annotated versions:
`hardware/annotated/A1…A5`.

### 3.1 Board architecture (P1, component side)

| # | Feature | Location (px) | Identification | Confidence |
|---|---|---|---|---|
| 2 | **CYUSB3014-BZX** | 1185–1555 × 630–980 | FX3, 121-ball BGA | **certain** (marking) |
| 3 | **Pango PGL25G** | from x 1680, cut off by frame | FPGA, 484-ball (22 × 22 field visible as silkscreen numbering `22…1`) | **certain** |
| 4 | **Micron D9PTK** | 1055–1595 × 85–430 | DDR3L, FPGA frame buffer | **high** (marking) |
| 1 | **J4** | 365–595 × 215–880 | 40-pin, 2 × 20, board-to-board / FPC, **the only connector in frame** | **certain** |
| 5 | buck + inductor | 975–1125 × 815–1010 | FX3 (or FPGA) core rail | **medium** |
| 6 | 8-pin IC + passives | 630–950 × 700–960 | auxiliary rail regulator / load switch | **low-medium** |

The DDR3L cannot belong to the FX3 (the FX3 has no DRAM interface); it is the FPGA's frame buffer.
That fixes the data flow: **sensors → FPGA (+DDR3) → GPIF II → FX3 → USB**.

### 3.2 Board architecture (P2–P4, back side)

| # | Feature | Location (px) | Identification | Confidence |
|---|---|---|---|---|
| 4 | **Winbond 25Q64JVSIQ** | 680–960 × 85–310 | 64 Mbit (8 MB) SPI NOR boot flash | **certain** (marking `25Q64JVSIQ 2310`) |
| 1 | **J4** | 85–215 × 375–835 | same connector, seen from the back | **certain** |
| 3 | two labelled vias | ≈ (545, 620) and (600, 620) | silkscreen **`CLK_PCLK_M`** → pixel clock test points | **certain** (silkscreen) |
| 2 | two labelled vias | ≈ (550, 740) and (610, 740) | silkscreen **`DP`** and **`DN`** → USB 2.0 D+/D− test points | **certain** (silkscreen) |
| 5 | four 0603 capacitors | 338–432 × 618–848 | **shunt** decoupling bank between two vertical rails — *not* series AC coupling | **high** (see §3.4) |

### 3.3 FX3 orientation — proved three ways

The photograph is rotated 180° relative to the chip marking. This matters because a mirrored ball map
lands on the wrong balls. Three independent cues agree:

1. **Laser dot.** The package carries a round laser mark 1 mm inside the corner at image
   (≈1505, 958) — i.e. exactly on ball **A1**. Datasheet package note 7: "A1 corner to be identified
   by chamfer, laser or ink mark…".
2. **Silkscreen direction arrows.** A long arrow under the package points **left** along row A; a
   vertical arrow on the right points **up** along column 1. Both originate at the bottom-right corner,
   i.e. they are the "1 → 11" and "A → L" direction indicators starting at pin 1.
3. **Text orientation.** `CYUSB3014-BZX` is printed upside-down in the image.

Consequence: in the photographs **A1 is the bottom-right corner**, **row A is the bottom edge**,
**column 1 is the right edge**. The SuperSpeed balls A3–A6 therefore sit on the bottom edge, 2.6–5.0 mm
from the package's right edge — immediately above the 1.6 mm strip of top copper that separates the
package from the board edge.

> **Correction to a plausible mis-reading.** A superficial look at the P1 silkscreen might suggest
> `1 → 11` runs along the *top* of the footprint (the FPGA's own `22…1` numbering is nearby and is easy
> to confuse). The arrow directions and the laser dot both contradict that; the ball field in
> `A2_fx3_ball_field.png` is drawn from the dot, not from the silkscreen.

### 3.4 The four capacitors beside J4 are not AC coupling

They are 0603/0805 parts lying horizontally, stacked vertically at 1.4 mm pitch, with **all left pads
on one vertical net and all right pads on another** (`hardware/crops/P2_acbank_zoom.png`). Series AC
coupling would require each capacitor to sit *in line* with its own signal trace, one per line, with no
common rail on either side. These are shunt parts across two rails — a decoupling bank for the supplies
that leave through J4.

This distinction matters: if these had been the 0.1 µF SSTX pair, the conclusion of this report would
have been the opposite.

### 3.5 What is *absent* — and how strongly that counts

| Requirement for a SuperSpeed channel | Present? | How it was looked for |
|---|---|---|
| 0.1 µF series pair on SSTX (A5/A6) | **No** | every 2-pad part within 10 mm of the FX3 and within 15 mm of J4; none is in series with a signal |
| ≤ 0.5 pF ESD array (e.g. SP3010-04UTG) | **No** | no 4-channel array package adjacent to the FX3, J4, or the board edge |
| SS test pads / vias | **No** | the board has *labelled* test points for `DP`, `DN`, `CLK`, `PCLK` — and none for SS |
| 4-conductor path to any connector | **No** | the only connector is J4 (40 pins), and the visible USB pair is the 2-wire DP/DN |
| 200 Ω R_USB3 on B3 | **Unknown** | a 0402 resistor under/next to the package cannot be identified or read optically |
| SS PHY rails powered (A2, B5) | **Probably yes** | required even in USB-2.0-only designs (AN70707 App. C) |

The **labelled test points** are the strongest single structural clue on this board. A designer who
bothers to silk-screen `DP`, `DN`, `CLK_PCLK_M` for bench probing — and who leaves the SS pairs
unconnected — is following the standard USB-2.0-only practice. A designer who had routed four 5 Gb/s
pairs to a connector would almost certainly have labelled them too, or at least would have left the
AC-coupling footprint that the pairs require.

### 3.6 The one trace pair that *is* visible

Near the FX3's top-left corner (image coordinates ≈1120–1280 × 460–600) a coupled pair escapes the
package and turns down the left side. In board coordinates that corner is opposite pin 1 — the
GPIF/GPIO region, not the USB region. It is most consistent with the GPIF II bus or a clock, and it is
**not** offered as SuperSpeed evidence.

Around row A itself — the bottom edge, where A3–A6 would emerge — the visible top copper shows the
ground pour, two silkscreen lines, and at most two thin traces that belong to other nets. **There is no
bundle of four.**

---

## 4. Requirement-by-requirement correlation

Every row is a requirement from Infineon's own documents, checked against the photographs.

| # | Requirement (source) | What the board shows | Verdict |
|---|---|---|---|
| 1 | Four SS conductors from A3–A6 (datasheet Fig. 6) | no 4-trace escape on row A; no SS vias or pads elsewhere | **not met** (subject to inner-layer caveat) |
| 2 | 0.1 µF ±? series caps on SSTX, at the receptacle, with GND cut-out (AN70707 §12.1) | no series caps anywhere in the USB path | **not met** |
| 3 | ≤ 0.5 pF ESD on the four lines (AN70707 §10) | no ESD array | **not met** |
| 4 | 200 Ω ±1 % on B3 — SS PHY bias (datasheet Table 7, AN70707 Fig. 14) | not identifiable; permitted to be absent (App. C) | **unknown → assume absent** |
| 5 | U3TXVDDQ / U3RXVDDQ at 1.2 V with ferrite (AN70707 App. C) | power clusters exist; can neither confirm nor deny the ferrite/ball connection | **probably met** |
| 6 | 90 Ω ±7 % differential, solid reference, zero vias, < 76 mm (AN70707 §12.1) | no channel exists to evaluate | **not met** |
| 7 | Connector and cable carrying 4 SS conductors + shield | 7-pin connector: 12 V, GND, 4 × USB 2.0, **1 unconnected** (user-measured pinout) | **impossible** (1 spare, 4 needed) |
| 8 | Host side SS-capable | Intel xHCI 20 Gb/s, SS peer port exists | **met** |
| 9 | Firmware enables SS | `CyU3PConnectState(1,1)` at every connect site; SS descriptors + BOS present | **met** |

Nine requirements. The board meets two — and both are on the *host/firmware* side, not the board.

---

## 5. Why the geometry is decisive (and where it is not)

### 5.1 The favourable reading

All four SS balls are in **row A**, the outermost row. A designer routing them would normally take them
straight out on the top layer — no via, no HDI, no microvia. At 0.8 mm pitch one 0.1/0.1 mm trace fits
between pads, and a 0.45/0.25 mm dog-bone via fits diagonally. So if the pairs were routed on the
surface, **they would be visible as four traces crossing the package edge between 2.6 and 5.0 mm from
the right edge**. They are not.

### 5.2 The unfavourable reading — and why it does not rescue the design

Even if the designer instead dropped each ball into a dog-bone via inside the 1 mm band under the
package, the signals would have to *go somewhere*. Any destination that makes them usable requires at
least one of:

* **AC-coupling pads** — none exist anywhere on either photographed side;
* **ESD array pads** — none exist;
* **test points** — the board has them for USB 2.0 and the pixel clock only;
* **connector pins** — J4 is the only connector, and the external 7-pin connector cannot carry four
  more conductors.

A routed-but-unterminated SuperSpeed pair would be a stub with no AC coupling and no receptacle — it
would not train, and it would not be a "usable SuperSpeed circuit" under any reading of the question.

### 5.3 The B3 problem — the argument that closes the case

Suppose, against the photographic evidence, that the four SS pairs *are* routed somewhere useful. The
link still cannot train, because the SS PHY needs its **200 Ω ±1 % calibration resistor on ball B3**,
and Infineon explicitly permits that resistor to be omitted on a USB-2.0-only design (AN70707 App. C).
B3 is in **row B** — the second row, not the perimeter — so it is strictly harder to reach than A3–A6.

Therefore **any** electrical modification that hopes to achieve a 5 Gb/s link must, at minimum:

1. reach A3–A6 (perimeter row, under a 1 mm package overhang), **and**
2. reach B3 (second row, under 1.8 mm of package), **and**
3. add two 0.1 µF caps in series with SSTX, **and**
4. add a ≤ 0.5 pF ESD array, **and**
5. create a 90 Ω ±7 % channel with a solid reference plane, no vias, < 76 mm, **and**
6. terminate it in a connector and cable that carry four SS conductors and a shield.

Items 1–2 are a **BGA rework**. Items 3–6 are a **new PCB**. That is the whole answer.

---

## 6. Feasibility options, ranked by realism

| # | Option | What it takes | Effort | Risk | Verdict |
|---|---|---|---|---|---|
| **A** | Cable swap only | a USB 3.0 cable with the same 7-pin connector | minutes | none | **Impossible.** Four SS conductors do not fit in the connector's one spare pin. |
| **B** | Populate missing parts on existing footprints | find unpopulated AC-cap / ESD / connector footprints | hours | low | **Impossible.** No such footprints exist. |
| **C** | Solder a flying USB 3.0 lead to exposed vias/pads | locate SS vias, cut stubs, add caps + ESD, strain-relieve | days | **very high** | **No-go.** No SS vias were found; SI analysis (§7) predicts certain failure. |
| **D** | BGA rework + new internal harness | lift the FX3, bond to A3–A6 and B3, re-ball, add caps/ESD, add a panel-mount USB-C, new cable | weeks, specialist | **very high** | **Not recommended.** Cost and risk exceed a new board; reliability of a reworked 0.8 mm BGA in a handheld device is poor. |
| **E** | New plug-in daughterboard (mainboard untouched) | *only if* X-ray/TDR proves SS on J4; then design a daughterboard with AC caps, ESD and a USB-C receptacle, plus a new cable | weeks | medium | **Conditionally possible.** Probability that J4 carries SS: **low (~10–15 %)**. This is the only path that would not touch the mainboard. |
| **F** | New mainboard PCB (re-spin) | reuse FX3 + PGL25G + DDR3L + firmware; route SS properly, add USB-C, keep the 12 V input | months | medium | **The only "properly engineered" solution.** The firmware already offers SS with no code change; the FPGA bitstream and protocol are already documented in this workspace. |

**Effort vs. benefit.** SuperSpeed removes the throughput wall that currently limits the scanner: at
High Speed the practical ceiling is ≈ 40–45 MB/s, while the documented 68 ms three-camera cadence needs
≈ 58 MB/s (a 1.45× deficit), which is the most likely cause of the 115 `grabImage failed 1034`
timeouts in the recorded session. A re-spin buys up to ~45 % more aggregate frame rate and removes the
bandwidth pressure entirely. That is a real engineering benefit — but it is a **product-level** change,
not a modification.

### 6.1 Why the connector arithmetic is not the only argument — but is the one that binds here

A seven-contact connector *could* in principle carry SuperSpeed if the 12 V return were moved to the
metal shell: 12 V, D+, D−, SSTX±, SSRX± = exactly seven. That is poor practice at 5 A and the cable is
known not to do it, but it is worth stating so the argument is not overclaimed.

The supplied cable's measured allocation — 12 V, ground, four USB 2.0 conductors, one unconnected — has
**one** spare contact against a requirement of **four**. No re-pinning of the existing cable can bridge
that. The connector is therefore a binding constraint independent of everything else in this report.

### 6.2 Why the firmware enables SuperSpeed on a USB 2.0 product

This is not a contradiction, and it is the reason the question is worth asking at all. SHINING 3D's own
software treats a 480 Mb/s link as degraded:

* `AppCommon_E7.dll` (EXStar 1.2.2.0) ships the adjacent UI strings
  **"The current USB dirve is USB2.0"** and **"Notice: Please change to USB3.0"** (the typo is in the
  vendor binary); the same pair appears in `Sn3DprocessManager.exe`.
* `Sn3DCalibCaptureImageProcess.dll` carries a per-capture field `USBProtocol` whose literal is `USB3.0`.
* `SH3DU3DRIVER.inf` is branded **"Shining3d U3 Scanner Installation Disk"** and binds
  `SHINING3D_E10 = "Einstar"` to `USB\VID_3267&PID_0003`.

So the product family is built around a USB 3.0 expectation — the FreeScan/Einstar platform is driven by
one "U3" driver — while this particular model's manual says `Interface: USB 2.0`, its successor moved to
USB-C, and the unit enumerates at 480 Mb/s. The FX3 firmware enables SuperSpeed because the platform
firmware is shared; the **board and cable are what differ**.

---

## 7. Signal integrity of the tempting shortcut

The tempting modification is: find four vias near the FX3, solder a USB 3.0 cable's SS pairs to them,
add two 0.1 µF caps, and run the cable out of the housing. The channel budget says this cannot work.

| Quantity | Value | Consequence for a hand-soldered tap |
|---|---|---|
| Total link loss at 2.5 GHz, short channel | ≈ −4 dB | the entire budget for a device sitting at the host connector |
| Cable assembly alone | −7.5 dB @ 2.5 GHz | more than a third of the long-channel budget, before any PCB |
| FX3 SS PCB length allowance | **< 76.2 mm total** | a 50–100 mm flying lead consumes or exceeds it |
| Differential impedance | **90 Ω ±7 %** | hand-cut wire pairs land at 100–300 Ω with ±20 % variation |
| Intra-pair skew | **0.12 mm ≈ 0.7 ps** | hand-cut wires: 2–5 mm → 12–30 ps = 6–15 % of a 200 ps UI |
| Vias on SS | **zero recommended** | a tap adds at least two, plus two solder joints and a pad |
| Reference plane | solid ground on the adjacent layer | none along a flying lead |
| Connector impedance | 75–105 Ω, 50 ps TDR rise | a splice is a gross discontinuity |

**Predicted outcome:** the receiver equaliser cannot recover the channel; the link fails to train and
the device falls back to USB 2.0 High Speed — exactly the failure mode USB-IF documents as "not able to
train to SuperSpeed, instead trained to operate at USB 2.0 high speed". The optimistic outcomes are an
intermittent 5 Gb/s link that drops under load, and radiated-emissions failures. **A tap that works
requires a real 90 Ω channel — i.e. a board.**

---

## 8. Mechanical, shielding, grounding and power

Even if the electrical problem were solved, the physical integration would not be free:

* **Enclosure.** The 7-pin connector is a keyed push-pull part with a red alignment dot, sealing the
  housing. Adding a USB-C or USB 3.0 Micro-B receptacle means cutting the shell, providing a
  strain-relieved exit, and re-qualifying ingress protection.
* **Shielding.** The existing cable's shield terminates in that connector. A new cable must bond its
  shield to the housing/chassis at both ends with a 360° termination, or the SS pairs will radiate.
* **Grounding.** The USB 3.0 link's reference is the host's chassis ground through the cable shield.
  The scanner runs from a 12 V/5 A adapter; the ground topology (adapter return vs. shield) must be
  designed, not improvised.
* **Power.** SuperSpeed itself does not increase the FX3's supply current materially, but the FX3's SS
  PHY adds ~100–150 mW when active. The board's existing 1.2 V rails are almost certainly adequate
  (they must be populated even in USB-2.0-only designs). The 12 V/5 A budget is dominated by
  illumination, not USB.
* **Thermal.** No change of consequence; the FX3 already runs.
* **Serviceability.** A reworked BGA inside a handheld scanner is a reliability liability: thermal
  cycling plus a 0.8 mm pitch reball is a known failure mode.

---

## 9. Ranked candidate connection points

Ranked by *plausibility of being a SuperSpeed net* × *accessibility*. Confidence is in the
identification, not in the idea.

| Rank | Candidate | Where | Confidence it is SS | Access | Comment |
|---|---|---|---|---|---|
| 1 | **J4 pins** (if SS was routed for a future variant) | 40-pin connector, both sides accessible | **low, ~10–15 %** | good | The only place SS could exist without BGA rework. Test with X-ray or TDR; a DMM cannot settle it. |
| 2 | **Vias in the 1.6 mm strip below the FX3** | x 1330–1470, y 985–1080 (P1) | **very low, <5 %** | medium | Two or three vias are visible; none aligns with a 4-trace bundle and none is labelled. Destructive to test. |
| 3 | **FX3 balls A3–A6** | under the package, row A | **n/a — they are SS by definition** | **none** | 1.0 mm inside the body edge; 0.30 mm balls at 0.8 mm pitch. Not probeable, not solderable with the part fitted. |
| 4 | **FX3 ball B3 (R_USB3)** | under the package, row B | **n/a — required for SS** | **none** | Second row; even harder. Without it the PHY cannot calibrate. |
| 5 | **New PCB** | — | — | — | Not a "connection point"; the only path that satisfies every requirement. |

The board also has **no unpopulated footprints** anywhere in the USB path that could be populated as a
shortcut; every 2-pad site near the FX3 and J4 is occupied.

---

## 10. Unpowered multimeter inspection plan

> **Visual guide:** `hardware/annotated/A7_multimeter_procedure.png` — probe map, ground reference,
> preparation notes and the decision tree on one page.

**Purpose.** These measurements cannot prove that a BGA ball is unconnected — only X-ray or
de-soldering can. What they *can* do is settle the two facts that decide between options D, E and F:
**(i) is the SuperSpeed PHY biased at all**, and **(ii) what does J4 actually carry**.

**Safety.** Work unpowered (12 V adapter and USB both disconnected). Use a DMM in resistance/diode
mode; do not exceed 2 V on any pin. Photograph every pad before touching it. A fine-tip probe (≤0.5 mm)
and a ground clip on a mounting-hole ring are required.

### M1 — Establish the ground reference and the rails (5 min)

| Step | Probe | Expect | Meaning |
|---|---|---|---|
| M1.1 | mount ring ↔ J4 pin shells | < 1 Ω | chassis/ground reference found |
| M1.2 | each J4 pin ↔ GND | most < 1 Ω or open; a few at 1–50 kΩ | splits the 40 pins into ground / power / signal |
| M1.3 | J4 pin ↔ J4 pin, looking for 12 V feed | ~0 Ω to the 12 V input if present | identifies the power pair |

### M2 — Confirm the USB 2.0 routing (10 min)

| Step | Probe | Expect | Meaning |
|---|---|---|---|
| M2.1 | `DP` via ↔ J4 pins | continuity on exactly one pin | proves the DP net reaches J4 |
| M2.2 | `DN` via ↔ J4 pins | continuity on exactly one pin (adjacent to M2.1) | proves the pair |
| M2.3 | `DP` ↔ `DN` | open, or a few MΩ | sanity (no short) |
| M2.4 | `CLK`/`PCLK` vias ↔ J4 | continuity on two further pins | calibrates the method and maps two more J4 pins |

If M2 succeeds, the inference "J4 carries USB" becomes a **measurement**, not an inference — and J4
becomes the only place where a SuperSpeed variant could plausibly have been routed.

### M3 — Find the two PHY reference resistors (20 min) — **the decisive test**

Both resistors are 1 % parts to GND, placed within a few millimetres of the FX3.

| Step | Probe | Expect | Meaning |
|---|---|---|---|
| M3.1 | every 0402/0603 resistor near the FX3, one lead on GND | one reads **6.04 kΩ ±1 %** | **R_USB2 (C8) found** — the method works |
| M3.2 | same sweep | one reads **200 Ω ±1 %** | **R_USB3 (B3) found** → the SS PHY is biased and the design anticipated SS |
| M3.3 | same sweep | no 200 Ω part | **R_USB3 absent** → the SS PHY has no calibration reference |

Interpretation:

* **200 Ω found** → the mainboard has SS *infrastructure*. The remaining question is only whether the
  pairs reach J4; go to M4.
* **200 Ω not found** → the mainboard is a textbook AN70707 App. C USB-2.0-only design. **Option D
  (BGA rework) becomes mandatory for any modification, and option E is dead.** The report's NO-GO
  stands with no further measurement possible on this board.
* Beware false positives: 200 Ω parts also appear in LED bias and termination networks. Require the
  part to be adjacent to the FX3 and to have one pad on ground.

### M4 — Only if M3 found the 200 Ω: characterise J4 for SuperSpeed

A DMM cannot see a 90 Ω differential pair. Two options:

| Method | What it proves | Cost |
|---|---|---|
| **2D X-ray** of the J4-to-FX3 region | whether four traces leave row A and where they go | a lab visit |
| **TDR** on each J4 pin (50 ps rise) | a 90 Ω differential signature on a pin pair = a routed SS pair | lab equipment |
| **Capacitance** J4 pin → GND | weak: distinguishes "connected to something" from "floating" only | DMM with pF range |

If either test shows four SS conductors reaching J4, **option E becomes viable** and only a new
daughterboard + cable is needed — with the mainboard left untouched. If not, **option F** is the only path.

### M5 — Record and stop

Photograph the probe points, record every value, and do not power the board with a modified harness.
Nothing in this plan requires soldering.

---

## 11. What the photographs prove vs. what needs measurement

| Claim | Status | Basis |
|---|---|---|
| The controller is a CYUSB3014-BZX(I) | **Proved** | package marking |
| The package is a 121-ball 0.8 mm BGA and its orientation is known | **Proved** | laser dot + silkscreen arrows + marking |
| The four SS balls are A3–A6 on row A, 2.6–5.0 mm from the right edge | **Proved** | datasheet + measured package geometry |
| No 0.1 µF series AC-coupling pair exists in the USB path | **Proved (optical)** | every 2-pad part inspected |
| No SuperSpeed ESD array is fitted | **High confidence (optical)** | no 4-channel package found |
| No SuperSpeed test pads or labels exist | **Proved** | only DP/DN/CLK/PCLK are labelled |
| The 7-pin connector cannot carry SS | **Proved (arithmetic)** | 1 spare conductor, 4 required |
| The board is a USB-2.0-only design in practice | **High confidence** | all of the above + vendor manual + 480 Mb/s enumeration |
| The SS balls are unconnected | **Not proved** — inner layers/vias invisible | needs X-ray or de-soldering |
| R_USB3 (200 Ω) is absent | **Not proved** | needs M3 |
| J4 carries or does not carry SS | **Not proved** | needs M4 |
| A flying-lead modification would fail | **Predicted (high confidence)** | quantified SI budget |
| Firmware would negotiate SS unchanged | **Proved (earlier work)** | `CyU3PConnectState(1,1)` at every site |

---

## 12. Residual unknowns

1. **BGA ball connectivity** — invisible by construction; only X-ray or destructive inspection resolves it.
2. **R_USB3 population** — decidable by M3, not by photograph.
3. **J4 pinout** — 40 pins, only 4 identified by inference (DP, DN, CLK, PCLK).
4. **Layer count and stackup** — not visible; ≥ 4 required by AN70707, ≥ 6 probable.
5. **The board's identity within the product** — most consistent with the acquisition ("head") board;
   the external 7-pin connector is not in frame on either side.
6. **Whether a USB 3.0 variant of this board exists** — no public evidence; the successor product moved
   to USB-C.
7. **Exact FX3 variant** — BZXI by marking convention; the silicon is the same 3014 in any case.

---

## 13. Recommendation

**Do not modify this board.** The evidence is coherent and points one way: a USB-2.0-only FX3 design,
built to Infineon's own USB-2.0-only checklist, with a connector that physically cannot carry
SuperSpeed. The only modification that could work is a BGA rework plus a new harness — more work, more
risk and worse reliability than a new board.

**If USB 3.0 throughput is the goal, the rational path is option F:** a new mainboard that reuses the
CYUSB3014, the PGL25G, the DDR3L, the boot flash and — crucially — the **existing firmware**, which
already advertises SuperSpeed and would negotiate 5 Gb/s with no code change. This workspace already
contains the FX3 memory map, the FPGA bitstream analysis, the USB protocol, the calibration format and
a working acquisition backend, so a re-spin is an engineering exercise with known unknowns rather than
a research project.

**Before committing to a re-spin**, run **M3**. Twenty minutes with a multimeter decides whether this
board is the textbook App. C design (expected) or a board that was quietly provisioned for SuperSpeed —
and that single measurement determines whether option E is worth a lab visit.

---

## Appendix A — Primary documents

| Document | Number / revision | Date | Used for |
|---|---|---|---|
| EZ-USB FX3 datasheet (CYUSB301x/CYUSB201x) | **001-52136 Rev.\*Z** | 2022-09-29 | ball map Fig. 6, pin list Table 7, ordering, ESD |
| AN70707 — FX3/FX3S/SX3 hardware design guidelines & schematic checklist | **001-70707 Rev.\*Q** | 2021-10-26 | §12.1 SS layout, App. C/D unused-SS practice, Fig. 14/15 |
| 121-ball FBGA package outline | **001-54471 Rev.\*F** | 2017-06-28 | 10 × 10 mm, 0.8 mm pitch, ball Ø, A1 marking |
| EZ-USB FX3 technical reference manual | **001-76074 Rev.\*F** | 2019-05-09 | PHY/register context |
| USB 3.2 specification / USB-IF CabConn & equaliser guidance | — | — | link budget, cable loss, impedance mask |

Local copies and extracted text: `hardware/analysis/sources/`, `hardware/analysis/fx3_docs/`.

## Appendix B — Deliverables

| File | Contents |
|---|---|
| `hardware/annotated/A1_top_side_overview.png` | component-side architecture map |
| `hardware/annotated/A2_fx3_ball_field.png` | FX3 orientation + reconstructed ball field + SS balls |
| `hardware/annotated/A3_bottom_side_overview.png` | back-side features, DP/DN and CLK/PCLK test points |
| `hardware/annotated/A4_signal_path_audit.png` | signal-path audit and the five missing requirements |
| `hardware/annotated/A5_ball_map.png` | documented 121-ball map, colour-coded |
| `hardware/annotated/A6_row_a_escape.png` | 8× zoom of the row-A escape region with the projected A3–A6 columns |
| `hardware/annotated/A7_multimeter_procedure.png` | probe map, ground reference and decision tree for the 20-minute test |
| `hardware/analysis/fx3_ballout.md` / `.json` | full ball map with citations, machine-readable |
| `hardware/analysis/fx3_ss_layout_requirements.md` | BOM, impedance, BGA escape, SI of a tap |
| `hardware/analysis/product_teardown_corroboration.md` | public evidence, connector, vendor software |
| `hardware/crops/` | every zoom used as evidence |
| `hardware/make_annotations.py`, `hardware/annotate.py` | reproducible figure generation |

## Appendix C — Reproduce

```bash
cd <repo>
python3 hardware/make_annotations.py        # regenerates A1–A5 from the original photos
python3 - <<'PY'                            # white-balanced working copies
from PIL import Image; import numpy as np
for n in ('P1_top_side','P2_bottom_side'):
    im=Image.open(f'hardware/photos/{n}.webp').convert('RGB')
    a=np.asarray(im).astype(np.float32); m=a.reshape(-1,3).mean(0)
    Image.fromarray(np.clip(a*(m[1]/m),0,255).astype(np.uint8)).save(f'hardware/annotated/{n}_wb.png')
PY
```

Figures are generated from the **unmodified** supplied photographs; only a global grey-world white
balance and unsharp masking are applied, and every annotation is drawn as an overlay, never painted
into the photo.
