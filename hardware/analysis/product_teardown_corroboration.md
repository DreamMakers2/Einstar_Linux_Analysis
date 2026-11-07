# Einstar (`EinScan10_01`, `3267:0003`) — public teardown, connector and USB-link corroboration

**Research date:** 2026-11-07
**Scope:** public evidence only (web). Local artifacts are used only where they are *product-level*
evidence shipped by SHINING 3D (manuals, INF files, UI strings) — the FX3 firmware analysis that
proves the SS PHY is enabled is **not** repeated here (see `analysis/usb3/firmware_descriptors.txt`).

**Confidence legend**

| Label | Meaning |
|---|---|
| **CONFIRMED** | Stated by a primary source (SHINING 3D document, regulatory filing) and read directly. |
| **PROBABLE** | Supported by ≥2 independent observations, or by one primary source plus physical reasoning. |
| **UNVERIFIED** | No public source; recorded as an open question. Never presented as fact. |

---

## 0. Bottom line

| Question | Answer | Conf. |
|---|---|---|
| Is there a public teardown of the **Einstar** itself? | **No public teardown found.** No FCC filing exists for it (it has no intentional radiator), and no teardown video/photo set surfaced in EN or ZH sources. | CONFIRMED (negative) |
| Does any public photo show the Einstar mainboard? | **No.** The only public SHINING 3D "internal photos" filing is for the **Einstar Vega**, a different product. | CONFIRMED |
| Is the Einstar officially USB 2.0? | **Yes** — the official manual and the official docs site both list `Interface: USB 2.0`. | CONFIRMED |
| Does any source claim USB 3.0? | **Yes, one** — a SHINING 3D forum moderator recommends a "USB 3.0 Type-A" cable. It conflicts with the manual. | CONFIRMED (statement exists) / conflict unresolved |
| Is a pinout published? | **No.** No pinout, no connector part number, no service manual. | CONFIRMED (negative) |
| Can the connector family be identified? | **No.** Only its *class* is documented (keyed push-pull, red alignment dot). Family remains **UNVERIFIED**; a shortlist is given in §2.3. | UNVERIFIED |
| Do SHINING 3D's own binaries expect USB 3.0? | **Yes** — the shipped EXStar app warns `"The current USB dirve is USB2.0"` / `"Notice: Please change to USB3.0"`, and the driver package is branded **U3**. | CONFIRMED (strings read from official installer) |
| Does a USB 3.0 variant / cable exist? | **No Einstar USB 3.0 cable is sold.** The successor **Einstar 2** is USB 3.0 over **USB Type-C**. | CONFIRMED |

The interesting result is the **contradiction**: the hardware path is documented as USB 2.0 and the
unit enumerates at 480 Mbps, yet SHINING 3D's software, driver package and one support answer all
assume USB 3.0. That is consistent with an FX3 design whose SuperSpeed pairs are enabled in firmware
but not carried to the host by the shipped cable/connector — but **no public source proves where the
SS pairs terminate**. See §7 for what the supplied photos can and cannot settle.

---

## 1. Teardown / internal photos

### 1.1 Regulatory route: FCC

The Einstar is a wired device with no radio (it is powered from its own 12 V/5 A adapter), so it has
**no FCC equipment authorization of its own**. The grantee code for SHINING 3D Tech. Co., Ltd. is
`2AMG4`; the public grantee listing contains **28 FCC IDs**, none of which is the Einstar handheld:

<https://fccid.io/2AMG4>

Relevant entries from that list (product purpose as filed):

| FCC ID | Date | Purpose |
|---|---|---|
| `2AMG4-EINSTAR3` | 2024-11-13 | Wi-Fi & Bluetooth Module (Class II permissive change) — **host device is the Einstar Vega** |
| `2AMG4-ROCKIT` | 2025-11-26 | 3D Scanner |
| `2AMG4-EINSTART-P` / `-C` | 2018 / 2017 | 3D Printer (the similarly-named *Einstart* printer line) |
| `2AMG4-FSTTGE0A0`, `-FSTSTE0A0`, `-FS7SS`, … | 2025–2026 | FreeScan-family laser scanners |

**Trap to avoid:** the string `EINSTAR3` looks like "Einstar 3" but is the **Einstar Vega**. The
product label exhibit for that FCC ID reads:

> `Type: 3D Scanner   Model: Einstar Vega   Rating: 14V == 5A(MAX)   MADE IN CHINA`

<https://fccid.io/2AMG4-EINSTAR3/Label/Product-Label-and-Location-Host-7595632>

The schematics and block-diagram exhibits of that filing are **not public** (the exhibit pages carry
no document URL; they are held under confidentiality). Only photos, labels, manuals and test reports
are downloadable.

### 1.2 The one public SHINING 3D internal-photo set — and why it is not the Einstar

Public exhibits in `2AMG4-EINSTAR3`:

* Internal Photos — <https://fccid.io/2AMG4-EINSTAR3/Internal-Photos/Internal-Photos-Host-7819373> (4 pages)
* Internal Photos — <https://fccid.io/2AMG4-EINSTAR3/Internal-Photos/Internal-Photos-Host-7595634> (3 pages)

What is visible (read from the filings; no images redistributed here):

* A handheld body roughly 150 × 60 mm (ruler in frame), opened to show a single green mainboard.
* A **lithium-ion pouch cell** with a `YJ` / `4.45V` label — i.e. a **battery-powered** device.
* A metal-shielded **Wi-Fi/BT module**; the filing identifies it as `FCE863R` (module grant
  `XMR2024FCE863R`, change-in-identification), with SAR testing at 2.4/5 GHz and Bluetooth.
* A **touchscreen FPC connector** silkscreened `触摸显示屏接口` ("touch display interface") and a
  **camera-module connector** silkscreened `大相机模组接口` ("large camera module interface").
* A **USB Type-C** receptacle at the bottom edge of the board.
* Board silkscreen `PCB008672/A030`.
* Air vents, a tripod thread, and a status LED.

This is a **standalone, screened, battery + Wi-Fi product** — nothing like the USB-powered,
bus-powered Einstar. It is reported here only because it is the single public SHINING 3D scanner
teardown, and because it answers "what does a 2024-era SHINING 3D scanner mainboard look like"
(SoC + Wi-Fi module + battery charger + Type-C), **not** "what is inside the Einstar".
The resolution of the published photos does **not** permit chip-level identification, and no
FX3/Pango device is identifiable in them.

### 1.3 Everything else that was searched

No Einstar teardown, disassembly, repair guide, board photo or chip-level discussion was found.
Searches performed (all returned no Einstar internals):

| Where | Query / probe | Result |
|---|---|---|
| Web (EN) | `Einstar teardown disassembly 3D scanner mainboard` | marketing pages only |
| Web (EN) | `Einstar scanner repair internal photos` | none |
| Web (EN) | `Einstar CYUSB3014` / `Einstar PGL25G` / `Einstar scanner board Pango FPGA` | no page pairs the Einstar with either chip |
| Web (ZH) | `Einstar 拆解 拆机 扫描仪 主板`, `Einstar 拆机 主板 芯片 先临三维`, `Einstar 扫描仪 拆解 知乎` | no teardown article |
| Bilibili | `search.bilibili.com/all?keyword=Einstar 拆解` | search page returns no teardown result (API blocked, HTTP 412) |
| YouTube | `einstar teardown`, `einstar disassembly`, `einstar repair`, `einstar inside teardown` | only reviews, calibration and "scan a car part" videos; **no disassembly** |
| Reddit | `search.json` for `einstar teardown / connector / cable / usb 3.0` | API blocked (HTML challenge); site-scoped web search returned only the Vega review thread `r/3DScanning` |
| Official forum | `forum.einstar.com/search.json?q=cable` (41 posts) / `connector` / `data cable` / `pin` | only cable-length and connection-fault threads (§2.1) |
| SHINING 3D KB | `support.einstar.com` search for `cable`, `connector`, `power adapter` | no cable/connector article (search is JS-only; article index probed directly) |
| Official store | `einstar.com/sitemap.xml` → product sitemap (23 products) | **no replacement data cable** is sold; accessories are markers, tripod, SpaceMouse, Pro Kit |
| Repair sites | `ifixit`-style query | none |

**Verdict: no public teardown of the Einstar exists as of 2026-11-07.**

---

## 2. Connector identity

### 2.1 What the official documentation actually says

From the Einstar device page and manual (official):

* The cable plugs into the **bottom** of the scanner.
* **"the red dot should be consistent with the front side of the scanner"** — i.e. the connector is
  **keyed with a visible red alignment dot on the plug**.
  <https://docs.shining3d.com/exstar/1.0.0/en-us/device/>
* The power supply is **12 V / 5 A**, and the power cable **plugs into the data cable**, not into the
  scanner — i.e. **12 V is injected inline** at a barrel jack on the cable assembly.
  <https://docs.shining3d.com/exstar/1.2.2/en-us/install/> (same text in the V1.2.1.1 manual PDF,
  sha256 `7750996e…4cd3d4`)
* The host end is a **USB Type-A** plug (official component illustration, item ③ in
  `https://doc-asset.shining3d.com/exstar/components.png`).

The same page's illustrations show the scanner-end connector as a **slim cylindrical plug with a
ribbed/knurled coupling collar and a red dot**, mating into a **recessed oval socket with a single
row of flat contacts**. The drawing is stylised (it is line art, not a photo), so the contact count
in the drawing is **not** reliable — it must not be used to argue 6 vs 7 pins.

### 2.2 Pin count and the pin budget

The project's physical inspection reports **7 contacts**. Taking that as given:

* A USB 3.0 SuperSpeed link needs **4** additional contacts (SSTX±, SSRX±) on top of **D+/D−**,
  **VBUS/12 V** and **GND**.
* 7 contacts can only carry SS **if** ground returns through the shell:
  `12 V, D+, D−, SSTX+, SSTX−, SSRX+, SSRX−` = exactly 7.
* Returning a 12 V / 5 A (60 W) rail through a connector shell is poor practice and unusual in a
  consumer product, so the far more likely allocation is
  `12 V, GND, D+, D−` + 3 others (ID/insertion-detect, shield/drain, spare or a power enable).

**Assessment (PROBABLE, not confirmed):** the 7-pin external link is a **USB 2.0 + 12 V** interface,
and the SuperSpeed pairs do not leave the scanner through it. This is a *pin-budget* argument, not a
documented fact — it would be overturned by a continuity test or a connector pinout.

### 2.3 Connector family — shortlist and why it stays UNVERIFIED

Documented properties: 7 contacts, single row, keyed, push-pull/knurled coupling, red alignment dot,
moulded plug body, 12 V/5 A capability.

| Candidate family | Fit | Notes |
|---|---|---|
| **Hirose HR10** (HR10A-7P-7S) | plausible | 7-pin push-pull, keyed, common on industrial cameras; but HR10 uses **round pins in a circle**, not a single row of flats |
| **LEMO 0B / 1B** (e.g. FGG.0B.307) | plausible | push-pull, keyed, red marking common; expensive, usually metal shell |
| **ODU MINI-SNAP size 0** | plausible | push-pull, keyed, 7-pin variants exist |
| **Weipu SF / SP** | plausible | Chinese push-pull circular, widely used in domestic instruments; cost fits a ~USD 1 k scanner |
| **Hirose LF series** (rectangular push-pull, single row) | **best geometric fit** | single row of flat contacts + push-pull sleeve matches the official drawing better than a circular MIL-style shell |
| **Custom SHINING 3D part** | plausible | no distributor listing, no datasheet, no service part number found |

**Status: UNVERIFIED.** No public source names the connector, and no distributor listing for an
Einstar cable exists (the official store does not sell one — §1.3). Any family claim beyond the
shortlist above would be fabrication.

### 2.4 Published pinout

**None.** No pinout, no service manual, no schematic, no repair article, and no forum post documents
which contact is 12 V, GND, D+, D−.

The only related public statement is SHINING 3D's answer about cable **length**, which does not
describe the connector:

> "High-Speed、 USB 3.0、 Type-A、Support data transmission extension cable. For lengths over 3 meters,
> it is recommended to use a cable with a signal amplifier."
> — SHINING 3D staff, 2025-03-12, <https://forum.einstar.com/t/einstar-data-cable/929>

---

## 3. USB 2.0 vs USB 3.0

### 3.1 Official statements

| Product | Statement | Source |
|---|---|---|
| **Einstar** | `Interface  USB 2.0.` (Operating Environment table) | <https://docs.shining3d.com/exstar/1.2.2/en-us/install/> |
| **Einstar** | `Interface  USB 2.0.` — manual V1.2.1.1 "Operating Environment" table (local copy sha256 `7750996e…4cd3d4`) | PDF distributed via <https://support.einstar.com/support/solutions/articles/60000782576-einstar-software-download> |
| **Einstar** | recommends a "High-Speed、 USB 3.0、 Type-A" **extension cable** (not a claim about the scanner's link speed) | <https://forum.einstar.com/t/einstar-data-cable/929> |
| **Einstar 2** | `Interface  USB 3.0` | <https://docs.shining3d.com/exstar-hub/2.0.0/einstar2/en-us/pc-installation/> |
| **Einstar 2** | "⑥ USB Type-C Port — Connect the device to a computer using a Type-C cable" | <https://docs.shining3d.com/exstar-hub/2.0.0/einstar2/en-us/device/> |
| **Einstar Vega** | USB Type-C; battery + Wi-Fi (FCC label & internal photos) | <https://fccid.io/2AMG4-EINSTAR3/Label/Product-Label-and-Location-Host-7595632> |
| **EinScan Pro 2X V2** | "connect it to the computer through **USB 3.0 port**" | <https://docs.shining3d.com/einscanpro/latest/2xv2/en-us/device/> |
| **EinScan Pro 2X V2** | datasheet: `Connection  USB 3.0`; weight "1.13 KG (include the **USB3.0 cable**)" | <https://strategic3dsolutions.com/wp-content/uploads/2024/05/EinScan-Pro-2X-V2-S3DS.pdf> |

So: the Einstar is documented **USB 2.0 only**, while its successor and its more expensive sibling
are documented **USB 3.0**. The single "USB 3.0" statement for the Einstar is a support reply about
buying a longer **third-party** cable — it says the *extension cable* should be USB 3.0 Type-A, which
is over-specification advice, not a statement that the scanner links at 5 Gbps.

### 3.2 Is the conflict resolvable from public data?

Not fully. Two readings are possible and both fit the evidence:

1. **"USB 2.0 product" reading** — the scanner is a USB 2.0 device (manual, enumeration at 480 Mbps).
   The bulky 12 V/5 A supply is consistent with a design that spends its budget on illumination and
   compute rather than link speed; the FX3's SS pairs are simply not brought out.
2. **"USB 3.0-capable but shipped as 2.0" reading** — the FX3 firmware enables SS and the software
   warns when it sees USB 2.0, so the design *expects* a 5 Gbps link that the shipped cable/connector
   does not deliver (or delivers only on some units).

Public data cannot separate these. What is **CONFIRMED** is that SHINING 3D's own software treats
USB 2.0 as a degraded state (§6).

### 3.3 Cables and accessories sold

* The official store sitemap lists **23 product pages** (plus the site root); **none is a data cable**
  (markers, tripod, SpaceMouse, Pro Kit, software, refurbished units).
  Source: <https://www.einstar.com/sitemap.xml> (product sitemap).
* Resellers sell only the **power** cable as a spare (`Additive-X` SKU `SHGEINSCANPOWCAB`,
  <https://www.additive-x.com/shop/shining-3d-einstar-3d-scanner-power-cable.html>), not the data cable.
* **No USB 3.0 cable, no "USB 3.0 upgrade kit", no USB 3.0 variant of the Einstar is sold.**

### 3.4 What the newer products use

* **Einstar 2** — USB 3.0 over **Type-C**; also has a battery, Wi-Fi hotspot (`Einstar2_xxx`) and
  EXStar Hub software. <https://docs.shining3d.com/exstar-hub/1.1.0/einstar2/zh-cn/pc-match/>
* **Einstar Vega** — standalone with touchscreen, battery, Wi-Fi/BT module, USB-C (FCC filing above).
* **Rockit / Rigil** — separate FCC IDs (`2AMG4-ROCKIT`, `2AMG4-RIGIL`), wireless-capable; no public
  teardown.

None of these can be used to infer the Einstar's board: they are a different generation (SoC-based,
battery-powered, Wi-Fi) rather than an FX3 + FPGA acquisition board.

---

## 4. Sibling products (FX3 + Pango Logos architecture)

**Premise status:** the claim that EinScan SE, EinScan SP, EinScan Pro 2X and Einstar share an
**FX3 + Pango PGL25G** architecture is a *project-internal hypothesis*. **No public source documents
the silicon inside any EinScan/Einstar scanner.** It is treated here as a prior, not a fact.

What *is* public and relevant:

| Product | Public interface claim | Source |
|---|---|---|
| EinScan Pro 2X V2 | **USB 3.0** ("through USB 3.0 port"; datasheet ships a "USB3.0 cable") | docs + datasheet above |
| EinScan SE / SP V2 | datasheet requirement reads "1 × USB 2.0 or 3.0" (host-side requirement; ambiguous) | <https://machines-3d.com/images/Image/File/Fiches%20techniques/SCANNER%203D/%5BFiche%20Technique%5D%20Shining%203D_Einscan_SE.pdf> |
| Einstar | **USB 2.0** | §3.1 |

Consequences for the SS question:

* **PROBABLE:** SHINING 3D *does* route SuperSpeed when the product needs it — the Pro 2X is
  specified USB 3.0 and is sold with a cable SHINING 3D itself calls a "USB3.0 cable". So a
  USB 2.0-only Einstar is a **deliberate product decision**, not a company-wide limitation.
* **UNVERIFIED:** whether the Pro 2X actually trains at 5 Gbps, and whether its controller is an FX3.
  No teardown, no `lsusb` dump, no chip photo is public.
* **UNVERIFIED:** whether the Einstar's SS pairs reach the 7-pin connector on any variant.

**Any FX3-based scanner known to run at 5 Gbps?** No publicly *confirmed* one. The closest is the
Pro 2X, which is *specified* USB 3.0 (implying 5 Gbps) but whose controller is not publicly
identified.

Supporting silicon facts (for the architecture discussion, not for the Einstar specifically):

* **CYUSB3014 = EZ-USB FX3**, marketed as a "USB 5 Gbps peripheral controller"; its **GPIF II** can
  connect to "a processor, image sensor, **FPGA**, or ASIC".
  <https://www.infineon.com/products/universal-serial-bus/usb-3-2-peripheral-controllers/ez-usb-fx3-usb-5gbps-peripheral-controller>
* **Pango PGL25G** (Logos series, not Logos-2): 27,072 logic cells / 22,560 LUTs, 1,080 Kbit block
  RAM, 4 PLLs, max 308 user I/O, **DDR3 up to 800 Mbps**, family SERDES up to 6.375 Gbps.
  <https://www.pangomicro.com/product/logos_family/1797.html>
  A 22.5 kLUT device with ~1 Mbit BRAM and a DDR3-800 interface is exactly the class of part used as
  a camera/frame-buffer front end feeding an FX3 over GPIF II — consistent with the supplied photos
  (PGL25G + Micron D9PTK DDR3L + FX3).

---

## 5. FX3 designs that ship USB 2.0-only (prior calibration)

**Mechanism (standard USB 3.x link-training behaviour; PROBABLE for any given board):** the FX3 has
*both* a SuperSpeed PHY and a High-Speed PHY. USB 3.0 link training is autonomous: if the SS pairs are
not connected, not AC-coupled, or not terminated at the far end, the link simply never leaves USB 2.0
and the device enumerates at 480 Mbps. No firmware change is required to "be USB 2.0" — the same
firmware image runs either way. This is why a board can have SS enabled in firmware and still
enumerate at HS.

**Cost of doing SS properly (CONFIRMED, vendor app note):** Cypress/Infineon *AN70707 — EZ-USB FX3
Hardware Design Guidelines and Schematic Checklist* requires, for the SS pairs:

* 0.1 µF AC-coupling capacitors on `SS_TX`, with a plane cut-out under the pads,
* external low-capacitance ESD devices (e.g. SP3010-04UTG, ≤ 0.5 pF) because the FX3's internal
  protection on `SSRX±/SSTX±` is only ±2.2 kV HBM,
* impedance-controlled, length-matched routing,
* separate `U3TXVDD` / `U3RXVDD` supplies with ferrite beads.

Source: AN70707 (Cypress doc. 001-70707 Rev. \*G), mirror:
<https://svn.code.sf.net/p/jaer/code/devices/firmware/CypressFX3/docs/AN70707%20-%20EZ-USB%20FX3%20Hardware%20Design%20Guidelines%20and%20Schematic%20Checklist.pdf>

That BOM + layout cost is exactly what a cost-reduced, 12 V-powered, 480 Mbps scanner would skip.

**What such designs do with the SS pins — observed patterns (PROBABLE, from general practice):**

| Pattern | Consequence |
|---|---|
| Left **unconnected** (NC) on the package, no escape routing | cheapest; link always HS; no test access |
| Routed to **vias / test pads** only, no connector | allows bench probing; used when SS may be enabled later |
| Routed to a **connector that lacks SS contacts** | same as NC electrically; the "capability" is decorative |
| Fully routed with AC caps + ESD + SS contacts | true 5 Gbps |

**Named commercial FX3 products that are USB 2.0-only: UNVERIFIED.** No public teardown was found
that (a) identifies a CYUSB3014 and (b) documents a 480 Mbps-only link together with the SS pin
treatment. The nearest public artefact is an Infineon community thread literally titled
*"CYUSB3014 USB 2.0 only"* (<https://community.infineon.com/t5/USB-superspeed-peripherals/CYUSB3014-USB-2-0-only/td-p/41450>);
the thread body could not be retrieved (Cloudflare interstitial), so only its existence and title are
cited, not its content. This is a **gap in the public record**, not evidence either way.

**Calibration for the Einstar:** the prior probability that a 2022 consumer scanner uses an FX3 in
USB 2.0-only mode is **high** — it is a supported, zero-cost configuration, and the product's own
documentation says USB 2.0. The surprising part is only that the firmware *enables* SS (§6 shows the
host software expects it too).

---

## 6. Firmware / driver hints at product level

All items below are **product-level artefacts shipped by SHINING 3D** in the official EXStar
V1.2.2.0 installer (<https://support.einstar.com/support/solutions/articles/60000782576-einstar-software-download>).
No firmware was re-analysed; these are UI/driver strings.

### 6.1 The application warns when the link is USB 2.0 — CONFIRMED

`AppCommon_E7.dll` (sha256 `c09a53ea8a8379703a974ce2fe26d02f5d933a1b0d2b6bb0207fa1bde4525bd2`)
contains, as plain ASCII UI strings, adjacent to each other:

```
The current USB dirve is USB2.0
Notice: Please change to USB3.0
```

(the `dirve` typo is in the original binary). A parallel UTF-16 block in the same DLL holds the
localised versions, and a nearby table compares the literals `USB2.0` / `USB3.0` — i.e. the app
branches on the negotiated link speed and tells the user to move to a USB 3.0 port.

The same pair of strings appears in `Sn3DprocessManager.exe`.

### 6.2 The capture/calibration module carries a `USBProtocol` = `USB3.0` constant — PROBABLE

`Sn3DCalibCaptureImageProcess.dll` contains the symbol/string sequence:

```
... CaptureImageFinished ... addInfo . USBProtocol . USB3.0 ...
```

i.e. a per-capture field named `USBProtocol` whose literal value in this build is `USB3.0`.
Interpretation (PROBABLE): the capture path tags frames with the link protocol, and the shipped
default is USB 3.0.

### 6.3 The driver package is branded "U3" and binds the Einstar PID — CONFIRMED

`Resource/driver/UEC/SH3DU3DRIVER.inf` (sha256 `f1cc96289e681b1e3bd89d447bb1b0c32f40a7a4d742198586cb5443662a67a5`):

```
DiskName = "Shining3d U3 Scanner Installation Disk"
SHINING3D_E10 = "Einstar"        ; USB\VID_3267&PID_0003
SHINING3D_UE  = "FreeScan_UE"    ; USB\VID_3267&PID_0002
SHINING3D_FACE= "Facecan"        ; USB\VID_3267&PID_0004
```

So SHINING 3D's own driver for `3267:0003` is named **U3** (USB 3.0) and is shared across the
FreeScan/Einstar/Facecan family. A second INF (`Resource/driver/FX3/cyusb3.inf`) is the stock Cypress
`CYUSB3` driver and contains **no** `3267` VID entry — the Einstar does not bind to the generic
Cypress driver.

### 6.4 What is *not* found

* No INF text mentioning `SuperSpeed`, `5Gbps`, or a USB 3.0-specific device variant.
* No alternate "USB 3.0" product ID: the INF maps the Einstar (`E10`) to a single PID, `0003`, and the
  observed unit enumerates as `3267:0003` at 480 Mbps.
* No firmware/driver string that says the Einstar *requires* USB 3.0; the warning is advisory.

---

## 7. Photograph provenance

### 7.1 What the supplied photos show (read from the files, sha256 in `hardware/photos/`)

| File | sha256 (short) | Content |
|---|---|---|
| `P1_top_side.webp` | `0deceaa7…` | Top side: **Pango PGL25G** (package code `6IFBG484`, i.e. PGL25G-6I-FBG484; other line codes `UTSC2207`, `CMS34-B R1-68`, `FACC70B`), **CYUSB3014-BZX** (`CYP 625297`, `A 33 THA`, `1 2237`), **Micron D9PTK 2HK17** DDR3L, a 40-pin FPC (J4) at the left edge, FPGA BGA row-letter silkscreen `AUTRMNJHGFEDCBA` along the package edge |
| `P2_bottom_side.webp` | `4aa46b46…` | Bottom side: **Winbond W25Q64JVSIQ** (`25064JVSIQ 2310`), two QFN **buck regulators** with their inductors, 8-pin SOIC devices, labelled test pads **`CLK_PCLK_M`**, **`DP`**, **`DN`** (two pads each), J4 seen from below with pin numbers `40…21` / `20…1` |
| `P3_bottom_side_2.webp` | `709ba4d5…` | Same area, different angle/lighting (confirms `CLK_PCLK_M`, `DP`, `DN`, W25Q64JVSIQ) |
| `P4_bottom_side_3.webp` | `5e1e5cd3…` | Same area, third angle |
| `P5_connector_J4.webp` | `a50e4ddf…` | Close-up of **J4**: 40-pin FPC, contacts numbered 1–40, two mounting holes, gold fingers entering from below |

Notable: the **only** USB-related test access on the board is the `DP`/`DN` pair (USB 2.0).
There are **no** labelled `SSTX`/`SSRX` or `TX±`/`RX±` pads anywhere in the photographed area.
That is consistent with a USB 2.0-focused bring-up, but it is **not proof** that SS is unrouted —
SS pairs can leave through J4 without test pads.

### 7.2 Board identity — which board is this?

Observations that constrain the answer:

1. **No external cable connector is visible.** Neither photographed side shows the 7-pin push-pull
   connector, a USB receptacle, or a 12 V jack. (Caveat: the bottom edge of `P1` is partly cut off, so
   a small connector on that edge cannot be excluded from the photos alone.)
2. The **only** high-density interconnect shown is **J4**, a 40-pin FPC.
3. The board carries a complete USB device subsystem (FX3 + SPI boot flash) **and** a frame-processing
   subsystem (PGL25G + DDR3L + two bucks), plus sensor-side clocking (`CLK_PCLK_M` — a pixel clock).
4. Both bucks and the DDR3L are populated, i.e. the board is self-sufficient power-wise; it is not a
   passive adapter.
5. Project data (not public): the enumerated device descriptor declares `bmAttributes = 0x80`
   (bus-powered) with `MaxPower = 100 mA`, i.e. the FX3 reports itself as VBUS-powered even though the
   product ships a 12 V/5 A adapter — consistent with 12 V being used for the illumination/camera
   load rather than for the USB domain.

**Assessment:**

* **PROBABLE:** this is the **scanner's main acquisition ("head") board** — FX3 as USB device
  controller, PGL25G as camera/frame front-end with DDR3L frame buffer, W25Q64 as FX3 (and/or FPGA)
  configuration flash, J4 as the ribbon to the sensor/laser assembly and/or to a separate bottom I/O
  board that carries the 7-pin connector and the 12 V input.
* **RULED OUT:** it is **not** a bare "USB interface board" hosting the external connector — no such
  connector exists on it.
* **UNVERIFIED:** whether J4 carries the USB 2.0 (and possibly SS) pairs to the 7-pin connector, or
  whether the external connector sits on a separate board not shown in the photographs. Continuity
  between J4 pins and the 7-pin contacts would settle it.

### 7.3 Matching board photos in public sources

**None found.** No public photo matches this board (no `PGL25G`, `CYUSB3014-BZX`, `D9PTK`, `J4` or
`CLK_PCLK_M` appears in any indexed page). The FCC internal photos that do exist belong to the
Einstar Vega and show a visibly different board (`PCB008672/A030`, SoC + Wi-Fi module + battery +
Type-C), which does **not** match the supplied photos.

---

## 8. What would settle the open questions

| Open question | Decisive probe |
|---|---|
| Does the 7-pin cable carry SS? | Continuity test plug↔cable on all 7 contacts; look for 4 twisted pairs vs 2 |
| Does J4 carry SS? | Probe J4 pins for the FX3 `SSRX±/SSTX±` balls (or AC-coupling caps) during enumeration |
| Connector family | Photograph the plug/socket moulding marks; measure contact pitch; compare against Hirose LF / HR10, LEMO 0B, ODU, Weipu catalogs |
| Is the link ever 5 Gbps? | `lsusb -t` / sysfs `speed` on a USB 3 host **with a known-good USB 3 cable** if one can be mated; check FX3 `USB3_LNKFAIL` event counter |
| Sibling silicon | Teardown of an EinScan Pro 2X (documented USB 3.0) to see whether it is FX3 + Pango and how SS is routed |

---

## Appendix A — sources cited

**SHINING 3D (primary)**

* Einstar device page (cable/red dot/12 V 5 A): <https://docs.shining3d.com/exstar/1.0.0/en-us/device/>
* Einstar specs (`Interface USB 2.0`): <https://docs.shining3d.com/exstar/1.2.2/en-us/install/>
* Einstar manual PDF (V1.2.1.1): <https://support.einstar.com/support/solutions/articles/60000782576-einstar-software-download>
* Official forum, data-cable thread: <https://forum.einstar.com/t/einstar-data-cable/929>
* Einstar 2 specs (`Interface USB 3.0`): <https://docs.shining3d.com/exstar-hub/2.0.0/einstar2/en-us/pc-installation/>
* Einstar 2 ports (`USB Type-C Port`): <https://docs.shining3d.com/exstar-hub/2.0.0/einstar2/en-us/device/>
* Einstar 2 wired connection (Type-C): <https://docs.shining3d.com/exstar-hub/1.1.0/einstar2/zh-cn/pc-match/>
* EinScan Pro 2X V2 device page (`USB 3.0 port`): <https://docs.shining3d.com/einscanpro/latest/2xv2/en-us/device/>
* Official store product index: <https://www.einstar.com/sitemap.xml>

**Regulatory**

* SHINING 3D grantee list (28 IDs): <https://fccid.io/2AMG4>
* Einstar Vega filing: <https://fccid.io/2AMG4-EINSTAR3>
* Vega internal photos: <https://fccid.io/2AMG4-EINSTAR3/Internal-Photos/Internal-Photos-Host-7819373>, <https://fccid.io/2AMG4-EINSTAR3/Internal-Photos/Internal-Photos-Host-7595634>
* Vega label (`Model: Einstar Vega`): <https://fccid.io/2AMG4-EINSTAR3/Label/Product-Label-and-Location-Host-7595632>

**Silicon**

* Infineon EZ-USB FX3 (5 Gbps, GPIF II → FPGA): <https://www.infineon.com/products/universal-serial-bus/usb-3-2-peripheral-controllers/ez-usb-fx3-usb-5gbps-peripheral-controller>
* Cypress AN70707 (SS layout/BOM requirements): <https://svn.code.sf.net/p/jaer/code/devices/firmware/CypressFX3/docs/AN70707%20-%20EZ-USB%20FX3%20Hardware%20Design%20Guidelines%20and%20Schematic%20Checklist.pdf>
* Infineon community, "CYUSB3014 USB 2.0 only": <https://community.infineon.com/t5/USB-superspeed-peripherals/CYUSB3014-USB-2-0-only/td-p/41450> (title only; body behind Cloudflare)
* Pango Logos series / PGL25G: <https://www.pangomicro.com/product/logos_family/1797.html>, <https://www.pangomicro.com/product/logos_family/>

**Reseller**

* Einstar power cable spare: <https://www.additive-x.com/shop/shining-3d-einstar-3d-scanner-power-cable.html>
* EinScan Pro 2X V2 datasheet: <https://strategic3dsolutions.com/wp-content/uploads/2024/05/EinScan-Pro-2X-V2-S3DS.pdf>
* EinScan SE datasheet: <https://machines-3d.com/images/Image/File/Fiches%20techniques/SCANNER%203D/%5BFiche%20Technique%5D%20Shining%203D_Einscan_SE.pdf>

## Appendix B — local artefacts referenced

| Path | sha256 | Used for |
|---|---|---|
| `sources/web/extracted/EXStar_v1.2.2.0/AppCommon_E7.dll` | `c09a53ea…25bd2` | §6.1 USB2.0/USB3.0 UI strings |
| `sources/web/extracted/EXStar_v1.2.2.0/Sn3DCalibCaptureImageProcess.dll` | — | §6.2 `USBProtocol`/`USB3.0` |
| `sources/web/extracted/EXStar_v1.2.2.0/Resource/driver/UEC/SH3DU3DRIVER.inf` | `f1cc9628…a67a5` | §6.3 "U3" driver, `E10 = Einstar` |
| `sources/web/downloads/SHINING_3D_Einstar_User_Manual_V1.2.1.1.pdf` | `7750996e…cd3d4` | §3.1 `Interface USB 2.0` |
| `hardware/photos/P1–P5.webp` | see §7.1 | §7 board provenance |

Nothing proprietary is redistributed here; the FCC exhibits and vendor PDFs are public documents and
are cited by URL only.
