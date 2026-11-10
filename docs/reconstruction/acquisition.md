# Acquisition runbook: live, recorded and simulated USB frames

Status: 2026-11-11.  Tools: `driver/tools/replay_frames.cpp`,
`driver/tools/capture_frames.cpp`, shared core `driver/tools/capture_core.*`,
tests `driver/tests/test_replay_frames.cpp`.

> **Validation status — read this first.**
> No scanner is attached to this machine.  Everything in this document that
> involves `--backend libusb` (device detection, metadata read, calibration
> read, exposure/gain/trigger writes, live capture) is **compiled and linked but
> untested**.  Only the `simulated` backend and the replay path have been run
> and measured.  Treat the first live session as bring-up: start with
> `--probe-only`, then one group, then a short capture.

The acquisition tools write exactly the layout the reconstruction CLI already
loads:

```
DIR/
  frame_000000_cam0.pgm   # stream 0 = right IR   (rotated 180 in calibration)
  frame_000000_cam1.pgm   # stream 1 = left IR    (calibration orientation)
  frame_000000_cam2.pgm   # stream 2 = colour     (rotated 180 in calibration)
  frame_000001_cam0.pgm
  ...
  frames_manifest.json    # --manifest (replay) / always (capture)
  calibration.bin         # libusb capture: 6568-byte factory blob (read-only)
```

Each PGM is binary P5, 1280x1024, 8-bit.  `recon_cli list --frames DIR` and
`recon_cli reconstruct --frames DIR --id N --profile ...` consume the triplets
directly (`recon/src/frame_set.cpp`).  `frames_manifest.json` is the
driver-side provenance manifest (schema `einstar-frames-v1`): it records the
backend, whether the run was simulated or live, the dense output group id and
the raw device trigger-group counter, each stream path and per-camera device
frame id, payload digests, reassembly statistics and timing.

## 1. Build and test

```sh
cmake -S driver -B build/driver -G Ninja -DCMAKE_BUILD_TYPE=Release
cmake --build build/driver -j"$(nproc)"
ctest --test-dir build/driver --output-on-failure
```

New binaries: `build/driver/einstar-capture-frames`,
`build/driver/einstar-replay-frames`.  `test_replay_frames` runs offline in
about 1.5 s and needs no hardware.

## 2. Data path and group semantics

```
 USB EP 0x83 / simulator / recording
        │  [u32 BE len][32-byte header][payload]
        ▼
 PacketFramer ─► FrameReassembler (one in-flight frame per camera)
        │             completes only on header[8] == 1 with exactly 1,310,720 B
        ▼
 einstar::Frame {camera_id, frame_index, device_timestamp, payload}
        ▼
 capture::GroupWriter ─► frame_%06u_cam%u.pgm + frames_manifest.json
```

The wire semantics are the ones locked by `driver/tests/test_conformance.cpp`
and `docs/protocol/einstar_usb_protocol.md` §3.1:

| header field | meaning | use here |
|---|---|---|
| `[2]` bits 3..5 | one-hot camera selector `1/2/4` | stream index 0/1/2 |
| `[8] == 1` | **last** packet of a frame | frame completion |
| `[12..15]` | u32 BE device frame id, **valid only on the last packet**; a per-exposure slot counter | recorded as `frame_ids[camera]` |
| `[16..23]` | u64 BE **trigger-group counter**, identical for every camera frame of one trigger group, **not a clock** | **group key** |

A group therefore has exactly one frame per camera.  The output file id is a
dense zero-based sequence (`%06u`, 0..999999) because the reconstruction loader
matches exactly six digits and the raw device counter is a u64; the manifest
maps `group` (output id) to `device_group` (raw counter).  `Frame::frame_index`
is never used as the group key.

A grouping anomaly is never silently accepted:

| anomaly | detection | reported as |
|---|---|---|
| a camera frame never completes (lost/truncated packets) | group has 2 of 3 cameras when the stream ends or when it is 8 groups behind | `groups_incomplete` + `incomplete_groups[]` with `missing` cameras |
| all three frames of a group are lost | gap in the device trigger counter (stride inferred from observed groups) | `groups_absent` + `absent_group_ids[]` |
| the same camera appears twice in a group | second frame for that `(group, camera)` | `duplicate_frames` + `duplicate_group_frames[]`; first frame is kept |
| a frame arrives for a group already written | group id already in the written set | `late_frames` |
| wrong payload size / camera | size != 1280*1024 | `frames_invalid` |

`"complete": true` in the manifest means none of these occurred.  A LOSSY run
still writes every complete group and exits 0 (the summary says `LOSSY`), so
scripts must check `complete`/`groups_incomplete` rather than only the exit
code.  Corrupt recordings and unusable arguments exit non-zero.

> Limitation (measured, 2026-11-11): `SimulatedTransport` declares a
> `FaultConfig::loss_probability` field but the generator never consults it, so
> a packet is not actually dropped.  The `lossy` preset in the new tools
> therefore uses the faults that *do* remove stream data — frame truncation
> (`truncate_frame_probability`) plus short reads and timeouts.  Those faults
> are keyed on the per-group frame index and hit all three cameras of a group
> together, so the simulator produces **whole missing groups** (`groups_absent`),
> not partial ones.  The partial-group path is covered deterministically by a
> crafted recording in `test_replay_frames`
> (`incomplete_group_is_reported_not_silently_dropped`).

## 3. Simulated capture (no hardware)

```sh
./build/driver/einstar-capture-frames \
    --backend simulated --groups 5 --seed 7 \
    --record /tmp/einstar/clean.e1r \
    --out /tmp/einstar/frames
```

* deterministic: same seed, faults and options produce byte-identical PGMs;
* `SimulatedTransport` uses a virtual clock (40 ms trigger, 300 MB/s link,
  0.5 ms URB service); wall time and virtual time are reported separately;
* `--faults clean|lossy|hostile`, `--trigger US` (cadence of the model),
  `--seed`, `--queue-depth` are available; `--json` appends a one-line
  machine-readable summary as the **last stdout line** (human progress lines
  precede it, so parse `tail -1`);
* `--record FILE` writes the exact raw transfer stream with the existing
  `RawRecorder` (header CRC, per-record CRC, trailer digest).

Observed on this machine (32 vCPU, Release):

| run | wall | virtual | note |
|---|---|---|---|
| capture 50 groups (196.6 MB) | 0.447–0.451 s (437–441 MB/s) | 1.98 s (99.4 MB/s, 25.0 groups/s) | host cost incl. writing 150 PGMs |
| replay same recording | 0.230–0.235 s (836–856 MB/s) | 0.951 s (207 MB/s, ~52.7 groups/s) | replay is host-paced, see §6 |

These are **simulated/host numbers, not scanner measurements**.

## 4. Replaying a recording

```sh
# verify + replay every transfer through the pipeline and write PGMs
./build/driver/einstar-replay-frames \
    --in /tmp/einstar/clean.e1r --out /tmp/einstar/replay --manifest

# first 2 groups only
./build/driver/einstar-replay-frames \
    --in /tmp/einstar/clean.e1r --out /tmp/einstar/replay2 --max-groups 2
```

What it does, in order:

1. `recording::read_recording()` validates the 64-byte header CRC, every
   record header/length/CRC, and the trailer.
2. The tool additionally requires a trailer and re-checks the FNV-1a 64 stream
   digest, transfer count and byte count.  **Nothing is written before this
   passes**: a corrupted recording is refused with the failing check named, and
   the output directory is not created.
3. The recording is served through `SimulatedTransport` (`sim.replay`) and the
   normal `AcquisitionPipeline`/`FrameReassembler`, exactly like
   `einstar-record-replay play`.  Every complete frame goes through the same
   `GroupWriter` as a live capture.
4. It prints groups written, cameras per group, incomplete/absent/duplicate
   counts, bytes, wall/virtual duration, throughput and cadence; with
   `--manifest` it writes `frames_manifest.json`.

Replay exit status: `0` when verification passed and at least one complete
group was written (even if some groups were lost), `1` on verification/IO
failure or zero complete groups, `2` on usage errors.

`einstar-record-replay record --faults hostile` recordings replay the same way;
protocol resynchronisation shows up as `parse_errors`/`resync_bytes` in the
manifest.

## 5. Live capture (untested — scanner required)

### 5.1 Detection

```sh
lsusb -d 3267:0003                     # expect "SHINING 3D ... Einstar"
./build/driver/einstar-capture-frames --backend libusb --probe-only
```

The `libusb` backend opens VID `3267` / PID `0003`, claims interface 0, runs
the control-channel handshake and reads:

* serial (16 uppercase hex digits, redacted in logs/manifest unless
  `--show-serial`; `serial_fnv1a64` is always recorded),
* firmware string, e.g. `EinScan10_01_SC130_FX3_V2.10_FPGA_V3.7_EN`
  (parsed into FX3/FPGA/locale when possible),
* sensor count, per-camera max width/height, exposure/gain, trigger counts and
  period, temperature (best effort),
* the 6568-byte factory calibration blob via the **read-only** page-read
  opcode `0x57` / bulk `0x02→0x82` (pages 0 and 1), written to
  `OUT/calibration.bin`.

If the device is absent the tool exits 1 with the libusb error and a hint.
If any read fails (busy device, wrong endpoint state) it closes the channel and
the transport and exits without touching the stream.

### 5.2 Permissions (udev)

On Linux, either run as root or install a udev rule for the scanner:

```sh
sudo tee /etc/udev/rules.d/60-einstar.rules >/dev/null <<'EOF'
SUBSYSTEM=="usb", ATTR{idVendor}=="3267", ATTR{idProduct}=="0003", MODE="0666"
EOF
sudo udevadm control --reload-rules
sudo udevadm trigger
```

`TAG+="uaccess"` + `GROUP="plugdev"` is the stricter alternative.  EXStar (or
any other process holding the device) must be closed; `libusb_claim_interface`
returns `BUSY` otherwise.  A USB 2.0 port is sufficient for the measured
stream; a USB 3 port does not raise the 45 MB/s USB 2.0 ceiling.

### 5.3 Read-only probe

```sh
./build/driver/einstar-capture-frames --backend libusb --probe-only \
    --out /tmp/einstar/live_probe --show-serial
```

This opens the device, reads the metadata above and the calibration blob, and
closes it.  It does **not** submit stream reads, does not start the trigger and
does not send any set command.  Use it to prove detection, permissions,
metadata decode and the calibration read before capturing.

### 5.4 Volatile settings and capture

```sh
./build/driver/einstar-capture-frames \
    --backend libusb --groups 10 --timeout-seconds 60 \
    --exposure-us 0:8000 --exposure-us 1:8000 --exposure-us 2:12000 \
    --gain 2:64 \
    --trigger 68000 \
    --record /tmp/einstar/live.e1r \
    --out /tmp/einstar/live --show-serial --json
```

Sequence (matches `libeinstar/tools/einstar-probe.cpp --scan`, the
known-working reference):

1. `AcquisitionPipeline::start()` opens the transport, claims interface 0 and
   submits `queue_depth` bulk-IN reads on EP `0x83`.
2. The control channel attaches to the same open transport
   (`claim_interface=false`) and reads identity/calibration.
3. `--exposure-us` / `--gain` are applied per camera (value for all cameras, or
   `CAM:VALUE`).  Range queries (`0x20/0x21`, `0x24/0x25`) are best effort; an
   out-of-range value is rejected before any write.  Any refusal aborts the
   capture and the trigger is not started.
4. `start_streaming(period)` sends the library sequence: period 200000 µs,
   trigger counts 1 IR + 1 colour, then the requested period.  Optional
   `--trigger-counts MONO:RGB` overrides the counts afterwards.
5. Frames are drained until `--groups` complete groups are written or
   `--timeout-seconds` expires (default 30 s).
6. `stop_streaming()` sets period 200000 µs and counts 0:0 (volatile), then the
   channel closes and the pipeline stops/cancels all transfers.
7. The manifest and (optionally) the recording are finalised; a timeout writes
   the partial manifest and exits 1.

`--trigger off` means "do not touch the trigger"; the tool then only captures
frames another process already enabled (normally none, because the interface
is exclusively claimed).  Default live period is 68000 µs (the vendor EXStar
scanning value observed in the session log); the default is 40000 µs for the
simulator.

### 5.5 What the live manifest records

Top level: `"backend": "libusb"`, `"simulated": false`, `"live": true`.
`device` contains the redacted serial (or full with `--show-serial`), serial
FNV-1a 64, firmware string and parsed versions, sensor count, the calibration
file/bytes/digest, and `volatile_settings` (`applied`, trigger period/counts,
per-camera exposure and gain).  `safety` is always
`{"persistent_writes": false, "firmware_updates": false, "reboot": false}`.

## 6. Throughput expectations

USB 2.0 High Speed practical ceiling: **~45 MB/s** (the vendor driver's
0xA400-byte transfers and the pipeline's default request size are sized for
this; the offline model uses 300 MB/s for the hypothetical SS path and 40 MB/s
for a conservative USB 2.0 model).  One synchronised group is three
1,310,720-byte frames:

```
group bytes      = 3 × 1,310,720 = 3,932,160 B
45 MB/s ceiling  = 45,000,000 / 3,932,160 ≈ 11.4 groups/s  (≥ 87.4 ms/group)
40 MB/s model    = 40,000,000 / 3,932,160 ≈ 10.2 groups/s  (≈ 98.3 ms/group)
vendor 68 ms trigger → 14.7 groups/s needs 57.8 MB/s > 45 MB/s, so USB 2.0
is link-limited: expect ≈ 11 groups/s, not 14.7.
40 ms trigger (25 fps) → 98.3 MB/s, impossible on USB 2.0.
```

So the three-camera stream is throughput-bound on USB 2.0, and the trigger is
the limiter only when it is slower than the link.  The "three-frame group
floor" is 3,932,160 bytes; a group cannot be complete until that much has
arrived.  If the link is ~45 MB/s, a 10-group capture needs ≥ 39.3 MB and
≥ ~0.9 s of link time (plus per-URB service and host time).

Measured on this machine, **simulated only**:

| path | wall throughput | virtual link throughput | cadence |
|---|---|---|---|
| simulated capture, 50 groups | 437–441 MB/s (host, incl. 150 PGM writes) | 99.4 MB/s / 25.0 groups/s | 40.03 ms mean |
| replay 50 groups | 836–856 MB/s (host) | 207 MB/s / ~52.7 groups/s | replay-paced, not a device cadence |

Replay serves recorded bytes as fast as the host can submit and parse them;
its virtual clock has only the 0.5 ms transfer service latency, so its
"cadence" is **not** the original device cadence.  Do not use replay cadence
as a hardware measurement.

## 7. Safety rules

* **Read-only by default.** `--probe-only` performs no stream reads and no set
  commands.  Nothing in these tools writes flash, persistent calibration,
  firmware, or reboots the device.  The control channel's command set does not
  even contain the firmware-update (`0x06`), reboot (`0x08`) or page-write
  (`0x58`) opcodes.
* **Volatile only.** Capture sends exposure (`0x23`), gain (`0x27`), trigger
  counts (`0x41`) and trigger period (`0x49`) set commands.  They do not
  survive a power cycle, and the tool stops triggering before it exits.
* **Calibration** is read from the device (page read `0x57`) and written to a
  local file only.
* **Abort on refusal.** A non-zero device status on any set command aborts
  before the trigger starts (or stops the trigger if it was already running);
  the tool never retries a set command with different values.
* Recordings and PGMs are private data.  Do not commit them; write them under
  `/tmp` or an ignored data directory.

## 8. Known gaps / live-pending list

1. **The `libusb` path has never run against a scanner.** It is compiled and
   linked against libusb-1.0.30 and exercised only up to "device not found".
2. The control-channel metadata sequence, calibration page read, and volatile
   set commands are code-verified against libeinstar and the vendor protocol
   but not re-validated with this tool.  The first live session should run
   `--probe-only`, then `--groups 1`, then a longer capture with `--record`.
3. `SimulatedTransport::FaultConfig::loss_probability` is declared but not
   wired into the generator (see §2).  This affects
   `einstar-record-replay --faults lossy|hostile` too; those presets exercise
   short reads, timeouts and truncation, not packet drops.
4. Multi-exposure trigger mode (`MONOTRIGCOUNT > 1`, the vendor's
   `{MONO:3, RGB:1}` scanning mode) would put more than one mono frame per
   camera in one trigger group.  The current grouping writes exactly one frame
   per camera per group and reports the extra frames as duplicates.  Validate
   and, if the vendor mode is needed, extend the grouping key with the slot
   counter (header 12..15) under live evidence.
5. Live camera-to-stream mapping must be confirmed on the first frames
   (stream 0 = right IR rotated 180°, stream 1 = left IR, stream 2 = colour,
   per `recon/include/einstar/recon/frame_set.hpp`).  Inspect the PGMs and the
   manifest's per-camera `frame_ids`.
6. The real link ceiling, per-group loss rate and trigger cadence are unknown
   until measured with a scanner; the numbers in §6 marked SIMULATED are a
   model, not hardware results.

## 9. Troubleshooting

| symptom | likely cause | check |
|---|---|---|
| `no Einstar device found (VID 0x3267, PID 0x0003)` | cable/power, wrong VID/PID, permissions | `lsusb -d 3267:0003`, udev rule, `dmesg` |
| `libusb_claim_interface ... BUSY` | EXStar or another process holds the device | close EXStar, `fuser -v /dev/bus/usb/...` |
| handshake timeout / echo mismatch | interface state after a crashed previous run | unplug/replug; `--probe-only` first |
| calibration page read failed | device busy or page opcode not accepted | retry with EXStar closed; the tool aborts rather than capturing without metadata |
| `no complete three-camera group` on replay | recording verified but every frame incomplete | inspect `groups_absent`/`groups_incomplete`; record again |
| replay refuses: `record CRC mismatch` / `trailer does not match` | corrupted or truncated recording | re-record; the tool writes nothing on refusal |
| capture times out with 0 groups | trigger not started / wrong period / no frames | `--probe-only`, then `--trigger 68000`, check firmware/stream |
| PGMs exist but colours/IR look wrong | stream-to-camera association or 180° rotation | compare with the mapping table in §2/§8 |
