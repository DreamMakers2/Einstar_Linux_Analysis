# Readiness assessment — can the new backend replace libeinstar?

Status: 2026-11-11. The scanner is **attached and has been exercised**. This
document separates four states:

| State | Meaning |
|---|---|
| **Implemented** | code exists and is wired into the build |
| **Offline-validated** | covered by deterministic tests independent of the implementation (golden vectors, invariants, fuzzing, sanitizers) |
| **Clean-room verified** | a fresh clone of the published repository passes its own `scripts/verify_all.sh` (278 tests, 70 skipped without private fixtures) and builds both C++ trees (`recon` ctest 1/1, `driver` ctest 13/13) with no files missing |
| **Hardware-validated** | observed working against the physical scanner |
| **Unresolved** | known gap; a gate below is required before it can be trusted |

## 1. Status matrix

| Capability | Implemented | Offline-validated | Hardware-validated | Notes |
|---|---|---|---|---|
| USB enumeration, descriptor parse, `bInterval=0` detection | yes | yes | **yes** — 2026-11-07 and 2026-11-11, HS 480 Mbps | kernel repairs the interval to 7; no patch needed; the device is USB 2.0 on the wire (`protocol/usb3_capability.md`) |
| Async bulk-IN transfers, queue depth, cancellation | yes | yes (simulator + ASan/TSan) | **yes** — 600 groups / 2,300 MB in 60 s, 0 timeouts, 0 stalls | 8 URBs × 41,984 B |
| Packet framing | yes | yes — golden vectors + raw/legacy modes | **yes** — **no length prefix**: `[32B header][40,960B payload]`; 9,660/9,660 transfers were 40,992 B | the documented `[u32 len]` is host-side only (`protocol/einstar_usb_protocol.md` §3) |
| Three-camera reassembly | yes | yes (synthetic + loss/reorder/fuzz) | **yes** — 600/600 groups, 1,800 frames, 3 startup drops | one in-flight frame per camera; packet index (header 9..11) detects gaps |
| Frame id / group semantics | yes | yes | **yes** — the slot counter is **8-bit and wraps at 256**; the u64 group counter is monotonic | ordering is modular, group key is the u64 counter |
| Sequence-gap, incomplete-frame, overflow, stale, timeout handling | yes | yes | **yes** — per-reason drop counters; startup group identified as the only loss | counters + loss events; no silent drops |
| Lossless recording / replay | yes | yes (byte-exact, corruption/bomb rejection) | **yes** — 1,050/1,050 PGMs byte-identical live vs replay | CRC-32 per record, trailer digest, framing stamped in metadata |
| C ABI for a Rust control plane | yes | yes (FFI test) | no | single-stream view; camera_id exposed |
| Command channel (serial, firmware, exposure, gain, trigger, page read) | yes | yes — 22 cases / 4,379 checks against a byte-accurate fake FX3 | **yes** — serial, firmware `…FX3_V2.10_FPGA_V3.7_EN`, 3 sensors, page read of the 6568-byte blob (4 reads identical) | firmware update and flash writes deliberately excluded; gain round-trips within 1 LSB |
| Illuminator control (laser 0x67/0x68, strobe 0x6f/0x70) | yes | yes | **yes** — found at 0 %, set to 100 %/1000:300, restored to 0 on exit | volatile only; the cause of the first black IR frames |
| Calibration model + rectification + triangulation | yes | yes (synthetic round-trip, real-capture consistency) | **yes** — factory intrinsics+distortion fit live plate at 0.38–0.41 px (IR) / 0.37 px (colour) | CCF key proved; absolute scale still plate-dependent |
| EXStar calibration workflow (5×5×3) | documented/implemented | yes (XML, code, real session log; 2m56s) | no | no device write performed |
| Adaptive 3-camera calibration engine | yes | yes (98 tests; synthetic NEES ≈1; vendor 0.367 px; held-out 0.455 px) | no | `calib/`; marginal covariance/stopping audit 2026-11-10 |
| Calibration uncertainty/quality report | yes | yes (NEES, coverage, stability, held-out; 0.91–1.02 NEES/n) | no | no physical artefact validation |
| Depth from real stereo pairs | partial (offline script) | consistency-only | **yes** on live stereo pairs (see the reconstruction report) | still no calibrated artefact; absolute accuracy remains unproven |
| Colour (RGB) registration | yes | synthetic + corpus | **diagnosed live**: colour intrinsics 0.37 px; the factory colour **extrinsic is off by ≈18 mm (≈49 px)** | `color_status` stays `stale`; a candidate correction needs an independent colour check |
| Performance claims | — | simulated scenarios only | no | see `docs/benchmarks/offline_benchmarks.md` |

## 2. What changed in this round (and why it mattered)

0. **Live bring-up (2026-11-11).** Three defects that only hardware could expose:
   the stream has **no length prefix** (the documented `[u32 len]` is a host-side
   field), the per-exposure **slot counter is 8 bits and wraps at 256**, and the
   **speckle illuminator is volatile and was left at 0 %** by a previous client
   (IR frames near-black). Fixes: `FramingMode::Raw` as the default with the
   legacy mode preserved for old recordings, modular frame ordering with a
   rescue from the u64 group counter, and read/set/restore of laser and strobe.
   Also found: the reconstruction profile stores `skew` in absolute pixels while
   `calib.CameraIntrinsics` normalizes it — mixing them costs 10 px; new
   `calib/recon_profile.py` loads profiles correctly.  Full report:
   `docs/reconstruction/live_bringup_2026-11-11.md`.

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

| # | Gate | State | Evidence |
|---|---|---|---|
| G1 | Enumeration + claim | **PASS** | `3267:0003`, interface 0 claimed, HS 480 Mbps, next open after every run succeeds |
| G2 | Identity | **PASS** | serial, `EinScan10_01_SC130_FX3_V2.10_FPGA_V3.7_EN`, 3 sensors, 1280×1024 per camera |
| G3 | Command channel | **PASS (1 LSB)** | exposure read-back exact (1200 µs), gain set 105 reads 106, trigger period and counts read back as set, page 0/1 read = factory blob byte-for-byte |
| G4 | Framing | **PASS after fix** | with the documented length prefix: 358,923,797 parse errors and 0 frames. With the raw framing: 115,168 packets, 0 parse errors, 600/600 groups |
| G5 | Cadence | **PASS** | 100 ms trigger → 10.00 groups/s, 39.4 MB/s, 600/600 over 60 s; 68 ms is link-limited to ~80 ms (≈49 MB/s ceiling) |
| G6 | Recovery | **PARTIAL** | a 600-group run crosses the 8-bit slot wrap with zero loss; no cable-pull/`clear_halt` injection has been performed yet |
| G7 | Plate truth | **PASS (+ residual)** | 8×5 markers, 28.0 mm pitch; live detection 40/40 with UV-label witnesses verified in 350/350 frames; manufacturing tolerance still unmeasured |
| G8 | Calibration physical accuracy | **BLOCKED** | needs the one-session calibration (§`calibration/one_session_plan.md`) and a calibrated artefact for scale |
| G9 | Recording | **PASS** | 60 s recording replays byte-exactly; live-vs-replay PGMs 1,050/1,050 identical |
| G10 | Side-by-side with libeinstar | **NOT RUN** | requires the vendor stack on a second host or a re-run |
| G11 | Long soak (≥ 8 h) | **NOT RUN** | bounded captures only; thermal behaviour unmeasured (the temperature getter returns 0 raw) |

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

The backend is **live-verified for acquisition**: it opens the device, reads the factory
calibration, controls the illuminator, captures synchronised three-camera groups for minutes at
a time, records losslessly and replays byte-exactly, and leaves the device in a safe idle state.
The bring-up found three real defects that no offline test could have caught (missing length
prefix, 8-bit slot-counter wrap, volatile illuminator left off) and one convention trap in the
Python profiles (skew).

It is **not yet a production replacement**: recovery under an induced cable/halt event, a long
soak, thermal behaviour, side-by-side comparisons with the vendor stack, and any absolute
accuracy claim are still open. Absolute scale remains unresolved and cannot be settled with the
plate alone.

### Scanner readiness (2026-11-12)

| item | state |
|---|---|
| live scanning on hardware | **verified**: 4/4 frames captured, tracked and fused from the physical device, capture stopped cleanly, trigger 0/0 and illuminator 0 % afterwards |
| depth reconstruction | 35 ms/frame at scale 0.25, 132 ms at 0.5 (C++ engine) |
| tracking | **projective ICP on the engine's depth maps is the scanner default**: near-object depth mask, 0.24 deg median tilt error at scale 0.5 against the plate's own PnP pose (cloud path 0.58 deg), 0.06 deg at scale 1.0; falls back to the cloud tracker when depth maps are unavailable; degeneracy and loss signals reported |
| fusion + meshing | TSDF + marching cubes; sphere recovered to 0.03 mm p95; fused plate 0.60 mm thick across 12 independent poses |
| colour registration | **geometry fixed**: shipped record 461 px, re-fitted to 0.34 px held-out and 0.32 px on an independent session; engine reports `color ok`, colour_fraction 1.00. **White balance** removes a measured 29.2 % cast to 2.5 % (recorded per scan). Photometry still needs a colour-lit scene: ambient-only at the 10 ms exposure limit reaches p99 luminance 37/255. |
| export | OBJ, PLY, STL and a reopenable project document |
| performance | live budget measured: ~8 fps best single-core configuration, ~4 fps at default quality; 10 fps not reached |
| accuracy | **absolute dimensional accuracy still unverified** (no external artefact) |

### Calibration wizard readiness (2026-11-11, updated)

| item | state |
|---|---|
| Qt6 application (PySide6) | launches, renders three cameras + overlays + coverage map + observability, controls work, headless snapshot mode tested |
| one decision path for live and replay | same controller wraps `calib.session`; replay determinism tested |
| live acquisition through the GUI | **verified on hardware** (25 s: 6 accepted, ring 120, 0 drops, safe shutdown) |
| capture tool control/ring/signals | `--control/--ring/--max-seconds/--session-dir`; 13/13 driver tests incl. 134 new capture-session checks |
| coverage gate (D1–D8) | measured on real data; one-axis sweep correctly NOT READY with named missing dimensions; readiness path exercised both ways in tests |
| observability gate | marginal σ, condition number, per-parameter verdicts; 0.3–2.5 ms |
| profile lifecycle | factory/candidate/validated, 9 gates, activation refused on failure, store byte-identical, rollback |
| offline rehearsal | sweep (16 accepted, NOT READY), vendor corpus (17 accepted, candidate produced, 4/9 gates fail honestly) |
| synthetic full-coverage rehearsal | **achieved**: self-validating plan, 33 accepted, coverage 8/8 |
| validated candidate profile | **achieved** on synthetic truth: 9/9 lifecycle gates, activation → revision 1, rollback → factory |
| partial-plate recognition | visibility-aware witness with 10 measured regression tests; detector-side partial assignment still open (documented) |
| failure-case matrix | 17 measured cases, all refused; one real gap found (jittery timestamps disabled the motion gate) and fixed |
| one comprehensive session | **ready to run**; operator guide in `calibration/wizard_operator_guide.md` |

### Reconstruction and calibration readiness (2026-11-11)

| item | state |
|---|---|
| live acquisition | verified (600 groups, 10 Hz, 0 link loss) |
| plate detection / pose on live IR | 40/40 markers, 0.38–0.41 px, UV labels witnessed in every frame |
| factory intrinsics + distortion | fit live data at 0.38–0.41 px (IR), 0.37 px (colour) |
| factory IR stereo extrinsic | effective correction 0.239° / 1.3 mm; cross-camera 5.18 → 0.42 px |
| factory colour extrinsic | ≈18 mm (≈49 px) off; colour stays gated |
| focal length from the sweep | **unobservable** (degenerate with distance, measured) |
| distortion refinement from the sweep | **not separable** (limited image coverage) |
| absolute scale | unresolved; needs a calibrated artefact |
| one-session calibration | plan written, needs D1–D8 pose diversity (`calibration/one_session_plan.md`) |

### Adaptive calibration readiness (2026-11-10)

| item | state |
|---|---|
| plate spec | recovered (28.0 mm, 8x5) |
| detector | selected (`auto` offline, `centroid_bg` live), benchmarked |
| solver | LM 0.23 s, covariance calibrated for fx/baseline/3-D; cx caveat |
| guidance | implemented + headless dashboard; closed-loop simulation |
| stopping | multi-gate + mm saturation; vendor corpus fails stability (honest) |
| strategy evidence | 10-trial Monte Carlo, task-space best (10/10 vs vendor) |
| real-corpus accuracy | unverified; scale traceable to plate pitch only |
| hardware validation | blocked on a calibrated artefact (B15) |
