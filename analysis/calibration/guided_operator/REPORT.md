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

## Root cause in the selector

The family rank in `coverage._families` is **additive and type-based**, not
deficit-based:

| unlock | weight |
|---|---:|
| distance band | 1 |
| tilt axis/quadrant | 2 |
| roll sector | 1 |
| image corner | 3 |
| image region | 2 |
| colour participation (D6) | **not represented at all** |

A family that fills a completely empty distance band (weight 1) therefore ranks
below any tilt nudge (weight 2), even when several distance bands have zero
views; and no family is generated specifically to recover colour participation.
This is consistent with the observed end state (D1/D4/D6 still missing while the
tilt cluster keeps receiving views), and with the guidance repeatedly choosing a
small signed-tilt change.

A proposed fix (next change, not shipped): rank families by **normalized
deficit** over the checks they unlock — `sum((need - have) / need)` — with the
objective's priority order as the tie-break, and generate an explicit colour
family when D6 is short.  That removes the starvation without lowering any
threshold, and can be re-validated with this same operator.

## What this means

* The offline **curated** rehearsals remain valid (37 real-image views, 8/8,
  validated candidate).
* Acceptance criteria 9 (“selection prioritizes actual missing evidence”) and
  10 (“a realistic from-scratch session reaches READY with a small number of
  informative views”) are **not fully demonstrated for a guidance-following
  operator**. The product still needs a guidance-selection fix, not another
  hardware session.
* The next fix should be explicit: maintain a **fixed absolute tilt
  reference**, count the four absolute quadrants from accepted poses, and make
  the guidance request the least-covered quadrant until each has the required
  evidence — do not let a moving relative centre or a latch declare it done.
  This needs a focused design + replay, not a threshold tweak.

## Evidence

`run_latched.json` and `run_stable_reference.json` contain the full per-target
log (pose, instruction, acceptance) and the final measurement/validation
summary. Both are synthetic; no private recording, serial or factory content.
