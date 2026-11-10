# Guided one-session calibration recorder

Host-side recorder that turns a single guided capture session into a versioned
calibration profile.  It does not talk to the scanner, does not write to the
device, never activates a new profile, and keeps every candidate frame set
(accepted and rejected) with SHA-256 provenance.

Status: **implemented, offline-tested** on the 25-view vendor corpus and a
probe-style triplet.  A real user session is pending (no scanner attached).

## What it does

1. **Discover** three-camera sets from:
   * `--frames DIR` — `frame_%06d_cam{0,1,2}.pgm` probe triplets.  Hardware
     mapping: **cam0 = right IR (rotated 180°)**, **cam1 = left IR (as
     calibrated)**, **cam2 = colour (rotated 180°)**.  The loader rotates
     `right`/`tex` back in memory and records `rotated_180`; the retained file
     bytes are the original probe bytes.
   * `--vendor-corpus DIR` — `imageLeft/imageRight/imageTex N.bmp` (already in
     calibration orientation; no rotation).
   * `--replay MANIFEST` — explicit sets, with optional `"rotate_180"` and
     per-camera timestamps.
2. **Gate** every set: all three cameras decodable, plate detected, PnP RMS,
   stereo-consistent board pose (catches mirrored/wrong grid assignments),
   dwell/motion, distance coverage, tilt/roll variety and image quadrants.
3. **Accept** a stationary set only once per pose; a repeated pose is not
   double-counted.  Rejected sets stay in the session with their reasons.
4. **Reserve a held-out split** (default 5 of the target) that is never used
   for the fit; report held-out reprojection.
5. **Solve** a host-side replacement profile (LM, free intrinsics/kc by
   default), report the nominal-pitch vs anchored baseline difference, write a
   versioned profile revision and an exported reconstruction profile.
6. **Verify**: `--verify SESSION` re-hashes and re-decodes every retained file
   and recomputes the session content digest.

## Usage

```bash
# one real session (probe triplets)
.venv/bin/python tools/calibsession/calibrate_session.py \
  --frames /path/to/probe_frames \
  --factory-dir sources/calibration/res_Einscan-E10_200x150 \
  --target 24 --out ~/.local/share/einstar/sessions/2026-11-11

# offline evidence on the vendor corpus
.venv/bin/python tools/calibsession/calibrate_session.py \
  --vendor-corpus <private-inputs>/vendor-calib-rapid \
  --factory-dir sources/calibration/res_Einscan-E10_200x150 \
  --out /tmp/session_vendor --target 24 --json

# verify a finished session
.venv/bin/python tools/calibsession/calibrate_session.py --verify /tmp/session_vendor
```

`--mode quick|standard|high` scales the target; `--min-accepted`,
`--min-train`, `--min-heldout` and `--strict` tune the stop rules.  The
planning range is **20–40 accepted sets**; 24 is the default compromise
(16 train + 5 held-out + margin).  Fewer than ~12 cannot support a free
distortion model (see `docs/calibration/covariance_and_stopping.md`).

## Outputs

```
<session>/manifest.json            complete candidate log, decisions, coverage,
                                   stopping, completeness, profile provenance
<session>/sets/<seq>_<label>/      byte-exact copies + per-set meta.json
<session>/accepted.json / heldout.json / candidates.jsonl
<session>/coverage.json / stopping.json / completeness.json
<session>/profiles/                versioned revision (not activated), raw
                                   result.json and exported reconstruction_profile.json
```

Exit status is non-zero when the session is incomplete under the selected
rules; the manifest is still written.

## Evidence

Vendor corpus (25 sets): **21 accepted**, 16 train + 5 held-out, 4 rejected +
1 session-rejected, 3 gated; colour frames present and the plate detected in
**21/21** accepted sets (`hasTexture: true` means participation, not colour
accuracy); completeness passes; `--verify` re-hashes 75 files; stop recommended
after 21 views with held-out RMS 0.21 px.  Solver: baseline 160.10 mm at the
nominal 28.0 mm pitch (equivalent 27.73 mm, 0.975 % below the factory
158.557 mm), not anchored.

Automated tests: `tests/test_calibsession.py` (**18 passed**) cover discovery,
mapping/rotation, plate PnP, replay determinism, completeness, digest
verification and the sequential dwell state machine.

## Safety and limits

* Read-only with respect to the scanner; no firmware, flash, or persistent
  calibration writes.
* A new profile is written as a **revision** and is not activated; the factory
  snapshot is immutable.
* The exported reconstruction profile defaults to `color_status: unknown` even
  when the fit included the colour camera.  Set `ok` only after an independent
  colour check; the reconstruction engine then uses it.
* One unit/language variant only; no absolute accuracy claim (the 28.0 mm pitch
  scale ambiguity is reported, not hidden).
