# Partial-plate recognition

Status: 2026-11-11.  This note documents how the wizard decides that a plate
label assignment is trustworthy when part of the calibration plate is outside
the camera image, and what was measured to justify each rule.

## 1. The problem

The 40-point calibration lattice is symmetric.  The Klein four-group

```
identity   (u, v) -> (u, v)
rot180     (u, v) -> (7-u, 4-v)
mirror_u   (u, v) -> (7-u, v)
mirror_v   (u, v) -> (u, 4-v)
```

maps the lattice onto itself, so **all four label assignments reproduce the
same 40 image points with identical residuals**.  Reprojection error therefore
cannot detect a flipped plate.  Only the large-marker pattern can:

* three on-lattice large markers at `(2,1)`, `(4,1)`, `(5,1)`, area ≈ 4× a small
  marker;
* one **off-lattice** chiral marker at `(3.5, 3)`, not part of the lattice.

`mirror_u` maps the chiral marker to itself and swaps `(2,1) <-> (5,1)`, which
is why it can reach 3 of 4 witnesses on a real plate.

## 2. The rule

`tools/sweep/orient.py::check_orientation` scores all four hypotheses and
decides as follows when the caller supplies `image_shape`:

1. **Visibility.** A witness whose predicted pixel lies outside
   `[margin, w-1-margin] × [margin, h-1-margin]` cannot be observed and is not
   counted against the frame.  Visibility is judged once, from the identity
   positions, because the four hypotheses label the *same physical markers*.
2. **Strict on what is visible.** Identity must hit *every* predicted-visible
   witness (so 4/4 when all four are inside) and must strictly beat every
   alternative hypothesis on that same subset.
3. **Chirality.** `det(H) > 0` is required in all cases.  Measured on a
   mirrored plate: the score table is *identical* to the correct plate
   (`identity 4`, `mirror_u 3`) and only `det(H) = −6508` rejects it, so the
   determinant is not redundant with the area scores.
4. **Floor.** With fewer than three predicted-visible witnesses the frame is
   rejected as `insufficient_witness`.  At two witnesses identity and
   `mirror_u` tie exactly (measured `{identity 2, mirror_u 2}`), so two is
   never enough.
5. **Missing but inside is not excused.** A witness predicted *inside* the
   image that is absent, small or displaced fails the frame
   (`partial_witness` / `ambiguous`).  Only genuine out-of-frame loss is
   tolerated — occlusion by an object cannot be distinguished from an
   ambiguity, so it is refused.

Without `image_shape` the historical strict 4/4 rule is unchanged, so the sweep
analysis and every existing caller behave exactly as before.

## 3. Measured behaviour

Harness: factory E10 intrinsics, synthetic plate rendered at 400 mm
(`family_pose(400, 0, 0, 0)`), detected with the real `detect_grid_auto`
(40/40 markers, RMS 0.697 px, `det(H)=+6463.1`, pitch 79.9 px, small-marker
area 98 px).  Witness pixels: `(2,1)=(503.1,454.2)`, `(4,1)=(664.0,454.1)`,
`(5,1)=(744.5,454.1)`, chiral `(3.5,3)=(623.8,615.3)`.

| case | verdict | scores id/rot180/mirror_u/mirror_v | visible |
|---|---|---|---|
| full image | `verified`, ok | 4/0/3/0 | 4 |
| usable area 600 rows (chiral outside) | `partial_verified`, ok | 3/0/2/0 | 3 |
| chiral masked, still inside the frame | `partial_witness`, rejected | 3/0/2/0 | 4 |
| usable area 640 × 1024 (2 witnesses) | `insufficient_witness`, rejected | 2/0/2/0 | 2 |
| usable area 640 × 600 (1 witness) | `insufficient_witness`, rejected | 1/0/1/0 | 1 |
| mirrored plate layout | `mirrored_geometry` (det −6508) | 4/0/3/0 | 4 |
| rot180-permuted labels | rejected (`partial_witness`) | 0/4/0/3 | 4 |
| determinism | identical dict, byte-identical re-render | — | — |

Mutation check: monkeypatching `check_orientation` to ignore `image_shape`
(the pre-upgrade behaviour) fails exactly the three visibility-dependent tests
and passes the other seven, so the new rules are genuinely exercised.

## 4. What is still limited — the detector

The witness can only judge an observation that exists, and the strict detector
requires **all four** large markers as anchors.  Two separate failure modes
were measured when the chiral marker is hidden:

* `detect_grid_auto` returns `None`; or
* worse, it returns a self-consistent **shifted sub-lattice** — measured
  `u ∈ 1..5, v ∈ 1..4` instead of the canonical `0..7 × 0..4` — which belongs to
  a smaller plate and is silently wrong.

`calib/detect_partial.py` closes this.  It finds the three on-lattice large
markers by area, identifies the collinear triple with pitch-normalised gaps
2:1 (the asymmetric `(2,1) → (4,1) → (5,1)` signature), derives the square
lattice basis from the row and tries both perpendicular signs, then fits a
homography to every matched blob and **rejects** anything that does not reach
the canonical origin.

Measured on a plate at 400 mm with the chiral marker masked:

| path | result |
|---|---|
| strict detector | returns a shifted sub-lattice (`u` min > 0) or `None` |
| `detect_grid_partial` | **39 / 40 points**, span `u 0..7`, `v 0..4`, RMS < 1.5 px |
| witness, marker inside the image | `partial_witness`, **rejected** (correct: occlusion is not provable) |
| witness, marker outside the usable area | `partial_verified`, **accepted**, identity 3/3 beats `mirror_u` 2/3 |

The controller prefers the partial labelling whenever the strict one is
missing or does not reach the canonical span, and only when it explains at
least as many markers.  Seven regression tests cover the full plate, the
masked plate, both witness outcomes, random blobs, a coarse lattice without the
2:1 signature, determinism and the controller preference rule.

## 5. Temporal and stereo evidence

Deliberately *not* used to override the witness.  A temporally smooth pose
sequence and a consistent stereo pair can both be produced by a globally wrong
label assignment (a plate turned over between frames, or a rig whose two
cameras agree on the wrong labels), so neither may outvote the marker pattern.
They remain useful as *additional* rejection criteria (a jump inconsistent with
the previous pose still fails the view) and as a tie-breaker only when the
witness is already unambiguous.
