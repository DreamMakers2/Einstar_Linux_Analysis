# Einstar calibration wizard — operator guide

Status: 2026-11-12.  Offline-rehearsed, stress-tested for six minutes (twice the
length of the session that used to fail), and smoke-tested on the physical
scanner in short capped runs.  The workflow is ready for the single
comprehensive calibration session.

## 1. Before you start

1. Scanner connected over USB 2.0 (High Speed).  `lsusb -d 3267:0003` shows it.
2. The user account may open the device node (this project uses a user-scoped
   ACL; no system-wide udev rule is required on the reference machine).
3. The **original Einstar calibration plate** is mounted and visible, roughly
   0.3–0.7 m from the scanner.  Nothing else should move in front of it.
4. ~80 GB free where the session will be written (one minute of 10 Hz capture
   is about 1.2 GB for the raw stream; the wizard keeps a 120-group frame ring
   and copies accepted triplets out of it).
5. No other program (EXStar, the recorder, the live viewer) may hold the
   scanner: they would make the USB interface busy.

## 2. Launch

```sh
cd <repo>
.venv/bin/python tools/wizard/gui/app.py \
    --source live \
    --session-dir ~/einstar-calibration-01 \
    --mode high
```

`--mode` is `quick` (2 bootstrap views), `standard` (3) or `high` (3, tighter
quality gate and finish criteria).  Use `high` for the real session.

The window shows, left to right:

* **Left IR**, **Right IR** and **colour** previews with the detected markers
  circled in green, plus a coverage map of the plate centre in the fourth tile.
  Each tile carries its own one-line status (markers found, assignment, colour).
* **NEXT MOVE** — the one instruction to follow, sized to fit, with a line of
  immediate feedback under it ("captured view 7", "too fast — hold still", …).
* **3-D PLATE** — the known plate geometry drawn at the estimated pose, with
  distance, tilt and roll.  It shows where the board is and how it is tilted;
  drag it to orbit, double-click to return to the scanner view.  It refreshes at
  10 Hz, drops to 5 Hz if the pipeline is behind, and never takes time from
  acquisition or the solver.
* **SESSION** — accepted / rejected / redundant counts, the eight coverage
  dimensions with the exact hint for each missing one, and a readiness verdict
  ("NOT READY — missing …" or "READY — press Finish").
* **Advanced diagnostics** (collapsed) — per-parameter observability, the
  uncertainty table and the gate detail.
* **ACQUISITION / CONTROLS / decision log** on the right.  The header always
  states the truth: `live · 9.9 fps · 4.6 decisions/s`, or
  `RECORDING · STALLED` with the reason if frames stop arriving.

## 3. The one adaptive sequence

Press **Start recording** when the plate is visible and the previews look
normal (markers circled, not saturated).  Then follow **NEXT MOVE**:

| message | what to do |
|---|---|
| "Hold still for a moment (moving at N mm/s)" | the plate is moving; pause your hand briefly — the wizard will not accept a blurred view |
| "Move closer / farther" | change the plate distance to the band it names (300–380, 380–460, 460–560 mm) |
| "Tilt … left / right / up / down" | rotate the plate out of plane about that axis, both signs |
| "Rotate the plate by N°" | rotate it in its own plane (four sectors, 0/90/180/270) |
| "Put the plate centre in the <corner/edge>" | move so the plate sits in that part of the image |
| "Good viewpoint captured — keep moving" | the view was accepted; move on |
| "This viewpoint is redundant" / "↺ adds nothing new" | you are too close to a pose you already have; make a bigger change |
| "⚠ plate not read — show it fully" | the plate is cropped, at too steep an angle or ambiguous; bring the whole plate into both IR views |
| the colour tile goes blank for a pose | the colour sensor cannot see the plate at this angle (it is offset from the IR pair) — the IR data is still used; keep going |
| "ambiguous marker assignment" | the wizard cannot prove which corner of the plate is which; rotate it slightly (or bring the whole plate into both IR views) — it will not guess |
| "READY TO FINISH" | every dimension is covered; press **Finish** |

You never need to count images, measure the plate, or calculate angles.  The
wizard decides what is informative; identical-looking frames are rejected as
redundant and counted, not stored twice.

A useful rhythm is: move to a pose, hold for a second, move on.  Continuous
motion is fine as long as the plate stays sharp — the wizard measures the blur
and the plate speed itself.

## 4. Controls

* **Pause** stops processing and keeps everything open; **Resume** continues.
  Both are exercised by a test, so pause cannot silently keep consuming.
* **Resume after a crash**: the session is autosaved at least every two
  seconds.  Relaunch with `--resume` on the same `--session-dir` and the wizard
  rebuilds the accepted views, the observations and the calibration, then
  continues.  Verified against an uninterrupted run: same accepted count and a
  final calibration identical to 0.000e+00.
* **Cancel (keep data)** ends the session but keeps every frame, decision and
  the raw recording for later offline work.  No profile is produced.
* **Finish && build profile** runs the final solve, the Phase-7 validation
  gates and writes a **candidate** profile with `validation: pending`.
* Closing the window or pressing Ctrl-C stops the trigger, turns the
  illuminator off and finalises the recording — always.

## 5. What "finished" means

The session is finished when *all eight* coverage dimensions are satisfied:

1. three distance bands, ≥3 views each;
2. out-of-plane tilt on **both** axes, positive and negative (≥15°);
3. in-plane roll in four sectors;
4. the plate centre in the 3×3 image regions, including all four corners;
5. ≥12 shared markers in both IR cameras (partial visibility is fine as long
   as the orientation witness holds);
6. colour participation (≥12 markers) when colour is enabled;
7. sharp, correctly exposed, non-saturated frames;
8. stationary dwell views for the noise estimate.

If a dimension cannot be reached, the wizard keeps naming it in
**NOT READY — missing: …** instead of letting you finish with a bad dataset.

## 6. After the session

```
SESSION/
  session.json  manifest.json  decisions.jsonl  report.md
  groups/000123/cam{0,1,2}.pgm     accepted triplets
  candidate/profile.json           validation: pending
  candidate/validation.json        every gate, metric and reason
  raw.e1r                          lossless USB recording of the whole session
```

Read `report.md` first: it lists the coverage, the observability verdicts, the
validation gates and why the candidate passed or failed.  A candidate that
fails any gate is **never** activated, and the factory profile is untouched.

To activate a validated profile (CLI, after reviewing the report):

```sh
.venv/bin/python tools/wizard/observability_cli.py activate \
    --candidate SESSION/candidate/profile.json \
    --store <private-data>/calib-profiles --serial <redacted>
```

Rollback is a single call and restores the previous revision (or the factory
state when none exists).

## 7. If something goes wrong

| symptom | meaning | action |
|---|---|---|
| previews stay black | illuminator off or scanner held by another process | check the probe (`einstar-capture-frames --probe-only`); close EXStar |
| "0 markers" in one camera | plate not visible in that camera | move so the plate is in both IR views |
| markers circled but no pose | ambiguous or mirrored marker geometry | rotate the plate slightly; the wizard refuses to guess |
| "duplicate / too similar" constantly | moving too little | make a clearly larger change of pose |
| `NOT READY` after a long session | the motion never covered a dimension | follow the missing-dimension hints; they name the pose family |
| validation fails on `stereo_consistency` / `pose_diversity` | the dataset does not support that parameter | do not activate; add the missing pose family and repeat *only* that part |

## 8. Honest limits

* **Absolute scale is not certified.** The plate pitch (28.0 mm) is an input;
  a global rescale leaves every reprojection identical.  A geometrically
  validated profile is produced and safe to use, but without an external length
  artefact it cannot be called metrologically accurate.  The report labels this
  explicitly.
* **Colour stays gated** until an independent colour check passes; the shipped
  factory colour extrinsic is ~18 mm off (≈49 px).
* **Partial plates are accepted only when the geometry proves them.** At an
  image edge the wizard tolerates a marker leaving the frame, but only if the
  missing marker is genuinely outside the usable image; a marker that is
  inside the image and missing (occluded, blurred out, merged) is refused
  rather than guessed.  Three of the four orientation witnesses must remain
  visible, and the plate chirality (`det > 0`) is always checked.  Details and
  measurements: `partial_plate_recognition.md`.
* **Distortion is released only on evidence.** At finish the wizard refits with
  and without freeing the lens model and keeps the factory distortion unless
  held-out prediction improves by at least 10 %.  On the vendor corpus freeing
  it made held-out error *worse* by 4.1 %, so the factory model is kept.
* **Tilt is measured, not assumed.** The wizard reports the plate's actual
  out-of-plane tilt from the pose, and the two tilt axes come from
  `coverage.tilt_deg`.  Note for developers: `calib.adaptive.board_pose` uses
  the opposite axis convention (requesting `tx` shows up as measured `ty`); the
  wizard and the synthetic generator use `coverage.family_pose`, which is the
  exact inverse.  Verified by round-tripping a rendered pose through PnP.
* **Do not expect a 25-view session to fix distortion.** Measured on the vendor
  corpus: freeing distortion made held-out prediction *worse* by 4.1 %; the
  wizard refuses that release unless held-out improves by ≥10 %.
* The existing one-axis sweep is **not** a calibration: it covers one line in
  pose space and the wizard classifies it as not ready, naming exactly what is
  missing.


## Managing profiles from the application

The launcher's **Profiles…** button opens the profile manager:

* every device store found in the profile store root, with the active state
  (`factory` or `revision N`), whether the active revision's manifest verifies,
  and the number of revisions;
* one row per revision with its state and verification result;
* **Roll back to selected** / **Roll back to factory**, which call
  `lifecycle.rollback` - the store's factory guard still applies, and a rollback
  is not a deletion (the revision stays available).

Activation of a *new* candidate remains part of the calibration flow, because it
requires the candidate's validation verdict next to the profile; the manager is
for inspecting and for going back.

`tools/wizard/profile_view.py` provides the read-only description used by the
window, so the same facts can be checked from a script.
