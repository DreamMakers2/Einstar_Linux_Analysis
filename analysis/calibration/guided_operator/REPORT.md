# Guidance-driven operator rehearsal — negative result

**Date:** 2026-11-14
**Question:** does the live guidance, followed by a cooperative operator, fill
all eight coverage dimensions in a small number of views?

**Answer: no, not yet.** On a synthetic rig with perfect visibility and a
cooperative operator that renders exactly the requested pose (plus a short
hold and a retry on rejection), the loop reaches **7/8** and stops at the
45-target budget. The four signed tilt quadrants remain incomplete.

## Setup

* Same synthetic rig for the controller factory and for every rendered view
  (`seed=7`, 1.5 px factory perturbation), so the only variable is guidance.
* The operator asks `WizardController.snapshot()["guidance"]`, renders the
  requested `distance_mm`, absolute `tilt_deg`, `roll_deg` and image region
  (lateral offset from the region), ingests the move plus three 10 Hz hold
  frames, and retries a rejected target with a small correction.
* High mode, async solver, max 45 target poses (~133 ingested groups).
* Two variants:
  * `run_latched.json` — the shipped behaviour in round 3/4 (tilt checks latch
    once satisfied);
  * `run_stable_reference.json` — an experimental variant that also gives
    guidance a fixed tilt reference from the bootstrap view and latches only the
    signed axes, not the quadrants. **This variant was not shipped.**

## Result

| | latched (shipped) | stable reference (experimental) |
|---|---:|---:|
| target poses | 45 | 45 |
| ingested groups | 133 | 133 |
| accepted views | 132 | 131 |
| fit RMS | 0.42197 px | 0.41735 px |
| D1, D3–D8 | satisfied | satisfied |
| D2 (tilt) | **quadrants missing** | **quadrants missing** |
| finish gate | `passed=true`, `would_activate=false` | same |
| wall time | ~205 s | ~205 s |

Both runs end with instructions like “Tilt 3 deg right and 3 deg down”: the
guidance keeps asking for small nudges around the current tilt cluster instead
of driving to the under-covered signed quadrant.

## Why

The coverage gate centres the tilt excursion on a statistic of the accepted
views. As the operator follows the nudges, that centre moves with the cluster;
a quadrant can be satisfied at one step and unsatisfied at the next without
any evidence being removed. Two consequences:

1. **Latching hides the gap.** Once a quadrant check has been satisfied, the
   latch keeps it satisfied for guidance, so guidance stops asking for the
   missing combination; the final validation deliberately does not latch and
   finds the quadrant missing.
2. **A stable guidance reference alone is not enough.** Passing the bootstrap
   tilt as the family-generation centre changes the absolute targets, but the
   guidance still treats the *current measurement's* quadrant state as
   satisfied, so it keeps choosing carrier/filler families.

The curated 37-view hybrid rehearsal does reach 8/8, but it was selected
against the gate; it is not evidence that the guidance-driven workflow finds
that set by itself.

## Correction: this operator is open-loop

The operator renders the **nominal** family pose and ingests it. It never sees
the preview and cannot adjust until the plate is actually in the requested image
corner.  `_image_targets` requests a corner through a lateral offset; under
tilt/roll the nominal pose does not always project the plate centre into that
corner, so the instruction repeats and the accepted set stalls.

Two selector variants were tried after the first result:

| variant | accepted | 45-target result |
|---|---:|---|
| fixed type weights (shipped) | 132 | 7/8; D2 quadrants missing; D1/D4/D6 also missing at the end |
| deficit-normalized ranking (not shipped) | **16** | 7/8; D2 quadrants **and image corners** missing |

The deficit-ranked variant did not help: corner attempts starved everything
else.  An earlier revision of this report blamed fixed type weights as the
primary cause; that is not established by this experiment.

What the runs do show:

* the nominal family → pose mapping is **not sufficient** to guarantee the
  requested image region under tilt/roll for an open-loop actuator; a human can
  close that loop from the preview, the synthetic operator cannot;
* the guidance has no machine-checkable “the plate is in the requested corner”
  confirmation, so the product relies on the operator's eyes for that part;
* the measured calibration quality is good (RMS 0.42 px), so the failure is
  **coverage progress**, not bad observations.

## What this means

* The offline **curated** rehearsals remain valid (37 real-image views, 8/8,
  validated candidate).
* Acceptance criteria 9 and 10 are **unverified**, not disproven: the only
  guidance-following operator tested is open-loop, and it fails for a reason
  (nominal pose vs projected region) that a human can correct visually.
* A valid test needs a **closed-loop operator**: render, detect the projected
  plate centre, adjust the nominal pose until the actual region/quadrant
  matches the request, then ingest; or have the controller verify the region
  from the preview before counting progress.  That is the next verification to
  build, and it does not need hardware.

## Closed-loop test: the stall is now reproducible

A closed-loop operator was built for the next step: it renders the requested
pose, detects the plate, checks that the measured plate centre is really in the
requested 3x3 image cell, adjusts the lateral offset and only then ingests.
On the 45-target run it **does** place the plate in the requested cells
(attempts `[2,0]`, `[0,2]`, `[2,2]`, `[0,0]`, `[1,0]`), but the session stalls
after **7 accepted views** (`closed_loop_run.json`).

The loop is visible in the log: guidance returns the same pose, the controller
rejects it as `too similar to an accepted view (0.5 deg, 0.1 mm)`, and the same
instruction comes back. Final coverage: D3/D4/D5/D8 true; D1, D2, D6 and D7
false; finish refuses (7 views, `right.tz`/`tex.tz` unobservable).

Two code facts explain it:

1. **No refresh after a duplicate.** Guidance is keyed on the accepted pose /
   coverage; a rejected duplicate at the unchanged pose does not force a
   recompute, so the wizard keeps repeating an instruction that has just been
   refused. A human would keep holding the same pose and see no progress.
2. **Colour has no family.** D6 (colour participation) is a pose-relevant
   dimension, but the shipped selector has no colour unlock/family. When it is
   the remaining deficit, `_families` can return no admissible next pose and
   guidance falls into a hold.

A colour-only family (varied tilt/roll) was prototyped and not shipped: without
the refresh fix the operator still stalled on the same pose. The next fix is
therefore ordered:

1. force guidance to recompute after a duplicate/rejected view (add a
   rejection/attempt counter to the guidance key);
2. generate a colour-participation family that differs from the current pose;
3. re-run this closed-loop operator and the curated replay.

This is a concrete product bug with a reproducible harness, not hardware work.

## Evidence

`run_latched.json`, `run_stable_reference.json` and `run_deficit_ranked.json`
contain the full per-target log (pose, instruction, acceptance) and the final
measurement/validation summary.  All are synthetic; no private recording,
serial or factory content.
