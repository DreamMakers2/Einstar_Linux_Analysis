# USB 3.0 SuperSpeed capability of the original Einstar (EinScan10_01, 3267:0003)

Status: **investigated with the scanner connected, strictly read-only.** No firmware was flashed,
no device state was changed, no experimental or vendor-specific requests were sent. Only standard
GET_DESCRIPTOR queries and passive host inspection were used.

> **Follow-up, 2026-11-07 — the device-side question is now answered.** Mainboard photographs plus the
> official Infineon ball map settle §4/§7: the FX3 is a **CYUSB3014-BZXI** whose SuperSpeed balls
> **A3–A6** have no AC-coupling pair, no SS ESD array, no test pads and no four-conductor route to the
> connector, and the 7-pin connector has **one spare contact against four required**. SuperSpeed is
> blocked by the board, and a modification cannot fix it. Full analysis:
> [`docs/hardware/usb3_mainboard_feasibility.md`](../hardware/usb3_mainboard_feasibility.md).

**Answer in one paragraph.** The FX3 firmware is a complete, actively enabled SuperSpeed
implementation: it installs a USB 3.00 device descriptor, a BOS with the SuperSpeed capability,
and a 103-byte SS configuration whose bulk endpoints use 1024-byte packets and 16 KB bursts, and
it calls `CyU3PConnectState(connect, CyTrue)` on the real init path while programming the SS PHY
registers; it even handles a `USB3_LNKFAIL` event. The chip is an EZ-USB FX3, Infineon's
SuperSpeed family. Nevertheless the scanner negotiates **High Speed (480 Mbps)**, reports
`bcdUSB 2.00`, and stalls a BOS request. The host port is SuperSpeed-capable (it has an SS peer
port) and the supplied cable's PC end is a USB 3.0 Type-A plug (blue, 9 contacts). What cannot be
confirmed without opening the device is the scanner-side connector/PCB: the product uses a
proprietary keyed connector with 12 V/5 A power injection, the official manual states
"Interface: USB 2.0" and the official brochure "USB 2.0 or above", and the successor model moved
to USB-C. The evidence therefore points to a **USB 2.0 physical path (connector/PCB) with a
USB 3.0-capable controller and firmware**, i.e. SuperSpeed is blocked by hardware, not software.
Enabling it would need a hardware path that carries the SS pairs; if such a path exists on the
PCB, the firmware would train SS automatically with a suitable cable.

## 1. Observed link state (host side, this machine)

| Observation | Value | Evidence |
|---|---|---|
| Device | `3267:0003 Shining3d SHINING-CAM` | `lsusb` |
| Bus / port | Bus 001, port 5 → **HS root hub** (480 M) | `lsusb -t` |
| Negotiated speed | **480 Mbps (High Speed)** | sysfs `speed=480`, `lsusb -v` "Negotiated speed: High Speed" |
| `bcdUSB` | **2.00** | device descriptor |
| `bMaxPacketSize0` | 64 | device descriptor |
| Bulk `wMaxPacketSize` | 512 (HS) | endpoint descriptors |
| Device qualifier | `bcdUSB 2.00`, mps0 64, 1 config | GET_DESCRIPTOR(0x06) returned 10 bytes |
| BOS | **request stalls** (LIBUSB_ERROR_PIPE) | GET_DESCRIPTOR(0x0F), read-only |
| Host controller | Intel 700-series xHCI, USB 3.2 Gen 2x2 (20 Gb/s) | `lspci` |
| Port SuperSpeed peer | `usb1-port5` ↔ `usb2-port5` (peer exists, port is SS-capable) | sysfs |
| Other SS devices on the same controller | 10 Gb/s and 5 Gb/s mass storage enumerate normally | `lsusb -t` bus 002 |
| Kernel message | `usb 1-5: new high-speed USB device number 22 using xhci_hcd` | `journalctl -k` |
| `bInterval` repair | both interrupt endpoints `0 → 7` | kernel log (confirms earlier finding) |

At High Speed the FX3 serves its **USB 2.0 descriptor set**; a BOS request is not part of that
set (the host does not request BOS for `bcdUSB < 2.01`). The stall is therefore expected and is
not evidence against SS capability — but it also means the device never presents its SS identity.

Raw captures: `analysis/usb3/{lsusb_v,sysfs,kernel_log,control_queries}.txt`.

## 2. Firmware evidence — SuperSpeed is fully implemented and enabled

From the V2.10 EN firmware image (byte-verified, `docs/firmware/fx3_analysis.md` §3 and this
investigation):

| Descriptor | Runtime address | Contents |
|---|---|---|
| Device (HS/FS) | `0x4000E9DC` | `bcdUSB 0x0200`, class `EF/02/01`, mps0 64, VID/PID `3267:0003` |
| Device (SuperSpeed) | `0x4000E9F0` | **`bcdUSB 0x0300`**, same VID/PID, `bMaxPacketSize0 = 9` (512) |
| Device qualifier | `0x4000EA04` | 10 bytes, `bcdUSB 0x0200` |
| Other-speed config | `0x4000EA10` | 9 bytes, wTotalLength 9, 0 interfaces |
| Config (HS) | `0x4000EA1C` | 73 bytes, 5 endpoints, 512-byte packets |
| Config (SS) | `0x4000EA80` | **103 bytes**, 5 endpoints, **1024-byte packets**, one SS endpoint companion per endpoint |
| BOS | `0x4000EA68` | 22 bytes: USB 2.0 extension + **SuperSpeed capability (type 0x10/0x03, wSpeedsSupported 0x000E)** |
| Strings | `0x4000EAE8…` | LANGID, "Shining3d", "SHINING-CAM" |

The SS configuration is not boilerplate: it mirrors the vendor-specific `EF/02/01` interface and
the same five endpoints, and the image endpoint (`0x83`) has **`bMaxBurst = 15`** (16 KB per
burst) while all other endpoints use burst 0 — a deliberate high-throughput design for the
1.31 MB frames.

Installation and connect path (`FUN_40004BEC`, `FUN_40003E44`):

```
CyU3PUsbSetDesc(0, 0, 0x4000E9F0)   ; SS device descriptor
CyU3PUsbSetDesc(1, 0, 0x4000E9DC)   ; HS device descriptor
CyU3PUsbSetDesc(7, 0, 0x4000EA68)   ; BOS
CyU3PUsbSetDesc(2, 0, 0x4000EA04)   ; device qualifier
CyU3PUsbSetDesc(6, 0, 0x4000EA80)   ; SS configuration
CyU3PUsbSetDesc(4, 0, 0x4000EA1C)   ; other-speed configuration
CyU3PUsbSetDesc(3, 0, 0x4000EA10)   ; FS configuration
CyU3PUsbSetDesc(5, 0..2, strings)
...
CyU3PConnectState(0, 1)             ; init: stay disconnected, keep SS enabled
... later, on the application path:
CyU3PConnectState(1, 1)             ; connect WITH SuperSpeed enabled
```

`CyU3PConnectState(connect, enableSuperSpeed)` was confirmed from the decompiled body: when the
second argument is non-zero the function programs the **USB 3.0 SS PHY/port registers**
(`0xE0033004/08`, `0xE0033404/08`, `0xE0033050/3C`); when it is zero it configures the USB 2.0
PHY (`0xE0032074`, `0xE0031408/98`, `0xE003180C`). Every call site was enumerated:

| Call | Site | Meaning |
|---|---|---|
| `(1,1)` | `0x40003E44` (application init, called from `0x40008D70`) | **connect with SS enabled** |
| `(1,1)` | `0x400051C8` (comThread) | connect with SS enabled |
| `(1,1)` | `0x40003E88` after a link failure | reconnect with SS enabled |
| `(0,1)` | `0x40004BEC` (descriptor setup) | stay disconnected, SS enabled for the later connect |
| `(0,0)` | `0x40003E88`, `0x40005424` | disconnect |

There is **no `(1,0)` call anywhere**: the firmware never connects with SuperSpeed disabled.

The USB event handler also handles event `0x14`, logged as
**`USB3_LNKFAIL event,USB plug out?`**, and reconnects — the firmware explicitly expects and
recovers from SuperSpeed link failures.

## 3. Controller capability

The EZ-USB FX3 is Infineon's SuperSpeed USB 3.0/3.2 Gen 1 controller family. The firmware's SS
PHY programming, SS descriptors, BOS and `USB3_LNKFAIL` handling all target that capability.
The **exact variant** (CYUSB3011/3012/3013/3014) cannot be recovered from the firmware image: the
code, clock and memory footprint are compatible with several packages, and the image does not
read a chip-ID register. A teardown photo or the chip marking is required to name the part; this
does not affect the conclusion: the FX3 line is Infineon's SuperSpeed family, and the firmware is
demonstrably driving the SS PHY block that this code targets.

## 4. Cable and connector

| Item | Evidence | Assessment |
|---|---|---|
| PC-end plug | Supplied cable has a **USB 3.0 Type-A plug (blue insert, 9 contacts)** (user inspection) | The cable is of USB 3.0 construction; it *can* carry SS pairs |
| Scanner-end connector | Proprietary keyed push-pull connector with a **red alignment dot**, shown in the manual; 12 V/5 A power is injected into the cable at a breakout | Not a standard USB-C or USB 3.0 Micro-B; pinout undocumented |
| Manual | "Plug USB cable into the bottom of Scanner"; "Interface: **USB 2.0**" | Vendor documents a USB 2.0 interface |
| Official brochure | "Interface: **USB 2.0 or above**" | Same |
| Successor model (Einstar 2) | Uses a **Type-C** cable | The platform later moved to a standard connector |

A blue USB 3.0 A-plug does **not** prove that the SS pairs are populated through to the
proprietary device end; OEM cables frequently use a USB 3.0 connector for mechanical or
procurement reasons while wiring only D+/D−. The scanner-side pinout is the missing fact.

## 5. Feasibility matrix

| Layer | Capable? | Evidence | Confidence |
|---|---|---|---|
| **Firmware support** | **Yes — enabled** | SS device descriptor, BOS, SS config with 16 KB bursts, `CyU3PConnectState(1,1)`, SS PHY programming, `USB3_LNKFAIL` handler; no `(1,0)` path | **High** |
| **Chip capability** | **Yes** | EZ-USB FX3 SuperSpeed family; SS PHY register block is driven by the firmware | **High** (exact variant not read) |
| **PCB SS routing** | **Unknown, probably absent** | Product is specified USB 2.0; proprietary connector; no SS link on an SS-capable port | **Low confidence either way** |
| **Cable** | **Probably capable** | USB 3.0 Type-A plug, 9 contacts (user) | **Medium** (device-end wiring unverified) |
| **Host port** | **Yes** | Intel xHCI 20 Gb/s; port 5 has an SS peer; other SS devices work | **High** |
| **Currently negotiated speed** | **High Speed 480 Mbps** | sysfs, `lsusb -v`, kernel log | **Certain** |

**What prevents SuperSpeed today** (ranked):

1. **The device-side physical path does not present SS terminations** (connector and/or PCB not
   wired for SS). This is the only remaining explanation consistent with all observations,
   because firmware, controller, host port and (probably) the cable are all SS-capable.
2. The supplied cable may carry only USB 2.0 through its proprietary end even though its PC plug
   is USB 3.0.
3. Nothing in software disables SS; there is no firmware configuration switch to flip.

## 6. Realistic performance benefit

The current link is the binding constraint for the documented scan cadence:

```
frame on the wire     = 1,311,872 B   (1,310,720 payload + 32 × 36 B framing)
HS practical ceiling  ≈ 40 MB/s  →  30.5 frames/s aggregate (10.2 fps per camera)
68 ms cadence, 3 cams = 44.1 frames/s aggregate (14.7 fps per camera) = 57.9 MB/s needed
deficit               ≈ 1.45×
SS (5 Gb/s, ~400 MB/s practical) → cadence-limited 44.1 fps, no bandwidth wall
```

So SuperSpeed would raise the achievable aggregate frame rate by up to **~45 %** at the documented
cadence and remove the bandwidth pressure entirely (it would allow ~300 fps aggregate if the
sensors could produce it). It also explains a field symptom: the vendor session log contains
**115 `grabImage failed. errorCode: 1034`** timeouts over ~1.5 h — what a 58 MB/s workload looks
like on a 40 MB/s link.

If the device is truly HS-only, the vendor stack must be dropping or throttling frames; at HS the
backend should therefore treat the cadence as best-effort and expect the same timeouts.

## 7. Additional information required (and proposed tests, not performed)

| # | Test | What it decides | Invasive? |
|---|---|---|---|
| T1 | Inspect the scanner-side connector contacts (count, markings) and, if accessible, the cable's device-end pinout with a multimeter | Whether SS pairs exist at the device end and in the cable | No |
| T2 | Open the scanner / obtain internal photos (FCC or teardown) | Exact FX3 variant and whether SSTX/SSRX are routed to the connector | **Yes — not performed** |
| T3 | If a second, known USB 3.0 cable with the same proprietary connector exists, try it | Whether the supplied cable is the limiter | No |
| T4 | Ask SHINING 3D whether the Einstar is USB 2.0-only and whether a USB 3.0 variant exists | Authoritative product answer | No |
| T5 | Measure SS termination on the device connector with a VNA/TDR | PCB routing | **Yes — lab equipment** |

Note that **if** the PCB and connector do carry SS, the existing firmware would train SS
automatically (the connect call already enables it) — no firmware change would be needed. That is
the one scenario in which a cable swap alone would unlock USB 3.0. All evidence available today
makes that scenario unlikely but not impossible.

## 8. Limitations

* The scanner-side connector and PCB were not inspected (read-only investigation; the device was
  not opened).
* The blue 9-contact USB-A plug is the user's observation; cable continuity to the proprietary
  end was not measured.
* No streaming test was run (it would change device state), so the throughput figures are
  arithmetic from the link speed and frame size, not a live measurement.
* The exact FX3 variant is unknown; the SS capability claim rests on the family and on the
  firmware driving the SS PHY.

## 9. Reproduce

```bash
lsusb -d 3267:0003 ; lsusb -t
cat /sys/bus/usb/devices/1-5/{speed,version,rx_lanes,tx_lanes}
readlink -f /sys/bus/usb/devices/usb1/1-0:1.0/usb1-port5/peer
journalctl -k | grep -i "usb 1-5"
# read-only BOS/qualifier probe: analysis/usb3/bosquery.c (libusb)
```
