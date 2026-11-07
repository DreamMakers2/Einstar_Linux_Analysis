# libeinstar — gap analysis for a production Linux implementation

Audit of `<libeinstar>` @ `4564bb19` (plus two uncommitted include
fixes in the working tree), read-only, with a scratch build and test run.
Scope: correctness, robustness and portability of the USB/protocol layer, the
image pipeline and calibration ingestion. Every gap cites `file:line` evidence.
Confidence is stated per item; items marked **INF** are reasoned, not proven.

The ranking criterion is **expected impact on accuracy / speed / reliability**
of a production Linux driver, with safety called out separately. Deployment-only
issues are ranked lower but flagged.

---

## 1. Executive summary

The library is a clean, well-factored reverse-engineering artifact that works on
one unit (macOS, firmware V2.14) and is honest about provenance. It is **not yet
a production Linux driver**:

- **Blocker:** the Linux interrupt-descriptor quirk (`bInterval = 0 → 7`) is not
  handled anywhere; the driver is tested only on macOS.
- **Availability:** calibration ingestion is unit-specific (fixed 32-value IR
  key). Two real CCF sets supplied with this workspace are rejected by the
  parser; the on-device blob goes through the same parser, so a unit with a
  different key cannot provide calibration at all.
- **Correctness:** one frame assembler is shared by all three cameras; any
  interleaving of camera transfers corrupts or drops frames, silently.
- **Portability:** resolution (1280×1024), bit depth (8), camera count (3),
  stream-header offsets, camera one-hot codes and trigger timing are hardcoded;
  the firmware version is never parsed or gated.
- **Safety/lifecycle:** nothing stops the trigger or turns the light off if the
  process dies; `stop_scanning()` always costs 1 s.

Top 5 (the summary requested): **G1, G2, G3, G4, G5** (bInterval, calibration
key, stream demux, firmware/format hardcoding, exposure limits).

Build/test status: scratch build succeeds, `ctest` 1/1 (40 checks) pass without
OpenCV; with a scratch CMake patch for OpenCV 5 components the depth target
builds and 2/2 tests pass; a pristine `HEAD` checkout does **not** build on
GCC 16 (missing `<algorithm>`). Details in
`libeinstar_code_map.md` §7.

---

## 2. Ranking table

| Rank | ID | Severity | Area | Gap | Primary impact |
|---|---|---|---|---|---|
| 1 | G1 | Blocker (Linux) | USB | `bInterval = 0` descriptor quirk not handled | device unusable on Linux |
| 2 | G2 | High | Calibration | fixed 32-element IR key; real CCF sets rejected; on-device path shares it | no calibration / wrong depth |
| 3 | G3 | High | Streaming | single `FrameAssembler` for all 3 cameras, no demux | corrupt/lost frames |
| 4 | G4 | High | Firmware compat | no version check; resolution/bit-depth/camera-count/header hardcoded | breaks on other firmware/FPGA |
| 5 | G5 | High | Exposure/gain | device limits never queried; AE clamps hardcoded; no gain control | saturation, depth holes, set errors |
| 6 | G6 | High (safety) | Lifecycle | destructor leaves trigger/lighting on; 1 s stop; no signal cleanup | laser on after crash, state leakage |
| 7 | G7 | Med-High | Sync | colour frame paired with IR by "last seen", not frame id | depth into stale colour → drift |
| 8 | G8 | Med-High | Reliability | writes retried with a new packet id; up to ~10 s under a global mutex | duplicate commands, latency spikes |
| 9 | G9 | Medium | Reliability | bulk page loop advances by chunk size, not bytes read; trailing partial chunk dropped | calibration read fails under stress |
| 10 | G10 | Medium | Streaming | no backpressure/health counters; callback runs on USB thread; drops silent | frame loss, no diagnosis |
| 11 | G11 | Medium | Robustness | reply marker `0x02` and value-length semantics unchecked | accepts malformed replies |
| 12 | G12 | Medium (deploy) | Build | clean `HEAD` fails on modern GCC; OpenCV 5 components silently disable depth | CI/packaging broken |
| 13 | G13 | Medium | Identity | any PID under VID; no firmware/product validation at open; bus+port only | wrong device opened |
| 14 | G14 | Medium | Calibration | CCF tail (66 u32), 33rd Tex double, `.data_cm` 5 floats, `d5` unparsed | unknown accuracy terms |
| 15 | G15 | Medium | Depth | heuristic stretches/CLAHE can fabricate matches; no ground-truth validation | wrong depth breaks tracking |
| 16 | G16 | Med-Low | Timing | timestamp/clock domain and frame-id width ambiguous | no multi-sensor sync |
| 17 | G17 | Low-Med | Recovery | no hotplug, reconnect or device reset | manual replug after suspend |
| 18 | G18 | Low-Med | API safety | public `transact` allows unknown opcodes; `PageRead` has two paths | misuse risk |
| 19 | G19 | Medium | Tests | fake scanner encodes the same assumptions; no demux/short-read/version tests | regressions undetected |
| 20 | G20 | Low | Docs | doc/code drift (serial vs marker, depth range, PID, firmware version) | operator confusion |
| 21 | G21 | Low | Code | unchecked `build_command` in `read_page`; empty-reply loop; heartbeat read-only | latent UB/edge cases |

---

## 3. Detailed gaps

### G1 — Linux `bInterval = 0` descriptor quirk is not handled (blocker)

**Evidence.** The only USB plumbing is `LibusbTransport`
(`src/device.cpp:19-49`): `libusb_interrupt_transfer`, `libusb_bulk_transfer`,
`libusb_clear_halt`. `Device::open` does `libusb_set_auto_detach_kernel_driver`
and `libusb_claim_interface(0)` and nothing else (`src/device.cpp:156-161`).
There is no descriptor parse, no `bInterval` check, no kernel quirk, no udev
rule in the repo (`scripts/einstar-bringup.sh` only runs the tools; docs mention
"add a udev rule" in prose, `docs/HARDWARE_BRINGUP.md:12`).

The supplied device facts say Linux must patch the interrupt endpoint's
`bInterval` from 0 to 7. The repository's own README states the driver was
tested on macOS (`README.md:9-11`), where this is not exercised.

**Impact.** All commands use the interrupt endpoints `0x01/0x81`. If the kernel
rejects `bInterval = 0` URBs, `libusb_interrupt_transfer` fails, the handshake
fails and the device is unusable — no fallback to bulk/control exists.

**Confidence.** Fact of absence: **CV**. Consequence on a specific kernel:
**INF** (depends on kernel/XHCI behaviour); must be measured on Linux.

**Recommendation.** Add an explicit Linux bring-up path: detect the descriptor
value, refuse with an actionable error if it is 0, and ship the approved
workaround (kernel quirk/patch or a documented descriptor override) plus a udev
rule and an integration test that opens the device on Linux.

---

### G2 — Calibration is unit-specific and hard-fails on real CCF data (high)

**Evidence.**

- Fixed 32-element key and subtraction:
  `src/calibration.cpp:31-34` (declaration and comment "recovered by comparison
  with an EXStar `.data_cm` of the same scanner"), applied at
  `src/calibration.cpp:38` to every field.
- Hard gate on orthonormal rotation, no fallback:
  `src/calibration.cpp:72-81`, enforced by `parse_exstar_ccf`
  (`src/calibration.cpp:157-158`) and `parse_camera_model`
  (`src/calibration.cpp:137-138`). Failure is `NotARotation`
  (`src/calibration.hpp:82`).
- The on-device blob uses the same parser
  (`src/calibration.cpp:178`), so the same key applies to `read_calibration()`.
- The CCF files are 264 B (33 f64) and 520 B (65 f64), but only the first 32
  doubles are read (`kCcfFields = 32`, `src/calibration.cpp:30`). Bytes 256..519
  of the IR files are **66 little-endian uint32 values in 0..9**, and they differ
  between Left and Right of the same unit — per-file data, not padding.

**Reproduced.** Running the freshly built `einstar-calib` on both real CCF sets
in this workspace fails:

```
$ ./tests/build-libeinstar/einstar-calib sources/calibration/res_Einscan-E10_200x150
error: camera rotation is not orthonormal        (exit 1)
```

The same happens for `sources/calibration/ccf_200x150` and for both Left/Right
files. A window scan found no 9-consecutive-double window that is orthonormal,
raw or keyed, so the IR values are not a plain re-ordering either.

**Impact.** A unit whose CCF encoding differs from the one used to derive
`kCcfIrKey` cannot be calibrated: `Device::read_calibration()` returns
`ErrorCode::BadReply`, `einstar-rgbd`/RTAB-Map abort (`tools/einstar-rgbd.cpp:227-233`,
`integrations/rtabmap/CameraEinstar.cpp:250-258`). If a wrong key happens to
produce an orthonormal R, the error becomes a silent accuracy loss instead.

**Confidence.** Failure: **CV** (reproduced). Root cause (per-file key or
different format): **INF** — the tail is observed, its purpose is not proven.
The workspace CCF sets could be from a different unit/generation; that does not
remove the risk, because the parser has no per-unit mechanism.

**Recommendation.** Treat the CCF key as per-unit data: derive/verify it against
the on-device blob, store it with the device serial, or prefer `.data_cm` /
vendor cache. Parse and preserve the 65-double payload instead of assuming 32.
Fail with a diagnostic that names the calibration source and the mismatching
camera; never silently fall back to defaults.

---

### G3 — One frame assembler for three cameras, no demultiplexing (high)

**Evidence.** `Device::stream_loop` constructs exactly one
`FrameAssembler assembler(frame_size)` (`src/device.cpp:490`) and feeds every
EP `0x83` transfer to it (`src/device.cpp:501`). `FrameAssembler::push`
(`src/stream.cpp:23-54`) keeps a single `filled_`, one timestamp latch and one
buffer; the camera field is read only from the **last** transfer
(`src/stream.cpp:46`). `start_stream` has no camera parameter
(`src/device.hpp:117`).

**Impact.** If the firmware ever interleaves transfers from different cameras
or splits a frame set differently, payload bytes are concatenated across
cameras. The result is `SizeMismatch`/`Overflow` (frame dropped) or, worse, a
frame whose `camera_field` belongs to the last chunk. Nothing is logged: the
return value of `push` is compared only with `FrameComplete`
(`src/device.cpp:501-502`), so `SizeMismatch`/`Overflow` are silent.

**Confidence.** **CV** for the design; the reference unit evidently sends each
camera's frame contiguously (the repo reports working depth), so interleaving is
a firmware assumption, not an observed failure.

**Recommendation.** Demultiplex per camera (and sub-index) using the header in
every transfer; keep a small map of assemblers; count and report
`SizeMismatch`/`Overflow` per camera; add a test that interleaves three cameras.

---

### G4 — Firmware/format version blindness and hardcoded geometry (high)

**Evidence.**

- No version check: `firmware_version()` only returns the string
  (`src/device.cpp:271-275`); open never calls it (`src/device.cpp:165-179`);
  nothing parses `V2.14`/`V2.10`/`V3.7`.
- Fixed frame geometry: `start_stream(cb, frame_size = 1280 * 1024)`
  (`include/einstar/device.hpp:117`), `image_width/height = 1280/1024` in the
  model (`include/einstar/calibration.hpp:75-76`), `write_pgm(..., 1280, 1024)`
  (`tools/einstar-probe.cpp:212`), RTAB-Map constants
  (`integrations/rtabmap/CameraEinstar.cpp:59-60`).
- `max_width`/`max_height` and `pixel_bits` exist (`0x16/0x17/0x2e`,
  `protocol.hpp:66-67,76`) but only the probe prints them; the stream never
  adapts.
- Three cameras and one-hot ids are assumed (`stream.cpp:9-16`,
  `device.cpp:460`, `device.cpp:464`).
- The V2.10 firmware image supplied with this workspace shows the version is a
  build-time assembly of `EinScan10_01`, `_SC130_FX3_V`, `2.10`, `_FPGA_V`,
  `3.7`, `EN` (strings at image offsets ~169400 for `Shining3D`, UTF-16
  `SHINING-CAM` at ~57420), i.e. a static string, not a capability descriptor.

**Impact.** On a different FX3/FPGA build (including V2.10 vs the tested V2.14)
a resolution, pixel-format, camera-map or header change produces
`SizeMismatch`/`Overflow` with no frames, or frames silently attributed to the
wrong camera. There is no negotiated fallback.

**Confidence.** **CV** for the hardcoding; **INF** for which specific firmware
changes break it (no second device to test).

**Recommendation.** At open, read product/firmware and gate a known-good
capability table; query `max_width`, `max_height`, `pixel_bits`, sensor count and
configure `frame_size` from them; log a clear incompatibility error; add a
`--allow-unknown-firmware` escape hatch.

---

### G5 — Exposure/gain limits not queried; auto-exposure clamps are guesses (high)

**Evidence.**

- `MaxExposure 0x20`, `MinExposure 0x21`, `MaxGain 0x24`, `MinGain 0x25` are
  declared (`protocol.hpp:68-69,72-73`) but have **no call site** (grep over
  `src/`, `tools/`, `integrations/`).
- `set_exposure_us` / `set_gain` write the raw value and do not clamp
  (`src/device.cpp:415-425`); a device status error becomes `BadReply`
  (`src/device.cpp:244-245`) with no indication that the value was rejected.
- `AutoExposure` clamps to `min_us = 200`, `max_us = 6000`
  (`include/einstar/exposure.hpp:33-34`) while the repository's own notes say
  the device range is 1..10000 µs and gain 1..800 (`docs/PROTOCOL.md:19-20`).
- Gain is never adapted during a scan; `ScanSettings` fixes 105 for IR
  (`include/einstar/device.hpp:43`).
- The AE loop is open-loop in time: the comment says a new exposure is visible
  "a frame or two later" (`include/einstar/exposure.hpp:12-14`), but the code
  has no delay compensation beyond taking half of the correction
  (`src/exposure.cpp:24-31`).

**Impact.** The AE can command values the device rejects (the loop then keeps
retrying the same value, since `wanted()` is called again from the same frame
statistics), or values outside the useful range. Saturation cuts depth coverage;
too-dark speckle does the same. Both directly reduce scan accuracy/tracking.
Gain, which trades noise against exposure, is left at one value.

**Confidence.** **CV** for the missing queries/clamps; **INF** for the exact
device rejection behaviour (not tested without hardware).

**Recommendation.** Query and cache the four limits at open; clamp in the
setters; treat `DeviceStatus` as "value rejected" and back off; expose gain in
`ScanSettings` and the AE policy; add a saturation/coverage metric per frame.

---

### G6 — Trigger and lighting survive process death; stop costs 1 s (safety)

**Evidence.**

- `Device::~Device()` calls only `stop_stream()` (`src/device.cpp:181`); it does
  not `stop_scanning()` or `lights_off()`.
- `stop_scanning()` = `set_trigger_counts(0,0)` (`src/device.cpp:457`), which
  writes period 200000, writes counts 0, then unconditionally sleeps 1 s when
  RGB is 0 (`src/device.cpp:444-450`).
- `lights_off()` exists (`src/device.cpp:471-475`) but is only called by the
  tools/RTAB-Map destructor paths (`tools/einstar-probe.cpp:249`,
  `tools/einstar-rgbd.cpp:186`, `integrations/rtabmap/CameraEinstar.cpp:207-209`).
  `einstar-probe` installs no SIGINT/SIGTERM handler (grep: only `einstar-rgbd`
  and `einstar-rtabmap` do).
- The trigger starts only after `apply()` (`tools/einstar-rgbd.cpp:156-165`), but
  `apply()` leaves the light on until `lights_off()`.

**Impact.** If the process is killed (Ctrl-C in the probe, crash, OOM, SSH
disconnect), the scanner keeps triggering and the projector stays at its last
brightness. For a laser/LED illuminator this is a safety and device-heat issue,
and the next session inherits an unknown state (period 200000, counts 0/nonzero).
Clean shutdown always costs ≥1 s, which is visible in every tool and in
RTAB-Map teardown.

**Confidence.** **CV** for the code paths; **INF** for whether the firmware has
its own watchdog (unknown). Treat as a safety gap regardless.

**Recommendation.** Best-effort `stop_scanning()` + `lights_off()` in the
destructor and in signal handlers; add a `--no-lights`/timeout watchdog; make
the stop settle configurable (or poll `GetTriggerSwitch` until 0); document the
power-off behaviour.

---

### G7 — Colour/IR pairing uses "last colour frame", not frame id (accuracy)

**Evidence.** RTAB-Map's `Impl::onFrame` stores every colour frame in
`lastColor` and pairs it with an IR pair when both IR images for a `frame_id`
exist (`integrations/rtabmap/CameraEinstar.cpp:104-123`); the depth is registered
into that colour image (`:404`, `:421`). `einstar-rgbd` does the same
(`tools/einstar-rgbd.cpp:134-149`). The probe writes each camera's frame
independently and pairs only for auto-exposure (`tools/einstar-probe.cpp:193-200`).
No code compares the colour frame's `frame_id` with the IR pair's id, and no
timestamp difference is checked.

**Impact.** At ~6 fps with a moving scanner, the colour image may be one frame
stale relative to the IR pair. Depth is then projected into a colour view from a
different pose, producing edge misregistration and odometry drift — exactly the
kind of error that is hard to see in a still frame and expensive in a fused
model. The repository's own bring-up notes report 0.5–0.8 mm face RMS and
accumulated scale error (docs/HARDWARE_BRINGUP.md:104-115), but do not isolate
this term.

**Confidence.** **CV** for the pairing logic; **INF** for the magnitude (depends
on whether the firmware emits colour and IR sets in lockstep).

**Recommendation.** Pair by `frame_id` (or nearest timestamp) and drop/report
mismatches; buffer a small window; expose the measured colour↔IR skew.

---

### G8 — Writes are retried as new commands; up to ~10 s under a global mutex

**Evidence.** `transact()` loops `options_.retries` (default 5) and calls
`build_command(ids_.next(), ...)` inside the loop (`src/device.cpp:230-231`), so
each retry is a new packet id. On failure it clears both command pipes and
sleeps 20 ms (`src/device.cpp:252-254`). The whole loop runs under
`command_mutex_` (`src/device.cpp:225`), and each attempt can consume
1000 ms (OUT) + 1000 ms (IN) (`src/device.cpp:237-240`). The same `retries`
value is reused for the bulk page path (`src/device.cpp:331`).

**Impact.** A lost reply after a successful write causes the write to be
re-sent — harmless for exposure/gain, but not for the trigger sequence
(`0x49/0x41/0x49`), where a duplicate can restart the period/counts. Any other
thread issuing commands blocks behind the retry loop for up to ~10 s, which is
visible as UI/scan stalls. There is no distinction between "send failed" and
"reply lost".

**Confidence.** **CV** for the logic; **INF** for device-side idempotency.

**Recommendation.** Retry only idempotent reads by default; for writes,
re-read state (`0x40/0x48/0x22/0x26`) and reconcile; use shorter per-attempt
timeouts with exponential backoff; do not hold the mutex across the whole retry
loop (serialize per command, not per attempt).

---

### G9 — Bulk page loop mis-handles short transfers (reliability)

**Evidence.** `bulk_exchange` (`src/device.cpp:327-369`):

- request: `for (off = 0; off + kChunk <= out.size(); off += kChunk)` — a
  trailing partial chunk is never sent (`:334`);
- reply: `for (off = 0; off + kChunk <= reply.size(); off += kChunk)` reads into
  `reply.data() + off` and adds `got` to `total`, but advances `off` by `kChunk`
  regardless of how many bytes libusb actually returned (`:348-357`). A short
  read therefore leaves a hole and under-counts.
- `read_page` then rejects `r.size() < 9 + 4096` as a **fatal** `BadReply`
  (`src/device.cpp:389`), without retrying the short read.

**Impact.** A single short bulk transfer on a busy bus aborts a calibration page
read, and `read_calibration()` fails. On a flaky cable this looks like a
calibration/device error rather than a transport hiccup. The request-side
partial-chunk bug is latent today (all requests are exactly `0x1400`) but will
bite if the page size changes.

**Confidence.** **CV** (the arithmetic is plain in the code).

**Recommendation.** Advance by `got`, loop until the expected byte count, verify
`total == expected`, and retry short reads before giving up; send trailing
partial chunks.

---

### G10 — No stream backpressure, health counters or latency control (reliability)

**Evidence.** `start_stream` returns `Ok` immediately after spawning the thread
(`src/device.cpp:477-482`); the first frame is not awaited. `stream_loop` calls
the user callback directly on the USB thread (`src/device.cpp:501-502`); a slow
callback delays the next bulk read, which then hits the fixed 200 ms timeout
(`src/device.cpp:494`) and is silently skipped. There are no counters for
timeouts, resets, `SizeMismatch`, `Overflow`, and no queue depth policy. The
RTAB-Map integration keeps only two ready frame sets and pops the oldest
(`CameraEinstar.cpp:123-128`), so a slow consumer loses frames without a trace.

**Impact.** Frame loss is invisible, so accuracy/odometry regressions cannot be
attributed; a burst of timeouts looks like "the scanner is slow". A production
driver needs drop counters and a bounded queue with an explicit policy.

**Confidence.** **CV**.

**Recommendation.** Add an `RxStats` struct (transfers, bytes, timeouts, resets,
completions, per-camera drops) and expose it; move callbacks to a bounded queue
with drop-oldest and a counter; make the stream timeout configurable; fail
`start_stream` if no frame arrives within a deadline.

---

### G11 — Reply validation is weaker than the protocol it documents (robustness)

**Evidence.** `validate_reply` checks only id/group/opcode/status
(`src/protocol.cpp:91-99`). `kReplyMarker = 0x02` (`protocol.hpp:45`) is used
only by the fake scanner (`tests/fake_scanner.hpp:109,146`), never by the real
parser. `reply_uint` accepts any value length 1..4 (`protocol.cpp:160-166`), so
a 1-byte reply to a 32-bit getter decodes as a small number instead of an error.
`reply_value` trusts byte 8 but clamps it (`protocol.cpp:154-158`). There is no
per-command expected-length table.

**Impact.** A stale/garbage 9-byte reply with the right id/group/opcode and
status 0 is accepted; a truncated value is silently accepted for numeric
getters (e.g. exposure 2184 truncated to 2 bytes = 8). Both are rare but produce
wrong state without a diagnostic.

**Confidence.** **CV**.

**Recommendation.** Validate marker, require an exact expected value length per
opcode (or a documented range), and reject unknown trailing lengths; add tests
with marker/length corruption (the fake scanner currently always sets the
marker correctly).

---

### G12 — Clean checkout does not build; OpenCV 5 silently disables depth (deployment)

**Evidence (reproduced).**

- `git show HEAD:src/device.cpp` lacks `#include <algorithm>`; building it with
  GCC 16.2.1 fails at `src/device.cpp:233` in the clean HEAD file (`:234` in the
  working tree; `'copy_n' is not a member of 'std'`) and
  `tests/fake_scanner.hpp:118` in HEAD (`:119` in the working tree). The working
  tree has both fixes
  **uncommitted** (`git status`: `M src/device.cpp`, `M tests/fake_scanner.hpp`)
  — the shipped commit is not buildable on a current toolchain.
- `CMakeLists.txt:48` requests OpenCV components `core imgproc calib3d`. OpenCV
  5.0.0 renamed/split them: `calib3d` no longer exists as a component
  (`/usr/lib/cmake/opencv5/OpenCVModules.cmake` has `opencv_calib`,
  `opencv_geometry`, no `opencv_calib3d`). Configure prints
  `OpenCV not found: einstar::depth disabled` and continues silently; the
  README promises `einstar::depth`.
- The tools/tests block then requests `imgcodecs` with `REQUIRED`
  (`CMakeLists.txt:70,85`), so the component set is inconsistent (if depth is
  found but imgcodecs is not, configure fails later).
- No udev rule or install hook is shipped, despite the Linux docs.

**Impact.** CI or a packager building from the repository gets either a compile
error or a depth-less library without noticing; the first Linux user hits
permissions/descriptor problems with no packaged fix.

**Confidence.** **CV** (both reproduced on this machine).

**Recommendation.** Commit the include fixes; use `find_package(OpenCV QUIET
COMPONENTS core imgproc)` plus a compile check for `cv::stereoSGBM`, or accept
both `calib3d` and `calib/geometry`; make the `imgcodecs` requirement
consistent; ship `70-einstar.rules`; add a CI matrix (GCC/Clang, OpenCV 4/5,
WITH_OPENCV on/off).

---

### G13 — Device selection is VID-only; no product/firmware validation at open

**Evidence.** Enumeration filters `idVendor == 0x3267` only
(`src/device.cpp:94`, `123`). The product string is used only as a preference
(`src/device.cpp:133-141`). `Device::open` performs the handshake and serial
query, but not `product_name()`/`firmware_version()`
(`src/device.cpp:165-179`); a device that answers the handshake and serial but
is not an Einstar will be accepted. Bus/port matching uses the immediate parent
port only (`src/device.cpp:98-99`, `124-125`).

**Impact.** With multiple SHINING 3D devices (or a future PID), the driver can
open the wrong one, or a wrong device can be treated as an Einstar and fail
mid-scan with a generic error.

**Confidence.** **CV**.

**Recommendation.** After open, require product name prefix `EinScan10` and a
parseable firmware string; record VID/PID/serial/firmware in `DeviceInfo`;
match on the full `port_numbers[]` path.

---

### G14 — Unparsed calibration fields (accuracy/unknowns)

**Evidence.** CCF: only 32 of 33/65 doubles are read (`calibration.cpp:30`);
the IR tail (66 uint32) is ignored; the 33rd Tex double is ignored. `.data_cm`:
the five float32 at byte 656 are never read (`calibration.cpp:131-144` reads only
doubles and the final int32). The sixth distortion term `d5` is stored
(`calibration.hpp:60-61`, `calibration.cpp:44`) but `depth.cpp:16-19` builds a
5-coefficient OpenCV vector and drops it. The CCF `header` and
`pair_rotation_raw` are zero for the CCF path (`calibration.cpp:150-162`).

**Impact.** If any of these terms matters (a second distortion model, a
per-unit scale, a pair rotation), depth accuracy is degraded in a way no test
catches. At minimum, the unknowns make the "calibration decoded" claim
incomplete.

**Confidence.** **CV** for what is unread; **UNK** for the effect.

**Recommendation.** Decode and validate the tail/float fields against a
`.data_cm` of the same unit; use `d5` if the OpenCV model supports it (or
document why not); add a reprojection/consistency check to the calibration
loader.

---

### G15 — Depth heuristics can fabricate matches (accuracy)

**Evidence.** `DepthParams` uses half-resolution matching (`scale = 0.5`),
CLAHE equalisation, SGBM parameters and two horizontal stretches
`{1.0, 0.6}` (`include/einstar/depth.hpp:30-67`). `StereoDepth::match`
(`src/depth.cpp:64-82`) runs SGBM on padded images; `disparity`
(`src/depth.cpp:157-198`) accepts a stretched match only where the plain pass
found nothing, then maps the coordinate back with `ur = (u - d) / k`. The
stretch is a horizontal rescale of the right image, not a true resampling of the
epipolar geometry; the comments describe it as an empirical fix for the verged
pair at ~20 cm (`depth.hpp:56-66`).

**Impact.** The stretch can produce plausible but wrong disparity on slanted or
repeating speckle, and the acceptance rule is "first pass that found something",
not a consistency check. Wrong depth is worse than no depth for tracking. The
repository reports agreement of 0.39 mm where both passes found depth
(`depth.hpp:62-63`), which is evidence of consistency, not of correctness.

**Confidence.** **CV** for the algorithm; **INF** for the error rate (no
ground-truth object in the repo; docs say accuracy was not measured,
README:11-13).

**Recommendation.** Validate against a measured artefact; add a left-right
consistency check; make `stretches` opt-in per working distance; expose
confidence/coverage per frame.

---

### G16 — Timestamp and frame-id semantics are opaque (timing)

**Evidence.** `FrameInfo::timestamp` is a BE64 read from the first transfer
(`stream.hpp:28`, `stream.cpp:32`); no unit, epoch or clock domain is known.
`frame_id` is a BE32 read from the last transfer (`stream.hpp:29`,
`stream.cpp:44`), but the probe treats it as 8-bit for file naming and
wrap-aware sequencing (`tools/einstar-probe.cpp:184`, `204-209`). The repository
lists both as open questions (docs/PROTOCOL.md:250).

**Impact.** Multi-sensor fusion (IMU, turntable, second camera) cannot use the
timestamps; frame pairing across cameras relies on id equality, whose width is
unverified.

**Confidence.** **CV** for the reads; **UNK** for the meaning.

**Recommendation.** Capture a long stream and characterise id/timestamp
monotonicity and wrap; expose both raw and a documented conversion; measure the
colour↔IR skew.

---

### G17 — No hotplug, reconnect or reset (recovery)

**Evidence.** `Device::open` is one-shot (`src/device.cpp:109-163`); there is no
`libusb_hotplug_register_callback`, no `libusb_reset_device`, and no retry loop
around open. The stream thread only clears halt/reset assembler on error
(`src/device.cpp:496-500`).

**Impact.** After suspend/resume, cable wiggle or a firmware stall, the user
must replug and restart the process. Production scanners are used on laptops
that sleep.

**Confidence.** **CV**.

**Recommendation.** Add hotplug watch and a bounded reconnect state machine;
reset the device on repeated stream errors; surface state to the app.

---

### G18 — Public raw-transaction API is broader than the safety model (API)

**Evidence.** `Device::transact(Command, payload)` is public
(`include/einstar/device.hpp:75`); `is_forbidden` blocks only `0x58` and system
`0x08` (`src/protocol.cpp:37-40`), so any other opcode — including undeclared
ones — is sent when `allow_writes` is set. `PageRead` is an `Opcode` and can be
issued through `transact` as a 16-byte interrupt request, bypassing the bulk
`read_page` path (`src/device.cpp:371-393`). `is_read_only` marks `HeartBeat`
read-only (`protocol.cpp:30`) although a heartbeat is plausibly a write.

**Impact.** A caller can put the device into an unknown state through the
public API, and a `transact(PageRead)` returns a malformed/short reply rather
than the page. Not a remote risk, but a production library should not expose
raw opcodes as a supported path.

**Confidence.** **CV**.

**Recommendation.** Make `transact` private/typed; whitelist opcodes; keep a
separate `read_page`; document the heartbeat as unverified.

---

### G19 — Test suite encodes the same assumptions as the code (verification)

**Evidence.** 40 checks across `test_protocol.cpp` (19), `test_device.cpp` (9),
`test_calibration.cpp` (7), `test_stream.cpp` (5), plus a separate
`test_depth.cpp` executable. `tests/fake_scanner.hpp` implements the protocol
from the same constants and always returns a correct marker/length
(`fake_scanner.hpp:142-189`). There is no test for camera interleaving, short
bulk reads, reply-marker corruption, firmware-version variants, `bInterval`, or
real USB. The optional real-data tests are skipped unless
`EINSTAR_CAMERA_MODEL`/`EINSTAR_CCF_DIR` are set (`test_calibration.cpp:80-109`,
`152-166`), and the real CCF data present here would **fail** them (§G2).

**Impact.** The suite gives false confidence: it passes while the Linux
descriptor, calibration-key and demux risks are untested. Regressions in those
areas will not be caught.

**Confidence.** **CV**.

**Recommendation.** Add fixtures from a real capture (raw transfers, page
replies), interleaving and short-read tests, malformed-reply tests, version
strings, and a hardware-in-the-loop job. Run the optional real-data tests in CI
with a recorded blob.

---

### G20 — Documentation and code drift (low)

**Evidence.**

| Claim | Doc | Code |
|---|---|---|
| Handshake bytes 9..16 are the serial | `docs/PROTOCOL.md:70-71` | bytes 9..16 are the `"Shining3"` marker; serial comes from `0x04` (`docs/PROTOCOL.md:15-16`, `device.cpp:173-177,209-210`) |
| PID `0x0003` | `docs/PROTOCOL.md:13` | no PID constant; any PID under the VID (`device.cpp:94`) |
| Default depth range 200–800 mm | `docs/HARDWARE_BRINGUP.md:48` | 130–800 mm (`include/einstar/depth.hpp:39-40`) |
| Tested firmware V2.14 | `README.md:10`, `docs/PROTOCOL.md:5` | supplied images are V2.10; code checks neither |
| Trigger period default 66666 | `docs/PROTOCOL.md:21` | no code reference; `start_scanning` always writes 200000/90000 |

The 66666 figure is context-only: it appears in no source file (grep).

**Impact.** Bring-up operators chase the wrong serial/offset and misjudge the
depth range; the V2.10/V2.14 difference is invisible to the code.

**Confidence.** **CV**.

**Recommendation.** Fix the doc contradictions; generate the command table from
`protocol.hpp`/tests; record the exact firmware tested per release.

---

### G21 — Minor code issues (low)

- `read_page` dereferences `build_command(...)` without checking `nullopt`
  (`src/device.cpp:378-380`); currently safe because the payload is 2 bytes, but
  it is UB if the payload grows.
- `read_custom_info` loops while `out.size() < size` (`device.cpp:398-403`); a
  `read_page` that returned an empty vector would spin forever (today it always
  returns 4096 or an error).
- `is_read_only(HeartBeat)` is true (`protocol.cpp:30`), although a heartbeat is
  likely a write; `SetIndication` is correctly not read-only.
- `Device::enumerate` opens every candidate to read the product string
  (`device.cpp:51-59`), which needs permissions and can be slow.
- `kReplyMarker` is dead code in the real parser (see G11).

---

## 4. Cross-cutting observations

### 4.1 Speed

- Command round-trip is serialized under one mutex; a failing device blocks all
  callers for up to ~10 s (G8).
- `stop_scanning()` always sleeps 1 s (G6).
- Stream processing is single-threaded at 200 ms timeouts; a slow callback
  drops frames rather than applying backpressure (G10).
- Depth matching runs two passes at 0.5 scale by default; the repository
  measured 65→131 ms/frame for `{1.0, 0.6}` on Apple M-series
  (`include/einstar/depth.hpp:65-66`), so the added stretch is a real cost.
- Page reads are 5 × 1 KiB chunks with 1 ms gaps plus 3-try/80 ms reply
  handling; a calibration read (2 pages) is not fast (G9).

### 4.2 Accuracy

- Calibration key/format (G2) is the dominant risk; it can silently bias every
  depth pixel.
- Colour/IR pairing (G7) biases depth-to-colour registration during motion.
- Exposure/gain limits (G5) change speckle contrast and depth coverage.
- Depth heuristics (G15) can add wrong depth, which is worse than holes.
- Unparsed calibration terms (G14) are unknown bias terms.

### 4.3 Reliability

- Linux descriptor (G1) is a hard blocker.
- Demux (G3), short reads (G9), retries (G8), no hotplug (G17) and no stream
  health (G10) are the main runtime failure modes.
- No watchdog or automatic cleanup (G6).

---

## 5. What could not be tested here

- No Einstar hardware is connected, so no real USB transfer, handshake,
  page read, stream or trigger was exercised (`einstar-probe` reports 0 devices).
- The two real CCF sets in the workspace are the only calibration ground truth;
  they fail the parser, but no matching `.data_cm` or on-device blob is available
  to prove which side is wrong (G2).
- The `bInterval` behaviour depends on the kernel/XHCI; this audit can only
  confirm the absence of any workaround.
- Firmware behaviour under error/duplicate commands is unknown; claims about
  device-side idempotency are marked **INF**.

---

## 6. Suggested order of work

1. **Linux bring-up (G1/G12)**: descriptor workaround, udev rule, CI build;
   prove the handshake and a page read on Linux.
2. **Calibration trust (G2/G14)**: derive/validate the IR key per unit; parse
   the full CCF; verify against the on-device blob; add a diagnostic path.
3. **Stream robustness (G3/G10/G11)**: per-camera assemblers, counters,
   marker/length validation, interleaving tests.
4. **Capability negotiation (G4/G13)**: parse firmware; query geometry/format;
   gate known versions.
5. **Exposure/gain (G5)**: query limits, clamp, close the AE loop, add gain.
6. **Lifecycle/safety (G6)**: RAII stop + lights-off, signal cleanup,
   configurable settle.
7. **Sync and accuracy (G7/G15/G16)**: frame-id pairing, consistency checks,
   ground-truth validation.
8. **Recovery/API/tests/docs (G8/G9/G17/G18/G19/G20/G21)**.
