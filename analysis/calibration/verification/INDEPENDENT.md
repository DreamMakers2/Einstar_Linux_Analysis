# Independent verification — milestone 2026-11-14 and fresh-01

**Date:** 2026-11-14
**Verifier:** Agent G (independent; no product code changed)
**Repo:** `<repo>`, HEAD `eee5a9a`
**Interpreter:** `.venv/bin/python` (CPython 3.14, PySide6 6.12.0, numpy 2.5.4, OpenCV 5.0.0)
**Constraints observed:** no scanner, no USB, private datasets read-only
(`~/einstar-calibration-*`, `~/einstar-private`), no push. Scratch work was done in
`/tmp/agv` and removed after collection.

> **Working-tree caveat.** At verification time the check-out was **dirty from a
> concurrent agent** (`M docs/calibration/auto_exposure.md`, `M tools/wizard/controller.py`
> — progress/`stall_hint` thresholds only — plus untracked
> `analysis/calibration/accuracy/` and `tools/wizard/guided_rehearsal.py`). To avoid
> attributing another agent's edits to this milestone, the end-to-end rehearsal was
> run **twice**: once on the working tree and once from a clean
> `git archive eee5a9a tools calib` export in `/tmp/agv/head`. Both runs agree to
> ~1e-7 px / ~1e-5 mm (solver/pool non-determinism); no product file was touched by
> this verification.

> **The tree moved again while verifying.** Uncommitted edits from the concurrent
> agent landed in `tools/wizard/exposure.py`, `tools/wizard/gui/modern_window.py`,
> `calib/guidance.py`, `calib/session.py`, `tools/wizard/coverage.py`,
> `tools/wizard/controller.py` and two test files after the audit. One of them
> implements exactly the EXP-1 fix reported in §4.1 (`RoiHold.box(..., observed_at=…)`,
> `fresh_s`, a `detections_age_s` snapshot field). All line references, test counts
> and conclusions here describe the **milestone commit `eee5a9a`**: the exposure files
> were clean when read and tested, and the E2E was re-run from a clean export of that
> commit.

## Verdict at a glance

| # | claim | verdict |
|---|---|---|
| 1 | reused session dir is isolated, nothing deleted | **holds** |
| 2 | factory profile byte-identical across `begin`/`finish`/E2E; `finish()` never activates | **holds** |
| 3 | §4.2 hybrid rehearsal: 37 accepted, 8/8 READY, validated candidate | **holds — every number reproduces** |
| 4 | exposure audit (no-op, hard/soft, plate ROI, stale ROI, freeze, readback, baseline) | **mostly holds — one real bug missed** |
| 5 | public tree carries no recording/firmware/serial/absolute private path | **holds, with two nuances** |
| 6 | targeted regression tests | **55 passed, 0 failed** |

No number in the milestone report or `fresh-01/REPORT.md` was contradicted. One
behavioural bug in the new exposure loop was found that the audit does not list
(§4.1), and one documentation/indexing ambiguity in §4.2 is recorded (§3.3).

---

## 1. Session isolation — **holds**

Script: `/tmp/agv/isolation.py` (run once, then removed). It builds
`/tmp/agv/iso/session` with a fake `session.json`, `views/000000.json`,
`decisions.jsonl`, `manifest.json`, a `frames/frame_000007_cam{0,1,2}.pgm` triplet
+ `frames_manifest.json`, a 16,402-byte fake `raw.e1r`, `status.json`,
`exposure-control.txt` and `control.txt`; it then calls, in this order:

```python
capture.LiveCaptureProcess(capture.CaptureConfig(session_dir=..., binary=Path("/bin/sh"))).archive_previous_output()
controller.WizardController(factory_profile=<synthetic>, session_dir=<same>, mode="quick",
                            source=<synthetic>, storage="production").begin()
```

Hardware is never touched (`binary=/bin/sh`, no `start()`).

| check | observed |
|---|---|
| capture archive kinds | `frames`, `recording`, `exposure-control.txt`, `control.txt`, `status` |
| destinations | `frames.previous-20261114-102128/`, `raw.e1r.previous-20261114-102128`, `status.json.previous-…`, `exposure-control.txt.previous-…`, `control.txt.previous-…` |
| fake `raw.e1r` | 16,402 B in the archive, **byte-identical** (`== RAW`) |
| archived frame ring | all three PGM triplets + `frames_manifest.json` present |
| fresh frame ring | `frames/` exists and is **empty** |
| controller quarantine | `previous-20261114-102128/` containing `session.json`, `manifest.json`, `decisions.jsonl`, `views/000000.json` |
| old session record | still parses (`{"schema": …, "fake": true}`) inside the quarantine |
| files before / after | 12 / 17; **all 12 pre-existing SHA-256 values still present** somewhere under the dir → nothing deleted |
| new session | `accepted = 0`, `_views = 0`, view store `views = 0` |
| stale exposure/control/status | gone from the root (moved, not dropped) |
| factory hash across `begin()` | unchanged |

The old `views/` is moved into `previous-<stamp>/`; the controller then recreates an
**empty** `views/` for the new production store (so "no views dir at root" is the wrong
test — emptiness is the right one). The two isolation mechanisms use different names
by design: the capture supervisor renames siblings as `<name>.previous-YYYYmmdd-HHMMSS`,
the controller collects artifacts into `previous-YYYYmmdd-HHMMSS/` (both UTC).

---

## 2. Factory protection — **holds**

| subject | before | after | result |
|---|---|---|---|
| real `~/einstar-private/einstar-profiles/factory-e10.json` across the whole E2E | `8259f97c…d1c4` | `8259f97c…d1c4` | identical |
| same file across an in-process synthetic `begin()`+`ingest()`+`finish()` | `8259f97c…` | `8259f97c…` | identical; `mtime_ns` unchanged too |
| the `einstar-profiles/` directory (2 files) across that `finish()` | hashes + mtimes | unchanged | identical |
| synthetic session's own factory profile (36-view `coverage`, 1.5 px perturb) | `c7a78569…` | `c7a78569…` | identical |

Synthetic-session `finish()` result: 36 ingested groups, **33 accepted**, `state=done`,
`ok=true`, `factory_unchanged=true`, candidate + `validation.json` written,
`passed=true`, `would_activate=true`.

Activation is absent by construction: nothing in `tools/wizard/` or
`tools/wizard/gui/` calls `lifecycle.activate(...)` (`grep -rn "activate("` is empty);
`finish()` ends with `_run_lifecycle_validate()` → `lifecycle.validate(...)` and then
re-hashes the factory profile (`controller.py:3341-3348`). The only writes are inside
the session directory. `tests/test_wizard_gui_ready.py` asserts the same hash invariant
and passed.

---

## 3. Real E2E reproduction — **holds, all numbers**

§4.2 does not contain a literal command block (the printed command in §4 is the
surrogate `--rotated-views 180` variant that §4.1 says was rejected). The §4.2
command was reconstructed from the prose. `--rotated-views none` is **not a valid
literal** — the parser yields `["none"]` and `RealViewSource` does
`float("none")` → `ValueError`; an empty value is the correct spelling of "none".

Exact command (clean HEAD export; the working-tree run was identical except
`--session-dir`):

```bash
cd /tmp/agv/head          # git archive eee5a9a tools calib | tar -x
<repo>/.venv/bin/python tools/wizard/gui/app.py \
  --source views --views-dir ~/einstar-calibration-fresh-01 --select 30 \
  --holds 7 --hold-repeats 8 --rotated-views '' \
  --vendor-views 14,22,4 --vendor-dir ~/einstar-private/exstar-20261114 \
  --async-solve --headless --finish --mode high \
  --session-dir /tmp/agv/e2e-head --duration 900
```

| metric | report §4.2 | clean HEAD `eee5a9a` | working tree |
|---|---:|---:|---:|
| accepted views | 37 (34+3) | **37** (34 recorded + 3 vendor) | 37 |
| rejected | — | 56 | 56 |
| coverage / READY | 8/8 | **8/8, `ready=True`** | 8/8 |
| lifecycle `passed` | true | **true** | true |
| `would_activate` | true | **true** | true |
| advisory | quality status fail | **`quality.status=fail`** (`sigma_fx` 0.5682 > 0.35) | same |
| fit RMS (robust) | 2.34 px | **2.34176748** | 2.34176735 |
| cross-camera worst RMS | 0.49 px | **0.4907995** | 0.4907964 |
| cross-camera p95 | 0.92 px | **0.9188989** | 0.9189061 |
| stereo baseline | 160.358 mm | **160.3580534** | 160.3580585 |
| baseline sigma | 0.0486 mm | **0.048626703** | 0.048626725 |
| stereo rotation sigma | 0.0067° | **0.00673923** (`observability.gates.stereo_rotation`) | 0.00673923 |
| colour translation | 0.050 mm / 0.145 px @400 | **0.05005816 mm / 0.14506756 px** | 0.05005827 / 0.14506787 |
| roll bins covered | 4/4 | **4/4** (range 44.15–276.77°) | 4/4 |
| stationarity intervals | 7 (7 holds) | **7** from 51 stationary stream observations; accepted_stationary_views 0 | 7 |
| held-out RMS | — | 0.3323 px (train 30 / test 7) | 0.3323 |

Wall time: ~50 s (working tree, 11:21:41→11:22:31) and ~65 s (clean HEAD export).
The `elapsed_s=900` printed by the launcher is the `--duration` cap, not the run time.

### 3.1 What is real and what is replayed
The 37 accepted views are 34 real fresh-01 PNG triplets plus 3 real EXStar BMP triplets;
the 7 holds are repeated real frames (stream dwell, no accepted duplicates). No rotated
surrogate was present (`rotated` labels: none). Marker counts per camera (left/right/tex) were 38/38/40, 40/40/40 and 35/35/35
for `vendor_15`, `vendor_23`, `vendor_05` — "sharp 40-marker views" is accurate
within the detector's count.

### 3.2 Reproducibility
The candidate `profile.json` differs only in `session.dir`/timestamps (the two files
hash differently: `ae72929b…` vs `83270d9b…`); every fitted number agrees to
~1e-7 px and the report's rounded table is reproduced exactly. No contradiction.

### 3.3 `--vendor-views` indexing (minor, documentation)
`calibrate_session.discover_vendor_corpus()` sorts 1-based corpus files
(`imageLeft1..25`) into a list, and `RealViewSource` indexes that list 0-based
(`sets[index]`). Therefore `--vendor-views 14,22,4` actually loads corpus files
**15, 23, 5**, visible in the decisions as `vendor_15`, `vendor_23`, `vendor_05`.
The report's "indices 14/22/4" are list positions, not file numbers. This does not
change the reproduced result (both runs used the same command), but a reader will
reasonably read "14" as `imageLeft14`.

---

## 4. Exposure code audit — **mostly holds; one real bug missed**

Reviewed `tools/wizard/exposure.py` (656 lines), the `_auto_expose` wiring in
`tools/wizard/gui/modern_window.py:1593-1785`, and the readback source
(`controller.py:5303-5308` ← capture `status.json`). `tests/test_exposure.py`:
**26 passed** (part of the 55 below).

| audit claim | finding |
|---|---|
| no-op suppression ("a change that would not alter a value is never emitted") | **holds relative to the last device readback** — every branch guards its assignment and `observe()` has a final belt-and-braces `out == current` check (`exposure.py:366-378`); an exhaustive 108-scenario sweep produced **0** `changed=True` advisories equal to `current` |
| hard vs soft saturation | **holds** — ROI: `sat ≥ 15 % or p95 ≥ 250` → immediate `×0.60` (`fast_down`), `sat > 4 %` or `p50 > 105 %` of target → `×0.85` after 2 observations; full-frame only `sat ≥ 25 %` (`_classify`, lines 579-601) |
| plate-ROI metering with marker masking | **holds** — padded marker bbox (`plate_bbox`) and a ±24 px square mask per marker (`FrameStats.from_image(exclude=…)`); masked sample falls back to unmasked only if the sample is entirely masked |
| stale-ROI up-suppression | **partial — see 4.1**; `metering=="stale"` correctly blocks "up"/"contrast" for both cameras, but the `stale` label itself is only reachable when the detection list is *empty* |
| freeze handling | **holds** — soft changes suppressed while frozen, hard clipping bypasses (`observe` lines 325-330); probe: frozen soft `False`, frozen hard `True` |
| readback mismatch reporting | **holds** — a lag ≤ 2 s is not reported, a readback stuck for 2.5 s produces one mismatch with `requested`/`readback`/`after_s`; `status()["mismatch_count"]` increments once |
| per-run baseline | implemented and applied once per `_on_start`; two nits in 4.3 |

### 4.1 BUG (missed): stale-ROI freshness is measured from the poll clock, not the detection

`RoiHold.box()` sets `_at = now` **every time it is handed a non-empty point list**
(`exposure.py:174-189`). `_auto_expose` hands it `snap["detections"]`, which is a
deep-copy of `_last_detections` (`controller.py:5389`) — a field updated **only on a
decision/ingest** (`controller.py:1770, 1830, 2034, 4783`), never on preview. The
preview image, by contrast, updates on every frame (`modern_window.py:1029`), and the
worker keeps previewing while a decision is in flight (newest-frame policy,
`worker.py:64-78`). Consequence: for as long as the decision stream lags, the *same*
old detection list is re-presented at 10 Hz and is re-labelled **`"roi"` (fresh)**
forever; the 2 s window never expires.

Probe (`/tmp/agv/exposure_probe.py`, removed):

```
RoiHold(hold_s=2).box(same 4-point list, t=0)   -> ("roi",  box)
RoiHold(hold_s=2).box(same 4-point list, t=100) -> ("roi",  box)   # never "stale"
```

Downstream effect with the *same* background-dark statistics (p50 30 DN, metering
chosen by the window):

| metering label | advisor result |
|---|---|
| `"roi"` (what the window produces for a retained detection list) | after 2 confirmations **IR 1200 → 1500 µs** (`IR dark (p50 30)`) |
| `"stale"` | held (`at limit: IR metering stale; holding`) |

So the audit's statement "a 2 s stale ROI reuses the last box but never raises
exposure from background" is true only for **detection loss** (empty list). For
**detection staleness** (decision lag/stall with the plate moving out of the old box)
the guard is bypassed and the loop can raise exposure from background — the exact
failure mode the audit set out to remove. fresh-01 itself measured decision p95
1.22 s / max 15.4 s; post-fix decisions are ≤ 0.87 s, so the exposure is bounded in
practice, but the invariant is not enforced. Fix: carry the ingest monotonic
timestamp with the detections and age the ROI from it (e.g. `RoiHold.box(points,
now, observed_at=…)`).

### 4.2 Secondary: identical re-requests while a write is unconfirmed

`_pending`/`_unconfirmed` are only used to *detect* a mismatch
(`exposure.py:531-556`); nothing suppresses a new advice while a request is in
flight. Probe: plate dark, device readback frozen at 1200 µs (a clamped or rejected
write) → `observe()` emits the **same** 1500 µs request **3 times** at the 0.75 s
cadence (2 duplicates), before the 2 s grace even expires. A clamped/rejected write
therefore rewrites `exposure-control.txt` every 0.75 s for as long as the condition
persists, which contradicts the spirit of "strict no-op suppression" (it is a no-op
relative to the file, not to the readback). The mismatch itself *is* reported, so the
audit's readback claim is not false — only the "no-op" framing overstates it.

### 4.3 Minor observations
* The baseline reset writes through `capture.set_volatile()` but is **not** entered
  into the advisor's `_pending`/`_unconfirmed`, so a rejected *baseline* write is
  never reported as a mismatch (only advisory writes are tracked).
* The baseline reset runs on the first GUI refresh where `state == "recording"` and a
  capture readback exists — i.e. **after** the capture child has started, so it cannot
  precede the first captured frames. It does take effect (the write is logged and the
  child polls the file), and `archive_previous_output()` already removes the sticky
  file, so no harm was demonstrated; the report's wording ("a new session does not
  inherit a pinned exposure file") is achieved primarily by the file move, not by this
  write.
* A hard clip bypasses freeze/interval for **both** cameras, so a soft RGB correction
  can ride along in the same evaluation.
* `fast_down=0.60` is ≈1.3 stops, not "one stop".

---

## 5. Privacy / publication — **holds, two nuances**

Public check-out `<repo>` at **`e88f269`** (one docs
commit after the `2ab35df` mirror cited in §7), working tree clean, 770 tracked files,
101 MB.

| check | result |
|---|---|
| strings `/home/user` | **0 files** |
| exact 16-char device serial (from the factory snapshot; value not reproduced here) | **0 files** |
| redacted serial pattern (as recorded; first/last 4 chars masked here) | **0 files** |
| `.bin/.img/.e1r/.pgm/.bmp/.tif/.exe/.dll/.zip/.7z` tracked | **0** |
| firmware payloads | none — no `sources/` firmware tree; only `analysis/firmware/*.json|csv` summaries and docs |
| private images committed | **0** of the 104 tracked images is byte-identical to any of the 567 private recording images (`~/einstar-calibration-fresh-01/views/**` PNGs + `~/einstar-private/exstar-20261114` BMPs) |
| recordings committed | none (`git ls-files` has no `.e1r`) |
| factory backup file | none |

Nuances, all consistent with the report's exact §7 wording ("no **absolute** private
path") but worth recording:

1. **Home-relative private paths remain**: 45 files contain `~/einstar-private/...`
   and 22 contain `~/einstar-calibration*` (including
   `fresh01/REPORT.md:3` and `exstar_reference/detect_vendor.py:52`). These name the
   private directories; they are not absolute. Other analysis files were scrubbed to
   `<private-inputs>/...` (44 files).
2. **The factory fingerprint is public**: 13 files carry the real factory profile
   SHA-256 `8259f97c…d1c4` and `<private-inputs>/einstar-profiles/factory-e10.json`.
   Two published `analysis/.../profile.json` files are `kind: candidate` (the
   project's own output) but share 6 factory intrinsics and the factory distortion
   coefficients (held, not freed). No backup file is committed, but the factory
   calibration is partly reconstructible from the published analysis — a reader of
   §7 should read "no backup file", not "no factory parameter content".
* `scripts/export_public.sh` is not in the public tree, so the export step itself is
  not reproducible from the published check-out.

No push was performed.

---

## 6. Targeted regression — **55 passed, 0 failed**

```bash
.venv/bin/python -m pytest \
  tests/test_session_hygiene.py tests/test_async_solve.py tests/test_dwell.py \
  tests/test_roll_sectors.py tests/test_real_view_source.py tests/test_exposure.py \
  tests/test_wizard_worker_budget.py tests/test_wizard_gui_ready.py -q
# 55 passed, 14 warnings in 172.27s
```

| file | tests |
|---|---:|
| `test_session_hygiene.py` | 5 |
| `test_async_solve.py` | 2 |
| `test_dwell.py` | 2 |
| `test_roll_sectors.py` | 5 |
| `test_real_view_source.py` | 2 (ran; fresh-01 present, **not skipped**) |
| `test_exposure.py` | 26 |
| `test_wizard_worker_budget.py` | 10 |
| `test_wizard_gui_ready.py` | 3 |

No failures and no skips. Per instructions the full ~8-minute suite, the public
check-out suite and the driver CTest were **not** re-run, so §5's
"651 passed / 8 skipped", "514 passed / 79 skipped" and "driver 12/12" remain
unverified by this report.

---

## 7. Contradictions and limits

* **No numeric contradiction** to either report was found.
* Imprecisions (none material to the claims): §4.2 has no literal command block;
  `--rotated-views none` is not a valid literal; "indices 14/22/4" are 0-based list
  positions (`vendor_15/23/05`); `previous-<utc>/` is actually
  `previous-YYYYmmdd-HHMMSS[.previous-…]`; `fast_down` is 0.60 (≈1.3 stops).
* **Unproven here:** anything requiring the scanner (exposure behaviour on a device,
  the RGB-dark limitation), the full/public suites and driver CTest, and the
  performance timings (solves, decision latency) — those were not re-measured.
  The exposure "after" numbers remain unit tests / simulation, as the audit itself
  states.

## 8. Evidence

Scratch scripts and outputs lived in `/tmp/agv` (`isolation.py`, `isolation.json`,
`factory_protect.py`, `factory_protect.json`, `exposure_probe.py`,
`exposure_probe.json`, `e2e/`, `e2e-head/`, `head/`) and were removed after this
report was written. The E2E sessions are reproducible with the command in §3; the
public-tree scan is reproducible with `git grep`/`git ls-files` in the check-out.
