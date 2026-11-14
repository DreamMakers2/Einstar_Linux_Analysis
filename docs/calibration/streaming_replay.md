# Streaming replay and offline corpus extraction

Status: implemented, tested and benchmarked on the real 51,977,409,627-byte
session (`<calibration-session>/raw.e1r`, read-only).  This document
describes the memory-bounded recording reader, the streaming replay path, the
`einstar-stream-frames` tool and its binary pipe protocol, the offline
full-corpus feature cache, and the bounded stop-path finalisation used by live
capture.

Everything below is reproducible from the commands in this file.  Evidence
JSONs live in `analysis/calibration/offline/evidence/` and logs in
`analysis/calibration/offline/logs/`.

---

## 1. The problem

`recording::read_recording()` loads every transfer payload into
`std::vector<std::byte>` and `replay_recording()` kept the whole `Recording`
alive.  The real recording has

| quantity | value |
|---|---|
| file size | 51,977,409,627 bytes |
| records | 1,266,763 (1 metadata + 1,266,761 transfers + 1 trailer) |
| transfer payload bytes | 51,926,738,976 |
| transfer records with non-Ok/empty status | 8 (Timeout, zero-length) |
| trigger groups (complete) | 13,195 |
| camera frames (complete) | 39,585 |
| trailer FNV-1a digest | `0x21763E9549FB05CA` |

so the old path needed ~52 GB of RAM plus the pipeline buffers, and the
available RAM was ~49 GB.  `einstar-replay-frames` also had to write 52 GB of
PGMs, which did not fit either (`/home` 24 GB free, `/tmp` 30 GB).

## 2. Memory-bounded streaming reader

`einstar::recording::RecordingStreamReader` (`driver/include/einstar/recorder.hpp`,
`driver/src/recorder.cpp`) is a pull-based reader:

* `open(path)` validates the 64-byte file header (magic `EINSTAR1`, version,
  header size, header CRC-32 over bytes `[0,32)`), then validates/skips leading
  metadata records until the first transfer.
* `read(destination, status)` serves payload bytes directly from disk into the
  caller's buffer.  A destination smaller than one record continues across
  calls (`payload_remaining_`), so the pipeline's 40 KB transfer buffers work
  unchanged.
* Every record is validated incrementally: record magic, total length
  (36-byte header + payload + CRC, ≤ 4 MiB by format), payload-length
  consistency, transfer-status range, and the record CRC-32 computed over
  header+payload as the bytes stream by.
* Transfer payloads feed an FNV-1a 64 digest (`stats.digest`) and the
  transfer/byte counters, including non-Ok and zero-length transfers, exactly
  like the recorder's trailer accounting.
* Metadata payloads are bounded (≤ one record) and exposed via
  `header().metadata`; the first 64 payload bytes of the first non-empty Ok
  transfer are kept for legacy framing detection.
* At EOF the trailer record is CRC-checked and compared with the computed
  digest/transfer count/byte count.  A missing, truncated, duplicated,
  non-final or mismatching trailer is a **hard failure**: `read()` reports
  `TransferStatus::Error`, `failed()` is set and `error()` carries a precise
  `Protocol` status.  Trailing bytes after the trailer are rejected too.
* `finish()` returns `StreamingVerifyStats` only after a clean `at_end()`;
  `trailer_verified` is true only when trailer digest *and* counts matched.

Memory is `O(destination)` plus the ≤4 MiB metadata/trailer record, i.e. a few
MB in the replay path.

`verify_recording_stream(path)` drives the same reader with a 1 MiB scratch
buffer for verify-only runs.

### Partial records, empty transfers, statuses

* destination < payload: bytes are returned in order over several `read()`
  calls; the CRC/digest is only finalized after the last byte.
* zero-length Ok transfer: validated (CRC), skipped, no stall.
* non-Ok transfer (Timeout/Stall/Error/…): any payload is consumed and
  validated but not delivered; the status is reported exactly once, like the
  old in-memory `ReplayTransfer` semantics.
* `last_host_time_ns()` / `last_device_timestamp()` return the timestamps of
  the transfer whose bytes were just returned (unit-tested with Ok/Timeout/
  Stall sequences), which is what makes replayed frame timing the real capture
  timing.

## 3. Streaming replay through the existing pipeline

`SimulatedTransport` already pulled replay bytes on demand
(`serve_replay`); `ReplaySource` now optionally carries a
`std::shared_ptr<ReplayReader>` instead of the in-memory
`vector<ReplayTransfer>`.  `stream_replay()` (`driver/tools/capture_core.cpp`)
then:

1. runs a full streaming verification pass (default; 76 s for 52 GB) before
   creating the output directory, preserving the "refuse corrupt input before
   writing" contract, **or** skips it with `--single-pass` (the replay pass
   still validates every byte it reads and the trailer at EOF);
2. opens a second `RecordingStreamReader` and installs it in
   `SimulatedTransport`; `AcquisitionPipeline` runs unchanged and the
   `GroupWriter` writes `frame_%06d_cam%u.pgm` triplets (or hands groups to a
   sink in pipe mode);
3. stops when the trailer is reached and drained, when `--max-groups` written
   groups have been produced, or when the sparse selection is exhausted.

Recorded transfer `host_time_ns` is propagated into the completion timestamp
(`SimulatedTransport` uses the reader's `last_host_time_ns()`), and the
simulator's virtual clock starts at the recording's `start_host_ns`.  Frame
`host_timestamp_ns` and group cadence are therefore the **real capture timing**
(measured cadence on the real recording: 101.969 ms mean = 9.81 fps, matching
the live status `1345.59 s / 13195 groups`), not a synthetic replay clock.

### Fast pacing (offline)

`--fast-pacing` sets `transfer_latency_ns = 0` and simulated bandwidth to
`1e12` B/s.  It only changes the *simulation's* event cadence; recorded host
timestamps, group ids, frame bytes and trailer verification are unchanged.
It is the default for `--pipe`; `--paced` forces the live-like pacing model.

Equivalence evidence (byte-identical output, different pacing):

| comparison | sha256 of all PGMs | result |
|---|---|---|
| groups 0–499 `--paced` vs `--fast-pacing` | `61cf7c2f0092b40a8540749fd4c4c27f2ed1469d9ce4e1be0de24ed4e728d72e` | identical (1500 files) |
| 20-group `--pipe` paced vs fast | `130a98edf4dcf9613cb4c5676355569590f064c6800efe753defff5c42c12321` | identical (78,647,104 bytes) |

### Group ranges and sparse selection

* `--start-group G` keeps absolute dense group ids: skipped groups are still
  decoded, validated and counted (`groups_skipped`), never written, and the
  first written file is `frame_00G...`.
* `--max-groups M` stops after M **written** groups.
* `--groups-file F` selects a sparse set of dense ids (stage-2 retention); ids
  are preserved, everything else is skipped.
* `GroupWriter` gained `set_group_base/filter/selection`, `groups_completed`,
  `groups_skipped`, an optional `GroupSink` (pipe mode) and optional digest
  computation (off in pipe mode; sha256 is computed in the Python workers).

## 4. Command line

```
build/driver/einstar-stream-frames --in RECORDING --out DIR \
    [--start-group G] [--max-groups M] [--groups-file F] \
    [--manifest] [--single-pass] [--fast-pacing|--paced] \
    [--queue-depth N] [--json REPORT]

build/driver/einstar-stream-frames --in RECORDING --verify-only [--json REPORT]

build/driver/einstar-stream-frames --in RECORDING --pipe --single-pass \
    --fast-pacing --queue-depth 32 [--max-groups M] > FRAMES.bin

build/driver/einstar-stream-frames --help
```

Exit codes: `0` success/verified, `1` verification or replay failure, `2` usage.

`einstar-replay-frames` keeps its old CLI; it now uses the streaming reader
internally (verify first, then stream), so it no longer OOMs on large
recordings.  `replay_recording()`'s contract (refuse corruption before writing,
same group ids/manifest) is unchanged and covered by
`test_replay_frames`.

## 5. Pipe protocol (`--pipe`)

A sequence of 64-byte little-endian headers followed by the payload.  The
writer emits cameras 0,1,2 of one complete trigger group consecutively.

| offset | type | field |
|---|---|---|
| 0 | u32 | magic `0x314D5246` ("FRM1") |
| 4 | u16 | version = 1 |
| 6 | u16 | reserved |
| 8 | u32 | absolute dense group id |
| 12 | u32 | camera (0 = right IR, 1 = left IR, 2 = colour) |
| 16 | u32 | device frame id |
| 20 | u32 | width (1280) |
| 24 | u32 | height (1024) |
| 28 | u32 | reserved |
| 32 | u64 | device trigger-group counter |
| 40 | u64 | captured group host time (ns) |
| 48 | u32 | payload bytes (1,310,720) |
| 52 | u32 | reserved |
| 56 | u64 | FNV-1a 64 of the pixels (0 unless `--digests`) |

The terminator record has magic `0x31444E45` ("END1"), `group = 0xFFFFFFFF`,
`device_group` = groups emitted, `host_time_ns` = frames emitted, no payload.
The Python reader validates magic/version/geometry/camera order and checks the
END counters against its own counts.

## 6. Verification and benchmark results (real recording, read-only)

Machine: Intel i9-14900K (32 threads), 62 GB RAM, NVMe, Linux.

### Full streaming verification (`--verify-only`)

```
build/driver/einstar-stream-frames --in <calibration-session>/raw.e1r \
    --verify-only --json analysis/calibration/offline/evidence/verify_full.json
```

| metric | value |
|---|---|
| wall | 76.244 s (user 63.474 s, sys 10.129 s) |
| throughput | 681.1 MB/s |
| peak RSS | 5,980 KiB (~5.8 MB) |
| records / transfers | 1,266,763 / 1,266,761 |
| payload bytes | 51,926,738,976 |
| digest | `0x21763E9549FB05CA` |
| trailer | digest MATCH, counts MATCH → **VERIFIED** |

The independently computed recording sha256 is
`b3053e3375672927fea739b1d11eab59880be63619f1884eb7c6428d33b3020b`
(`sha256sum`, 33.3 s, kept in
`analysis/calibration/offline/evidence/recording_sha256.txt`).

### Full replay statistics (single pass, fastest, no PGM output)

```
build/driver/einstar-stream-frames --in .../raw.e1r --pipe --single-pass \
    --fast-pacing --queue-depth 32 --json .../replay_stats_full.json > /dev/null
```

| metric | value |
|---|---|
| wall | 98.877 s (user 75.445 s, sys 11.858 s) |
| throughput | 525.3 MB/s |
| peak RSS | 103,664 KiB (~101 MB) |
| groups written | **13,195** |
| frames emitted | **39,585** |
| frames seen | 39,586 (1 incomplete group `device_group 13196`, cameras 1/2 missing) |
| trailer | digest MATCH → **VERIFIED** |

This reproduces the live session's `groups_written: 13195`,
`frames_written: 39585` and `frames_completed: 39586` exactly, and explains the
live `frames_dropped: 1` as the single truncated tail frame.

### Decode ranges (PGM output)

All runs use `--fast-pacing --queue-depth 32`; A/B are `--single-pass` (tail not
reached), C runs to EOF and re-verifies the trailer.

| range | groups written | skipped | wall | replay MB/s | peak RSS | result |
|---|---|---|---|---|---|---|
| 0 – 499 | 500 | 0 | 5.744 s | 343.3 | 99,268 KiB | 1,500 frames |
| 6000 – 6499 | 500 | 6000 | 53.186 s | 481.1 | 98,928 KiB | 1,500 frames, 19,500 seen |
| 12700 – 13194 | 495 | 12,700 | 103.243 s | 503.0 | 98,768 KiB | 1,485 frames, trailer **VERIFIED** |

The copy cost of skipped groups is the reason a range near the end costs a full
pass (there is no group index in the format; see limitations).

### Byte identity with the live capture

The live ring retained 120 groups (`13075–13194`, 360 PGM files) in
`<calibration-session>/frames` (read-only).  Replaying groups
12700–13194 produced **360/360 byte-identical** files (sha256 per file, all
match).  Evidence:
`analysis/calibration/offline/evidence/byte_identity_live_frames_13075_13194.json`.

This validates the reader, reassembler, palette and PGM writer end-to-end
against independent live output.

### Pacing overhead

Groups 0–499: `--paced` 7.077 s vs `--fast-pacing` 5.744 s (~19% faster), with
byte-identical output.  Before the pipeline histogram fix (below), the full
pipe replay ran at ~48 MB/s because the replay loop called
`AcquisitionPipeline::stats()` every iteration, which sorts the two
262,144-sample latency histograms.  The loop now uses the O(1)
`AcquisitionPipeline::bytes_received()` accessor, taking the full pass from
~18 min to 99 s.

## 7. Tests

`ctest` in `build/driver`: **14/14 passed** (34.6 s); it includes the new
`test_stream_recording` (8 cases, 281 checks):

* streaming reader validates header/records/CRC/metadata/trailer and matches
  the authoritative trailer digest/counts;
* partial destinations (1, 7, 4096 bytes) continue records losslessly;
* zero-length and non-Ok transfers are reported once and still digest-counted;
* corruption is a hard failure: bad record CRC, truncated payload/header,
  missing trailer, wrong trailer digest/counts, trailing bytes, bad magic,
  header CRC;
* `last_host_time_ns()` returns the just-delivered record (1000/1001/1003);
* streaming replay matches the simulated capture byte-for-byte, preserves
  full-run ids for `--start-group`/`--groups-file`, counts skipped groups,
  refuses corruption before writing, and paced/fast pipe output is identical;
* trailer-only finalisation reports declared evidence, is bounded (<1 s on a
  16 GiB sparse file whose only real records are header+trailer), and rejects
  missing/truncated/bad-CRC trailers;
* `capture_simulated` records `verification: full` in Auto mode and
  `verification: trailer` (digest_verified false) in explicit trailer mode.

Python: `tests/test_offline_features.py` (6 tests) pins the guidance-compatible
ROI quality metrics, the pixel/label schema, exception-safe per-group
processing (degenerate 0/3/collinear/NaN observations, `cv2.error` from the
detector, non-finite detections) and the corner-report counting.

## 8. Offline full-corpus feature extraction

No images are stored for the corpus.  The extractor is
`tools/offline/extract_features.py`:

1. spawns `einstar-stream-frames --pipe --single-pass --fast-pacing
   --queue-depth 32`;
2. reads FRM1 records in the parent, writes each complete group's three frames
   into one raw 3.93 MB file under `/tmp`, and submits a small
   `(paths, wire metadata)` task to a `ProcessPoolExecutor`;
3. workers run the existing detector (`tools.wizard.detection.detect_one` →
   `calib.detect`), guidance-compatible quality metrics, per-frame sha256 and
   the factory-intrinsics left/right PnP pose, then delete the temp file;
4. rows are written to group-range buckets and merged in ascending `group`
   order into `features.jsonl`; the C++ trailer/END counters are checked
   against the emitted rows/frames (mismatch fails the run and renames the
   partial file `.partial`).

Backpressure: the parent stops reading once `--max-inflight-groups` tasks are
pending, the OS pipe fills, and the C++ reader blocks.  RAM/tmpfs are
`O(in-flight)` (default 128 groups ≈ 0.5 GB), never `O(corpus)`.
`OMP/OPENBLAS/MKL_NUM_THREADS=1` and `cv2.setNumThreads(1)` are set inside
workers to avoid 16–24 × 32-thread OpenCV oversubscription.

### Row schema (one JSON object per line, sorted by `group`)

```
schema             "einstar-offline-group-features/1"
group              int    absolute dense group id (= frame_%06d)
device_group       int    raw wire trigger-group counter
host_time_ns       int    real capture timestamp of the group (min of the 3
                          frame completions recorded in the .e1r)
frame_ids          [3]    per-camera device slot ids (0=right IR,1=left IR,2=colour)
frames             [3]    {sha256, fnv1a64|null, width, height, detect_ms}
marker_counts      {left,right,tex}  detected lattice markers (0..40)
uv                 {left,right,tex}  [[x,y],...] marker PIXEL coordinates,
                          sorted by label (v,u) exactly like the live
                          manifest image_points
uv_labels          {left,right,tex}  [[u,v],...] labels aligned with uv
image_region       {left,right,tex}  {centroid_px, bbox, cell:[col,row],
                          cell_name} for 3x3 image cells (row 0 = top)
quality            {left,right,tex}  identical to calib.guidance.evaluate_frame:
                          active (1/0), area_frac (convex hull / image),
                          focus (Laplacian variance on marker bbox + 24 px),
                          contrast (p95-p5 on that ROI), saturation (whole
                          image, fraction >= 250); null when no detection
whole_frame        {left,right,tex}  {mean,std,p05,p50,p99} (not used by the
                          live quality rule; different names on purpose)
pose               null | {R:[9], t:[3] mm, camera:"left"|"right",
                          reprojection_rms_px, plate_centre_px}
shared_markers_ir  int    identical lattice labels in both IR cameras
assignment_ok      bool   both IR cameras full 40-marker, non-mirrored lattice
errors             {} | {camera|"pose": note}  malformed/dropped detections
```

Quality definitions match `calib.guidance.evaluate_frame` so the cache is
comparable with `decisions.jsonl` and the D7 rule; whole-frame statistics are
kept separately under `whole_frame`.

### Commands

```
# official full-corpus run (52 GB read once, no images stored)
.venv/bin/python tools/offline/extract_features.py \
    --in <calibration-session>/raw.e1r \
    --out analysis/calibration/offline/features \
    --workers 20 --max-inflight-groups 160 \
    --sha256-file analysis/calibration/offline/evidence/recording_sha256.txt

# corner / coverage answer over every group (including live rejects)
.venv/bin/python tools/offline/corner_report.py \
    --features analysis/calibration/offline/features/features.jsonl \
    --json analysis/calibration/offline/evidence/corner_report.json

# stage-1 selection for refinement (~1,200 groups)
.venv/bin/python tools/offline/select_stage1.py \
    --features analysis/calibration/offline/features/features.jsonl \
    --out analysis/calibration/offline/selection --count 1200

# retain only the selected groups' PGMs (sparse pass, ~4.7 GB)
build/driver/einstar-stream-frames --in .../raw.e1r \
    --groups-file analysis/calibration/offline/selection/stage1_groups.txt \
    --out analysis/calibration/offline/stage1_frames --manifest \
    --fast-pacing --queue-depth 32 \
    --json analysis/calibration/offline/evidence/stage1_frames.json
```

Expected paths:

* feature cache: `analysis/calibration/offline/features/features.jsonl`
* manifest: `analysis/calibration/offline/features/manifest.json`
* corner report: `analysis/calibration/offline/evidence/corner_report.json`
* stage-1 list: `analysis/calibration/offline/selection/stage1_groups.txt`
* retained PGMs: `analysis/calibration/offline/stage1_frames/`

### Measured full-corpus result (real recording)

```
command above, 20 workers, 160 groups in flight
wall 763.9 s  ->  17.3 groups/s, 67.9 MPix/s
rows 13,195   frames 39,585   payload 51,884,851,200 bytes (51.9 GB)
features.jsonl 91.5 MB (one compact row per group, no images)
peak RSS: C++ reader 103.6 MB, orchestrator 97.4 MB, max worker 1.24 GB
recording sha256 b3053e33... (reused from a one-time sha256sum)
trailer digest 0x21763E9549FB05CA MATCH; rows/frames == tool END counts
480 groups carry per-group `errors` notes (malformed / non-finite marker
data now degrades to pose=null instead of aborting the corpus run)
169 groups have zero detections but are still in the cache
```

Single-thread vs pool on a 40-group sample: 12.3 s (1 worker) vs 1.9 s
(20 workers), ~6.5x including process-pool startup; the full-corpus run is
~17 groups/s with 20 workers.

### Corner / coverage answer (all 13,195 groups, including live rejects)

| metric | value |
|---|---|
| groups with markers in both IR cameras (>0 / ≥12) | 11,117 / 10,150 |
| both IR full 40-marker assignment | 4,960 |
| groups with a factory-intrinsics pose | 11,785 |
| plate centre in top-right/bottom-right cell, any IR (pose, raw) | **280** |
| … plausible poses only (200–800 mm, RMS ≤ 2 px) | **177** |
| … both IR cameras in a corner cell (raw / plausible) | 70 / 52 |
| marker-centroid based, any IR (also covers no-pose groups) | 486 |

So the answer is **yes**: raw groups rejected by the live wizard do place the
plate centre in the top-right or bottom-right 3×3 image cell while both IR
cameras still see markers.  First candidates are groups 72–75 and 84–92
(top-right, 8–40 markers, 321–369 mm), group 110 (bottom-right, 297 mm) and
group 737 (bottom-right in both IR, 365 mm); the complete candidate lists are
in `analysis/calibration/offline/evidence/corner_report.json`.

Image-cell distribution (marker centroid): left TR=108, BR=81; right TR=344,
BR=247.  Pose-projected per-camera plate centre: left TR=45, BR=62; right
TR=239, BR=112.  The right camera is physically offset, so per-camera
projection is used rather than reusing the left centre.

### Stage-1 selection and retained frames

`select_stage1.py` found 3,943 eligible groups (full assignment both IR, ≥32
markers each, RMS ≤ 1 px, IR contrast ≥ 14, focus ≥ 150, saturation ≤ 2%) and
selected 1,200 spread over distance and tilt bins:

| distance band | selected |
|---|---|
| 300–380 mm | 217 |
| 380–460 mm | 506 |
| 460–560 mm | 458 |
| 560–700 mm | 19 |

| tilt band | selected |
|---|---|
| 0–15° | 559 |
| 15–30° | 438 |
| 30–50° | 203 |

Retaining those groups with a sparse `--groups-file` pass took 143.6 s
(60.7 s verification + 82.9 s replay at 624 MB/s), peak RSS 100 MB, and wrote
1,200 groups / 3,600 PGMs / **4.5 GB** (well under the 8 GB budget).  The 9
retained files that overlap the live ring (groups ≥ 13075) are byte-identical
to the live capture.

## 9. Live capture stop-path finalisation (bounded trailer check)

The live capture child used to call `verify_recording()` in
`run_capture_session()` finalisation, which re-read and re-digested the whole
recording.  On the 52 GB session that exceeded the process-safety watchdog's
5 s deadline and the child was SIGKILLed at stop.

`run_capture_session()` now resolves a verification mode
(`CaptureSessionOptions::recording_verify`, CLI
`--verify-recording=auto|trailer|full`):

* `auto` (default): full verification when the capture is simulated or the
  recording is ≤ 512 MiB; **bounded trailer-only check** for large real
  recordings;
* `trailer`: always read only the 64-byte file header and the final 72-byte
  trailer record, validate magic/version/header CRC, record magic/length 72/
  type 3/payload 32/CRC, and report the **declared** digest/transfer/byte
  counts;
* `full`: old behaviour (whole-file re-read + digest recomputation).

`read_recording_trailer()` reads 136 bytes total and never recomputes the
digest.  The frames manifest records this honestly:

```
"recording": { ..., "digest_fnv1a64": <declared>,
               "verification": "trailer"|"full",
               "trailer_checked": true,
               "digest_verified": false|true,
               "file_size": ... }
```

`digest_verified` is `true` only for the full mode.  The independent full
verification remains `einstar-stream-frames --verify-only` (run on the real
recording: digest MATCH).

## 10. Limitations

* The format has no group index or checkpoint, so `--start-group` / sparse
  selection still read (and validate) everything before the requested group.
  A range near the end costs a full pass.
* Default replay does a full verification pass and then a replay pass (two
  reads of the file).  `--single-pass` removes the extra pass; the replay pass
  still validates every record and the trailer at EOF, but frames may be
  written before a late corruption is detected (the run then fails).
* Verification is single-threaded (CRC-32 slice-by-8 + serial FNV-1a,
  ~680 MB/s end-to-end on this machine).  Parallelising would need chunked
  digest composition and is not required by the current workloads.
* The pipe protocol is an internal, versioned interface (`FRM1` v1), not a
  stable public API; the consumer must handle short reads and verify END
  counters.
* `--pipe` does not compute per-frame FNV digests by default (pass
  `--digests`); the extractor always records sha256 per frame in the workers.
* The corpus cache assumes `calib.plate` pitch 28.0 mm and the factory profile
  `factory-e10.json` for pose; it is derived evidence, not a recalibration.
* Detection runs without ROI prediction offline (each frame is a full
  detection), which keeps worker tasks independent but is ~1-2× slower than
  the live ROI path.
* `read_recording()` / `verify_recording()` remain available for small files
  and tests; they are intentionally still O(file).

## 11. Evidence index

| file | contents |
|---|---|
| `analysis/calibration/offline/evidence/verify_full.json` | full 52 GB streaming verification |
| `analysis/calibration/offline/evidence/replay_stats_full.json` | full replay statistics (groups/frames/trailer) |
| `analysis/calibration/offline/evidence/bench_range_000000_000499.json` | range A |
| `analysis/calibration/offline/evidence/bench_range_006000_006499.json` | range B |
| `analysis/calibration/offline/evidence/bench_range_012700_013194.json` | range C (trailer verified) |
| `analysis/calibration/offline/evidence/pacing_equivalence_range_0_499.json` | paced vs fast PGM sha256 |
| `analysis/calibration/offline/evidence/byte_identity_live_frames_13075_13194.json` | 360/360 sha256 match vs live frames |
| `analysis/calibration/offline/evidence/recording_sha256.txt` | recording sha256 |
| `analysis/calibration/offline/features/manifest.json` | corpus extraction manifest (sha256, trailer, counts, workers, RSS) |
| `analysis/calibration/offline/evidence/corner_report.json` | 3x3 cell counts and corner candidates |
| `analysis/calibration/offline/evidence/stage1_selection.json` | stage-1 selection summary |
| `analysis/calibration/offline/evidence/stage1_frames.json` | retained-frame pass report |
| `analysis/calibration/offline/evidence/benchmarks.json` | aggregated benchmark numbers |
| `analysis/calibration/offline/evidence/extraction_scaling.json` | 1 vs 20 worker sample |
| `analysis/calibration/offline/evidence/tests.json` | C++ ctest and Python test results |
| `analysis/calibration/offline/evidence/byte_identity_stage1_vs_live.json` | stage-1 frames vs live ring |
| `analysis/calibration/offline/logs/` | raw tool logs used for the tables |
