# Einstar / EinScan10_01 (SC130) FX3 firmware — functional map

Target: `analysis/firmware/EinScan10_EN.elf` (ELF32-ARM, ARM926EJ-S), rebuilt by
`tools/firmware/fx3img.py` from `EinScan10_01_SC130_FX3_V2.10_FPGA_V3.7_EN_IAP.img`.
Corpus: 518 Ghidra functions in `analysis/ghidra/FX3_FW/clean/*.c`.
All addresses are **load addresses** (the ones Ghidra used, i.e. real target addresses).

Status: **partial draft** — memory map, RTOS, USB, command protocol and the flash/update path are
proven from code; the sensor and FPGA sub-sections are still being tightened (§7, §8).

## 0. Confidence scheme used throughout

| label | meaning |
|---|---|
| **proven** | directly readable from a constant, a call, a descriptor byte or a literal-pool pointer at the cited address |
| **high** | follows from proven facts plus the documented Cypress FX3 SDK API shape; no contradicting evidence |
| **medium** | consistent with several independent observations but one step of inference (e.g. API name matched by argument pattern only) |
| **low / open** | stated as a hypothesis; listed again in §11 |

Nothing in this document is a recovered symbol. The image is stripped; `FUN_xxxxxxxx` names are
Ghidra's. Every "role" below is an **inferred** name proposed by this analysis.

---

## 1. Memory map

The boot image section table (verified in `docs/firmware/format.md` §3 and re-checked against the
ELF program headers) defines five load regions. Instruction plausibility was cross-checked against
the 518 Ghidra function extents, which is stronger evidence than entropy alone: a byte range is
"code" here iff at least one analysed function covers it.

| Range | Size | ELF phdr | Content | Evidence |
|---|---|---|---|---|
| `0x00000100`–`0x00002627` | `0x2528` | ph0 | **ITCM**: exception/entry stubs + early init code | 37 functions; region starts with `push {r4,lr}` at 0x100 (objdump), not an ARM vector table |
| `0x40003000`–`0x40012FFF` | `0x10000` | ph1 | **SYSMEM code** (section 2) + `.rodata` string pool | 156 functions; C-runtime start at 0x40003000; strings 0x4000D7B7–0x4000EFBF live inside this range |
| `0x40013000`–`0x40022FFF` | `0x10000` | ph2 | **SYSMEM code** (section 3) | 253 functions |
| `0x40023000`–`0x40029D33` | `0x6D34` | ph3 | **SYSMEM code** (section 4): ThreadX kernel + SDK helpers | 67 functions; section ends at 0x40029D33 |
| `0x40029D34`–`0x4002FFFF` | `0x62CC` | — | `0xFF` fill, **not loaded / unused** | every byte = 0xFF |
| `0x40030000`–`0x4003063F` | `0x640` | ph4 | **initialised data**: tables, version string, ThreadX banner | see below |
| `0x40030640`–`0x400354CF` | `0x4E90` | ph4 | **.bss** (zeroed at boot) | `entry` zeroes `0x40030640..0x400354D0` (0x40016F50); the C-runtime stub at 0x40003004-0x4000301C does the same with literals 0x40030640 / 0x400354D0 |
| `0x40000000`–`0x40002FFF` | `0xC000` | — | **not in the image**; low SYSMEM scratch used by the ROM/boot path and by ThreadX start-up | `entry` writes 0x40001FB4…0x40001FF4 before entering the kernel; `FUN_40014D50` (reboot) writes the entry address to `0x40000000` |

### 1.1 What is at 0x100 (ITCM)

Not a vector table. The first eight words are `push {r4,lr}; mov r0,#0x12; bl …; mov r0,#0x40; bl …;
pop {r4,pc}` etc. — ordinary functions that take an argument and call a common handler around
0x2618/0x2620. Six such stubs exist (0x100, 0x118, 0x130, 0x148, 0x178, 0x194); 0x148 reads
`[0x40032DDC+0x10]` and tail-branches through it when non-zero, i.e. a **hook dispatch**, and 0x194
pokes `0x40032DDC` and a `0xFFFFF0xx` system register. The ARM exception vectors themselves are
supplied by the FX3 ROM/boot loader, not by this image. *Confidence: high (objdump).*

### 1.2 Data at 0x40030000

| Address | Content |
|---|---|
| 0x40030000–0x400302C7 | structured tables: `00 00 80 80 22 22 55 55 1F 1F 7F 7F …` (a 2-bpp dither/level table), followed by 12-byte records `{u32 0x1E086001, u32 0x000302C4, u32 0x80000000}` and `{u32 0x4E080302, u32 0x00000300, …}` — plausibly GPIF/DMA or panel timing records. Not decoded. |
| 0x400302C8 | `"EinScan10_01_SC130_FX3_V0.7_FPGA_V2.6_CH"` — the **build's own** version string |
| 0x40030308 | `"Shining3D"` (manufacturer, also used as USB string) |
| 0x40030524 | `"Copyright (c) 1996-2008 Express Logic Inc. * ThreadX ARM9/RVDS Version G5.1.5.1 SN: 2923-115-1301 *"` — **RTOS identification** |
| 0x40030320 | `u16` "last command byte" (duplicate-command suppression, see §6) |
| 0x40030324 | **static** 3 × 12-byte EP2 command table `{u32 cmd; u32 requiredArg; u32 handler}` — see §4.2 |
| 0x40030348 | `u16` last EP2 command byte |
| 0x40030640… | .bss (all mutable state, all TCBs, all DMA channel objects) |

> Note the version discrepancy: the *runtime* descriptor string is rebuilt at 0x4000310C from
> `"EinScan10_01"+"SC130"+"_FX3_V"+"2.10"+"_FPGA_V"` (literal pool 0x40003354–0x40003368), while the
> static banner at 0x400302C8 says V0.7/V2.6. The V2.10 image therefore reports FX3 2.10 but still
> carries the older CH banner text. *Confidence: proven (both strings present).*

### 1.3 String pool

One contiguous `.rodata` block **inside section 2**, `0x4000D7B7`–`0x4000EFBF`
(≈ 6 KB, 130 format/message strings). It is reached only through ARM literal pools placed after
each function, which is why naive string search finds duplicates without owners — see §10 for the
attribution method.

### 1.4 ThreadX control blocks

ThreadX object control blocks are statically allocated in **.bss**, contiguous, `0xA8` bytes each
(the value `0xA8` is also the last argument of every `FUN_40023850` thread-create call).

| Object | Address | Proof |
|---|---|---|
| TCB "20:FPGA Boot Thread" | `0x400340D4` | `FUN_400057B0`: `FUN_40023850(&DAT_400340D4,"20:FPGA Boot Thread",…)`; next TCB at +0xA8 |
| TCB "21:comThread" | `0x4003417C` | same call, +0xA8 from the previous |
| TCB "21:ControlThread" | `0x40033E98` | same call |
| TCB "21:EMC_WDG_Thread" | `0x40035428` | same call; **0x40035428 + 0xA8 = 0x400354D0 = end of .bss** |
| TCB "01_DMA_THREAD" | `0x40032C54` | `FUN_40014EDC` |
| TCB "02_SYSTEM_THREAD" | `0x40032FCC` | `FUN_40018F28` |
| TCB "03_PIB_THREAD" | `0x40032EE8` | `FUN_40018F28` |
| TCB "04_UIB_THREAD" | `0x400334B8` | `FUN_4001C234` |
| TCB "05_LPP_THREAD" | `0x40032E18` | `FUN_40017894` |
| TCB "06_SIB_THREAD" | `0x400336F8` | `FUN_400214B0` |
| TCB "07_DEBUG_THREAD" | `0x40032940` | `FUN_40013CDC` |
| System Timer TCB | `0x400339F0` | `FUN_400223CC` = ThreadX `_tx_timer_initialize` |
| byte pool | `0x40030598/0x4003059C` | `FUN_40023850` walks `DAT_4003059C` entries of the pool at `0x40030598` |

**Stacks.** There is no static stack: `FUN_40006990(size)` allocates every thread stack from the
ThreadX byte pool at creation time (`0x1000` ×3 and `0x400` in `FUN_400057B0`, `0x400`/`0x800` for
the SDK threads). The supervisor/IRQ stack used before the scheduler starts is set up by the
ThreadX low-level init (`FUN_400148C0` / `FUN_40014828` / `FUN_40017550` sequence in `entry`), and
`entry` saves the incoming `r11` into `0x40001FF0`. The `0x40001Fxx` block is ThreadX's boot-time
control block and lives in the *un-loaded* low SYSMEM window (§1).

The `0x400-`prefixed names (`01_DMA_THREAD` … `07_DEBUG_THREAD`) are the **Cypress FX3 SDK's own**
threads; `0x40022xxx`–`0x40029xxx` is the ThreadX kernel + SDK runtime. ThreadX API functions can be
identified by their object-ID immediates (`TX_THREAD_ID`=0x54485244, `TX_QUEUE_ID`=0x51554555,
`TX_SEMAPHORE_ID`=0x53454D41, `TX_MUTEX_ID`=0x4D555445, `TX_BYTE_POOL_ID`=0x42595445,
`TX_TIMER_ID`=0x4154494D), e.g. `FUN_40027A20` (`tx_thread_create`) and `FUN_40023448`/`FUN_400234E8`
(queue) contain them. *Confidence: high.*

---

## 2. Boot chain and RTOS

```
FX3 ROM  →  boot image entry 0x40016F50  (entry())
               ├─ _DAT_fffff014 = 0xFFFFFFFF          ; CPU/ROM control
               ├─ FUN_40014DBC(0xD3)                  ; clock/PLL setup
               ├─ FUN_40018FF0(); FUN_40016E94()      ; SDK + heap init
               ├─ zero .bss  0x40030640..0x400354D0
               ├─ build ThreadX start-up blocks at 0x40001FB4 / 0x40001FC4
               ├─ FUN_400148C0(&DAT_40001FB4)         ; kernel enter, phase 1
               ├─ FUN_40014828(1,0,0)                 ; kernel enter, phase 2
               ├─ FUN_40017550(&DAT_40001FC4)         ; scheduler start
               └─ FUN_40021988(); (*0x40016F88)(0)    ; → tx_application_define path
```

`entry` is the boot-image terminator address, so this is `main()`. It never returns; the failure
branch spins forever. *Confidence: proven.*

RTOS: **ThreadX G5.1.5.1 (ARM9/RVDS)**, copyright string at `0x40030524`.

Thread creation chain (callers of callers):

```
0x4000691C  → FUN_40018F28  → creates 01..07 SDK threads
                              └─ 02_SYSTEM_THREAD entry FUN_400190C4
                                   └─ FUN_400057B0 → creates the four Einstar app threads
```

### 2.1 Application thread table (proven)

All four are created by `FUN_400057B0` through `FUN_40023850` (= SDK `CyU3PThreadCreate`, a wrapper
that zeroes a 0xA8-byte TCB and returns ThreadX status codes 0x03/0x05/0x0E/0x0F/0x10/0x18).
Stacks come from `FUN_40006990(size)` (ThreadX byte-pool allocate).

| # | Name string | TCB | Entry | Stack | Prio | Auto-start | Job |
|---|---|---|---|---|---|---|---|
| 1 | `20:FPGA Boot Thread` | 0x400340D4 | `FUN_400050CC` | 0x1000 | 9 | yes | one-shot: SPI init, tweak flash status register, load FPGA, then signal comThread+ControlThread via `FUN_40023BFC` and exit |
| 2 | `21:comThread` | 0x4003417C | `FUN_400051C8` | 0x1000 | 8 | no | init sensors + USB + version string + two software timers (1000/2000 ticks), enable USB connect, then loop on the **EP2 bulk** channel |
| 3 | `21:ControlThread` | 0x40033E98 | `FUN_400055E8` | 0x1000 | 8 | no | loop on the **EP1 interrupt** channel: get buffer → `FUN_40008D70` → send 50-byte reply |
| 4 | `21:EMC_WDG_Thread` | 0x40035428 | `FUN_40005424` | 0x400 | 8 | yes | 1 s supervisory loop: watchdog kick, watchdog-timeout latch, USB-transfer-error reboot, reboot request, USB liveness counter |

### 2.2 Thread bodies

**`20:FPGA Boot Thread` — `FUN_400050CC`** (proven, complete):
`FUN_40007A4C` (SPI init) → `FUN_400085E0` → `FUN_40004FA4` → `FUN_400033E0` →
log `flash_read_status` → `FUN_40008478(&st)` (read flash status register) → log
`before write SR:[…]` → `FUN_400083C4(1,0,0)`, `FUN_400083C4(0x31,2,0)`, `FUN_400083C4(0x11,0,0)`
(three status-register writes: SR1, SR2=0x31, SR3=0x11) → re-read and log `after write SR:[…]` →
`FUN_40004F18` → `FUN_400085E0` → `FUN_40008218` (**FPGA load**, §8) → `FUN_40021C04(500)` →
`FUN_40023BFC(&DAT_4003417C)` and `FUN_40023BFC(&DAT_40033E98)` (start comThread and ControlThread).

**`21:comThread` — `FUN_400051C8`**: `"Config cameras..."` → `FUN_400067C0` (SC130GS per-camera table
write), `FUN_400060A8` (gain mode / GPIO_LVDS_CAM), `FUN_4000606C` → `FUN_40004BEC` (whole USB stack)
→ `"dscr Init Successful"` → `FUN_40003510` (GPIF load + SM start) → `FUN_4000310C` (version string) →
`"EMC_WDG_ProgressTimer Successful1"` → `FUN_40023D04(&DAT_40030654,…1000…)` and
`FUN_40023D04(&DAT_400340A8,…2000…)` (two software timers) → `FUN_4000684C(2,2)` →
`FUN_40019130(1,1)` (USB connect) → `FUN_40021C04(3000)` → infinite loop (§6.2).

**`21:ControlThread` — `FUN_400055E8`**: infinite loop (§6.1).

**`21:EMC_WDG_Thread` — `FUN_40005424`**: infinite loop, every 1000 ms:
`FUN_40008B78()` (watchdog kick — toggles GPIO 0x26), then checks
1. `DAT_40030680` ≠ 0 → `"EMC WDG Timeout %d"` → **reboot**;
2. `DAT_40030681` ≠ 0 (set by the EP-event callback on a halt) → `"Detect USB Transfer Error!!! Reboot device"` → **reboot**;
3. `DAT_4003064C` = 1 (set after a successful firmware update) → **reboot**;
4. USB liveness: if `FUN_40020124()`==1 and powered and configured → counter := 0; else counter++.
   When counter > 9 → `"\r\nU_u USB Reconnect..."`, counter := 0, `FUN_40019130(0,0)` (disconnect),
   sleep 25, **reboot**.
Finally `FUN_40023BC8()` (tx_thread_relinquish).

---

## 3. USB device stack

### 3.1 Enumerated descriptors (proven — bytes dumped from the image)

| Descriptor | Address | Contents |
|---|---|---|
| Device (HS/FS) | `0x4000E9DC` | `bcdUSB=0x0200`, class `EF/02/01`, `bMaxPacketSize0=64`, **VID 0x3267, PID 0x0003**, bcdDevice 0x0000, iMan=1 iProd=2 iSer=0, 1 config |
| Device (SuperSpeed) | `0x4000E9F0` | `bcdUSB=0x0300`, same VID/PID, `bMaxPacketSize0=9` |
| Device qualifier | `0x4000EA04` | `0x0A,0x06`, bcdUSB 0x0200, class EF/02/01, mps0 64, 1 config |
| Other-speed config | `0x4000EA10` | 9 bytes, wTotalLength 9, 0 interfaces |
| Config (FS) | `0x4000EA1C` | 73 bytes, 1 interface, `bmAttributes=0x80`, 100 mA |
| Config (SS) | `0x4000EA80` | 103 bytes, 1 interface, same, 1024-byte packets, EP3 maxburst 15 |
| BOS | `0x4000EA68` | 22 bytes, 2 device-capability descriptors (USB 2.0 extension `0x10/7`, SuperSpeed `0x10/10`) |
| String 0 | `0x4000EAE8` | LANGID 0x0409 (en-US) |
| String 1 | `0x4000EAEC` | `"Shining3d"` (UTF-16LE) |
| String 2 | `0x4000EB00` | `"SHINING-CAM"` (UTF-16LE) |

Interface: `bInterfaceClass=0xEF, sub=0x02, proto=0x01`, 5 endpoints, followed by one 20-byte
class-specific descriptor `14 24 01 00 00 01 00 00 00 01 00 04 05 06 07 08 09 0A 0B 08`
(type 0x24, subtype 0x01). **Not identified** — it is not a UVC VC header (payload does not fit
bcdUVC/wTotalLength). Flagged in §11.

### 3.2 Endpoint and DMA-channel configuration (proven)

`FUN_400035C0` is the single configuration point. It is called from `FUN_40003E88` on
`USB SETCONF` and from `FUN_40003E44`.

| EP | Dir | Type | wMaxPacket (FS/SS) | SDK channel object | DMA type | buf size | count | prodSckId | consSckId | Used by |
|---|---|---|---|---|---|---|---|---|---|---|
| 0x01 | OUT | interrupt | 512 / 1024 | `0x40035224` | 3 (MANUAL_IN) | 0x200 | 4 | 0x0401 | 0x3F00 | ControlThread RX |
| 0x81 | IN | interrupt | 512 / 1024 | `0x4003537C` | 4 (MANUAL_OUT) | 0x200 | 4 | 0x3F01 | 0x0301 | ControlThread TX |
| 0x02 | OUT | bulk | 512 / 1024 | `0x40033FFC` | 3 (MANUAL_IN) | **0x1400** | 4 | 0x0402 | 0x3F00 | comThread RX |
| 0x82 | IN | bulk | 512 / 1024 | `0x40033F50` | 4 (MANUAL_OUT) | **0x1400** | 4 | 0x3F01 | 0x0302 | comThread TX |
| 0x83 | IN | bulk | 512 / 1024 (burst 15) | `0x400352D0` | 0 (AUTO) | 0xA020 | 4 | 0x0100 | 0x0303 | GPIF image stream |

Evidence: the `CyU3PEpConfig_t` built at `0x400035C0` sets `enable=1`, packet size `0x200`
(`CyU3PSetEpConfig(ep,&cfg)` = `FUN_40019524`), and each `CyU3PDmaChannelConfig_t` is filled with
`{size,count,prodSckId,consSckId}` immediately before `FUN_40011E00(&channel, type, &cfg)`
(`CyU3PDmaChannelCreate`), then `FUN_40013074(&channel,0)` (`CyU3PDmaChannelSetXfer`).

* `0x3F00/0x3F01` are the UIB (USB) socket pair; `0x03xx/0x04xx/0x0100` are internal (PIB/CPU)
  sockets. The channel↔endpoint pairing is **independently proven** by the CLEAR_FEATURE
  endpoint-reset table in `FUN_40004210` (§3.5).
* `0x1400` = 5120 bytes is the deliberate EP2 buffer size — it comfortably holds a 4096-byte
  firmware-update record plus its 9-byte header and checksum byte (§6.3).
* EP3's size field reads `0xA020` (40992) — atypical. Recorded as observed; the exact meaning in
  stream mode is **open** (§11).

### 3.3 USB stack initialisation — `FUN_40004BEC` (proven)

```
FUN_400182E0(1,&pibCfg)                       ; CyU3PPibInit (PIB clock, struct {2,0,0,3})
FUN_4001BD10()                                ; CyU3PUsbStart   → "CyU3PUsbStart failed to Start"
FUN_4001ED5C(FUN_40004210,1)                  ; CyU3PUsbRegisterSetupCallback(cb,isSetup)
FUN_4001ECF8(FUN_40003E88)                    ; CyU3PUsbRegisterEventCallback
FUN_4001ED4C(&LAB_40003DDC)                   ; LPM / link-power callback
FUN_400205E0(FUN_40004180,0xFFF,0x0E,0x0E)    ; per-EP event slots (EP1/2/3) + callback
FUN_4001B3A8(0,0,0x4000E9F0)                  ; CyU3PUsbSetDesc: SS device descriptor
FUN_4001B3A8(1,0,0x4000E9DC)                  ;   HS/FS device descriptor
FUN_4001B3A8(7,0,0x4000EA68)                  ;   BOS
FUN_4001B3A8(2,0,0x4000EA04)                  ;   device qualifier
FUN_4001B3A8(6,0,0x4000EA80)                  ;   SS configuration
FUN_4001B3A8(4,0,0x4000EA1C)                  ;   other-speed configuration
FUN_4001B3A8(3,0,0x4000EA10)                  ;   (FS) configuration
FUN_4001B3A8(5,0,0x4000EAE8/EAEC/EB00)        ;   strings 0,1,2
FUN_40019130(0,1)                             ; CyU3PUsbConnect  → "USB Connect failed"
```
Each call's error string is distinct and proven by literal-pool pointer (§9).

### 3.4 Event callback — `FUN_40003E88`

`switch(event)` with the FX3 event numbers; each arm logs a string:

| ev | string | action |
|---|---|---|
| 0 | `USB connected...` | `FUN_4001A41C` |
| 1 | `USB disconnected...` | clear `DAT_40030650` (link-up flag) |
| 2 | `USB SUSPEND...` | clear `DAT_40030643` |
| 4 | `USB reset...` | clear `DAT_40030643` |
| 5 | `USB SETCONF.` | set configured flag, `FUN_40003C24`, `FUN_4001A41C`, **`FUN_400035C0`** (EP+DMA config), `FUN_40003E04` |
| 6 | `USB Speed Change.` | log only |
| 8 | `latency Exit: …` | `FUN_40020480(4,buf)` dumps 6 LPM words |
| 0xB | `USB Powered` | `DAT_40030641=1`, sleep 100 |
| 0xC | `USB plug out` | `DAT_40030641=0`, **reboot** |
| 0x14 | `USB3_LNKFAIL event,USB plug out?` | disconnect+reconnect (`FUN_40019130(0,0)` then `(1,1)`) |

### 3.5 Setup-request handling — `FUN_40004210`

The callback receives the 8-byte setup packet packed into two u32 words and unpacks
`bmRequestType` (byte 0), `bRequest` (byte 1), `wValue` (bytes 2-3), `wIndex` (bytes 4-5),
`wLength` (bytes 6-7). It first prints `REQ 0x%x 0x%x 0x%x 0x%x 0x%x`.

Dispatch is on **bmRequestType**, not bRequest:

| bmRequestType | arm | behaviour |
|---|---|---|
| `0x00` standard/device | logs `Class Request Target Device` | — (handled by SDK) |
| `0x01` standard/interface | if `bRequest∈{1,3}` and `wValue==0`: `FUN_4001B920`/`FUN_40019E18` | SET_INTERFACE/CLEAR_FEATURE |
| `0x02` standard/endpoint | GET_STATUS / CLEAR_FEATURE / SET_FEATURE / SET_ADDRESS / GET_DESCRIPTOR / … / SYNCH_FRAME | logs the request name; **CLEAR_FEATURE drives the endpoint-reset paths** |
| `0x20` class/device | bRequest 1,2,3,9,0xA,0xB | see below |
| `0x21` class/interface | logs `Class Request Target Interface` | — |
| `0x22` class/endpoint | logs `Class Request Target Endpoint` | — |
| default (0x40/0xC0 vendor) | logs `Vendor Request` | **no vendor control-request handler exists in this firmware** |

`bmRequestType==0x20` sub-cases (proven):
* `bRequest=1` → `FUN_40006C68(buf,0,0x40)`, `FUN_40006D68(buf,&DAT_40033F48,0)`, `FUN_4001B064(0x40,buf)`
* `bRequest=2` → returns one byte, chosen by `wValue&0xff`: 2 → `DAT_400306C4`, 7 → `DAT_400306C5`
* `bRequest=3` → `FUN_40019E18()` (ack) + log `" set feature 03"`
* `bRequest=9` → `FUN_4001A140(0x40,buf,&len)` twice, then `FUN_40006D68(&DAT_40033F40,buf,0x40)`
* `bRequest=0xA/0xB` → ack and store `wValue>>8` into `DAT_400306C4/C5`

**There is no `bRequest 0xA0` vendor handler in this image.** The 9-byte command protocol the host
uses runs over the EP1/EP2 data endpoints (§6), not over control transfers. The only vendor-typed
control requests fall into the `default:` arm and are merely logged.

### 3.6 Endpoint reset paths (proven — this table also proves the channel mapping)

`CLEAR_FEATURE(ENDPOINT_HALT)` at `FUN_40004210`, `wIndex` = endpoint:

| wIndex | log | channel reset | extra |
|---|---|---|---|
| 0x01 | `Reset endpoint1 OUT` … `OK` | `0x40035224` | stall→unwind: `FUN_4001B570(ep,1)`, `FUN_4002119C(0x7D)`, `FUN_40012BB8(ch)`, `FUN_4001A084(ep)`, `FUN_4001AF18(ep)`, `FUN_40013074(ch,0)`, `FUN_4001B920(ep,0,1)`, `FUN_40019E18()`, `FUN_4001B570(ep,0)` |
| 0x81 | `Reset endpoint1 IN` … `OK` | `0x4003537C` | same |
| 0x02 | `Reset endpoint2 OUT` … `OK` | `0x40033FFC` | same |
| 0x82 | `Reset endpoint2 IN` … `OK` | `0x40033F50` | same |
| 0x83 | `Reset endpoint3 IN` … `OK` | `0x400352D0` | also `FUN_40017038(1)`, `FUN_40003510()` (GPIF restart), sets `DAT_40031780=1` |

### 3.7 "USB Transfer Error → Reboot"

`FUN_40004180` (EP-event callback) logs `EP Event CB epNum : %d evtype : %d` for every event except
evtype 1 and 4, and on `evtype == 0x100` logs `Halting EP epNum : %d` and sets `DAT_40030681 = 1`.
The watchdog thread sees that flag within 1 s and prints
`Detect USB Transfer Error!!! Reboot device`, then calls `FUN_40014D50(0)`.

`FUN_40014D50` is the **system reboot**: it pokes `0xE0050000`, then (for the non-zero argument form)
writes the firmware entry (`0x40016F50`) to `0x40000000` and triggers the reset. *Confidence: high.*

---

## 4. Command protocol (host ↔ FX3)

Two independent channels, both using the same header layout:

```
offset 0   1   2   3   4   5   6   7   8   9 ...
       [seq][?][ cmd_hi ][ cmd_lo ][ arg31..arg0 ][ payload ... ]
         u8   u8    u16 big-endian    u32 big-endian
```

* **EP1 (interrupt, 0x01 OUT / 0x81 IN)** — short control-style commands. `FUN_40008D70`.
* **EP2 (bulk, 0x02 OUT / 0x82 IN)** — command + data blocks (firmware update). `FUN_4000BE60`.

The response echoes byte 0, sets byte 1 = 2, bytes 2-3 = the command, byte 4 = status,
bytes 5-8 = 0, then payload from byte 9.

### 4.1 EP1 dispatcher — `FUN_40008D70` (4900 bytes, the largest function)

```
local_21 = hdr[0]                       ; sequence / command class
local_24 = (hdr[2]<<8)|hdr[3]           ; 16-bit opcode   (big-endian)
local_28 = BE32(hdr[4..7])              ; 32-bit argument (address / value / index)
```

Duplicate suppression: if `hdr[0]` equals `DAT_40030220` and is neither 0xFF nor 0xFE, the command is
dropped and 0 is returned; otherwise the byte is latched. (`DAT_40030320` in the data map.)

45 opcodes are dispatched. Every arm first calls `FUN_40008BB4(arg, want, len, out)` which is the
generic "fill status + length" helper (it writes status 0 and a 32-bit length field into the reply).

| opcode | action / handler | meaning (inferred) |
|---|---|---|
| 0x0000 | `FUN_40006D68(out+9,"Shining3D",0x12)` | read manufacturer string |
| 0x0001 | `FUN_40006D68(out+9,"EinScan10_01",0xD)` | read product string |
| 0x0002 | — | read 1 byte |
| 0x0004 | `_DAT_E0055014` bytes | chip/device ID from a system register |
| 0x0005 | version banner `EinScan10_01_SC130_FX3_V0.7_FPGA_V2.6_CH` | read version |
| 0x0007 | `FUN_40005C50(9)` → 14 status bits into out+9..+0x16; if bit17 set and pending → `FUN_40003E44` | read FPGA/status word + ack |
| 0x0009, 0x000B, 0x000D | `FUN_40005BAC` / `FUN_40005C50` | FPGA register write / read |
| 0x00BB | `FUN_40005C50` | FPGA register read |
| 0x00EF | `FUN_40005BAC(0xC,…)`, sleep 1000 | FPGA register 0x0C write |
| 0x1000 | read 0x67 bytes | bulk read of a 103-byte block |
| 0x1001 | read 2 bytes | small read |
| 0x1016 / 0x1017 / 0x1020 / 0x1022 / 0x1024 / 0x102E / 0x1036 / 0x1037 | read/`FUN_40005C50` | sensor/FPGA register reads |
| 0x1026, 0x1027 | `FUN_40006450`, `FUN_4000D36C`, `FUN_4000D748` | exposure/gain programming |
| 0x103E, 0x1040, 0x1042, 0x1048, 0x1050(→`FUN_40005AEC`), 0x1067, 0x106F, 0x1071 | `FUN_40005C50` (+`FUN_40005AEC` for 0x1050) | register/telemetry reads; 0x1050 = ADT7420 temperature |
| 0x104E | `FUN_40005C50` + log `LD work mode 0x%x` | laser/LD work-mode read |
| 0x106F | `FUN_40005C50` + log `Get Led Brightness 0x%x` | LED brightness read |
| 0x1062 | `FUN_4000684C` | GPIO/LED write |
| 0xCC00 | `FUN_40007EC8(0,0x1000)`, sleep, **`FUN_40014D50`** | flash erase + **reboot** |
| 0xCCCC | — | no-op / ping |
| others (`0x103F,0x1043,0x1049,0x1068,0x1073,0x1075,0x107B,0x00EF`) | `FUN_40005BAC` | FPGA/register writes |

*(Table generated by parsing the `if (local_24 == …)` arms of the decompiled body and listing the
callees inside each arm; the "meaning" column is inferred.)*

### 4.2 EP2 dispatcher — `FUN_4000BE60` (1060 bytes)

```
if (hdr[0] == DAT_40030348) { DAT_40030348 = 0xFFFF; return -1; }   ; duplicate suppression
DAT_40030348 = hdr[0];
reply[0]=hdr[0]; reply[1]=2; reply[2..3]=hdr[2..3] (LE copy); reply[5..8]=0;
if (DAT_40030648 == 0) {                    ; normal mode
    for (i=0;i<3;i++)
        if (BE16(hdr+2) == table[i].cmd) {          ; table at 0x40030324, stride 0x0C
            if (table[i].arg != 0 && BE32(hdr+4) != table[i].arg) { reply[4]=2; return 0x400; }
            reply[4]=3;
            if (table[i].handler) { n = table[i].handler(hdr, reply); ... reply[4]=0; return n; }
        } else reply[4]=1;
} else {                                     ; firmware-update mode
    return FUN_4000B320(hdr, reply);
}
return 0x400;                                ; fixed 1024-byte reply
```

The 3-entry table lives in **initialised data** at `0x40030324` (not .bss — I initially assumed
otherwise; the image bytes are non-zero). It decodes as `{u32 cmd; u32 requiredArg; u32 handler}`:

| row | addr | cmd | requiredArg | handler | role |
|---|---|---|---|---|---|
| 0 | 0x40030324 | `0x0006` | `5` | `FUN_4000B22C` | **start firmware update** ("type 6") |
| 1 | 0x40030330 | `0x1057` | `2` | `FUN_4000BB00` | read one 4 KB block from the data region |
| 2 | 0x4003033C | `0x1058` | `0` (wildcard) | `FUN_4000BC28` | erase + program one 4 KB block |

`requiredArg` is compared against the big-endian 32-bit argument field (`hdr+4`); a mismatch returns
reply status 2. This is why `FUN_4000BB00` and `FUN_4000BC28` have no direct caller — they are only
reached through this table.

**Data-region commands (proven).** `FUN_4000BB00` and `FUN_4000BC28` both take a 16-bit big-endian
block index from `hdr+8` and require `index < 0x100`; the flash address is
`(0x400 + index) * 0x1000` = **`0x400000 + index*0x1000`**. So there is a 1 MB read/write region at
flash `0x400000`–`0x4FFFFF`, used for bulk data (4 KB at a time) independently of the firmware image.
`FUN_4000BB00` reads 4096 bytes into `reply+9`; `FUN_4000BC28` erases the sector and programs 4096
bytes from `hdr+10`.

### 4.3 Firmware-update path (proven end-to-end)

1. **Enter update mode** — command `0x0006`, dispatched only when `hdr+4` == 5 (table row 0) —
   `FUN_4000B22C(hdr, reply)`:
   copies 4 payload bytes from `hdr+9`, byte-swaps them to little-endian → `DAT_40030644`
   (**total firmware size**), sets `DAT_40030648 = 1`, logs
   `Firmware size is %d, update starting...`.
2. **Every subsequent EP2 packet** goes to `FUN_4000B320`:
   ```
   len = BE32(hdr+4) - 2                     ; payload length
   if (len >= 0x1001) -> "Firmware update package size is 0x%x, TOO BIG" -> error
   chk = hdr[9 + len]                        ; 1 checksum byte after the payload
   ... first packet only:
       read flash 0x3F000, 0x100 -> app-flag struct at 0x4003278C
       log "Before pstAppFlag->uiNewAppAddr: 0x%x, pstAppFlag->uiOldAppAddr: 0x%x"
       if new==0x1C0000:  (isBootNew ? new=0x40000,old=0x1C0000 : new=0x1C0000,old=0x40000)
       if new==0x40000 :  (isBootNew ? new=0x1C0000,old=0x40000 : new=0x40000,old=0x1C0000)
       else               new=0x1C0000, old=0x40000
       log "After pstAppFlag->…"; ucIsBootNewApp = 1
   ... every packet:
       FUN_40007C90( (newApp>>12) + page, 0 )            ; erase 4 KB sector
       FUN_40007F64( (newApp>>8) + page*0x10, 0x1000, hdr+9, 0, 0 )   ; page-program 4096 B
       FUN_40007F64( (newApp>>8) + page*0x10, 0x1000, &DAT_40031788, 0, 1 ) ; read back
       sum = Σ readback bytes (8-bit)
       if (chk == sum) { page++; received += len; reply[4]=0;
                         if (received < total) return 0x400;
                         log "Firmware update over, firmware size %d, recive bytes %d"
                         erase sector 0x3F; write 12-byte app-flag at 0x3F000;
                         DAT_40030648=0; DAT_4003064C=1 (=> reboot); return 0x400; }
       else log "Firmware update package ucCheckByte(0x%x) != ucCheckCal(0x%x)"
   on any error: log "Firmware update error", reply[4]=2, clear mode + counters
   ```
3. **Reboot** — `DAT_4003064C = 1` is consumed by the EMC watchdog thread (§2.2), which reboots.

**The 4097-byte record is confirmed on the wire**: payload length must be `< 0x1001`, the checksum
byte sits at `9+len`, and it is the 8-bit sum of the payload — byte-for-byte the same rule as the
`.img` container in `docs/firmware/format.md` §2, and the same as the host code `FUN_18000bc80`.

### 4.4 Flash addressing — an important correction to `format.md`

`FUN_40007F64(addr>>8, size, buf, dev, mode)` internally does `local_c = param_1 << 8`, so its
**parameter is a flash address divided by 256**:

| call | effective flash address |
|---|---|
| `FUN_40007F64(0x3F0, 0x100, …)` (read app-flag) | **`0x3F000`** |
| `FUN_40007C90(0x3F, 0)` (erase before flag write) | `0x3F << 12` = **`0x3F000`** |
| `FUN_40007C90((newApp>>12)+page, 0)` | `newApp + page*0x1000` |
| `FUN_40007F64((newApp>>8)+page*0x10, 0x1000, …)` | `newApp + page*0x1000` |

`format.md` §7 states the app-flag block is at **0x3F0**. With the `<<8` scaling the block is at
**0x3F000**, i.e. the last 4 KB sector *below* application slot A (0x40000) — which is exactly where
a second-stage loader would keep its parameter sector. The two independent call sites (read 0x3F0 /
erase sector 0x3F / write 0x3F0) agree. *Confidence: proven for the arithmetic; the resulting map is
the natural reading.*

---

## 5. Sensor path — SC130GS over I2C

### 5.1 I2C primitives

| Function | Role | Evidence |
|---|---|---|
| `FUN_400108A0` | I2C write (`CyU3PI2cTransmitBytes`) | used by every register write |
| `FUN_400103D4` | I2C read (`CyU3PI2cReceiveBytes`) | used by every register read |
| `FUN_40008820(n)` | I2C bus pause/lock (called with 8, 5, 0) | wraps every transaction |
| `FUN_4000302C` / `FUN_40003048` | enter/exit a critical section around I2C | mutex at 0x400306A4 |

Preamble layout used everywhere: `{devAddr, reg_hi, reg_lo}` (3 bytes) or `{devAddr, reg}` (2 bytes),
so the sensors use a **16-bit register address**.

The bus is **multiplexed**, not multi-drop. `FUN_40008820(n)` is a 4-bit channel select that drives
four GPIOs through `FUN_4000FA8C`: GPIO 23 (`0x17`), 25 (`0x19`), 26 (`0x1A`) as the 3-bit channel
code and GPIO 27 (`0x1B`) as a bank/enable bit. Every I2C transaction is wrapped in
`FUN_40008820(ch)` … `FUN_40008820(0)`.

| Mux ch | Selected device | 8-bit I2C address | Evidence |
|---|---|---|---|
| 0 | none (idle) | — | all four GPIOs driven low |
| 1 | SC130GS camera 1 | `0x64` | `FUN_400067C0` / `FUN_400060A8` with `FUN_40008820(1)`, log `GPIO_LVDS_CAM1` |
| 2 | SC130GS camera 2 | `0x64` | same, `FUN_40008820(2)`, `GPIO_LVDS_CAM2` |
| 4 | SC130GS camera 4 | `0x64` | same, `FUN_40008820(4)`, `GPIO_LVDS_CAM4` |
| 5 | **FPGA register block** | `0x20` | `FUN_40005BAC` / `FUN_40005C50` call `FUN_40008820(5)` |
| 8 | ADT7420 temperature sensor | `0x90` write / `0x91` read | `FUN_40005AEC` calls `FUN_40008820(8)` |

Channel numbers 3, 6, 7, 9…15 are decoded by the same switch but unused by this firmware.

### 5.2 Register access

* `FUN_40005DF8(reg16, value)` — write one byte: preamble `{0x64, reg>>8, reg}`, 1 data byte,
  logs `Reg write: 0x%x` and on failure
  `WriteI2C_seneor_register_for_sc130 err, write data:0x%x 0x%x`. Retried once.
* `FUN_40005C50(reg)` — read 4 bytes big-endian from the **FPGA** (addr 0x20), returns u32.
* `FUN_40005BAC(reg, value)` — write 4 bytes to the **FPGA** (addr 0x20), logs
  `write data To FPGA Failed %d`.

### 5.3 Per-camera addressing (three cameras)

There is no separate I2C address per camera: all three sensors answer at `0x64` and are separated
by the **I2C mux** in §5.1. The firmware selects mux channel 1 / 2 / 4 before each table write and
labels them `GPIO_LVDS_CAM1`, `GPIO_LVDS_CAM2`, `GPIO_LVDS_CAM4`:

* `FUN_400067C0` — "sc130gs_write_table_for_sc130": for each camera it selects the GPIO then writes
  the register table; three distinct error strings carry the GPIO name
  (`… GPIO_LVDS_CAM1, ErrCode:0x%x`, `…CAM2…`, `…CAM4:%d`).
* `FUN_400060A8` — "SetSc132GainLinearMode": per-camera gain-mode setup, again with three strings
  `SetSc132GainLinearMode GPIO_LVDS_CAM1:%d` / `CAM2` / `CAM4`.
* `FUN_400066B0` — the plain `sc130gs_write_table` (no GPIO prefix).

So the camera index is the mux channel (1/2/4 — CAM3 is absent), and the same sensor register set is
written three times through `FUN_400066B0(camera, table)`:

```
FUN_40008820(camera);
for (i = 0; ; i++) {
    if (table[i].addr == 0x0001) break;             /* terminator */
    FUN_40005DF8(table[i].addr, table[i].val);      /* 16-bit reg, 8-bit value */
}
FUN_40008820(0);
```

Table format is 4-byte records `{u16 reg; u8 value; u8 delay_ms}` terminated by `reg == 0x0001`.
The table used for all three cameras is at **`0x4000E3CC`**; its first entries are
`0x0103=0x01` (software reset), `0x0100=0x00`, `0x3000=0x00`, `0x3001=0x00`, `0x3018=0x70`,
`0x3019=0x00`, `0x3022=0x19`, `0x302B=0x80` — the `0x30xx` bank is the SC130GS register page.

### 5.4 Exposure / gain

Strings `WriteAllSensorExptime failed ret %d`, `read seneor_register from 3e01/3e02/3e08/3e09
failed ret = %d`, `I2cReadSeneorReg 0x%x failed: 0x%x`, `sc130gs_write_table write seneor_register
addr:0x%x val:0x%x failed ret 0x%x` are present in the pool but **have no literal-pool reference**,
i.e. the functions that contain them were not recovered as separate Ghidra functions (or the code is
unreachable). The registers named (0x3E01/0x3E02/0x3E08/0x3E09) are the SC130GS exposure
(coarse/fine) and gain registers. Exposure programming is reached from opcodes 0x1026/0x1027 via
`FUN_40006450`, `FUN_4000D36C`, `FUN_4000D748`. *Confidence: medium — the opcode→function edge is
proven, the exact register semantics are inferred from the register numbers.*

---

## 6. FPGA path

Two independent interfaces:

1. **Bitstream download over SPI** — `FUN_40008218`, called once from the FPGA boot thread.
2. **Runtime register interface over I2C** — `FUN_40005BAC` / `FUN_40005C50`, slave address `0x20`,
   8-bit register index, 32-bit big-endian value. This is what opcodes 0x0007/0x0009/0x000B/0x000D/
   0x00BB/0x00EF/0x1016/0x1017/0x1020/0x1022/0x1024/0x102E/0x1036/0x103E/0x1040/0x1042/0x1048/0x104E/
   0x1067/0x106F/0x1071 read and write (LED brightness, LD work mode, status bits, …).

### 6.1 SPI flash primitives (proven)

| Function | SPI command | Meaning |
|---|---|---|
| `FUN_4001145C(cs)` | — | slave-select assert/deassert (devices 0 and 1) |
| `FUN_400114E4(buf,n)` | — | SPI transmit n bytes |
| `FUN_40010E34(buf,n)` | — | SPI receive n bytes |
| `FUN_40007B3C(dev)` | `0x06` WRITE_ENABLE then `0x05` READ_STATUS poll | `SPI WR_ENABLE command failed`, `SPI READ_STATUS command failed`, `SPI status read failed` |
| `FUN_40007C90(sector,dev)` | `0x06`, then `0x20` SECTOR_ERASE + 24-bit addr = `sector<<12` | 4 KB sector erase |
| `FUN_40007F64(addr>>8,size,buf,dev,rd)` | `0x02` PAGE_PROGRAM or `0x03` READ, 24-bit address MSB-first, 256-byte pages | `SPI access - addr: 0x%x, size: 0x%x, pages: 0x%x.` |
| `FUN_400083C4(sr,val,0/1)` | `0x06` + `0x01` WRSR | status-register write (SR1/SR2/SR3) |
| `FUN_40008478(&buf)` | `0x05` | status-register read |
| `FUN_400079F0(dev,level)` | — | CS wrapper used around every transaction |

`FUN_40007B3C` blocks until WIP clears (`Spi wait status failed, address: 0x%x failed`).

### 6.2 FPGA bitstream load — `FUN_40008218`

```
FUN_4000FA8C(0x33,0); sleep 10; FUN_4000FA8C(0x33,1); sleep 10      ; reset pulse on GPIO 0x33
base = FUN_4000C290()                                              ; FPGA slot base (0x80000/0x200000)
for (i = 0; i < 0x180; i++) {                                      ; 384 × 4 KB = 1.5 MB
    wait status; send {0x03, base>>16, base>>8, base}              ; SPI flash READ
    FUN_4000FA8C(0x33,0); FUN_40010E34(&DAT_40030778, 0x1000); FUN_4000FA8C(0x33,1);
    base += 0x1000;
}
```
Evidence: the loop bound `0x17F`/`0x180`, the 4-byte read header built as
`CONCAT13(base, CONCAT12(base>>8, CONCAT11(base>>16, 3)))`, the three SPI error strings owned by
this function, and the GPIO 0x33 toggling around each 4 KB transfer.

Bus parameters are set by `FUN_40007A4C` (`CyU3PSpiInit` + `CyU3PSpiSetConfig`): **30 MHz**
(`30 000 000`), 8-bit words, `FUN_400111C0` config, error strings `CyU3PSpiInit failed` /
`CyU3PSpiSetConfig failed`. GPIOs are initialised in `FUN_400085E0` (`CyU3PGpioInit` +
`CyU3PGpioSetSimpleConfig` for 0x24, 0x25, 0x33, 0x34, 0x17, 0x19, 0x1A, 0x1B); the last four are
exactly the I2C mux lines of §5.1, which independently corroborates that mapping.

**Direction is proven, delivery is not.** `FUN_400114E4` writes `0xE0000C10` and polls
`0xE0000C04 & 0x10` (TX-ready) ⇒ **transmit**. `FUN_40010E34` writes dummy `0` to `0xE0000C10` and
reads `0xE0000C14` (RX) ⇒ **receive**. So the loop really does read 1.5 MB *out of* the flash into
the .bss scratch buffer `0x40030778`, and no software path forwards it. GPIO 0x33 is driven low for
the data phase of each 4 KB chunk and high afterwards, with the flash CS held asserted throughout.

Leading hypothesis (inference, not proven): the FPGA is a **passive slave on the same SPI bus** —
GPIO 0x33 gates its configuration port, so the flash-read stream is clocked into the FPGA while the
FX3 discards it. The competing hypothesis (a "read the flash and throw it away" self-test) does not
explain GPIO 0x33's per-chunk toggling. Discriminating test: probe the SPI clock/data lines during
boot, or read the FPGA's `CONFIG_STATUS` through the I2C register interface (`FUN_40005C50`).

### 6.3 `GetFpgaLoadAddr` — `FUN_4000C290` (proven)

```
FUN_4000C7A0(&flag,0,0x100);
FUN_40007F64(0x3F0, 0x100, &flag, 0, 1);        ; read 256 bytes at flash 0x3F000
log "uiNewAppAddr: 0x%x, uiOldAppAddr: 0x%x, ucIsBootNewApp: 0x%x"
if (newApp == 0x1C0000) return isBootNew ? 0x200000 : 0x80000;
if (newApp == 0x40000)  return isBootNew ? 0x80000  : 0x200000;
return 0x80000;
```
So the **FPGA slot is chosen to mirror the application slot that will boot**, with a 0x180000
stride: app A 0x40000 ↔ FPGA A 0x80000, app B 0x1C0000 ↔ FPGA B 0x200000.
Logged as `GetFpgaLoadAddr: 0x%x`. *Confidence: proven.*

### 6.4 Flash layout (revised)

| Range | Content | Evidence |
|---|---|---|
| `0x000000`–`0x03EFFF` | second-stage loader / boot region (not in this image) | implied by the flag sector placement |
| `0x03F000`–`0x03F0FF` | **boot parameter block**: `{u32 uiNewAppAddr; u32 uiOldAppAddr; u8 ucIsBootNewApp; …}` | `FUN_4000B320`, `FUN_4000C290` |
| `0x040000`–`0x06E764` | application slot A (this FX3 image, 0x2E764 bytes) | `uiNewAppAddr = 0x40000` |
| `0x080000`–`0x17586C` | FPGA configuration slot A (0xF586C) | `GetFpgaLoadAddr → 0x80000` |
| `0x1C0000`–`0x1EE764` | application slot B | `uiOldAppAddr = 0x1C0000` |
| `0x200000`–`0x2F586C` | FPGA configuration slot B | `GetFpgaLoadAddr → 0x200000` |
| `0x400000`–`0x4FFFFF` | **bulk data region**, 256 × 4 KB blocks, read by cmd `0x1057`, erased+written by `0x1058` | `FUN_4000BB00`, `FUN_4000BC28` (`(0x400+index)*0x1000`, `index<0x100`) |

A/B rule (proven): the new image is written to the *inactive* slot; `ucIsBootNewApp` is set to 1 so
the next update flips back. `format.md` §7's `0x3F0` should be corrected to `0x3F000`; the rest of
that table is confirmed.

---

## 7. Error handling

The firmware has no error *codes* of its own on the wire beyond the reply status byte
(0 = OK, 1 = unknown command, 2 = bad argument / checksum error, 3 = dispatched). All diagnostics go
to `FUN_40014018(channel, fmt, …)` — channel 4 = debug console, 2 = a second (quieter) channel.

| Recovery behaviour | Trigger | Function |
|---|---|---|
| **reboot** | EP halted (`DAT_40030681`), watchdog timeout (`DAT_40030680`), update complete (`DAT_4003064C`), USB unplug event 0x0C, USB liveness counter > 9, opcode 0xCC00 | `FUN_40005424` → `FUN_40014D50` |
| **USB re-enumerate** | `USB3_LNKFAIL` event 0x14 | `FUN_40003E88` → `FUN_40019130(0,0)`/`(1,1)` |
| **endpoint reset** | CLEAR_FEATURE(HALT) | `FUN_40004210` (§3.6) |
| **retry** | I2C sensor read/write retried once; SPI status polled | `FUN_40005C50`, `FUN_40007B3C` |
| **abort command** | checksum mismatch, size > 0x1000, unknown opcode | `FUN_4000B320`, `FUN_4000BE60`, `FUN_40008D70` |
| **halt** | thread-create failure in `FUN_400057B0` | infinite `while(1)` |

Full string→function attribution: §9.

---

## 8. The ~45 most important functions

`conf`: P=proven, H=high, M=medium. "strings" are literal-pool-proven unless marked ✎ (text match only).

| # | Address | Inferred name / role | Evidence | conf |
|---|---|---|---|---|
| 1 | `0x40016F50` | `main` / boot-image entry | boot-image terminator; zeroes .bss; ThreadX start | P |
| 2 | `0x40023850` | thread-create entry point (`CyU3PThreadCreate` wrapper or ThreadX variant; both exist — cf. `FUN_40027A20`) | 11 args, last = 0xA8 (TCB size), ThreadX status codes 0x03/0x05/0x0E/0x0F/0x10/0x18 | H |
| 3 | `0x400057B0` | create the 4 Einstar threads | 4 × thread name strings | P |
| 4 | `0x400050CC` | `20:FPGA Boot Thread` | flash SR writes + `FUN_40008218` + start other threads | P |
| 5 | `0x400051C8` | `21:comThread` | init + EP2 loop; "Config cameras…", "dscr Init Successful" | P |
| 6 | `0x400055E8` | `21:ControlThread` | EP1 loop → `FUN_40008D70`, 50-byte reply | P |
| 7 | `0x40005424` | `21:EMC_WDG_Thread` | "EMC WDG Timeout", "Detect USB Transfer Error!!! Reboot device", "U_u USB Reconnect..." | P |
| 8 | `0x40008D70` | EP1 command dispatcher | 45 opcodes on BE16(hdr+2), BE32(hdr+4) | P |
| 9 | `0x4000BE60` | EP2 command dispatcher | 3-entry filter table 0x40030324, update-mode flag | P |
| 10 | `0x4000B22C` | enter firmware-update mode | "Firmware size is %d, update starting..." | P |
| 11 | `0x4000B320` | firmware-update record handler | 6 update strings; flash erase/program/verify; app-flag A/B flip | P |
| 12 | `0x4000C290` | `GetFpgaLoadAddr` | "GetFpgaLoadAddr: 0x%x", 0x40000/0x1C0000/0x80000/0x200000 | P |
| 13 | `0x400035C0` | USB endpoints + DMA channels | 8 DMA/EP error strings, `0x3F00/0x3F01`, `0x1400` | P |
| 14 | `0x40004BEC` | USB stack init + descriptors | 9 descriptor error strings, `FUN_4001B3A8` ×9 | P |
| 15 | `0x40004210` | USB setup-request handler | 32 request strings, endpoint-reset table | P |
| 16 | `0x40003E88` | USB event callback | 13 event strings | P |
| 17 | `0x40004180` | EP-event callback | "EP Event CB epNum : %d evtype : %d", sets halt flag | P |
| 18 | `0x400205E0` | per-EP event-slot programming | writes 0x40033420[0..0x1F], `FUN_40020148` per EP | M |
| 19 | `0x40003510` | GPIF load + SM start | "CyU3PGpifLoad failed", "CyU3PGpifSMStart failed" | P |
| 20 | `0x40003C24` | USB stop / disconnect | "STOP!!!!", "CyU3PSetEpConfig failed" | P |
| 21 | `0x40007F64` | SPI page program / read | "SPI access - addr…", cmd 0x02/0x03, addr>>8 param | P |
| 22 | `0x40007C90` | SPI 4 KB sector erase | cmd 0x20, addr = sector<<12 | P |
| 23 | `0x40007B3C` | SPI WREN + status poll | cmd 0x06/0x05, 3 strings | P |
| 24 | `0x40008218` | FPGA bitstream load loop | 0x180 × 0x1000, GPIO 0x33, 3 SPI strings | H |
| 25 | `0x400083C4` | SPI write status register | cmd 0x01, 2-byte SR | H |
| 26 | `0x40008478` | SPI read status register | cmd 0x05 | H |
| 27 | `0x40008BB4` | command reply/len helper | writes status + BE32 length | P |
| 28 | `0x40005BAC` | FPGA register write (I2C 0x20) | "write data To FPGA Failed %d" | P |
| 29 | `0x40005C50` | FPGA register read (I2C 0x20) | preamble {0x20,reg}, 4-byte BE | P |
| 30 | `0x40005DF8` | sensor register write (I2C 0x64) | "Reg write: 0x%x", 16-bit reg | P |
| 31 | `0x40005AEC` | read ADT7420 temperature | "Failed to ReadI2C_ADT7420", 0x90/0x91 | P |
| 32 | `0x400060A8` | SetSc132GainLinearMode | 3 × GPIO_LVDS_CAM strings | P |
| 33 | `0x400067C0` | SC130GS per-camera table write | 3 × "sc130gs_write_table_for_sc130 GPIO_LVDS_CAM…" | P |
| 34 | `0x400066B0` | SC130GS plain table write | "sc130gs_write_table …" ✎ | M |
| 35 | `0x4000310C` | build version string | "EinScan10_01"+"SC130"+"_FX3_V"+"2.10"+"_FPGA_V" | P |
| 36 | `0x40014018` | debug printf (UART) | all format strings, channel arg | H |
| 37 | `0x40008B78` | watchdog kick | toggles GPIO 0x26 each second | H |
| 38 | `0x40014D50` | system reboot | writes entry to 0x40000000, resets | H |
| 39 | `0x40003060` / `0x4000307C` | semaphore take / give (flash critical section) | `FUN_40023788`/`FUN_40023810` on 0x400306A4 (TX_SEMAPHORE_ID) | H |
| 40 | `0x40006990` | thread-stack allocator | byte-pool alloc, size rounded to 4 | H |
| 41 | `0x40011E00` | `CyU3PDmaChannelCreate` | (channel,type,cfg) + cfg layout | H |
| 42 | `0x40012884` / `0x40012534` / `0x40012EC0` / `0x40013074` | DMA get / discard / send / set-xfer | used symmetrically in both thread loops | H |
| 43 | `0x40019524` | `CyU3PSetEpConfig` | (ep, cfg) + error string | H |
| 44 | `0x4001B3A8` | `CyU3PUsbSetDesc` | (type,index,ptr) × 9 | H |
| 45 | `0x4001BD10` / `0x40019130` / `0x40019E18` | `CyU3PUsbStart` / `CyU3PUsbConnect` / `CyU3PUsbAckSetup` | error strings / ack pattern | H |
| 46 | `0x400223CC` | ThreadX `_tx_timer_initialize` | creates "System Timer Thread", TX_TIMER_ID | H |
| 47 | `0x40027A20` | `tx_thread_create` | 10 args, TX_THREAD_ID immediate | H |
| 48 | `0x40018F28` | SDK thread bootstrap (01…07) | 6 thread-create calls | P |
| 49 | `0x400190C4` | `02_SYSTEM_THREAD` entry | calls `FUN_400057B0` | P |
| 50 | `0x40014EDC` | `01_DMA_THREAD` creator | name string + queue create | P |
| 51 | `0x4000BB00` | EP2 cmd `0x1057`: read 4 KB flash block | table row 0x40030330; `(0x400+idx)*0x1000` | P |
| 52 | `0x4000BC28` | EP2 cmd `0x1058`: erase+program 4 KB block | table row 0x4003033C; erase then page-program | P |
| 53 | `0x40007A4C` | SPI init/config (30 MHz) | `CyU3PSpiInit` / `CyU3PSpiSetConfig` strings, 30000000 | P |
| 54 | `0x400085E0` | GPIO init (0x17/0x19/0x1A/0x1B/0x24/0x25/0x33/0x34) | `CyU3PGpioInit` + 8 × `FUN_40017588` | P |
| 55 | `0x400033E0` | UART init at 115200 (debug console) | `FUN_4000FD74` config with `0x1C200` | H |
| 56 | `0x40008820` | I2C mux / device select (ch 1,2,4,5,8) | 4 GPIO writes 0x17/0x19/0x1A/0x1B, 16-way switch | H |

---

## 9. String → function attribution

Method: every 4-byte word inside a function body (and the literal pool that follows it) whose value
equals the address of a NUL-terminated string in the loaded image is a **proven** reference. This is
necessary because the decompiler inlines the literal text, so a duplicated string (e.g.
`"I2C initialization failed!"` at both 0x4000E194 and 0x4000E588) cannot otherwise be attributed.
The reverse index (`string address → owning function`) is regenerated by
`tools/firmware/fx3_annotate.py`.

Functions with the most proven string references:
`FUN_40004210` (32, USB setup), `FUN_40003E88` (13, USB events), `FUN_40004BEC` (9, USB init),
`FUN_400035C0` (8, EP/DMA config), `FUN_40008D70` (7, LED/version), `FUN_4000310C` (6),
`FUN_400051C8` (6), `FUN_4000B320` (6, firmware update).

133 of the 269 printable strings have **no pointer reference at all** — they are either duplicated
literals the compiler merged, or dead strings from an earlier build. The most interesting dead block
is `0x4000E5FC`–`0x4000E9C0` (sensor/exposure messages, `WriteAllSensorExptime`, `…from 3e01/3e02/
3e08/3e09`, `I2cReadSeneorReg`, `PS_SWITCH SET failed`, `CyU3PDmaChannelReset %d`) plus the whole
`0x4000EDCC`–`0x4000EFC0` update block — **except** that `0x4000EDCC` *is* referenced from
`0x4000B31C`; the block is live. See §11 for what this implies about missing functions.

---

## 10. Corrections / additions to the existing docs

1. **`format.md` §7 flash map** — the app-flag block is at flash **`0x3F000`**, not `0x3F0`
   (`FUN_40007F64` scales its address argument by 256; `FUN_40007C90` erases sector 0x3F = 0x3F000).
   Everything else in that table (0x40000 / 0x1C0000 / 0x80000 / 0x200000) is confirmed by
   `GetFpgaLoadAddr` and by the update handler.
2. **`format.md` §4/§7** says the FPGA is programmed by the "20:FPGA Boot Thread" via SPI flash access
   — confirmed — but the FPGA also has a **runtime I2C register interface at slave 0x20**, which is
   how the LED brightness / LD work mode / status opcodes actually talk to it.
3. The FPGA payload's first non-0xFF byte at image offset 0x40190 maps to `slot + 0x40190`, i.e.
   `0x840190`/`0x2400190` — the 0x190-byte 0xFF prefix is *inside* the SPI-read window, so the
   loader reads it and discards it (the loop is blind to content).

---

## 11. Open questions

1. **FPGA bitstream delivery** (§6.2): the SPI transfer direction is proven to be *receive*, so the
   bitstream is read from flash into `0x40030778` and never copied anywhere by software. Whether the
   FPGA is a passive slave on the same bus (GPIO 0x33 as config gate) is the main open question.
2. **`requiredArg == 5` for command `0x0006`** — the update-start packet must carry the 32-bit value 5
   in `hdr+4`, yet `FUN_4000B22C` then reads a 4-byte firmware size from `hdr+9`. The 5 is not
   explained by the payload length arithmetic (`len = arg-2 = 3`). Possibly a protocol version tag.
3. **EP3 DMA size `0xA020`** — implausible as a byte count; need to check whether stream-mode
   channels reinterpret the field.
4. **The 20-byte class-specific descriptor** at 0x4000EA2E (`14 24 01 …`) is not UVC-shaped; its
   owner class is unknown.
5. **`bRequest 0xA0`** does not exist as a vendor control request in this firmware; if the host sends
   it, it is answered by the SDK default handler. Confirm against the host DLL's
   `CyU3PUsbControlTransfer` calls.
6. **GPIO numbers** for `GPIO_LVDS_CAM1/2/4` and for the FPGA `0x33` reset line are passed as
   register values; the pin→camera mapping is not yet decoded.
7. **Dead string block** 0x4000E5FC–0x4000E9C0 implies at least one function (the SC130GS
   exposure writer `WriteAllSensorExptime`) that Ghidra did not recover as a function. Worth a
   targeted re-analysis of that address range.

---

## 12. Reproduce

```bash
python3 tools/firmware/fx3_annotate.py --csv analysis/firmware/fx3_symbols.csv \
        --json analysis/firmware/callgraph.json
python3 tools/firmware/fx3_annotate.py --top 60            # markdown table
llvm-objdump -d --triple=armv5te-none-eabi --start-address=0x100 \
        --stop-address=0x4002a000 analysis/firmware/EinScan10_EN.elf
```
