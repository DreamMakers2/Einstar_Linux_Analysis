# Offline benchmarks — separated link scenarios

Status: **simulated device, real host measurements.** No scanner was attached. Every row below
comes from the deterministic simulator in `driver/` (virtual clock); `host_ms` / `host_fps` are
real wall-clock measurements of the reference machine (i9-14900K, 32 threads).

Reproduce:

```bash
cmake -S driver -B build && cmake --build build -j
./build/einstar-bench --scenarios-only --out bench_scenarios.json
```

> Earlier tables used a single generic "300 MB/s" model. That number is neither USB 2.0 nor
> USB 3.0 and **must not** be quoted as achievable scanner throughput. The scenarios below
> replace it.

## 1. Link scenarios (1280×1024, 8-bit, 32 packets/frame)

| scenario | link model | trigger | cameras | depth | agg MB/s | frame/s | fps/camera | p50 µs | p99 µs | host ms/60 | host fps |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| usb2-hs | 40 MB/s (practical HS bulk) | none | 3 | 1 | 27.1 | 20.7 | 6.88 | 1550 | 1550 | 11.5 | 5198 |
| usb2-hs | 40 MB/s | none | 3 | 8 | **40.0** | 30.5 | 10.16 | 7347 | 8397 | 13.4 | 4487 |
| usb3-ss | 350 MB/s (hypothetical) | none | 3 | 1 | 67.7 | 51.6 | 17.21 | 620 | 620 | 10.9 | 5522 |
| usb3-ss | 350 MB/s | none | 3 | 8 | 229.7 | 175.2 | 58.41 | 980 | 1460 | 13.2 | 4542 |
| trigger-68ms | 350 MB/s | 68 ms | 3 | 1 | 58.2 | 44.4 | **14.81** | 620 | 10344 | 11.1 | 5391 |
| trigger-68ms | 350 MB/s | 68 ms | 3 | 8 | 60.1 | 45.8 | 15.28 | 1100 | 52564 | 13.9 | 4327 |
| host-only | 10 GB/s | none | 3 | 1 | 83.2 | 63.5 | 21.16 | 504 | 504 | 11.4 | 5281 |
| host-only | 10 GB/s | none | 3 | 8 | 627.3 | 478.6 | 159.53 | 517 | 534 | 13.6 | 4418 |

Reading:

* **usb2-hs saturates at 40.0 MB/s** in the model. The real scanner was measured at
  **45.2 MB/s** with an 86.2 ms three-frame group floor (see
  [measured_cadence.md](measured_cadence.md)), so treat 40 MB/s as a conservative floor and
  45.2 MB/s as the measured ceiling.
* **usb3-ss is hypothetical.** The device exposes USB 3.0 descriptor sets, but the previously
  observed connection was High Speed. This row exists to bound the upside, not to claim it.
* **trigger-68ms** is cadence-limited: 44.4 frames/s aggregate = 14.8 fps per camera. p99 rises
  to ~10 ms at depth 1 and ~53 ms at depth 8 — buffering converts cadence jitter into tail
  latency, which matters for live feedback.
* **host-only** measures the pipeline ceiling (627 MB/s at depth 8 with the simulator's
  zero-cost device): the host is not the bottleneck at the vendor cadence.

## 2. Bandwidth vs cadence — the important arithmetic

```
frame on the wire   = 1,311,872 B   (1,310,720 payload + 32 × 36 B framing)
3-camera group      = 3,935,616 B
68 ms cadence needs = 57.9 MB/s aggregate
USB 2.0 HS practical (40 MB/s)  -> 30.5 fps aggregate, 10.2 fps/camera
USB 3.0 SS (350 MB/s, if usable) -> cadence-limited 44.1 fps aggregate, 14.7 fps/camera
```

**A USB 2.0 High Speed link cannot carry three full 1280×1024 frames every 68 ms.** The question
was settled when the scanner was connected (2026-11-07): it negotiates **High Speed 480 Mbps**
(`bcdUSB 2.00`, no BOS), so the USB 2.0 branch is the operative one and the device must be
dropping or throttling frames. The firmware does enable SuperSpeed, but the physical path does
not train it; see [../protocol/usb3_capability.md](../protocol/usb3_capability.md). The HS ceiling
is ~1.45× short of the 68 ms cadence, which matches the 115 frame timeouts in the vendor log.

## 3. Sync vs async, copy vs zero-copy (same simulated device)

From the depth/copy tables in the same run (300 MB/s generic model, for relative comparison only):

| experiment | depth 1 (sync) | depth 8 | depth 16 |
|---|---:|---:|---:|
| saturated throughput | 65.5 MB/s | 207.2 MB/s | 245.0 MB/s |
| host wall time / 120 frames | 34.5 ms | 37.4 ms | 46.4 ms |
| zero-copy host time / 120 frames | — | 16.4 ms | 17.5 ms |

Async issuance raises achievable throughput 3.2–3.7× over one outstanding transfer **when the
link, not the cadence, is the limit**; at the vendor cadence all depths deliver the same frame
rate and only latency tails differ. Zero-copy roughly halves host wall time by removing the
payload memcpy.

## 4. What is measured vs modelled

| Quantity | Source |
|---|---|
| frame rate, MB/s, device latency, drops, queue depth | **virtual clock simulator** (deterministic, seeded) |
| `host_ms`, `host_fps` | **real** wall clock on the reference machine |
| USB 2.0 / USB 3.0 link rates | assumed practical values, documented above |
| scanner behaviour (trigger grouping, frame counts, real link speed) | **unknown — hardware required** |

## 5. What must be measured on hardware before any performance claim

1. Negotiated link speed (`lsusb -t`, `usb_device_speed`) and actual bulk throughput.
2. Frames per trigger group and per-camera cadence at `ScanTriggerPeriod 68000`.
3. Whether the device ever sends fewer than 32 packets per frame (ROI/binning).
4. End-to-end latency from trigger to frame delivery, and p99 under a 10-minute scan.
5. Drop/recovery behaviour under induced stalls (clear_halt, cable pull).
