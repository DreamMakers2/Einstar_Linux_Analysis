# Einstar USB acquisition backend

Production-oriented, offline-testable acquisition core for the SHINING 3D
Einstar (EinScan10 / E10, USB `3267:0003`). C++23, CMake, no Qt, no CUDA, no
network, no hardware required to build, test or benchmark.

> **Validation status (2026-11-11).** The libusb backend is now **verified
> against a physical Einstar**: probe, metadata, 6568-byte factory calibration
> read, volatile illuminator/exposure/gain/trigger writes and synchronised
> three-camera capture all ran on hardware; 600/600 groups in 60 s at a 100 ms
> trigger (39.4 MB/s) and 1,050/1,050 replay frames byte-identical to the live
> capture.  Sections below marked **SIMULATED** are still simulator-only
> numbers.  The bring-up found and fixed two real wire-format defects
> (no length prefix on the stream; an 8-bit slot counter that wraps every 256
> groups) — see `docs/reconstruction/acquisition.md` and
> `docs/protocol/einstar_usb_protocol.md` §3.

### Live quick start (verified)

```sh
./build/driver/einstar-capture-frames --backend libusb --probe-only --out /tmp/probe
./build/driver/einstar-capture-frames --backend libusb \
    --groups 600 --timeout-seconds 120 --trigger 100000 \
    --laser 100 --strobe 0:1000 --strobe 1:300 \
    --exposure-us 0:1200 --exposure-us 1:1200 --exposure-us 2:5000 \
    --gain 0:105 --gain 1:105 --gain 2:180 \
    --record /tmp/sweep.e1r --status-file /tmp/sweep/status.json \
    --progress 10 --out /tmp/sweep/frames --show-serial --json
./build/driver/einstar-replay-frames --in /tmp/sweep.e1r --out /tmp/replay --manifest
```

```
driver/
  include/einstar/     public headers
    status.hpp         Status/ErrorCode, std::expected result alias
    clock.hpp          Clock, SteadyClock, VirtualClock
    transport.hpp      Transport abstraction + completion contract
    usb_descriptor.hpp USB config parser and bInterval quirk codec
    packet.hpp         [u32 BE len][32-byte header][payload] framer
    frame.hpp          BufferPool, BufferHandle, copy / zero-copy Frame
    reassembler.hpp    (camera, frame_index) reassembly, gaps, timeouts
    queue.hpp          bounded queue (3 policies) + lock-free SPSC ring
    stats.hpp          latency histograms, pipeline counters, metrics
    recorder.hpp       append-only recording format (CRC per record)
    sim.hpp            deterministic fault-injecting device simulator
    pipeline.hpp       async acquisition pipeline + runner thread
    libusb_transport.hpp
    ffi.h              C ABI for the Rust control plane
  src/                 implementation
  tools/               record_replay.cpp, bench_acq.cpp
  tests/               deterministic offline tests (tiny assert harness)
  CMakeLists.txt
```

## Build, test, benchmark

Verified on this machine: GCC 16.2.1, CMake 4.4.5, libusb-1.0.30, 32 vCPU.

```sh
cmake -S driver -B build/driver
cmake --build build/driver -j"$(nproc)"
ctest --test-dir build/driver --output-on-failure
./build/driver/einstar-bench --out build/driver/bench_results.json
```

Real configure/build output (abridged):

```
-- libusb-1.0 found: building LibUsbTransport
-- Configuring done
-- Generating done
[100%] Built target einstar-bench
[100%] Built target test_stress
```

Real `ctest` output:

```
 1/11 Test #1  test_conformance ...............   Passed    0.00 sec
 2/11 Test #2  test_packet ....................   Passed    0.00 sec
 3/11 Test #3  test_reassembler ...............   Passed    0.00 sec
 4/11 Test #4  test_queue .....................   Passed    0.01 sec
 5/11 Test #5  test_transport_sim .............   Passed    0.01 sec
 6/11 Test #6  test_pipeline ..................   Passed    0.18 sec
 7/11 Test #7  test_record_replay .............   Passed    0.07 sec
 8/11 Test #8  test_descriptor_ffi ............   Passed    0.04 sec
 9/11 Test #9  test_fuzz ......................   Passed    1.20 sec
10/11 Test #10 test_control_channel ...........   Passed    0.02 sec
11/11 Test #11 test_stress ....................   Passed   20.29 sec

100% tests passed out of 11
Total Test time (real) =  21.7 sec
```

90 test cases / 57,450 checks in total (`test_stress` alone: 10,000 frames, 40,017 checks;
`test_control_channel` 4,379; `test_fuzz` 8,547). The same suite is clean under
AddressSanitizer + UndefinedBehaviorSanitizer and ThreadSanitizer, including the stress and fuzz
binaries. Additional builds verified:

```sh
# Library only (no tools/tests) - the requested core build:
cmake -S driver -B build/driver-core -DEINSTAR_BUILD_TESTS=OFF -DEINSTAR_BUILD_TOOLS=OFF
cmake --build build/driver-core --target einstar_core -j16     # exit 0

# Simulated path only, no libusb dependency:
cmake -S driver -B build/driver-nolusb -DEINSTAR_WITH_LIBUSB=OFF
cmake --build build/driver-nolusb -j16                          # exit 0

# ASan + UBSan (all tests except the 10k stress):
cmake -S driver -B build/driver-asan -DEINSTAR_SANITIZE=ON
cmake --build build/driver-asan -j16 && ctest --test-dir build/driver-asan -E stress
# 7/7 passed; the 10k-frame stress test also passes under ASan/UBSan (exit 0)
```

### Record / replay

```sh
./build/driver/einstar-record-replay record --out /tmp/einstar_clean.e1r \
    --frames 30 --seed 7 --faults clean
./build/driver/einstar-record-replay play --in /tmp/einstar_clean.e1r --queue-depth 8
./build/driver/einstar-record-replay dump --in /tmp/einstar_clean.e1r
```

Actual output:

```
recorded /tmp/einstar_clean.e1r
  transfers        : 946
  bytes            : 39356160
  digest (FNV-1a64): 0x73496957272E97A5
  frames completed : 30
  fault preset     : clean
replay /tmp/einstar_clean.e1r
  recorded bytes : 39356160
  replayed bytes : 39356160
  recorded digest: 0x73496957272E97A5
  replayed digest: 0x73496957272E97A5
  byte-exact     : PASS
  frames         : 30 (reassembly completed 30)
  packets parsed : 960
  parse errors   : 0
```

`--faults hostile` (loss, short reads, corrupt lengths, duplicate headers,
stalls, timeouts, truncated frames) also round-trips byte-exactly; it exercises
the resync/recovery paths instead of clean frames.

## Benchmark (SIMULATED - not a hardware measurement)

`einstar-bench` separates the link model explicitly: **USB 2.0 HS** (~40 MB/s practical),
a **hypothetical USB 3.0 SS** (~350 MB/s), the **vendor 68 ms trigger cadence**, and a
**host-only** ceiling (`--scenarios-only`). The generic 300 MB/s model is used only for the
relative depth/copy tables and is never a scanner result. Frames are 1280x1024 grayscale
(1,310,720 B, 32 x 40,960 B chunks) with 500 us per-URB service latency and a virtual clock.
`host_ms` / `host_fps` are the only real (wall-clock) numbers; they measure this machine's CPU
cost of parsing, reassembling and callback delivery. Full analysis:
`docs/benchmarks/offline_benchmarks.md`.

```
SIMULATED RESULTS - no scanner, no CUDA, no network. Device model: 300 MB/s link, 40.0 ms trigger period, 1280x1024 frames.
'sync' = one outstanding transfer. host_ms/host_fps are real wall-clock measurements of this machine; everything else is virtual-clock.

experiment mode       xfer depth copy      frames MB/s(sim) fps(sim)    p50us    p95us    p99us    maxus  drops  qpeak   host_ms   host_fps
---------------------------------------------------------------------------------------------------------------------------------------------------
depth      sync   0x00A400     1 copy         120      32.9    25.10    639.9    639.9  20801.6  20801.6      0      1      33.5     3583.2
depth      async  0x00A400     2 copy         120      33.0    25.14    779.9  28301.6  28941.6  28941.6      0      1      19.4     6174.9
depth      async  0x00A400     4 copy         120      33.0    25.17    919.8  32581.5  33221.5  33221.5      0      1      20.3     5922.2
depth      async  0x00A400     8 copy         120      33.0    25.18   1339.7  35141.3  35781.2  35781.2      0      1      21.9     5483.0
depth      async  0x00A400    16 copy         120      33.0    25.18  35861.4  37760.9  37900.8  37900.8      0      1      25.0     4792.3
copy-vs-zero async  0x00A400     2 zero-copy    120      33.0    25.14    779.9  28301.6  28941.6  28941.6      0      1      20.5     5839.7
copy-vs-zero async  0x00A400     8 zero-copy    120      33.0    25.18   1339.7  35141.3  35781.2  35781.2      0      1      17.6     6826.3
copy-vs-zero async  0x00A400    16 zero-copy    120      33.0    25.18  35861.4  37760.9  37900.8  37900.8      0      1      17.5     6868.8
xfer-size  async  0x004000     8 copy         120      33.0    25.16    773.1  31349.4  31567.9  31567.9      0      1      20.9     5730.3
xfer-size  async  0x00A000     8 copy         120      33.0    25.17   1319.2  35086.7  35223.2  35223.2      0      1      21.3     5639.0
xfer-size  async  0x00A400     8 copy         120      33.0    25.18   1339.7  35141.3  35781.2  35781.2      0      1      21.3     5636.3
xfer-size  async  0x040000     8 copy         120      33.0    25.18  76504.7  78252.4  78252.4  78252.4      0      1      23.4     5138.6
saturated  sync   0x00A400     1 copy         240      65.5    50.01    639.9    639.9    639.9    639.9      0      1      34.5     6965.8
saturated  async  0x00A400     2 copy         240     107.6    82.07    639.9    779.9    779.9    779.9      0      1      35.7     6731.6
saturated  async  0x00A400     4 copy         240     158.3   120.79    779.9   1059.8   1059.8   1059.8      0      1      35.8     6699.1
saturated  async  0x00A400     8 copy         240     207.2   158.05   1059.8   1619.6   1619.6   1619.6      0      1      38.1     6296.5
saturated  async  0x00A400    16 copy         240     245.0   186.92   1619.6   2739.1   2739.1   2739.1      0      1      46.4     5167.7

JSON written to build/driver/bench_results.json
```

Reading the numbers:

- **Triggered mode** is capped by the 40 ms trigger: about 25 fps / 33 MB/s at
  every depth. Deeper queues do not raise throughput; they only keep more
  reads outstanding. The high p95/p99 here is **time waiting for the next
  trigger**, not USB service time (a read submitted between triggers is
  modelled as a held bulk-IN URB). The p50 shows the per-URB service cost.
- **Saturated mode** removes the trigger gap to expose the pipeline ceiling:
  65.5 -> 245.0 MB/s for depth 1 -> 16 (the model approaches the 300 MB/s link
  as depth grows; depth 32 would be closer still).
- **Zero-copy** retains transfer buffers as frame chunks instead of copying
  them into a contiguous frame. Device-model throughput is unchanged, but the
  measured host cost drops by roughly 30% (depth 16: 25.0 ms -> 17.5 ms wall
  time for 120 frames; ~4.8k -> ~6.9k frames/s of host processing).
- **Transfer size**: `0x40000` reads reach the link ceiling in one batch but
  span several frames, so their per-read latency is the highest. `0xA400`
  matches the vendor driver's transfer size and is the default.
- `drops` is 0 and `qpeak` is 1 for every clean benchmark row; the JSON records
  drops and peak frame-queue occupancy per row.

The bench writes machine-readable rows (`schema: einstar-bench-v1`,
`"simulated": true`, `"hardware": false`, device model, and every row's
throughput/latency/occupancy) to `bench_results.json`.

## Design rationale

### Transport and determinism

`Transport` is the only place USB exists: open/claim, clear-halt, async bulk-IN
submission, cancellation, `poll()`, clock. Two implementations:

- `SimulatedTransport`: generates the vendor byte stream
  (`[u32 BE len][32-byte header][payload]`), models a shared 300 MB/s link,
  per-URB latency, a 40 ms trigger (bytes of frame N become readable at
  `N * 40 ms`), and injects deterministic faults from a seed. It uses a
  `VirtualClock`; `poll()` advances virtual time to the next scheduled event,
  so no test depends on wall time and repeated runs are bit-identical.
- `LibUsbTransport`: asynchronous libusb transfers (one `libusb_transfer` per
  submission), completion callbacks, `libusb_cancel_transfer`, descriptor
  inspection, clear-halt recovery, and error mapping.

The simulator's fault set covers loss, short reads (a packet split across
transfers, exercising the framer's carry buffer), stalls (endpoint halts until
`clear_halt`), timeouts, corrupt length prefixes (framer resynchronisation),
duplicated header-only packets, truncated frames, injected latency and
cross-frame packet reordering (packets of consecutive frames are interleaved;
intra-frame reordering has no wire chunk index and is therefore unrecoverable).

### Async acquisition

- Configurable `queue_depth` outstanding reads; preallocated `BufferPool`
  blocks with intrusive-refcount `BufferHandle` ownership; no allocation on
  the data path after `start()`.
- A lock-free SPSC completion ring hands completions from the transport poll
  thread to the pipeline; a safe inline fallback processes a completion
  directly if the ring ever fills, so a completion is never lost.
- `Copy` mode assembles one contiguous frame buffer (one memcpy per packet of
  payload). `ZeroCopy` mode retains the transfer buffers as frame chunks: a
  `Frame` is a scatter list of `{BufferHandle, offset, size}` and
  `copy_payload_to()` / `materialize()` gather only when a consumer needs a
  linear image. This is the zero-copy path used by the C++ engine.
- Frames are delivered through a bounded queue with an explicit policy:
  `DropOldest` (live preview default), `DropNewest`, or `Block` (lossless
  consumers; requires a consumer thread). Occupancy, peak and drop counters are
  exported.
- `stop()` cancels every outstanding transfer, drains exactly one completion
  per transfer, flushes partial frames and returns an error if anything was
  not reclaimed. Tests assert `transport.outstanding() == 0` and pool use
  returns to zero.

### Reassembly

Frames are keyed by `(camera_id, frame_index)`, not by arrival order, so
per-frame packets from multiple cameras may interleave. `header[8] == 1` marks
the first packet of a frame (per the Windows DLL evidence; note that
`libeinstar` interpreted the same byte as a *last* flag - this backend follows
the Windows evidence). A frame completes when exactly `frame_size` payload
bytes have been accumulated. The reassembler detects:

- sequence gaps using frame **start order** (so cross-frame interleaving is not
  misreported as loss),
- duplicate header-only packets, orphan packets (first packet lost),
  restarts, overflow, stale frames and capacity eviction,
- incomplete frames via a configurable partial-frame timeout.

There is no chunk index in the vendor header, so intra-frame packet
reordering/loss is only observable through a short frame (timeout), never
silently accepted as a complete frame.

### Recording

Append-only file (all integers little-endian), with a 64-byte header and
records of `36-byte header + payload + u32 CRC32`:

```
file:  "EINSTAR1" | version u16 | header_size u16 | flags u32 |
       start_host_ns u64 | ... | header_crc u32
record: magic u32 | record_bytes u32 | type u8 | status u8 | flags u16 |
        sequence u32 | host_time_ns u64 | device_timestamp u64 |
        payload_len u32 | payload | crc32 (record header + payload)
trailer payload: digest u64 (FNV-1a over all recorded transfer bytes),
                 transfer_count u64, byte_count u64, reserved u64
```

A dedicated writer thread with its own bounded queue decouples disk I/O from
acquisition; the default policy is `Block` (lossless) and drops are counted if
another policy is selected. `play` re-reads the file, validates every record
CRC and the trailer digest, feeds the exact bytes through
`SimulatedTransport` and confirms byte-for-byte equality (see output above).
`record_replay` also supports `--faults lossy|hostile` to record recovery paths.

### Instrumentation

`PipelineStats` exposes transfer/byte/packet/frame counters, timeouts, stalls,
cancellations, resubmits, parse errors/resync bytes, reassembly errors,
sequence gaps, partial timeouts, queue drops, pool occupancy/peak, recorder
counters and per-transfer/frame latency histograms (min, p50, p95, p99, max,
mean). Percentiles are nearest-rank over a bounded ring of recent samples
(deterministic for identical inputs).

## The Linux `bInterval = 0` quirk (corrected)

The Einstar declares `bInterval = 0` on its interrupt endpoints (`0x01`, `0x81`), which is
invalid per USB 2.0. **No kernel patch, udev rule or user-space descriptor override is needed:**
Linux USB core repairs the value before any driver is involved.

`drivers/usb/core/config.c::usb_parse_endpoint()`:

```c
case USB_SPEED_HIGH:
    n = fls(d->bInterval * 8);
    if (n == 0)
        n = 7;              /* 8 ms = 2^(7-1) uframes */
    j = 16;
...
if (d->bInterval < i || d->bInterval > j) {
    dev_notice(..., "has an invalid bInterval %d, changing to %d", ..., n);
    endpoint->desc.bInterval = n;      /* kernel schedules with 7 */
}
```

Consequences for this component:

* `UsbConfigDescriptor::parse()` still detects the raw value 0 and exposes
  `zero_interval_interrupt_endpoints()` / `quirk_notes()` for diagnostics — libusb reports the
  raw descriptor even though the kernel schedules with 7.
* Claiming the interface **never** depends on a patch. The simulator's old "refuse to claim
  until patched" gate was wrong and has been removed; `apply_descriptor_patch()` remains as a
  diagnostic/experiment helper.
* This matches the field observation: libeinstar captured images on a stock CachyOS kernel with
  no modification.

## Integration boundary

The C ABI is `include/einstar/ffi.h` (built as `libeinstar.so`, output name
`einstar`):

```c
einstar_config_init(&config);                 // ABI version + struct_size
einstar_pipeline* p = einstar_pipeline_create(&config, &error);
einstar_pipeline_start(p, &error);
einstar_pipeline_poll(p, timeout_ms, &error); // drive from the control plane
einstar_pipeline_read_frame(p, &view, timeout_ms, &error); // ring export
einstar_frame_copy_payload(p, &view, dst, cap, &error);    // gather if chunked
einstar_pipeline_release_frame(p, view.token, &error);
einstar_pipeline_stop(p, &error);
einstar_pipeline_destroy(p);
```

- **Rust control plane**: bindgen the header; the opaque `einstar_pipeline*` is
  the only handle. `einstar_config` carries `struct_size`/`abi_version`;
  errors are stable numeric codes plus a 256-byte message. The control plane
  owns device lifecycle, scan settings, recording paths and firmware updates;
  it may run `poll` on a dedicated thread or call `read_frame` from a consumer
  thread. Callbacks (`einstar_pipeline_set_callback`) and ring export are
  mutually exclusive.
- **Qt 6 GUI**: only via the Rust control plane (its IPC/FFI), never against
  `Transport` or libusb. The GUI receives frame views/metadata through the
  control plane's frame export.
- **C++23 / CUDA reconstruction engine**: link `einstar_core` and consume
  `einstar::Frame` directly. In `ZeroCopy` mode `Frame` holds `BufferHandle`
  chunks (device buffers retained, no memcpy); the engine can register those
  spans with CUDA (host-pinned staging is an engine concern) or call
  `copy_payload_to()`/`materialize()` for a linear image. `Frame` is movable
  and can be handed across a ring buffer; its buffers return to the pool when
  the last handle is released. A frame ring is the bounded `Frame` queue.
- **TensorRT**: same `Frame` path and ring; bind the input buffer to the
  contiguous frame (Copy mode) or gather chunks into the TensorRT input buffer
  (ZeroCopy mode). No separate transport path exists for engines.
- **Firmware updater (control plane responsibility)**: verified facts from the
  FX3 work are: the application-flag sector is at flash **0x3F000** (not
  0x3F0), and the update payload length is **argument 2** and must be
  read-back verified. This acquisition component does not implement flash
  writes; keep those rules in the control-plane updater.

Typical data flow:

```
Qt6 GUI ── control plane (Rust) ── C ABI libeinstar.so ── AcquisitionPipeline
                                                             ├─ LibUsbTransport (device)
                                                             └─ SimulatedTransport (tests/bench)
C++23/CUDA/TensorRT engines ── einstar::Frame ring (zero-copy BufferHandle chunks)
```

## Test coverage map

| Test binary | Focus |
|---|---|
| `test_packet` | framing across transfer boundaries, split header/payload, corrupt length resync, header-only packets, unknown cameras |
| `test_reassembler` | chunk-boundary completion, multi-camera interleave, gaps, partial timeout, duplicates/orphans/restarts, overflow, capacity, zero-copy chunks, flush |
| `test_queue` | drop-oldest/drop-newest/block, close semantics, SPSC ring wrap + multithread FIFO/sum |
| `test_transport_sim` | determinism, byte stream, short reads, stalls + clear-halt, timeouts, cancellation, quirk patch, replay, fault-storm determinism |
| `test_pipeline` | clean capture, zero-copy, drop-oldest backpressure, partial timeout, cancellation with zero leaked transfers, stall/timeout recovery, record+replay, callback mode, cross-frame reordering, repeatability |
| `test_record_replay` | CRC vector, round-trip metadata/statuses, corruption/truncation detection, record -> replay byte equality |
| `test_descriptor_ffi` | descriptor parse/patch/malformed inputs, C ABI config/read/copy/release/stats/callback/error paths |
| `test_stress` | 10,000 x 1280x1024 zero-copy frames, per-frame digest, all continuity counters zero, pool bounded and drained |

## Limitations / hardware bring-up checklist

- No physical-device validation. `LibUsbTransport` is compile/link verified;
  `SimulatedTransport` is the tested backend.
- This component covers **EP 0x83 acquisition**. The command channel
  (`0x01/0x81` interrupt request/reply, page reads, trigger/exposure commands)
  is not implemented here; the existing libeinstar control layer remains the
  command-plane reference. The `Transport` interface and descriptor handling
  are ready for it.
- `bInterval = 0` is repaired by Linux USB core (see above); no kernel change is required.
- Device timestamp units/epoch are unknown; the u64 is preserved verbatim and
  host monotonic time is recorded separately.
- Framing follows the verified semantics: one-hot camera in `header[2]` bits 3..5, sub-index in
  bits 0..2, `header[8] == 1` on the **last** packet, frame id `header[12..15]` valid only on
  that packet, timestamp `header[16..23]` latched from the first packet. Locked by
  `tests/test_conformance.cpp` with hand-written vectors.
- The command channel (interrupt EP1 request/reply: serial, firmware string, exposure, gain,
  trigger, page read) is implemented in `control_channel.*`; see `CONTROL_CHANNEL.md`. Firmware
  update and persistent flash writes are deliberately out of scope.
- Without a chunk index, intra-frame reordering cannot be repaired; the
  simulator only reorders across frames/cameras.
- Zero-copy retention couples the transfer pool to consumer speed. If the
  consumer stalls, submissions stop (reported as `submission_starved`) rather
  than overwriting live frames; size the pool for the frame queue you need.
- Benchmarks use a virtual clock for device timing; they cannot predict real
  USB scheduling, xHCI behaviour or kernel jitter.
