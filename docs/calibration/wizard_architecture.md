# Calibration wizard — architecture and interface contract

Status: 2026-11-11.  This document is the contract between the parallel
workstreams.  Implement to these signatures; do not change them without telling
the integrator.

## 0. Goal and non-goals

A single, trustworthy Linux application that records **one** comprehensive
calibration dataset with the original 41-marker plate, guides the operator
toward genuinely informative views, validates a candidate profile, and never
touches the factory calibration.

Non-goals this round: a second calibration session, multi-exposure HDR,
absolute-scale certification, any persistent write to the scanner.

## 1. Evidence that shapes the design

Measured on the live unit (`docs/reconstruction/live_bringup_2026-11-11.md`):

* the one-axis sweep is 1-D: translation singular values `[958.8, 43.2, 0.4]`,
  scaled condition number `1.06e6`, **0 constrained / 4 weak / 22 unobservable**
  parameters, `corr(fx, depth) = 0.9991`;
* the vendor 25-view corpus improves conditioning 8.5x (5–9x on `fx`/`tz`) but
  **does not** constrain distortion at all;
* three profiles with indistinguishable reprojection residual move the
  reconstructed plate by **+5.3 mm**, so residual and plane RMS cannot select a
  calibration;
* the factory stereo extrinsic is offset by ~0.239° / 1.3 mm, the factory
  colour extrinsic by ~18 mm (≈49 px);
* the plate pitch is a pure **gauge**: a global board rescale leaves the cost
  identical.

Therefore: accept/reject and "done" decisions must be made on **coverage +
marginal parameter uncertainty + held-out cross-camera prediction**, never on
training RMS.

## 2. Component map and file ownership

```
tools/wizard/
  types.py            FrameGroup, CameraSet, SourceInfo            [integrator]
  sources.py          LiveSource, ReplayDirSource, RecordingSource [agent C]
  controller.py       WizardController (wraps calib.session)       [agent C]
  capture.py          LiveCaptureProcess (control-file protocol)   [agent C]
  coverage.py         CoverageSpec, measure(), guidance targets    [agent B]
  observability.py    marginal sigmas, rank, correlations, gate    [agent B]
  lifecycle.py        factory/candidate/validated, gates, rollback [agent B]
  session_io.py       session dir, atomic manifest, resume         [agent B]
  observability_cli.py / rehearsal.py  headless drivers            [agent B/C]
  gui/app.py, gui/main_window.py, gui/tiles.py, gui/panels.py      [integrator]
  tests/              unit + end-to-end                            [all, own files]
driver/tools/capture_frames.cpp   --control, --ring, signals       [agent A]
```

Nothing in `calib/`, `recon/` or `driver/include` is rewritten.  `calib/` bug
fixes are allowed only where listed in §8 and must be reported.

## 3. Frame data model (identical for live, replay and synthetic)

```python
# tools/wizard/types.py
CAMERA_KEYS = ("left", "right", "tex")     # calib.calibrate.CAMERAS order
FILE_CAM = {"left": 1, "right": 0, "tex": 2}   # stream/file index per camera

@dataclass(frozen=True)
class FrameGroup:
    index: int                  # dense, 0-based, assigned by the source
    device_group: int | None    # u64 trigger-group counter (live); None otherwise
    timestamp_s: float          # host time of the group
    images: dict[str, np.ndarray]   # keys CAMERA_KEYS, oriented uint8 (1024,1280)
    slot_ids: dict[str, int]        # 8-bit exposure slot id per camera
    complete: bool              # all three cameras present
    source: str                 # "live" | "replay" | "synthetic"
    label: str = ""
```

**Orientation is applied exactly once, in the source**, so the controller and the
calibration engine always see calibration-oriented images:

* `left`  = `frame_%06d_cam1.pgm` as-is;
* `right` = `frame_%06d_cam0.pgm` rotated 180°;
* `tex`   = `frame_%06d_cam2.pgm` debayered **BayerRG → BGR**, then rotated
  180°, converted to grey for detection (the colour image is kept alongside for
  preview/validation).

A source must be deterministic: replaying the same recording twice yields
bit-identical `FrameGroup.images`.

## 4. Controller contract

```python
class WizardController:
    def __init__(self, *, factory_profile: Path, session_dir: Path,
                 mode: str = "high", coverage: CoverageSpec | None = None,
                 solve_fn=None, pitch_mm: float = 28.0) -> None: ...
    def begin(self) -> dict: ...            # creates session dir, writes manifest
    def ingest(self, group: FrameGroup) -> Decision: ...   # the only decision path
    def snapshot(self) -> dict: ...         # everything the GUI renders
    def pause(self) -> None: ...            # stops source acquisition
    def resume(self) -> None: ...
    def finish(self) -> dict: ...           # final solve + candidate profile
    def cancel(self) -> dict: ...           # keeps data, no profile
```

* `ingest` wraps `calib.session.CalibrationSession.ingest` and adds the
  coverage/observability bookkeeping; it must return the *same* `Decision` for
  identical input regardless of source.  A determinism test asserts this.
* `snapshot()` is cheap (< 5 ms) and is what the GUI polls at 10 Hz.  Expensive
  work (solve, observability) happens on the controller's own schedule.
* `finish()` writes a **candidate** profile with `validation: pending` and the
  full validation report; it never activates it.

## 5. Live capture process protocol (agent A + C)

The GUI never speaks USB directly.  It spawns exactly one
`build/driver/einstar-capture-frames` and talks to it through a **control file**
and the existing status file.

Capture tool additions (agent A):

```
--control PATH      poll a command file every loop iteration (default off)
                    commands (last line wins, atomically written):
                      start | pause | resume | stop
--ring N            keep only the newest N complete groups on disk
                    (0 = keep everything, default); `frames_manifest.json`
                    always records every group, so nothing is lost
--max-seconds S     hard safety limit, then a graceful stop
--session-dir DIR   write raw.e1r + frames/ + status.json + manifest.json
                    in the standard layout (equivalent to the three flags)
```

Signals: `SIGINT`/`SIGTERM` must perform the same graceful stop as the `stop`
command (stop trigger, illuminator to 0, finalise manifest + recording, exit 0
with a clear "stopped by signal" line).  A `pause` stops triggering and turns
the illuminator off but keeps the device open and the recording open; `resume`
re-enables.  Each transition is written to the status JSON
(`state: running|paused|stopping|stopped`, `stop_reason`).

## 6. Coverage specification (agent B)

```python
@dataclass
class CoverageSpec:
    # each dimension: requirement + measured value + satisfied flag
    distance_bands_mm: tuple[tuple[float,float], ...]  # (300,380),(380,460),(460,560)
    min_views_per_distance_band: int = 3
    tilt_axes: str = "two"          # positive+negative out-of-plane on both axes
    min_tilt_deg: float = 15.0
    min_views_per_tilt_quadrant: int = 2
    roll_bins: int = 4              # in-plane orientation sectors
    min_views_per_roll_bin: int = 1
    image_regions: int = 9          # 3x3; plate centre must visit >= 6, corners >= 1 each
    min_shared_markers: int = 12    # both IR cameras, unambiguous assignment
    min_colour_markers: int = 12    # for colour participation
    require_colour: bool = True
    max_saturation_frac: float = 0.05
    min_focus: float = 150.0
```

`measure(accepted_views) -> dict` returns per-dimension `{required, measured,
satisfied, deficit, hint}`; `readiness(...)` returns a boolean plus the list of
unsatisfied dimensions.  **A 1-D sweep must fail this gate** and say why.

Partial visibility is allowed: a view is acceptable with ≥ `min_shared_markers`
shared between the IR pair and an unambiguous orientation witness, even if the
plate is cut off at an edge — but the witness check must pass (large-marker
area signature + off-lattice chiral marker), never a bare count.

## 7. Observability and lifecycle (agent B)

```python
def marginal_sigmas(result, *, free_mask=None) -> dict[str, float]
def identifiability(result) -> dict   # condition number, rank, eigen tail,
                                      # top correlations, weakest direction
def observability_gate(result, coverage: dict, *, target="standard") -> dict
    # per-parameter verdict constrained|weak|unobservable, plus gates:
    #   stereo_rotation, baseline, colour (if enabled), model_sensitivity
```

```python
# lifecycle.py
STATES = ("factory", "candidate", "validated")
def save_candidate(session_dir, profile, report) -> Path      # validation: pending
def validate(candidate_path, *, gates) -> dict                # pass/fail + reasons
def activate(candidate_path, store_root, serial) -> dict      # only after pass
def rollback(store_root, serial, revision=None) -> dict       # restores previous
def active_profile(store_root, serial) -> dict                # factory if none
```

Hard rules: the factory file is opened read-only and its SHA-256 is verified
before and after any operation; activation writes a **new** revision through
`calib.profiles.ProfileStore`; rollback is one call; a failed validation leaves
every existing profile byte-identical (assert it).

## 8. Known defects to fix (report first, then fix with a test)

1. `calib/detect.py::assign_grid` returns an empty `big_markers` array although
   it uses the large markers as anchors — the chirality/orientation witness is
   therefore re-derived by every caller.  Return the four anchor labels.
2. `calib/guidance.py::focus_metric` returns raw Laplacian variance, which is
   exposure dependent (measured 8 % drift on the sweep).  Add a normalised
   variant and use it in the wizard's quality gate.
3. Native solver tangential Jacobian: verify against finite differences; fix if
   wrong.  `calib/solvers.py` is the place; do not change the Ceres path.
4. `calib/calibrate.py` cannot express per-parameter free sets (the study
   re-implemented it).  Add an optional `free_mask` **without changing default
   behaviour**, so the wizard can do staged release.

## 9. Session directory layout

```
SESSION/
  session.json              # atomic: state, config, hashes, counters
  manifest.json             # accepted views: index, device_group, pose, quality
  decisions.jsonl           # one line per ingest (accepted or rejected + reasons)
  groups/000123/cam0.pgm    # materialised accepted triplets (copied from the ring)
  groups/000123/cam1.pgm
  groups/000123/cam2.pgm
  raw.e1r                   # lossless USB recording (live) or source path (replay)
  status.json               # live capture status (live only)
  candidate/profile.json    # validation: pending
  candidate/validation.json # gates, held-out numbers, observability
  report.md                 # human-readable session report
```

Every JSON write is atomic (`.tmp` + `rename`).  `resume` re-reads
`session.json` + `manifest.json`, re-checks file hashes, and continues.

## 10. Acceptance gates for the wizard itself

1. Offline rehearsal over the vendor corpus, both sweeps, synthetic truth and a
   crafted failure set (missing camera, ambiguous assignment, blurred, clipped,
   unsynchronised) — all decisions deterministic and explained.
2. The 1-D sweeps must be classified **not ready**, with the specific missing
   dimensions named.
3. A synthetic multi-view scene with known truth must reach **ready**, produce a
   candidate whose held-out cross-camera error beats the factory profile, and
   whose parameter changes stay inside the injected perturbation.
4. A deliberately bad candidate (e.g. wrong roll diversity) must be rejected and
   leave every profile file byte-identical.
5. GUI: launches on this host, renders all three tiles with overlays, shows
   guidance, accepts a view, rejects a duplicate, pauses/resumes, and shuts down
   leaving the device idle.
