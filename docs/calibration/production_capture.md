# Production capture data handling — bounded RAM, compact views, no raw stream by default

Date: 2026-11-14. Subject: the 51.98 GB recording produced by an ordinary
calibration session (`session-03`: 13,195 trigger groups / 39,585 frames /
22.4 min) and the production storage path that replaces it. Every number below
is from a run on this host; the raw measurement JSON is reproducible with the
commands in §11.

Host: 32 logical CPUs, 62 GB RAM, `btrfs` on NVMe (`/home`), Python 3.14.9,
NumPy 2.5.4, OpenCV 5.0.0. The recovered session
`~/einstar-calibration-03` was read **read-only** (its PGMs are `0444`); no
file in it was modified, and no frame was copied into the repository.

## 1. What was wrong

`einstar-capture-frames --session-dir DIR` is defined as
`--out DIR/frames --record DIR/raw.e1r --status-file DIR/status.json`. The
wizard supervisor always passed `--session-dir`, so **every** calibration
streamed every raw USB transfer to disk "just in case":

| quantity | measured |
|---|---|
| `raw.e1r` | 51,977,409,627 B (48.4 GiB) |
| groups / frames | 13,195 / 39,585 (complete, 1 incomplete at the tail) |
| raw bytes per group | 3,939,175 B (three 1280×1024 8-bit frames = 3,932,160 B payload + record headers) |
| stream rate | 38.63 MB/s (elapsed 1,345.59 s) |
| raw bytes per accepted view | 175,008,113 B (297 accepted / 22.4 min) |

The accepted views themselves needed only 1,167,866,667 B (1.09 GiB) as
uncompressed PGMs. The recording was **44.5× larger than the data the
calibration actually used**, and it grew linearly with wall-clock time, not
with progress. That must not be the normal user experience; it is valuable
only as a developer/regression dataset.

## 2. What changed

| file | change |
|---|---|
| `tools/wizard/ring_buffer.py` | new bounded RAM rolling buffer of recent triplets |
| `tools/wizard/session_io.py` | production `ViewStore`: lossless PNG views, append-only CRC journal, `storage.json`, crash recovery, verification/replay, `compact_session` migration |
| `tools/wizard/capture.py` | `record` storage mode; **production is the default** (`--out`/`--status-file`, no `raw.e1r`); diagnostic mode keeps `--session-dir` |
| `tools/wizard/controller.py` | **integrated 2026-11-14**: `storage=` selects the accepted-view layout, production is the default; accepted views go through `ViewStore.add_view`, no per-group PGM staging |
| `tools/wizard/storage_bench.py` | reproducible benchmark used for every number here |
| `tests/test_production_storage.py` | 24 tests: buffer bound, retention, truncation recovery, estimate, round-trip, diagnostic and no-`.e1r` guards |

The wire protocol, the C++ driver and the acquisition command are unchanged:
`--control`, `--exposure-control`, `--ring`, `--trigger`, exposure/gain and the
status contract are identical. Only the *output destination* flags differ.

## 3. Production storage layout

```text
SESSION/
  storage.json                 # atomic summary: mode, measured bytes/view, codec
  views.jsonl                  # append-only, CRC-framed journal (one record/view)
  views_manifest.json          # rebuildable digest of every stored view
  views/000123/cam0.png        # lossless PNG triplet, names match FILE_CAM
  views/000123/cam1.png
  views/000123/cam2.png
  views/000123/observation.json  # refined marker observations + pose + quality
  views/000123/view.json         # hashes, byte sizes, dimensions, timestamps
  diagnostics/...              # diagnostic mode only; explicitly capped
```

Crash-safety protocol for one accepted view:

1. encode three frames losslessly, write them plus `observation.json` and
   `view.json` into `views/.NNNNNN.tmp/`, `fsync` every file;
2. atomically rename the directory to `views/NNNNNN/`;
3. append one framed journal record (`E1V1` + length + CRC-32 + JSON), `fsync`;
4. rewrite `storage.json` atomically.

A crash at any point leaves one of: no view; a complete view directory that the
journal does not name (recovered from `views/`); or a torn journal tail that is
dropped while the intact prefix is kept. A writable re-open truncates the torn
tail before appending, so a later record can never become unreachable behind
garbage.

The manifest keeps the existing compact projection
(`_normalise_view` drops raw marker pixels); the per-view
`observation.json` keeps the **refined observations** so a replay feeds the
solver exactly the same input without re-detecting frames.

## 4. Measured: which lossless codec

24 real accepted views (72 frames) sampled across the session-03 corpus with
`storage_bench --compare-codecs`; PGM payload is 3.93 MB/view and encode times
are for the whole three-frame view:

| codec | bytes/view | ratio vs PGM | encode/view | decode/view |
|---|---:|---:|---:|---:|
| PGM (current `groups/`) | 3,932,211 | 1.00× | ~0.9 ms write | ~1.8 ms read |
| PNG level 1 | 2,852,312 | 1.38× | 40.9 ms | 19.9 ms |
| **PNG level 3 (chosen)** | **1,887,887** | **2.08×** | **53.5 ms** | **19.5 ms** |
| PNG level 9 | 1,863,757 | 2.11× | 610.7 ms | 19.2 ms |
| zstd -3 | 2,320,813 | 1.69× | 27.3 ms | — |
| zstd -9 | 2,187,866 | 1.80× | 71.9 ms | — |
| zstd -19 | 2,045,097 | 1.92× | 647.7 ms | — |

PNG level 3 wins: it is 2.08× smaller than PGM, needs no external process (the
`zstd` rows include a process spawn per frame, a handicap in their favour on
size but not on ratio), and decodes with the same OpenCV the source already
uses. Level 9 buys 1.3 % for 11× the CPU. `zstandard` is not installed, and
adding a binary dependency for a worse ratio at equal speed is not justified.
The end-to-end store measured 2.09–2.12× on the same corpus (§5, §7), so the
sampled number holds for the real path.

## 5. Measured: production session size

The full production path was run on evenly-spread real views, output on the
NVMe filesystem.  The 40- and 297-view runs held no ring (so their RSS is the
storage path alone); the 60-view run also held the 256 MiB ring, which is the
RAM figure quoted in §6.  The encode spread is disk/memory-pressure noise, not
a different code path.

| views | stored bytes | bytes/view | session tree | encode p50 | encode p95 | verify (re-hash+decode) |
|---:|---:|---:|---:|---:|---:|---:|
| 40 | 74,939,780 | 1,873,494 | 75,134,400 | 70.8 ms | 84.6 ms | 0.84 s |
| 60 | 112,545,060 | 1,875,751 | 112,835,835 | 112.7 ms | 248.2 ms | 2.58 s |
| 297 (whole corpus) | 558,873,778 | 1,881,730 | 560,305,182 | 176.1 ms | 238.2 ms | 14.85 s |

`stored bytes` is the PNG triplet; the session tree adds `views.jsonl`
(80,219 B for 60 views), observations (2,025 B/view), `manifest.json` and
`storage.json`. The whole 297-view corpus —
1.09 GiB of PGM in the legacy layout — fits in **560 MB (0.52 GiB)**, a
**2.09×** reduction, with `verify_views()` checking all 891 frame files
byte-for-byte and pixel-for-pixel in 14.85 s (20 views/s).

A real 3-view migration (`compact_session`, §7) measured **2.121×**
(11,796,633 B → 5,560,531 B), i.e. the sampled codec number holds end-to-end.

### Production vs lossless, for a realistic calibration

| | production (measured) | lossless `.e1r` (session-03 rate) |
|---|---:|---:|
| 40 accepted views | **75.1 MB** | 7.00 GB |
| 60 accepted views | **112.8 MB** | 10.50 GB |
| 297 accepted views | **560.3 MB** (whole corpus) | 51.98 GB (22.4 min) |
| write bandwidth while calibrating | **0.42 MB/s** at the measured 13.24 accepted views/min (1.88 MB/view) | **38.63 MB/s** continuously |
| retained growth | 1.88 MB per accepted view | 3.94 MB per captured group, 9.8 groups/s |

The 60-view production session is **93× smaller** than the raw stream a real
session at that acceptance rate would have written.

## 6. Measured: RAM and write cost

Bounded RAM buffer (`tools.wizard.ring_buffer`, default 256 MiB), filled with
80 real triplets:

| metric | measured |
|---|---|
| retained | 68 groups / 204 frames / 6.94 s at 9.8 Hz |
| accounted bytes | 267,404,288 B (payload 267,386,880 B + 68×256 B wrapper) — **≤ 268,435,456 B budget** |
| evictions | 12 groups, oldest first; frame arrays not copied (`entry.images[cam] is source`) |
| exactness | `nbytes == sum(entry.nbytes)` after every push; `stats()["consistent"]` true |
| peak RSS delta | 301 MB (the 256 MiB payload plus the source page cache of the read arrays) |

Storage-path RAM (no ring): peak process RSS 65.5 MB (40 views) / 69.6 MB
(297 views) against a 47–49 MB interpreter+NumPy+OpenCV baseline → **16–21 MB
working set**, i.e. one view in flight, independent of session length.

**What the live controller actually holds:** one camera group (3 × 1280×1024 =
3.93 MB) plus the compact stationarity/pose stream — the current decisions need
no multi-frame image history, so the production path deliberately does **not**
allocate the 256 MiB ring.  The ring module is implemented, measured and tested
(figures above) for consumers that do need short-horizon frames; enabling it is
a bounded, explicit choice, not a hidden default.  In RAM terms the live path is
therefore smaller than the ring budget, and never grows with session length.
With the 256 MiB ring held at the same time: peak RSS **284.1 MB** — the ring
dominates, as designed.

Write cost of the durability protocol (16 real views, mean per view):
`fsync=True` **130.0 ms**, `fsync=False` 111.6 ms → fsync costs **+18.4 ms per
accepted view** and is kept on. At 13.24 accepted views/min that is a 2.9 %
duty cycle on the storage thread and 0.42 MB/s of writes.

Encoder cost is not on the acquisition path: the store runs only after the
controller accepted a view (0.22 accepts/s measured), so a 250 ms p95 encode is
~5 % of one core and cannot pace the 10 Hz preview.

Acquisition command comparison (real binary, simulated transport, 300 groups;
this measures the recorder's cost, not USB cadence):

| mode | wall | tool throughput | session bytes | `raw.e1r` |
|---|---:|---:|---:|---:|
| production (`--out`) | **3.096 s** | 387.1 MB/s | 471,996,689 | **none** |
| diagnostic (`--session-dir`) | 5.774 s | 204.5 MB/s | 1,653,692,026 | 1,180,569,600 B |

The production command does strictly less I/O and the same acquisition work.
No code on the preview/decision path changed.

Preview path check with the existing replay harness (real frames, 10 Hz paced,
split decisions):

```text
worker.preview: count 101, hz 10.0014, p50 97.7 ms, p95 146.0 ms
```

That is the 10 Hz ceiling for this 120-group replay (the live baseline in
`wizard_performance.md` is 9.75 Hz p50 113 ms with a full detector load), and
it is unchanged by this work — the replay does not touch `capture.py` at all.

## 7. Crash-recovery evidence

`tests/test_production_storage.py` exercises each case; the measured sequence
on a 5-view session is:

1. **Torn journal tail + lost view write** (crash while encoding): truncate
   `views.jsonl` mid-record and remove `views/000004/` → loader returns the
   **intact prefix of 4 views**, reports `journal.truncated=True`,
   `tail_bytes=1282`, and `verify_views()` still passes (the four views are
   intact). `load()` surfaces a note instead of pretending.
2. **Crash between directory rename and journal append** (the common case):
   truncate the last record but keep the directory → the view is recovered from
   `views/`; `recovered_orphans=[5]`, `n_views` includes it, verification
   passes.
3. **Writable re-open after a torn tail**: the store truncates the journal to
   the last intact record (`recovery.repaired=True`), then appends normally;
   the resulting journal has no truncated/corrupt tail and all views verify.
4. **Corrupt middle record**: `read_journal()` stops exactly at the damage
   (2 of 5 records readable); the loader recovers the 5 complete view
   directories, `verify_views()["journal_ok"]=False` (the *index* needs a
   rebuild) while `ok=True` because no view data is missing;
   `rebuild_manifest()` regenerates the index with 5 views.
5. **Truncated frame file**: a PNG cut to 32 bytes is reported as a file/pixel
   mismatch; `verify_views()["ok"]` is false.

Real-data migration (`compact_session`) on a copy of three real accepted views
in `/tmp` — the source PGMs are `0444`, so the copy was made writable to
exercise deletion; nothing was written into the recovered session or the repo:

```text
legacy verify: True   pgm bytes: 11,796,633   (3 views, 0444)
compact: n_views=3 pgm_bytes_before=11796633 stored_bytes=5560531 ratio=2.121
verify after: True  storage ok: True  checked: 18  missing: []
mode: production  mean bytes/view: 1,853,510 (1,713,778 .. 1,925,846)
resume: next_group_index=297   tree bytes: 5,585,453
```

Per camera: left 641,432 B, right 672,996 B, tex 399,350 B — the grey colour
frame compresses best. PGM and PNG are byte-for-byte lossless per frame; only
the container differs.

## 8. Regression limits

These are the limits a normal session must satisfy; the tests pin the ones that
can be pinned offline.

| limit | value | status |
|---|---|---|
| a normal **≤60-view** calibration retains **< 1 GB** | measured **0.113 GB** | passes with 8.9× headroom |
| a normal calibration records **no raw `.e1r`** | `raw.e1r` absent; `recording.requested=false` | guard test + real simulated run |
| full 297-view corpus retained | 0.560 GB | 2.09× smaller than PGM |
| bounded RAM history | ≤ configured budget (default 256 MiB); 68 groups / 6.94 s measured | adversarial-size tests |
| storage-path working set | < 64 MB above interpreter baseline | 16–21 MB measured |
| encode+fsync per accepted view | ≤ 300 ms p95 | 248 ms p95 (60 views) |
| verification of a whole corpus | ≤ 30 s for 297 views | 14.85 s |
| acquisition/preview cadence | unchanged 10 Hz path | preview 10.001 Hz on replay; command tokens unchanged |
| production session never contains `--session-dir`/`--record` | enforced by validation + test | `test_default_capture_command_has_no_raw_recording` |

The 1 GB limit is about the **calibration data**. During a live session one
bounded ring exists on disk and is transient, not part of the session record:
the capture `frames/` ring (`--ring`, GUI default 120 groups ≈ 472 MB). A
production controller writes **no per-group staging ring**: it only writes an
accepted view, atomically. The controller's old `ring_keep` crash-evidence ring
(64 groups ≈ 252 MB) now exists only for `storage="legacy"` and for the
missing-view extension workflow. Retained data is 1.69–1.88 MB per accepted
view regardless.

## 9. Selecting diagnostic (lossless) mode

Diagnostic mode is opt-in and explicit; it is the only mode that writes
`raw.e1r`. It keeps the exact transfer stream for regression/forensics.

```python
from tools.wizard import capture
config = capture.CaptureConfig(session_dir=DIR, record=True)          # Python
config = capture.CaptureConfig.from_mapping({"storage": "diagnostic"},  # wizard
                                            session_dir=DIR)            # mapping
```

```sh
# operator opt-in through the existing GUI binary, no GUI change needed:
EINSTAR_CAPTURE_STORAGE=diagnostic tools/wizard/gui/app.py --source live --session-dir ~/einstar-calibration-04
```

`EINSTAR_CAPTURE_STORAGE` is read only by `CaptureConfig.from_mapping` (the
wizard/GUI configuration path); the explicit dataclass constructor states its
intent and is not overridden by the environment.

For a permanent GUI toggle, `tools/wizard/gui/controller_adapter.py`
`open_source(kind="live")` needs one line: pass
`capture_args={"ring": 120, "storage": "diagnostic"}` when the operator
selects Developer mode (the mapping parser already accepts `storage`,
`storage_mode`, `record`, `record_lossless`, `diagnostic`, `developer`). That
file is owned by another agent, so it was not edited.

## 10. Controller integration (wired 2026-11-14)

`WizardController` now uses the production layout by default. The change is
deliberately small and keeps the storage class independent of the decision
logic:

```python
ctl = WizardController(..., storage="production")  # the default
# begin(): session_io.create(..., storage="production") + open_store()
# accept:  record = store.add_view(group, observation, view_id=view_id)
#          entry["files"] = {cam: path/sha256/bytes}  # names views/*.png
# legacy:  storage="legacy" keeps _materialise() + groups/*.pgm
```

* In production the per-group PGM staging step is skipped entirely. A frame
  that is not accepted never touches the disk; an accepted view is written as
  its three lossless PNGs plus `observation.json`/`view.json`, journalled
  append-only and fsynced.
* `manifest.json` names the PNG paths and their hashes, so
  `session_io.verify()` checks the production files with the same code that
  checked PGMs (and additionally verifies the store journal). `session_io.load()`
  reports `storage.mode`, the view rows and any journal recovery;
  `session_io.resume()` extends the next index from the production views.
* If a view write fails (full disk, permission), the calibration is not
  silently presented as clean: the controller records `_storage_error`, the
  snapshot exposes it, and the final verdict's `storage_ok` gate forces
  `would_activate=false` with the reason. The observation is still in
  `session.json`, so a resume can solve, but no profile from that session is
  activatable.
* `storage="diagnostic"` uses the same compact layout and additionally lets the
  capture side keep the lossless `.e1r`; the controller itself never records a
  stream. `store.add_diagnostic()` remains available for explicitly requested
  bounded frames and returns `None` in production.

### Measured on the recovered real session (24 frames)

The controller was run twice over the same 24 real groups from
`~/einstar-calibration-03/groups` through `ReplayDirSource` (read-only
symlinks), once per storage mode; all 24 frames were accepted in both runs:

| | production | legacy |
|---|---:|---:|
| session tree | **40,538,377 B** | 94,759,912 B |
| per accepted view | **1,689,099 B** | 3,948,330 B |
| accepted-view files | 40,113,068 B (PNG) | 94,373,064 B (PGM) |
| JSON/journal/manifest | 425,309 B | 386,848 B |
| staging ring retained | **0 B** | 0 B (all groups accepted) |
| wall (begin → last decision) | 33.20 s | 30.69 s |

The production path is **2.34× smaller** end-to-end (2.35× on the view files
alone) and costs **105 ms per accepted view** for PNG encode+fsync, which is
well inside the 0.22 accepts/s measured live cadence. Reproduce with
`.venv/bin/python -m tools.wizard.controller_storage_bench --session
~/einstar-calibration-03 --factory ~/einstar-private/einstar-profiles/factory-e10.json
--views 24 --out analysis/calibration/controller_storage_2026-11-14.json`;
the JSON in this repository is that run. An 8-view real run was
then verified and resumed through `session_io`: `verify` ok (48 files
re-hashed, storage ok), `resume` returned `next_group_index=8`, and the session
contained 24 PNGs and zero PGMs. The regression test
`test_real_session_production_storage_is_compact_and_verifiable` pins
`< 3 MB/view` and the verify/resume round-trip on the same data (skipped when
the private session is absent).

The legacy command below still works for old sessions and for any session
recorded before this change:

```sh
.venv/bin/python -m tools.wizard.session_io compact SESSION --delete-pgm
.venv/bin/python -m tools.wizard.session_io verify SESSION
.venv/bin/python -m tools.wizard.session_io summary SESSION
.venv/bin/python -m tools.wizard.session_io estimate --views 60
```

## 11. Reproduction

```sh
# codec comparison on real views (read-only source)
OPENBLAS_NUM_THREADS=1 .venv/bin/python -m tools.wizard.storage_bench \
  --compare-codecs --groups ~/einstar-calibration-03/groups --views 24 \
  --json-out /tmp/codecs.json

# production store: 40 / 60 / all 297 real accepted views
OPENBLAS_NUM_THREADS=1 .venv/bin/python -m tools.wizard.storage_bench \
  --groups ~/einstar-calibration-03/groups \
  --manifest ~/einstar-calibration-03/manifest.json \
  --out ~/einstar-prod-bench --views 60 --json-out /tmp/prod_60.json
OPENBLAS_NUM_THREADS=1 .venv/bin/python -m tools.wizard.storage_bench \
  --groups ~/einstar-calibration-03/groups \
  --manifest ~/einstar-calibration-03/manifest.json \
  --out ~/einstar-prod-bench-all --views 297 --no-ring --json-out /tmp/prod_297.json

# bounded ring with real triplets + fsync cost: see §6 (inline script)

# capture command comparison (simulated backend, no scanner)
./build/driver/einstar-capture-frames --out /tmp/cap-prod/frames \
  --status-file /tmp/cap-prod/status.json --ring 120 --backend simulated \
  --groups 300 --timeout-seconds 120
./build/driver/einstar-capture-frames --session-dir /tmp/cap-diag --ring 120 \
  --backend simulated --groups 300 --timeout-seconds 120

# preview path, existing replay harness (real frames, 10 Hz paced)
QT_QPA_PLATFORM=offscreen .venv/bin/python tools/wizard/gui/app.py \
  --source replay --frames ~/einstar-calibration-03/frames --headless \
  --duration 10 --async-decisions --pace 10 --session-dir /tmp/replay-headless

# tests
.venv/bin/python -m pytest tests/test_production_storage.py -q
.venv/bin/python -m pytest tests -q
```
