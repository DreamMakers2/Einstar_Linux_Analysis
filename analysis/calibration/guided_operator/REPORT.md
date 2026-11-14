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

## Partial fix: the duplicate stall is gone, D2 still oscillates

The shipped-selector fix (guidance refresh epoch + avoid the just-refused pose +
distinct distance depths + colour family, no check latch) was tested with the
same closed-loop operator:

| | before | after |
|---|---:|---:|
| accepted in 45 targets | 7 | **31** |
| accepted in 20 targets | 7 | 19 |
| D1 distance bands | missing | **satisfied** |
| D6 colour | missing | **satisfied** |
| D2 tilt | missing | still missing |
| finish | refused (7 views) | refused (D2) |

The operator no longer loops on `too similar`: it moves through distance bands
and colour, and the fit stays good (RMS 0.414 px). The remaining failure is
narrower: the relative tilt centre still oscillates between the y axis and the
four quadrants, so D2 is satisfied at some steps (20-target run: only quadrants
missing) and unsatisfied at the end (45-target run: y axis + quadrants). A check
latch can hold y but then the quadrants never fill; no latch lets y flip back.

This needs the **stable absolute tilt reference** design, not another nudge:
count the four absolute quadrants (and the two signed axes) from accepted poses
against the bootstrap reference, and make guidance request the least-covered
absolute quadrant until it has the required evidence. That is the next change;
the duplicate-stall fix is a real improvement and is shipped.

## Stable-reference experiment (negative)

The proposed next fix — pass the bootstrap tilt as the **measurement** centre as
well as the guidance reference, making D2 absolute and monotonic — was tried.
It made the workflow worse: the closed-loop operator accepted only **5 views in
45** targets, D1/D4/D6/D7 also failed, and finish refused. Absolute ±20°
targets consume the whole budget and the extreme poses lose image-region and
colour evidence.

So the relative gate plus the partial fix (31 accepted, D1/D6 filled) remains
the better state. The remaining D2 oscillation needs a **two-phase / per-check
hysteresis** policy — relative for acquisition, absolute (or latched with
quadrant-specific targets) for completion — not a global reference switch. That
is the next design; the experiment was reverted, not shipped.

## Two-phase fix: 18 views, 8/8, validated

The two-phase design works. Acquisition keeps the session-relative tilt centre;
once D1 and D3–D6 are satisfied and only D2 remains, the controller counts the
four **absolute** signed tilt quadrants from the bootstrap view and forces the
least-covered one until it has the required evidence. The synthetic operator
adds a small pose variation when a view is refused (what a human does), and the
run completes:

| | result |
|---|---:|
| target views / accepted | **20 / 20** |
| coverage | **8/8** |
| finish | `passed=true`, `would_activate=true` |
| fit RMS | **0.4204 px** |

This is the offline from-scratch proof (cooperative closed-loop operator, same
rig, real region verification). The curated 37-view real-image replay still
reaches 8/8 with the same code. The phase gate matters: applying the absolute
reference from the start accepted only 5 views; applying it only for completion
costs a handful of views after the relative phase.

## Evidence

`run_latched.json`, `run_stable_reference.json`, `run_deficit_ranked.json`,
`closed_loop_run.json` (before) and `closed_loop_after.json` (after) contain the
full per-target log (pose, instruction, acceptance) and the final
measurement/validation summary.  All are synthetic; no private recording,
serial or factory content.
