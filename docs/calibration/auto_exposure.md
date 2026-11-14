# Safe automatic exposure and illumination

Status 2026-11-14. Implemented for the live capture path. The 2026-11-12
hardware run proved the control path; the 2026-11-14 fresh session showed the
*policy* was ineffective (29 no-op writes, RGB pinned, 17 IR reversals), and
`tools/wizard/exposure.py` was rewritten from that evidence
(`analysis/calibration/auto_exposure/AUDIT.md`). Absolute photometric accuracy
is still not claimed, and no post-fix hardware run has been performed.

## Why

The shipped defaults (IR 1200 µs / gain 105, RGB 5000 µs / gain 180, laser 100 %)
were chosen for a normally lit calibration plate. The IR preview in the live
test measured a median of only 25 DN, so the detector and the operator were
working with a dark image. The old capture path could only change exposure at
startup; changing it required restarting capture and breaking the recording.

## Control path

```
wizard AutoExposure.observe(frame stats)
        │  advice (exposure/gain/laser only when a rule fires)
        ▼
LiveCaptureProcess.set_volatile(...)
        │  atomic write of SESSION/exposure-control.txt
        ▼
C++ capture session polls the file (stat first; read only on change)
        │  validates ranges, calls ControlChannel volatile setters
        ▼
device status: exposure_us, gain, laser_percent (readback)
```

The control file is deliberately tiny and atomic:

```
exposure 0:1500 1:1500 2:6250
gain 0:105 1:105 2:180
laser 100
```

* Unknown lines and out-of-range values are ignored with one stderr warning.
* The live driver applies each value through the existing volatile control
  channel and updates the reported `DeviceInfo` only after a successful write.
* A rejected write leaves the previous setting in place; the session does not
  pretend it changed.
* No persistent device write, flash write or firmware command is involved.
* Every applied change is written to `SESSION/exposure.jsonl` and the final
  settings are in `status.json` and the capture manifest.

## Decision rules (2026-11-14)

Metering is the **detected plate** (bbox from the latest decision) with a disk
masked around every marker, so the deliberately clipped retro-reflective marker
cores do not drive the loop. A valid box is reused as ``stale`` for 2 s after
detection is lost; after that the metering is ``frame`` and **no up-correction
is allowed** (a dark background must never raise exposure).

| condition (plate ROI) | action |
|---|---|
| saturation ≥ 15 % or p95 ≥ 250 | **one** observation, ×0.60 exposure, IR pair together, bypasses freeze/interval |
| saturation > 4 % | ×0.85 after 2 observations |
| p50 > target_high × 1.05 | ×0.85 after 2 observations |
| p50 < target_low × 0.95 **and** p95 < 235 | ×1.25 after 2 observations |
| p50 < target_low but p95 ≥ 235 | hold, report "highlight headroom" |
| p50 outside band, metering `stale` | down-steps allowed, **up suppressed** |
| metering `frame` | hold unless full-frame saturation ≥ 25 % |
| exposure at limit | step gain, then illuminator; report the limit once |
| proposed value equals the last readback | **not emitted** |

There is a 5 % dead band on both target edges. A change needs two observations
(except hard clipping). The advisor evaluates at most every 0.75 s; the GUI has
no second gate. Accepted views freeze **soft** corrections for 1.6 s, and a
detected stationary hold (D7 window) also freezes soft corrections, but hard
clipping and urgent far-out-of-band corrections still act.

The advisor stores the requested values and compares them with the next device
readback within a 2 s grace; a persistent difference is surfaced as
``status()["mismatch"]`` and in the session log. Every log entry carries a
``run`` id.

IR cameras 0 and 1 are always set to the same exposure; RGB is independent. The
advisor uses a stride-4 sample of the 1.3 MP frame, so it costs well under a
millisecond on the preview path.

The UI opt-out is `--no-auto-exposure`. The default is on for live capture.

## 2026-11-14 evidence

| | legacy policy (fresh-01, 447 s) | new policy (closed-loop simulation) |
|---|---:|---:|
| exposure writes | 51 | **8** |
| IR direction reversals | 31 | **1** |
| RGB pinned (µs/gain at max) | 15 s | **0 s** |
| no-op fields / writes | 19 / 0 | **0 / 0** |
| final RGB gain | 300 | **180** |

Closed-loop simulation starts from the observed pinned state (IR 2325 µs,
RGB 20000 µs / gain 300), sweeps 420 → 550 → 320 → 420 mm with 2.5 s detection
loss every 8 s. Real-frame classification of 35 accepted views × 2 cameras:
IR 28 ok / 7 up (legacy 24/11), RGB 14 ok / 21 up (unchanged; at the limit).
The remaining RGB darkness is a hardware/light-budget limit, not a control
loop that can be fixed by more gain — and at least one `gain 2:300` write was
rejected by the device (`status 3`), which the wizard now reports.

## Measured behaviour (2026-11-12, physical scanner)

12 s live run, plate in view:

| quantity | value |
|---|---:|
| IR exposure | 1200 → 1500 → 1875 → 2343 µs |
| RGB exposure | 5000 → 6250 → 7812 → 9765 µs |
| gain | unchanged 105 / 105 / 180 |
| laser | 100 % during capture, 0 % after stop |
| transfer timeouts | 0 |
| auto-exposure log entries | 3 |
| final status readback | matches the last requested values (before cleanup) |

The run is recorded in `analysis/calibration/overhaul/live_validation_2026-11-12.json`.
This is a functional stability test, not a photometric calibration. RGB remains
gated on a valid colour extrinsic; auto exposure does not change that gate.

## Limits

* The Einstar does not expose a calibrated lux meter; the target DN band is an
  empirical operating range, not a measurement standard.
* A change applied while streaming can coincide with a partial USB frame. The
  conservative cadence (≤1 change/s, freeze after accept) keeps this rare, but
  it is not a hard real-time guarantee.
* The illuminator is shared by both IR cameras. Lowering it to protect
  saturation also lowers the stereo signal; the floor is 40 % and the advisor
  prefers exposure/gain first.
* `laser_percent` 0 after stop is a command/readback result. Optical darkness
  cannot be proven from software.

## Plate-ROI metering (fix for hunting)

The first 180 s soak changed exposure **55 times**, oscillating IR between
2340 and 2925 µs. The cause was metering the whole frame: the dark background
dominated the median, so the advisor kept raising exposure until bright
markers/speckle hit the saturation rule, then lowered it again.

The fix meters the plate itself:

* IR/RGB brightness and contrast come from the bounding box of the latest
  detected markers (`FrameStats.from_image(..., bbox=...)`), with 15 % padding.
* Saturation and p99 still come from the full sensor, so bright markers do not
  masquerade as plate exposure and clipping is still detected.
* The colour camera uses its own marker bbox.

After the fix, a 60 s run made **3 changes** total (IR 1200→1500 µs; RGB
5000→9765 µs) and then stayed fixed. A 120 s soak made the same 3 changes,
with preview 9.96 Hz p95 117 ms, 0 transfer timeouts, 3 startup frame drops and
verified cleanup. See `analysis/calibration/overhaul/sustained_soak_120s.json`.
