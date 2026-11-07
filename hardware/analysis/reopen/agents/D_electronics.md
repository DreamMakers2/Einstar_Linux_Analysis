# D — Electronics / schematic-feasibility memo
### EZ-USB FX3 (CYUSB3014-BZX) USB 3.0 front end on the SHINING 3D Einstar mainboard

| | |
|---|---|
| **Author** | subagent D (electronics/schematic analysis) |
| **Date** | 2026-11-08 |
| **Target** | Einstar mainboard — CYUSB3014-BZX (121-ball FBGA), Pango PGL25G, DDR3L, 40-pin board-to-board/FFC connector, 7-pin external connector |
| **Question** | Why does a firmware-SuperSpeed-capable FX3 link at USB 2.0 HS, and which electrical hypotheses are worth testing unpowered? |
| **Access** | Documents only. No hardware, no power, no soldering. I made **no** multimeter measurements; every "reading" below is a prediction from device physics, explicitly labelled. |
| **Output of prior work used** | None as evidence. Prior figures/docs in this tree were **not** used as measurement sources; where I looked at a photo myself I say so and give the crop box. |

---

## 0. Labelling and source conventions

Every substantive statement carries one of:

| Tag | Meaning |
|---|---|
| **[Q]** | **Quoted fact** — wording is in a local document; reference given as `DOC §/Fig./Table, p.` |
| **[D]** | **Derived** — arithmetic or logic applied to [Q] facts (working shown) |
| **[I]** | **Inference** — my engineering judgement; may be wrong; falsifiable by the measurements in §7 |
| **[O]** | **My own observation** — something I looked at myself (photo crop, document rendering). Not a measurement. |

Local sources (all under `hardware/analysis/fx3_docs/`):

| Key | File | Identity |
|---|---|---|
| **DS** | `cyusb301x_ds.pdf` / `.txt` | Cypress CYUSB301X datasheet, **001-52136 Rev. \*O**, 2014-05-02 (46 pp.) |
| **AN** | `an70707_v18.pdf` / `.txt` | Infineon AN70707, **001-70707 Rev. \*Q** (v18), 2021-10-26 (47 pp.) |
| **AN-G** | `an70707.pdf` / `.txt` | Older AN70707, **Rev. \*G** (Cypress) — used only for revision comparison |
| **ECN** | `usb31_ecn_rxac.txt` | USB-IF ECN *Rx AC Coupling Capacitor Option*, Form 20131608-ECN (USB 3.1) |
| **CAB** | `usbif_cabcon.txt` | USB-IF whitepaper *Managing Connector and Cable Assembly Performance for USB SuperSpeed*, Rev 1.0, 2013-02-01 |
| **SP** | `sp3010.txt` | Littelfuse SP3010-04UTG summary (4-ch ESD array, UDFN-10) |
| **API** | `fx3_apiguide.txt` | EZ-USB FX3 API Reference Guide (SDK) |
| **SSEK** | `ssek_ug.txt` | SuperSpeed Explorer Kit user guide |

> The PDFs in `fx3_docs/` are the **Rev \*O** datasheet and **Rev \*Q** app note. A Rev \*Z datasheet PDF also exists in `hardware/analysis/sources/`; I did **not** cite it, so page numbers below are Rev \*O's. The Ω glyph does not survive text extraction from the Rev \*O PDF (it extracts as a space); I re-rendered the relevant page and read it visually — the printed text is `200 Ω ±1%` **[O, DS p.18]**.

---

## 1. Executive answers

1. **The FX3 SS front end is four signal balls, two 1.2 V rails, one ground and one resistor**: SSRXM / SSRXP / SSTXP / SSTXM = **A3 / A4 / A5 / A6**; the SS-PHY 1.2 V rails **A2 (U3RXVDDQ)** and **B5 (U3TXVDDQ)**; the SS-PHY ground **A1 (U3VSSQ)**; and the reference resistor **B3 (R_USB3, 200 Ω ±1 % to GND)**. AC coupling is **two 0.1 µF capacitors in series on SSTX only**; ESD is the **SP3010-04UTG** (≤ 0.5 pF added). The SS rails need **22 µF + 0.1 µF each, behind their own ferrite bead** from the same 1.2 V source that feeds VDD/AVDD.
2. **"No AC-coupling caps on the mainboard ⇒ no SuperSpeed path" is not valid as stated.** It conflates *a capacitor component on this PCB* with *series capacitance in the SSTX net*. AN70707 makes placement a function of **far-end topology**, not of board boundaries, and the USB 3.1 ECN explicitly contemplates AC coupling "within the media or within the receiving component" (its RX-side clause). The claim becomes valid only in the stronger form: *no series 0.1 µF anywhere between A5/A6 and the far-end receiver* (or a DC path, or double-coupling that drops the total below 75 nF).
3. **"PHY supply missing" is a low-probability blocker, not a leading one.** The datasheet requires the **same 1.2 V supply** to feed VDD, AVDD, U3TXVDDQ and U3RXVDDQ; if the FX3 enumerates at all, that source exists. Appendix C *still requires* both SS rails to be connected in a USB-2.0-only design. The only realistic failure modes are an **open bead**, a **shorted rail**, or **missing 22 µF** (inrush/POR) — all falsifiable at the bead/cap in one measurement. Also: **A1 is U3VSSQ, a ground ball, not a rail** — the task premise is wrong on that point.
4. **R_USB3** is a 0201/0402 two-terminal part, almost certainly placed at the **A1-corner group** near balls A2–A7/B3–B7, on the top **or bottom** side (B3 is a row-B ball, so it fans out through a via or between the row-A pads). A 200 Ω 1 % part reads: B3→GND ≈ 200 Ω (or *lower*, if the unpowered die shunts it), ground pad→GND ≈ 0 Ω, pad→pad = 200 Ω ±1 %. A resistor is **linear, polarity-symmetric, 0.000 V in diode mode, and reads the same at every ground point**; a semiconductor path is none of those.
5. **128 Ω is not a standard 1 % resistor value and is not a trace.** 127/129/130 Ω parts exist (E96/E192), so a genuine resistor is possible; but 128 Ω is also exactly what a **200 Ω ∥ ~350 Ω** network, a **DC-connected SSRX pair** (spec 72–120 Ω differential + probe/path ohms), or a **contact/nonlinear artifact** produces. The decision table in §6 separates them with six observations, of which **polarity reversal** and **capacitance mode** are the most decisive.
6. **The single most decisive safe measurement** is not at the BGA: it is **capacitance + resistance from the 40-pin connector's SS pin candidates to the FX3-side escape via/stub**. It cleanly separates the three worlds — *caps on mainboard* (≈100 nF, DC open), *caps on daughterboard* (≈0 Ω, ≈5 pF), *no path* (open, ≈0 pF). Full ranked plan in §7.
7. **The SSTX 0.1 µF pair cannot be identified by marking** (MLCCs are unmarked). It is identified by **topology** (two identical parts **in series**, neither pad grounded, on the two parallel SSTX traces) plus **C = 100 nF**. Package: 0402 (1.0 × 0.5 mm) is the norm; 0201 (0.6 × 0.3 mm) is acceptable. A shunt part with one pad on GND is a decoupling cap and is disqualified.

---

## 2. The FX3 USB 3.0 front end — exact requirements

### 2.1 The balls (121-ball BGA, top view)

FX3 121-ball ball map **[Q]** — DS **Figure 7, p.13**, repeated as Table 7 pin list **pp.15–18**:

```
       1          2          3          4          5          6          7        8      9    10    11
 A  U3VSSQ   U3RXVDDQ    SSRXM      SSRXP      SSTXP      SSTXM      AVDD      VSS     DP    DM    NC
 B   VIO4     FSLC[0]    R_USB3     FSLC[1]   U3TXVDDQ    CVDDQ      AVSS      VSS    VSS   VDD   TRST#
 C  GPIO[54]  GPIO[55]     VDD      GPIO[57]   RESET#     XTALIN   XTALOUT   R_USB2  OTG_ID TDO   VIO5
```

The whole USB 3.0 analog front end is therefore **one contiguous group on the row-A edge, columns 2–7, plus row B columns 3, 5, 6, 7**. That is the single most useful fact for optical work: if the board supports SS, four differential traces plus a reference resistor and two 1.2 V feeds all leave the package in **one corner region** (the A1 corner).

Ball-centre coordinates from the A1 corner, pitch 0.8 mm, first ball 1.0 mm in from both body edges **[D]**: `(10 mm − 11×0.8 mm)/2 = 1.0 mm`:

| Ball | Signal | (x, y) mm from A1 corner | Note |
|---|---|---|---|
| A1 | U3VSSQ | (1.0, 1.0) | **SS-PHY ground return — not a supply** |
| A2 | U3RXVDDQ | (1.8, 1.0) | 1.2 V SS RX rail |
| A3 | SSRXM | (2.6, 1.0) | SS RX− |
| A4 | SSRXP | (3.4, 1.0) | SS RX+ |
| A5 | SSTXP | (4.2, 1.0) | SS TX+ → **series 0.1 µF** |
| A6 | SSTXM | (5.0, 1.0) | SS TX− → **series 0.1 µF** |
| A7 | AVDD | (5.8, 1.0) | 1.2 V PLL/analog |
| B3 | R_USB3 | (2.6, 1.8) | 200 Ω ±1 % → GND |
| B5 | U3TXVDDQ | (4.2, 1.8) | 1.2 V SS TX rail |
| B6 | CVDDQ | (5.0, 1.8) | 1.7–3.6 V clock I/O |
| B7 | AVSS | (5.8, 1.8) | analog ground |

**Correction to the task premise.** The brief lists "A1" among the PHY *rails*. A1 is **U3VSSQ** — the SS-PHY ground **[Q, DS Fig. 7 p.13 and Table 7 p.17]** (`A1 … PWR U3VSSQ`). It must be tied to ground, but it carries no voltage and cannot be "missing a supply". This matters practically: a probe on A1 is a **ground reference**, and any resistance measured "to A1" is a resistance to the SS-PHY ground plane, not to a rail.

### 2.2 Requirements table

| # | Item | Ball(s) | Requirement | Source | Tag |
|---|---|---|---|---|---|
| R1 | SSTXP | A5 | SS TX+; routed to the SS path; 90 Ω diff ±7 %; < 3 in; no vias; polarity swap allowed | DS Fig.7 p13 / Table 7 p16; AN §12.1 p21–22, Table 11 layout #6, #15 | [Q] |
| R2 | SSTXM | A6 | SS TX−; same | same | [Q] |
| R3 | SSRXP | A4 | SS RX+; **no** series capacitor in FX3 reference designs | DS p16; AN Fig.15 p21 | [Q] |
| R4 | SSRXM | A3 | SS RX−; same | same | [Q] |
| R5 | TX AC coupling | in series with A5, A6 | **2 × 0.1 µF**, one per line, placed **symmetrically** | AN §12.1 p21 + Fig.15; Table 11 schematic #15, layout #10 | [Q] |
| R6 | Cap package | — | **Not specified by Infineon.** 0402 (1.0×0.5 mm) is the practical norm; 0201 (0.6×0.3 mm) acceptable; larger parts add ESL and defeat the ground cut-out | — | [I] |
| R7 | Cap placement | — | near **receptacle** for host/device; near **FX3 TX pins** for hub/embedded-controller far end; GND cut-out under caps; solid reference on the immediately adjacent layer | AN p21 (2 sentences), §17.1 p39, Fig.16/17 | [Q] |
| R8 | RX AC coupling | A3/A4 | **Optional**; if used, 297–363 nF and "within the media or within the receiving component" | ECN Table 6-2 p4 | [Q] |
| R9 | ESD | on A3–A6 | SP3010-04UTG recommended; added capacitance **≤ 0.5 pF**; FX3 internal HBM on SS pins is only **±2.2 kV** | AN §10 p19 + Fig.12; Table 11 schematic #8; DS p13 | [Q] |
| R10 | R_USB3 | **B3** | **200 Ω ±1 %** to GND | DS Table 7 p18; AN §12.1 p21 + Fig.14 (R47); §17.1 p39 ("200E ±1 %") | [Q] |
| R11 | R_USB2 | **C8** | **6.04 kΩ ±1 %** to GND | DS Table 7 p18; AN Fig.14 (R48) | [Q] |
| R12 | U3TXVDDQ | **B5** | 1.2 V, **1.15–1.25 V**; **22 µF** bulk **+ 0.1 µF** at pin; **own ferrite bead**; source ripple < 70 mV | DS p22 ("A 22-µF bypass capacitor is required"); AN Table 3 p8, Fig.1 p6, §17.1 p38 | [Q] |
| R13 | U3RXVDDQ | **A2** | identical to R12 | same | [Q] |
| R14 | U3VSSQ | **A1** | SS-PHY ground return; connect to ground | DS Fig.7 p13, Table 7 p17 | [Q] |
| R15 | AVDD / AVSS | A7 / B7 | 1.2 V; **2.2 µF + 0.1 µF**; **own bead**; noise **≤ 20 mV p-p** (all other rails ≤ 100 mV) | DS p22–23; AN Table 3 p8, Fig.1 | [Q] |
| R16 | CVDDQ | B6 | 1.7–3.6 V (1.8/3.3 typ); 0.1 µF + 0.01 µF; bead | DS p22; AN Table 3, Fig.1 | [Q] |
| R17 | VDD | B10, C3, E9, F11, H1, J11, L5, L7 | 1.2 V core; 22 µF group + per-pin 0.1/0.01 µF | DS p22, Table 7 pp.17–18; AN Table 3 | [Q] |
| R18 | VBATT / VBUS | E10 / E11 | 3.2–6 V / 4.0–6 V; VBUS 0.1 µF (checklist asks 1 µF); OVP recommended | DS p22; AN Table 11 #13–14, §4 p7 | [Q] |
| R19 | 2.0-only omissions | A3–A6, B3 | **may be left open**, but both U3 rails still connected | AN **Appendix C, p.40** (quoted in §2.7) | [Q] |
| R20 | Inrush | A2, B5 | **up to 800 mA for ~10 µs** at first SS-PHY enable/reset; VDD must not dip below **0.83 V for > 200 ns** (POR) | AN §4.2 pp.8–9 | [Q] |

### 2.3 The AC coupling, precisely

**Value and side.** AN §12.1 p.21 **[Q]**:

> "USB 3.0 traces require additional AC coupling capacitors (0.1 µF) placed on the SS_TX lines. Place these capacitors symmetrically and close to the EZ-USB™ FX3 device. See Figure 15."

Figure 15 (p.21) **[Q, verified visually]**: `SSRXP A4 → SS_RX_P` and `SSRXM A3 → SS_RX_M` go straight through; `SSTXP A5 → C43 0.1uF → SS_TX_P` and `SSTXM A6 → C46 0.1uF → SS_TX_M`. So the **value is 0.1 µF, the side is TX only, the quantity is two, and they are in series** (not shunt).

**Package is not specified anywhere in the local corpus** **[Q by absence]**. The physical constraints that *are* specified: a **ground cut-out in the shape of the capacitor on the immediately adjacent solid GND layer** ("mandatory to provide the ground reference on layer, which is immediate to solid GND layer", AN p.21, Fig.16/17 **[Q]**), and layout checklist items 11 and 17 ("cut-out on immediate layer and reference GND split plane on immediate third layer"; "two planes underneath the AC coupling capacitors have cut in shape of the capacitor"). A 0402 is the normal choice; 0201 is used where the cut-out must be smaller. Neither is quoted — this is **[I]**.

**Why 0.1 µF and why small.** The value sets the low-frequency corner (the ECN's RX-detect window is quoted as 75–265 nF, ECN p.1 **[Q]**), while at 2.5 GHz the part is dominated by its ESL, not its capacitance **[D]**: a 0.1 µF 0402 with ~0.5 nH ESL is series-resonant around 22 MHz, and at 2.5 GHz presents ≈ 8 Ω of ESL per capacitor (≈ 16 Ω differential for the pair). That is why the app note insists on small parts and a reference-plane cut-out. (ESL figure is a typical-value assumption, not from the local corpus — **[I]**.)

### 2.4 ESD

AN §10 p.19 **[Q]**:

> "The SSRX+, SSRX-, SSTX+, SSTX- pins have only up to ±2.2-kV human body model (HBM) internal ESD protection. You can include additional protection to these pins by using high-performance, low-capacitance external ESD devices (SP3010-04UTG), as shown in Figure 12. To prevent an effect on the performance of this bus, the added capacitance should not exceed 0.5 pF."

SP3010-04UTG, from the local part summary **[Q, SP]**: 4-channel + zener rail-to-rail array, **UDFN-10**, **0.45 pF typ per I/O**, ±8 kV contact / ±15 kV air, 0.1 µA leakage. The 10-pin UDFN is an **easy probe target** (0.5 mm pitch) — see §7. Note the AN phrasing ("You can include…") and checklist item 8 ("Do the SuperSpeed USB lines have ESD device connected?") make it **recommended, not mandatory** **[Q]**.

### 2.5 Reference resistors

DS Table 7 p.18 **[Q, verified visually]**:

> `C8 … R_usb2 — "Precision resistor for USB 2.0 (Connect a 6.04 kΩ ±1% resistor between this pin and GND)"`
> `B3 … R_usb3 — "Precision resistor for USB 3.0 (Connect a 200 Ω ±1% resistor between this pin and GND)"`

AN Figure 14 p.21 **[Q]** draws them as **R48 = 6.04K/1 %** and **R47 = 200/1 %**, both to ground. AN §17.1 p.39 **[Q]** restates: "Make sure that the USB3 reference pin (B3) is connected to a resistor (200E) with +/- 1% tolerance."

Two documentation traps worth knowing:

* In Rev \*O Table 7 the **Name column for the R_usb3 row reads "U3TXVDDQ"** — a typo in the datasheet (the WLCSP column reads B9, the BGA column B3, and the description is unambiguous). Do not read that row as a power pin **[O, DS p.18]**.
* The tolerance is **1 %**, so a correct part reads **198–202 Ω** at 25 °C **[D]**.

### 2.6 The SS-PHY rails and their filtering

DS p.10 **[Q]**:

> "VDD: This is the supply voltage for the logic core. The nominal supply-voltage level is 1.2 V. … **The same supply must also be used for the following:** • AVDD … • **U3TXVDDQ/U3RXVDDQ: These are the 1.2-V supply voltages for the USB 3.0 interface.**"

DS p.22 **[Q]**: `U3TXVDDQ / U3RXVDDQ — USB 3.0 1.2-V supply — 1.15 / 1.25 V — "1.2-V typical. A 22-µF bypass capacitor is required on this power supply."`

AN Figure 1 p.6 **[Q, verified visually]**: each of A7 (AVDD), A2 (U3RXVDDQ), B5 (U3TXVDDQ) is fed from the common **V1p2** rail through its **own MPZ2012S601A ferrite bead**, with **2.2 µF + 0.1 µF** on AVDD and **22 µF + 0.1 µF** on each SS rail; CVDDQ also gets a bead. AN Table 3 p.8 **[Q]** adds the per-pin 0.1 µF. AN §17.1 p.38 **[Q]**: "Make sure that AVDD, CVDD, U3TXVDDQ, and U3RXVDDQ are isolated and connected with ferrite beads. Also, the source of these supplies should have noise/ripple less than 70mV."

**Current.** There is **no steady-state current specification for A2/B5** in the local datasheet **[Q by absence]**: `ICC Core ≤ 200 mA` is defined as "Total current through AVDD, VDD", and `ICC USB ≤ 60 mA` covers VBATT/VBUS. The only published SS-rail current number is the **800 mA / ~10 µs inrush** (AN §4.2) **[Q]**, which exists to size the 22 µF and the regulator.

**Revision trap.** The older local AN (Rev \*G) Table 3 lists **2.2 µF** for U3RXVDDQ/U3TXVDDQ; Rev \*Q lists **22 µF**, matching the Rev \*O datasheet **[Q, AN-G p.5 vs AN p.8]**. A board designed to the older note may legitimately carry 2.2 µF, which is *not* a defect per se but is below the current requirement and gives a worse inrush dip (AN Fig.5: 200 mV drop in the non-optimised 2.2 µF design vs 112 mV with 22 µF) **[Q]**.

### 2.7 What may legally be left open in a USB-2.0-only design

AN **Appendix C, §18, p.40** **[Q, verified visually — full text]**:

> **"Appendix C – FX3/SX3 device connections to USB 2.0-only host**
> • FX3/SX3 SuperSpeed pins TX+/- pins (A5 and A6) and RX+/- pins (A3 and A4) can be left open.
> • Both USB3.0 power pins (U3TXVDDQ and U3RXVDDQ) should be connected to 1.2V with a ferrite bead.
> • The USB3 reference pin (B3) can be left open.
> • Make sure that layout guidelines are followed per Section 12.1."

This single paragraph settles three questions at once **[D]**:

1. A USB-2.0-only FX3 design **may legally have no AC caps, no ESD array, and no 200 Ω resistor**. Their absence is therefore **not** evidence of a fault — it is evidence of *design intent*, and it is exactly what a cost-optimised 2.0-only board looks like.
2. A USB-2.0-only design **may not** leave A2/B5 unpowered. The rails are required even when the SS pins are open. So "the board is 2.0-only, therefore the PHY rails were omitted" is **contradicted by the vendor's own appendix**.
3. The appendix is about a **host** connection. The mirror-image appendix (D, p.41) makes the same rail statement for a 3.0-only design and allows DP/DM and C8 to be open. There is **no** appendix that permits omitting the 1.2 V SS rails.

### 2.8 The rest of the front end (for completeness)

* **USB 2.0 pair**: DP = **A9**, DM = **A10** **[Q]**. R_USB2 at **C8**. The 2.0 PHY is inside the same block and is powered from VBATT/VBUS through the internal 3.3 V regulator **[Q, DS p.10]**.
* **Power-on / enumeration**: the driver "detects the connection speed and takes care of providing the appropriate USB descriptors" **[Q, API §1.6.1]**. A device that cannot train SuperSpeed therefore still enumerates at HS by design — the observed behaviour is the *expected* fallback, not an anomaly.
* **VBUS threshold**: "If VBUS is less than 4.1 V, then FX3 behaves as if no VBUS is connected … does not enumerate at all" **[Q, AN §4 p.7]** — a useful sanity check but not a 2.0-vs-3.0 discriminator.

---

## 3. Does "no AC-coupling capacitors on the mainboard" prove "no SuperSpeed path"?

### 3.1 The claim, restated precisely

> **C1:** "There are no 0.1 µF caps on the mainboard, therefore there is no SuperSpeed path."

C1 is a **non sequitur**. It silently substitutes *"a capacitor component on this PCB"* for *"series capacitance in the SSTX net"*. Those are different propositions, and only the second one is electrically required.

### 3.2 What the documents actually require

| Point | Evidence | Consequence for C1 |
|---|---|---|
| The requirement is **series 0.1 µF on the TX lines** | AN §12.1 p.21; Fig.15; Table 11 #15; §17.1 p.39 **[Q]** | A *net-level* requirement, not a *board-level* one |
| Placement is **topology-dependent** | AN p.21: "AC caps should be placed near to USB connector only for Host and device implementation … AC caps should be placed near to FX3 device only if FX3 connected to HUB or other emdedded controller"; §17.1 p.39 repeats it as "near to the type-B receptacle … near to the FX3S/SX3 TX pins for … hub/embedded controller" **[Q]** | Infineon's own rule is about **which end of the link** the caps sit at, not about which PCB |
| The **purpose** is common-mode isolation from the cable/host | AN §17.1 p.39: "This will prevent the common-mode noise from the cable/host entering FX3/SX3 device TX lines" **[Q]** | Caps on a daughterboard between the cable and the FX3 still perform this function, provided the intervening path is short |
| AC coupling may live **in the media** | ECN Table 6-2 p.4: RX coupling "required to be either **within the media or within the receiving component**" **[Q]** | The standard explicitly contemplates coupling outside the transmitter's PCB |
| TX nominal is **100 nF**, and the RX-detect window is **75–265 nF** | ECN p.1: "combined value of 100nF (TX side) and 330nF (Optional RX side) can be meet minimum capacitance requirement for RX detection (75nF~265nF)" **[Q]** | Gives a *quantitative* test: measure the series capacitance |

There is **no sentence anywhere in the local corpus** that says the capacitors must be on the same PCB as the FX3 **[Q by absence — I searched AN Rev \*Q, AN Rev \*G, ECN, CAB, DS, API, SSEK for placement language]**.

### 3.3 When C1 *is* valid

C1 becomes a valid argument only in one of these stronger forms:

| # | Condition | Why it is decisive | How to test (§7) |
|---|---|---|---|
| **V1** | The SS pairs are **not routed past the connector** (NC pins, or stubs that stop at the connector) | Then there is no path, caps or not | M4/M5 |
| **V2** | There is a **DC path** from A5/A6 all the way to the far-end receiver — i.e. **no series capacitor anywhere**, mainboard *and* daughterboard | Violates the TX AC-coupling requirement; the two sides' common modes are not isolated; SS training is **not expected** to succeed **[I]** | M5 (0 Ω + ~5 pF ⇒ this is the "caps on daughterboard" signature **only if** the daughterboard is known to carry them; otherwise it is V2) |
| **V3** | Caps are fitted **on both** mainboard and daughterboard in series | **0.1 µF + 0.1 µF in series = 50 nF < 75 nF** minimum ⇒ RX-detect window violated **[D]** | M5 reads ≈50 nF instead of ≈100 nF |
| **V4** | The mating board is a **USB-2.0-only** design (no SS pins, no caps, no receptacle SS contacts) | The path ends at the connector | Connector/daughterboard inspection |

### 3.4 What the absence of mainboard caps *does* prove

Three things, all weaker than C1 **[D]**:

1. **Design intent.** A board built for SS almost always fits the caps (they are two cents). Their absence on *both* sides is a strong signal the product was designed as 2.0-only — consistent with the Appendix C checklist and therefore with the observed 2.0 link.
2. **Nothing about the daughterboard.** A top-side photo cannot see the bottom side, and a bottom-side photo cannot see a 0201 under a shield or under the package shadow. "Not seen" ≠ "not fitted".
3. **Nothing about the link's compliance** unless V1–V4 is established.

### 3.5 Engineering caveat: a board-to-board/FFC hop is a poor SS channel

Even with the caps correctly placed on the daughterboard, the AN's layout rules are hard to meet across a connector **[Q/I]**:

* 90 Ω differential **±7 %** (AN §12.1 p.22; Table 11 layout #6) **[Q]**;
* **no vias** on SS traces (Table 11 layout #15) **[Q]** — impossible at a connector;
* **< 3 inches** total (AN §12.1 p.21) **[Q]**;
* a 0.5 mm-pitch FFC/board-to-board hop is not a controlled-impedance launch; the CAB whitepaper's mated-connector requirement is **75–105 Ω** at 50 ps risetime, including the PCB footprint **[Q, CAB §"Connector and Cable Assembly Signal Integrity Requirements", Fig.1]**.

So: **legally placeable on the daughterboard; electrically marginal**. That is the honest verdict — not "impossible", and not "fine".

### 3.6 Verdict

| Statement | Verdict |
|---|---|
| "No caps on the mainboard ⇒ no SS path" | **Invalid as stated** (non sequitur) |
| "No series 0.1 µF anywhere in the SSTX net ⇒ no SS link" | **Valid** (this is V2) |
| "Caps on the daughterboard, none on the mainboard" | **Not prohibited by anything in the corpus**; the ECN's media language (RX) shows off-board coupling is contemplated; but it is **off Infineon's recommended placement** and SI-risky |
| "Caps on both boards" | **Invalid design** (50 nF < 75 nF) — a real, testable failure mode |

---

## 4. PHY supply: what must be powered, and is "missing PHY supply" plausible?

### 4.1 Rail inventory (core/IO vs SS-PHY-specific)

| Rail | Ball(s) | Domain | Voltage | Current | Filtering | Needed for HS? |
|---|---|---|---|---|---|---|
| **VDD** | B10, C3, E9, F11, H1, J11, L5, L7 | core logic | 1.2 V (1.15–1.25) | ≤ 200 mA total with AVDD **[Q]** | 22 µF group + 0.1/0.01 µF per pin; **no bead on VDD in the optimised design** (AN Fig.6) | **yes — essential** |
| **AVDD / AVSS** | A7 / B7 | PLL, crystal, analog | 1.2 V | included in the 200 mA | **2.2 µF + 0.1 µF, own bead**; ≤ 20 mV p-p | **yes — essential** |
| **U3RXVDDQ** | **A2** | **SS PHY RX** | 1.2 V (1.15–1.25) | **not specified**; inrush 800 mA/10 µs **[Q]** | **22 µF + 0.1 µF, own bead** | no (SS only) — but **must be connected** per App. C |
| **U3TXVDDQ** | **B5** | **SS PHY TX** | 1.2 V | same | same | no (SS only) — but **must be connected** per App. C |
| **U3VSSQ** | **A1** | **SS PHY ground** | 0 V | — | plane/vias | yes (ground) |
| **CVDDQ** | B6 | clock I/O | 1.7–3.6 V | small | 0.1 + 0.01 µF, bead | yes (clock) |
| **VIO1–VIO5** | L9/H11, F1, E3, B1, C11 | digital I/O | 1.7–3.6 V (VIO5 1.15–3.6) | small | 0.1 µF each | depends on interface |
| **VBATT / VBUS** | E10 / E11 | USB 2.0 transceiver + internal 3.3 V LDO | 3.2–6 V / 4.0–6 V | ≤ 60 mA | 0.1 µF (1 µF per checklist); OVP | **yes — essential** |

### 4.2 The key logical point

**[Q]** DS p.10 says the **same 1.2 V supply** must feed VDD, AVDD **and** both U3 rails. **[Q]** AN Fig.1 shows the SS rails tapped off that same `V1p2` node through beads.

**[D]** Therefore: *if the FX3 enumerates at all* — and it does, at HS — then:

* the 1.2 V source exists and is in regulation;
* the only ways A2/B5 can be dead are **an open ferrite bead**, **a shorted rail**, or **a missing/damaged 22 µF** (which does not remove the rail, but degrades inrush);
* a **design-level omission of the rails is ruled out by Appendix C**, which requires them even for a 2.0-only product.

### 4.3 Is "PHY supply missing" a plausible blocker?

| Variant of the hypothesis | Plausibility | Reason |
|---|---|---|
| The board was designed without the SS rails | **Very low** | Appendix C requires them even in 2.0-only designs **[Q]**; the datasheet requires the same 1.2 V source **[Q]** |
| A bead is open / not fitted | **Low but real** | A single missing 0805 bead kills SS TX or RX while leaving HS untouched. One continuity measurement settles it **[I]** |
| The 22 µF is missing / 2.2 µF fitted | **Low** | Does not stop SS by itself; risks the inrush/POR dip (0.83 V / 200 ns) **[Q]**; measurable at the cap |
| The rail is shorted | **Very low** | Would load the 1.2 V regulator and likely prevent enumeration altogether |
| Firmware never enables the SS PHY | **Not a supply issue** — but a real alternative | See §9 |

**Ranking.** "PHY supply missing" should sit **below** the reference-resistor hypothesis, the AC-coupling hypothesis, and the connector/daughterboard hypothesis. It is cheap to falsify (M6) but unlikely to be the answer.

### 4.4 A related failure mode worth naming

Even with the rails present, **inrush** is a genuine SS-specific failure mechanism **[Q, AN §4.2]**: at first SS-PHY enable the two rails draw up to 800 mA for ~10 µs; if the 1.2 V network sags below **0.83 V for > 200 ns**, the on-chip POR resets the whole device. A board with 2.2 µF instead of 22 µF, or a weak regulator, can therefore *reset when SS is enabled* — which a host may report as a disconnect/re-enumerate rather than a clean 2.0 link. This is untestable unpowered except by confirming the bulk capacitors (M6) **[I]**.

---

## 5. B3 / R_USB3 — how a 200 Ω 1 % resistor appears, and how to read it

### 5.1 What the part is

**[Q]** 200 Ω ±1 % from **B3** to GND (DS Table 7 p.18; AN Fig.14 p.21; AN §17.1 p.39). **No other component is specified on that net.** **[I]** The resistor is a plain thick-film chip resistor, **0201 or 0402**, unmarked (0402 and smaller chip resistors carry no value marking; 0603 1 % parts sometimes carry a 3-character E96 code, which in any case is not readable at 37 px/mm).

### 5.2 Optical signature — where to look

Ball geometry gives a small, searchable region **[D]**:

* **Which edge:** the **row-A edge**, at the **A1-corner end**, columns 2–4 — i.e. the corner where the SSRX pair (A3/A4) and the two 1.2 V rails (A2, B5) also escape. R_USB3 is the *only* two-terminal part that the datasheet requires on this corner group.
* **B3 is not an edge ball.** It sits at (2.6, 1.8) mm from the A1 corner, one row in from the row-A edge, directly "below" A3. It therefore cannot reach the board edge directly: it either **fans out through a via** (dog-bone) or escapes on the surface between the A2/A3 or A3/A4 ball pads (0.8 mm pitch − 0.35 mm pad = 0.45 mm gap; a 0.10 mm trace plus 2 × 0.10 mm clearance = 0.30 mm fits).
* The resistor will be within a few millimetres of that escape, **on the same side as the escape** — very often the **bottom side**, with the via dropping straight from the B3 pad to the resistor's pad.
* Its ground pad goes to a **ground via or the ground pour**, typically within 1 mm.
* Expected appearance: a single 2-terminal rectangle, **1.0 × 0.5 mm (0402 ≈ 37 × 18 px at 37 px/mm)** or **0.6 × 0.3 mm (0201 ≈ 22 × 11 px)**, light-brown body with bright tinned ends, **sitting alone** (not in a row of three), with one pad on a short trace to a via that comes from under the package and the other pad on a via/pour.

Two heuristic warnings **[I]**:

* **Absence on the top side proves nothing.** The natural layout is via-under-B3 → resistor on the **bottom** side → ground via. Check both sides before concluding.
* **Absence of *any* part on the net is a finding, not a defect.** Per Appendix C, B3 may be left open in a 2.0-only design. A missing R_USB3 is therefore *evidence of 2.0-only intent* (or of a population variant), and by itself explains a failed SS bring-up — but it does **not** prove the SS traces are absent.

### 5.3 What an unpowered meter reads

Assumptions: board unpowered, no stored charge, DMM in resistance mode, fine probes. "Expected" values are **[D]** from the network; the die behaviour is **[I]**.

| Probe pair | Expected reading | Caveats |
|---|---|---|
| **(a) B3 → GND** (not reachable without touching the ball; listed for completeness) | **200 Ω** if the die pin is high-Z when unpowered. If the die presents a parallel path to ground, **< 200 Ω**, and possibly **polarity-dependent** | BGA ball access is out of scope per the task; use (b)/(c) |
| **(b) resistor pad (B3 side) → GND** | **≈ 200 Ω** (the resistor), *unless* the die shunts it — then lower, and asymmetrical | This is the practical measurement |
| **(b′) resistor pad (GND side) → GND** | **0.0–0.3 Ω** (same net) | Confirms which pad is ground |
| **(c) pad → pad** | **198–202 Ω** (200 Ω ±1 %) if the die side is high-Z; **lower in one polarity** if the die clamps | The definitive "is the resistor there and correct" test |

**Why the die can pull the reading down.** Every FX3 pin has ESD structures to its rails. With the board unpowered, an ohmmeter's test current can forward-bias a pin→rail diode, so the "ground" seen through the pin is really *the die's rail network to ground*, a nonlinear, polarity-dependent path **[I]**. This is precisely the mechanism that can turn a 200 Ω resistor into a **128 Ω** reading — see §6.

### 5.4 Resistor vs "200 Ω of semiconductor / ground path"

| Property | 200 Ω chip resistor | Semiconductor / ESD path | Wrong-ground path |
|---|---|---|---|
| Polarity reversal (swap probes) | **identical** (±0.5 %) | **differs**, often > 2:1 | identical |
| Diode mode, both ways | **0.000 V** both ways | **0.4–0.8 V** one way, **OL** the other | 0.000 V |
| Test-current dependence (two meter ranges, or two meters) | **none** | **strong** | none |
| 2-wire vs 4-wire | difference = lead resistance only (0.2–1 Ω) | 4-wire does **not** linearise it | same |
| Capacitance mode | **≈ 0.1–0.3 pF** (below resolution) | 0.5–5 pF (ESD), 1–3 pF (die) | pF |
| To **different ground points** (connector GND, mounting hole, shield) | **same value at every true ground** | same value, but polarity flips | **different values** — the tell |
| Warm with a finger | < 0.1 % change (thick-film TCR) | diode drop falls ~2 mV/°C; resistance can halve | none |
| Linearity (apply a second, known resistor in parallel and check the parallel combination) | follows 1/R arithmetic exactly | does not | does not |

**The single cleanest discriminator:** *diode mode + polarity swap*. A resistor is a straight line through the origin; a junction is not.

---

## 6. The "128 Ω" puzzle

### 6.1 First, what 128 Ω is *not*

* **Not a trace.** Copper from a BGA ball to a connector pin is **0.02–0.5 Ω** (a 50 mm, 0.1 mm-wide 35 µm trace is ≈ 0.24 Ω; a plane is ≪ 0.1 Ω) **[D]**.
* **Not a standard 1 % value.** E96 has **127** and **130**; E192 adds **129** — **128 is in neither series**. So 128.0 cannot be a nominal value. It *is*, however, inside the ±1 % band of a **127 Ω** part (125.7–128.3) or a **129 Ω** part (127.7–130.3), so a genuine resistor remains possible **[D]**.
* **Not a capacitor** — a series 0.1 µF would make the resistance reading **ramp to OL**, and would read **100 nF** in capacitance mode **[D]**.

### 6.2 Hypotheses

| ID | Physical cause | How it arises on this board |
|---|---|---|
| **H1** | Direct trace / plane | Probes on two points of the same net |
| **H2** | 0 Ω link fitted | A jumper/0 Ω in the SS or supply path |
| **H3** | Ferrite bead DCR | Measuring across a bead (MPZ2012S601A class): DCR is a fraction of an ohm — **not 128 Ω** unless the bead is damaged/open |
| **H4** | Single resistor 100/120/127/130 Ω | A termination or bleeder; 128 Ω ≈ 130 Ω part + lead error, or 127/129 Ω E192 |
| **H5** | Two 200 Ω in parallel | R_USB3 + a second 200 Ω (e.g. on a daughterboard) → **100 Ω**, not 128 |
| **H6** | **200 Ω ∥ ~350 Ω** | R_USB3 fitted **and** the unpowered die shunting B3 with ≈ 330–360 Ω → **124–129 Ω** **[D]** |
| **H7** | ESD diode / junction | Probing a pin with no series part; forward-biased ESD structure |
| **H8** | **FX3 RX termination** | Probing **across** an SSRX pair that is DC-connected to the PHY. The USB 3.x **R_RX-DIFF-DC is 72–120 Ω** **[Q, ECN Table 6-1/6-2 p.3–4]** → 120 Ω + a few ohms of path = **~128 Ω** |
| **H9** | Ground-network parallel path / wrong "ground" | Second probe on a filtered ground (shield RC, split plane) |
| **H10** | Meter / contact error | Oxidised pad, probe pressure, long leads, low battery |
| **H11** | Series 0.1 µF | Included for completeness — would **not** read 128 Ω |
| **H12** | Rail with a bleed resistor | 1.2 V rail + a 120–130 Ω bleeder to ground (uncommon but possible) |

### 6.3 Decision table

Predictions are **[D]/[I]**; "≈" means within meter/lead error. Assumes a 2-wire DMM unless stated.

| Hypothesis | 2-wire R | 4-wire R | Reverse polarity | Diode mode | Capacitance mode | Different ground points | Repeatability / pressure | Matches 128 Ω? |
|---|---|---|---|---|---|---|---|---|
| **H1** trace/plane | 0.2–0.8 Ω (lead-dominated) | 0.02–0.2 Ω | identical | 0.000 V | < 5 pF | same | stable | **no** |
| **H2** 0 Ω link | 0.3–1.0 Ω | 0.05–0.2 Ω | identical | 0.000 V | < 5 pF | same | stable | **no** |
| **H3** ferrite bead | 0.1–0.6 Ω | 0.05–0.4 Ω | identical | 0.000 V | **asymmetric**: one side 22 µF/0.1 µF, other ~pF | same | stable | **no** (unless damaged: then OL) |
| **H4** 127/129/130 Ω resistor | value + 0.3–1 Ω | value ±1 % | identical | 0.000 V | ≈ 0.2 pF | same | stable | **yes** (127/129) |
| **H5** 2 × 200 Ω ∥ | 100 Ω | 100 Ω | identical | 0.000 V | ≈ 0.4 pF | same | stable | **no** |
| **H6** 200 Ω ∥ ~350 Ω | 124–129 Ω | same | **identical if both are resistors**; differs if the 350 Ω is the die | 0.000 V if purely resistive; junction if die | ≈ 0.2–2 pF | same | stable | **yes** |
| **H7** ESD diode | 0.3–3 kΩ one way, **OL** the other | same (nonlinear) | **differs strongly** | **0.4–0.8 V / OL** | 0.5–5 pF | same | drifts with warmth | **no** (but a *parallel* diode can pull a resistor down) |
| **H8** RX termination (DC-connected pair) | **72–120 Ω** (spec) + path ⇒ **~128 Ω** | same | **differs** (ECN note: "ΔV<0 … could be as low as 0 Ω") | may show a junction | 1–3 pF | same | sensitive to meter current | **yes, plausibly** |
| **H9** wrong ground | **depends on the ground chosen** | same | identical | 0.000 V | depends | **varies strongly** | stable | possible |
| **H10** contact/meter | 5–500 Ω, **unstable** | often much lower | varies | erratic | erratic | varies | **changes on re-seat** | possible |
| **H11** series 0.1 µF | **ramps to OL** | ramps to OL | same | OL/charging | **100 nF** | — | — | **no** |
| **H12** rail + bleeder | 120–130 Ω | same | identical | 0.000 V | **22 µF + 0.1 µF** | same | stable | **yes** |

### 6.4 The procedure that separates them (S1–S8, all unpowered, all safe)

| Step | Action | Decision rule |
|---|---|---|
| **S1** | Short the probes in 2-wire; note the offset. Repeat in 4-wire if available | Offset > 2 Ω ⇒ H10 is live; clean/replace leads before trusting anything |
| **S2** | Re-measure the same two points 3×, re-seating each time | Spread > 10 % ⇒ H10 (or a junction). Stable ⇒ resistive network |
| **S3** | Reverse polarity | Ratio > 1.2 ⇒ **H7/H8** (nonlinear). Identical ⇒ **H4/H5/H6/H12** |
| **S4** | Diode mode, both polarities | 0.4–0.8 V / OL ⇒ junction (**H7**), *even if* a resistor is also present — the diode is in parallel |
| **S5** | Capacitance mode across the same points | **100 nF** ⇒ series cap (**H11**); **22 µF** ⇒ rail (**H12**); **50 nF** ⇒ double coupling; pF ⇒ resistor/trace |
| **S6** | Measure the same node to **three** ground points (connector GND pin, mounting hole, shield) | Values differ ⇒ **H9**; identical ⇒ true ground reference |
| **S7** | 4-wire (or null the leads) | Drops by 20–40 Ω ⇒ H10 dominated; drops by ~1 Ω ⇒ real value |
| **S8** | Compare with the expected schematic value for that net (R_USB3 = 200 Ω; SS pair diff = 72–120 Ω; rail = 22 µF) | The hypothesis that matches *both* the DC and the C-mode reading wins |

**Most probable readings of the reported 128 Ω, in order [I]:**

1. **H8** — if the probes were across an SS pair that is DC-connected to the FX3 (the ECN's own 72–120 Ω differential window plus path resistance lands at ~128 Ω). *This would be good news: it would prove the pair is routed to the PHY.*
2. **H6** — if the probes were B3↔GND and R_USB3 is fitted; the die's parallel path pulls 200 Ω down.
3. **H10** — if the value moved when the probes were re-seated.
4. **H4** — a 127/129 Ω E192 part (unlikely on a USB ref-design net, which specifies 200 Ω).

**What would make 128 Ω decisive:** the probe points. Without them the number is not interpretable — which is why S1–S8 start by re-establishing *where* the probes were.

---

## 7. Ranked unpowered measurement plan

Constraints: **no power, no soldering**; access to 40-pin connector pins, two-terminal parts near the FX3, test points, and (optionally) package-edge trace stubs/vias — **not** BGA balls. Ranked by *information per unit of risk*.

Risk scale: **0** = none; **L** = low (large pads, meter current only); **M** = moderate (small pads, probe slip can bridge); **H** = high (0.1 mm features, ESD/probe-slip damage to the FX3 possible).

### 7.0 Preconditions (do these first, they cost nothing)

* Confirm the board is **fully unpowered** (USB and any battery/DC jack removed) and that no bulk capacitor holds charge. A 22 µF at 1.2 V holds ~16 µJ — harmless, but it will confuse a resistance reading until discharged.
* Use a **grounded wrist strap**; the FX3 SS pins are only ±2.2 kV HBM **[Q, DS p.13]**.
* Use a DMM whose resistance mode is **< 1 V open-circuit** (most are); avoid "continuity beep" modes with high compliance.
* Photograph both sides at high magnification **before** probing, so every later reading has a coordinate.

### 7.1 The plan

| # | Risk | Measurement | Probe points | Expected under each hypothesis | Rules in / out |
|---|---|---|---|---|---|
| **M0** | **0** | **Optical inventory (both sides)** | — | — | Locate pin-1 (A1) corner; count 2-terminal parts within ~4 mm of the A2–A7/B3–B7 group; look for a lone 0402 near the B3 escape; look for a 10-pin UDFN (SP3010) near the connector; check the bottom side under the package shadow | Establishes whether R_USB3/caps/ESD are even present before touching anything. **Not conclusive alone** (0201 parts, bottom side, shield) |
| **M1** | **0** | **Meter validation** | probes shorted; then across a known 100 Ω 1 % resistor if available | offset 0.1–0.5 Ω (2W), ~0 Ω (4W) | Calibrates every later number. If offset > 2 Ω, fix leads before proceeding |
| **M2** | **L** | **Ground map** | 40-pin GND pins ↔ mounting hole ↔ shield ↔ test-point ground | < 0.5 Ω on a common ground; > 10 Ω ⇒ filtered/split ground | Prevents H9 (wrong ground) from poisoning later readings; identifies a true ground reference |
| **M3** | **L** | **Connector pin survey (C and R to GND)** | each of the 40 pins → M2 ground, one at a time | power pins: large C (1–22 µF); GND: 0 Ω; signal: pF + OL; a pin with a **series cap to an IC**: small C to GND | Builds a pin map without any schematic; finds candidate SS pins (small C, no DC path) |
| **M4** | **L–M** | **Pair identification** | candidate pins, pin-to-pin | DC-connected pair: **72–130 Ω** (H8). AC-coupled pair: **open**, and pin-to-pin C is small (each line has its own cap; the only series path between the two pins runs through the unpowered die, which is high-Z — a **≈50 nF** pin-to-pin reading would itself be evidence that *both* caps are fitted and that the die has a path) | Distinguishes a differential pair from two unrelated signals |
| **M5** | **M–H** | **★ The decisive test: connector SS pin ↔ FX3-side escape via/stub** (A3/A4/A5/A6) | one connector pin vs its corresponding via/trace stub; **C mode first, then R mode** | **≈100 nF, DC open** ⇒ caps on **mainboard** (SS path present, properly coupled) • **≈0 Ω, ≈5 pF** ⇒ caps on **daughterboard** (path present, coupling remote) • **≈50 nF** ⇒ **double coupling** (V3 — design error) • **open, ≈0 pF** ⇒ **no path** (V1) | **This one measurement decides §3.** Do C-mode first (meter injects < 1 V); use the via, not the 0.1 mm trace, if possible |
| **M6** | **L** | **Rail/bead check** | the 22 µF caps near the FX3 (both ends of each bead) | one side ≈ 22 µF to GND, other side ≈ 0 ⇒ bead intact and rail decoupled; both sides ≈ 22 µF ⇒ bead shorted; neither ⇒ cap missing/open | Falsifies/confirms §4 in one pass. Also identifies which of the two rails (A2 vs B5) is affected |
| **M7** | **M** | **R_USB3 verification** | candidate 0402 near the B3 escape, pad-to-pad and pad-to-GND | 198–202 Ω pad-to-pad; 0 Ω one pad to GND; **< 200 Ω and polarity-dependent** ⇒ die shunts it (still fitted) | Confirms the part and its value; distinguishes H4/H6 from "no resistor" |
| **M8** | **M** | **★ The 128 Ω re-measurement** | documented points from the prior measurement | See §6.3 | Turns an anecdote into evidence. Use S1–S8 |
| **M9** | **H** | **Package-edge stub probing** (last resort) | exposed trace stub / via at row A, columns 2–7 | continuity to the connector; C to the connector | Only if M5 is ambiguous. Highest ESD/slip risk; **never** probe between two balls with a single tip |
| **M10** | **L** | **ESD array mapping** (if a 10-pin UDFN is found near the connector) | its pins to connector pins | 4 channels map 1:1 to the four SS lines; 0.45 pF per line | Gives the SS net names **without** touching the BGA — the best "free" win if the array is fitted |

### 7.2 What each measurement rules out, in one line

* **M5 = 100 nF** ⇒ the mainboard *does* carry the AC caps ⇒ §3's premise is false and the 2.0 link has another cause (R_USB3, bead, firmware, connector).
* **M5 = 0 Ω + 5 pF** ⇒ the SS pairs *are* routed to the connector and are DC-coupled on the mainboard ⇒ the daughterboard must carry the caps; if it does not, **V2 is confirmed**.
* **M5 = open** ⇒ **V1 confirmed**; the SS pairs never leave the FX3/connector region ⇒ the 2.0 link is a design consequence, not a fault.
* **M7 = no part** ⇒ the board was built to the Appendix C 2.0-only recipe (or a population variant) ⇒ a missing R_USB3 alone explains the failed SS bring-up.
* **M6 = open bead / missing cap** ⇒ a real, repairable SS-only fault.

### 7.3 Probe-safety notes specific to this package

* The SS balls are in **row A**, and **B3/B5 sit one row in**; that corner is dense with **dog-bone vias on a 0.8 mm pitch** (AN §11 p.20 recommends 11 mil BGA pad, mask and paste) **[Q]**. A slip there can short a 1.2 V rail to a signal.
* Prefer **vias and connector pins** over ball-adjacent traces. A via barrel is ~0.25–0.3 mm and mechanically tolerant; a 0.1 mm trace is not.
* Measure **capacitance before resistance** on any net that might be a rail: a resistance measurement on a 22 µF net is meaningless until the cap charges, and the charging current can look like a "reading".

---

## 8. Which two-terminal parts could be the SSTX 0.1 µF pair?

### 8.1 The identifying criteria (in order of strength)

| # | Criterion | Why it is decisive |
|---|---|---|
| 1 | **In series, not shunt** — *neither* pad reads 0 Ω to ground | A decoupling capacitor has one pad on GND. An AC-coupling capacitor has **both** pads on signal nets. This single test eliminates most of the small parts on the board |
| 2 | **C = 100 nF ±10 %** (pad-to-pad) | The specified value (AN §12.1, Table 11 #15) |
| 3 | **DC open both ways** (resistance ramps to OL) | Confirms a capacitor, not a resistor/bead |
| 4 | **It comes in a pair**, identical, same orientation, on **two parallel traces** | One per SSTX line, "placed symmetrically" (AN §12.1 p.21) |
| 5 | **Position**: near the receptacle *or* near the FX3 TX pins, depending on the far end (AN p.21, §17.1 p.39) | On this board the far end is a connector/daughterboard, so either the connector end or the A5/A6 escape is plausible |
| 6 | **Size 0402 (1.0×0.5 mm) or 0201 (0.6×0.3 mm)**, MLCC body (light brown/grey, bright tinned ends) | Larger parts (0603+) are implausible for a 5 Gbps line with a ground cut-out |
| 7 | **A ground cut-out under the part** (visible as a clearance in the adjacent copper) | Required by AN p.21/Fig.16–17 |

### 8.2 Package marking

**MLCCs carry no value marking** — a 0.1 µF 0402 and a 1 µF 0402 are visually identical. There is therefore **no marking that can confirm the part**; only the **size** (0402/0201) plus the **electrical tests** above. Any claim of the form "this part is marked X therefore it is the AC cap" is unsupportable **[I]**.

### 8.3 What I actually looked at (my own observation, not a measurement)

I cropped `hardware/photos/P1_top_side.webp` around the FX3 and the connector (crop boxes `(1330,880)-(1720,1160)` and `(880,640)-(1160,900)`, 4–5× LANCZOS upscale; the board is photographed rotated 180°, so the FX3's pin-1 triangle appears at the lower-right of the package in image coordinates) **[O]**:

* The **A1 corner carries a white triangle** and the silkscreen designator **U3**; the package edge is ringed with **dog-bone vias at 0.8 mm pitch** — consistent with the ball map and with the AN's 11 mil pad recommendation **[O]**.
* **I found no two-terminal part in the cropped field around the A1/B3 corner on the top side** (the crop reaches ≈5 mm beyond the package edge on the left, right and bottom). That is consistent with R_USB3 living on the **bottom side** (or not being fitted) — it is **not** evidence either way **[O/I]**.
* Between the FX3 and the 40-pin connector there is a cluster of small parts that includes **a row of three small two-terminal parts inside one silkscreen box**, with a larger (0805/1206-class) part immediately below **[O]**. That grouping is the familiar shape of a **power-rail decoupling bank**, **not** of a two-cap AC-coupling pair. I did **not** measure it, and I do not assign it.
* On the bottom side (`P2_bottom_side.webp`, crop `(360,700)-(760,940)`) there are **labelled test points "DP" and "DN"** with two ~1 mm bare pads **[O]**. This matters for §7: the board exposes USB 2.0 test points but I saw **no equivalent SS test points**. That is weak evidence that SS debug was not a design priority — **not** proof.

**Bottom line for §8.** The AC caps, if fitted, are **two identical 0402/0201 MLCCs in series on the two SSTX traces**. The way to find them is not to look for a marking but to **test candidate parts for "neither pad grounded, 100 nF, DC open"**. If no such pair exists on either side, then either the caps are on the daughterboard (V2/§3) or the design never had an SS path (V1).

---

## 9. Residual unknowns and what would change the conclusion

| # | Unknown | Why it matters | What resolves it |
|---|---|---|---|
| U1 | **Are the SS pairs routed to the 40-pin connector at all?** | The whole of §3 hinges on it | M4/M5 |
| U2 | **Do the SS pin names appear on the connector?** (no schematic) | Determines V1 vs V2 | M5, or daughterboard inspection |
| U3 | **Is R_USB3 fitted, and on which side?** | A missing/invalid 200 Ω alone explains a failed SS bring-up; 128 Ω may be evidence it *is* fitted | M7 + bottom-side optical |
| U4 | **What were the 128 Ω probe points?** | Without them the number is not interpretable | M8 (repeat with documentation) |
| U5 | **Is the 22 µF / 2.2 µF population correct, and are the beads intact?** | Inrush/POR is a real SS-only failure mode | M6 |
| U6 | **Does the FX3 firmware actually attempt SS?** (descriptor/PHY config) | A firmware decision to run 2.0-only would mimic a hardware fault | Firmware/USB descriptor capture (out of scope here) |
| U7 | **Is the observed 2.0 link stable, or does it re-enumerate?** | Distinguishes "no SS path" from "POR on SS enable" | Host-side enumeration log |

**Falsification summary.** The memo's main claims are falsifiable by single measurements:

* **§3** is falsified if **M5 reads ≈100 nF** (caps *are* on the mainboard) or if the daughterboard is found to carry them (then the path exists and the cause is elsewhere).
* **§4** is falsified if **M6 finds an open bead or a missing 22 µF** — that would promote "PHY supply" from low to leading hypothesis.
* **§5/§6** are falsified if **M7 shows no 200 Ω part anywhere** near B3 on either side (a design omission, not a fault) or if **M8 reproduces 128 Ω with a documented, DC-connected SS pair** (which would *confirm* the pair is routed — the opposite of the "no path" conclusion).

---

## Appendix A — Verbatim passages used (with locations)

**A1. Appendix C, USB-2.0-only design** — AN 001-70707 Rev \*Q, §18, p.40 **[Q]**:
> "• FX3/SX3 SuperSpeed pins TX+/- pins (A5 and A6) and RX+/- pins (A3 and A4) can be left open.
> • Both USB3.0 power pins (U3TXVDDQ and U3RXVDDQ) should be connected to 1.2V with a ferrite bead.
> • The USB3 reference pin (B3) can be left open.
> • Make sure that layout guidelines are followed per Section 12.1."

**A2. AC coupling** — AN §12.1, p.21 **[Q]**:
> "USB 3.0 traces require additional AC coupling capacitors (0.1 µF) placed on the SS_TX lines. Place these capacitors symmetrically and close to the EZ-USB™ FX3 device. See Figure 15."

**A3. AC-cap placement, topology-dependent** — AN §12.1, p.21 **[Q]**:
> "AC coupling capacitors should have a cut-out in the shape of these capacitors on the immediate solid GND layer to reduce reflection on the super speed signals. It is mandatory to provide the ground reference on layer, which is immediate to solid GND layer. … AC caps should be placed near to USB connector only for Host and device implementation which helps to avoid the cable noise reaching to FX3 device. AC caps should be placed near to FX3 device only if FX3 connected to HUB or other emdedded controller."

**A4. Same rule, troubleshooting wording** — AN §17.1, p.39 **[Q]**:
> "**TX A/C capacitor:** A/C capacitor should be connected on TX lines of the FX3/SX3 device. This will prevent the common-mode noise from the cable/host entering FX3/SX3 device TX lines. The AC capacitor should be near to the type-B receptacle for the application which uses the type-B receptacle; the AC capacitor should be near to the FX3S/SX3 TX pins for the application which uses the hub/embedded controller."

**A5. Reference resistors** — AN §17.1, p.39 **[Q]**:
> "**USB reference resistor:** Make sure that the USB3 reference pin (B3) is connected to a resistor (200E) with +/- 1% tolerance. Make sure that the USB2 reference pin (C8) is connected to a resistor (6.04k) with +/- 1% tolerance." *(the "200E" is the app note's own notation for 200 Ω — sic)*

**A6. Reference resistor values** — DS Table 7, p.18 **[Q, verified visually]**:
> "R_usb2 — Precision resistor for USB 2.0 (Connect a 6.04 kΩ ±1% resistor between this pin and GND)"
> "R_usb3 — Precision resistor for USB 3.0 (Connect a 200 Ω ±1% resistor between this pin and GND)"

**A7. SS rails** — DS p.22 **[Q]**:
> "U3TXVDDQ — USB 3.0 1.2-V supply — 1.15 / 1.25 V — 1.2-V typical. A 22-µF bypass capacitor is required on this power supply."
> (identical row for U3RXVDDQ)

**A8. Same 1.2 V source** — DS p.10 **[Q]**:
> "VDD … The same supply must also be used for the following: • AVDD: This is the 1.2-V supply for the PLL, crystal oscillator, and other core analog circuits • U3TXVDDQ/U3RXVDDQ: These are the 1.2-V supply voltages for the USB 3.0 interface."

**A9. Inrush / POR** — AN §4.2, pp.8–9 **[Q]**:
> "When the USB3.0 SuperSpeed PHY is enabled for the first time, or a reset event; an initial inrush current is expected on the 1.2 V U3RXVDDQ and U3TXVDDQ supplies for ~10 µs. The magnitude of this current can be as high as 800 mA. … The POR circuit can fire if the 1.2-V core VDD voltage falls to less than 0.83 V for more than 200 ns."

**A10. ESD** — AN §10, p.19 **[Q]**:
> "The SSRX+, SSRX-, SSTX+, SSTX- pins have only up to ±2.2-kV human body model (HBM) internal ESD protection. You can include additional protection to these pins by using high-performance, low-capacitance external ESD devices (SP3010-04UTG), as shown in Figure 12. To prevent an effect on the performance of this bus, the added capacitance should not exceed 0.5 pF."

**A11. RX AC coupling option** — ECN Table 6-2, p.4 **[Q]**:
> "C RX-AC-COUPLING — AC Coupling Capacitor — 297 (min) / 363 (max) nF — Receivers may be AC coupled if desired. If used, the AC coupling is required to be either within the media or within the receiving component."

**A12. RX impedance and its polarity caveat** — ECN Table 6-1, p.3 **[Q]**:
> "R RX-DC … 18 (min) 30 (max) Ω … R RX-DIFF-DC … 72 (min) 120 (max) Ω … Z RX-HIGH-IMP-DC-POS … 10k (min) Ω … Rx low frequency CM impedance with the Rx terminations not powered"
> "Note 1. Impedance is only specified for ∆V>0. ∆V<0 is not specified and could be as low at 0Ω."

**A13. Decoupling table** — AN Table 3, p.8 **[Q]**:
> "AVDD (A7) — 2.2 µF — 0.1 µF; U3RXVDDQ (A2) — 22 µF — 0.1 µF; U3TXVDDQ (B5) — 22 µF — 0.1 µF; VDD (B10, J11) — 22 µF — 0.01 µF and 0.1 µF"

---

## Appendix B — Ball-map extract and coordinate arithmetic

**B1.** 121-ball, 10 × 10 mm, 0.8 mm pitch **[Q, DS Features p.1 and Fig.7 p.13]**. 11 × 11 balls; centre-to-centre span 10 × 0.8 = 8.0 mm; edge margin = (10 − 8.0)/2 = **1.0 mm** to the first ball centre **[D]**.

**B2.** Row A, columns 1–11 (top view) **[Q, DS Fig.7 p.13]**: `A1 U3VSSQ | A2 U3RXVDDQ | A3 SSRXM | A4 SSRXP | A5 SSTXP | A6 SSTXM | A7 AVDD | A8 VSS | A9 DP | A10 DM | A11 NC`.
Row B: `B1 VIO4 | B2 FSLC[0] | B3 R_USB3 | B4 FSLC[1] | B5 U3TXVDDQ | B6 CVDDQ | B7 AVSS | B8–B9 VSS | B10 VDD | B11 TRST#`.

**B3.** Pixel scale (from the share brief's nominal 37 px/mm, which I did **not** re-derive): 0.8 mm pitch = **29.6 px**; 0402 = **37 × 18 px**; 0201 = **22 × 11 px**; 0603 = **59 × 30 px**. Treat these as order-of-magnitude for optical search only **[I]**.

---

## Appendix C — Arithmetic

| Quantity | Working | Result |
|---|---|---|
| Two 0.1 µF in series | 0.1·0.1/(0.1+0.1) | **50 nF** |
| 0.1 µF + 0.33 µF in series | 0.1·0.33/0.43 | **76.7 nF** (inside 75–265 nF) |
| 200 Ω ∥ 340 Ω | 200·340/540 | **125.9 Ω** |
| 200 Ω ∥ 350 Ω | 200·350/550 | **127.3 Ω** |
| 200 Ω ∥ 360 Ω | 200·360/560 | **128.6 Ω** |
| 200 Ω ∥ 200 Ω | — | **100 Ω** |
| 6.04 kΩ ∥ 200 Ω | — | **193.6 Ω** |
| 0.1 µF at 2.5 GHz (ideal C) | 1/(2π·2.5e9·1e-7) | **0.64 mΩ** |
| 0.5 nH ESL at 2.5 GHz | 2π·2.5e9·0.5e-9 | **7.9 Ω** per cap |
| 0.1 µF + 0.5 nH series resonance | 1/(2π√(LC)) | **≈ 22.5 MHz** |
| Trace resistance, 50 mm × 0.1 mm × 35 µm Cu | ρL/A, ρ = 1.7e-8 Ω·m | **≈ 0.24 Ω** |
| A1 corner offset | (10 − 11×0.8)/2 | **1.0 mm** |
| Ball pitch in px at 37 px/mm | 0.8 × 37 | **29.6 px** |

---

*End of memo. No hardware was touched; no measurement in this document was made by me. All readings marked **[D]/[I]** are predictions to be checked against §7.*
