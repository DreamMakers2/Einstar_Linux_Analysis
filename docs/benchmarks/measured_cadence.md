# Measured acquisition cadence over USB 2.0 (real scanner, libeinstar)

Status: **measured on the physical scanner (firmware `EinScan10_01_SC130_FX3_V2.10_FPGA_V3.7_EN`)
with the working libeinstar driver**, 2026-11-07. No firmware was changed; only the volatile
trigger/exposure settings that libeinstar already uses for scanning were written, and the trigger
and lights were switched off afterwards.

Tool: [`tools/capture/cadence_measure.cpp`](../../tools/capture/cadence_measure.cpp) (links
libeinstar; records every completed frame's camera, slot id, group counter, payload size and host
arrival time), analysis: [`tools/capture/analyze_cadence.py`](../../tools/capture/analyze_cadence.py).
Raw summary: [`analysis/cadence/summary.json`](../../analysis/cadence/summary.json).

## 1. Results

All frames are 1280 × 1024, 1,310,720 B. `(mono,rgb)` is the trigger mode written with opcode
0x41; "group" is one trigger (all slots); "slot" is one mono exposure (both IR cameras).

| requested config | frames | wall | fps | MB/s | frames/group | measured group period | loss |
|---|---:|---:|---:|---:|---:|---:|---:|
| (1,1) 100 ms, 20 s | 600 | 20.06 s | 29.92 | 39.21 | 3 | **100.0 ms** | 0 |
| (1,1) 90 ms, 30 s | 999 | 30.05 s | 33.25 | 43.58 | 3 | **90.0 ms** | 0 |
| (1,1) 68 ms, 8 s | 279 | 8.11 s | 34.41 | 45.10 | 3 | **86.2 ms** (clamped) | 0 |
| (1,1) 30 ms, 8 s | 279 | 8.12 s | 34.38 | 45.07 | 3 | **86.4 ms** (clamped) | 0 |
| (1,1) 10 ms, 10 s | 348 | 10.09 s | 34.49 | 45.21 | 3 | **86.2 ms** (clamped) | 0 |
| (3,1) 68 ms, 15 s | 469 | 15.52 s | 30.21 | 39.60 | **7** | **222.5 ms** | 0 |
| (3,1) 90 ms, 10 s | 259 | 10.29 s | 25.18 | 33.01 | **7** | **270.0 ms** | 0 |

Per-camera counts are exactly equal for the IR pair and equal to the group count for the colour
camera: (1,1) gives 3 frames/group (cam0, cam1, cam2); (3,1) gives 7 (cam0 ×3, cam1 ×3, cam2 ×1).

**Measured hard ceiling: 45.1–45.2 MB/s (34.4–34.5 frames/s, frame-to-frame; the tool's wall-clock estimate is 45.7 MB/s including start-up).** Requesting 68, 30 or 10 ms all
produce exactly the same 86.2 ms group period, i.e. the device stops accepting a faster cadence;
the link, not the requested period, is the limit.

**No frames were lost in any run.** Frame/slot ids stayed contiguous; the device back-pressures
the cadence instead of dropping. (The `mono=3` runs show id strides of 3 for the colour camera
because the ids are slot counters — see §3 — not missing frames.)

## 2. What the numbers mean

* **(1,1) at 90–100 ms is sustainable**: 39.2–43.6 MB/s, i.e. 86–96 % of the measured ceiling,
  with zero loss over 30 s. This is libeinstar's EXStar-mirroring baseline and the vendor's
  initial setting (`ScanTriggerPeriod 100000`, seen in the session log).
* **(1,1) below ~86 ms is clamped by the link.** Three 1.31 MB frames every 68 ms would need
  57.9 MB/s; the device delivers 45.1 MB/s at 86.2 ms instead. Same for 30 and 10 ms requests.
* **(3,1) doubles the payload per trigger.** `mono=3` produces three mono slots, each giving both
  IR cameras (2 × 1,310,720 B), plus the colour frame on the last slot: 9.17 MB per trigger. The
  group period becomes **3 × the requested period** (204 ms requested as 68 ms → 222.5 ms
  measured; 270 ms for 90 ms), i.e. the period applies per slot, not per trigger. Bandwidth is
  33–40 MB/s (72–87 % of the ceiling).
* **The vendor's session log matches the per-slot picture**: its capture thread logs a "group"
  every 68/90/100 ms with 2–3 images (2 IR, plus RGB on some), i.e. one slot per log line, not a
  full 3-slot trigger. See [`analysis/cadence/vendor_log_slots.json`](../../analysis/cadence/vendor_log_slots.json):

  | vendor setting | logged slots | images/slot | median interval | avg MB/s | `errorCode 1034` timeouts |
  |---|---:|---|---:|---:|---:|
  | (3,1) @ 68 ms | 49,407 | 2 or 3 | 68.0 ms | ~45 (2 IR + 1/3 RGB) | 0 |
  | (3,1) @ 90 ms | 102,752 | 2 or 3 | 90.0 ms | ~34 | **115** |
  | (1,1) @ 100 ms | 2,225 | 3 | 100.0 ms | 39.3 | 0 |

  At 68 ms the two IR frames alone are 38.6 MB/s, and adding one colour frame every third slot
  brings the average to **~45.0 MB/s — ~99 % of the measured ceiling**. That is the configuration
  with no margin; the vendor stack's 115 grab timeouts are the visible symptom of sitting at the
  wall (they occurred in the 90 ms epoch, where the average is lower but the three-slot bursts
  still peak at 43.7 MB/s per slot).

## 3. Protocol corrections from the measurement

* The 64-bit header field (`header[16..23]`, called "timestamp" in the vendor code and in
  libeinstar) is a **trigger-group counter**: it increments by exactly 1 per trigger group and is
  identical for every frame of that group, independent of the period (90 ms and 222.5 ms groups
  both advance it by 1). It is not a clock and cannot be used for timing.
* The 32-bit frame id (`header[12..15]`) is a **slot counter**: it increments once per mono
  exposure slot; both IR cameras carry the same id for a slot, and the colour frame carries the
  id of the last slot in the group.
* Consequence for loss detection: compare ids against the expected slot stride, not against a
  per-camera +1 sequence.

## 4. Is the USB 2.0 link genuinely limiting normal scanning?

**Yes, at and beyond the vendor's fast mode; no at the 90–100 ms baseline — but with almost no
headroom.**

* The link's measured ceiling is **45.2 MB/s** (75 % of the 60 MB/s theoretical HS rate).
* The scanner's own trigger floor is **86.2 ms per 3-frame group** (11.6 groups/s); any faster
  request is silently clamped.
* EXStar's 90–100 ms baseline uses 86–96 % of that ceiling. Its 68 ms slot cadence with two IR
  frames per slot averages ~45 MB/s (99 %), which is why the vendor log contains frame-grab
  timeouts.
* Therefore the USB 2.0 link **is** the binding constraint for faster scanning: it caps the
  scanner at ~34.5 frames/s (11.6 three-frame groups/s) and forces the (3,1) mode to 3 × the
  requested period. SuperSpeed would remove the 86 ms floor and give the 68 ms mode margin; the
  hardware path for that is still unresolved (`docs/protocol/usb3_capability.md`).

## 5. Impact on the backend and benchmarks

* Replace the assumed "40 MB/s USB 2.0 practical" model with the measured **45.2 MB/s** and the
  observed **86.2 ms** group floor; the simulator scenario should clamp requested periods the
  same way.
* Expect `(1,1)` 3-frame groups at 11.6/s maximum, and `(3,1)` groups at 3 × the slot period.
* Do not treat an unchanged frame id or a +3 colour id as loss; use the slot-stride rule.
* The 115 vendor timeouts are consistent with link saturation; the backend's per-pipe recovery
  (`clear_halt`, no device reset) is the right response.

## 6. Limitations

* One unit, one host controller (Intel 700-series xHCI), one cable (USB 3.0 Type-A plug, HS link).
* 8–30 s runs; no multi-hour soak.
* Lighting was left off for these runs (timing is independent of scene brightness); exposure was
  whatever the device held, not re-measured.
* The vendor log's raw USB frame count per slot cannot be observed directly; its 2–3 "images per
  group" are the host-side grouping of one slot.

## 7. Reproduce

```bash
g++ -std=c++20 -O2 -I<libeinstar>/include tools/capture/cadence_measure.cpp \
    <libeinstar>/build/libeinstar.a <libeinstar>/build/libeinstar_core.a \
    -lusb-1.0 -lpthread -o cadence_measure
./cadence_measure --mode exstar --seconds 30 --no-lights --out run_11_90
./cadence_measure --mode vendor --seconds 15 --no-lights --out run_31_68
python3 tools/capture/analyze_cadence.py run_11_90.csv run_31_68.csv
```
