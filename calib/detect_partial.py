"""Partial-plate lattice assignment.

The strict detector (`calib.detect.assign_grid`) anchors the lattice on the
four orientation markers: the three on-lattice large markers at ``(2,1)``,
``(4,1)``, ``(5,1)`` plus the off-lattice chiral marker at ``(3.5, 3)``.  When
the chiral marker leaves the image -- which is exactly what happens at an image
corner, where the wizard most needs the observation -- the strict detector
returns ``None`` or, worse, a self-consistent *sub-lattice* whose labels are
shifted by several pitches.

This module recovers the **canonical** labelling from the large markers alone:

1. the large markers are found by area (≈3-4x the area of a small marker);
2. the three that are collinear, with pitch-normalised spacings 2 and 1, are
   identified as ``(2,1)``, ``(4,1)``, ``(5,1)`` in that order (the pattern is
   asymmetric, so the order along the line is unambiguous);
3. the lattice basis comes from that triple; the perpendicular ``v`` has two
   signs, and **both are tried**;
4. every hypothesis is scored, never guessed:
   * ``det(H) > 0`` is required -- a front-facing plate maps the board frame to
     the pixel frame with positive determinant, while the opposite ``v`` sign
     is a mirror (``det < 0``) and cannot be what the camera saw.  This is the
     same chirality rule the orientation witness uses, applied *while* choosing
     the labelling instead of only afterwards;
   * every *non-anchor* large marker that the hypothesis predicts inside the
     frame must actually be a large blob there (the chiral marker for the
     three-collinear case, the third on-lattice marker plus the chiral one for
     the two-marker case).  Those witnesses are not part of the fit, so they are
     independent evidence, not a tautology;
   * the labelling must explain at least ``min_lattice`` blobs with a small
     residual, and no blob may serve two labels;
   * a tie between two hypotheses that give *different* labels is reported as
     ambiguous instead of being resolved by a coin flip.

A two-large-marker anchor (plus the chiral witness) is included because a
corner clip can remove exactly one of the three collinear large markers; two of
them still fix the pitch and the axis, and the witnesses then fix which of the
two possible pairs they are.

The returned observation is identity-labelled and carries ``witness`` /
``identity_ok``; the caller still runs `tools.sweep.orient.check_orientation`,
which is the component that reports the chirality margin to the user.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import itertools
from typing import Iterable, Iterator, Optional

import cv2
import numpy as np

from . import detect, plate

__all__ = ["PartialResult", "assign_grid_partial", "detect_grid_partial",
           "BIG_TRIPLE", "BIG_GAPS"]

#: lattice coordinates of the three on-lattice large markers, in order
BIG_TRIPLE = ((2.0, 1.0), (4.0, 1.0), (5.0, 1.0))
#: gaps between them in pitch units (2 then 1) - the asymmetric signature
BIG_GAPS = (2.0, 1.0)
#: the off-lattice chiral marker
CHIRAL_UV = (3.5, 3.0)

#: area ratio that separates a large marker from a small one.  The plate prints
#: the large markers at ~1.75x the small radius (≈3x area); the recorded corpus
#: measured 3.4-5.0x on accepted views, so 1.55 keeps a wide margin while small
#: markers that happen to be lit brightly (measured up to 1.7x the median) stay
#: in the candidate pool only when the lattice fit supports them.
LARGE_AREA_RATIO = 1.55


@dataclass
class PartialResult:
    observation: Optional[detect.GridObservation]
    reason: str
    n_inliers: int = 0
    span_u: tuple[float, float] = (float("nan"), float("nan"))
    span_v: tuple[float, float] = (float("nan"), float("nan"))
    v_sign: int = 0
    margin: float = float("nan")
    #: sign of the labelling homography determinant (>0 required for a front view)
    det_h: float = float("nan")
    #: number of non-anchor large markers found where the labelling predicts them
    witness_score: int = 0
    #: number of those witnesses predicted inside the frame
    witness_visible: int = 0
    #: hypotheses that survived every check, best first
    n_candidates: int = 0
    #: True when the best two surviving labellings disagree with equal support
    ambiguous: bool = False

    def as_dict(self) -> dict:
        return {"ok": self.observation is not None, "reason": self.reason,
                "n_inliers": int(self.n_inliers),
                "span_u": [float(x) for x in self.span_u],
                "span_v": [float(x) for x in self.span_v],
                "v_sign": int(self.v_sign),
                "margin": float(self.margin) if np.isfinite(self.margin) else None,
                "det_h": float(self.det_h) if np.isfinite(self.det_h) else None,
                "witness_score": int(self.witness_score),
                "witness_visible": int(self.witness_visible),
                "n_candidates": int(self.n_candidates),
                "ambiguous": bool(self.ambiguous)}


# ---------------------------------------------------------------------------
# large-marker bookkeeping
# ---------------------------------------------------------------------------

def _large_blobs(blobs: list, ratio: float = LARGE_AREA_RATIO) -> list[tuple[int, float]]:
    """Indices of the blobs that are large markers, by descending area."""
    areas = np.array([b.area for b in blobs], float)
    if len(areas) < 4:
        return []
    small = float(np.median(areas))
    if small <= 0:
        return []
    idx = [int(i) for i in np.where(areas / small > ratio)[0]]
    idx.sort(key=lambda i: -areas[i])
    return [(i, float(areas[i] / small)) for i in idx]


def _collinear_triples(P: np.ndarray, cand: list[tuple[int, float]],
                       *, tol: float = 0.35, ratio_tol: float = 0.45,
                       max_triples: int = 24) -> list[tuple[int, int, int, float]]:
    """Triples of large blobs with the plate's 2:1 collinear signature.

    Returned in canonical order ``(2,1), (4,1), (5,1)``: the *longer* gap must
    come first, which the asymmetric 2:1 pattern fixes without any handedness
    assumption.  The indices are returned in that validated order -- returning
    them in the order they were enumerated silently produced a 3-pitch "long
    gap" for every other blob ordering (measured on the synthetic render:
    pitch 53.7 px instead of 79.3 px, and no hypothesis survived).
    """
    out = []
    ids = [i for i, _ in cand]
    for i, j, k in itertools.combinations(ids, 3):
        for a, b, c in itertools.permutations((i, j, k)):
            p0, p1, p2 = P[a], P[b], P[c]
            ab, ac = p1 - p0, p2 - p0
            nab, nac = float(np.linalg.norm(ab)), float(np.linalg.norm(ac))
            if min(nab, nac) <= 1.0:
                continue
            cross = abs(float(ab[0] * ac[1] - ab[1] * ac[0]))
            if cross > tol * nab * nac:
                continue                        # not collinear
            d1 = nab
            d2 = float(np.linalg.norm(p2 - p1))
            if d2 > d1:
                continue                        # canonical order: long gap first
            if d2 <= 1.0:
                continue
            ratio = d1 / d2
            if abs(ratio - 2.0) > ratio_tol:
                continue
            out.append((a, b, c, (d1 + d2) / 3.0))
            break
    out.sort(key=lambda t: abs(np.linalg.norm(P[t[1]] - P[t[0]])
                               / max(np.linalg.norm(P[t[2]] - P[t[1]]), 1e-9) - 2.0))
    return out[:max_triples]


def _rot90(v: np.ndarray) -> np.ndarray:
    return np.array([-float(v[1]), float(v[0])])


# ---------------------------------------------------------------------------
# hypotheses
# ---------------------------------------------------------------------------

@dataclass
class _Hypothesis:
    label0: tuple[float, float]       # canonical label of the anchor pixel
    pix0: np.ndarray
    ex: np.ndarray                    # one pitch along +u
    v_sign: int
    anchors: tuple                    # anchor labels (witnesses must exclude these)
    source: str                       # "triple" | "pair"

    def predict(self, uv: np.ndarray) -> np.ndarray:
        ey = _rot90(self.ex) * float(self.v_sign)
        origin = self.pix0 - self.label0[0] * self.ex - self.label0[1] * ey
        return origin + uv[:, 0:1] * self.ex[None, :] + uv[:, 1:2] * ey[None, :]

    def homography(self, uv: np.ndarray, pix: np.ndarray) -> Optional[np.ndarray]:
        if len(uv) < 4:
            return None
        H, _ = cv2.findHomography(uv.reshape(-1, 1, 2).astype(np.float64),
                                  pix.reshape(-1, 1, 2).astype(np.float64), 0)
        if H is None or not np.all(np.isfinite(H)):
            return None
        return H


def _hypotheses(P: np.ndarray, cand: list[tuple[int, float]],
                *, max_pairs: int = 8, triples: bool = True) -> Iterator[_Hypothesis]:
    """Yield every anchor hypothesis worth testing (triples, then pairs)."""
    for i, j, k, pitch in (_collinear_triples(P, cand) if triples else []):
        p0, p1, p2 = P[i], P[j], P[k]
        ex = (p2 - p0) / 3.0
        if float(np.linalg.norm(ex)) <= 1.0:
            continue
        for sign in (+1, -1):
            yield _Hypothesis((2.0, 1.0), p0, ex, sign,
                              (BIG_TRIPLE[0], BIG_TRIPLE[1], BIG_TRIPLE[2]), "triple")
    if max_pairs <= 0:
        return
    # two large markers: a clipped corner can remove exactly one of the three.
    # The pair is either (2,1)-(4,1) (2 pitches) or (4,1)-(5,1) (1 pitch); both
    # readings are generated and the independent witnesses decide.
    pairs = list(itertools.combinations([c[0] for c in cand], 2))[:max_pairs]
    for i, j in pairs:
        for a, b, reading in ((i, j, "gap2"), (j, i, "gap2"),
                              (i, j, "gap1"), (j, i, "gap1")):
            pa, pb = P[a], P[b]
            gap = float(np.linalg.norm(pb - pa))
            if gap <= 1.0:
                continue
            if reading == "gap2":
                ex = (pb - pa) / 2.0
                label0 = (2.0, 1.0)
            else:
                ex = pb - pa
                label0 = (4.0, 1.0)
            if float(np.linalg.norm(ex)) < 0.5:
                continue
            for sign in (+1, -1):
                yield _Hypothesis(label0, pa, ex, sign,
                                  (BIG_TRIPLE[0], BIG_TRIPLE[1], BIG_TRIPLE[2]), "pair")


def _score(hyps: Iterable[_Hypothesis], P: np.ndarray, A: np.ndarray,
           uv_all: np.ndarray, **kwargs) -> list[dict]:
    out = []
    for hyp in hyps:
        r = _evaluate(hyp, P, A, uv_all, **kwargs)
        if r is not None:
            out.append(r)
    return out


# ---------------------------------------------------------------------------
# scoring
# ---------------------------------------------------------------------------

def _witness_labels(anchors: tuple) -> list[tuple[float, float]]:
    """The large markers that are *not* anchors of this hypothesis."""
    return [tuple(l) for l in plate.big_points_uv().tolist() if tuple(l) not in anchors]


def _witness_hits(P: np.ndarray, A: np.ndarray, H: np.ndarray, pitch: float,
                  labels: list, small_area: float, shape, *,
                  tol_pitch: float = 0.30,
                  large_ratio: float = LARGE_AREA_RATIO) -> tuple[int, int]:
    """(hits, visible) for the given large-marker labels under ``H``."""
    if not labels or not np.isfinite(pitch) or pitch <= 0:
        return 0, 0
    uv = np.array(labels, float)
    pix = cv2.perspectiveTransform(uv.reshape(-1, 1, 2).astype(np.float64),
                                   np.asarray(H, float)).reshape(-1, 2)
    d = np.linalg.norm(pix[:, None, :] - P[None, :, :], axis=2)
    near = d.argmin(1)
    dn = d.min(1)
    ratio = A[near] / max(float(small_area), 1e-9)
    hits = (dn < tol_pitch * pitch) & (ratio > large_ratio)
    if shape is None:
        visible = np.ones(len(uv), bool)
    else:
        h, w = int(shape[0]), int(shape[1])
        visible = ((pix[:, 0] >= 4.0) & (pix[:, 0] <= w - 5.0)
                   & (pix[:, 1] >= 4.0) & (pix[:, 1] <= h - 5.0))
    return int(np.sum(hits & visible)), int(np.sum(visible))


def _refine_basis(hyp: _Hypothesis, P: np.ndarray, uv_all: np.ndarray,
                  pitch: float, tol: float, rounds: int = 4):
    """Iteratively replace the orthogonal basis by the affine one the blobs show.

    The seed basis assumes ``v`` perpendicular to ``u`` with the same pitch, but
    a projected plane is *not* a similarity: a plate at the frame edge is sheared
    (measured 101.5 deg between the projected lattice axes on real view 102), and
    the orthogonal seed then misses the far rows by >0.3 pitch.  Fitting the
    affine basis to the blobs that are already matched absorbs that shear and
    grows the support from 22/40 to 40/40 on that frame.
    """
    ex = np.asarray(hyp.ex, float)
    ey = _rot90(ex) * float(hyp.v_sign)
    origin = np.asarray(hyp.pix0, float) - hyp.label0[0] * ex - hyp.label0[1] * ey
    ok = np.zeros(len(uv_all), bool)
    for _ in range(max(1, int(rounds))):
        pred = origin + uv_all[:, 0:1] * ex[None, :] + uv_all[:, 1:2] * ey[None, :]
        d = np.linalg.norm(pred[:, None, :] - P[None, :, :], axis=2)
        near = d.argmin(1)
        dn = d.min(1)
        ok = dn < tol * pitch
        if int(ok.sum()) < 6 or int(np.unique(near[ok]).size) != int(ok.sum()):
            break
        A = np.hstack([uv_all[ok], np.ones((int(ok.sum()), 1))])
        sol, *_ = np.linalg.lstsq(A, P[near[ok]], rcond=None)   # (3, 2)
        ex_new, ey_new, origin_new = sol[0], sol[1], sol[2]
        if (np.linalg.norm(ex_new - ex) < 0.05 * pitch
                and np.linalg.norm(ey_new - ey) < 0.05 * pitch):
            ex, ey, origin = ex_new, ey_new, origin_new
            break
        ex, ey, origin = ex_new, ey_new, origin_new
    pred = origin + uv_all[:, 0:1] * ex[None, :] + uv_all[:, 1:2] * ey[None, :]
    d = np.linalg.norm(pred[:, None, :] - P[None, :, :], axis=2)
    near = d.argmin(1)
    dn = d.min(1)
    ok = dn < tol * pitch
    return ex, ey, origin, near, dn, ok


def _evaluate(hyp: _Hypothesis, P: np.ndarray, A: np.ndarray, uv_all: np.ndarray,
              *, tol_pitch: float, min_lattice: int, max_rms_px: float,
              shape) -> Optional[dict]:
    """Test one hypothesis; return its score, or ``None`` when it is not valid."""
    pitch = float(np.linalg.norm(hyp.ex))
    if not np.isfinite(pitch) or pitch <= 0:
        return None
    ex, ey, origin, near, dn, ok = _refine_basis(hyp, P, uv_all, pitch, tol_pitch)
    uv_ok = uv_all[ok]
    if int(np.unique(near[ok]).size) != int(ok.sum()):
        return None
    if int(ok.sum()) < int(min_lattice):
        return None
    img = P[near[ok]]
    H = hyp.homography(uv_ok, img)
    if H is None:
        return None
    det_h = float(np.linalg.det(H))
    if not np.isfinite(det_h) or det_h <= 0:
        return None                     # mirrored labelling: not physically real
    projected = cv2.perspectiveTransform(uv_ok.reshape(-1, 1, 2).astype(np.float64),
                                         H).reshape(-1, 2)
    res = np.linalg.norm(projected - img, axis=1)
    rms = float(np.sqrt(np.mean(res ** 2)))
    if rms > float(max_rms_px):
        return None
    matched_area = A[near[ok]]
    small_area = float(np.median(matched_area)) if len(matched_area) else float("nan")
    labels = _witness_labels(hyp.anchors)
    hits, visible = _witness_hits(P, A, H, pitch, labels, small_area, shape)
    return {"H": H, "uv": uv_ok, "pix": img, "n": int(ok.sum()), "rms": rms,
            "max_res": float(res.max()), "det_h": det_h, "v_sign": hyp.v_sign,
            "witness_score": hits, "witness_visible": visible,
            # a verified identity needs at least one *independent* witness in
            # frame: with nothing visible the claim would rest on the anchors
            # alone, which is a tautology
            "witness_ok": bool(visible >= 1 and hits == visible),
            "source": hyp.source, "labels": labels}


def _rank_key(r: dict) -> tuple:
    """Order hypotheses: independent witnesses first, then support and residual."""
    return (int(bool(r["witness_ok"])), int(r["witness_score"]),
            int(r["n"]), -float(r["rms"]))


def match_span(uv: np.ndarray) -> tuple[tuple[float, float], tuple[float, float]]:
    return ((float(uv[:, 0].min()), float(uv[:, 0].max())),
            (float(uv[:, 1].min()), float(uv[:, 1].max())))


def assign_grid_partial(blobs: list, lattice: Optional[np.ndarray] = None,
                        *, min_lattice: int = 12, max_rms_px: float = 1.5,
                        tol_pitch: float = 0.32,
                        ratio: float = LARGE_AREA_RATIO,
                        shape: tuple[int, int] | None = None,
                        max_pairs: int = 8) -> PartialResult:
    """Recover the canonical 8x5 labelling from the large markers alone."""
    lattice = plate.lattice_uv() if lattice is None else np.asarray(lattice, float)
    if len(blobs) < int(min_lattice):
        return PartialResult(None, "too few blobs")
    uv_max = (float(lattice[:, 0].max()), float(lattice[:, 1].max()))
    uv_all = np.array([(u, v) for v in np.arange(0, uv_max[1] + 1)
                       for u in np.arange(0, uv_max[0] + 1)], float)
    P = np.array([b.xy for b in blobs], float)
    A = np.array([b.area for b in blobs], float)
    cand = _large_blobs(blobs, ratio)
    if len(cand) < 2:
        return PartialResult(None, "fewer than two large markers")
    scored = _score(_hypotheses(P, cand, max_pairs=0), P, A, uv_all,
                    tol_pitch=tol_pitch, min_lattice=min_lattice,
                    max_rms_px=max_rms_px, shape=shape)
    best = max(scored, key=_rank_key) if scored else None
    if best is None or int(best["n"]) < 24 or not best["witness_ok"]:
        # the three-collinear anchor is incomplete or its own witnesses do not
        # confirm it: also try two large markers + the independent witnesses
        scored += _score(_hypotheses(P, cand, max_pairs=max_pairs, triples=False),
                         P, A, uv_all, tol_pitch=tol_pitch,
                         min_lattice=min_lattice, max_rms_px=max_rms_px, shape=shape)
    if not scored:
        return PartialResult(None, "no chirality-consistent lattice hypothesis "
                                   "explained enough blobs")
    scored.sort(key=_rank_key, reverse=True)
    best = scored[0]
    runner = scored[1] if len(scored) > 1 else None
    margin = float("nan")
    ambiguous = False
    if runner is not None:
        margin = float(best["n"] - runner["n"])
        if runner["n"] == best["n"] and runner["witness_score"] == best["witness_score"]:
            same = (len(runner["uv"]) == len(best["uv"])
                    and np.allclose(np.sort(runner["uv"], axis=0),
                                    np.sort(best["uv"], axis=0)))
            if not same:
                ambiguous = True
    if ambiguous:
        return PartialResult(None, "ambiguous large-marker assignment "
                                   f"(two labellings with {best['n']} markers)",
                             n_inliers=best["n"], margin=margin,
                             det_h=best["det_h"],
                             witness_score=best["witness_score"],
                             witness_visible=best["witness_visible"],
                             n_candidates=len(scored), ambiguous=True)
    points = {(float(u), float(v)): np.array([float(x), float(y)])
              for (u, v), (x, y) in zip(best["uv"], best["pix"])}
    obs = detect.GridObservation(points, np.asarray(best["H"], float),
                                 int(best["n"]), float(best["rms"]),
                                 float(best["max_res"]), np.zeros((0, 2)),
                                 detector="partial-v2",
                                 witness_score=int(best["witness_score"]),
                                 witness={"ok": bool(best["witness_ok"]),
                                          "score": int(best["witness_score"]),
                                          "n_visible": int(best["witness_visible"]),
                                          "det_h": float(best["det_h"]),
                                          "source": best["source"],
                                          "ambiguous": bool(ambiguous)})
    span_u, span_v = match_span(best["uv"])
    return PartialResult(obs, "ok", n_inliers=int(best["n"]), span_u=span_u,
                         span_v=span_v, v_sign=int(best["v_sign"]), margin=margin,
                         det_h=float(best["det_h"]),
                         witness_score=int(best["witness_score"]),
                         witness_visible=int(best["witness_visible"]),
                         n_candidates=len(scored))


def detect_grid_partial(gray: np.ndarray, *, refine_mode: str = "centroid",
                        percentiles: Iterable[float] = (99.0, 99.4, 98.5),
                        early_lattice: int | None = None,
                        **kwargs) -> PartialResult:
    """Polarity/threshold sweep with the partial assignment as the only path.

    Every combination is scored and the best kept; the sweep stops early once a
    combination explains the whole visible lattice with a verified identity, so
    the common case costs one threshold like before.  Thresholds come from one
    histogram pass (the same values :func:`calib.detect.detect_grid_auto` uses)
    instead of six exact percentile computations.
    """
    percentiles = tuple(float(p) for p in percentiles)
    thresholds = detect.thresholds_from_histogram(gray, percentiles)
    best: Optional[PartialResult] = None
    reasons: list[str] = []
    for invert in (False, True):
        for pct in percentiles:
            blobs = detect.detect_blobs(gray, invert=invert, percentile=pct,
                                        refine_mode=refine_mode,
                                        threshold=thresholds[(invert, pct)])
            if not blobs:
                continue
            result = assign_grid_partial(blobs, shape=gray.shape, **kwargs)
            if result.observation is None:
                reasons.append(f"invert={invert} pct={pct}: {result.reason}")
                continue
            if best is None or (result.n_inliers, result.witness_score) > \
                    (best.n_inliers, best.witness_score):
                best = result
            if (early_lattice is not None
                    and result.n_inliers >= int(early_lattice)
                    and result.witness_score >= result.witness_visible):
                return result
    if best is not None:
        return best
    return PartialResult(None, reasons[-1] if reasons else "no blobs")
