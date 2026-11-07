# FX3 firmware evolution: V1.0/V2.6 → V2.10/V3.7

Auto-generated data: `docs/firmware/compare_V1.0_to_V2.10.md`,
`analysis/firmware/compare_V1.0_V2.10.json`
(tool: `tools/firmware/compare_fw.py`).

## Inputs

| | V1.0 / FPGA V2.6 | V2.10 / FPGA V3.7 |
|---|---|---|
| file | `EinScan10_01_SC130_FX3_V1.0_FPGA_V2.6_CH.img` | `EinScan10_01_SC130_FX3_V2.10_FPGA_V3.7_EN_IAP.img` |
| provenance | EXStar 1.0.6.0 `fabu_UPDATE/writeBootLoader/` | factory image (also in 1.0.6.0/1.2.2.0) |
| container | raw FX3 image (no 4097-byte layer) | 4097-byte record container |
| FX3 image size | 1,310,720 | 1,310,720 |
| entry | `0x40015488` | `0x40016F50` |
| image checksum | `0x91AEFEAA` | `0x0FE314BC` |
| FPGA payload | 1,005,676 B @ `0x40190`, `1b994d4d…` | 1,005,676 B @ `0x40190`, `cca7860b…` |

Section sizes changed (this is a recompile/relink, not a patch):

| load address | V1.0/V2.6 | V2.10/V3.7 | delta |
|---|---|---|---|
| `0x00000100` | 8,992 | 9,512 | +520 |
| `0x40003000` | 65,536 | 65,536 | 0 |
| `0x40013000` | 65,536 | 65,536 | 0 |
| `0x40023000` | 17,280 | 27,956 | +10,676 |
| `0x40030000` | 17,032 | 21,712 | +4,680 |

Because code was recompiled, raw byte diffs are not attributable per function. The
layout-independent signal is the **string-set difference**: V1.0 has 131 strings, V2.10 has
167 (55 added, 19 removed).

## What V2.10 actually adds (evidence: added strings and their contexts)

### 1. A native in-firmware update path (the big one)

New strings in V2.10:

```
Firmware size is %d, update starting...
Firmware update over, firmware size %d, recive bytes %d
Firmware update package size is 0x%x, TOO BIG
Firmware update package ucCheckByte(0x%x) != ucCheckCal(0x%x)
Firmware update error
```

`ucCheckByte != ucCheckCal` is exactly the 4097-byte record checksum verification described in
`docs/firmware/format.md`: the device recomputes the 8-bit sum of each 4096-byte package and
compares it with the byte the host appended. V1.0 instead used a host-driven, read-back-verified
scheme, visible in the *removed* strings:

```
DATA CHECK RIGHT
Data Check Error and Upgrade! Failed!
Read back error check beyond 4 times and update firmware failed.
FirmwareSize = %d
DATA update over
```

**Conclusion:** V2.10 moved firmware programming *into* the FX3 firmware with an
integrity-checked package protocol, replacing an older read-back/retry scheme. This is a real
functional and robustness improvement, and it is the protocol libeinstar/vendor tools must
implement.

### 2. Much broader USB setup-request handling

Added: `GET_DESCRIPTOR`, `SET_DESCRIPTOR`, `GET_CONFIGURATION`, `SET_CONFIGURATION`,
`GET_INTERFACE`, `SET_INTERFACE`, `GET_STATUS`, `CLEAR_FEATURE`, `SET_FEATURE`, `SET_ADDRESS`,
`SET_SEL`, `SET_ISOCH_DELAY`, `SYNCH_FRAME`, `Class Request Target Device/Interface/Endpoint`,
`REQ 0x%x 0x%x 0x%x 0x%x 0x%x`.

This is a standard-conformant request table, consistent with the device now being a composite
(EF/02/01) USB 2.0/3.0 device that must answer the full request set from Windows/macOS stacks.

### 3. Endpoint reset and DMA error recovery

Added: `Reset endpoint1 IN/OUT OK`, `Reset endpoint2 IN/OUT OK`, `Reset endpoint3 IN OK`,
`Bulk DMA Discard failed!`, `Bulk DMA send failed!`, `Control DMA Discard failed!`,
`Control DMA send failed!`, `CyU3PDmaChannelReset %d`.

V2.10 can recover individual pipes and DMA channels instead of resetting the whole device —
relevant because the image stream is the failure-prone path (see the 117 timeouts in the real
session log).

### 4. A/B application-slot boot logic

Added: `Before/After pstAppFlag->uiNewAppAddr: 0x%x, pstAppFlag->uiOldAppAddr: 0x%x`,
`GetFpgaLoadAddr: 0x%x`.

This matches the decompiled `GetFpgaLoadAddr` (`FUN_4000c290`) and slot toggling
(`FUN_4000b320`): V2.10 implements the power-fail-safe A/B app + FPGA slots at
`0x40000`/`0x80000` and `0x1C0000`/`0x200000`.

## What did *not* change

* The FPGA payload size is identical (1,005,676 B at the same image offset), consistent with the
  same FPGA device; the content differs (526,936 bytes differ), i.e. a genuine V2.6 → V3.7
  bitstream change.
* Frame format, endpoint set and command opcodes are unchanged between the two (the host code
  that speaks to both is identical).

## Practical implications for the Linux project

1. Target V2.10's in-firmware update protocol (`system op 0x06` + 4097-byte records); the
   V1.0 protocol is obsolete and its recovery image should only be used with the Cypress boot
   app path.
2. Expect and handle endpoint stalls/halts: V2.10 added explicit reset commands, and the
   vendor log shows timeouts in normal operation. The new backend must implement
   `clear_halt` + resubmit, not device reset.
3. There is no newer firmware to target. The "newer is better" question is settled for this
   product: V2.10 *is* the improved release, and it is what every unit runs.

## Caveat

String evidence proves capability presence, not implementation quality. The `ucCheckByte`
verification is proven by the string plus the host-side implementation (`FUN_18000bc80`)
and the container's verified 320/320 checksums. The *exact* flash write sequence in V2.10 is
decompiled in `analysis/ghidra/FX3_FW/` and mapped in `docs/firmware/fx3_analysis.md`.
