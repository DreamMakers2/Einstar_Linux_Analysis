"""Informative-view selection and near-duplicate rejection.

The scene is static and the plate is rigid, so two frames are "the same
viewpoint" when the plate sits at the same pose in camera coordinates.  The
descriptor used for coverage is therefore

    [x, y, z]            board origin in the left IR camera, mm
    [pitch, yaw, roll]   board orientation, deg (ZYX Euler)
    [u, v]               board-origin image position, px (adds nothing geometric
                         beyond the pose for a fixed intrinsic, but keeps the
                         descriptor interpretable and catches intrinsics drift)

each dimension divided by a *physical* tolerance, so a unit distance means
"just distinguishable".  The defaults (5 mm, 0.5 deg, 25 px) are the
distinguishability scale of this rig: 5 mm of board motion at 400 mm is 14 px
of image motion and 0.7 deg of triangulated angle, i.e. far above the measured
pose noise floor (see the report).

Selection is deterministic greedy farthest-point: seed at the descriptor
farthest from the centroid, then repeatedly add the frame whose nearest
selected neighbour is farthest, stopping when that distance drops below
``tau_min``.  No RNG anywhere.

**The result is not a set of independent viewpoints.**  The sweep is a 1-D
trajectory: every selected frame lies on the same curve, so the selection
subsamples a line rather than spanning a volume.  The reported coverage is a
subsampling residual, not a diversity measure.
"""

from __future__ import annotations

from typing import Sequence

import numpy as np

DEFAULT_SCALES = {"t_mm": 5.0, "rot_deg": 0.5, "px": 25.0}
DIM_NAMES = ["x", "y", "z", "pitch", "yaw", "roll", "u", "v"]


def descriptor(rec: dict, image_point: Sequence[float] | None = None,
               scales: dict | None = None) -> np.ndarray:
    sc = dict(DEFAULT_SCALES)
    if scales:
        sc.update(scales)
    d = np.array([rec["t"][0], rec["t"][1], rec["t"][2],
                  rec["pitch_deg"], rec["yaw_deg"], rec["roll_rel_deg"],
                  image_point[0] if image_point is not None else rec.get("u_c", 0.0),
                  image_point[1] if image_point is not None else rec.get("v_c", 0.0)], float)
    d[:3] /= sc["t_mm"]
    d[3:6] /= sc["rot_deg"]
    d[6:8] /= sc["px"]
    return d


def descriptor_matrix(recs: Sequence[dict], image_points: Sequence | None = None,
                      scales: dict | None = None) -> np.ndarray:
    rows = []
    for i, r in enumerate(recs):
        rows.append(descriptor(r, None if image_points is None else image_points[i],
                               scales))
    return np.asarray(rows, float)


def _pairwise_min(desc: np.ndarray, idx: Sequence[int]) -> np.ndarray:
    """For every row: distance to the nearest row in ``idx`` (inf if empty)."""
    if len(idx) == 0:
        return np.full(len(desc), np.inf)
    S = desc[np.asarray(idx, int)]
    d = np.linalg.norm(desc[:, None, :] - S[None, :, :], axis=2)
    return d.min(1)


def greedy_farthest(desc: np.ndarray, tau_min: float = 1.0,
                    tau_dup: float | None = None,
                    start: int | None = None) -> list[int]:
    """Deterministic greedy farthest-point selection.

    ``tau_min`` is the stopping radius (in tolerance units): the loop stops when
    every remaining frame is closer than ``tau_min`` to the selected set.
    ``tau_dup``, when given, marks a candidate as a duplicate (never selected)
    once it lies closer than that to a selected frame; it only affects the
    bookkeeping, the stopping rule is ``tau_min``.
    """
    n = len(desc)
    if n == 0:
        return []
    if start is None:
        centroid = desc.mean(0)
        start = int(np.argmax(np.linalg.norm(desc - centroid, axis=1)))
    selected = [int(start)]
    dmin = np.linalg.norm(desc - desc[start], axis=1)
    dmin[start] = -np.inf
    while True:
        cand = int(np.argmax(dmin))
        if not np.isfinite(dmin[cand]) or dmin[cand] < tau_min:
            break
        selected.append(cand)
        dmin = np.minimum(dmin, np.linalg.norm(desc - desc[cand], axis=1))
        dmin[cand] = -np.inf
    return selected


def distinct_count(desc: np.ndarray, tau: float) -> int:
    """Number of distinguishable poses at separation ``tau`` (greedy cover)."""
    return len(greedy_farthest(desc, tau_min=tau))


def duplicate_fraction(desc: np.ndarray, tau: float) -> dict:
    """Fraction of frames that have another frame within ``tau``."""
    if len(desc) < 2:
        return {"tau": tau, "n_with_neighbour": 0, "fraction": 0.0}
    d = np.linalg.norm(desc[:, None, :] - desc[None, :, :], axis=2)
    np.fill_diagonal(d, np.inf)
    nn = d.min(1)
    return {"tau": float(tau),
            "n_with_neighbour": int(np.sum(nn < tau)),
            "fraction": float(np.mean(nn < tau)),
            "nn_p50": float(np.median(nn)),
            "nn_p95": float(np.percentile(nn, 95))}


def coverage_report(desc: np.ndarray, selected: Sequence[int]) -> dict:
    sel = np.asarray(sorted(selected), int)
    if len(sel) == 0:
        return {"n_selected": 0}
    S = desc[sel]
    d = np.linalg.norm(S[:, None, :] - S[None, :, :], axis=2)
    np.fill_diagonal(d, np.inf)
    nn_sel = d.min(1)
    cover = _pairwise_min(desc, sel)
    return {
        "n_selected": int(len(sel)),
        "n_frames": int(len(desc)),
        "reduction": round(1.0 - len(sel) / len(desc), 4),
        "min_pairwise": round(float(nn_sel.min()), 4),
        "mean_nn_selected": round(float(nn_sel.mean()), 4),
        "cover_radius": round(float(cover.max()), 4),
        "cover_p95": round(float(np.percentile(cover, 95)), 4),
        "cover_median": round(float(np.median(cover)), 4),
    }
