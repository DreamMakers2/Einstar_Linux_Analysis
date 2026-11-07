# Readiness assessment — can the new backend replace libeinstar?

Status: 2026-11-07. The scanner is **not attached**. This document separates four states:

| State | Meaning |
|---|---|
| **Implemented** | code exists and is wired into the build |
| **Offline-validated** | covered by deterministic tests independent of the implementation (golden vectors, invariants, fuzzing, sanitizers) |
| **Hardware-validated** | observed working against the physical scanner |
| **Unresolved** | known gap; a gate below is required before it can be trusted |

## 1. Status matrix

| Capability | Implemented | Offline-validated | Hardware-validated | Notes |
|---|---|---|---|---|
| USB enumeration, descriptor parse, `bInterval=0` detection | yes | yes | **yes** — connected 2026-11-07, HS 480 Mbps | kernel repairs the interval to 7; no patch needed; the device is USB 2.0 on the wire (`protocol/usb3_capability.md`) |
| Async bulk-IN transfers, queue depth, cancellation | yes | yes (simulator + ASan/TSan) | no | libusb path compile/link-verified only |
| Packet framing (`[len][32B header][payload]`) | yes | **yes** — hand-written golden vectors | no | semantics: one-hot camera bits 3..5, last flag byte 8 |
| Three-camera reassembly | yes | yes (synthetic + loss/reorder/fuzz) | no | one in-flight frame per camera by construction |
| Frame id / timestamp semantics | yes | yes | no | id valid on last packet; timestamp latched from first |
| Sequence-gap, incomplete-frame, overflow, stale, timeout handling | yes | yes | no | counters + loss events; no silent drops |
| Lossless recording / replay | yes | yes (byte-exact, corruption/bomb rejection) | no | CRC-32 per record, trailer digest |
| C ABI for a Rust control plane | yes | yes (FFI test) | no | single-stream view; camera_id exposed |
| Command channel (serial, firmware string, exposure, gain, trigger, page read) | yes | yes — 22 cases / 4,379 checks against a byte-accurate fake FX3 | no | request/echo/status/retry/timeout paths covered; firmware update and flash writes deliberately excluded |
| Calibration model + rectification + triangulation | yes | yes (synthetic round-trip, real-capture consistency) | no | absolute `fx/fy/cx/cy` key not fully pinned |
| Depth from real stereo pairs | partial (offline script) | consistency-only | no | no ground truth; needs an artefact measurement |
| Performance claims | — | simulated scenarios only | no | see `docs/benchmarks/offline_benchmarks.md` |

## 2. What changed in this round (and why it mattered)

1. **Framing defect fixed.** The first backend decoded the last-packet flag as a first-packet flag,
   read the camera from header bits 0..2 instead of 3..5, and required a frame id on every packet.
   It passed only because the simulator emitted the same wrong convention. The reassembler was
   rewritten around one in-flight frame per camera, completion on last packet + exact size, and
   the frame id taken from the last packet only. `driver/tests/test_conformance.cpp` now pins
   every field with hand-written byte vectors that never involve the simulator.
2. **`bInterval` misconception removed.** Linux USB core repairs `bInterval = 0` to 7 at high
   speed. The backend no longer refuses to claim the interface, and the docs no longer claim a
   kernel patch is required. This matches the field evidence (libeinstar worked on a stock
   kernel).
3. **Benchmarks separated.** The generic 300 MB/s model is gone from the headline; the tool now
   reports USB 2.0 HS, hypothetical USB 3.0, the vendor 68 ms cadence, and a host-only ceiling as
   distinct scenarios, with the ~58 MB/s vs ~40 MB/s cadence problem called out explicitly.
4. **Tests hardened.** 11 C++ binaries / 90 cases, including conformance vectors, deterministic
   fuzzing and a 10 000-frame stress run; clean under ASan+UBSan and TSan. Recording rejects bad
   magic, bad CRC, truncation and absurd record lengths. The command channel adds 21 cases /
   4,373 checks against a fake device that validates wire sizes, argument fields and packet ids.

## 3. Gates before replacing libeinstar

Each gate is a concrete check with a pass condition. All must pass on the physical unit.

| # | Gate | Pass condition |
|---|---|---|
| G1 | Enumeration + claim | device opens, interface 0 claimed, `lsusb -t` shows the negotiated speed; both interrupt endpoints schedule with interval 7 (no patch) |
| G2 | Identity | `Serial` (sys 0x04) and `FirmwareVersion` (0x05) match the vendor string format; sensor count = 3; resolution 1280×1024 per camera |
| G3 | Command channel | set/get exposure and gain round-trip within 1 LSB; trigger period read-back equals 68 000 µs after set; page 0 read equals the factory calibration blob byte-for-byte |
| G4 | Framing | a 60 s capture yields zero parse errors, three camera streams, monotonic per-camera frame ids, no size mismatches |
| G5 | Cadence | measured frames per trigger group and per-camera rate. The link question is now partly answered: the device negotiates HS 480 Mbps, so the 68 ms/3-frame cadence exceeds the link (~1.45×) and the device must be dropping or throttling frames |
| G6 | Recovery | induced `clear_halt`/cable event: stream resumes < 100 ms, no corrupted or short frames delivered, loss events counted |
| G7 | Recording | record 60 s, replay offline, byte-exact digest, then decode both paths and compare frames |
| G8 | Calibration | rectified IR pair shows vertical disparity residual < 0.5 px (median) on real captures; depth of a flat target within the vendor's own spread |
| G9 | Side-by-side | the same object scanned with libeinstar and with the new backend; mesh/point-cloud deviation below the noise floor of two repeated vendor scans |

## 4. Known risks at bring-up

| Risk | Mitigation |
|---|---|
| Streaming semantics may still differ in an unobserved corner (e.g. sub-index wrap, header padding) | record raw transfers first; the recorder stores exact bytes, so any residual mismatch can be re-analysed offline |
| The 68 ms cadence may not correspond to three full frames | G5 measures it before any performance work; the benchmark model has both branches |
| Calibration `fx/fy/cx/cy` key is not fully proved | rectification is insensitive to ±1 px in `f`; vertical-residual check (G8) catches wrong R/T, and a physical artefact check catches scale |
| libusb event-thread ownership | the transport delivers completions from `poll()` on the caller thread; `close()` refuses to close a context still owned by libusb (`shutdown_complete()` reports it) |
| No hardware means no soak test | G6/G9 include a 60 s and a side-by-side run; an 8 h soak is required for production sign-off |

## 5. Rollback

`libeinstar` remains the working reference and is untouched. The new backend is additive: if a
gate fails, the existing driver keeps working and the discrepancy becomes a new recorded
fixture for offline analysis.

## 6. Honest summary

The backend is **ready for controlled offline-to-hardware bring-up**, not for production
replacement. It has been hardened where it was provably wrong (framing, `bInterval`, shutdown
ordering) and the remaining risk is dominated by unobserved device behaviour rather than by
known defects. The first hardware session should follow gates G1–G7 in order; G8–G9 require the
calibration and reconstruction work to be finished.
