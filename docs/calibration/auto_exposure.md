# Safe automatic exposure and illumination

Status 2026-11-12. Implemented for the live capture path; validated in a
controlled 12 s hardware run. Absolute photometric accuracy is not claimed.

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

## Decision rules

| rule | action |
|---|---|
| saturation >1 % or p99 ≥250 | reduce exposure 20 %; at minimum, reduce gain; at gain minimum, reduce laser by 10 (floor 40 %) |
| p50 < target low | raise exposure 25 %; at maximum, raise gain; at gain maximum, raise laser by 10 (ceiling 100 %) |
| p50 > target high | reduce exposure 15–20 % |
| contrast too low | treated as under-exposed |
| accepted view | freeze all settings for 1.6 s |
| saturation during freeze | saturation override still applies |

IR cameras 0 and 1 are always set to the same exposure; only RGB is independent.
A change needs two consecutive observations in the same direction (hysteresis),
and evaluations are at least 1 s apart. The advisor uses a stride-4 sample of the
1.3 MP frame, so it costs well under a millisecond on the preview path.

The UI opt-out is `--no-auto-exposure`. The default is on for live capture.

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
