# EZ-USB FX3 (CYUSB3014) — SuperSpeed hardware design & PCB layout requirements

**Purpose.** Establish, from primary Infineon/Cypress and USB-IF documents, exactly what a
CYUSB3014 design must contain and how it must be laid out for USB 3.0 SuperSpeed (5 Gb/s), so
that we can judge whether a USB-2.0-only PCB can be modified into a working SuperSpeed port.

**Scope.** Device side only (FX3 as a USB 3.0 peripheral), Standard-B and Micro-B receptacles.
Not covered: host-side design, Type-C/PD, hub silicon.

**Compiled** 2026-11-07. All numeric values below are quoted from the cited document; anything
computed or inferred here is explicitly marked `DERIVED` or `ESTIMATE`.

---

## 0. Confidence scheme and source ledger

| Tag | Meaning |
|---|---|
| **CONFIRMED** | Stated verbatim (or directly read off a schematic/figure) in a primary Infineon/Cypress or USB-IF document. |
| **PROBABLE** | Consistent across secondary sources / industry practice, or implied but not stated by a primary doc. |
| **UNVERIFIED** | Could not be confirmed from a primary source in this session; treat as a hypothesis. |
| **DERIVED** | Arithmetic or geometric consequence of CONFIRMED values (work shown). |

### Sources

| ID | Document | Revision / date | URL |
|---|---|---|---|
| **S1** | AN70707 — *EZ-USB™ FX3/FX3S/SX3 hardware design guidelines and schematic checklist* | Rev. \*Q (v18), 2021-10-26 | <https://www.infineon.com/dgdl/Infineon-AN70707_EZ-USB_FX3_FX3S_SX3_hardware_design_guidelines_and_schematic_checklist-ApplicationNotes-v18_00-EN.pdf?fileId=8ac78c8c7cdc391c017d0739793e5dfd> · archived copy used: <http://web.archive.org/web/20221121042018/https://www.infineon.com/dgdl/Infineon-AN70707_EZ-USB_FX3_FX3S_SX3_hardware_design_guidelines_and_schematic_checklist-ApplicationNotes-v18_00-EN.pdf?fileId=8ac78c8c7cdc391c017d0739793e5dfd> |
| **S2** | AN70707 — earlier revision (*FX3/FX3S* title, pre-SX3) | Rev. \*G, 2014-04-11 | <https://svn.code.sf.net/p/jaer/code/devices/firmware/CypressFX3/docs/AN70707%20-%20EZ-USB%20FX3%20Hardware%20Design%20Guidelines%20and%20Schematic%20Checklist.pdf> |
| **S3** | CYUSB301X datasheet — *EZ-USB FX3 SuperSpeed USB Controller* | 001-52136 Rev. \*O, 2014-05-02 | <https://svn.code.sf.net/p/jaer/code/devices/firmware/CypressFX3/docs/CYUSB301X%20-%20EZ-USB%20FX3%20SuperSpeed%20USB%20Controller.pdf> |
| **S4** | CYUSB3KIT-003 SuperSpeed Explorer Kit User Guide (contains the USB-connector, ESD, VBUS, crystal schematics) | 001-93186 Rev. \*A, 2014 | <https://www.infineon.com/dgdl/Infineon-SuperSpeed_Explorer_Kit_User_Guide-UserManual-v01_00-EN.pdf?fileId=8ac78c8c7d0d8da4017d0ef82cf70d57> · archived: <http://web.archive.org/web/20240907152401/https://www.infineon.com/dgdl/Infineon-SuperSpeed_Explorer_Kit_User_Guide-UserManual-v01_00-EN.pdf?fileId=8ac78c8c7d0d8da4017d0ef82cf70d57> |
| **S5** | USB 3.1 ECN *Rx AC Coupling Capacitor Option* (USB-IF) | Form 20131608-ECN | <https://emb.macnica.co.jp/macnica_wp/wp-content/uploads/2019/02/USB-3.1-ECN-Rx-AC-Coupling-Capacitor-Option.pdf> |
| **S6** | USB 3.0 *Cable & Connector Compliance Document* (USB-IF) | Rev. 1.02, 2011-10-04 | <http://ieb-srv1.upc.es/gieb/tecniques/pdf/USB_normes/CabConn_3_0_Compliance_Document_v1.02_2011-10-04.pdf> |
| **S7** | USB-IF — *Managing Connector and Cable Assembly Performance for USB SuperSpeed* | Rev. 1.0 | <https://www.usb.org/sites/default/files/USB_SuperSpeed_CabCon_Whitepaper.pdf> |
| **S8** | USB-IF — *USB 3.0 Super Speed Equalizer Design Guidelines* | 2011-06-10 | <https://usb.org/sites/default/files/USB_Superspeed_Equalizer_Design_Guidelines_2011-06-10.pdf> |
| **S9** | Littelfuse SP3010-04UTG datasheet (4-channel 0.45 pF ESD array) | — | <https://www.avaq.com/files/uploads/datasheets/aa89ddb4-2db1-448a-be90-10cedf138e19.pdf> |
| **S10** | EZ-USB FX3 SDK *API Guide* (Doxygen; `CyU3PConnectState`) | SDK 1.3.x | <https://community.infineon.com/gfawx74859/attachments/gfawx74859/usb-superspeed-peripherals/38623/1/FX3APIGuide.pdf> |
| **S11** | AtlasPCB — *BGA Fanout Routing Strategies* (secondary, fab process guidance) | 2026-05 | <https://www.atlaspcb.com/blog/bga-fanout-routing-strategies-dog-bone-via-in-pad/> |
| **S12** | Infineon AN140 — TVS diode selection for USB 3.0 (Infineon ESD parts) | — | <https://www.infineon.com/dgdl/AN140.pdf?fileId=db3a304317a748360118089324954410> |

**Access note.** `infineon.com/dgdl/*` and `community.infineon.com` are behind a Cloudflare
challenge from this environment (HTTP 403). S1 and S4 were retrieved through the Internet
Archive; S2/S3 through a public SVN mirror of Cypress documentation. S12 (AN140) could not be
retrieved at all and is referenced only as a pointer. Quoted text below is taken from the
retrieved PDFs, not from memory.

---

## 1. Mandatory external components on the SuperSpeed path

### 1.1 AC-coupling capacitors — **mandatory, 0.1 µF, on SSTX only**

| Parameter | Value | Source / confidence |
|---|---|---|
| Value | **0.1 µF** | S1 §12.1: "USB 3.0 traces require additional AC coupling capacitors (0.1 µF) placed on the SS_TX lines." **CONFIRMED**. Repeated in S1 Table 11 schematic checklist item 15: "Does the TX pins of FX3 has AC (0.1uF) capacitor?" and layout checklist item 10. |
| Quantity | **2** (one per line, SSTX+ and SSTX−), placed **symmetrically** | S1 §12.1 ("Place these capacitors symmetrically"). **CONFIRMED** |
| Which side | **Transmitter side.** For a device with a receptacle this is the FX3's SSTX (balls A5/A6) | S1 §12.1 + Appendix B. **CONFIRMED** |
| Allowed total series capacitance | **75 nF … 265 nF** | S5 states the window explicitly: "combined value of 100nF (TX side) and 330nF (Optional RX side) can be meet minimum capacitance requirement for RX detection (**75nF~265nF**)". This is the USB 3.x transmitter AC-coupling range; 0.1 µF sits at its centre. **CONFIRMED** |
| Optional RX capacitors | **330 nF nominal (297–363 nF)** — optional | S5 Table 6-2 (`C RX-AC-COUPLING`, min 297 nF, max 363 nF): "Receivers may be AC coupled if desired." **CONFIRMED**. FX3 reference designs do **not** fit them; not required. |
| Tolerance | Not specified by Cypress. A ±10 % X7R keeps 100 nF inside 75–265 nF with large margin | **PROBABLE / DERIVED** |
| Package | Not specified by Cypress. 0402 is the de-facto choice; 0201 on dense boards | **UNVERIFIED** (no primary statement) |
| Voltage rating | Not specified. 16 V (or 25 V) X7R is universal practice; the part only sees the SS common-mode bias | **PROBABLE** |
| Required or optional? | **Required.** AN70707 says the traces "require **additional** AC coupling capacitors", i.e. there is no on-die TX AC coupling. Without them there is a DC path between two current-mode transmitters and the link cannot train | S1 §12.1, S1 Table 11 item 15. **CONFIRMED** |

**Placement (this changed between revisions — use v18):**

- **v18 (S1 §12.1, and Appendix B "TX A/C capacitor"):** for a **device with a receptacle**,
  place the AC capacitors **near the USB connector**; place them near the FX3 **only** when the
  FX3 is connected to a hub or another on-board controller.
  > "AC caps should be placed near to USB connector only for Host and device implementation
  > which helps to avoid the cable noise reaching to FX3 device. AC caps should be placed near
  > to FX3 device only if FX3 connected to HUB or other embedded controller." — S1 §12.1
  **CONFIRMED**
- Older **Rev \*G (S2)** said only "close to the EZ-USB FX3 device". **Superseded** — do not
  cite the old rule.
- **Mandatory ground cut-out:** the AC capacitors must have a **cut-out in the immediate solid
  GND layer in the shape of the capacitor**, and there must be a ground reference on the layer
  immediately below that. S1 §12.1: "It is mandatory to provide the ground reference on layer,
  which is immediate to solid GND layer." Checklist items 11 & 17. **CONFIRMED**

### 1.2 ESD protection — **recommended; ≤ 0.5 pF added capacitance**

- FX3's SuperSpeed pins have **only ±2.2 kV HBM** internal protection, versus ±8 kV contact /
  ±15 kV air-gap (IEC61000-4-2 level 4C) on D+/D−/GND.
  > "The SSRX+, SSRX-, SSTX+, SSTX- pins have only up to ±2.2-kV human body model (HBM)
  > internal ESD protection." — S1 §10; identical sentence in S3 (datasheet). **CONFIRMED**
- Cypress's recommended external part: **SP3010-04UTG** (Littelfuse), a 4-channel array.
  S1 §10: "You can include additional protection to these pins by using high-performance,
  low-capacitance external ESD devices (SP3010-04UTG)". **CONFIRMED**
- **Capacitance limit:** "To prevent an effect on the performance of this bus, the added
  capacitance should not exceed **0.5 pF**." — S1 §10. **CONFIRMED**
- SP3010-04UTG actual value: **0.45 pF typ per I/O**, 4 channels, IEC61000-4-2 ±8 kV contact
  (S9). **CONFIRMED** — i.e. it sits just under Cypress's ceiling, which is why it is the
  named part.
- **Placement:** "ESD devices should be placed as close to the receptacle as possible."
  — S1 §12.1 (text accompanying Fig. 36). **CONFIRMED**. They go **connector side of the AC
  caps** (the SSEK schematic in §4 shows exactly this ordering: connector → ESD → FX3).
- **Infineon alternative:** the ESD5V3U1U family (e.g. ESD5V3U1U-02LRH) is Infineon's own
  USB 3.x TVS and is discussed in AN140 (S12) against the SP3010. Its line capacitance could
  **not** be verified here (`infineon.com` blocked) — **UNVERIFIED**. Do not assume it meets
  0.5 pF without checking its datasheet.
- Board-level note: the **SSEK uses one SP3010-04UTG for all four SS lines** (S4, Figure 4-2;
  see §4.3 below). That is the reference-design practice. **CONFIRMED**

### 1.3 Common-mode chokes — **not required**

- USB 3.0 does **not** require a common-mode choke on SuperSpeed pairs, and **AN70707 lists no
  CMC** anywhere — not in §12.1, not in the schematic checklist (15 items), not in the layout
  checklist (18 items). **CONFIRMED by absence** in S1.
- The Cypress reference design has **no CMC on SS**: the SSEK schematic shows only
  - **L2 = BLM21PG221SN1D** in series with **VBUS**, and
  - **L4 = BLM21PG221SN1D** in the **shield** RC/LC termination,
  and **nothing in series with SS_TX/SS_RX** (S4 Figure 4-2). **CONFIRMED**
- Practical position: a CMC is an **optional EMC measure**, not a functional requirement. If
  one is added it must be a SuperSpeed-rated (wideband, low differential-to-common-mode
  conversion) part and it must be placed on the **connector side of the AC capacitors** so the
  caps still isolate the cable. **PROBABLE** (reasoning from S1's stated purpose for the caps:
  "prevent the common-mode noise from the cable/host entering FX3/SX3 device TX lines",
  S1 Appendix B).

### 1.4 Series resistors and termination — **none on SSTX/SSRX**

- **No series resistors** are specified or fitted on SSTX/SSRX in any Cypress document.
  AN70707's only series-resistor requirement is **22 Ω on GPIF II lines** (checklist item 9).
  **CONFIRMED by absence / by contrast.**
- The only SS-related passives are:
  - **R_USB3 = 200 Ω ± 1 % from ball B3 to GND** — the USB 3.0 PHY reference resistor.
    S1 §12.1: "The R_USB3 pin should be tied to ground through a 1% 200-Ω precision
    resistor." Datasheet S3 Table 7: "Precision resistor for USB 3.0 (Connect a 200 Ω ±1%
    resistor between this pin and GND)". **CONFIRMED**
  - **R_USB2 = 6.04 kΩ ± 1 % from ball C8 to GND** — USB 2.0 PHY reference (needed always).
    **CONFIRMED** (S1 §12.1, S3 Table 7).
- **Termination is internal.** The receiver's DC termination is specified in the USB spec, not
  by external parts: `R RX-DC` = **18–30 Ω** common-mode, `R RX-DIFF-DC` = **72–120 Ω**
  differential (S5 Table 6-2). **CONFIRMED**
- No external AC termination, no pull-up on SSTX. **CONFIRMED by absence.**

### 1.5 Pull-ups / pull-downs on SS lines, and SS receiver detect

- **No external pull-up or pull-down on any SS line.** Receiver detection works from the
  receiver's own DC termination; an unpowered/high-impedance receiver is detected via
  `Z RX-HIGH-IMP-DC-POS ≥ 10 kΩ` (S5 Table 6-2 note). **CONFIRMED**
- Consequence for a board with no SS path: the host sees **no SS termination** on its TX pair,
  never starts SS link training, and enumerates the device over D+/D− at High Speed. This is
  the standard USB 3.0 fallback and is exactly the failure mode USB-IF documents as
  *"Not able to train to SuperSpeed – instead, the system is trained to operate at USB 2.0
  high speed"* (S7 §Introduction). **CONFIRMED (failure mode) / DERIVED (mechanism)**
- There is **no "SS enable" strap pin** on FX3. Enabling/disabling SuperSpeed is a **firmware**
  action (§6.3), not a resistor strap. **CONFIRMED** (S1 pin lists; S10).

### 1.6 SuperSpeed BOM — minimum set

| # | Part | Value / type | Where | Status |
|---|---|---|---|---|
| 1 | AC coupling cap | 0.1 µF, ≥16 V, 0402 | SSTX+ (A5) and SSTX− (A6), near receptacle | **Mandatory** (S1) |
| 2 | AC coupling cap (optional) | 0.1 µF | SSRX± | Optional (S5) |
| 3 | ESD array | SP3010-04UTG (0.45 pF) | all four SS lines, at receptacle | Recommended (S1) |
| 4 | Reference resistor | 200 Ω ±1 % | B3 (R_USB3) → GND | **Mandatory for SS** (S1, S3) |
| 5 | Reference resistor | 6.04 kΩ ±1 % | C8 (R_USB2) → GND | Mandatory always (S1, S3) |
| 6 | Bulk + decoupling | 22 µF + 0.1 µF | U3TXVDDQ (B5), U3RXVDDQ (A2) | **Mandatory** (S1 Table 3) |
| 7 | Ferrite bead | e.g. MPZ2012S601A / BLM21PG221SN1D | AVDD, U3TXVDDQ, U3RXVDDQ, CVDDQ | **Mandatory** (S1 checklist item 3) |
| 8 | Shield termination | RC or LC to GND | receptacle shield tabs | Recommended (S1) |
| 9 | USB 3.0 receptacle | Std-B / Micro-B with SS contacts | — | **Mandatory** |

No common-mode choke, no series resistor, no external termination, no pull-up/down.

---

## 2. Impedance and routing rules

All values below are from **AN70707 Rev \*Q (S1) §12.1**, which is the current revision. Where
Rev \*G (S2) differs, the difference is called out.

### 2.1 Numeric rules

| Parameter | Requirement | Confidence |
|---|---|---|
| Differential impedance | **90 Ω ± 7 %** — "Keep the USB signal line impedance at 90 Ω differential (±7%)." | **CONFIRMED** (S1 §12.1) |
| Single-ended impedance | **45 Ω** on each line, specifically in the four via-transition sections of each pair | **CONFIRMED** (S1 Fig. 25: "These four sections should be routed as a single ended trace. The impedance of each individual trace should be maintained at 45 ohms.") |
| Intra-pair spacing `S` | **8 mil** (0.203 mm) | **CONFIRMED** (S1 Table 10) |
| Trace width `W` | **11 mil** (0.279 mm) | **CONFIRMED** (S1 Table 10) |
| Gap to other planes `G` | **8 mil** minimum (0.203 mm) | **CONFIRMED** (S1 Table 10) |
| Escape clause | "variation in the above values can be ignored if the lines have a characteristic impedance of 90 Ω" | **CONFIRMED** (S1 Table 10) |
| Intra-pair length match | **0.12 mm (5 mil)** | **CONFIRMED** (S1 §12.1) |
| Intra-pair skew in time | ≈ **0.7–0.85 ps** (0.12 mm at 150–180 ps/inch in FR-4) | **DERIVED** — the 150–180 ps/inch figure is quoted in S1 §5.1 ("microstrip traces in PCBs (150 to 200 ps/inch)") |
| HS pair match (D+/D−) | **1.25 mm (20 mil)** in v18 — note Rev \*G said **50 mil**, and 1.25 mm is 49 mil, so the v18 "20 mil" text is internally inconsistent | **CONFIRMED (both texts), inconsistency flagged** |
| Max SS trace length | **< 3 inches (76.2 mm)** | **CONFIRMED** (S1 §12.1). S1 §12.1 also: "Both routing schemes … are tested to work at SS trace length of up to three inches." |
| Minimum layer count | **4** | **CONFIRMED** (S1 §12.1) |
| Vias on SS traces | **None.** Layout checklist item 15: "Is it ensured that there are no vias on the SS traces?" | **CONFIRMED** (S1 Table 11) |
| Ground via distance | "The distance between the signal and ground vias should be at least **40 mils**" (1.016 mm), and "about 40 mils" centre-to-centre between via pairs | **CONFIRMED** (S1 §12.1, Fig. 25/34) |
| Via void | One **common** void per differential pair, not separate voids | **CONFIRMED** (S1 §12.1, Fig. 34) |
| Spacing to other pairs/signals | **3× trace width** from other differential pairs, clocks and high-speed signals; **2W** minimum between ground fill and the pair | **CONFIRMED** (S1 §12.1) — with W = 11 mil this is 33 mil ≈ 0.84 mm and 22 mil ≈ 0.56 mm | 
| Bends | **45° or arcs only; no 90° bends** | **CONFIRMED** (S1 §12.1, Fig. 21) |
| Stubs | **None.** Probe pads must lie *in* the trace, not branch off it | **CONFIRMED** (S1 §12.1, Fig. 27) |
| Serpentine tuning | length < 100 mil, `S2 < 2·S1`, `E=F=G=3W` | **CONFIRMED** (S1 Fig. 24) |
| Single-arc tuning | `S2 < 5·S1` | **CONFIRMED** (S1 Fig. 23) |

### 2.2 Reference plane and layer transitions

- "All SS signal lines should be routed entirely over a **solid ground plane on an adjacent
  layer**. Splitting the ground plane underneath the SS signals increases loop inductance,
  introduces impedance mismatches and increases electrical emissions." — S1 §12.1.
  **CONFIRMED**
- "Avoid any split in the immediate plane under the USB lines." — S1 §12.1. **CONFIRMED**
- If two pairs must cross on different layers, a **ground layer must run all the way between**
  the two signal layers. — S1 §12.1, Fig. 20. **CONFIRMED**
- If a layer change is unavoidable: continuous ground, ground via next to each signal via,
  ≥ 40 mil separation, common void. — S1 §12.1. **CONFIRMED**
  *(In practice the "no vias on SS traces" checklist item means the intended answer is zero
  transitions. Every transition is a documented deviation.)*
- Guard traces with stitching vias along the SS traces are called out in the layout checklist
  (item 9). **CONFIRMED** (S1 Table 11).

### 2.3 Stackup

Cypress does **not** mandate a specific stackup or dielectric; it mandates the 90 Ω target and
a solid reference. The FX3 DVK's 8-layer stackup is published as an example (S1 §12.1.1):

```
       1.3 mil   copper + plating     TOP
       4.3 mil   FR-4
       0.65 mil  copper               GROUND
       4 mil     FR-4
       0.65 mil  copper               SIGNAL
       9.5 mil   FR-4
       0.65 mil  copper               POWER
       18 mil    FR-4
       0.65 mil  copper               POWER
       9.5 mil   FR-4
       0.65 mil  copper               SIGNAL
       4 mil     FR-4
       0.65 mil  copper               GROUND
       4.3 mil   FR-4
       1.3 mil   copper + plating     BOTTOM
```

**CONFIRMED** (S1 §12.1.1, "8-layer PCB example", reproduced from the FX3 DVK). Note the
signal layers are only 4 mil from their reference planes, which is what makes 11 mil / 8 mil
geometry land on 90 Ω — **a 1.6 mm 4-layer board with 0.2 mm prepreg will need very different
trace widths to hit 90 Ω, so the Table 10 numbers are stackup-specific, not universal.**
**DERIVED**

---

## 3. BGA escape routing: 0.8 mm pitch, 121-ball

### 3.1 What the package actually is

- **121-ball BGA, 10 × 10 mm, 0.8 mm pitch** (S1 §11, S3). **CONFIRMED**
- Recommended land pattern — **note the revision change**:
  - v18 (S1 §11): **BGA pad 11 mil, solder mask 11 mil, solder paste 11 mil** (≈0.279 mm).
  - Rev \*G (S2): "The recommended pad size is **0.241 mm (9.5 mil)**."
  Both are Cypress recommendations for the same package; the current doc says 11 mil.
  **CONFIRMED (both), revision change flagged.**
- **Where the SS balls are** (S3 Figure 7, 121-ball map, top view):

```
       1            2            3       4       5       6       7      8    9    10   11
  A  U3VSSQ   U3RXVDDQ   SSRXM   SSRXP   SSTXP   SSTXM   AVDD    VSS   DP   DM   NC
  B   VIO4     FSLC[0]   R_USB3  FSLC[1] U3TXVDDQ CVDDQ  AVSS    VSS  VSS  VDD  TRST#
  C  GPIO[54] GPIO[55]    VDD   GPIO[57] RESET#  XTALIN XTALOUT R_USB2 OTG_ID TDO VIO5
```

  So: **SSRX− = A3, SSRX+ = A4, SSTX+ = A5, SSTX− = A6, U3RXVDDQ = A2, U3TXVDDQ = B5,
  R_USB3 = B3, AVDD = A7, D+ = A9, D− = A10.** **CONFIRMED** (S3 Figure 7 and Table 7).
- **Critical geometric fact: the four SuperSpeed balls sit on row A — the outermost row of the
  package — as a contiguous run A3…A6, flanked by U3RXVDDQ (A2) and AVDD (A7), which are also
  edge balls.** **CONFIRMED** (S3 Figure 7).

### 3.2 Escape geometry (arithmetic)

With pad = 11 mil = 0.279 mm and pitch 0.8 mm:

| Quantity | Value | Derivation |
|---|---|---|
| Pad-edge to pad-edge gap | **0.521 mm** | 0.8 − 0.279 |
| Traces fitting between pads at 0.10 mm width / 0.10 mm space | **1** (uses 0.30 mm, 0.22 mm spare) | 0.1 + 2×0.1 = 0.3 ≤ 0.521 |
| Traces fitting at 0.075 mm / 0.075 mm (3 mil rules) | **2** (uses 0.375 mm) | 2×0.075 + 3×0.075 = 0.375 ≤ 0.521 |
| Dog-bone via pad that fits diagonally between 4 balls | **0.45 mm pad / 0.25 mm drill** | pad centre to cell centre = 0.8/√2 = **0.566 mm**; required = 0.225 + 0.1 + 0.14 = **0.465 mm** → 0.10 mm margin |
| Microvia variant | 0.10 mm laser drill, 0.25 mm capture pad | fits with large margin, but requires HDI (1+N+1 minimum) |

All **DERIVED** from the CONFIRMED 0.8 mm pitch and 0.279 mm pad. The "one trace between balls
at 0.1/0.1 mm" rule of thumb is therefore correct for this package; two traces are possible
only with 3 mil design rules.

Corroboration (secondary): dog-bone fanout "works effectively for BGA pitch ≥ 0.8 mm with
standard PCB fabrication (5/5 mil trace/space)" and via-in-pad/HDI "becomes mandatory below
0.8 mm pitch" (S11). **PROBABLE**

### 3.3 Consequence for this package — the good news

Because SSRX/SSTX are on **row A**, they can escape **radially on the top layer with no vias
at all** — which is precisely what the "no vias on the SS traces" checklist item wants. The
inner rows (B…L) are where dog-bones and layer changes are needed; the SS signals do not have
to go there. **DERIVED**, consistent with S1's guidance to route USB first.

This also means: **a correct FX3 SS layout does not need laser-drilled microvias, blind/buried
vias, or HDI.** A 4-layer board with 0.1 mm trace/space and 0.45/0.25 mm dog-bone vias (used
only for the power/ground balls) is sufficient. **DERIVED.** Anyone claiming "FX3 SuperSpeed
requires HDI" is over-generalising; it is required only if you must escape the inner rows or
use a pitch below 0.8 mm.

### 3.4 The bad news for a hand modification

The pads are **not accessible** once the part is soldered:

- Ball array spans 10 gaps × 0.8 mm = **8.0 mm** between outermost ball centres on a **10 mm**
  package → each outer ball centre is **1.0 mm inside the package edge**.
- Pad radius is 0.14 mm, so the **pad edge is ≈0.86 mm inside the package outline**.
- The solder ball itself (~0.4–0.5 mm) is likewise under the body.

**DERIVED.** Conclusion: with the BGA installed you cannot touch A3–A6 with an iron. Tapping
them requires removing and re-balling the FX3 (rework-station task), or finding a via/test
point that the original designer happened to route — which a USB-2.0-only design will not
have, because the nets were never routed.

**Practical corollary:** the only sane tap point on a USB-2.0-only product is the **USB
receptacle**, and only if that receptacle is a USB 3.0 type (Std-B / Micro-B with the extra
SS contacts) that was wired for USB 2.0 only. If the product uses a **5-pin USB 2.0 Micro-B**
receptacle, there are **no SS contacts at all** and the connector plus its shell cut-out must
be changed. **DERIVED** (mechanical facts of the two connector families).

---

## 4. Reference schematic around the USB connector

Cypress publishes two reference designs: **CYUSB3KIT-001 (FX3 DVK)** and **CYUSB3KIT-003
(SuperSpeed Explorer Kit)**. The SSEK user guide (S4) reproduces the USB section schematically;
that figure is the basis of this section. Everything in §4.2–§4.7 was **read directly off the
schematic in S4 Figure 4-2 / 4-3 / 4-4** and is **CONFIRMED**.

### 4.1 Which connector Cypress actually used

> "A **standard B-type USB 3.0 connector** is on the development board. Figure 4-2 shows the
> USB 3.0 pins (SS_TX_M, SS_TX_P, SS_RX_P, and SS_RX_M) and USB 2.0 pins (D+ and D–) that are
> available on the J1 USB connector." — S4 §4.2

**CONFIRMED.** The SSEK uses **Std-B**, not Micro-B. Micro-B is what a small device would use;
electrically identical apart from the pin numbering and the ID pin.

### 4.2 Connector pinout (Std-B, as drawn in S4)

| J1 pin | Connector signal | Net in the SSEK | Goes to |
|---|---|---|---|
| 1 | VBUS | — | J3 jumper → L2 ferrite → OVP → `USB3_VBUS` |
| 2 | D− | `SSDM` | U9 (82400102) → `SS_DM` → FX3 A10 |
| 3 | D+ | `SSDP` | U9 → `SS_DP` → FX3 A9 |
| 4 | GND | — | GND |
| 5 | STDB_SSTX− | `SS_TX_M` | U8 (SP3010) → `SS_TX_M_3` → FX3 **A6** |
| 6 | STDB_SSTX+ | `SS_TX_P` | U8 → `SS_TX_P_3` → FX3 **A5** |
| 7 | GND_DRAIN | — | GND |
| 8 | STDB_SSRX− | **`SS_RX_P`** | U8 → `SS_RX_P_3` → FX3 **A4** |
| 9 | STDB_SSRX+ | **`SS_RX_M`** | U8 → `SS_RX_M_3` → FX3 **A3** |
| 10, 11 | SHIELD1 / SHIELD2 | — | **C76 0.1 µF 25 V ∥ L4 BLM21PG221SN1D** → GND |

**Note the deliberate polarity inversion on the RX pair**: the receptacle's `SSRX−` (pin 8)
drives the net named `SS_RX_P`, and `SSRX+` (pin 9) drives `SS_RX_M`. This is legal and
expected:

> "The polarity can be swapped on the USB 3.0 differential pairs. Polarity detection is done
> automatically by the USB 3.0 PHY during link training, as defined in the USB3.0 specification
> section 6.4.2, and does not require any additional changes to device firmware." — S1 §12.1
> **CONFIRMED**

This matters for a modification: **you may cross the RX pair** if it makes the wiring easier,
but you may **not** cross TX with RX (see the wiring rule in §4.8).

### 4.3 ESD device

- **U8 = SP3010-04UTG** (Littelfuse), a 10-pin flow-through array: the four SS lines pass
  straight through it (I/O on pins 1, 2, 4, 5 on one side and 6, 7, 9, 10 on the other; GND on
  3 and 8). One part protects all four SS lines. (S4 Fig. 4-2)
- **U9 = Würth 824 001 02** dual USB 2.0 ESD array on D+/D−, with **C3 = 0.1 µF 25 V** on its
  VBUS side.

**CONFIRMED.** Cypress's own board therefore puts ESD **between the connector and the FX3**,
which is the ordering AN70707 §12.1 asks for ("ESD devices should be placed as close to the
receptacle as possible").

### 4.4 VBUS path — no divider

```
J1.1 VBUS ──► J3 (961102-6404-AR, VBUS jumper) ──► L2 (BLM21PG221SN1D) ──┬──► U11 NCP360SNT1G IN
                                                                        └── C80 1 µF 16 V ── GND
U11 OUT ──┬── C77 1 µF 16 V ── GND
          └──► USB3_VBUS  (to FX3 VBUS, ball E11)
```

- **There is no resistive divider.** FX3's VBUS pin is rated **4.0 V … 6.0 V** (S1 Table 2,
  S3 Table 7), so 5 V VBUS connects essentially directly. **CONFIRMED**
- AN70707 requires **0.1 µF** on the VBUS pin (S1 Table 3: "VBUS (E11) — 0.1 µF") and, in v18,
  **1 µF** (checklist item 14: "Does the VBUS pin of FX3 has 1uF capacitor?"). The SSEK uses
  1 µF on each side of the OVP. **CONFIRMED** — and note the doc itself is inconsistent
  (0.1 µF in Table 3, 1 µF in checklist item 14); fit 1 µF, it satisfies both.
- **OVP is mandatory only if VBUS can exceed 6 V** (e.g. a 9 V charger). S1 §3.1 and S3 §"VBUS
  Overvoltage Protection": "The maximum input voltage on FX3's VBUS pin is 6 V." **CONFIRMED**
- Do **not** confuse this with a "VBUS detect divider": FX3 detects VBUS by level, and the
  firmware can override VBUS/VBATT selection. **CONFIRMED** (S1 §3.1).

### 4.5 19.2 MHz crystal circuit

```
        XTALIN  (C6) ────┬──────────── Y1 pin 1
                         │
                        C11 = 13 pF, 50 V
                         │
                        GND

       XTALOUT  (C7) ────┬──────────── Y1 pin 3
                         │
                        C12 = 13 pF, 50 V
                         │
                        GND

  Y1 = NDK NX2520SA-19.200000MHZ-NBG1   (pins 2 and 4 → GND)
  FSLC0 / FSLC1 / FSLC2 ──┬── R76 = 10 kΩ ── GND      (selects the 19.2 MHz crystal)
```

**CONFIRMED** (S4 Figures 4-3 and 4-4). Notes:

- The user-guide *text* says "FSLC[0:2] lines of FX3 are tied to ground"; the *schematic* shows
  a **10 kΩ** pull-down (R76). The schematic is authoritative. **CONFIRMED**
- Crystal spec: **±100 ppm**, −40…+85 °C, drive level per the power-dissipation equation
  (S1 §5.1, Table 4). Cypress characterised the NX3225SA; the NX2520SA is a listed equivalent.
- **No vias on XTALIN/XTALOUT** in v18 ("We recommend not to have vias on XTAL IN and XTAL OUT
  traces"), crystal **within 2 cm** of the FX3. **CONFIRMED**

### 4.6 Complete SS-section netlist excerpt

Reconstructed from S4 Fig. 4-2 plus S1 §12.1 (the AC capacitors are *not* on the SSEK connector
sheet — Cypress put them on the FX3 sheet; their presence and value are mandated by S1):

```
# ---- FX3 U3 (CYUSB3014-BZX)  ->  J1 (USB 3.0 Std-B receptacle) ----
# U8 = SP3010-04UTG, flow-through: receptacle-side pins -> FX3-side pins (pin numbers not asserted)
U3.A5  SSTXP  ---+---[ C_ACTX_P  0.1uF ]---+--- U8 ---+--- J1.6  STDB_SSTX+
                  |                                  |
U3.A6  SSTXM  ---+---[ C_ACTX_M  0.1uF ]---+--- U8 ---+--- J1.5  STDB_SSTX-
                                          (SP3010-04UTG, 0.45 pF/line)
U3.A4  SSRXP  ----------------------------+--- U8 ---+--- J1.8  STDB_SSRX-   (polarity inverted)
                                          |          |
U3.A3  SSRXM  ----------------------------+--- U8 ---+--- J1.9  STDB_SSRX+
U3.A2  U3RXVDDQ ---[ FB ]--- 1V2   + 22uF || 0.1uF
U3.B5  U3TXVDDQ ---[ FB ]--- 1V2   + 22uF || 0.1uF
U3.B3  R_USB3  ---[ 200R 1% ]--- GND
U3.C8  R_USB2  ---[ 6.04k 1% ]--- GND
U3.A9  DP  --- U9 --- J1.3
U3.A10 DM  --- U9 --- J1.2
J1.1   VBUS --- J3 --- L2(FB) --- U11(OVP) --- U3.E11 VBUS   + 1uF
J1.4   GND
J1.7   GND_DRAIN --- GND
J1.10/11 SHIELD --- [ C76 0.1uF 25V  ||  L4 FB ] --- GND
```

*(The AC-cap position shown at the FX3 is the "FX3 connected to hub/embedded controller" case;
for a device with a receptacle, S1 Appendix B says put them at the receptacle. Either satisfies
the electrical requirement; the v18 preference for a receptacle design is "near the
connector".)*

### 4.7 Micro-B variant (for a small device)

The USB 3.0 Micro-B receptacle carries the same four SS signals on different pins. Industry
pinout (**PROBABLE** — the USB 3.0 specification's Tables 5-11/5-12 define the cable wiring, but
the specification itself was not retrievable here; this table is the universally implemented
assignment):

| Pin | Signal |
|---|---|
| 1 | VBUS |
| 2 | D− |
| 3 | D+ |
| 4 | ID (OTG) |
| 5 | GND |
| 6 | SSTX− |
| 7 | SSTX+ |
| 8 | SSRX− |
| 9 | SSRX+ |
| shell | Shield / GND_DRAIN |

Mapping to FX3 is then: **A6 SSTX− → pin 6, A5 SSTX+ → pin 7, A3 SSRX− → pin 8,
A4 SSRX+ → pin 9** (polarity may be swapped on RX). **PROBABLE**

### 4.8 Wiring rule that must not be broken

- **FX3 SSTX → receptacle SSTX; FX3 SSRX → receptacle SSRX.** The crossover is inside the
  cable. S1 Appendix B: *"TX pins of the FX3/SX3 device should be connected to TX pins of the
  USB3.0 Type-B receptacle. Rx pins of the FX3/SX3 device should be connected to RX pin of the
  USB receptacle. Use a USB3.0 cable which connects the host TX to FX3/SX3 device RX pins and
  host RX to FX3 device TX pins."* **CONFIRMED**
- Polarity **within** a pair may be inverted; **pairs may not be swapped with each other**.
  **CONFIRMED** (S1 §12.1 allows polarity inversion only).

---

## 5. Power: rails, sequencing, decoupling

### 5.1 Rails

| Rail | Function | Min | Typ | Max | Source |
|---|---|---|---|---|---|
| **VDD** | Core | 1.15 | **1.2** | 1.25 V | S1 Table 2 / S3 |
| **AVDD** | PLL, crystal oscillator, core analog | 1.15 | **1.2** | 1.25 V | S1 Table 2 |
| **U3TXVDDQ** | **USB 3.0 TX PHY supply** | 1.15 | **1.2** | 1.25 V | S1 Table 2, ball B5 |
| **U3RXVDDQ** | **USB 3.0 RX PHY supply** | 1.15 | **1.2** | 1.25 V | S1 Table 2, ball A2 |
| **VIO1–VIO4** | I/O domains | 1.7 | 1.8 / 2.5 / 3.3 | 3.6 V | S1 Table 2 |
| **VIO5** | I²C + JTAG | 1.15 | 1.2 / 1.8 / 2.5 / 3.3 | 3.6 V | S1 Table 2 |
| **CVDDQ** | Clock I/O | 1.7 | 1.8 / 3.3 | 3.6 V | S1 Table 2 |
| **VBATT** | USB I/O / analog battery input | 3.2 | 3.7 | 6 V | S1 Table 2 |
| **VBUS** | USB I/O / analog | 4.0 | 5 | 6 V | S1 Table 2 |

**CONFIRMED** for all rows.

### 5.2 Decoupling (v18 values)

| Domain (balls) | Group bulk | Per-pin |
|---|---|---|
| VDD (B10, J11) | **22 µF** | 0.01 µF **and** 0.1 µF |
| VDD (H1, L7, F11, L5) | — | 0.1 µF |
| VDD (C3, E9) | — | 0.01 µF |
| AVDD (A7) | **2.2 µF** | 0.1 µF |
| **U3RXVDDQ (A2)** | **22 µF** | **0.1 µF** |
| **U3TXVDDQ (B5)** | **22 µF** | **0.1 µF** |
| CVDDQ (B6) | — | 0.01 µF and 0.1 µF |
| VIO1 (L9, H11) | 0.01 µF | 0.1 µF |
| VIO2–VIO5 | — | 0.01 µF and 0.1 µF |
| VBUS (E11) | — | 0.1 µF (checklist says 1 µF) |

**CONFIRMED** (S1 Table 3). **Revision note:** Rev \*G (S2) specified **2.2 µF** on
U3RXVDDQ/U3TXVDDQ; v18 raised it to **22 µF**. Use 22 µF.

### 5.3 Ferrite beads and ripple

- "Are the ferrite beads connected on **AVDD, U3TXVDD, U3RXVDD, and CVDD**?" — S1 Table 11,
  schematic checklist item 3. **CONFIRMED (mandatory).**
- "Make sure that AVDD, CVDD, U3TXVDDQ, and U3RXVDDQ are isolated and connected with ferrite
  beads. Also, the source of these supplies should have **noise/ripple less than 70 mV**."
  — S1 Appendix B. **CONFIRMED**
- Recommended bead in the power figure: **MPZ2012S601A** (S1 Figure 1). **CONFIRMED**

### 5.4 Inrush and sequencing

- "When the USB3.0 SuperSpeed PHY is enabled for the first time, or a reset event, an initial
  inrush current is expected on the 1.2 V **U3RXVDDQ and U3TXVDDQ** supplies for **~10 µs**.
  The magnitude of this current can be as high as **800 mA**." — S1 §4.2. **CONFIRMED**
- If the same 1.2 V rail feeds VDD, the droop must not trip POR: **VDD < 0.83 V for > 200 ns
  resets the chip**. Design the 1.2 V network so VDD never falls below 0.83 V during inrush;
  Cypress's optimised example went from 2.2 µF + choke to **22 µF** and removed the choke from
  VDD, cutting the droop from 200 mV to 112 mV and the inrush from 800 mA to 320 mA.
  — S1 §4.2. **CONFIRMED**
- There is **no explicit rail sequencing requirement** stated for VDD vs VIO vs VBUS in
  AN70707 beyond "VBUS/VBATT can be turned OFF if USB is not used; if the USB port is used one
  or both supplies must be present." **CONFIRMED by absence.** VIO rails may be powered down
  individually when the corresponding interface is unused; **VIO1 must not be powered down if
  GPIF II is used.** **CONFIRMED**

### 5.5 Is there a separate supply for the SS PHY?

**Yes — two of them: U3TXVDDQ (B5) and U3RXVDDQ (A2), each 1.2 V, each with its own ferrite bead
and its own 22 µF + 0.1 µF.** AVDD is *not* the SS PHY supply; it is the PLL/crystal/core-analog
1.2 V rail. **CONFIRMED** (S1 Table 2 + Table 3 + Figure 1).

**This is the single most important fact for the feasibility question.** Cypress requires these
two rails to be present **even on a USB-2.0-only design**:

> "Both USB3.0 power pins (U3TXVDDQ and U3RXVDDQ) should be connected to 1.2V with a ferrite
> bead." — S1 Appendix C. **CONFIRMED**

So a compliant USB-2.0-only FX3 board *should* already have the SS PHY powered. If a particular
board omitted them (or fitted the ferrite but no bulk cap), the SS PHY cannot be brought up by
any amount of trace rework — the board would have to be reworked at the power section too.

---

## 6. Unused-SuperSpeed design practice (the USB-2.0-only FX3)

### 6.1 What Cypress says to do with the pins

AN70707 **Appendix C — "FX3/SX3 device connections to USB 2.0-only host"** (S1) is explicit and
short enough to quote in full:

> - "FX3/SX3 SuperSpeed pins **TX+/− pins (A5 and A6) and RX+/− pins (A3 and A4) can be left
>   open**."
> - "Both USB3.0 power pins (**U3TXVDDQ and U3RXVDDQ**) should be **connected to 1.2V with a
>   ferrite bead**."
> - "The USB3 reference pin (**B3**) can be left open."
> - "Make sure that layout guidelines are followed per Section 12.1."

**CONFIRMED.**

So the answers are:

| Question | Answer | Confidence |
|---|---|---|
| Are SSTX/SSRX left floating, grounded, or terminated? | **Left open (floating).** No termination, no ground, no pull. | **CONFIRMED** (S1 App. C) |
| Is the SS reference resistor fitted? | **Not required** — **B3 may be left open** on a USB-2.0-only design. | **CONFIRMED** (S1 App. C) |
| Must the SS PHY rails be present? | **Yes** — U3TXVDDQ and U3RXVDDQ at 1.2 V with ferrite. | **CONFIRMED** (S1 App. C) |
| Do the SS pins need to be connected for HS enumeration? | **No.** The HS path (D+/D−) is independent; the chip enumerates at HS with A3–A6 open. | **CONFIRMED** (S1 App. C) |
| Is there a strap/register that must be set? | **No hardware strap.** SuperSpeed is enabled/disabled **in firmware** via `CyU3PConnectState(connect, ssEnable)`. | **CONFIRMED** (S10) |

### 6.2 The consequence nobody expects: B3 is left open

Because Appendix C permits **B3 (R_USB3) to be left open** on a USB-2.0-only design, a compliant
USB-2.0-only FX3 board may have **no 200 Ω ±1 % reference resistor**. Without it the USB 3.0 PHY
has no calibration reference. **Any SuperSpeed modification must add that 200 Ω 1 % resistor to
B3** — and B3 is a BGA ball in row B, i.e. *not* on the outer row, so it is even harder to reach
than A3–A6. **DERIVED from S1 App. C + S3 ball map + §3.4 geometry.**

### 6.3 Firmware side

The FX3 SDK's connection call is:

```c
CyU3PReturnStatus_t CyU3PConnectState (CyBool_t connect, CyBool_t ssEnable)
```

> "Enable / disable the USB connection. This function is used to enable or disable the USB PHYs
> on the FX3 device…"
> `connect` — "CyTrue to enable connection, CyFalse to disable connection."
> `ssEnable` — "**Whether to enumerate as a SS device or FS/HS device**"

— S10, §5.36.7.1. **CONFIRMED.**

Implications:

- On a USB-2.0-only board the firmware should pass `ssEnable = CyFalse` so the SS PHY is not
  brought up. Passing `CyTrue` is not fatal — the device still enumerates over D+/D− at HS
  because the host never sees SS termination — but it wastes power and enables the 800 mA
  inrush event.
- **No register write, no OTP bit, no strap is required** to run SS. Conversely, **nothing in
  firmware can create SS capability that the board does not physically have.** The PHY needs
  the AC caps, the 200 Ω reference, the powered rails and a 90 Ω channel; firmware only decides
  whether to *offer* SS.
- Forcing **USB 3.0-only** (no HS fallback) is a separate, documented option: S1 Appendix D
  points to **KBA219491** for "USB 3.0-only application". **CONFIRMED** (pointer only; the KBA
  itself is behind Cloudflare and was not read).

### 6.4 What this means for a "USB 2.0-only product"

A USB-2.0-only product built to Cypress's own guidance therefore has, at minimum:

1. A3–A6 **unconnected** (no traces, no vias, no test points);
2. B3 **unconnected** (no 200 Ω);
3. **no** 0.1 µF AC caps, **no** SS ESD array;
4. **but** U3TXVDDQ/U3RXVDDQ **powered** (this is required even in App. C).

Item 4 is the only piece of SS infrastructure that is guaranteed to exist. Items 1–3 are
exactly the parts a modification would have to add — and items 1 and 2 are on a 0.8 mm BGA.

### 6.5 Relevance to this project (project evidence, *not* a vendor requirement)

Recorded here because it bears on the feasibility question, but it is **our firmware analysis**,
not an Infineon statement:

- The FX3 firmware image on the unit under study contains **both** descriptor sets — a
  `bcdUSB = 0x0200` device descriptor and a `bcdUSB = 0x0300` one with a BOS descriptor
  advertising SuperSpeed (`wSpeedsSupported = 0x000E`), plus an SS configuration with 5
  endpoints, 1024-byte packets and an SS endpoint companion on every endpoint. See
  [`analysis/usb3/firmware_descriptors.txt`](../../analysis/usb3/firmware_descriptors.txt).
- Every connect site calls `CyU3PConnectState(connect, ssEnable)` with **`ssEnable = 1`**; there
  is no `(1, 0)` call anywhere in the image. Per S10 that means the firmware always asks for SS
  and never deliberately restricts itself to HS.
- **Interpretation:** the firmware side of a SuperSpeed modification is already in place — it
  would not need a code change to *offer* SuperSpeed. Whether the link comes up is purely a
  hardware question (AC caps, 200 Ω on B3, powered PHY rails, 90 Ω channel, SS receptacle).
  Note the usual caveat: the FX3 SDK selects the descriptor set by negotiated speed, so the mere
  presence of the 0x0300 set is normal SDK behaviour and does **not** by itself prove that the
  board was ever intended to run SS.

---

## 7. Signal integrity of a field modification (wire tap)

### 7.1 The channel budget the link has to fit into

| Quantity | Value | Source |
|---|---|---|
| Total link loss at 2.5 GHz, short channel (device at the host connector) | **≈ −4 dB** | S8 §1.2 |
| Total link loss at 2.5 GHz, long channel (3 m cable + ~10 in host PCB + up to 2 in device PCB) | **≈ −17 … −18 dB** | S8 §1.2 |
| Receiver equaliser must therefore cover | **−4 dB … −18 dB**, "adding a couple of dB on either side is a good idea" | S8 §5 |
| Loss consumed by the **cable assembly alone** | **−7.5 dB @ 2.5 GHz** — "more than a third of the total link loss budget" | S7 §Introduction |
| Cable assembly insertion-loss mask | (100 MHz, −1.5 dB), (1.25 GHz, −5.0 dB), (2.5 GHz, −7.5 dB), (7.5 GHz, −25 dB) | S6 §3 / S7 |
| Transmitter de-emphasis | **3.5 dB ± 0.5 dB** | S8 §3.1 |
| Mated connector differential impedance | **75 … 105 Ω** (= 90 Ω ± 15 Ω), 50 ps (20–80 %) TDR rise time | S6 §3 |
| Differential-to-common-mode conversion of the mated cable assembly | **≤ −20 dB, 100 MHz … 7.5 GHz** | S6 §3 |
| Unit interval (Gen 1) | **200 ps** | S5 Table 6-2 |

**CONFIRMED** for all rows.

### 7.2 What a 5–10 cm flying lead actually does

Take the proposed modification: solder the four SS wires of a USB 3.0 cable onto exposed
vias/test pads with 5–10 cm of unshielded wire.

| Violation | Magnitude | Why it matters |
|---|---|---|
| **Length** | 50–100 mm of added lead vs. the FX3's **< 76.2 mm total** SS budget (S1) | A 10 cm lead alone exceeds the entire allowance Cypress gives the PCB. **DERIVED** |
| **Impedance** | A hand-soldered twisted pair is typically 100–130 Ω with ±20 % variation along its length; a parallel two-wire lead can be 150–300 Ω | The spec wants **90 Ω ± 15 Ω at the connector** (S6). Every 20 Ω step is a reflection. **DERIVED / ESTIMATE** |
| **Reference plane** | None. The return current has no defined path; it will flow through whatever ground loop exists | Directly contradicts "routed entirely over a solid ground plane on an adjacent layer" (S1). Causes radiation and crosstalk. **CONFIRMED (rule) / DERIVED (effect)** |
| **Stub / transition** | Two solder joints, a pad, a via (if used) and a wire breakout — each an impedance discontinuity | S1 forbids stubs outright and requires a common void + ground vias at any transition. **CONFIRMED (rule)** |
| **Skew** | Hand-cut wires: ±2–5 mm length mismatch is typical → **12–30 ps** at 6 ps/mm | FX3 rule is **0.12 mm ≈ 0.7 ps** (S1). 30 ps is **15 % of a 200 ps UI**. **DERIVED / ESTIMATE** |
| **Differential-to-common-mode conversion** | Asymmetric hand wiring easily exceeds −20 dB | This is the spec's EMI proxy (S6); it is also what corrupts the RX eye. **DERIVED** |
| **Crosstalk** | Two unprotected pairs 1–2 mm apart for 10 cm, no ground between | S1 requires **3W (≈0.84 mm) plus ground fill** between pairs *on a PCB with a reference plane*; a flying lead has neither. **DERIVED** |

**Insertion-loss estimate (not measured):** 10 cm of small-gauge unshielded pair contributes
roughly **0.5–2 dB at 2.5 GHz** by conductor/dielectric loss alone. That number is *not* what
kills the link — it fits inside the ~20 dB budget. What kills it is the **reflection/ILD** from
three or four gross impedance discontinuities and the **mode conversion** from an
unreferenced, asymmetric lead. A receiver equaliser trained on a −4 dB channel cannot absorb
−10 dB of ripple. **ESTIMATE** (order-of-magnitude, from S7/S8 budgets).

### 7.3 Predicted outcome, in order of likelihood

1. **No SuperSpeed link at all; fallback to USB 2.0 High Speed (480 Mb/s).** The host's SS
   receiver-detect or the subsequent link training fails, and the device enumerates over
   D+/D−. This is the failure mode USB-IF names explicitly: *"Not able to train to SuperSpeed –
   instead, the system is trained to operate at USB 2.0 high speed"* (S7). **CONFIRMED as a
   documented failure mode; highly likely here.**
2. **Link trains at 5 Gb/s but is unreliable** — CRC errors, retries, occasional drops to HS on
   reconnect, and a throughput far below the ~400 MB/s the FX3 can do. The FX3 exposes error
   counters via `CyU3PUsbGetErrorCounts` (S10), so this is measurable rather than arguable.
   **PROBABLE**
3. **Works on a bench with a short, well-twisted, well-grounded lead and a lucky cable** — and
   fails on the customer's cable. **PROBABLE** (this is the classic intermittent outcome and is
   why USB-IF wrote S7).
4. **Radiated emissions / RFI failure.** An unreferenced 5 Gb/s lead is an antenna; USB-IF warns
   specifically that poor SS connector/cable practice makes nearby wireless devices stop
   working (S7). Passing EN55022/FCC Part 15 with this attached is unlikely. **PROBABLE**

**Bottom line:** a 5–10 cm wire tap is not a marginal-but-usable modification. It violates the
length rule, the impedance rule, the reference-plane rule, the no-stub rule and the skew rule
simultaneously. The realistic outcome is **fallback to USB 2.0 with occasional flakiness**, not
SuperSpeed.

### 7.4 What a modification that *could* work would look like

For completeness, the minimum bar if the goal is a genuine SS port:

1. **Receptacle**: fit a USB 3.0 Std-B or Micro-B receptacle (mechanical cut-out change if the
   product has a 5-pin USB 2.0 Micro-B). **Mandatory.**
2. **Channel**: 90 Ω ± 7 % differential, ≤ 76 mm, solid ground reference on the adjacent layer,
   **zero vias**, no stubs, 0.12 mm intra-pair match, ESD at the connector, AC caps per §1.1.
   **Mandatory.**
3. **Reference resistor**: 200 Ω ± 1 % on **B3**. **Mandatory** — and B3 is a non-perimeter ball,
   so this alone forces BGA rework.
4. **Tap the SS balls A3–A6**: requires removing and re-balling the FX3, or the original design
   must have routed them to test points (it will not have).
5. **Power**: confirm 1.2 V on A2 and B5 with ferrite beads and 22 µF + 0.1 µF. If the original
   design omitted them, fix that too.
6. **Firmware**: pass `ssEnable = CyTrue`; the SS descriptor set and BOS must exist.

Items 3 and 4 both require BGA rework. That is the crux: **the modification is not a soldering
job, it is a board re-spin or a BGA rework plus a hand-built 90 Ω channel.** For a one-off
feasibility proof, a small interposer/riser PCB that picks up A3–A6 through a re-balled FX3 (or
a redesigned 4-layer carrier) is more realistic than wire taps.

---

## 8. Quick-reference summary

### 8.1 Mandatory BOM on the SS path

| Item | Value | Notes |
|---|---|---|
| AC caps on SSTX | **2 × 0.1 µF** | near receptacle (device) / near FX3 (hub); cut-out in GND under pads |
| AC caps on SSRX | not required; if fitted, **330 nF (297–363 nF)** per USB 3.1 ECN | not fitted in FX3 reference designs |
| ESD | **SP3010-04UTG**, 0.45 pF, ≤ 0.5 pF allowed | at the receptacle, connector side of the caps |
| R_USB3 | **200 Ω ± 1 %**, ball B3 → GND | mandatory for SS |
| R_USB2 | **6.04 kΩ ± 1 %**, ball C8 → GND | mandatory always |
| U3TXVDDQ / U3RXVDDQ | **1.2 V + ferrite + 22 µF + 0.1 µF** each | mandatory even on USB-2.0-only designs |
| Shield | RC or LC to GND | SSEK: 0.1 µF ∥ ferrite |
| CMC | **not required** | none in any Cypress design |
| Series R on SS | **none** | internal termination only |

### 8.2 Impedance / skew / routing

- **90 Ω ± 7 %** differential; **45 Ω** single-ended through via transitions.
- **0.12 mm (5 mil) ≈ 0.7 ps** intra-pair match; **1.25 mm** for D+/D−.
- **< 76.2 mm (3 in)** total; **≥ 4 layers**; solid reference; **no vias**; **no stubs**;
  45°/arc bends only; **3W** to other pairs, **2W** ground-to-pair spacing.
- 8-layer DVK stackup: 4 mil signal-to-reference, 11 mil / 8 mil geometry.

### 8.3 BGA

- 121-ball, 10 × 10 mm, **0.8 mm** pitch, 11 mil pads.
- SS on **row A (perimeter)** → escapable with **no vias**, no HDI.
- 1 trace at 0.1/0.1 mm between pads; dog-bone 0.45/0.25 mm via fits diagonally.
- **Pads are 0.86 mm inside the package outline → not hand-solderable.**

### 8.4 Realistic consequence of a wire tap

Falls outside the length, impedance, reference-plane, stub and skew rules at the same time.
**Expected result: no SS link, fallback to USB 2.0 HS**, with intermittent 5 Gb/s operation and
EMI problems as the optimistic case. A working modification requires BGA rework (for A3–A6 and
B3) plus a properly terminated 90 Ω channel — i.e. a board re-spin.

---

## 9. Open items / could not verify

| Item | Status | Why |
|---|---|---|
| AC-cap package size (0402 vs 0201) and voltage rating | **UNVERIFIED** | Cypress states only "0.1 µF"; no case size or rating anywhere in S1/S2/S4 |
| ESD5V3U1U line capacitance | **UNVERIFIED** | `infineon.com` blocked; AN140 (S12) not retrievable |
| KBA219491 (USB 3.0-only application) and the AC-coupling KBA | **UNVERIFIED** | `community.infineon.com` behind Cloudflare |
| USB 3.0/3.1 specification §6.4.2 (polarity inversion) and Tables 5-11/5-12 (Micro-B wiring) | **not retrieved** | usb.org PDF paths 404 from here; cited via S1/S6 cross-references |
| Micro-B pinout | **PROBABLE** | industry-standard, not read from the specification |
| Full CYUSB3KIT-001 / CYUSB3KIT-003 schematic sheets (FX3-side AC caps) | **not retrieved** | only the connector sheet is reproduced in S4 |
| Measured insertion loss of a 10 cm flying lead | **ESTIMATE** | would need VNA measurement; not performed |

### Documents worth obtaining for a follow-up

1. **CYUSB3KIT-003 schematic PDF** (full sheets) — would settle AC-cap package and position.
2. **CYUSB3KIT-001 (FX3 DVK) schematic** — the design AN70707's figures are drawn from.
3. **KBA219491** and the community KBA on AC-coupling capacitance.
4. **AN140** — Infineon's own USB 3.0 ESD selection note.
5. **USB 3.1/3.2 specification** §6.4.2, §6.8, Table 6-20 — for the normative AC-coupling and
   impedance text rather than the ECN's restatement.
