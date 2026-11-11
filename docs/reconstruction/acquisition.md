# Acquisition runbook: live, recorded and simulated USB frames

Status: 2026-11-11 (live path VERIFIED on hardware).  Tools:
`driver/tools/replay_frames.cpp`, `driver/tools/capture_frames.cpp`, shared core
`driver/tools/capture_core.*`, tests `driver/tests/test_replay_frames.cpp`.

> **Validation status — read this first.**
> The live `--backend libusb` path is **verified on a physical Einstar**
> (2026-11-11): read-only probe, metadata + 6568-byte calibration read, volatile
> illuminator/exposure/gain/trigger writes, and synchronised three-camera capture
> all ran.  Measured on this unit: 600/600 complete groups in 60 s at a 100 ms
> trigger (10.00 groups/s, 39.4 MB/s), 1,800 frames written, 3 dropped (all in
> the first device group after trigger start).  A 1.38 GB raw recording replayed
> offline produced **1,050/1,050 byte-identical PGMs**.
>
> Two wire-format defects were found and fixed by this bring-up; both are
> described in `docs/protocol/einstar_usb_protocol.md` §3:
> 1. the stream has **no u32 length prefix** (`[32-byte header][payload]` per
>    transfer);
> 2. the slot counter in header 12..15 is **8-bit and wraps every 256 trigger
>    groups**, so it must never be used as a monotonic ordering key.
>
> The illuminator (laser/speckle projector + strobe) is a **volatile** setting
> that a previous client left at 0; IR frames are then nearly black.  The tool
> now reads it, can set it (`--laser`, `--strobe`) and always turns it off on
> exit.

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
        │  [32-byte header][payload]      <-- hardware-verified (raw) framing
        ▼
 PacketFramer (FramingMode::Raw) ─► FrameReassembler (one in-flight frame per camera)
        │             completes only on header[8] == 1 with exactly 1,310,720 B
        ▼
 einstar::Frame {camera_id, frame_index(8-bit slot), device_timestamp(u64 group), payload}
        ▼
 capture::GroupWriter ─► frame_%06u_cam%u.pgm + frames_manifest.json
```

Legacy recordings/tests that carry the vendor DLL's `[u32 BE len][header][payload]`
framing still load: the framing is stamped into the recording metadata
(`framing=length-prefixed`) and, for recordings without it, detected from the
first transfer.  A `PacketFramer` in the wrong mode is a hard failure, not a
silent one: raw framing on length-prefixed bytes resynchronises and reports
`parse_errors`/`resync_bytes`, and vice versa.

The wire semantics are the ones locked by `driver/tests/test_conformance.cpp`
and `docs/protocol/einstar_usb_protocol.md` §3.1:

| header field | meaning | use here |
|---|---|---|
| `[2]` bits 3..5 | one-hot camera selector `1/2/4` | stream index 0/1/2 |
| `[8] == 1` | **last** packet of a frame | frame completion |
| `[9..11]` | u24 BE packet index inside the frame, 0..31, camera-independent | deterministic lost-packet detection (`packet_index_gaps`) |
| `[12..15]` | u32 BE device frame id, **valid only on the last packet**; a per-exposure slot counter. **Only 8 bits are significant: it wraps 255→0 every 256 groups** | recorded as `frame_ids[camera]`; **never** used for ordering (ordering uses the modular delta) |
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

## 5. Live capture (verified on hardware)

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

This is the **verified** command shape for the reference unit (2026-11-11).
The illuminator flags matter: with `laser 0` the IR streams are almost black.

```sh
./build/driver/einstar-capture-frames \
    --backend libusb --groups 600 --timeout-seconds 120 \
    --trigger 100000 \
    --laser 100 --strobe 0:1000 --strobe 1:300 \
    --exposure-us 0:1200 --exposure-us 1:1200 --exposure-us 2:5000 \
    --gain 0:105 --gain 1:105 --gain 2:180 \
    --record /tmp/einstar/sweep.e1r \
    --status-file /tmp/einstar/sweep/status.json \
    --progress 10 \
    --out /tmp/einstar/sweep/frames --show-serial --json
```

Measured with exactly that command: 600/600 groups, 1,800 frames, 60.0 s,
39.4 MB/s sustained, cadence 99.999 ms mean (98.9..100.8 ms), 3 frames dropped
(all in the first device group after trigger start), raw recording 2.37 GB,
`frames_manifest.json` `complete`-equivalent except the documented startup
group.

`--laser PERCENT` (opcode 0x68, 0..100), `--strobe ROUTE:VALUE` (opcode 0x70,
route 0/1) and `--exposure-us`/`--gain`/`--trigger` are all **volatile**.  The
tool reads the illuminator state in every probe and reports it, and it always
sets laser/strobe to 0 before closing the device (the vendor's `lights_off()`
safe state), so an unattended scanner is never left projecting.

`--status-file PATH` rewrites a small `einstar-live-status-v1` JSON twice per
second (groups written, cadence counters, drops, throughput, exposure/gain,
laser, output directory); `tools/liveview/live_view.py` consumes it together
with the newest PGMs to show a live dashboard.  `--progress SECONDS` prints the
same counters to stderr, which is how the 8-bit slot-counter wrap was found.

### 5.4a Startup transient and the first group

The first device trigger group after `start_streaming` is **always** incomplete
on the reference unit (one camera frame does not arrive / arrives truncated).
The writer therefore never emits it, and the manifest reports it under
`incomplete_groups` with its `device_group` and `missing` cameras.  This is
expected and is why a 600-group run reports `groups_written: 600`,
`groups_incomplete: 1`.  Vendor software discards the first two frame sets for
the same reason.

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

**LIVE measurements (2026-11-11, High Speed 480 Mbps, bus 1 port 5, one
device on that bus):**

| trigger set | measured cadence | throughput | complete groups |
|---|---|---|---|
| 68000 us | 80.25 ms mean (79.3..83.0) — **link-limited, not trigger-limited** | 49.3 MB/s | 20/20 (1 startup group incomplete) |
| 100000 us | 100.0 ms mean (98.9..100.8) | 39.4 MB/s | 600/600 over 60 s |
| 150000 us | 149.9 ms mean (149.0..150.0) | 27.4 MB/s | 20/20 |

The practical USB 2.0 ceiling on this unit is therefore **≈49 MB/s**, not the
45 MB/s used by the model: 3,932,160 B / 80.25 ms = 48.99 MB/s.  A 100 ms
trigger (39.3 MB/s, 80 % link duty) ran for 60 s with zero link-induced loss,
so it is the recommended default for sweep capture.  At 68 ms the device simply
cannot deliver full groups faster than ~80 ms.

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

## 8. Live-verified facts and remaining gaps

Verified on hardware (2026-11-11, firmware `EinScan10_01_SC130_FX3_V2.10_FPGA_V3.7_EN`):

1. Enumerates as `3267:0003`, one interface, endpoints `0x01`/`0x81` (interrupt)
   and `0x02`/`0x82`/`0x83` (bulk), High Speed 480 Mbps, bus-powered 100 mA.
2. The control channel handshake, serial, firmware string, sensor count (3),
   per-camera 1280x1024 and exposure/gain getters all work; the 6568-byte
   calibration page read succeeds and is byte-identical across four independent
   reads on two days.
3. Stream framing is `[32-byte header][payload]` with **no length prefix**;
   one packet per transfer; 40,992 B per packet; 32 packets per frame.
4. Synchronised groups are real: the three camera frames of a group carry the
   same u64 trigger-group counter **and** the same 8-bit slot id.
5. The illuminator is volatile and is **not** enabled automatically: a unit left
   by a client that calls `lights_off()` reports `laser 0`, and its IR frames
   are near-black (mean ≈ 11/255) with only the projected speckle visible.
   `--laser 100 --strobe 0:1000 --strobe 1:300` restores full illumination.
6. 100 ms trigger sustains 10.00 groups/s at 39.4 MB/s for at least 60 s;
   68 ms is link-limited to ~80 ms per group (~49 MB/s).
7. Live capture and offline replay of the same raw recording produce
   byte-identical PGMs (1,050/1,050 verified).

Remaining gaps:

1. Multi-exposure trigger mode (`MONOTRIGCOUNT > 1`, the vendor's
   `{MONO:3, RGB:1}` scanning mode) would put more than one mono frame per
   camera in one trigger group.  The current grouping writes exactly one frame
   per camera per group; validate and, if the vendor mode is needed, extend the
   grouping key with the slot counter under live evidence.
2. The three cameras of a group are transmitted **sequentially** (all 32 packets
   of camera 1, then camera 2, then camera 4), so the group occupies ~2/3 of a
   frame transmission time.  Whether the three sensors are exposed
   simultaneously is not observable from the stream (the "timestamp" field is a
   group counter, not a clock).  Do not claim simultaneity; measure motion
   effects on the images instead.
3. `SimulatedTransport::FaultConfig::loss_probability` is declared but not wired
   into the generator (see §2).
4. The absolute USB ceiling depends on the host controller and bus topology;
   the ~49 MB/s above is one unit on one port with no other High Speed device
   active on that bus.
5. No USB 3 path is pursued (the device is a USB 2.0 device; it negotiated
   480 Mbps).

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


## Verified no-device behaviour

With no scanner attached, both a capture attempt and `--probe-only` exit **1**
with a clear diagnostic and create **no output directory** (the output path is
only prepared after the device opens):

```
$ ./build/driver/einstar-capture-frames --out /tmp/live --groups 1 --backend libusb
cannot open the scanner: no Einstar device found (VID 0x3267, PID 0x0003); ...
$ echo $?
1
$ test -e /tmp/live || echo "no artifacts"
no artifacts
```

This is the safe behaviour expected before the first live bring-up; it does not
prove the successful live path.
