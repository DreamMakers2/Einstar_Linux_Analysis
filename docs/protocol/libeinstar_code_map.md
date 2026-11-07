# libeinstar — code map, build and test record

Audit target: `<libeinstar>`, commit
`4564bb19d2b6385016bb355c706245d26cdece87` ("docs: mark libeinstar as tested on
hardware", 2026-10-30), plus two **uncommitted** one-line include fixes in the
working tree (`src/device.cpp`, `tests/fake_scanner.hpp`).

Read-only: no file in `<libeinstar>` was modified. All builds were done
from copies under `<repo>/tests/`.

---

## 1. Repository shape and provenance

| Item | Value |
|---|---|
| Upstream | hardparking/libeinstar (per task brief) |
| Audited commit | `4564bb19…`, 2026-10-30 |
| Working-tree state | `M src/device.cpp`, `M tests/fake_scanner.hpp` (both add `#include <algorithm>`), untracked nested clone `libeinstar/` |
| License | BSD-3-Clause (`LICENSE`) |
| Build system | CMake ≥ 3.20, C++17, Ninja/Make; targets `einstar_core`, `einstar`, optional `einstar_depth`, tools, tests, optional RTAB-Map |
| Tested by author on | macOS, one Einstar, firmware `EinScan10_01_SC130_FX3_V2.14_FPGA_V3.7_EN` (`README.md:9-11`) |
| Firmware images in this workspace | `EinScan10_01_SC130_FX3_V2.10_FPGA_V3.7_{EN,CH}_IAP.img` |

Note: `<libeinstar>/libeinstar/` is a second, untracked checkout of the
same project (clean `HEAD`, no include fixes, contains its own `build/`). It is
not part of the audited tree; the parent working tree is authoritative. Its
`src/device.cpp` lacks the `<algorithm>` include and therefore does not build on
GCC 16 either.

---

## 2. Module map

```
include/einstar/ + src/
├── protocol.hpp/.cpp     wire constants, packet codec, reply validation, XOR mask
├── stream.hpp/.cpp       EP 0x83 32-byte-header frame reassembly
├── calibration.hpp/.cpp  CCF / .data_cm / device-blob parsing, Pose math
├── exposure.hpp/.cpp     host-side IR auto-exposure control loop
├── device.hpp/.cpp       libusb transport, handshake, transact/retry, page I/O,
│                         stream thread, trigger/lighting sequencing   (only libusb user)
├── depth.hpp/.cpp        IR stereo → depth in colour camera (OpenCV; optional)
├── result.hpp            Result<T>/Status/Error/ErrorCode
├── span.hpp              minimal C++17 span (ByteView/MutableByteView)
└── transport.hpp         Transport interface (test seam; libusb impl in device.cpp)

tools/                    einstar-probe, einstar-calib, einstar-depth, einstar-rgbd
integrations/rtabmap/     CameraEinstar (RTAB-Map camera), einstar-rtabmap, einstar-synth
tests/                    einstar-tests (40 checks) + einstar-depth-test
scripts/                  einstar-bringup.sh (hardware bring-up bundle)
docs/                     PROTOCOL.md, HARDWARE_BRINGUP.md
```

`einstar_core` is pure (no I/O): `protocol.cpp`, `stream.cpp`, `calibration.cpp`,
`exposure.cpp`. `einstar` adds `device.cpp` and links libusb. `einstar_depth`
adds `depth.cpp` and OpenCV; it is absent when OpenCV is not found.

---

## 3. File inventory

| File | Lines | Role | Protocol relevance |
|---|---:|---|---|
| `include/einstar/protocol.hpp` | 183 | constants, enums, codec API | all opcodes, packet offsets, wire sizes, mask, reply decoders |
| `src/protocol.cpp` | 202 | codec implementation | build/validate packets, BE readers, mask, strings, temperature |
| `include/einstar/stream.hpp` | 67 | stream header constants, `FrameInfo`, `FrameAssembler` | 32-byte header, camera field, last flag, frame id, timestamp |
| `src/stream.cpp` | 58 | frame assembly | transfer→frame state machine |
| `include/einstar/calibration.hpp` | 119 | calibration types/constants | CCF sizes, device blob size/offset, stream↔camera map, orientation |
| `src/calibration.cpp` | 193 | parsers | `.data_cm`, CCF, device block, IR key, Pose math |
| `include/einstar/exposure.hpp` | 55 | AE parameters/class | host-side only |
| `src/exposure.cpp` | 46 | AE algorithm | histogram/ratio control |
| `include/einstar/device.hpp` | 141 | `Device`, `DeviceInfo`, `ScanSettings`, `OpenOptions` | command API, defaults, stream API |
| `src/device.cpp` | 506 | libusb transport + sequencing | handshake, transact/retry, page bulk, stream thread, trigger, lighting |
| `include/einstar/depth.hpp` | 112 | `DepthParams`, `StereoDepth` | stereo→colour depth, orientation helpers |
| `src/depth.cpp` | 244 | depth implementation | rectification, SGBM, splatting, Bayer conversion |
| `include/einstar/transport.hpp` | 29 | transfer abstraction | test seam, status codes |
| `include/einstar/result.hpp` | 46 | error/Result types | error taxonomy |
| `include/einstar/span.hpp` | 43 | span | byte views |
| `tools/einstar-probe.cpp` | 259 | CLI probe/capture | handshake, getters, flash dump, frames, AE |
| `tools/einstar-calib.cpp` | 54 | calibration converter | prints OpenCV YAML |
| `tools/einstar-depth.cpp` | 103 | single depth image tool | depth pipeline |
| `tools/einstar-rgbd.cpp` | 257 | RGB-D recorder/replay | live scan, calibration, depth+colour |
| `integrations/rtabmap/CameraEinstar.cpp` | 436 | RTAB-Map camera | frame pairing, depth, recording |
| `integrations/rtabmap/include/.../CameraEinstar.h` | 98 | camera API | — |
| `integrations/rtabmap/einstar_rtabmap.cpp` | 160 | RTAB-Map scan app | odometry/ICP params |
| `integrations/rtabmap/einstar_synth.cpp` | 182 | synthetic sequences | test data |
| `tests/fake_scanner.hpp` | 199 | simulated device | protocol model used by tests |
| `tests/test_protocol.cpp` | 221 | 19 checks | byte layouts, reply decode, mask, AE |
| `tests/test_stream.cpp` | 64 | 5 checks | assembly state machine |
| `tests/test_calibration.cpp` | 166 | 7 checks | `.data_cm`, CCF, device blob |
| `tests/test_device.cpp` | 187 | 9 checks | handshake/retry/writes/pages/stream |
| `tests/test_depth.cpp` | 285 | standalone depth test | synthetic stereo accuracy |
| `tests/test_main.cpp` / `check.hpp` | 20 / 41 | test harness | — |
| `scripts/einstar-bringup.sh` | 79 | hardware bring-up | runs probe/calib/replay, bundles logs |
| `docs/PROTOCOL.md` | 253 | protocol notes | provenance tags `[hw]/[der]/…` |
| `docs/HARDWARE_BRINGUP.md` | 135 | bring-up guide | expected values, failure table |

---

## 4. Class and function map

### 4.1 `protocol.hpp` / `protocol.cpp` — codec

| Symbol | Location | Role |
|---|---|---|
| `kVendorId`, `kInterface`, `kEp*` | `protocol.hpp:18-25` | `0x3267`, interface 0, EPs `01/81/02/82/83` |
| `kCommandSize`, `kHandshakeSize`, `kHandshakeByte` | `protocol.hpp:27-29` | 16, 100, `0xFF` |
| `kPageTransferSize`, `kPageSize`, `kStreamPackageSize` | `protocol.hpp:30-32` | `0x1400`, `0x1000`, `0xA400` |
| `kPacketIdModulus` | `protocol.hpp:33` | 254 |
| `kOff*`, `kOffReply*`, `kReplyMarker`, `kMaxPayload` | `protocol.hpp:35-46` | packet/reply offsets |
| `Group`, `SystemOpcode`, `Opcode` | `protocol.hpp:49-92` | 2 groups, 4 + 28 opcodes |
| `Command`, `command()` | `protocol.hpp:94-99` | group+code pair |
| `is_read_only`, `is_forbidden` | `protocol.cpp:7-40` | write policy |
| `wire_size` | `protocol.cpp:42-67` | 10/16/50/64/100 |
| `build_command` | `protocol.cpp:69-78` | 16-byte request, ≤8-byte payload |
| `to_string(ReplyError)` | `protocol.cpp:80-89` | error text |
| `validate_reply` | `protocol.cpp:91-99` | id/group/opcode/status only |
| `camera_mask` | `protocol.cpp:101-103` | `1 << cam` |
| `pack/unpack_trigger_counts` | `protocol.cpp:105-111` | RGB high nibble, mono low |
| `reply_string` | `protocol.cpp:113-121` | NUL-terminated value |
| `PacketIdCounter::next` | `protocol.cpp:123-127` | 0..253 rolling |
| `read_be16/32/64`, `write_be16/32` | `protocol.cpp:129-152` | big-endian primitives |
| `reply_value`, `reply_uint` | `protocol.cpp:154-166` | length-clamped value views |
| `is_handshake_reply` | `protocol.cpp:168-171` | `"Shining3"` at 9..16 |
| `reply_serial` | `protocol.cpp:173-183` | first 8 value bytes as `%02X` |
| `decode_temperature_raw` | `protocol.cpp:185-188` | i16 BE `>> 7` |
| `mask_encode`, `mask_decode` | `protocol.cpp:190-200` | key `n|(n<<4)` |

### 4.2 `stream.hpp` / `stream.cpp` — stream reassembly

| Symbol | Location | Role |
|---|---|---|
| `kStreamHeaderSize`, `kStreamPayloadOffset` | `stream.hpp:20-21` | 0x20 |
| `kOffStreamCamera/LastFlag/FrameId/Timestamp` | `stream.hpp:22-25` | 0x02/0x08/0x0c/0x10 |
| `FrameInfo`, `Frame` | `stream.hpp:27-39` | metadata + payload |
| `FrameInfo::camera_index` | `stream.cpp:9-16` | 1/2/4 → 0/1/2 |
| `StreamEvent` | `stream.hpp:41-47` | Accepted/FrameComplete/TooShort/SizeMismatch/Overflow |
| `FrameAssembler` ctor/reset/push/take | `stream.cpp:18-56` | single-buffer assembly |
| `Device::stream_loop` | `device.cpp:489-504` | owns the only assembler |

### 4.3 `calibration.hpp` / `calibration.cpp` — calibration

| Symbol | Location | Role |
|---|---|---|
| `kCameraModelFileSize` | `calibration.hpp:26` | 692 |
| `kColorCamera/kIrLeft/kIrRight` | `calibration.hpp:29-31` | 0/1/2 |
| `calibration_index_for_stream` | `calibration.hpp:36-38` | 0→2, 1→1, 2→0 |
| `stream_orientation` | `calibration.hpp:43-45` | 180° for streams 0 and 2 |
| `Pose` | `calibration.hpp:50-56` | R/T, inverse, compose, apply |
| `CameraIntrinsics`, `CameraCalibration`, `CameraModel` | `calibration.hpp:58-80` | model |
| `CalibrationError`, `CalibrationResult` | `calibration.hpp:82-88` | errors |
| `le_double`, `le_i32` | `calibration.cpp:13-25` | little-endian readers |
| `kCameraOffsets{4,38,60}` | `calibration.cpp:27` | `.data_cm` camera blocks |
| `kCcfFields=32` | `calibration.cpp:30` | CCF field count |
| `kCcfIrKey[32]` | `calibration.cpp:33-34` | IR de-obfuscation key |
| `read_ccf` | `calibration.cpp:36-48` | CCF field mapping |
| `kPairOffset=26` | `calibration.cpp:54` | `.data_cm` pair block |
| `read_camera` | `calibration.cpp:56-66` | 22-double camera block |
| `transpose`, `is_rotation` | `calibration.cpp:68-81` | orthonormality gate |
| `determinant` | `calibration.cpp:85-88` | rotation check |
| `Pose::apply/inverse/operator*` | `calibration.cpp:90-115` | pose math |
| `CameraModel::relative` | `calibration.cpp:117-119` | `to ∘ from⁻¹` |
| `parse_camera_model` | `calibration.cpp:131-144` | 692-byte `.data_cm` |
| `load_camera_model` | `calibration.cpp:146-148` | file wrapper |
| `parse_exstar_ccf` | `calibration.cpp:150-162` | 264/520/520 bytes |
| `parse_device_calibration` | `calibration.cpp:164-179` | blob → block → CCF |
| `load_calibration` | `calibration.cpp:181-186` | path sniffer |
| `load_exstar_ccf` | `calibration.cpp:188-191` | directory wrapper |

### 4.4 `exposure.hpp` / `exposure.cpp` — auto-exposure

| Symbol | Location | Role |
|---|---|---|
| `AutoExposureParams` | `exposure.hpp:24-36` | 11 tuning constants |
| `AutoExposure::update` (single) | `exposure.cpp:34-38` | one image |
| `AutoExposure::update` (pair) | `exposure.cpp:40-44` | min of both wanted |
| `AutoExposure::wanted` | `exposure.cpp:9-32` | percentile histogram + ratio |

### 4.5 `device.hpp` / `device.cpp` — transport and sequencing

| Symbol | Location | Role |
|---|---|---|
| `DeviceInfo` | `device.hpp:27-34` | VID/PID/bus/port/product/heuristic |
| `ScanSettings` | `device.hpp:38-46` | lighting/exposure defaults |
| `OpenOptions` | `device.hpp:48-53` | `allow_writes`, `mask_commands`, `timeout_ms=1000`, `retries=5` |
| `LibusbTransport` | `device.cpp:19-49` | sync transfers, clear_halt, describe |
| `product_string` | `device.cpp:51-59` | descriptor string |
| `looks_like_einstar` | `device.cpp:63-65` | `SHINING-CAM` / `EINSCAN10` / `EinScan10` |
| `to_string(ErrorCode)` | `device.cpp:69-80` | error text |
| `Device::enumerate` | `device.cpp:82-107` | VID scan |
| `Device::open` (USB) | `device.cpp:109-163` | select, open, claim |
| `Device::open` (transport) | `device.cpp:165-179` | handshake + serial |
| `Device::~Device` | `device.cpp:181` | `stop_stream()` only |
| `transfer_error` | `device.cpp:183-186` | libusb rc → Error |
| `Device::handshake` | `device.cpp:188-212` | drain/clear/0xFF/marker |
| `require_writes` | `device.cpp:214-220` | policy gate |
| `Device::transact` | `device.cpp:222-257` | packet build, retry, mask |
| `Device::set` | `device.cpp:259-263` | status-only wrapper |
| `product_name`, `firmware_version` | `device.cpp:265-275` | system strings |
| `query_uint`, `query_camera` | `device.cpp:277-288` | numeric getters |
| `sensor_count` | `device.cpp:290-294` | `0x51` |
| `exposure_us` | `device.cpp:296` | `0x22` |
| `gain` | `device.cpp:298-302` | `0x26` |
| `max_width`, `max_height` | `device.cpp:304-306` | `0x16/0x17` |
| `trigger_period` | `device.cpp:308` | `0x48` |
| `temperature_raw` | `device.cpp:310-315` | `0x50`, requires 2-byte value |
| `trigger_counts` | `device.cpp:317-321` | `0x40` |
| `bulk_exchange` | `device.cpp:327-369` | 1 KiB chunked page I/O |
| `read_page` | `device.cpp:371-393` | `0x57`, 4096 B |
| `read_custom_info` | `device.cpp:395-405` | pages until size |
| `read_calibration` | `device.cpp:407-413` | 6568 B → parser |
| `set_exposure_us` | `device.cpp:415-419` | mask + u32 |
| `set_gain` | `device.cpp:421-425` | mask + u16 |
| `set_trigger_period` | `device.cpp:427-431` | u32 |
| `set_strobe_brightness` | `device.cpp:433-437` | route + u16 |
| `set_laser_brightness` | `device.cpp:439-442` | u8 clamp 100 |
| `set_trigger_counts` | `device.cpp:444-450` | period 200000, counts, 1 s settle |
| `start_scanning` | `device.cpp:452-455` | counts 1/1 then period |
| `stop_scanning` | `device.cpp:457` | counts 0/0 |
| `apply` | `device.cpp:459-469` | all cameras + lights |
| `lights_off` | `device.cpp:471-475` | routes 0, laser 0 |
| `start_stream` | `device.cpp:477-482` | thread spawn |
| `stop_stream` | `device.cpp:484-487` | flag + join |
| `stream_loop` | `device.cpp:489-504` | EP 0x83 → assembler → callback |

### 4.6 `depth.hpp` / `depth.cpp` — stereo depth (optional)

| Symbol | Location | Role |
|---|---|---|
| `DepthParams` | `depth.hpp:30-68` | scale 0.5, 130–800 mm, block 5, stretches `{1.0,0.6}` |
| `StereoDepth` ctor | `depth.cpp:86-150` | left/right choice, rectification, masks, disparity window, colour maps |
| `rectify` | `depth.cpp:152-155` | remap to rectified scaled images |
| `disparity` | `depth.cpp:157-198` | CLAHE, multi-stretch SGBM, merge |
| `depth_in_color` | `depth.cpp:200-236` | disparity→Z→colour projection, splat |
| `undistort_color` | `depth.cpp:238-242` | colour remap |
| `camera_matrix`, `distortion_coeffs` | `depth.cpp:12-19` | OpenCV forms |
| `to_calibration_orientation` | `depth.cpp:21-26` | 180° rotate |
| `color_in_calibration_orientation` | `depth.cpp:28-32` | BayerRG→BGR + rotate |
| `match` | `depth.cpp:64-82` | padded SGBM invocation |
| `rectified_bounds` | `depth.cpp:44-54` | view bounds for principal points |

### 4.7 Tools

| Tool | Key functions | Lines |
|---|---|---|
| `einstar-probe` | `main` 57; `write_pgm` 47; `report` 42; frame capture/epoch logic 174-217; AE 190-200 | 259 |
| `einstar-calib` | `main` 22; `print_matrix` 14 | 54 |
| `einstar-depth` | `main`/depth loop | 103 |
| `einstar-rgbd` | `Writer` 51-96; `replay` 98-120; `live` 122-189; `main` 193-257 | 257 |

### 4.8 RTAB-Map integration

| Symbol | Location | Role |
|---|---|---|
| `CameraEinstar::Impl` | `CameraEinstar.cpp:64-177` | device, depth, buffers, AE, record |
| `Impl::onFrame` | `:97-134` | IR pair by frame id; `lastColor` pairing; discard 2 sets |
| `Impl::nextLive` / `nextReplay` | `:136-176` | queue / PGM replay |
| `CameraEinstar::init` | `:220-339` | open/calibrate/start stream + trigger |
| `CameraEinstar::captureImage` | `:361-434` | depth, record, rotate output |
| `einstar_rtabmap` `main` | `einstar_rtabmap.cpp:55-160` | odometry/ICP, DB, poses |
| `einstar_synth` | `einstar_synth.cpp` | synthetic RGB-D sequences |

---

## 5. Protocol constant index

| Constant | Value | File:line |
|---|---|---|
| Vendor ID | `0x3267` | `protocol.hpp:18` |
| Interface | `0` | `protocol.hpp:19` |
| EP command out/in | `0x01` / `0x81` | `protocol.hpp:21-22` |
| EP bulk out/in | `0x02` / `0x82` | `protocol.hpp:23-24` |
| EP stream in | `0x83` | `protocol.hpp:25` |
| Command packet | 16 B | `protocol.hpp:27` |
| Handshake | 100 B, byte0 `0xFF` | `protocol.hpp:28-29` |
| Page transfer/page | `0x1400` / `0x1000` | `protocol.hpp:30-31` |
| Stream transfer buffer | `0xA400` | `protocol.hpp:32` |
| Packet id modulus | `0xFE` | `protocol.hpp:33` |
| Packet offsets | id 0, group 2, opcode 3, status 4, len 7, payload 8 | `protocol.hpp:35-40` |
| Reply offsets | len 8, value 9, marker `0x02` | `protocol.hpp:43-45` |
| Handshake marker | `"Shining3"` at 9..16 | `protocol.hpp:168` |
| Stream header | `0x20`; camera `0x02`; last `0x08`; id `0x0c`; ts `0x10` | `stream.hpp:20-25` |
| Camera mask | `1 << cam` | `protocol.cpp:101-103` |
| Trigger pack | `rgb<<4 \| mono` | `protocol.cpp:105-107` |
| Device blob | `6568` B, block `0x39B`, block size `0xF21` | `calibration.hpp:109-110`, `calibration.cpp:167` |
| CCF sizes | Tex 264, IR 520 | `calibration.hpp:99-100` |
| CCF fields | 32 | `calibration.cpp:30` |
| IR key | 32 ints | `calibration.cpp:33-34` |
| `.data_cm` | 692 B; cameras 4/38/60; pair 26; tail int32 at 676 | `calibration.hpp:26`, `calibration.cpp:27,54,142` |
| Scan defaults | strobe 1000/300, laser 100, IR 4000 µs/105, colour 5000 µs/180 | `device.hpp:38-46` |
| AE clamps | 200–6000 µs | `exposure.hpp:33-34` |
| Depth | scale 0.5, 130–800 mm, block 5, stretches `{1,0.6}` | `depth.hpp:38-67` |

---

## 6. Call flows

### 6.1 Open

```
Device::open(optional<DeviceInfo>, OpenOptions)
  libusb_init → get_device_list → filter idVendor==0x3267
  [optional bus/port match] → libusb_open
  set_auto_detach_kernel_driver(1) → claim_interface(0)
  Device::open(unique_ptr<Transport>)
    handshake()                       device.cpp:188
      drain 5× interrupt-IN, 30× bulk-IN (10 ms)
      clear_halt(0x81), clear_halt(0x82)
      send 100 B (byte0=0xFF) EP 0x01, recv 100 B EP 0x81
      require "Shining3" at 9..16
    transact(SystemOpcode::Serial)    64-byte wire; reply_serial → 16 hex chars
```

### 6.2 Scan start (library)

```
start_scanning(90000)                  device.cpp:452
  set_trigger_counts(1,1)              device.cpp:444
    SetTriggerPeriod(200000)  0x49 wire 50
    SetTriggerSwitch(0x11)    0x41 wire 10
    (no sleep: rgb != 0)
  SetTriggerPeriod(90000)     0x49 wire 50
```

`stop_scanning()` → `set_trigger_counts(0,0)` → period 200000, counts 0x00,
sleep 1000 ms (`device.cpp:444-450,457`).

### 6.3 Calibration read

```
read_calibration()                     device.cpp:407
  read_custom_info(6568)               device.cpp:395
    read_page(0) → 4096 B
    read_page(1) → 4096 B (2472 used)
  parse_device_calibration(blob)       calibration.cpp:164
    block @0x39B, 0xF21 B; type==4; "FQFQ" @0xF1C
    slots 0x108/0x610/0xB18 → 520/520/264
    parse_exstar_ccf(tex,left,right)   calibration.cpp:150
      read_ccf ×3 (subtract kCcfIrKey for IR)
      is_rotation ×3 or NotARotation
```

### 6.4 Stream

```
start_stream(cb, 1280*1024)            device.cpp:477
  thread: stream_loop                  device.cpp:489
    bulk_transfer(0x83, buf[0xA400], 200 ms)
    FrameAssembler::push               stream.cpp:23
      header parse, last flag, size check, copy payload
    on FrameComplete → cb(Frame&&)     (USB thread)
stop_stream() → flag=false → join
```

---

## 7. Build and test record

### 7.1 Environment

```
Linux cachyos 7.3.1-1-cachyos x86_64, 32 CPUs
GCC 16.2.1, CMake 4.4.5, Ninja, libusb 1.0.30
OpenCV 5.0.0 present (module layout: calib + geometry, no calib3d)
No Einstar connected: `einstar-probe` → "0 device(s) with VID 0x3267"
```

### 7.2 Scratch build without OpenCV (the required build)

Source copy (original repo untouched):

```
mkdir -p <repo>/tests/libeinstar-scratch
cd <libeinstar>
tar --exclude=./build --exclude=./.git --exclude=./libeinstar -cf - . \
  | tar -xf - -C <repo>/tests/libeinstar-scratch
```

Configure/build/run:

```
cd <repo>/tests
cmake -S libeinstar-scratch -B build-libeinstar -G Ninja \
      -DEINSTAR_WITH_OPENCV=OFF -DEINSTAR_BUILD_RTABMAP=OFF \
      -DCMAKE_BUILD_TYPE=Release
cmake --build build-libeinstar
ctest --test-dir build-libeinstar --output-on-failure
./build-libeinstar/einstar-tests
```

Result: configure OK; 17/17 build steps OK; `ctest` **1/1 Passed (1.28 s)**;
direct binary: **40 tests, 0 failed checks** (`EXIT=0`). `einstar-probe` and
`einstar-calib` built; no `einstar-depth`/`einstar-rgbd` (OpenCV disabled).

### 7.3 Build with OpenCV 5

The stock CMakeLists requests `COMPONENTS core imgproc calib3d`. With
`-DEINSTAR_WITH_OPENCV=ON -DOpenCV_DIR=/usr/lib/cmake/opencv5`:

```
-- OpenCV not found: einstar::depth disabled
```

Reason: OpenCV 5.0 provides `opencv_calib` and `opencv_geometry`; no
`opencv_calib3d` module/component exists. This is a silent degradation.

Scratch-only patch (allowed; original untouched):

```
cp -a libeinstar-scratch libeinstar-scratch-cv
sed -i 's/COMPONENTS core imgproc calib3d/COMPONENTS core imgproc calib geometry/' CMakeLists.txt
sed -i 's/COMPONENTS imgcodecs/COMPONENTS imgcodecs core imgproc/' CMakeLists.txt
cmake -S libeinstar-scratch-cv -B build-libeinstar-cv2 -G Ninja \
      -DEINSTAR_WITH_OPENCV=ON -DOpenCV_DIR=/usr/lib/cmake/opencv5 \
      -DCMAKE_BUILD_TYPE=Release
cmake --build build-libeinstar-cv2
ctest --test-dir build-libeinstar-cv2 --output-on-failure
```

Result: 25/25 build steps OK (one deprecation warning at `src/depth.cpp:51`,
`cv::Mat_` comma initializer); `ctest` **2/2 Passed (1.60 s)**. Direct depth
test:

```
coverage 100.0%, median depth 300.00 mm (expected 300.0)
slanted plane: coverage 95.2% (plain matching 65.6%), median |error| 0.53 mm
PASS
```

### 7.4 Clean `HEAD` build (the uncommitted fixes matter)

```
cp -a libeinstar-scratch libeinstar-head
cd <libeinstar>
git show HEAD:src/device.cpp      > .../libeinstar-head/src/device.cpp
git show HEAD:tests/fake_scanner.hpp > .../libeinstar-head/tests/fake_scanner.hpp
cmake -S libeinstar-head -B build-libeinstar-head -G Ninja -DEINSTAR_WITH_OPENCV=OFF
cmake --build build-libeinstar-head
```

Result: **build fails** (`EXIT=1`):

```
libeinstar-head/src/device.cpp:233:10: error: 'copy_n' is not a member of 'std'; did you mean 'copy'?
libeinstar-head/tests/fake_scanner.hpp:118:12: error: 'copy_n' is not a member of 'std'; did you mean 'copy'?
```

The working tree fixes this by adding `#include <algorithm>` at
`src/device.cpp:1` and `tests/fake_scanner.hpp:1`; both changes are
**uncommitted** (`git status`).

### 7.5 Real calibration data (no hardware)

```
cd <repo>
./tests/build-libeinstar/einstar-calib sources/calibration/res_Einscan-E10_200x150
```

Result: **exit 1**, `error: camera rotation is not orthonormal`. The same for
`sources/calibration/ccf_200x150`. See gap **G2**: the fixed `kCcfIrKey` does
not decode these real CCF sets, and only 32 of 65 doubles are read.

### 7.6 Test inventory

| File | Checks | What it covers |
|---|---:|---|
| `tests/test_protocol.cpp` | 19 | byte layouts for set exposure/gain, payload limit, camera mask, reply validation, id wrap, XOR mask, handshake marker, observed replies, temperature, read-only policy, system group, wire sizes, forbidden ops, trigger packing, strings, auto-exposure (4 checks) |
| `tests/test_device.cpp` | 9 | handshake/serial, getters, write gating, forbidden ops, retry count/halts, mask mode, start/stop sequence, multi-page custom info, stream reassembly |
| `tests/test_calibration.cpp` | 7 | `.data_cm` parse/poses, wrong size/rotation, optional real model/CCF, device blob magic/type, optional blob-vs-CCF |
| `tests/test_stream.cpp` | 5 | complete frame, short last transfer, header-only, overflow, camera mapping |
| `tests/test_depth.cpp` | standalone main | synthetic plane depth, slanted-plane stretch, optional real calibration |
| **total** | **40** | all pass |

Optional real-data tests are skipped unless `EINSTAR_CAMERA_MODEL` /
`EINSTAR_CCF_DIR` are set (`test_calibration.cpp:80-109,152-166`). The real CCF
data present here would fail them.

### 7.7 Test gaps

- No test interleaves camera transfers or checks per-camera demux.
- No short-read/short-write bulk test; the fake scanner always returns full
  chunks (`fake_scanner.hpp:116-121`).
- The fake scanner always sets the correct reply marker/length
  (`fake_scanner.hpp:145-148`), so G11 is untested.
- No firmware-version variants, no `bInterval`/descriptor test, no real USB.
- No test for `read_calibration()` against a real 6568-byte blob (only the
  optional CCF-vs-blob consistency check).
- No stress/latency/teardown test (1 s stop, retry storms).

---

## 8. Reproduction notes

- Scratch trees and logs live in `<repo>/tests/`:
  `libeinstar-scratch/`, `libeinstar-scratch-cv/`, `libeinstar-head/`,
  `build-libeinstar/`, `build-libeinstar-cv2/`, `build-libeinstar-head/`,
  `ctest-libeinstar.log`, `ctest-libeinstar-cv.log`.
- The original `<libeinstar>` was not modified; only `git show` (read)
  and `tar` (read) touched it.
- No USB device was opened; `einstar-probe` enumeration only.
