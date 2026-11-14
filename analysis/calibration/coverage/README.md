# Coverage / stationarity validation artifacts

Machine-readable output for `docs/calibration/coverage_stationarity_fix.md`.

## Reproduce

```bash
.venv/bin/python analysis/calibration/coverage/validate_session03.py
```

The script reads `<calibration-session>` and the factory profile
under `<private-inputs>` **read-only**.  It does not re-run detection,
does not decode `raw.e1r`, and does not fabricate raw-frame poses or motion.

## Files

| file | contents |
| --- | --- |
| `session03_validation.json` | before/after measurement, corrected readiness, per-view quality/stationarity rows, dwell-admission hypothetical, raw-frame caveat |
| `d4_attainability.json` | factory-rig projection of the right-corner targets, tilt/roll robustness scan, full-plate feasibility, actual corrected guidance target |
| `d7_stationarity.json` | D7 before/after, accepted-only vs stream counts, dwell intervals, raw-frame caveat |
| `validate_session03.py` | the generator for the three JSON files |

## Key invariants

* D7 **quality** is measured from accepted views' nested
  `quality.metrics.{left,right}.{focus,saturation}`; missing metrics never pass.
* D7 **stationarity** is measured from the supplied observation stream and
  counts **qualified dwell episodes**, not frames or single samples.  An
  episode needs at least `min_dwell_observations=2` consecutive stationary
  samples and `min_dwell_s=0.3 s` of duration; one decision sample separated by
  a long dt only bounds net displacement and is *not* a dwell (the raw 10 Hz
  trace of session 03 moves at 5-1800 mm/s inside those gaps).
  `measure(views, spec)` with no stream reports stationarity as
  *unestablished*, never as a silent pass.
* `dwell=True` views count for D7 but are excluded from D1-D6 pose coverage.
* No threshold was lowered: 3 mm/s, 1.5 deg/s, and >= 6 distinct dwell
  intervals remain the D7 stationarity gate.
* D4 is "zero right-corner visits in the 297 accepted views"; the verified
  52 GB raw recording is being scanned separately for never-accepted
  right-corner frames.  These artifacts do not decode it.
