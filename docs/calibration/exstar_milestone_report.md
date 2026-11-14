# Milestone report — the Windows EXStar reference and what it changed

Date: 2026-11-14.  Scope: the fresh EXStar calibration the user ran on Windows,
the comparison with the factory and with our Linux candidate, and the code that
came out of it.  Every number is from a named document or a test in this tree.

Companion documents:
[`exstar_reference_video_analysis.md`](exstar_reference_video_analysis.md) ·
[`exstar_vs_factory_vs_linux.md`](exstar_vs_factory_vs_linux.md) ·
[`exstar_hardware_state.md`](exstar_hardware_state.md) ·
[`exstar_vendor_corpus_refit.md`](exstar_vendor_corpus_refit.md) ·
[`white_balance.md`](white_balance.md) ·
[`missing_views_workflow.md`](missing_views_workflow.md) ·
[`production_capture.md`](production_capture.md).

---

## 1. EXStar's workflow and its real timings

The supplied recording is **66.400 s, 1992 frames, 1920×1080, 30 fps**, and its
audio track is **digital silence** (1,062,200 samples/channel, all zero).  It is
**not a raw screen capture**: the container is stamped by a consumer editor
(`encoder=https://clipchamp.com`), the geometric and white-balance success
dialogs are 12.03 s apart in the video but **102.4 s apart on disk**, and two
progress bars vanish at 85–88 % and ~22 % without ever completing.  About
**85–90 s of the session are missing from the file.**  Timing was therefore
cross-checked against the EXStar artefacts' filesystem timestamps.

| phase | video time | evidence |
|---|---:|---|
| geometric acquisition (25 poses) | **46.867 s** | 25 accepted views, 1.87 s each |
| geometric solve | 1.733 s | progress bar 1 % → 85–88 % |
| success modal | 4.367 s | operator clicks `Next` |
| white-balance acquisition | **7.666 s** | 2 frames per camera |
| WB success modal | 3.767 s | |
| geometric total | **48.600 s** | |
| white-balance total | **7.666 s** | |

On disk the whole session spans **5 min 03 s** (00:09:52 app start → 00:14:55 WB
commit), of which **3 min 19 s** elapses before the geometric CCF is written and
**1 min 42 s** between the CCF and the WB write.  The difference between the
video (56.3 s of work) and the disk (5 min 03 s) is the deleted footage plus app
startup and operator reading time.

**What the operator actually does.**  **25 poses = 5 orientations × 5 distance
marks.**  Pose 1 is square-on; poses 2–5 are ~30° tilts at four in-plane
rotations.  The operator moves the **scanner**, not the plate (the plate is only
turned for white balance), and the five distance marks can be filled in any
order (five different orders were observed).  **Acceptance is automatic**: no
shutter, no `Start`, no `Finish`, and no mouse pointer on screen for the whole
acquisition.  Feedback is a colour/shape change — grey+blue reticles collapse to
a green disc with a check, a box fills, `Height(N/5)` increments — and guidance
appears only when needed, in imperatives: `Too Far → move it downwards`,
`Too close → move it upwards`.  Two corrective messages in the entire session.

**What it does *not* show:** any residual, coverage map, per-view score or
quality metric.  Completion is a boolean `Calibration succeeded`.  EXStar's own
engine log (`log/algLog/CAL_*_info.log`) is encrypted, so per-view residuals are
not recoverable from it.

**The efficiency lesson is not speed, it is view count.**  EXStar is only ~2.4×
faster per accepted view than our wizard; it asks for **11.9× fewer views**
(25 vs 297).  At EXStar's per-view rate our 297-view session would still take
~9.3 min.  The second lesson is that EXStar *ticks marks while the scanner
moves* — it never rejects a stationary frame as "redundant", which is exactly
the rejection that destroyed our D7 evidence.

---

## 2. EXStar vs factory vs our Linux candidate

Decoded from the CCF records (see the decoding trap in
[`exstar_vs_factory_vs_linux.md`](exstar_vs_factory_vs_linux.md) §1.1: the IR
records are keyed, and quoting the stored doubles as focal lengths inflates `fx`
by 8 px).

| quantity | device factory records | CCF 2026-02 | **CCF 2026-11-14** | **Linux candidate** |
|---|---:|---:|---:|---:|
| IR baseline (mm) | 160.0916 | 158.5570 | **158.6128** | **160.0137** |
| IR relative rotation (°) | 22.0859 | 22.1383 | 22.1290 | 22.1300 |
| colour↔left translation (mm) | 122.8774 | 125.3176 | 125.3362 | 122.8779 |
| colour↔left rotation (°) | 16.9983 | 17.0049 | 17.0004 | 17.0836 |
| IR `fx` L / R (px) | 1158.76 / 1157.27 | 1159.12 / 1158.09 | 1158.44 / 1156.98 | 1159.35 / 1158.55 |
| distortion | kc₁ | **bit-identical to kc₁** | **bit-identical to kc₁** | kc₁ (held) |

* The new EXStar run changed the baseline by **+0.035 %**, the IR rotation by
  **−0.0093°**, and `fx` by **−0.67 / −1.11 / −0.15 px**.
* **Distortion was not re-estimated** — the five Brown–Conrady terms are
  bit-identical to the 2026-02 CCF and equal to the device's factory records to
  within 6.3e-16.  The
  manufacturer holds distortion across every recalibration; our model does the
  same, now on the manufacturer's authority rather than ours.
* The scanner carries **two vendor calibrations that differ by 1 %**: the older
  unkeyed factory records (160.0916 mm) and the CCF branch (158.557/158.6128 mm).
  **Our recovered rig reproduces the factory branch** — 0.08 mm on the baseline,
  0.007° in direction, and 0.0005 mm / 0.039° on the colour extrinsic — while the
  CCF's colour extrinsic is **8°** off.  The "+0.92 % baseline discrepancy" is a
  disagreement *between two vendor datasets*, not between us and the vendor.
* Refitting the vendor's **own 25 fresh images** with our estimator gives
  baseline **160.210 mm** at **0.314 px** RMS and 0.033 px epipolar.  The
  vendor's new CCF explains those same images at **1.420 px**, and a rig scale
  of **×1.0100** collapses it to **0.412 px** (0.445 px for the 2026-02 CCF).  The same +1 % reappears on data we
  did not capture.
* Held-out model selection (293 views) prefers freeing the principal point and
  distortion (0.7512 vs 0.7567 px) — and that moves `fx` **down** and the
  baseline **up**, i.e. **away** from the CCF and onto the device record.  Across
  six model variants the baseline stays in **160.010–160.070 mm**; nothing
  approaches 158.56 mm.

**What this does and does not establish.**  It establishes that our fit is not
the outlier.  It does **not** establish which branch is dimensionally correct:
reprojection error is invariant to the target's metric scale, both branches were
produced with the same 28.0 mm plate, and no local artefact has a certified
length.  No candidate was activated.

---

## 3. USB / hardware investigation

* **Calibration is stored on the device, not only in host files.**  EXStar's own
  log shows `writeCalibrateResultToDev` → `write data , offSet.923` (0x39B, the
  CCF block; its records sit at 0x4A3 / 0x9AB / 0xEB3) at 00:13:11.941 and
  `writeWhiteBalanceResultToDev` → `offSet.4796` (0x12BC) at 00:14:54.314,
  with device acknowledgements (`WriteData result.0`).
* **Verified by readback.**  A single bounded, read-only 4 KiB page-read after
  the Windows run produced a dump with SHA-256 `256dd440…f9121148`; its CCF and
  WB bytes are **byte-identical to the host's new files**, the embedded date
  string changed `2026-02-09 21:34` → `2026-11-14 00:13`, and the identity region
  (`0x000..0x39A`) plus the production records, part number and <redacted-production-date> date
  are untouched.  366 bytes changed in 53 runs, all in the two calibration regions.
* **Firmware/identity match.**  `EinScan10_01_SC130_FX3_V2.10_FPGA_V3.7_EN`,
  3 sensors, 1280×1024 — identical before and after; no firmware update path was
  taken (only a version check with `isForceFWUpdate=false`).
* **Safety.**  Enumeration, `Get*` queries and two read-only page reads; **zero
  writes**, no `0x58` (erase/program), no reboot, no firmware path.  Trigger
  counts **0/0**, illuminator **0 %**, no streaming, no process holding the
  device; the scanner is idle.
* **Hazard recorded for future work:** the CCF block starts at `0x39B` inside a
  4 KiB erase block, so any Linux write-back must be a read-modify-write that
  preserves `0x000..0x39A`; a zero-filled 6568-byte write would destroy the
  factory intrinsics.

---

## 4. White balance — findings and status

**Recovered from the shipped binary**, not inferred: `Sn3DColorCorrect.dll`,
`SNColorAlgorithm::snWhiteBalanceCali`.  Per frame it takes a **centred window**,
rejects clipped pixels, subtracts the per-channel dark level, and sets

```
gain[0] = ΣG / Σslot0      gain[1] = 1 (pinned)      gain[2] = ΣG / Σslot2
```

storing a strictly diagonal matrix.  A failed measurement logs
`whiteBalance false.write default WB.` and substitutes defaults.  The capture
protocol is proven from the Windows log: 5000 µs, **gain 200, LED on** →
`wbdata.txt`; **gain 400, LED off** → `wbdata_noled.txt`; only the colour camera
is reconfigured.

| | previous | new (2026-11-14) |
|---|---:|---:|
| slot 0 gain | 1.622664 | **1.603466** |
| slot 2 gain | 1.766063 | **1.794047** |
| 3×3 CCM, offsets, dark | unchanged bytes | **unchanged bytes** |

White balance is therefore a **separate, versioned, reversible gain layer**, not
part of the geometry and not part of the CCM — the structure our implementation
copies.

**Implemented:** `calib/white_balance.py` (photometric layer, `layer:
"photometric"`, geometry digest `null`, refuses factory paths and mismatched
channel conventions, six refusal branches, exact inverse) plus
`tests/test_white_balance.py` (**35 tests**).  On the real EXStar WB frames the
cast drops from **34.96 % → 1.93 %** (and 37.38 % → 1.59 %) measured on an
annulus *outside* the estimation window, i.e. held-out.

**Two traps, both proven and both avoided:** EXStar's slot 0 is the Bayer phase
at (odd, odd), which lands in slot 2 of a `COLOR_BayerRG2BGR` buffer — copying
the stored matrix directly leaves an **11.3 % / 11.1 %** cast; and the gains must
be applied to **dark-subtracted** data or they over-correct by 11–15 %.

**Not claimed:** colorimetry.  The plate's reflectance is unknown, there is no
chart and no spectrometer.  Which Bayer phase is physically red is also
unresolved; one frame of a known-colour object would settle it and one of our
two debayer mappings is wrong.

---

## 5. Corrections to the Linux wizard

| defect | correction |
|---|---|
| readiness displayed 5/8 while D4+D7 were unsatisfied, and duplicated D7 (previous milestone) | one projection, honest 8-dimension count |
| **no way to add the missing evidence without re-recording** | **Capture Missing Views**: `calib/missing_views.py`, controller extension mode, `tools/wizard/missing_views.py` CLI, launcher `--complete-session`.  The base session is opened **read-only**, verified by digest, and the new evidence goes to a **new** session directory |
| an operator cannot tell a genuine still from a pause | `HoldTracker` (ALIGNING → HOLDING → ACCEPTED, with progress) that separates **intentional movement** (pose change > move threshold → reset) from **micro-instability** (within jitter tolerance → keep counting), and refuses a short or drifting "hold" with a named reason (`hold_too_short`, `hold_unstable`) |
| duplicate dwell frames were rejected as redundant, destroying D7 | in extension mode the dwell frame **is** the evidence; other frames are rejected with stable tokens (`hold_pending`, `not_needed_dwell_only`) so counters stay meaningful |
| a normal session recorded 52 GB "just in case" | production capture is the default: **no `raw.e1r`**, compressed accepted views, CRC-framed append-only journal; the controller holds one 3.93 MB group in flight (the tested 256 MiB ring module is available for multi-frame consumers but not allocated on the live path).  Lossless recording is diagnostic mode |

Verified on the real session: base **6/8** (D4+D7) → recovered overlay **7/8**
(D7 only), plan of **4 holds, ~1.6 min**.  A controlled synthetic extension of
4 × 2 s holds reaches **8/8 READY**, and **activation stays false** in every
assessment and in the extension manifest.

---

## 6. Normal calibration: runtime, RAM and storage

Measured with `tools/wizard/storage_bench.py` on the real session-03 frames
(read-only) and on the replay harness:

**What is live today:** the production default stops the lossless `raw.e1r`
stream (the 52 GB problem) and, since 2026-11-14, the controller writes the
compressed `ViewStore` layout for accepted views. Measured on 24 real views
from the recovered session: **1.69 MB per accepted view vs 3.95 MB as PGM
(2.34× smaller, 40.5 MB vs 94.8 MB for the session tree)**, at +95 ms/view
encode, with no per-group staging writes; `verify` and `resume` pass on the
same session. The legacy `groups/*.pgm` layout remains available through
`storage="legacy"` for old sessions and comparison runs.

| quantity | compressed store (implemented, benchmarked) | lossless `.e1r` |
|---|---:|---:|
| bytes per accepted view (3 cameras + observation) | **1.876 MB** | 175.0 MB |
| 25-view calibration (EXStar's count) | **≈ 47 MB** | ≈ 4.4 GB |
| 40 views | **75.1 MB** | 7.00 GB |
| 60 views | **112.8 MB** | 10.50 GB |
| whole 297-view corpus | 560 MB | **51.98 GB** |

| resource | measured |
|---|---|
| ring buffer, 256 MiB budget | 68 groups / 204 frames / 267,404,288 B / **6.94 s**, 12 evictions, exact accounting |
| store-only working set | 16–21 MB (peak RSS 65.5–69.6 MB vs 47–49 MB baseline) |
| peak RSS with the ring | 284.1 MB |
| encode + fsync per accepted view | p50 **112.7 ms**, p95 **248.2 ms** |
| sustained write rate at 13.24 accepts/min | **0.42 MB/s** (vs 38.63 MB/s lossless) |
| preview / acquisition (replay harness) | **10.001 Hz**, p50 97.7 ms, p95 146.0 ms |
| crash recovery | truncated journal recovers the **intact prefix of 4/5**, torn record rebuilt from `views/`, writable re-open repairs the tail |

Regression limits pinned in [`production_capture.md`](production_capture.md):
a ≤60-view session must stay **< 1 GB** retained (measured 0.113 GB) and must
not record a raw stream; ring ≤ budget; storage working set < 64 MB; encode
≤ 300 ms p95; corpus verify ≤ 30 s.

---

## 7. Numerical accuracy and independent validation

The candidate's colour channel is validated **geometrically** (the colour
extrinsic is free in the fit and its epipolar residual is 0.63 px against 31.3 px
for the shipped CCF), while the solver's own validation record marks the colour
*camera model* `not_applicable` because the accepted set contains no colour-only
views.  Both statements are true and they are about different things: geometry
is validated, colour photometry is not (§4).

| result | value | source |
|---|---|---|
| recovered candidate, 293 of 297 views | RMS **0.6514 px**, held-out **0.770 ± 0.019 px** | `analysis/calibration/solver/final/validation.json` |
| epipolar consistency (ours vs factory CCF) | **0.333 px** vs 0.465 px | same |
| colour epipolar (ours vs shipped CCF) | **0.63 px** vs 31.3 px | same |
| our fit on the vendor's fresh 25 images | **0.314 px**, 0.033 px epipolar | `exstar_vendor_corpus_refit.md` |
| the vendor CCF on its own images | **1.420 px** (2026-02: 1.395 px) | same |
| held-out model comparison | candidate 0.7567; PP free 0.7526; kc free 0.7522; both 0.7512 px | same |
| parity with the device factory records | **0.08 mm** baseline, **0.007°**, colour **0.0005 mm / 0.039°** | `exstar_vs_factory_vs_linux.md` |

Validation gates are unchanged: 17 constrained / 1 weak / 0 unobservable
parameters, condition 1,804, split-half reproducibility 1.2–2.3× σ.  The
candidate remains a **standard-target** result.  It is **not activated**, and
`finish()` still refuses an activatable candidate while the plan is unsatisfied.

Tests in this working tree: **600 Python passed, 8 skipped**; **12/12** driver
C++ tests (`build/driver-ninja`).  Clean-clone verification of the published
tree (fresh `git clone`, tests run from the clone): **463 passed, 79 skipped,
0 failed**, `verify_all.sh --with-driver` including the C++ build; the published
suite is a subset (542 collected vs 608 here) — the skips are the real-data and
hardware tests that need fixtures which are not published.

---

## 8. Remaining limitations and uncertainties

1. **Absolute scale — the 1 % branch question.**  Two vendor calibrations on the
   same device differ by 1 %; ours matches the factory branch.  Nothing local can
   adjudicate which is dimensionally correct.  This is now a *policy* choice
   (reproduce EXStar-on-Windows at 158.56 or the 28.0 mm plate / device record at
   160.09), and it must become explicit profile metadata rather than an accident
   of the fit.
2. **D7 is still unsatisfied** for the recovered session: 2 qualified dwell
   intervals of the 6 required.
3. **No colorimetry.**  White-balance cast removal is validated; colour
   *accuracy* is not.  The Bayer phase that is physically red is unknown, and one
   of our two debayer conventions is therefore wrong.
4. **GUI entry point is now wired.**  The modern window has a
   **Complete calibration…** button (idle/cancelled/error only).  It asks for
   the base session, derives `<base>-ext`, and restarts the wizard with
   `--source live --complete-session BASE --session-dir <base>-ext` after
   releasing the scanner; a live run whose stop cannot be verified refuses to
   start a second process.  The launcher flag still works for scripted use.
5. **EXStar's engine log is encrypted**, so its per-view residuals and acceptance
   thresholds remain unrecoverable.
6. **`fy − fx` disagrees in sign** between our vendor-corpus fit (+1.7 px) and our
   session fit (−1.3 px); sub-pixel focal comparisons between the two are not
   trustworthy.

---

## 9. The user's next physical action

**A. Four to six short poses (≈ 2–3 min).**  This is the *only* capture needed;
the 52 GB recording is not repeated and the 297 views are not re-detected.

```
tools/wizard/gui/app.py --source live \
    --complete-session ~/einstar-calibration-03 \
    --session-dir ~/einstar-calibration-03-ext
```

(the controller will verify the base digests before accepting anything)

The tool plans **4 poses** for this session — two corners and two holds:

| # | kind | what to do |
|---|---|---|
| 1 | still | plate centre in the **top-right** of the image at ~510 mm, one sharp still |
| 2 | still | plate centre in the **bottom-right** of the image at ~510 mm, one sharp still |
| 3 | hold | ~510 mm, tilt ~20° right + 20° up, roll 180°, centre **bottom-left**, hold still for 1 s |
| 4 | hold | ~420 mm, tilt ~20° right + 20° down, roll 90°, centre **bottom-right**, hold still for 1 s |

Each hold shows a progress ring; the pose is accepted only when the ring
completes, so "keep it there until the ring fills" is the whole instruction.
Plate = the original Einstar calibration plate, unchanged.

**If the tool still reports stationarity after that**, do two more 1 s holds at
~340 mm and ~420 mm at a tilt/roll the earlier poses did not use (e.g. 20° left
+ 20° down at 90°, then 20° left + 20° up at 180°).  The reason is a real
measurement difference, not a bug: the planner falls back to the *live decision
stream* for a session that predates the stored dwell stream, and that stream
over-counts intervals (4) where the complete 10 Hz stream qualifies only 2.  The
authoritative count is the stricter one, so plan for up to four holds and let
the readiness gate — not the planner — decide when it is done.

**B. One measurement (≈ 1 minute, no scanner).**  Measure the plate's marker
pitch with a caliper or any instrument good to ±0.05 mm: the distance between
two adjacent marker centres, ideally over 4 pitches and divided by 4.  Nominal is
**28.0 mm**.  That single number decides the 1 % branch (28.0 → 160.09 mm rig,
27.72 → 158.56 mm rig) and is the difference between a "standard-target"
candidate and a dimensionally-defensible one.

Nothing else is blocked on physical interaction.  Everything above the line was
built and verified offline against the real recovered data.
