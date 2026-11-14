# Auto exposure — live audit and repair

**Date:** 2026-11-14
**Scope:** `<calibration-session>` (real high-mode session,
1280 × 1024, 100 ms trigger) and the live wizard path in
`tools/wizard/gui/modern_window.py` / `tools/wizard/exposure.py`.
**Constraint:** read-only on the session and private datasets; no scanner,
firmware, persistent device state or hardware access was used.

**Verdict**

* **Active? Yes.** Auto exposure is enabled by default
  (`gui/app.py: --no-auto-exposure` is `store_true`; the window is constructed
  with `auto_exposure=not args.no_auto_exposure`). The session log contains 93
  entries, `exposure-control.txt`, `status.json` and the device readbacks agree
  at the end (IR 2325 µs, RGB 20000 µs, gains 105/105/300, laser 100 % during
  capture, 0 % after stop).
* **Effective? No.** In the retained 447 s run: **29 / 60 entries are no-ops**
  (48 %), all re-requesting `rgb_gain 300`; the IR loop reversed direction
  **17 times** between 1488 and 2921 µs; RGB was **pinned at 20000 µs / gain
  300 in 58 / 60 entries**; **31 entries are labelled "within target" while
  changing a value**; the accepted-view freeze was bypassed by a full-frame
  `p99 >= 250` test on almost every frame.
* The controller is not the only problem: the retained `exposure.jsonl` is
  append-only across capture runs with no run id, so **33 / 93 entries belong
  to a previous run**.

Machine-readable summary: [`summary.json`](summary.json). Simulation output:
[`before_after.json`](before_after.json). Per-frame classification:
[`real_frame_classification.json`](real_frame_classification.json).

---

## 1. Reconstructing the controller decisions

### 1.1 Run boundaries and log hygiene

| quantity | value |
|---|---:|
| log entries in `exposure.jsonl` | 93 |
| entries from the retained run (matches `ui_metrics.auto_exposure[0]`) | 60 |
| entries from an earlier run appended to the same file | 33 |
| retained-run span | 447.4 s |
| capture wall time (`status.json`) | 516.7 s |
| fields identifying a run | none (before this change) |

The retained run already starts at IR 2338 µs, RGB 20000 µs / gain 300: the
capture child applies an **existing** `exposure-control.txt` on startup
(`VolatileSettingsFile::poll` treats the first stat as "changed"), so a new
recording inherits the previous run's last values instead of the configured
baseline. That is why the controller never had far-distance headroom in the
second run.

### 1.2 What changed, and why the log is misleading

| statistic | retained run |
|---|---:|
| entries that write a value identical to the previous setting | 29 (48.3 %) |
| of those, exactly `{"rgb_gain": 300}` | 29 |
| reason `within target` | 31 |
| … of those that still changed a real value (`rgb_exposure_us`) | 2 |
| reason `IR dark (p50 …)` | 15 |
| reason `IR saturation …` | 14 |
| … of those where saturation ≤ 0.01 **and** p99 < 250 (mean-high down-steps mislabelled) | 2 |
| entries changing IR exposure **and** RGB gain together | 6 |
| IR exposure range / final | 1488–2921 µs / 2325 µs |
| IR direction reversals | 17 |
| RGB settings observed / pinned entries | (16000,300), (20000,300) / 58 |

The no-op is a code defect, not a device defect. The RGB up-branch did:

```python
gain[2] = min(RGB_POLICY.max_gain, gain[2] + RGB_POLICY.gain_step)
changes["rgb_gain"] = gain[2]        # unconditionally non-empty
```

At gain 300 this assigns 300 again and records a "change"; the reason string is
left at its default `"within target"`. The same branch also explains the log
churn: a write every ~3.2 s that the device ignores because the value is
unchanged.

### 1.3 Condensed timeline

| t (s) | event |
|---:|---|
| 0 | start pinned: IR 2338, RGB 20000/300 |
| 0–15 | RGB down-step 20000→16000 and back (a real oscillation at the limit); IR 2338→1870 |
| 40–64 | IR 1875→2343→2928→3660→4575 (five up-steps); first saturation down-step at 4575 |
| 93–180 | IR 4575→3660→2928→2342→2927→3658→2926→3657→4571 (saturation / dark alternation) |
| 180–316 | quiet at IR ~2325 (plate normal) |
| 316–460 | IR 2338→1870→2337→2921→2336→1868→1494→1867 (saturation cascade) |
| 511–600 | IR 1492↔2331 with p50 45–63; at 596–600 it cites `p50 14/16` — full-frame background, i.e. detection was lost |
| 629–696 | IR 2326↔1860↔1488; 696 has saturation 0.117 |
| 677–687 | RGB 20000→16000→20000 (the only RGB exposure movement in the run) |
| 687–896 | IR 1860↔2325, 29 `rgb_gain 300` no-ops; last entry at 447 s |
| end | status: IR 2325, RGB 20000/300, laser 0 % after stop |

The two `p50 14/16` entries are the smoking gun for the metering fallback: a
plate was in view, but the bounding box was missing, so the median came from
the dark background.

### 1.4 Quality-gate interaction (why the operator saw rejections)

`session.json` counters: 866 decisions, **43 rejected for saturation**
(162 accepted, 704 rejected overall). The retained per-decision rows that carry
a `saturated` reason are in the earlier append segment: 50 rows, **36 of them
between 0.010 and 0.012** (median max-channel saturation 0.0108, worst 0.1227).
Those frames were rejected just above 1 %.

The live quality gate therefore used **1 %** at run time, while the advisor also
used 1 % on the **full sensor** and the coverage spec is 5 %
(`spec.max_saturation_frac = 0.05`). The three thresholds disagreed; the advisor
could therefore never accept a frame the coverage gate would have accepted, and
the reference markers — deliberately saturated at 255 DN — supplied most of that
1 %.

---

## 2. Readback

| evidence | value |
|---|---|
| last request (`exposure.jsonl`) | IR 2325, RGB 20000, gain 105/105/300, laser 100 |
| `exposure-control.txt` | `exposure 0:2325 1:2325 2:20000`, `gain 0:105 1:105 2:300`, `laser 100` |
| `status.json` at stop | `[2325,2325,20000]`, `[105,105,300]`, laser 0 |
| `capture.stderr.log` | `warning: volatile settings change rejected: volatile settings: gain camera 2: device status 3 for camera/0x27` |

The final readback matches, but **at least one requested `gain 2:300` write was
rejected** and the wizard never surfaced it. `LiveCaptureProcess.set_volatile`
returns a *file-write* record (`applied: true`), not a device confirmation, and
`_log_exposure` stored the advisory request. There was no readback, no
verification, and no mismatch state. `set_volatile` also updates its own
`_exposure_current` optimistically, so later partial merges assume the rejected
value took effect.

---

## 3. Wiring audit (the questions in the brief)

| question | finding |
|---|---|
| enabled by default? | **Yes.** `app.py` default `auto_exposure=True`; the log proves it ran. |
| applied to the capture child? | **Yes**, through the atomic `exposure-control.txt`; the C++ session polls it and updates `DeviceInfo` only after a successful write. |
| device readback matched? | End-of-run yes; per-write **not checked**. One rejection was invisible to the log. |
| where does `_plate_roi` come from? | `snap["detections"]` → `controller._last_detections`, refreshed on each decision. It is empty whenever detection fails (most rejected frames: no/ambiguous markers). The old code then fell back to **full-frame** statistics. |
| how stale? | The image (`_last_ir_image`) and detection snapshot arrive on different paths and may be one frame apart; the old code had no staleness marker and no reuse policy. |
| metering the plate or background? | **Mixture.** ROI when ≥ 4 markers were detected; the dark background otherwise. The `p50 14/16` entries prove background metering in a live plate session. |
| evaluate at 10 Hz but change ≥ 0.75 s? | Refresh is 100 ms; the GUI gate was 0.75 s **and** `AutoExposure.min_interval_s` was 1.0 s, so the effective evaluation interval was ~1.5 s; two confirmations → **~3.2 s** between changes. |
| freeze after accept too long / effective? | The freeze was not too long — it was **not effective**. The emergency test used full-frame `p99 ≥ 250` or `sat > 2 %`, true whenever markers were in view, so soft changes could fire immediately after an accepted view. During stationary holds the freeze expired after 1.6 s. |

---

## 4. Root causes

1. **Unconditional RGB gain assignment** — no "did it move?" check, so the
   controller wrote `rgb_gain 300` forever (29/60 entries) and labelled it
   `within target`.
2. **Full-frame fallback when detection was lost** — the dark-background median
   (14–45 DN) drove exposure up; RGB reached 20000 µs / gain 300 and stayed
   there (58/60 entries). Most decisions had no usable detection, so this was
   the common path, not an edge case.
3. **Marker clipping treated as plate clipping** — saturation and `p99` were
   taken from the full sensor, where the retro-reflective markers always clip.
   The 1 % rule fired on normal marker growth at close range while the dark
   rule fired at the same time, producing 17 reversals with 25 % up / 20 % down
   steps.
4. **Freeze bypass** — `p99 ≥ 250` in the emergency test, for the same marker
   reason.
5. **Double cadence gate** — 0.75 s in the GUI plus 1.0 s in the advisor made
   the loop slower than either document claimed.
6. **Reasons and logs did not describe the action** — RGB-only changes logged
   `within target`; mean-high down-steps logged `saturation` (two of them at
   p99 214/231); the log stored the request, never the readback.
7. **Sticky control file** — the capture child applies the previous run's
   `exposure-control.txt` at startup, so a new recording starts pinned.
8. **Append-only, run-blind logging** — 33 of 93 entries are from another run;
   a reviewer cannot reconstruct one session from the file.

---

## 5. Changes

Only `tools/wizard/exposure.py`, `tools/wizard/gui/modern_window.py` and
`tests/test_exposure.py` were modified.

### 5.1 Controller (`tools/wizard/exposure.py`)

* `FrameStats` now carries `p95` and a `metering` provenance
  (`"roi" | "stale" | "frame"`); `from_image(..., exclude=points)` masks a
  disk around each detected marker so saturated marker cores are excluded from
  brightness/clipping statistics.
* New `plate_bbox()` and `RoiHold` (2 s hold): a valid detection refreshes the
  box; a recent box is reused as `"stale"`; after the window the metering is
  `"frame"` and **no up-correction is allowed**.
* `CameraPolicy` gains `fast_down=0.60`, `soft_saturation=0.04`,
  `hard_saturation=0.15`, `frame_hard_saturation=0.25`, `clip_p95=250`,
  `headroom_p95=235`, 5 % dead band on both target edges, and
  `step_down=0.85`, `step_up=1.25`.

| condition (metered plate ROI) | action |
|---|---|
| saturation ≥ 15 % or p95 ≥ 250 | **1 observation**, ×0.60 exposure, IR pair together, bypasses freeze and interval |
| saturation > 4 % | ×0.85 after 2 observations |
| p50 > target_high × 1.05 | ×0.85 after 2 observations |
| p50 < target_low × 0.95 and p95 < 235 | ×1.25 after 2 observations |
| p50 < target_low but p95 ≥ 235 | **hold** and report "highlight headroom" |
| p50 outside band but `metering == "stale"` | down/clipping may act; **up is suppressed** |
| `metering == "frame"` | hold unless full-frame saturation ≥ 25 % |
| exposure at limit | gain first, then illuminator; then report the limit once |
| any proposed value equal to the readback | **not emitted** (no no-op writes) |

* Every request is tracked against the next readback with a 2 s grace
  (the child polls the file); a persistent difference is reported once via
  `status()["mismatch"]` / `mismatch_count`. `status()` also exposes
  hardware maxima, IR pair mismatch, last stats, limits, freeze and counters.
* `observe(..., stationary=True)` freezes soft corrections during a stationary
  hold but still allows hard clipping and "urgent" (far outside the band)
  corrections, preventing both hunting and a dark deadlock.

### 5.2 Live wiring (`gui/modern_window.py`)

* Uses `RoiHold` per camera and meters the plate bbox with marker exclusion;
  the full frame is only a gross-clipping fallback.
* Removes the duplicate 0.75 s gate; the advisor owns the 0.75 s cadence.
* Passes the stationarity flag from the last decision to `observe()`.
* Re-establishes the configured baseline once per recording run (from
  `capture.config`, not a hard-coded value), so a run cannot inherit a pinned
  `exposure-control.txt`.
* Logs `requested`, `readback_before`, the `set_volatile` write record, the
  measured stats, the limits and the mismatch; writes
  `exposure-status.json`; adds `auto_exposure_status` to `ui_metrics.json`.
* Log entries now carry a `run` id and the session start time.

White balance was not touched.

---

## 6. Measured after

### 6.1 Tests

| suite | result |
|---|---|
| `tests/test_exposure.py` (was 6 tests) | **26 passed** |
| adjacent GUI/wizard modules (`test_wizard_gui`, `test_gui_extension`, `test_qt_teardown`, `test_wizard_acquisition_ui`, `test_wizard_status`, `test_wizard_lifecycle_coverage_wiring`) | **33 passed** |
| full `tests/` suite | **624 passed, 3 failed, 8 skipped** (1250 s); the failures are `test_wizard_plate3d` (2) and `test_wizard_moving_replay`, unrelated to exposure — `plate3d.py` has concurrent uncommitted edits by another agent and moving-replay passed when rerun alone |

New coverage: no-op suppression at limits, hard-clipping fast-down +
IR pairing, soft saturation confirmation, dead band, far-distance convergence
without oscillation, distance sweep without pinning, stale-ROI fallback,
frame-metering fallback, highlight-headroom hold, freeze vs stationary hold,
readback lag/mismatch, status and reset.

### 6.2 Real accepted frames (35 views × 2 cameras, marker detector re-run)

| classification | legacy rule | new rule |
|---|---:|---:|
| IR ok / up / down | 24 / 11 / 0 | 28 / 7 / 0 |
| RGB ok / up / down | 14 / 21 / 0 | 14 / 21 / 0 |
| disagreements | — | **4 / 70** |

The 4 differences are IR `p50` 63–64 DN: the new 5 % dead band holds instead of
nudging up. On healthy accepted frames the two controllers agree; the legacy
hunting came from rejected/fallback frames, which are exactly what the new
metering and provenance rules fix.

### 6.3 Closed-loop simulation (synthetic plant, **not hardware**)

`replay_compare.py` drives the legacy controller (extracted from git HEAD) and
the new controller through the same 180 s distance schedule (420 → 550 → 320 →
420 mm) and the same detection-loss pattern (2.5 s in every 8 s, matching the
rejected-frame rate). Both start from the observed pinned state. The scene
model is normalised from the real session (plate p50 58 DN at 2325 µs / 420 mm;
marker fraction 0.5 % at 420 mm, 2 % at 320 mm; highlight bloom calibrated from
the `IR saturation 0.011–0.117` log entries).

| metric | legacy | new |
|---|---:|---:|
| writes | 51 | **8** (1 baseline + 7 corrections) |
| no-op writes | 0 in this scenario (29 in the real log) | **0** |
| IR direction reversals | 31 | **1** (the deliberate distance reversal) |
| time IR plate outside 65–150 DN | 34.5 s | **28.5 s** |
| time RGB at 20000 µs + gain 300 | 15.0 s | **0 s** |
| final IR / RGB | 1483 µs / 10236 µs | 1405 µs / 4687 µs |
| final plate p50 | 78.5 DN | 74.3 DN |

Pinned-state scenario (the real end-of-run readback, plate out of view, 40
evaluations):

| metric | legacy | new |
|---|---:|---:|
| writes | 18 | **0** |
| no-op fields | **19** (`rgb_gain 300` every evaluation) | **0** |
| whole-call no-ops | 1 | **0** |

Residual 28.5 s out of band for the new controller is the intended trade: a
2 s stale-ROI hold plus two-observation confirmation after a fast distance
change. It is bounded, monotone (no reversals) and does not pin.

---

## 7. Remaining limits

* **No hardware validation was performed.** The "after" numbers are unit tests,
  real-frame re-analysis and a synthetic closed-loop model; the scanner was
  never opened. A short live run is still required before claiming photometric
  behaviour.
* The scene model is linear and normalised from one session; it cannot prove
  absolute DN or the real hardware response at 20000 µs.
* Stale-ROI fallback deliberately suppresses up-corrections for up to 2 s after
  detection is lost; a fast move away from the plate during a detection outage
  may therefore stay dark slightly longer.
* The baseline reset writes one volatile block at the start of every recording
  run (only when auto exposure is enabled). It intentionally discards manual
  pre-recording adjustments in favour of the launch configuration.
* The quality gate uses 1 %/2 % in different code paths while the coverage spec
  is 5 %; the advisor now measures marker-excluded plate clipping, but the
  decision-time 1 % gate in `calib/` is out of scope here and still rejects
  frames the coverage spec would accept.
* `exposure-control.txt` remains sticky at capture start for launchers that do
  not enable auto exposure; the GUI mitigation does not change the capture
  child's startup semantics (`tools/wizard/capture.py`, not modified).
* `status.json` reports `transfer_timeouts: 320` for this run (0 sequence gaps
  and 0 partial timeouts in the capture summary). It was not investigated here.
* `exposure.jsonl` remains append-only. New entries carry a `run` id; the 33
  legacy entries cannot be attributed retroactively.

---

## 8. Reproduction

```bash
# controller tests
.venv/bin/python -m pytest tests/test_exposure.py -q

# before/after simulation (writes before_after.json)
.venv/bin/python analysis/calibration/auto_exposure/replay_compare.py

# real-frame classification (read-only; writes real_frame_classification.json)
.venv/bin/python analysis/calibration/auto_exposure/classify_real_frames.py
```
