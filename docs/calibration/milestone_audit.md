# Independent audit of the 2026-11-14 EXStar milestone

Auditor: independent agent ("audit-agent"), not part of the six-agent milestone.
Date: 2026-11-14.  Working tree `9c37cf4`; published tree
`https://github.com/DreamMakers2/Einstar_Linux_Analysis` at `92cc629`
(fresh clone in `/tmp/public-fresh`).

Method: every load-bearing number was recomputed from its stated inputs with the
tree's own tools (or from the raw bytes where the tool itself was under audit).
Nothing in `~/einstar-calibration-03`, `~/einstar-private`, the factory profile or
the published artefacts was written; the audit wrote only this file and
`analysis/exstar_reference/audit_findings.json`.

Verdict labels: **CONFIRMED** (recomputed, matches as stated) ·
**NOT REPRODUCED** (measured differently) · **UNSUPPORTED** (no evidence in the
shipped data/code) · **DEFECT** (code or document is wrong and should be fixed).

---

## 0. Executive summary

The milestone's *measurement* work is unusually solid: the CCF decode, the rig
geometry, the device readback, the white-balance reproduction and the missing-view
assessment all reproduce.  The failures are in **integration and in the summary's
rhetoric**, and two of them matter operationally:

1. **The operator command in the milestone report dead-ends at 7/8.**
   `--complete-session ~/einstar-calibration-03` (the *original* 6/8 session)
   produces a plan of **2 D4 stills + 2 D7 holds**, and D4 can never be satisfied
   in an extension session because dwell views are excluded from D1–D6 by design.
   Four genuine 2 s holds at exactly the planned poses give 7/8, `ready False`,
   `activation False`.  The working workflow (recovered 7/8 overlay) is only
   reachable through the CLI `--recovered`, which the launcher does not expose.
2. **The production-storage claim is not wired into the live path.**
   `ViewStore` and the 256 MiB RAM ring are used only by `storage_bench.py` and the
   tests.  The controller still writes uncompressed PGM triplets (~3.93 MB/view).
   `production_capture.md` §10 says so explicitly ("controller not edited … 112.8 MB
   instead of 236 MB … Until it lands"); the milestone report and STATUS present
   the benchmark as the new default.

Smaller defects: the white-balance factory-path guard misses the real factory
profile path, two wrong cells in the EXStar comparison table (one propagated into
the summary), and the "10.3 % / 11.4 %" white-balance trap is not reproducible as
stated.  The test suite is honest about counts (578/8 private, 12/12 + 1/1 C++)
but the published clone runs a *different, smaller* suite (79 collected tests are
not published), so its 441/79 is not "the same tests minus fixtures".

---

## 1. Numbers, recomputed

### 1.1 CCF — baseline, rotation, extrinsics, distortion

Command (both record sets, key mode as documented):

```sh
.venv/bin/python tools/ccf/decode_ccf.py --txt ~/einstar-private/exstar-20261114/res/RightCCF.txt --key-mode best
# fx=1156.980609  fy=1157.472158  cx=635.274145  cy=494.123730
# kc = [-0.162050120, 0.177924663, 0.000837870, 0.000024227, 0.012359554]
# T  = [-23.030420, 12.548272, 388.826135]   det(R)=1.000000000000
```

Rig geometry was recomputed independently (`/tmp/audit/repro_ccf.py`, 22-double
records located by structure at 12/260/508 in `~/einstar-private/calibration.bin`):

```
F  IR B= 160.0916 rot= 22.0859  L-Tex B= 122.8774 rot= 16.9983  R-Tex B= 37.2149
A  IR B= 158.5570 rot= 22.1383  L-Tex B= 125.3176 rot= 17.0049  R-Tex B= 38.6281
N  IR B= 158.6128 rot= 22.1290  L-Tex B= 125.3362 rot= 17.0004  R-Tex B= 38.6443
L  IR B= 160.0137 rot= 22.1300  L-Tex B= 122.8779 rot= 17.0836  R-Tex B= 37.1367
direction angles: L-R  F-A=0.3132 F-N=0.3034 F-L=0.0073 A-L=0.3077
                  L-Tex F-A=8.0739 F-N=8.0490 F-L=0.0385 A-L=8.0509
L-F deltas (mm): [-0.0779, +0.0005, -0.0781]
```

| claim | verdict |
|---|---|
| CCF 2026-11-14 baseline **158.6128 mm**, rotation **22.1290°** | **CONFIRMED** |
| previous CCF **158.5570 mm / 22.1383°** | **CONFIRMED** |
| device 22-double records **160.0916 mm / 22.0859°** | **CONFIRMED** |
| Linux candidate reproduces the device record to **0.08 mm** (0.0779) and colour extrinsic to **0.0005 mm / 0.039°** (0.0385) | **CONFIRMED** |
| CCF colour extrinsic **~8°** from ours (8.0490° F-vs-N, 8.0509° A-vs-L) | **CONFIRMED** |
| baseline change **+0.035 %**, rotation **−0.0093°**, fx **−0.67/−1.11/−0.15 px** | **CONFIRMED** (0.03516 %, −0.009322°, −0.6747/−1.1063/−0.1483) |
| distortion **bit-identical** across A → N | **CONFIRMED** (`max|Δkc| = 0.0` exactly, all three cameras) |
| distortion bit-identical to the *device* records | **UNSUPPORTED as worded** — `max|Δkc|` = 5.24e-16 (left), 6.33e-16 (right), 0.0 (tex).  The specialist doc says "residual ≤ 6.3e-16" (correct); the summary says "bit-identical across all sets". |
| two wrong cells in `exstar_vs_factory_vs_linux.md` §3 | **DEFECT** — see §6 |
| `tools/ccf/validate_ccf.py` 40/40 | **CONFIRMED** (`40/40 checks passed`) |

Provenance hashes in `exstar_vs_factory_vs_linux.md` §1 all match the private
files (`7061f68a…`, `a31a0119…`, `6e8a7416…`, `b083003d…`), and the A-set hashes
match `sources/calibration/res_Einscan-E10_200x150/`.

### 1.2 Device readback (`exstar_hardware_state.md`)

Recomputed from the bytes, not from the document:

```
post-run dump sha256     256dd440554387da8fcd5b48baa0b2e8c314a1eb90a89b85b2987bb0f9121148   (matches)
new Left/Right/TexCCF    at 1191 / 2479 / 3767, byte-identical to the host files
wbdata / wbdata_noled    at 4800 / 4872, byte-identical
ParaCCM_0 / ParaDark_0   at 765 / 825, byte-identical to the 2026-02 set (unchanged)
"2026-11-14 00:13"       at 3503   (the old "2026-02-09 21:34" is gone)
"<redacted-production-date>"             at 6291, untouched
differing bytes vs pre-run dump: 366, first 1191, last 4941, none ≤ 0x39A
u32 at 0x39B = 4 (CCF block type), u32 at 0x12BC = 5 (WB block)  — both offsets confirmed
```

**CONFIRMED**, including "identity region `0x000..0x39A` untouched" and "366 bytes
changed".  (The only thing not verifiable locally is "53 runs" — that needs the
vendor log, which is private.)

### 1.3 White balance (`white_balance.md`)

```sh
.venv/bin/python -m calib.white_balance estimate --led-on …/wb/imageTex1.bmp --led-off …/wb/imageTex2.bmp \
    --out /tmp/audit/wb.json --divisor 8 --dark 0.04502069 0.0455484 0.04504853
# led_on  : slot0=1.76675 slot2=1.58539   (EXStar order: 1.585387 / 1.766746)
# led_off : slot0=1.78123 slot2=1.59834   (EXStar order: 1.598342 / 1.781225)
.venv/bin/python -m calib.white_balance validate /tmp/audit/wb.json --led-on … --led-off …
# led_on  outside_before 0.349516 → outside_after 0.019270
# led_off outside_before 0.373839 → outside_after 0.015901 ; passes true
```

| claim | verdict |
|---|---|
| stored gains reproduced to **−1.1 % / −1.5 %** | **CONFIRMED** (−1.127 %, −1.522 %; no-LED −0.431 %, −0.838 %) |
| cast **34.96 % → 1.93 %** and **37.38 % → 1.59 %** outside the window | **CONFIRMED** (0.349516→0.019270; 0.373839→0.015901) |
| gains must be applied to dark-subtracted data or over-correct **11–15 %** | **CONFIRMED** (measured 14.91 % on, 11.44 % off) |
| copying the stored matrix onto a `COLOR_BayerRG2BGR` buffer leaves a **10.3 % / 11.4 %** cast | **NOT REPRODUCED** — see §6 |
| frame means 24.14 / 29.09 DN, phase spread 29.1 % / 35.2 % | **CONFIRMED** (spread is `(max−min)/min`: 29.08 %, 35.15 %) |

### 1.4 Missing views

```sh
.venv/bin/python -m tools.wizard.missing_views --session-dir ~/einstar-calibration-03 \
    --recovered analysis/calibration/recovery/session_recovered
# original : 6/8 satisfied — NOT READY ; D4 top_right/bottom_right 0, D7 "only 4 distinct dwell intervals"
# recovered: 7/8 satisfied — NOT READY ; D7 "only 2 distinct dwell intervals"
# plan     : 4 pose(s) (cap 8, ~1.6 min) ; all 4 are D7, hold 1.0 s
```

| claim | verdict |
|---|---|
| base **6/8** (D4+D7), recovered **7/8** (D7 only), plan **4 holds ~1.6 min** | **CONFIRMED** |
| D7 deficit: **2** qualified intervals of the 6 required (recovered base) | **CONFIRMED** (4.643 s, 225.402 s) |
| D7 in the *original* session | **4** intervals from the decision-rate stream — the doc's table says so, the summary's §5/§8 juxtaposition does not |
| 4 holds at the plan poses reach **8/8 READY**, activation false (recovered base) | **CONFIRMED** — `base 7/8 → 4 genuine 2 s holds → 8/8 ready True activation False intervals 6` |
| the same 4 holds on the **original** base reach 8/8 | **DEFECT — they reach 7/8** (see §2.1) |

### 1.5 Production storage

```sh
OPENBLAS_NUM_THREADS=1 .venv/bin/python -m tools.wizard.storage_bench \
  --groups ~/einstar-calibration-03/groups --manifest ~/einstar-calibration-03/manifest.json \
  --out /tmp/audit/prod40 --views 40 --json-out /tmp/audit/prod_40.json
# stored_bytes_per_view 1,873,494.5 ; ratio_vs_pgm 2.0989 ; session_tree_bytes 75,141,261
# verify_ok True (120 files) ; ring capacity 68 groups / 6.9387755 s ; lossless_reference_bytes 7,000,324,520
```

| claim | verdict |
|---|---|
| **1.873 MB/view**, **75.1 MB** for 40 views, **7.00 GB** lossless reference | **CONFIRMED** (75,141,261 B vs the doc's 75,134,400 B — 0.009 %; 1.876 MB is the *60-view* figure) |
| 256 MiB ring = **68 groups / 6.94 s**, exact accounting | **CONFIRMED** |
| compressed views + CRC journal are the **new default** for a live session | **DEFECT — not wired** (see §2.2) |

### 1.6 Solver / validation numbers

`analysis/calibration/solver/final/validation.json` contains every figure the
report quotes: RMS **0.65144**, held-out **0.76959 ± 0.01885**, epipolar **0.33283**
vs **0.46519**, colour epipolar **0.62969** vs **31.3446**, 17 constrained /
1 weak (`tex.tz`) / 0 unobservable, condition **1803.54**, baseline error
**0.9187 %**.  **CONFIRMED as quoted.**  The "split-half 1.2–2.3× σ" figure is not
in this file and was not verified.

The vendor-corpus numbers are in `analysis/exstar_reference/summary.json`:
`ours_free` RMS **0.3138**, baseline **160.2097**, epipolar **0.0334**;
`ccf_NEW` **1.4199**; rig-scale scan `ccf_A best_s=1.01 → 0.44477`,
`ccf_NEW best_s=1.01 → 0.41225`.  **CONFIRMED**, except that the report pairs
`1.420 px` (NEW) with `0.445 px` (which is the *2026-02* CCF at s=1.01); the NEW
CCF collapses to **0.412 px**.

---

## 2. Defects, ordered by severity

### 2.1 DEFECT (high) — the report's operator command cannot finish

*Where.* `docs/calibration/exstar_milestone_report.md` §9 lines 291–294;
`tools/wizard/gui/app.py:115` (`begin_extension(args.complete_session)`, no
recovered-overlay option); `calib/missing_views.py:plan` (emits D1–D6 entries);
`tools/wizard/coverage.py:594` `_is_dwell` and `:752` `pose_idx` (dwell views are
excluded from D1–D6); `tools/wizard/controller.py:4049` `_extension_ingest`
(accepts **only** a completed hold).

*Reproduction.* The report's command, driven through the controller (base opened
read-only, extension written to `/tmp`):

```
begin_extension ok: True
n_planned 4 remaining_dwell 2 ready False
  1. D4  hold=None  cell=top-right
  2. D4  hold=None  cell=bottom-right
  3. D7  hold=1.0   cell=bottom-left
  4. D7  hold=1.0   cell=bottom-right
```

Then executing that plan with four *genuine* 2 s holds at exactly those poses:

```
base: 6 /8 ['D4', 'D7']
after 4 genuine 2 s holds at the planned poses: 7 /8 ['D4'] ready False activation False
corners: {'top_left': 9, 'top_right': 0, 'bottom_left': 2, 'bottom_right': 0}
stationary intervals: 8
```

*Why it matters.* (a) The D4 instruction says "capture one sharp still", but the
extension ingest accepts nothing except a completed hold, so a still is rejected.
(b) Even if the operator holds still for a second, the resulting `dwell=True` view
is excluded from D4 by `coverage._is_dwell`, so D4 stays at 0.  The plan can never
close, `finish()` keeps `would_activate: false`, and the operator has no way to
add the two right-corner views.  The working path (recovered 7/8 base) is only
reachable via the CLI `--recovered`; `app.py` has no such flag, so the launcher —
the only GUI path — dead-ends.

*Fix.* Either give the launcher a `--recovered`/evidence-overlay option (and make
it the default when the recovered overlay is present), or make the extension
contribute the planned stills to D1–D6 (i.e. do not mark a hold `dwell=True` when
its pose matches a D1–D6 plan target), or refuse to plan D1–D6 entries in
extension mode and say why.

*Test that would catch it.* The existing controller test builds a base that is
already **7/8 with D7 only**.  Add a test with a base whose unsatisfied
dimensions include D4, run `begin_extension`, and assert either that the plan is
D7-only or that four accepted holds produce 8/8.

### 2.2 DEFECT (high, documentation) — the storage layer is not in the live path

*Where.* `docs/calibration/exstar_milestone_report.md:196` ("production capture is
the default: no `raw.e1r`, 256 MiB bounded ring, **compressed accepted views**,
CRC-framed append-only journal") and its §6 table (:208–216, column "production
(new default)"); `docs/STATUS.md` §0b row "production capture".

*Reality.* The only caller of `session_io.ViewStore.add_view` is
`tools/wizard/storage_bench.py:209`; the only user of
`tools/wizard/ring_buffer.py` is the same benchmark plus tests.  A live session
still writes three uncompressed PGMs per accepted view through
`controller._record_accept` → `controller._move_to_groups` (controller.py:1564,
:998).  `begin()` calls `session_io.create()` **without** `storage=`, i.e. the
documented legacy layout — `session_io.py:139-158` says so in as many words:
"`None` (the historical default) creates only the legacy structure and is what
the current controller calls".  So a live session does not even get `views/` or
`storage.json`.  `docs/calibration/production_capture.md` §10 states this explicitly:
"`tools/wizard/controller.py` is owned by another agent. It currently writes
uncompressed PGMs … With that change … a 60-view session retains 112.8 MB instead
of 236 MB … Until it lands".  So the specialist document is honest and the
summary is not.

*Numbers.* What is live: `raw.e1r` is off by default (`capture.py:365`,
`record: bool = False`, and `_validate_storage_args` refuses `--session-dir` /
`--record` smuggling).  That alone takes a 297-view session from 52 GB to ~1.17 GB
(3.93 MB/view PGM), not to 560 MB: 40 views cost 157 MB in the live path, not
75.1 MB, and 297 views cost ~1.17 GB, not 560 MB.  The 1.873 MB/view and
112.8 MB figures are real *benchmark* results (reproduced in §1.5), but they
are not what a user gets.

*Test that would catch it.* An end-to-end controller test that accepts ≥2 views
and asserts the session contains `views/*.png` + `views.jsonl` (and no
`groups/*.pgm`), i.e. the same assertion the doc's §10 makes.

### 2.3 DEFECT (medium) — the white-balance factory-path guard misses the real paths

`calib/white_balance.py:94`:

```python
_FACTORY_MARKERS = ("res/einscan-e10", "res/einscan-ea", "einstar-calibration-03",
                    "/factory/", "\\factory\\")
```

Probe against the paths this project actually uses:

```
False  ~/einstar-private/einstar-profiles/factory-e10.json
False  ~/einstar-private/einstar-profiles/colour-fitted-e10.json
False  ~/einstar-private/factory-snapshot-20261111/calibration.bin
False  ~/einstar-private/calibration.bin
False  <repo>/sources/calibration/res_Einscan-E10_200x150/LeftCCF.txt
True   ~/einstar-calibration-03/session.json
```

The real factory profile (`einstar-profiles/factory-e10.json`, SHA-256
`8259f97c…`) and the repo's own factory-record directory
(`res_Einscan-E10_…`, underscore not slash) are **not** refused by
`write_profile`.  In practice a write is still stopped by `FileExistsError`
(`overwrite=False`), but `write_profile(profile, factory_path, overwrite=True)`
would replace the factory profile with a photometric layer.  The test
`test_write_refuses_factory_paths` passes only because it uses a synthetic
`res/Einscan-E10/` path.

*Fix / test.* Add `einstar-profiles`, `factory-e10`, `factory-snapshot`,
`res_Einscan-E10` to the markers; extend the test with the real path strings.

### 2.4 DEFECT (medium) — two wrong cells in the EXStar comparison table

`docs/calibration/exstar_vs_factory_vs_linux.md:121–122`:

| printed | decoded | its own Δ requires |
|---|---|---|
| N IR-right `fx` **1157.8806** | **1156.9806** | 1158.0869 − 1.1063 = 1156.9806 ✔ |
| N IR-left `cx` **617.0906** | **623.0906** | 623.9122 − 0.8216 = 623.0906 ✔ |

Both Δ columns are correct; the absolute cells are typos.  The `fx` typo is
propagated into `exstar_milestone_report.md:83` ("1158.44 / **1157.88**").
Everything else in that table matches the decode to the last digit.

### 2.5 DEFECT (low/medium) — the "10.3 % / 11.4 %" white-balance trap is not reproducible

`docs/calibration/white_balance.md:280`, repeated at
`exstar_milestone_report.md:178`.  Measured with the module's own metric
(`mask_means` on the lit-plate mask, dark subtracted):

```
imageTex1 stored matrix LITERAL in BGR slots : b/g 0.9208 r/g 1.1128  cast 11.28 %
imageTex2 stored matrix LITERAL in BGR slots : b/g 0.9123 r/g 1.1111  cast 11.11 %
imageTex1 through from_exstar_matrix (reversal): cast 3.02 %
imageTex2 through from_exstar_matrix (reversal): cast 2.08 %
imageTex1 window means, literal             : cast 23.0 %
```

None of these is 10.3 %/11.4 %.  The pair is exactly the inter-channel gain ratio:
`1.585387/1.766746 − 1 = −10.26 %` and `1.766746/1.585387 − 1 = +11.44 %`.  So the
number is a back-of-envelope gain-ratio identity, not a measured cast; the actual
consequence of the mistake is either ~11 % (literal placement, plate mask) or
~3 % (after the documented reversal).  The *conclusion* (slot order matters, and
must be stored in the profile) is still correct and independently proven by the
synthetic-phase test.

*A test that would catch it:* assert the documented number from the same
computation that produces it (and name the metric and mask in the doc).

### 2.6 DEFECT (low) — `tools/wizard/storage_bench.py` is not runnable as a script

```
$ .venv/bin/python tools/wizard/storage_bench.py --help
ImportError: cannot import name 'GenericAlias' from 'types'
  (consider renaming '/…/tools/wizard/types.py' …)
```

`tools/wizard/types.py` shadows the stdlib when the script's directory is
`sys.path[0]`.  `tools/wizard/missing_views.py` works around this explicitly; the
benchmark does not, and the audit brief's own phrasing ("run
tools/wizard/storage_bench.py") fails.  The documented `-m tools.wizard.storage_bench`
form works.  Low impact, but the same trap will bite the next script added there.

### 2.7 Soft spot (low) — extension evidence can be fabricated by editing `holds.jsonl`

`calib/missing_views.py:1326 _hold_rows` has a fallback: a hold record **without
per-sample rows** contributes two `stationary: True` rows, regardless of measured
speed.  `ExtensionStore.append_hold` (`:1214`) validates the base digests but not
the hold's duration/samples/pose, so a hand-written line in
`extension/holds.jsonl` counts as a dwell interval.  The controller never
produces such a record (`HoldTracker` always emits `samples`), so this is a trust
boundary, not a live bypass — but it is the one place where "evidence" is asserted
rather than measured.

---

## 3. Danger classes (a)–(d)

**(a) Writes to the base session or the device — no bypass found.**
`load_base_evidence` only reads and re-verifies digests; `ExtensionStore.create`
refuses the base path; `begin_extension` refuses a non-empty or identical
directory; the CLI refuses `--json` inside any evidence directory; the recovered
overlay is refused if the factory digest differs.  New modules contain no USB /
libusb / ioctl / device path (`subprocess` appears once, in `storage_bench`, for
`zstd`).  The base session's full tree fingerprint (1455 files, sizes, mtimes)
is **byte-identical before and after** every test, CLI run, controller run and
benchmark in this audit (§4).  One residual: nothing tests a *concurrent* writer,
but the read-only permissions (0555/0444) make an accidental write fail loudly.

**(b) An extension accepting evidence without a genuine hold — no bypass found in the tracker; two soft spots.**
Acceptance requires `status.state == "ACCEPTED" and status.hold` from
`HoldTracker`, which needs two consecutive still samples (≤3 mm/s, ≤1.5 deg/s)
and then a contiguous window inside **2.5 mm / 1.0°** of the anchor for ≥1.0 s.
Probes (`/tmp`):

```
2 frames 1 s apart, 2 mm step       → HOLDING, not accepted (needs a 3rd update)
2.4 mm/s drift, 10 Hz, 1.0 s        → ACCEPTED (deviation 2.4 mm < jitter 2.5 mm)
5.0 mm/s drift                      → never accepted
1.4 deg/s rotation                  → hold_unstable (deviation > 1.0°)
2.4 mm/s drift over 1.6 s           → accepted at 1.0 s, then "move_to_the_next_pose"
```

So the binding constraint is the *jitter* tolerance (2.5 mm / 1.0°), i.e. the
accepted envelope is tighter than the advertised D7 thresholds.  Residual soft
spots: (i) a hold at **any** distinct pose is accepted even when it matches no
plan target (`_extension_accept`, controller.py:3906 returns `target=None` but
still stores it); (ii) §2.7 above.

**(c) Losing an accepted view on crash / replaying a corrupt one silently — the store is sound but not used; the replay default does not verify.**
`ViewStore.add_images` writes `views/.NNNNNN.tmp` → fsync → rename → journal
append (fsync) → summary; `reload()` recovers orphan directories from `views/`
and `_repair_journal` truncates a torn tail.  That protocol survived review, and
the tests exercise it.  Two caveats: `iter_views(verify=False)` is the default
(session_io.py:1478), so the *replay* path does not check the recorded SHA-256 or
pixel hash unless the caller asks; and because the live controller never stores a
view through it (§2.2), "the production path cannot lose a view" is currently a
property of a code path the wizard never executes.

**(d) White balance applied to the factory profile or double-applied — no double-apply path; the path guard is incomplete.**
`load_profile` refuses a wrong layer, wrong channel order/slot phase, and any
`activation.activated: true`; `write_profile` refuses
`activation.activated`, a non-photometric layer, and existing files without
`overwrite`; nothing in the engine applies the profile implicitly, and there is
no stateful "apply" that could be run twice (`apply` only writes a preview PNG).
The gap is the guard's marker list (§2.3): the real factory profile path is not
recognised as a factory path.

---

## 4. Safety and non-destruction — all CONFIRMED

Base session (`~/einstar-calibration-03`, 51,977,409,627 B, mode 555/444):

```
session.json     b3dd1f649aa51cb3374e32d134fb45d213e02cdfe0cb4713702cb2f57c8cc883
manifest.json    c418520960095511a6deda47b37b475fa2b8578fa3eb2d1e71854e69d77d9c5a
decisions.jsonl  2e87e3498d1c1a657b596fbfed8098a812c0399a1a225cec62e7d7359e4d519a
status.json      12caa220e83538a669dd13a02c3d8594f374da1a58620df779ad1ff7c9394fe3
full tree        find … -printf '%p %y %s %T@\n' | sort | sha256sum
                 6db9b4c12fb3a0a58999332a11d76b74daaa4964c5dacf79514472619341ae7a  (identical before/after)
```

No file appeared, none changed size or mtime, permissions unchanged.
Factory profile `8259f97cc513eea6c25e23f0423be6c6067fabb2fc52bcbe3c01afa70ecda1c4`
and `factory-snapshot-20261111/calibration.bin`
`<redacted>` — unchanged.
`find ~/einstar-private -newermt '2026-11-14 03:00'` → empty.
Extension manifest created during the audit records
`"activation": {"activated": false, "factory_profile_modified": false}` and
`"read_only_base": true` with the four base digests.  `finish()`'s gate is
`coverage_ready` from the combined readiness; the controller-level test and the
4-hold reproduction both show `would_activate: false` while the plan is
unsatisfied.

---

## 5. Test-suite integrity and real counts

### 5.1 Modified existing tests — legitimate

```diff
 tests/test_capture_acquisition_state.py:  kwargs.setdefault("record", True)   # + one explicit record=True
 tests/test_capture_safety.py:             record=True in the child config
 tests/test_wizard_capture.py:             record=True in three configs, comment explains production default
```

All three pin the *diagnostic* mode in tests that are explicitly about the
lossless `.e1r`/`--session-dir` layout, which is exactly what the new default
requires.  The production default itself is pinned elsewhere
(`test_production_storage.py:506–566`: `config.record is False`,
`storage_mode == "production"`, no `--session-dir`, refusal of smuggled
`--record`).  **No coverage was weakened.**  Note, however, that two of these
three files are not published at all (§5.3).

### 5.2 New tests — assert real properties; six skip without private fixtures

| file | tests | skips without private fixtures | what still skips |
|---|---:|---:|---|
| `test_missing_views.py` | 23 | 2 | real session 6/8; recovered 7/8 (the latter never runs in the published tree) |
| `test_white_balance.py` | 35 | 2 | the two real-frame tests (−1.1 %/−1.5 % and the 1.93 % cast) |
| `test_production_storage.py` | 24 | 2 | the two runs that need the built capture binary |
| `test_complete_session_cli.py` | 4 | 0 | — |
| **total** | **86** | **6** | |

Measured with `HOME=/tmp/audit/emptyhome` on the published clone: **101 passed,
6 skipped** for these files (private tree: **86 passed, 0 skipped**).  The
synthetic 4-hold → 8/8 test, the tracker tests, the activation-false assertions
and the store crash-recovery tests are **not** skip-dependent, so the mechanism
is covered on any machine.  What is **only** covered by skipping tests: the real
6/8 and 7/8 counts, the white-balance reproduction and the held-out cast.

Two coverage gaps worth naming: (i) the synthetic 8/8 test fabricates holds with
`store.append_hold`, bypassing `HoldTracker`, so "4 holds through the live
ingest reach 8/8" is not tested end-to-end; (ii) no test uses a base whose
unsatisfied set includes D4 — which is exactly why §2.1 survived.

### 5.3 Real suite counts (run, not trusted)

| suite | command | result |
|---|---|---|
| private tree | `.venv/bin/python -m pytest tests -q` | **578 passed, 8 skipped, 0 failed** in 461.60 s ✓ (claimed 578/8) |
| driver C++ | `ctest --test-dir build/driver-ninja` | **12/12 passed** in 22.09 s ✓ |
| reconstruction C++ | `ctest --test-dir build/recon` | **1/1 passed** ✓ |
| published clean clone (`92cc629`, fresh) | `pytest tests -q` | **441 passed, 79 skipped, 0 failed** in 343.40 s |

Discrepancies in the clean-clone claim:

* the report says **440 passed**; the published HEAD gives **441** — the extra
  test is `test_begin_after_begin_extension_does_not_reset_the_extension`, added
  by the last commit (`9c37cf4` / `92cc629`) after the number was written;
* the published tree **omits 79 collected tests in 11 modules**, including
  `test_ccf.py` (19), `test_capture_acquisition_state.py` (9),
  `test_capture_safety.py` (8), `test_orientation_witness.py` (10),
  `test_offline_features.py` (6) and six wizard GUI/lifecycle modules.  The
  export allow-list in `scripts/export_public.sh` simply does not name them.
  So "440 passed, 79 skipped — the skips are the real-data and hardware tests"
  is not the whole story: the public suite is a different, smaller suite, and the
  safety tests for capture are not in it.  A `--collect-only` count is 586
  (private) vs 519 (public); note that the public run summary sums to 520
  outcomes against 519 collected — a one-item accounting difference I did not
  chase, with 0 failures either way.

---

## 6. Document inconsistencies and overstatements

| # | where | what |
|---|---|---|
| 1 | `exstar_milestone_report.md:196` + `STATUS.md` §0b | "compressed accepted views, CRC-framed append-only journal" presented as the new default; `production_capture.md` §10 says the controller is **not** integrated (236 MB live vs 112.8 MB measured). |
| 2 | `exstar_milestone_report.md:291–294` | the operator command uses the 6/8 base, whose plan is 2 D4 stills + 2 holds and can never be completed (§2.1); the verified workflow needs `--recovered`, which the launcher lacks. |
| 3 | `exstar_milestone_report.md:84` + `STATUS.md:24` vs `exstar_vs…:147–153` | "distortion bit-identical across all sets" vs the specialist's "residual ≤ 6.3e-16" against the device records (A≡N is bit-identical; F differs by ~5e-16). |
| 4 | `exstar_milestone_report.md:100–101` | pairs NEW-CCF 1.420 px with 0.445 px at ×1.01; 0.445 is the 2026-02 CCF — the NEW one gives 0.412 px. |
| 5 | `exstar_milestone_report.md:178` / `white_balance.md:280` | 10.3 %/11.4 % cast not reproducible (§2.5). |
| 6 | `exstar_vs…:121–122` → `report:83` | two wrong absolute cells (fx 1157.8806→1156.9806, cx 617.0906→623.0906). |
| 7 | `exstar_milestone_report.md:212` | "1.876 MB/view" is the 60-view figure; 40 views is 1.8735 MB (trivial). |
| 8 | `exstar_milestone_report.md:198–200` vs `missing_views_workflow.md:404` | §5 says "base 6/8 → recovered 7/8 … 2 qualified dwell intervals"; the 2 belongs to the *recovered* base, the original decision-rate stream reports 4. The specialist doc has the 4-vs-2 table; the summary's juxtaposition invites the wrong reading. |
| 9 | `exstar_milestone_report.md:252–256` | clean-clone "440 passed" is stale (441 now) and the published suite omits 79 tests (§5.3). |
| 10 | `validation.json` gate vs `report:245` | the gate records colour as `not_applicable` — "no colour views in the accepted set; colour stays 'unknown' and must not be marked ok" — while the report quotes the colour extrinsic as agreeing to 0.0005 mm / 0.039° and colour epipolar 0.63 vs 31.3 px. The colour extrinsics are free in the profile (`tex.rx..tz`), so they were estimated from colour observations; the two statements need one reconciling sentence. |
| 11 | `exstar_hardware_state.md:21` vs `report:120–121` | "0x39B" is correct as the *block* start (u32 type=4 there); the three records are at 0x4A7 / 0x9AF / 0xEB7, after a zeroed 264-byte slot. Clear in the specialist doc, potentially read as "the CCF file" in the summary. |

---

## 7. Publication hygiene — clean

Fresh clone `92cc629`:

```sh
grep -rniE "<device-serial>|<device-tag>|<part-number>|<home>/|<mount>/" \
    . --exclude-dir=.git | wc -l    # patterns redacted in this copy
# 0
```

* docs carry `<redacted-device-serial>`, `<redacted-part-number>`,
  `<redacted-production-date>`, `<EXStar-install>`; JSON carries
  `<EXStar-install>` and `~/einstar-private` (home-relative, no username);
* no `.bmp/.e1r/.bin/.pgm` was ever tracked (`git log --all --name-only`);
  largest blob in history is a 2.9 MB analysis JSON; tree is 51 MB;
* `sources/calibration/` (the vendor CCF bytes) is **not** published — good;
* the only judgement call: `analysis/exstar_reference/{ccf_decoded_all,
  device_records,inflash_records,summary,white_balance}.json` publish the
  *decoded numbers* of the factory/device calibration and the vendor corpus
  (including 13 k detection coordinates from the private frames).  That is
  consistent with the docs and contains no bytes, but it is the user's own
  scanner calibration and should be a conscious decision.

---

## 8. What I could not verify

* The video timeline claims (12.03 s vs 102.4 s, 25 poses, 85–90 s missing):
  the container is confirmed (66.400 s, 1992 frames, 1920×1080, 30 fps; audio
  decodes to 3,186,600 samples/channel at 48 kHz, **all zero** at 16-bit, or
  1,062,200 at 16 kHz — the doc's sample count implies a 16 kHz decode), but
  frame-level pose counting was not repeated.
* "53 runs" / the encrypted `algLog` — private vendor logs.
* "split-half 1.2–2.3× σ" — not in `validation.json`.
* Anything requiring the scanner (correctly: no device command was run).

---

## 9. Recommendations, in order

1. Fix the operator path (§2.1): expose the recovered overlay to the launcher, or
   make extension holds count for a planned D4 pose.  Until then, do **not**
   hand the §9 command to the operator.
2. Either wire `ViewStore`/`BoundedRingBuffer` into `_record_accept` or correct
   the milestone/STATUS wording to "raw stream disabled by default; compressed
   views available offline via `compact_session`" (§2.2).
3. Broaden `_FACTORY_MARKERS` and its test (§2.3).
4. Correct the two table cells and the 10.3/11.4 %, 0.445 px and "bit-identical"
   wordings (§2.4–2.5, §6).
5. Rewrite the clean-clone paragraph with the current numbers and state which
   test modules are not published (§5.3).
6. Add the D4-base controller test, and one end-to-end tracker→8/8 test (§5.2).

Machine-readable findings: `analysis/exstar_reference/audit_findings.json`.
