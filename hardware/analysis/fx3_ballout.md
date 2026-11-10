# CYUSB3014-BZX (EZ-USB FX3) — 121-ball FBGA ballout, verified

**Scope:** exact ball/pin assignment of the Cypress/Infineon **CYUSB3014** in the **121-ball, 10 × 10 mm, 0.8 mm-pitch BGA** package (ordering-code package suffix **BZX**), for a hardware feasibility study and for mapping photographed BGA escape traces back to signals.

**Status:** *complete* — all 121 balls recovered from the primary datasheet, cross-checked against two further independent sources. Nothing in the ball table is guessed. Three documentation inconsistencies inside the datasheet itself are flagged in §9.

---

## 0. TL;DR — the four SuperSpeed balls

| Signal (datasheet) | Common name | Ball | Row / edge | Position along edge |
|---|---|---|---|---|
| `SSRXM` | SSRX− / SS_RX_M | **A3** | row A → **top edge** (top view) | 2.6 mm from left edge |
| `SSRXP` | SSRX+ / SS_RX_P | **A4** | row A → **top edge** | 3.4 mm from left edge |
| `SSTXP` | SSTX+ / SS_TX_P | **A5** | row A → **top edge** | 4.2 mm from left edge |
| `SSTXM` | SSTX− / SS_TX_M | **A6** | row A → **top edge** | 5.0 mm from left edge |

All four are in **row A**, i.e. the edge that contains the A1 corner — **not** row B. In the datasheet's top view, that is the **top edge**, in the **left half** of the edge (columns 3–6 of 11). Confidence: **confirmed** (primary datasheet + app note + third-party CAD).

---

## 1. Document identity

| # | Document | Number / revision | Date | What it contains | URL |
|---|---|---|---|---|---|
| 1 | **EZ-USB FX3 datasheet** (CYUSB301X, CYUSB201X) — English, current | **001-52136 Rev. \*Z** | 2022-09-29 | §15 *Pin configurations*, **Figure 6** = "FX3 121-ball BGA ball map (Top view)" on p. 27; **Table 7** pin list pp. 28–31; **Figure 30** package drawing p. 66; **Table 24** ordering info p. 67 | [infineon.com PDF](https://www.infineon.com/assets/row/public/documents/30/49/infineon-cyusb301x-cyusb201x-ez-usb-fx3-superspeed-usb-controller-datasheet-en.pdf) |
| 2 | Same datasheet, **Japanese** translation | **001-79232 Rev. \*C** | 2022-11-30 | Same Figure 6/7 and Table 7, independently typeset — used as a second rendering of the same map | [infineon.com PDF](https://www.infineon.com/dgdl/Infineon-CYUSB301X_CYUSB201X_EZ-USB_FX3_SuperSpeed_USB_Controller-DataSheet-v04_00-JA.pdf?fileId=8ac78c8c7d0d8da4017d0eca15ef4296) |
| 3 | **Package outline drawing**, 121-ball FBGA | **001-54471 Rev. \*F** (28-JUN-17) | 2017-06-28 | "PACKAGE OUTLINE, 121 BALL FBGA, **10.0X10.0X1.2 MM BK0AA/FBI121/T4A121**"; embedded as Figure 30 of doc 1, p. 66 | [same PDF, p. 66](https://www.infineon.com/assets/row/public/documents/30/49/infineon-cyusb301x-cyusb201x-ez-usb-fx3-superspeed-usb-controller-datasheet-en.pdf#page=66) |
| 4 | **AN70707** — EZ-USB FX3/FX3S/SX3 hardware design guidelines and schematic checklist | **001-70707 Rev. \*Q** | 2021-10-26 | §11 package dims (p. 20), Table 3 decoupling ball numbers (p. 8), Fig. 14/15/41/42, **Appendix C p. 40** ("SS pins … can be left open") | [infineon.com PDF](https://www.infineon.com/assets/row/public/documents/30/42/infineon-an70707---ez-usb-fx3-fx3s-sx3-hardware-design-guidelines-and-schematic-checklist-applicationnotes-en.pdf) |
| 5 | **EZ-USB FX3 Technical Reference Manual** | **001-76074 Rev. \*F** | 2019-05-09 | Register/functional reference (no ball map) | [infineon.com PDF](https://www.infineon.com/assets/row/public/documents/30/57/infineon-ez-usb-fx3-technical-reference-manual-additionaltechnicalinformation-en.pdf) |
| 6 | Product page CYUSB3014-BZXI | — | — | Document index / ordering | [infineon.com/part/CYUSB3014-BZXI](https://www.infineon.com/part/CYUSB3014-BZXI) |

**Which revision contains the 121-ball BZX map?** Chapter 15 *Pin configurations*, with **Figure 6** (FX3 map) and **Figure 7** (FX3 Hi-Speed map), is present at least from **\*U (2017-04-20)** onward — \*U is the revision that *"Removed 131-ball WLCSP Package related information in all instances across the document"* (doc 1, revision history, p. 79), which is why older revisions also documented a second package. **\*Z (2022-09-29) is current** and is the revision transcribed here (the Japanese \*C of doc 001-79232, 2022-11-30, prints the identical map). Revisions \*W…\*Z record only editorial changes to Figures 6/7 and Table 7 ("Updated Figure 6", "Updated Table 7") and list no ball reassignment — but no pre-\*U PDF was obtained, so "unchanged since \*U" is a **probable**, not a confirmed, inference. For an audit, diff Figure 6 of the revision you hold against \*Z.

> **Note on `001-67239`:** this number could **not** be verified as an FX3 document. It does not appear at all in the current datasheet, in AN70707, or in the TRM, and no Infineon asset with that number resolves. Treat `001-67239` as unverified/mis-remembered. The FX3 datasheet number is **001-52136**.

---

## 2. Ordering code, "BZX", and package marking

Datasheet §21.1 *Ordering code definitions* (doc 1, p. 67) decomposes the code as:

```
CY USB X XXX - XXX X X
                  |   |  └─ T = Tape and Reel
                  |   └──── Temperature Range: I = Industrial (-40..+85 °C), C = Commercial (0..+70 °C)
                  └──────── Package Type: BZX = BGA
```

So **BZX is the package code (BGA)**, not a complete orderable part number, and in this family **BZX ⇒ 121-ball BGA** (Table 24 lists only "121-ball BGA" for every BZX variant).

**Orderable codes** (Table 24, doc 1, p. 67) — confirmed, primary:

| Ordering code | USB | SRAM | GPIF II bus | Temp | Package |
|---|---|---|---|---|---|
| CYUSB3011-BZXC | USB 3.0 | 256 kB | 16-bit | 0…+70 °C | 121-ball BGA |
| CYUSB3012-BZXC | USB 3.0 | 256 kB | 32-bit | 0…+70 °C | 121-ball BGA |
| CYUSB3013-BZXC | USB 3.0 | 512 kB | 16-bit | 0…+70 °C | 121-ball BGA |
| **CYUSB3014-BZXC** | USB 3.0 | 512 kB | 32-bit | 0…+70 °C | 121-ball BGA |
| **CYUSB3014-BZXI** | USB 3.0 | 512 kB | 32-bit | −40…+85 °C | 121-ball BGA |
| CYUSB2014-BZXC | USB 2.0 | 512 kB | 32-bit | 0…+70 °C | 121-ball BGA |
| CYUSB2014-BZXI | USB 2.0 | 512 kB | 32-bit | −40…+85 °C | 121-ball BGA |

Tape-and-reel suffixes (`-BZXCT`, `-BZXIT`) are **not** in the datasheet table but are standard Cypress/Infineon T&R codes and are the codes distributors stock (e.g. DigiKey `CYUSB3014-BZXCT`). Confidence for the T&R variants: **probable** (distributor catalogues), not in the primary table.

**"CYUSB3014-BZX" vs "-BZXC" vs "-BZXI":** `CYUSB3014-BZX` alone is **not a complete orderable code** (it lacks the temperature letter). It is, however, exactly what is **laser-marked on the package**, with the temperature letter moved onto the second line next to the date code.

**Package marking actually observed on the unit in this workspace** (photo `hardware/photos/P1_top_side.webp`, cropped/rotated):

```
CYUSB3014-BZX
I 2237
A 33 THA
CYP 625297
        DC
```

* `I` = industrial temperature grade (⇒ CYUSB3014-BZXI), `2237` = date code 2022 week 37
* `A 33 THA` = lot/assembly code, `CYP 625297` = Cypress internal code, `DC` = assembly-site/die code
* a **round laser dot** sits at the A1 corner of the package top.

Confidence: **observed on one unit** (this board); the *convention* (part number without temp letter + `I`/`C` + date code) is consistent with §21.1 but is not itself documented in the datasheet → treat the marking layout as **probable**, the ordering codes as **confirmed**.

---

## 3. Package geometry and orientation (critical for photo → ball mapping)

All values below are from **001-54471 \*F** as reproduced in doc 1, Figure 30, p. 66 (primary, confirmed).

| Parameter | Value | Symbol |
|---|---|---|
| Ball count | **121** | N |
| Matrix | **11 × 11, fully populated — no depopulated positions** | MD = ME = 11 |
| Rows | **A, B, C, D, E, F, G, H, J, K, L** (letter **I is skipped**, JEDEC JEP95 / SPP-020) | |
| Columns | **1 … 11** | |
| Package body | **10.00 × 10.00 mm** | D, E |
| Body height | **1.20 mm max** | A |
| Ball array span (centre-to-centre of outer balls) | **8.00 × 8.00 mm** | D1, E1 |
| Pitch | **0.80 mm BSC** | eD, eE |
| Ball diameter | **0.25 / 0.30 / 0.35 mm** (min/nom/max) | b |
| Stand-off (ball height) | 0.15 mm min | A1 |
| Ball offset from package edge (centre of A1) | **1.00 mm** = (10.00 − 8.00)/2 *(derived)* | |
| SD / SE (outer-row centring) | 0.00 / 0.00 → **odd** number of balls per outer row, no offset | |
| Package code(s) | **BK0AA / FBI121 / T4A121** | |

### Orientation rules

1. **Figure 6 is a TOP VIEW** — "Figure 6 FX3 121-ball BGA ball map (Top view)" (doc 1, p. 27). In a top view: **A1 is the top-left cell**, columns **1 → 11 run left to right**, rows **A → L run top to bottom**.
2. The mechanical drawing confirms the mirror relationship: its **TOP VIEW** shows the A1 chamfer at the **top-left** of the body, while its **BOTTOM VIEW** shows ball 1 in the **top-right** with column labels `11 … 1` left-to-right. Flipping the bottom view horizontally reproduces Figure 6 exactly.
3. **A1 corner marking** — package drawing note 7: *"A1 CORNER TO BE IDENTIFIED BY CHAMFER, LASER OR INK MARK, METALIZED MARK, INDENTATION OR OTHER MEANS."* On the device in this workspace it is a laser dot plus the PCB silkscreen `A1` + triangle.
4. **Top view = PCB footprint view.** The package sits on the pads with A1 on the A1 pad, so the footprint seen from the board's top copper has exactly the Figure 6 arrangement. This is corroborated on the actual board: the silkscreen prints `1 —————→ 11` along the top of the footprint and `A ↓ L` down the left side, with the `A1` triangle at the top-left (photo `P1_top_side.webp`). **Confidence: confirmed** (datasheet + mechanical drawing + physical unit).

### Formula to convert a measured position on the package top to a ball

With origin at the **A1 corner** (top-left in top view), X to the right, Y downward, both in mm on the 10 mm body:

```
x_ball = 1.00 + 0.80 * (col - 1)      # A1 -> 1.00 mm, A11 -> 9.00 mm
y_ball = 1.00 + 0.80 * (row - 1)      # A  -> 1.00 mm, L   -> 9.00 mm
```

Inverse: `col = round((x - 1.00)/0.80) + 1`, `row = round((y - 1.00)/0.80) + 1`.
If your photo is upside-down relative to the marking text, rotate 180° first — otherwise you will mirror the map and land on the wrong balls.

---

## 4. Complete ball map (all 121 balls)

### 4.1 Grid (top view, exactly as datasheet Figure 6)

|     | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 | 11 |
|:---:|:--|:--|:--|:--|:--|:--|:--|:--|:--|:--|:--|
| **A** | U3VSSQ | U3RXVDDQ | SSRXM | SSRXP | SSTXP | SSTXM | AVDD | VSS | DP | DM | NC |
| **B** | VIO4 | FSLC0 | R_USB3 | FSLC1 | U3TXVDDQ | CVDDQ | AVSS | VSS | VSS | VDD | TRST# |
| **C** | G54 | G55 | VDD | G57 | RESET# | XTALIN | XTALOUT | R_USB2 | OTG_ID | TDO | VIO5 |
| **D** | G50 | G51 | G52 | G53 | G56 | CLKIN_32 | CLKIN | VSS | I2C58 | I2C59 | NC |
| **E** | G47 | VSS | VIO3 | G49 | G48 | FSLC2 | TDI | TMS | VDD | VBATT | VBUS |
| **F** | VIO2 | G45 | G44 | G41 | G46 | TCK | G2 | G5 | G1 | G0 | VDD |
| **G** | VSS | G42 | G43 | G30 | G25 | G22 | G21 | G15 | G4 | G3 | VSS |
| **H** | VDD | G39 | G40 | G31 | G29 | G26 | G20 | G24 | G7 | G6 | VIO1 |
| **J** | G38 | G36 | G37 | G34 | G28 | G16 | G19 | G14 | G9 | G8 | VDD |
| **K** | G35 | G33 | VSS | VSS | G27 | G23 | G18 | G17 | G13 | G12 | G10 |
| **L** | VSS | VSS | VSS | G32 | VDD | VSS | VDD | INT# | VIO1 | G11 | VSS |

`Gnn` = `GPIO[nn]`, `I2Cnn` = `I2C_GPIO[nn]`, `FSLCn` = `FSLC[n]`.

### 4.2 Ball-by-ball table

Source: doc 1, Figure 6 (map) + Table 7 (type / power domain / function). Every ball of the 11 × 11 array is listed; there are **no unverified balls**.

| Ball | Signal (datasheet) | Type | Power domain | Notes / common alias |
|:----:|:-------------------|:----:|:------------:|:---------------------|
| **A1** | `U3VSSQ` | PWR | — | Ground return for the USB3 SS block; corner ball A1. |
| **A2** | `U3RXVDDQ` | PWR | — | 1.15-1.25 V. Must be powered (ferrite bead) even if SS unused (AN70707 App. C). |
| **A3** | `SSRXM` | I | U3RXVDDQ | *SSRX- / SS_RX_M (USB3 SS receive, inverting)* — SuperSpeed RX differential pair, row A. Datasheet name: SSRXM. |
| **A4** | `SSRXP` | I | U3RXVDDQ | *SSRX+ / SS_RX_P (USB3 SS receive, non-inverting)* — SuperSpeed RX differential pair, row A. Datasheet name: SSRXP. |
| **A5** | `SSTXP` | O | U3TXVDDQ | *SSTX+ / SS_TX_P (USB3 SS transmit, non-inverting)* — SuperSpeed TX differential pair, row A. 0.1 uF AC-coupling cap in series (AN70707). |
| **A6** | `SSTXM` | O | U3TXVDDQ | *SSTX- / SS_TX_M (USB3 SS transmit, inverting)* — SuperSpeed TX differential pair, row A. 0.1 uF AC-coupling cap in series (AN70707). |
| **A7** | `AVDD` | PWR | — | 1.15-1.25 V analog core supply. Ferrite-bead isolated (AN70707). |
| **A8** | `VSS` | PWR | — | Ground. |
| **A9** | `DP` | I/O | VBUS/VBATT | *DPLUS / USB_D+ / DP* — USB 2.0 HS/FS/OTG data +. |
| **A10** | `DM` | I/O | VBUS/VBATT | *DMINUS / USB_D- / DM* — USB 2.0 HS/FS/OTG data -. |
| **A11** | `NC` | NC | — | *No connect* — Do not connect (leave open). |
| **B1** | `VIO4` | PWR | — | GPIO[53..57] / UART / SPI I/O supply. |
| **B2** | `FSLC[0]` | I | CVDDQ | *FSLC[0] / clock-frequency select 0* — Strapped at reset; selects 19.2 MHz crystal vs ext. clock frequency. |
| **B3** | `R_USB3` | I/O | U3TXVDDQ | *USB3 reference resistor* — 200 ohm +/-1% to GND (AN70707 Table/Fig 14). May be left open in USB2-only designs (AN70707 App. C). |
| **B4** | `FSLC[1]` | I | CVDDQ | *FSLC[1] / clock-frequency select 1* — Strapped at reset. |
| **B5** | `U3TXVDDQ` | PWR | — | 1.15-1.25 V. Must be powered (ferrite bead) even if SS unused (AN70707 App. C). |
| **B6** | `CVDDQ` | PWR | — | 1.7-3.6 V; supply for XTAL/CLKIN/RESET#/FSLC pins. |
| **B7** | `AVSS` | PWR | — | Analog ground return. |
| **B8** | `VSS` | PWR | — | Ground. |
| **B9** | `VSS` | PWR | — | Ground. |
| **B10** | `VDD` | PWR | — | 1.15-1.25 V core supply. |
| **B11** | `TRST#` | I | VIO5 | *JTAG TRST#* — JTAG, VIO5 domain, active low. |
| **C1** | `GPIO[54]` | I/O | VIO4 | UART_CTS / SPI_SSN |
| **C2** | `GPIO[55]` | I/O | VIO4 | UART_TX / SPI_MISO |
| **C3** | `VDD` | PWR | — | 1.15-1.25 V core supply. |
| **C4** | `GPIO[57]` | I/O | VIO4 | I2S_MCLK |
| **C5** | `RESET#` | I | CVDDQ | *RESET# / XRES / active-low reset* — Hard reset, active low. |
| **C6** | `XTALIN` | I/O | AVDD | *XTALIN / crystal in* — 19.2 MHz crystal (C6-C7). Can be left unconnected if external clock used. |
| **C7** | `XTALOUT` | I/O | AVDD | *XTALOUT / crystal out* — 19.2 MHz crystal (C6-C7). Can be left unconnected if external clock used. |
| **C8** | `R_USB2` | I/O | VBUS/VBATT | *USB2 reference resistor* — 6.04 kohm +/-1% to GND. May be left open in USB3-only designs (AN70707 App. D). |
| **C9** | `OTG_ID` | I | VBUS/VBATT | *OTG_ID* — USB OTG ID. May be left unconnected for device-only designs. |
| **C10** | `TDO` | O | VIO5 | *JTAG TDO* — JTAG, VIO5 domain. |
| **C11** | `VIO5` | PWR | — | I2C + JTAG I/O supply (can run at 1.2 V). |
| **D1** | `GPIO[50]` | I/O | VIO3 | I2S_CLK |
| **D2** | `GPIO[51]` | I/O | VIO3 | I2S_SD |
| **D3** | `GPIO[52]` | I/O | VIO3 | I2S_WS |
| **D4** | `GPIO[53]` | I/O | VIO4 | UART_RTS / SPI_SCK |
| **D5** | `GPIO[56]` | I/O | VIO4 | UART_RX / SPI_MOSI |
| **D6** | `CLKIN_32` | I | CVDDQ | *CLKIN_32 / 32 kHz clock in* — 32.768 kHz RTC/standby clock input. |
| **D7** | `CLKIN` | I | CVDDQ | *CLKIN / external clock in* — 19.2/26/38.4/52 MHz external clock. Leave open if crystal used. |
| **D8** | `VSS` | PWR | — | Ground. |
| **D9** | `I2C_GPIO[58]` | I/O | VIO5 | *I2C_SCL (GPIO[58])* — I2C master SCL; also GPIO. Boot-from-I2C uses this port. |
| **D10** | `I2C_GPIO[59]` | I/O | VIO5 | *I2C_SDA (GPIO[59])* — I2C master SDA; also GPIO. |
| **D11** | `NC` | NC | — | *No connect* — Do not connect (leave open). |
| **E1** | `GPIO[47]` | I/O | VIO3 | DQ[29] / UART_CTS |
| **E2** | `VSS` | PWR | — | Ground. |
| **E3** | `VIO3` | PWR | — | GPIO[46..52] / UART / I2S I/O supply. |
| **E4** | `GPIO[49]` | I/O | VIO3 | DQ[31] / UART_RX |
| **E5** | `GPIO[48]` | I/O | VIO3 | DQ[30] / UART_TX |
| **E6** | `FSLC[2]` | I | CVDDQ | *FSLC[2] / clock-frequency select 2* — Strapped at reset. |
| **E7** | `TDI` | I | VIO5 | *JTAG TDI* — JTAG, VIO5 domain. |
| **E8** | `TMS` | I | VIO5 | *JTAG TMS* — JTAG, VIO5 domain. |
| **E9** | `VDD` | PWR | — | 1.15-1.25 V core supply. |
| **E10** | `VBATT` | PWR | — | Battery/alternate USB I/O supply. |
| **E11** | `VBUS` | PWR | — | 4.0-6.0 V. USB VBUS sense/supply. |
| **F1** | `VIO2` | PWR | — | GPIO[33..45] I/O supply. |
| **F2** | `GPIO[45]` | I/O | VIO2 |  |
| **F3** | `GPIO[44]` | I/O | VIO2 |  |
| **F4** | `GPIO[41]` | I/O | VIO2 | DQ[24] / A0 (24-bit bus) |
| **F5** | `GPIO[46]` | I/O | VIO3 | DQ[28] / UART_RTS |
| **F6** | `TCK` | I | VIO5 | *JTAG TCK* — JTAG, VIO5 domain. |
| **F7** | `GPIO[2]` | I/O | VIO1 | GPIF II DQ[2] |
| **F8** | `GPIO[5]` | I/O | VIO1 | GPIF II DQ[5] |
| **F9** | `GPIO[1]` | I/O | VIO1 | GPIF II DQ[1] |
| **F10** | `GPIO[0]` | I/O | VIO1 | GPIF II DQ[0] |
| **F11** | `VDD` | PWR | — | 1.15-1.25 V core supply. |
| **G1** | `VSS` | PWR | — | Ground. |
| **G2** | `GPIO[42]` | I/O | VIO2 | DQ[25] / A1 (24-bit bus) |
| **G3** | `GPIO[43]` | I/O | VIO2 |  |
| **G4** | `GPIO[30]` | I/O | VIO1 | PMODE[0] strap (sampled at reset) |
| **G5** | `GPIO[25]` | I/O | VIO1 | CTL[8] |
| **G6** | `GPIO[22]` | I/O | VIO1 | CTL[5] / FLAGB |
| **G7** | `GPIO[21]` | I/O | VIO1 | CTL[4] / FLAGA |
| **G8** | `GPIO[15]` | I/O | VIO1 | GPIF II DQ[15] |
| **G9** | `GPIO[4]` | I/O | VIO1 | GPIF II DQ[4] |
| **G10** | `GPIO[3]` | I/O | VIO1 | GPIF II DQ[3] |
| **G11** | `VSS` | PWR | — | Ground. |
| **H1** | `VDD` | PWR | — | 1.15-1.25 V core supply. |
| **H2** | `GPIO[39]` | I/O | VIO2 |  |
| **H3** | `GPIO[40]` | I/O | VIO2 |  |
| **H4** | `GPIO[31]` | I/O | VIO1 | PMODE[1] strap (sampled at reset) |
| **H5** | `GPIO[29]` | I/O | VIO1 | CTL[12] / A0 |
| **H6** | `GPIO[26]` | I/O | VIO1 | CTL[9] |
| **H7** | `GPIO[20]` | I/O | VIO1 | CTL[3] / SLRD# |
| **H8** | `GPIO[24]` | I/O | VIO1 | CTL[7] / PKTEND# |
| **H9** | `GPIO[7]` | I/O | VIO1 | GPIF II DQ[7] |
| **H10** | `GPIO[6]` | I/O | VIO1 | GPIF II DQ[6] |
| **H11** | `VIO1` | PWR | — | GPIF II / GPIO[0..32] I/O supply. Must not be turned off while GPIF II is used (errata #1). |
| **J1** | `GPIO[38]` | I/O | VIO2 |  |
| **J2** | `GPIO[36]` | I/O | VIO2 |  |
| **J3** | `GPIO[37]` | I/O | VIO2 |  |
| **J4** | `GPIO[34]` | I/O | VIO2 |  |
| **J5** | `GPIO[28]` | I/O | VIO1 | CTL[11] / A1 |
| **J6** | `GPIO[16]` | I/O | VIO1 | PCLK (GPIF II clock output) |
| **J7** | `GPIO[19]` | I/O | VIO1 | CTL[2] / SLOE# |
| **J8** | `GPIO[14]` | I/O | VIO1 | GPIF II DQ[14] |
| **J9** | `GPIO[9]` | I/O | VIO1 | GPIF II DQ[9]/A1 (8-bit bus) |
| **J10** | `GPIO[8]` | I/O | VIO1 | GPIF II DQ[8]/A0 (8-bit bus) |
| **J11** | `VDD` | PWR | — | 1.15-1.25 V core supply. |
| **K1** | `GPIO[35]` | I/O | VIO2 |  |
| **K2** | `GPIO[33]` | I/O | VIO2 |  |
| **K3** | `VSS` | PWR | — | Ground. |
| **K4** | `VSS` | PWR | — | Ground. |
| **K5** | `GPIO[27]` | I/O | VIO1 | CTL[10] |
| **K6** | `GPIO[23]` | I/O | VIO1 | CTL[6] |
| **K7** | `GPIO[18]` | I/O | VIO1 | CTL[1] / SLWR# |
| **K8** | `GPIO[17]` | I/O | VIO1 | CTL[0] / SLCS# |
| **K9** | `GPIO[13]` | I/O | VIO1 | GPIF II DQ[13] |
| **K10** | `GPIO[12]` | I/O | VIO1 | GPIF II DQ[12] |
| **K11** | `GPIO[10]` | I/O | VIO1 | GPIF II DQ[10] |
| **L1** | `VSS` | PWR | — | Ground. |
| **L2** | `VSS` | PWR | — | Ground. |
| **L3** | `VSS` | PWR | — | Ground. |
| **L4** | `GPIO[32]` | I/O | VIO1 | PMODE[2] strap (sampled at reset) |
| **L5** | `VDD` | PWR | — | 1.15-1.25 V core supply. |
| **L6** | `VSS` | PWR | — | Ground. |
| **L7** | `VDD` | PWR | — | 1.15-1.25 V core supply. |
| **L8** | `INT#` | I/O | VIO1 | *INT# / CTL[15]* — GPIF II INT# (input) or CTL[15]. |
| **L9** | `VIO1` | PWR | — | GPIF II / GPIO[0..32] I/O supply. Must not be turned off while GPIF II is used (errata #1). |
| **L10** | `GPIO[11]` | I/O | VIO1 | GPIF II DQ[11] |
| **L11** | `VSS` | PWR | — | Ground. |

---

## 5. Mandatory focus signals

### 5.1 SuperSpeed (USB 3.2 Gen 1) — ball numbers and side

| Datasheet name | Alias | Ball | Direction | Power domain | Edge (top view) | x from left edge |
|---|---|---|---|---|---|---|
| `SSRXM` | SSRX−, SS_RX_M | **A3** | input | U3RXVDDQ | **top (row A)** | 2.60 mm |
| `SSRXP` | SSRX+, SS_RX_P | **A4** | input | U3RXVDDQ | **top (row A)** | 3.40 mm |
| `SSTXP` | SSTX+, SS_TX_P | **A5** | output | U3TXVDDQ | **top (row A)** | 4.20 mm |
| `SSTXM` | SSTX−, SS_TX_M | **A6** | output | U3TXVDDQ | **top (row A)** | 5.00 mm |

* **There is no `0` index** — FX3 has a single SuperSpeed port, so the datasheet names are bare `SSTXP/SSTXM/SSRXP/SSRXM` (no `SS_TX0_P` style). `SSRX+`/`SSRX−` appear in the text and in the ESD section (doc 1 §14, p. 26).
* Datasheet Table 7 (p. 29) literally reads: `A3 | U3RXVDDQ | I | SSRXM | SSRX-`, `A4 | U3RXVDDQ | I | SSRXP | SSRX+`, `A6 | U3TXVDDQ | O | SSTXM | SSTX-`, `A5 | U3TXVDDQ | O | SSTXP | SSTX+`.
* **Polarity order along the edge is RX−, RX+, TX+, TX−** (A3→A6). The RX pair sits next to the U3 supply corner (A1/A2); the TX pair sits next to AVDD (A7).
* AC coupling: 0.1 µF in series on the **TX** lines only, placed symmetrically and close to the FX3 (AN70707 §12.1, Fig. 15).
* ESD: only ±2.2 kV HBM on these four pins — external low-cap ESD (e.g. SP3010-04UTG, ≤0.5 pF) is recommended (AN70707 §10 / Fig. 12).

**Confidence: confirmed** by three independent sources:
1. Datasheet Figure 6 + Table 7 (001-52136 \*Z, and the JA 001-79232 \*C renders the same map);
2. AN70707 \*Q, Appendix C, p. 40 and Appendix B, p. 39, which name the balls explicitly: *"FX3/SX3 SuperSpeed pins TX+/- pins (A5 and A6) and RX+/- pins (A3 and A4) can be left open"* and *"the USB3 reference pin (B3)"*;
3. a third-party KiCad symbol + PCB (`cyusb3014-breakout`, see §8) where pads A3/A4 = `SS_RX_N`/`SS_RX_P` and A5/A6 = `SS_TX_C_P`/`SS_TX_C_N`.

### 5.2 USB 2.0

| Signal | Ball | Type | Note |
|---|---|---|---|
| `DP` (DPLUS, D+) | **A9** | I/O | VBUS/VBATT domain |
| `DM` (DMINUS, D−) | **A10** | I/O | VBUS/VBATT domain |
| `R_USB2` | **C8** | I/O | 6.04 kΩ ±1 % to GND |
| `OTG_ID` | **C9** | I | may be left open for device-only designs |

Confidence: **confirmed** (datasheet Table 7 + AN70707 App. B/D + third-party PCB pads A9=`SS_D_P`, A10=`SS_D_N`).

### 5.3 Power and ground

| Rail | Voltage | Balls | Count | Notes |
|---|---|---|---|---|
| **VDD** (core) | 1.15–1.25 V | **B10, C3, E9, F11, H1, J11, L5, L7** | 8 | 22 µF per group + 0.1/0.01 µF per pin |
| **AVDD** (analog) | 1.15–1.25 V | **A7** | 1 | ferrite bead isolated; 2.2 µF + 0.1 µF |
| **AVSS** | 0 V | **B7** | 1 | analog ground |
| **VIO1** | 1.7–3.6 V | **H11, L9** | 2 | GPIF II / GPIO[0..32]; must not be switched off (errata #1) |
| **VIO2** | 1.7–3.6 V | **F1** | 1 | GPIO[33..45] |
| **VIO3** | 1.7–3.6 V | **E3** | 1 | GPIO[46..52], UART, I2S |
| **VIO4** | 1.7–3.6 V | **B1** | 1 | GPIO[53..57], SPI |
| **VIO5** | 1.15–3.6 V | **C11** | 1 | I²C + JTAG (may be 1.2 V) |
| **CVDDQ** | 1.7–3.6 V | **B6** | 1 | clock/reset I/O supply |
| **VBATT** | 3.2–6 V | **E10** | 1 | USB I/O supply (battery) |
| **VBUS** | 4.0–6 V | **E11** | 1 | USB I/O supply (bus) |
| **U3TXVDDQ** | 1.15–1.25 V | **B5** | 1 | USB3 TX 1.2 V; 22 µF + 0.1 µF |
| **U3RXVDDQ** | 1.15–1.25 V | **A2** | 1 | USB3 RX 1.2 V; 22 µF + 0.1 µF |
| **U3VSSQ** | 0 V | **A1** | 1 | USB3 SS ground return (corner ball) |
| **VSS** | 0 V | **A8, B8, B9, D8, E2, G1, G11, K3, K4, L1, L2, L3, L6, L11** | 14 | |

There is **no `VDDUSB` and no separate `AVDD33`** on this device — the USB 2.0 PHY is fed from VBUS/VBATT, and the analog 1.2 V rail is `AVDD`. The datasheet §17.1 misprints the absolute-maximum line as "VDD, AVDDQ"; the correct rail name per Table 7 / Figure 1 of AN70707 is **AVDD**.

Total power/ground balls: **36 of 121**. Confidence: **confirmed** — datasheet Figure 6 + Table 7, and independently **AN70707 Table 3** (p. 8) which lists `VDD (B10, J11)`, `VDD (H1, L7, F11, L5)`, `VDD (C3, E9)`, `AVDD (A7)`, `U3RXVDDQ (A2)`, `U3TXVDDQ (B5)`, `CVDDQ (B6)`, `VIO1 (L9, H11)`, `VIO2 (F1)`, `VIO3 (E3)`, `VIO4 (B1)`, `VIO5 (C11)`, `VBUS (E11)`.

### 5.4 Clock (19.2 MHz crystal)

| Signal | Ball | Notes |
|---|---|---|
| **XTALIN** | **C6** | 19.2 MHz crystal, AVDD domain |
| **XTALOUT** | **C7** | 19.2 MHz crystal, AVDD domain |
| `CLKIN` | **D7** | external clock in (19.2 / 26 / 38.4 / 52 MHz) |
| `CLKIN_32` | **D6** | 32.768 kHz standby clock |
| `FSLC[0]` / `FSLC[1]` / `FSLC[2]` | **B2 / B4 / E6** | frequency-select straps sampled at reset |

Datasheet §10: *"FX3 allows either a crystal to be connected between the XTALIN and XTALOUT pins or an external clock to be connected at the CLKIN pin. The XTALIN, XTALOUT, CLKIN, and CLKIN_32 pins can be left unconnected if they are not used."*
Confidence: **confirmed**.

### 5.5 Reset, boot mode, wakeup

| Signal | Ball | Notes |
|---|---|---|
| **RESET#** | **C5** | active-low hard reset, CVDDQ domain |
| **PMODE[0]** | **G4** | = GPIO[30], strap sampled at reset |
| **PMODE[1]** | **H4** | = GPIO[31] |
| **PMODE[2]** | **L4** | = GPIO[32] |
| `I2C_SCL` | **D9** | = I2C_GPIO[58] |
| `I2C_SDA` | **D10** | = I2C_GPIO[59] |

* **There is no `WAKEUP` ball and no `BOOTLOADER` ball on FX3.** Boot source is selected *only* by the PMODE[2:0] strap at reset (Table 2/§8 of the datasheet); wakeup from suspend/standby is done by a firmware-configured GPIO (`GCTL_WAKEUP_*` registers, TRM §10.5) or by USB resume. If a schematic you are reverse-engineering shows a "WAKEUP" net on the FX3, it is a GPIO, not a dedicated pin. Confidence: **confirmed** (exhaustive 121-ball list + TRM register names).

### 5.6 Serial / debug (cheap to include)

| Function | Balls |
|---|---|
| **JTAG** | `TCK` **F6**, `TMS` **E8**, `TDI` **E7**, `TDO` **C10**, `TRST#` **B11** (all VIO5) |
| **I²C** | `SCL` **D9**, `SDA` **D10** (VIO5) |
| **SPI** (16-bit + UART + SPI config) | `SPI_SCK` **D4**, `SPI_SSN` **C1**, `SPI_MISO` **C2**, `SPI_MOSI` **D5** (VIO4) |
| **UART** (same config) | `UART_RTS` **D4**, `UART_CTS` **C1**, `UART_TX` **C2**, `UART_RX` **D5** |
| **UART** (32-bit config) | `UART_RTS` **F5**, `UART_CTS` **E1**, `UART_TX` **E5**, `UART_RX` **E4** |
| **I²S** | `I2S_CLK` **D1**, `I2S_SD` **D2**, `I2S_WS` **D3**, `I2S_MCLK` **C4** |

Note the SPI and UART signals **share the same four balls** (D4, C1, C2, D5) — the function is chosen by the GPIF II / IO configuration, not by different pins. Confidence: **confirmed** (Table 7 pp. 29).

### 5.7 GPIF II block location (summary, not all 60+ pins)

The GPIF II pins are `GPIO[0]`…`GPIO[32]` (DQ[0..31] + 16 CTL lines + PCLK + INT#). They occupy **rows F through L, columns 4 through 11** — i.e. the **bottom half / bottom-right quadrant** of the package in top view. The top-left region (rows A–E, columns 1–7) is USB, clock, reset and power.

Key control balls (slave-FIFO example mapping from Table 7):

| Signal | Ball | GPIO |
|---|---|---|
| **PCLK** | **J6** | GPIO[16] |
| **SLCS#** | **K8** | GPIO[17] / CTL[0] |
| **SLWR#** | **K7** | GPIO[18] / CTL[1] |
| **SLOE#** | **J7** | GPIO[19] / CTL[2] |
| **SLRD#** | **H7** | GPIO[20] / CTL[3] |
| **FLAGA** | **G7** | GPIO[21] / CTL[4] |
| **FLAGB** | **G6** | GPIO[22] / CTL[5] |
| **PKTEND#** | **H8** | GPIO[24] / CTL[7] |
| DQ[0] … DQ[7] | F10, F9, F7, G10, G9, F8, H10, H9 | GPIO[0..7] |
| DQ[8] … DQ[15] | J10, J9, K11, L10, K10, K9, J8, G8 | GPIO[8..15] |
| DQ[16] … DQ[23] | K2, J4, K1, J2, J3, J1, H2, H3 | GPIO[33..40] |
| DQ[24] … DQ[31] | F4, G2, G3, F3, F5, E1, E5, E4 | GPIO[41..49] |

Caveat: the `CTL[n]` → `SLCS#/SLWR#/SLOE#/SLRD#/FLAGA/FLAGB/PKTEND#` naming above is the **example slave-FIFO configuration** from Table 7; the datasheet explicitly notes *"The Slave FIFO control signal assignments can be modified using GPIF-II designer tool"* (Table 7, note 2). The **ball ↔ GPIO number** binding is fixed by silicon; only which CTL index carries which FIFO flag is configurable. PCLK is on GPIO[16] = ball J6.

---

## 6. Which edges carry what — where the SuperSpeed traces must escape

Edge membership (top view, A1 = top-left):

| Edge | Balls | Signals of interest |
|---|---|---|
| **Top (row A)** | A1–A11 | **U3VSSQ(A1), U3RXVDDQ(A2), SSRXM(A3), SSRXP(A4), SSTXP(A5), SSTXM(A6)**, AVDD(A7), VSS(A8), DP(A9), DM(A10), NC(A11) |
| **Bottom (row L)** | L1–L11 | VSS, VSS, VSS, GPIO[32]/PMODE[2](L4), VDD, VSS, VDD, INT#(L8), VIO1(L9), GPIO[11](L10), VSS |
| **Left (col 1)** | A1…L1 | U3VSSQ, VIO4, GPIO[54], GPIO[50], GPIO[47], VIO2, VSS, VDD, GPIO[38], GPIO[35], VSS |
| **Right (col 11)** | A11…L11 | NC, TRST#, VIO5, NC, VBUS, VDD, VSS, VIO1, VDD, GPIO[10], VSS |

**Consequence for the photograph:** the USB3 SuperSpeed escape is a **4-ball-wide group on the TOP edge, left of centre** — columns 3–6, i.e. **2.6 mm to 5.0 mm from the left package edge, 1.0 mm from the top edge**. The neighbours along that same row are:

| x from left edge | **1.0 mm** | 1.8 mm | **2.6 mm** | **3.4 mm** | **4.2 mm** | **5.0 mm** | 5.8 mm |
| Ball (row A) | A1 | A2 | **A3** | **A4** | **A5** | **A6** | A7 |
| Signal | U3VSSQ | U3RXVDDQ | **SSRXM (RX−)** | **SSRXP (RX+)** | **SSTXP (TX+)** | **SSTXM (TX−)** | AVDD |
| Function | USB3 gnd | USB3 RX 1.2 V | ← RX pair | | TX pair → | | analog 1.2 V |

* The **RX pair (A3/A4)** is the pair nearer the A1 corner and the U3RXVDDQ ball (A2).
* The **TX pair (A5/A6)** is the pair nearer AVDD (A7); its AC-coupling caps and any ESD device sit on that side.
* Row **B** below them carries `R_USB3` (B3, 200 Ω reference resistor) and `U3TXVDDQ` (B5) — so a small cluster of USB3 support parts (200 Ω, 22 µF/0.1 µF, AC caps, ESD) is expected **immediately outside the top edge, columns 1–6**.
* If the photographed board's top-edge escape group is 4 traces wide and starts ~1/4 of the way in from the A1 corner, that is the SuperSpeed port. If instead you see 2 traces on the top edge near A9/A10 (columns 9–10, right of centre), that is **USB 2.0 D+/D−**, not SuperSpeed.

Confidence: **confirmed** for the ball coordinates (primary doc); the "where the traces run on the PCB" statement is a **derived/expected** inference from ball position, not something the datasheet states.

---

## 7. Can the SuperSpeed pairs be left unconnected if USB 3.0 is unused?

**The datasheet does not say so explicitly.** It only marks the SS pins `NC` for the USB-2.0-only sibling part CYUSB2014 in Table 7, and adds the note under Figure 7: *"A2 and C3 need not be connected for FX3 Hi-Speed part."* There is no sentence in 001-52136 \*Z permitting SS pins to float on a CYUSB301x.

**The explicit statement is in AN70707 \*Q, Appendix C — "FX3/SX3 device connections to USB 2.0-only host", p. 40** (primary application note, confirmed):

> • *"FX3/SX3 SuperSpeed pins TX+/- pins (**A5 and A6**) and RX+/- pins (**A3 and A4**) can be left open."*
> • *"Both USB3.0 power pins (U3TXVDDQ and U3RXVDDQ) should be connected to 1.2V with a ferrite bead."*
> • *"The USB3 reference pin (**B3**) can be left open."*
> • *"Make sure that layout guidelines are followed per Section 12.1."*

And the mirror case, **Appendix D — "connections to USB 3.0-only host", p. 41**:

> • *"FX3/SX3 High-Speed pins DP/DM pins (**A9 and A10**) can be left open (or) it can be connected to other embedded controllers."*
> • *"The USB2 reference pin (**C8**) can be left open."*

**Practical reading:** you may leave A3/A4/A5/A6 and B3 open, **but you must still power B5 (U3TXVDDQ) and A2 (U3RXVDDQ) at 1.2 V** through a ferrite bead, and A1 (U3VSSQ) must be grounded. Do **not** copy the **CYUSB2014** (Hi-Speed, USB 2.0-only) ball map by mistake: there **A1 = VSS, A2 = VDD, B5 = VDD** (the USB3 supply balls are re-purposed to core supply/ground) and **A3–A6 and B3 are NC** (datasheet Figure 7, p. 27). That is a different device, not a configuration of the CYUSB3014.

---

## 8. Reference schematics / design material

| Source | What it gives you | Confidence |
|---|---|---|
| **AN70707 \*Q**, Fig. 14 (p. 21) — USB2/USB3 reference resistors; Fig. 15 (p. 21) — SS TX AC-coupling caps; Fig. 41 (p. 38) — USB3.0 Type-B receptacle ↔ FX3 connections; Fig. 42 (p. 38) — hub/embedded controller ↔ FX3 | the canonical FX3 USB3 front-end, with ball numbers in the surrounding text | primary |
| **AN70707 \*Q**, Appendix C/D (pp. 40–41) | exact "leave open" rules quoted in §7 | primary |
| **AN70707 \*Q**, Fig. 1 + Table 3 (pp. 6–8) | power-domain ball numbers and per-rail decoupling | primary |
| **AN70707 \*Q**, §11 Fig. 13 (p. 20) | package dimensions; recommends **11 mil** BGA pad / mask / paste | primary |
| `https://github.com/danielkucera/cyusb3014-breakout` — `lib/usb_controller.lib`, `project.kicad_pcb` | a complete, public FX3 breakout design. Verified pad→net map: A1=GND, A2/A7=+1V2, A3=`SS_RX_N`, A4=`SS_RX_P`, A5=`SS_TX_C_P`, A6=`SS_TX_C_N`, A9=`SS_D_P`, A10=`SS_D_N`, B1/B6/C11/E3/F1/H11/L9=+3V3, B3→200 Ω, C5=reset, C6/C7=crystal, E10=VBATT, E11=VBUS | **secondary** (third-party, but independent and machine-readable) |
| Board in this workspace: `hardware/photos/P1_top_side.webp` | actual device marked `CYUSB3014-BZX` / `I 2237`; the laser dot, the filled triangle and both direction arrows sit at the **bottom-right** corner, and the marking text is upside-down → **A1 is bottom-right, row A the bottom edge**. (An earlier version of this row said "A1 triangle top-left"; that was a mis-read of the FPGA's `22…1` numbering, and it contradicted the calibrated grid in `ball_grid_calibration.md`.) | observation |
| Infineon **CYUSB3KIT-003 SuperSpeed Explorer Kit User Guide**, doc **001-93186 Rev. \*A** (2014) | obtained: board block diagram, photo, VIO-domain jumper table (VIO1-3 = 3.3 V or 1.8 V; **VIO4/VIO5 fixed at 3.3 V**) — but it does **not** contain the FX3 schematic itself (schematic/PCB/gerber are distributed separately as kit design files) | secondary, no ball data |

---

## 9. Known inconsistencies, confidence summary, and what is missing

### 9.1 Inconsistencies found *inside* the primary datasheet

1. **Ball D11.** Figure 6 and Figure 7 both show **`NC`** at D11, and Table 7's NC block lists two `NC` rows (one with a blank ball number + `A11`). But Table 7's "I2C and JTAG" block lists `D11 | VIO5 | O | O[60] | GPIO`. **These contradict each other.** Recommendation: treat **D11 as no-connect / do not route**; do not rely on GPIO[60] being bonded. Confidence in D11 = **conflicting source**.
2. **Table 7 power block has empty BGA cells** for duplicate rails (a second `VDD`, a second `VIO1`, a second `VIO2`, two extra `VSS`). The ball numbers in §5.3 are taken from **Figure 6** and cross-checked against **AN70707 Table 3**, not from those blank rows.
3. Datasheet §17.1 writes "VDD, **AVDDQ**" in the absolute-maximum table; the rail is **AVDD** everywhere else (Table 7, AN70707 Table 2). Typo in the datasheet.

### 9.2 Confidence per item

| Item | Confidence | Basis |
|---|---|---|
| Package geometry (10×10, 0.8 pitch, 0.30 ball, 11×11 full, A1 chamfer) | **confirmed** | 001-54471 \*F via Figure 30 |
| All 121 ball names | **confirmed** | Figure 6 (EN) + Figure 6 (JA) + Table 7 |
| SS balls A3/A4/A5/A6 and their polarity | **confirmed** | datasheet + AN70707 App. B/C + third-party PCB |
| DP/DM = A9/A10 | **confirmed** | datasheet + AN70707 App. D + third-party PCB |
| Power balls (36) | **confirmed** | Figure 6 + Table 7 + AN70707 Table 3 |
| XTALIN/XTALOUT = C6/C7 | **confirmed** | Table 7 + third-party PCB + AN70707 Fig. 9 |
| RESET# = C5, PMODE = G4/H4/L4 | **confirmed** | Table 7 |
| I²C/JTAG/SPI/UART balls | **confirmed** | Table 7 |
| "SS may be left open" | **confirmed** (AN70707) / *absent from datasheet* | quoted in §7 |
| Tape-and-reel codes -BZXCT/-BZXIT | **probable** | distributor catalogues, not in datasheet |
| Package marking layout (`CYUSB3014-BZX` + `I dddd`) | **probable / observed on 1 unit** | photo |
| D11 | **conflicting** | Figure 6 vs Table 7 |
| `001-67239` as an FX3 doc number | **unverified / not found** | — |

### 9.3 Explicitly missing

* **No IBIS model** was obtained (the Infineon community IBIS threads are behind a JS challenge). An IBIS `.ibs` file would be the ideal machine-readable second source and is the recommended next step: it lists `[Pin]` numbers A1…L11 with `signal_name`.
* **No official Infineon reference-design schematic PDF with ball numbers** was obtained: the CYUSB3KIT-003 user guide (001-93186 Rev. \*A) contains only a block diagram and photos — the actual kit schematic/gerber are separate design files on the kit's Design Resources page, which were not downloaded. AN70707's figures + the third-party KiCad design cover this need in practice.
* **No 3-D/STEP model** or land-pattern (IPC-7351) drawing was retrieved; the datasheet gives pad recommendations only indirectly (AN70707 suggests 11 mil pads).
* Nothing about balls is guessed, so there is **no `UNVERIFIED` ball** in the tables. The only genuinely unresolved ball is **D11** (conflicting), plus the *existence* of GPIO[60].

---

## 10. Sources (all accessed 2026-11-07)

**Primary**
1. Infineon, *CYUSB301X, CYUSB201X, EZ-USB FX3 SuperSpeed USB Controller — Datasheet*, **001-52136 Rev. \*Z**, 2022-09-29 — <https://www.infineon.com/assets/row/public/documents/30/49/infineon-cyusb301x-cyusb201x-ez-usb-fx3-superspeed-usb-controller-datasheet-en.pdf>
   - §15 Figure 6 "FX3 121-ball BGA ball map (Top view)", p. 27
   - §16 Table 7 "CYUSB3011, CYUSB3012, CYUSB3013, CYUSB3014 and CYUSB2014 pin list", pp. 28–31
   - §10 "Clocking", p. 19 (XTALIN/XTALOUT/CLKIN may be left unconnected)
   - §14 "System-level ESD", p. 26
   - §20 Figure 30 "121-ball BGA package diagram" (= 001-54471 \*F), p. 66
   - §21 Table 24 "Ordering information" + §21.1 "Ordering code definitions", p. 67
   - Revision history, pp. 75–81
2. Infineon, same datasheet — Japanese, **001-79232 Rev. \*C**, 2022-11-30 — <https://www.infineon.com/dgdl/Infineon-CYUSB301X_CYUSB201X_EZ-USB_FX3_SuperSpeed_USB_Controller-DataSheet-v04_00-JA.pdf?fileId=8ac78c8c7d0d8da4017d0eca15ef4296>
3. Infineon/Cypress, *AN70707 — EZ-USB™ FX3/FX3S/SX3 hardware design guidelines and schematic checklist*, **001-70707 Rev. \*Q**, 2021-10-26 — <https://www.infineon.com/assets/row/public/documents/30/42/infineon-an70707---ez-usb-fx3-fx3s-sx3-hardware-design-guidelines-and-schematic-checklist-applicationnotes-en.pdf>
   - §3.1 Figure 1 + Table 2/3 (power domains, ball numbers), pp. 6–8
   - §11 package dimensions, p. 20
   - §12.1 USB 3.0 SuperSpeed design guidelines, p. 21
   - §17 Appendix B troubleshooting (B3/C8 ball numbers), p. 39
   - §18 Appendix C "connections to USB 2.0-only host", p. 40
   - §19 Appendix D "connections to USB 3.0-only host", p. 41
4. Infineon/Cypress, *EZ-USB® FX3™ Technical Reference Manual*, **001-76074 Rev. \*F**, 2019-05-09 — <https://www.infineon.com/assets/row/public/documents/30/57/infineon-ez-usb-fx3-technical-reference-manual-additionaltechnicalinformation-en.pdf>
5. Infineon product page, CYUSB3014-BZXI — <https://www.infineon.com/part/CYUSB3014-BZXI>

**Secondary**
6. danielkucera, *cyusb3014-breakout* (KiCad symbol + PCB) — <https://github.com/danielkucera/cyusb3014-breakout> (files `lib/usb_controller.lib`, `project.kicad_pcb`)
7. DigiKey part pages for `CYUSB3014-BZXC` / `-BZXI` / `-BZXCT` (tape-and-reel codes, availability) — e.g. <https://www.digikey.com/en/products/detail/infineon-technologies/CYUSB3014-BZXCT/4573035>
8. Workspace photo of the assembled board: `hardware/photos/P1_top_side.webp`

**Not usable / not found**
* `001-67239` — no such FX3 document resolves; not cited.
* Infineon community threads (IBIS model, KBA219491) — blocked by a JS challenge in this environment; not cited as evidence.
