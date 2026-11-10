# Guided one-session calibration recorder

Host-side recorder that turns **one** guided capture session with the original
41-marker Einstar plate into a complete, verifiable session and a versioned
calibration profile. It does not talk to the scanner, does not write to the
device, never activates a new profile, and keeps every candidate frame set
(accepted and rejected) with SHA-256 provenance.

Status: **implemented and offline-tested** on the 25-view vendor corpus, on
synthetic declared-detection sessions and on probe-style triplets. A real user
session is pending (no scanner attached).

---

## 1. Hardware stream mapping and orientation (read this first)

The probe's stream order is **not** left / right / colour. The mapping is fixed
by the hardware reference and encoded in `recon/src/frame_set.cpp`
(`kStreamRightIr = 0`, `kStreamLeftIr = 1`, `kStreamColor = 2`) and
`libeinstar/src/calibration.hpp`:

| probe file | stream | calibration camera | orientation of the file |
|---|---|---|---|
| `frame_%06d_cam0.pgm` | 0 | **right IR** | rotated 180° |
| `frame_%06d_cam1.pgm` | 1 | **left IR** | as calibrated |
| `frame_%06d_cam2.pgm` | 2 | **colour** | rotated 180° |

`--frames` maps `cam0 → right`, `cam1 → left`, `cam2 → tex`, and rotates
`right`/`tex` back by 180° **in memory**. The retained copy is the original
probe file, byte for byte (same SHA-256), so the raw evidence is never
rewritten; per-image `rotated_180` is recorded in the manifest. The vendor
corpus (`imageLeftN`/`imageRightN`/`imageTexN`) is already in calibration
orientation and is not rotated. Replay manifests may declare
`"rotate_180": ["right", "tex"]` when their files are probe-oriented.

Evidence: `docs/reconstruction/audit.md` §3, `docs/reconstruction/status.md`
and `recon/src/frame_set.cpp` (`calibration_image()` rotates streams 0 and 2).
With the old assumption the real capture yielded 2/400 feature matches; with
the correct mapping 292/300.

**Unobservable by calibration alone:** a *global* left/right swap or a *common*
180° rotation of all streams leaves reprojection residuals unchanged (the fit
absorbs it into the extrinsics). Only the hardware mapping disambiguates it.
What the recorder *can* check is a single stream being upside down: the fitted
left–right relative rotation is compared with the factory CCF, and a >10°
discrepancy fails completeness (`factory_rig_check.orientation_ok`). Use
`--frames-rotation as-calibrated` only if the files really are already
oriented.

---

## 2. Operator workflow

1. **Use the original plate.** The recovered 8×5, 41-marker board with 28.0 mm
   pitch. Do not substitute a printed ChArUco/AprilTag target: the plate is the
   mechanical reference and its geometry is what the factory CCF describes.
2. **Capture into one directory** with the probe:
   `frame_%06d_cam0.pgm`, `frame_000000_cam1.pgm`, `frame_000000_cam2.pgm`, …
   Move, hold still, and let the probe emit the set at the dwell. In the live
   `--frames` path the first frame after a move is rejected as motion and the
   stationary repeat is kept (one accepted view per pose).
3. **Run the recorder** (host-side; no device access):

   ```bash
   HOME=/tmp OPENBLAS_NUM_THREADS=1 .venv/bin/python \
     tools/calibsession/calibrate_session.py \
     --frames frames \
     --out ~/.local/share/einstar/sessions/2026-11-11_run1 \
     --target 24
   ```

   The shipped factory CCFs are used as the initialiser automatically when
   present (`--factory-dir` to override, `--no-factory` to disable). The
   factory snapshot is copied once and never overwritten; the new revision is
   saved but **not activated**.
4. **Watch the decisions.** Each set prints `ACCEPT` or `reject` plus exact
   reasons. Rejected sets are still copied into the session directory.
5. **Follow the guidance.** After a rejection or acceptance the tool can report
   the next target pose (`stopping.json` → `recommended_additional_views`).
   The stop decision is the session's observability report, not a hard count
   (see §7). `--stop-when-ready` ends as soon as the session and the data
   minima agree.
6. **End only when completeness is OK.** If not, the report names the exact
   missing item: more diverse sets, a distance bin, a tilt sign, roll variety,
   quadrants, held-out count, colour participation, a corrupt copy, or the
   profile.
7. **Verify** the finished session:

   ```bash
   .venv/bin/python tools/calibsession/calibrate_session.py \
     --verify ~/.local/share/einstar/sessions/2026-11-11_run1
   ```

   Exit code `0` = complete and every byte re-hashed; `2` = incomplete or
   corrupted; `1` = input/configuration error.

### Offline evidence on the vendor corpus

```bash
.venv/bin/python tools/calibsession/calibrate_session.py \
  --vendor-corpus <private-inputs>/vendor-calib-rapid \
  --factory-dir sources/calibration/res_Einscan-E10_200x150 \
  --out /tmp/session_vendor --target 24
```

Measured with this build: 25 candidates → **21 accepted** (16 train + 5
held-out) and 4 rejected (1 duplicate, 3 stereo-inconsistent); complete
manifest, `--verify` OK, stop recommended at 21 views. With `--localize auto`
the run takes ~10 s; with a warm detection cache and `--localize none`,
~3.5 s. The result is a 160.10 mm baseline at the nominal 28.0 mm pitch
(factory CCF 158.557 mm; equivalent pitch 27.73 mm, 0.975 % difference) — the
difference is reported, not hidden. Held-out median reprojection is 0.34 px
(0.22–0.53 px across the five views); training RMS is 0.37 px.

The corpus also contains one deliberately useful failure: vendor set 9 has a
single-camera PnP residual of 0.57 px, yet its implied left/right rig rotation
is 179.5° — a mirrored grid assignment. The recorder's stereo-consistency gate
rejects it; a per-view RMS gate alone would not. Sets 16 and 17 are rejected
the same way.

---

## 3. Inputs

Exactly one of:

### `--frames DIR`
`frame_%06d_cam{0,1,2}.pgm` (also `.ppm`, `.png`, `.bmp`, `.tif`) triplets.
Mapping/orientation as in §1. An optional `timestamps.json` supplies measured
per-camera timestamps:

```json
{"0": {"left": 12.001, "right": 12.0014, "tex": 12.0011},
 "1": [12.401, 12.4013, 12.4010]}
```

Camera names, `cam0/1/2`, or a 3-element list are accepted. Without the sidecar
the sets are still grouped by capture index (declared synchronisation); the
manifest records `sync.state = "declared"`, not `"measured"`. `--dwell auto`
uses the live motion gate for `--frames`; `--dwell assume` treats every set as
an already-committed keyframe.

### `--vendor-corpus DIR`
`imageLeftN.bmp`, `imageRightN.bmp`, `imageTexN.bmp`, grouped by `N` into
three-camera sets. Files are already in calibration orientation (no rotation).

### `--replay MANIFEST`
A JSON list (or `{"sets": [...]}`) of explicit sets:

```json
{"sets": [
  {"label": "s000",
   "timestamp": 0.0,
   "camera_timestamps": {"left": 0.0, "right": 0.0003, "tex": 0.0001},
   "cameras": {"left": "l.png", "right": "r.png", "tex": "c.png"},
   "rotate_180": ["right", "tex"],
   "detections": {
     "left":  {"3,2": [612.4, 480.1], "4,2": [671.9, 479.4]},
     "right": {"3,2": [452.0, 481.2]},
     "tex":   {"3,2": [610.7, 479.8]}}}
]}
```

`detections` is optional: when present the recorder uses the declared
`(u,v) → (x,y)` observations instead of running the detector (exact replay of
a recorded detection result), and the manifest records
`detection_source = "replay-manifest"`. Paths are relative to the manifest.
Duplicate labels, unknown cameras and missing declared files are hard input
errors.

### `--plate-profile cols=8,rows=5,pitch=28.0`
Default from `calib.plate`. Other topologies are rejected explicitly: the
large-marker code is defined for this 41-marker plate.

Other useful flags: `--mode quick|standard|high`, `--target 20…40`,
`--localize auto|conic_perspective|centroid|none`, `--cam-map` (diagnostics),
`--max-sets`, `--cache`, `--serial`, `--profile-root`, `--no-profile`,
`--no-export`, `--anchor-baseline`, `--strict`, `--force`, `--quiet`,
`--json`.

---

## 4. Session directory layout

```
<session>/
  manifest.json            every candidate: decisions, reasons, metrics, pose,
                           per-file SHA-256, train/heldout label, coverage,
                           stopping, completeness, profiles, content_digest
  config.json              inputs, plate, mode, rules, holdout policy
  candidates.jsonl         append-only candidate log (one object per set)
  accepted.json            accepted views with split labels
  heldout.json             validation-only views
  coverage.json            distance / tilt / roll / quadrant coverage
  stopping.json            session stopping report + final quality report +
                           the concrete additional views it would ask for
  completeness.json        every check with value/limit/pass and `missing`
  candidates_index.json    candidate-log digest
  sets/
    0000_vendor_01/
      imageLeft1.bmp       byte-exact copy of the source
      imageRight1.bmp
      imageTex1.bmp
      meta.json            per-file hash/size/shape/rotation, decision
  profiles/
    profile_manifest.json  saved revision, export, activation state
    result.json            raw solve result (`export_profile.py --result` input)
    exported/reconstruction_profile.json
    unknown/               ProfileStore: factory/ (immutable), revisions/000N/
```

Source images are never modified. Each copy's hash is verified against the
source after copying; `--verify` re-hashes and re-decodes every copy.

---

## 5. Per-set gates

Evaluation uses the existing `calib` stack:

* `calib.detect.detect_grid_auto` on all three cameras, with optional
  `calib.localize.refine_grid(method="auto")` on committed sets;
* `calib.guidance.evaluate_frame`: ≥16 markers per camera, ≥16 common
  left/right markers, the three on-lattice large markers, plate area 3–92 %,
  saturation <2 %, IR/colour contrast and focus thresholds, timestamp skew
  ≤3 ms when measured;
* **stereo-consistency pre-gate** (recorder extension): per-camera PnP RMS
  ≤3 px and the left/right pose implied by the two IR cameras must agree with
  the current fit to ≤2° / 6 mm. This catches mirrored grid assignments that a
  single-camera check cannot;
* board pose from the current/factory intrinsics; duplicate check against every
  accepted pose; predicted task-space gain from
  `calib.session.CalibrationSession`;
* dwell: `sequential` rejects board motion >3 mm / >1.5° between consecutive
  input sets, so the first frame after a move is rejected and the stationary
  repeat is accepted once. `assume` treats each set as a keyframe; duplicate
  and coverage checks still apply.

Every rejection is recorded with its exact reason and metrics. Nothing is
silently dropped.

### Adaptive acceptance, bootstrap and coverage

`GuidedSession` extends (does not rewrite) `calib.session.CalibrationSession`:
3–5 diverse bootstrap views, then a gain/new-coverage-bin acceptance rule,
then stricter refinement. Coverage is tracked in distance (200–600 mm, five
bins), signed tilt in x and y (both signs relative to the median pose), tilt
magnitude, roll, image quadrants, stereo overlap and large-marker visibility.
The acceptance decision and the task-space gain are the parent session's; the
recorder only adds the stereo gate and the held-out/train split.

---

## 6. Held-out reservation and why it is not free

Five validation-only views are reserved by a deterministic **online** rule:
after the bootstrap (4 views), every third accepted view becomes held-out —
accepted views 6, 9, 12, 15, 18 in the default target-24 plan. A candidate that
is too similar to an existing held-out pose is skipped and the next accepted
view is tried. The cadence is `max(3, round(target/8))`, so a 24-view plan
reserves five by accepted view 18; a 40-view plan paces them further apart.

* Held-out observations are **never passed to any training solve**. The
  `GuidedSession` subclass filters them out at the `_maybe_solve` seam; the fit
  that drives guidance, stopping and the final profile sees only train.
* The manifest labels every set `train`, `heldout` or `rejected`. Held-out RMS
  solves the board pose from the left held-out image only and predicts the other
  cameras through the fitted rig.
* **Tradeoff:** five views are removed from the fit. On the corpus, 21 accepted
  leaves 16 training views. The benefit is generalisation evidence that no
  training residual can fake (held-out 0.34 px vs training 0.37 px on the
  corpus; 0.16 px vs 0.14 px on synthetic data). The cost is a slightly weaker
  fit and five extra captures. If the session ends before five held-out views
  exist, completeness fails and names the next reservation index; the tool
  never silently keeps a smaller held-out set.

---

## 7. Completeness rules and stopping

`completeness.json` has every check (`name`, `value`, `pass`, `detail`) and an
exact `missing` list; the process exits `2` if anything is missing. Mandatory:

1. **Counts**: accepted ≥ `--min-accepted` (15), train ≥ `--min-train` (10),
   held-out ≥ `--min-heldout` (5).
2. **All three cameras** decoded/stored for every accepted set; the colour
   camera must be decodable in ≥90 % of *all* candidates and see the plate in
   ≥80 % of accepted sets (`hasTexture`-like state). A systematic colour loss
   fails clearly — this is the previous `hasTexture:false` failure mode.
3. **Consistent image size** per camera across accepted sets.
4. **No corrupt/truncated files**: every stored file re-hashed and decoded
   (PGM payload length, BMP size field, OpenCV decode).
5. **Coverage**: ≥3 of 5 distance bins, both signed-tilt signs (≥5° from the
   median), ≥12° maximum tilt, ≥75 % of the 2×2 quadrants for left/right IR,
   ≥16 stereo and ≥3 large markers per accepted set. Roll variety is reported;
   ≥2 roll bins is mandatory only with `--strict` (which also raises distance
   bins to 4, tilt to 20° and quadrants to 100 %).
6. **Sync evidence** for every accepted set: `measured` (timestamps, skew
   ≤3 ms) or `declared` (capture-index grouping). `unknown` fails.
7. **Dwell**: accepted sets are stationary keyframes; `moving` fails.
8. **Stereo gate** passed for every accepted set.
9. **Orientation**: fitted left/right rotation agrees with the factory rig
   within 10°; an upside-down single stream fails here.
10. **Profile**: a host-side revision was saved and is not activated; export is
    reported (failure is recorded, not silently ignored).

**Stopping is separate from completeness.** `--target 24` is a planning range
(20–40; accepted between 12 and 60), not a hard count. The recorder stops only
when `session.stop_decision()["stop"]` (the multi-gate quality report plus a
measured/predicted diminishing-return test), the accepted/held-out minima and
the coverage minima all agree (`--stop-when-ready`). If input is exhausted
earlier, the session ends incomplete and `stopping.json` lists the concrete
additional views the session would ask for (distance bin, tilt sign, roll,
quadrant, count, or the next best pose and operator instruction from
`session.recommend()`).

The output reports accepted/rejected counts, coverage, held-out RMS, final
training RMS, baseline and equivalent pitch, colour participation and a
`hasTexture`-like state, the stopping report, the recommended additional views,
and an **operator-effort estimate**. Effort uses the documented
`calib.guidance` model (`0.15 + lateral/150 + depth/150 + angle/25` units) and
an explicitly INFERRED wall-time model (6 s/unit + 1.5 s/view); it is not
measured human data.

---

## 8. Profiles, activation and export

The final solve uses the **training** observations with the LM backend
(`calib.solvers.make_problem` + `solve_joint(backend="lm")`, SciPy/engine
fallback), drops views with >3× the median per-view RMS if ≥8 views remain,
then:

* writes `profiles/result.json` (raw solve, exporter input);
* saves a `calib.profiles.ProfileStore` revision with CCF records, covariance
  and parameter names;
* snapshots the factory CCFs once into `factory/` and never overwrites them;
* restores/deletes the `active` pointer after saving, so the new revision is
  **not activated** (`profiles.activated = false`);
* runs `tools/calib/export_profile.py --result … --out …`, records the exact
  command, return code, stderr and SHA-256, patches the exported image
  dimensions from the session, and appends provenance. The exporter defaults
  the colour status to `unknown` even when the fit included the colour camera:
  the colour record is only marked `ok` after an independent colour check.

Nothing is written to the scanner. A device write remains a separate,
explicit, audited step (`calib/profiles.py` `export_plan`).

The nominal 28.0 mm pitch is kept by default. `--anchor-baseline` optionally
rescales to the factory baseline; the default reports the raw baseline and the
factory comparison (`baseline_delta_pct`, `equivalent_pitch_mm`) so the known
~1 % scale difference stays visible.

**Colour:** a stale or missing `TexCCF` never blocks geometry. The colour
intrinsics fall back to the left IR model, the state is reported as
`present | missing | unreadable | stale` (a factory left–tex baseline <20 mm or
>400 mm is treated as stale), and the fitted-vs-factory colour pose is recorded
but not used to gate geometry. Colour frames are retained regardless.

---

## 9. One capture session is not metrological accuracy

This recorder is scoped to **exactly one** guided session on the original
plate. Reasons:

* A calibration is only self-consistent within the capture set that produced
  it. Mixing sessions silently mixes different rig states, lighting, plate
  handling and detector conditions; this unit's factory colour record is
  already an example of a stale/mixed-generation record. The vendor procedure
  also uses one 25-pose session. One session keeps the train/held-out split
  meaningful and the provenance auditable.
* Repeating the *capture* is cheap; repeating the *decision* after adding data
  from another session is not statistically clean (the held-out split leaks).
  For a second session, run this tool again into a new directory and compare
  `content_digest`, coverage and held-out RMS; do not concatenate sessions.
* One session establishes **repeatability and internal consistency**, not
  absolute accuracy. Proven here: correct stream/orientation handling, complete
  capture, deterministic decisions, independent held-out reprojection, and a
  host-side profile comparable with the factory CCF. **Not** proven: absolute
  metric accuracy in millimetres, ISO/VDI conformity, or that 28.0 mm is the
  plate's true pitch. The pitch is a scale gauge that no reprojection residual
  can validate. A calibrated sphere bar (VDI/VDE 2634-2) or flatness artefact
  (2634-3) is required before any millimetre-accuracy claim.
  `stopping.json`/`coverage.json` are completeness/consistency evidence, not
  accuracy certificates.

---

## 10. Determinism

Given the same input files, mapping, mode and rules, two runs produce the same
decisions, accepted/held-out ids, labels/reasons and file SHA-256s.
`manifest.content_digest` fingerprints that decision content; floats are
rounded to 1e-2 because the LM may take one extra iteration (`nit` 9 vs 10) and
change poses at the 1e-6 level. The digest excludes wall-clock fields, profile
paths and detection-cache provenance. Full numeric values remain in the
manifest and `result.json`; use those, not the digest, for numerical
comparisons.

---

## 11. Tests

```bash
HOME=/tmp OPENBLAS_NUM_THREADS=1 .venv/bin/python -m pytest -q \
  tests/test_calibsession.py
```

Covers probe mapping/orientation (decoded arrays equal the
calibration-orientation originals, raw bytes/hashes preserved, PnP layout),
missing-rotation detection, vendor grouping, manifest round-trip and corruption
detection, completeness failures (missing colour/right, wrong size, too few
held-out), a synthetic declared-detection session, a full 25-set vendor session
(≥15 accepted, ≥5 held-out, complete, deterministic re-run), stale colour
profile handling, sequential dwell (first frame after a move rejected, repeat
accepted once per pose), and a bounded `calib.simloop` closed loop. Vendor
tests skip cleanly if the private corpus is absent.

No test opens a scanner, writes to a device, or modifies a private image.

---

## 12. Safety and limits

* Read-only with respect to the scanner: no firmware, flash, USB or persistent
  calibration writes. Only host files are read/written.
* A new profile is a **revision** and is not activated; the factory snapshot is
  immutable.
* Input images are never modified; every retained file is byte-identical to its
  source and SHA-256-verified.
* One unit/language variant only. No absolute accuracy claim; the 28.0 mm pitch
  scale ambiguity is reported, not hidden. Unobservable from residuals: a
  global left/right stream swap or a common 180° rotation (fixed by the
  hardware mapping), and the plate pitch (fixed only by a length artefact).
