# Final report — EXStar reference, calibration comparison and production workflow

Date: 2026-11-14.  Scope: the fresh EXStar calibration the user ran on Windows
on the same scanner, the comparison with the factory record and with the
recovered Linux calibration, the hardware/white-balance findings, and the
software changes that came out of them.  Every number is from a named document
or a test in this tree; inference is labelled.

Detailed companions:
[`exstar_reference_video_analysis.md`](exstar_reference_video_analysis.md) ·
[`exstar_vs_factory_vs_linux.md`](exstar_vs_factory_vs_linux.md) ·
[`exstar_vendor_corpus_refit.md`](exstar_vendor_corpus_refit.md) ·
[`exstar_hardware_state.md`](exstar_hardware_state.md) ·
[`white_balance.md`](white_balance.md) ·
[`missing_views_workflow.md`](missing_views_workflow.md) ·
[`production_capture.md`](production_capture.md) ·
[`milestone_audit.md`](milestone_audit.md).

---

## 1. EXStar's workflow and exact timings

The supplied recording is **66.400 s, 1992 frames, 1920×1080, 30 fps**, audio
track digital silence.  It is **edited**, not a raw capture: the container is
stamped by Clipchamp, the geometric and white-balance success dialogs are
12.03 s apart in the video but **102.4 s apart on disk**, and two progress bars
vanish before completing.  About **85–90 s of the session are missing**; timing
is therefore cross-checked against the EXStar artefacts' file timestamps.

| phase | measured | evidence |
|---|---:|---|
| geometric acquisition | **46.867 s for 25 accepted poses** (1.87 s/view) | 25 accepted views, automatic acceptance |
| geometric solve | 1.733 s | progress bar 1 % → 85–88 % |
| success modal | 4.367 s | operator clicks `Next` |
| **geometric total** | **48.600 s** | |
| white-balance acquisition | **7.666 s** (2 frames per camera) | |
| WB success modal | 3.767 s | |
| on-disk session | **5 min 03 s** | 3 min 19 s to CCF write, 1 min 42 s to WB write |

**What the operator does.**  25 poses = 5 orientations × 5 distance marks.
Pose 1 is square-on; poses 2–5 are ≈30° tilts at four in-plane rotations.  The
operator moves the **scanner**, not the plate.  Acceptance is automatic — no
shutter, no Start/Finish, no mouse pointer during acquisition.  Feedback is a
shape/colour change (reticles collapse to a green disc, a box fills,
`Height(N/5)` increments), with imperative guidance only when needed
(`Too Far → move it downwards`).  Two corrective messages in the whole session.
No residual, coverage map, per-view score or quality metric is shown; completion
is a boolean `Calibration succeeded`.  EXStar's engine log is encrypted, so its
acceptance thresholds remain unrecoverable.

**The efficiency lesson is view count, not speed.**  EXStar is only ≈2.4× faster
per accepted view than our wizard; it asks for **11.9× fewer views** (25 vs
297).  At EXStar's per-view rate, our 297-view session would still take
≈9.3 min.  EXStar also ticks marks while the scanner moves and never rejects a
stationary frame as "redundant" — the exact rejection that destroyed our D7
dwell evidence.

---

## 2. EXStar vs factory vs our Linux candidate

Decoded from the CCF records (the IR records are keyed; quoting stored doubles as
focal lengths inflates `fx` by 8 px — see the decoding trap in
`exstar_vs_factory_vs_linux.md` §1.1).

| quantity | device factory | CCF 2026-02 | **CCF 2026-11-14** | **Linux candidate** |
|---|---:|---:|---:|---:|
| IR baseline (mm) | 160.0916 | 158.5570 | **158.6128** | **160.0137** |
| IR relative rotation (°) | 22.0859 | 22.1383 | 22.1290 | 22.1300 |
| colour↔left translation (mm) | 122.8774 | 125.3176 | 125.3362 | 122.8779 |
| colour↔left rotation (°) | 16.9983 | 17.0049 | 17.0004 | 17.0836 |
| IR `fx` L / R (px) | 1158.76 / 1157.27 | 1159.12 / 1158.09 | 1158.44 / 1156.98 | 1159.35 / 1158.55 |
| distortion | kc₁ | bit-identical | **bit-identical** | kc₁ (held) |

* The new EXStar run moved the baseline by **+0.035 %**, the IR rotation by
  **−0.0093°**, and `fx` by −0.67/−1.11/−0.15 px.
* **Distortion was not re-estimated**: identical to the 2026-02 CCF and to the
  factory records within 6.3e-16 across all sets.  The manufacturer holds
  distortion across recalibrations; our model does the same, now on the
  manufacturer's authority.
* The scanner stores **two vendor calibrations 1 % apart**: the unkeyed factory
  records (160.0916 mm) and the CCF branch (158.557/158.6128 mm).  Our rig
  reproduces the **factory** branch to 0.08 mm / 0.007° and the colour extrinsic
  to 0.0005 mm / 0.039°; the CCF's colour extrinsic is **8°** off.  The "+0.92 %"
  is a disagreement between two vendor datasets, not between us and the vendor.
* Refitting the vendor's **own 25 fresh images** with our estimator gives
  baseline **160.210 mm** at **0.314 px** RMS and 0.033 px epipolar.  The
  vendor's new CCF explains those same images at **1.420 px**; a rig scale of
  **×1.0100** collapses it to 0.412 px (0.445 px for the 2026-02 CCF).
* Held-out model selection over 293 views prefers freeing the principal point
  and distortion (0.7512 vs 0.7567 px) and moves `fx` down / baseline up — away
  from the CCF and onto the device record.  Across six variants the baseline
  stays in **160.010–160.070 mm**; nothing approaches 158.56 mm.

**What this establishes:** our fit is not the outlier, and the vendor's own
recalibration reproduces their older branch.  **What it does not:** which branch
is dimensionally correct.  Reprojection error is invariant to target scale, both
branches used the same 28.0 mm plate, and no local artefact has a certified
length.  No candidate was activated.

---

## 3. USB / hardware investigation

Read-only throughout: enumeration, `Get*` queries and bounded page reads; zero
writes, no erase/program opcode, no reboot, no firmware path.  Trigger counts
0/0, illuminator 0 %, no process holding the device afterwards.

* **Calibration is stored on the device, not only in host files.**  EXStar's log
  shows `writeCalibrateResultToDev` → offset **0x39B** (CCF) and
  `writeWhiteBalanceResultToDev` → offset **0x12BC** (WB), with device acks.
* **Verified by readback.**  One bounded 4 KiB read-only page read after the
  Windows run produced digest `256dd440…f9121148`: CCF and WB bytes are
  byte-identical to the host's new files, the embedded date changed
  `2026-02-09 21:34` → `2026-11-14 00:13`, and the identity region, production
  records and part number are untouched.  366 bytes changed in 53 runs, all
  inside the two calibration regions.
* **Firmware/identity unchanged:** `EinScan10_01_SC130_FX3_V2.10_FPGA_V3.7_EN`,
  3 sensors, 1280×1024; only a version check with `isForceFWUpdate=false`.
* **Hazard for future write-back:** the CCF begins at 0x39B inside a 4 KiB erase
  block, so any Linux write must be a read-modify-write preserving
  `0x000..0x39A`; a zero-filled 6568-byte write would destroy the factory
  intrinsics.  No such write exists in this tree.

---

## 4. White balance — findings and status

Recovered from the shipped binary (`Sn3DColorCorrect.dll`,
`SNColorAlgorithm::snWhiteBalanceCali`): per frame it takes a **centred window**,
rejects clipped pixels, subtracts the per-channel dark level, and computes

```
gain[0] = ΣG / Σslot0      gain[1] = 1 (pinned)      gain[2] = ΣG / Σslot2
```

storing a strictly diagonal matrix; a failed measurement logs
`whiteBalance false.write default WB.` and substitutes defaults.  The capture
protocol is proven from the Windows log: 5000 µs, gain 200, LED on →
`wbdata.txt`; gain 400, LED off → `wbdata_noled.txt`; only the colour camera is
reconfigured.

| | previous | new (2026-11-14) |
|---|---:|---:|
| slot 0 gain | 1.622664 | **1.603466** |
| slot 2 gain | 1.766063 | **1.794047** |
| CCM, offsets, dark | unchanged bytes | **unchanged bytes** |

White balance is a **separate, versioned, reversible gain layer**, not geometry
and not the CCM.  `calib/white_balance.py` implements it (`layer:
"photometric"`, geometry digest `null`, refuses factory paths and mismatched
channel conventions, exact inverse) with **35 tests**.  On the real EXStar WB
frames the cast drops **34.96 % → 1.93 %** (and 37.38 % → 1.59 %) on an annulus
outside the estimation window, i.e. held-out.

**Two traps avoided:** EXStar's slot 0 is the Bayer phase at (odd, odd), which
lands in slot 2 of a `COLOR_BayerRG2BGR` buffer — copying the matrix directly
leaves an 11.3/11.1 % cast; and gains must be applied to dark-subtracted data or
they over-correct by 11–15 %.

**Not claimed:** colorimetry.  The plate's reflectance is unknown, there is no
chart or spectrometer, and which Bayer phase is physically red is unresolved.

---

## 5. Corrections to the Linux wizard

| defect | correction |
|---|---|
| displayed 5/8 while D4+D7 were unsatisfied, duplicated D7 | one honest 8-dimension projection |
| no way to add missing evidence without re-recording | **Capture Missing Views**: `calib/missing_views.py`, controller extension mode, CLI, launcher `--complete-session`, and a modern-window **Complete calibration…** button.  Base session opened read-only and digest-verified; new evidence goes to a **new** session |
| operator cannot tell a genuine still from a pause | `HoldTracker` (ALIGNING → HOLDING → ACCEPTED with progress) separating intentional movement from micro-instability; named refusals `hold_too_short`, `hold_unstable` |
| duplicate dwell frames rejected as redundant, destroying D7 | in extension mode the dwell frame **is** the evidence; other frames get stable tokens (`hold_pending`, `not_needed_dwell_only`) |
| a normal session recorded 52 GB "just in case" | production capture is the default: **no `raw.e1r`**, bounded RAM, compact accepted views, CRC-framed append-only journal.  Lossless recording is diagnostic mode |
| 297 uncompressed PGM views on disk | accepted views are now written through the **production `ViewStore`** (PNG triplet + observation + view.json) by default; the legacy `groups/*.pgm` layout is `storage="legacy"` only |
| a failed view write could look like a clean session | `snapshot()["storage"]` exposes the error and the candidate verdict gains a `storage_ok` gate, so such a session can never be activatable |
| a resumed session restarted the decision counter at 0 | resume keeps the on-disk decision count/sequence and re-attaches the stored view hashes on a production session |

Verified on the real session: base **6/8** (D4+D7) → recovered overlay **7/8**
(D7 only), plan of **4 holds, ≈1.6 min**.  A controlled synthetic extension of
4 × 2 s holds reaches **8/8 READY**, and **activation stays false** in every
assessment and in the extension manifest.

---

## 6. Normal calibration runtime, RAM and storage

**Storage, measured on 24 real groups from the recovered session** (reproducible
with `tools/wizard/controller_storage_bench.py`; JSON committed as
`analysis/calibration/controller_storage_2026-11-14.json`):

| | production | legacy |
|---|---:|---:|
| session tree | **40,538,377 B** | 94,759,912 B |
| per accepted view | **1,689,099 B** | 3,948,330 B |
| accepted-view files | 40,113,068 B (PNG) | 94,373,064 B (PGM) |
| staging ring retained | **0 B** | 0 B (all accepted) |
| wall (begin → last decision) | 33.20 s | 30.69 s |

Production is **2.34× smaller** end-to-end at **+105 ms per accepted view** for
PNG encode+fsync.  A 60-view calibration therefore retains ≈**113 MB** instead
of ≈236 MB, with **no** raw stream (the 52 GB problem).  `verify` re-hashed 144
files (48/view ×3? — 6 check files/view) in the benchmark and `resume` returned
the correct next index in a separate real 8-view run.

**RAM:** the live controller holds one 3.93 MB camera group plus the compact
stationarity stream — decisions need no multi-frame image history, so the tested
256 MiB rolling buffer (68 groups, 6.94 s at 9.8 Hz) is available but not
allocated on the production path.  The storage path's working set measured
16–21 MB and does not grow with session length.  **Runtime:** live acquisition
and preview stay on the existing ≈10 Hz path (9.96 groups/s, 39.2 MB/s on a
120 s soak); 35-view finalisation is 3.47 s (was 38.94 s); a 60-view solve is
2.53 s; the full 297-view replay takes 98.9 s.

---

## 7. Numerical accuracy and independent validation

* Standard-target candidate: 293 fitted views, **RMS 0.6514 px**, **held-out
  0.770 ± 0.019 px**, epipolar **0.333 px** vs 0.465 px for the factory record.
* Vendor-corpus refit: **160.210 mm**, RMS 0.314 px, epipolar 0.033 px.
* Baseline candidates remain in **160.01–160.07 mm** across six model variants;
  the 158.56 mm branch is not reachable with the 28.0 mm plate.
* Session-03 recovery: full 52 GB stream verified (trailer digest match,
  1,266,761 transfers, 681 MB/s), D4 right corners recovered from the raw
  recording, D7 genuinely 2 qualified dwell intervals (< 6 required).
* Gates refuse insufficient evidence: 17-case failure matrix, 10 orientation
  witness tests, readiness and lifecycle gates; no profile is ever activated
  automatically.

**Test evidence this milestone:** **600 Python passed, 8 skipped** in the
working tree (baseline 579 + 21 new); **15/15** GUI extension tests;
**12/12** driver C++ tests; **1/1** reconstruction C++ test.  The clean published
checkout (exported tree, no private fixtures, its own `scripts/verify_all.sh
--with-driver`) ran **463 passed, 79 skipped, 0 failed** and built the C++ driver
tests **14/14** plus recon 1/1; the result is recorded in `docs/readiness.md`.

---

## 8. Remaining limitations and uncertainties

1. **Absolute scale.**  The 1 % branch question is a disagreement between two
   vendor datasets; only a measured plate pitch or a certified artefact can
   decide it.  This must become explicit profile metadata, not an accident of
   the fit.
2. **D7 stationarity** is still unsatisfied for the recovered session: 2
   qualified dwell intervals of 6 required.
3. **No colorimetry.**  Cast removal is validated; colour *accuracy* is not, and
   one of the two debayer phase conventions is wrong.
4. **GUI extension restart** needs a scanner to exercise end-to-end; the argv
   contract and the refusal path are tested offline, and a live stop that cannot
   be verified blocks the restart by design.
5. **EXStar's engine log** is encrypted; its per-view residuals and acceptance
   thresholds remain unrecoverable.
6. **`fy − fx` disagrees in sign** between the vendor-corpus fit (+1.7 px) and
   the session fit (−1.3 px); sub-pixel focal comparisons are not trustworthy.

---

## 9. The user's next physical action

Nothing else can be settled offline.  The first action is the 4-pose completion
of the recovered session (≈2–3 min, no 297-view recording):

```sh
tools/wizard/gui/app.py --source live \
    --complete-session ~/einstar-calibration-03 \
    --session-dir ~/einstar-calibration-03-ext
```

or click **Complete calibration…** in the modern window (it derives
`~/einstar-calibration-03-ext`).  The plan is two image-corner stills and two
1 s holds; each hold's progress ring must complete before the pose is accepted.
Keep the original Einstar calibration plate; nothing else is needed.

The second action is **one measurement, ≈1 minute, no scanner**: the plate's
marker pitch with a caliper (distance between adjacent marker centres, ideally
over 4 pitches and divided by 4).  Nominal is **28.0 mm**; 28.0 selects the
160.09 mm rig, 27.72 selects 158.56 mm.  That single number decides the 1 %
branch and is the difference between a standard-target candidate and a
dimensionally defensible one.
