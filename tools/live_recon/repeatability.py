"""Pose-normalised repeatability of the reconstructed plate surface.

Method
------
Each group gets a plate pose ``X_cam = R X_board + T`` from the marker PnP.  Its
reconstructed plate points are expressed in that group's **board frame**, so the
scanner-to-plate motion is removed and two groups observed from different
positions become directly comparable (the plate is the common physical object).

For a pair of groups ``(i, j)`` the symmetric nearest-neighbour distance between
the two plate-frame clouds is a point-to-point repeatability measure (mm), and
the difference of their plate-frame plane offsets/normals is a depth-bias
measure.  Two pair families are reported:

``similar``
    nearest neighbours in pose space, preferring pairs from **different sweep
    legs** (the operator swept the plate out and back, so opposite legs revisit
    almost the same pose).  These isolate reconstruction noise from motion.
``different_angle``
    pairs with a large normal-angle difference (>= 8 deg).  On this sweep the
    plate rotation is strongly correlated with its translation (the sweep is an
    arc), so angle difference and position difference are confounded; the
    measured (dR, dT) of every pair is reported so the confound is visible.

Nothing here is a physical-accuracy claim: the plate surface is the reference,
and both clouds come from the same calibration.  It measures repeatability and
pose-dependent bias, not absolute correctness.
"""

from __future__ import annotations

import numpy as np

from . import common

try:  # scipy is available in the project venv; keep a numpy fallback anyway
    from scipy.spatial import cKDTree as _KDTree
except Exception:  # pragma: no cover
    _KDTree = None


# ---------------------------------------------------------------------------
# pose helpers
# ---------------------------------------------------------------------------

def pose_arrays(records) -> tuple:
    idx, R, T = [], [], []
    for k, r in enumerate(records):
        p = r.get("pose")
        if not p:
            continue
        idx.append(k)
        R.append(np.asarray(p["R"], dtype=np.float64).reshape(3, 3))
        T.append(np.asarray(p["T"], dtype=np.float64).reshape(3))
    if not idx:
        return np.array([], dtype=int), np.zeros((0, 3, 3)), np.zeros((0, 3))
    return np.asarray(idx), np.stack(R), np.stack(T)


def assign_legs(records, smooth: int = 7) -> list:
    """Segment the sweep into legs from the sign of the lateral velocity.

    The live sweep is an out-and-back motion; a leg boundary is where the
    smoothed plate translation reverses direction for at least a few groups.
    """
    idx, R, T = pose_arrays(records)
    legs = [0] * len(records)
    if len(T) < 3 * smooth:
        return legs
    v = np.diff(T, axis=0)
    k = np.ones(smooth) / smooth
    vs = np.stack([np.convolve(v[:, c], k, mode="same") for c in range(3)], axis=1)
    axis = vs[np.argmax(np.linalg.norm(vs, axis=1))]
    axis = axis / max(np.linalg.norm(axis), 1e-9)
    proj = vs @ axis
    sign = np.sign(proj)
    # hysteresis: flip only after `smooth` consecutive opposite samples
    leg = 0
    flips = 0
    cur = sign[0] if sign[0] != 0 else 1
    for i in range(len(sign)):
        if sign[i] != 0 and sign[i] == -cur:
            flips += 1
            if flips >= smooth:
                leg += 1
                cur = sign[i]
                flips = 0
        else:
            flips = 0
        legs[idx[i]] = leg
    legs[idx[-1]] = leg
    return legs


# ---------------------------------------------------------------------------
# pair selection
# ---------------------------------------------------------------------------

def _pair_matrices(idx, R, T):
    n = len(idx)
    M = R.reshape(n, 9)
    tr = M @ M.T                       # trace(R_i^T R_j) = <R_i, R_j>
    cosr = np.clip((tr - 1.0) / 2.0, -1.0, 1.0)
    dR = np.degrees(np.arccos(cosr))
    dT = np.linalg.norm(T[:, None, :] - T[None, :, :], axis=2)
    return dR, dT


def select_pairs(records, legs, min_gap: int = 12,
                 similar_rot_deg: float = 2.0, similar_trans_mm: float = 8.0,
                 max_per_anchor: int = 3,
                 diff_angle_min_deg: float = 8.0,
                 max_diff_pairs: int = 120,
                 max_similar_pairs: int = 160) -> dict:
    idx, R, T = pose_arrays(records)
    out = {"similar": [], "different_angle": [], "n_pose_records": int(len(idx))}
    if len(idx) < 4:
        return out
    dR, dT = _pair_matrices(idx, R, T)
    groups = [records[k]["group"] for k in idx]
    n = len(idx)
    # --- similar pose -----------------------------------------------------
    score = dR / 2.0 + dT / 20.0
    cand = []
    for a in range(n):
        order = np.argsort(score[a])
        taken = 0
        for b in order:
            if b <= a:
                continue
            if abs(groups[b] - groups[a]) < min_gap:
                continue
            if dR[a, b] > similar_rot_deg or dT[a, b] > similar_trans_mm:
                continue
            different_leg = legs[idx[a]] != legs[idx[b]]
            cand.append((float(score[a, b]), a, b, different_leg))
            taken += 1
            if taken >= max_per_anchor:
                break
    cand.sort(key=lambda z: (not z[3], z[0]))     # prefer different legs
    seen = set()
    for s, a, b, dl in cand:
        key = (min(a, b), max(a, b))
        if key in seen:
            continue
        seen.add(key)
        out["similar"].append({"a": int(groups[a]), "b": int(groups[b]), "score": s,
                               "different_leg": bool(dl)})
        if len(out["similar"]) >= max_similar_pairs:
            break
    # --- different angle --------------------------------------------------
    pairs = []
    for a in range(n):
        for b in range(a + 1, n):
            if abs(groups[b] - groups[a]) < min_gap:
                continue
            if dR[a, b] >= diff_angle_min_deg:
                pairs.append((float(dT[a, b]), a, b))
    pairs.sort(key=lambda z: z[0])      # smallest translation difference first
    used = set()
    for _, a, b in pairs:
        if a in used or b in used:
            continue
        used.add(a)
        used.add(b)
        out["different_angle"].append({"a": int(groups[a]), "b": int(groups[b]),
                                       "score": float(dR[a, b])})
        if len(out["different_angle"]) >= max_diff_pairs:
            break
    out["n_similar"] = len(out["similar"])
    out["n_different_angle"] = len(out["different_angle"])
    return out


# ---------------------------------------------------------------------------
# pairwise comparison
# ---------------------------------------------------------------------------

def _nn_distances(ref: np.ndarray, query: np.ndarray) -> np.ndarray:
    q = np.asarray(query, dtype=np.float64)
    if len(q) == 0 or len(ref) == 0:
        return np.zeros(0)
    if _KDTree is not None:
        tree = _KDTree(np.asarray(ref, dtype=np.float64))
        d, _ = tree.query(q, k=1)
        return d
    # fallback: chunked brute force
    out = np.empty(len(q))
    step = 512
    r = np.asarray(ref, dtype=np.float64)
    for i in range(0, len(q), step):
        blk = q[i:i + step]
        d = np.linalg.norm(blk[:, None, :] - r[None, :, :], axis=2)
        out[i:i + step] = d.min(axis=1)
    return out


def _plane_in_plate_frame(points: np.ndarray):
    fit = common.fit_plane(points)
    return fit


def compare_pair(pa: np.ndarray, pb: np.ndarray) -> dict:
    """Symmetric NN + plate-frame plane statistics for one pair of clouds."""
    a = np.asarray(pa, dtype=np.float64)
    b = np.asarray(pb, dtype=np.float64)
    if len(a) < 100 or len(b) < 100:
        return {"ok": False}
    d_ba = _nn_distances(a, b)
    d_ab = _nn_distances(b, a)
    fa = _plane_in_plate_frame(a)
    fb = _plane_in_plate_frame(b)
    out = {"ok": True,
           "n_a": int(len(a)), "n_b": int(len(b))}
    p = common.pct(d_ba, (50, 95))
    out["nn_b_to_a_p50_mm"] = float(p[0])
    out["nn_b_to_a_p95_mm"] = float(p[1])
    p = common.pct(d_ab, (50, 95))
    out["nn_a_to_b_p50_mm"] = float(p[0])
    out["nn_a_to_b_p95_mm"] = float(p[1])
    out["nn_p50_mm"] = float(0.5 * (out["nn_b_to_a_p50_mm"] + out["nn_a_to_b_p50_mm"]))
    out["nn_p95_mm"] = float(0.5 * (out["nn_b_to_a_p95_mm"] + out["nn_a_to_b_p95_mm"]))
    # coverage-robust agreement: a large NN at the *boundary* of the other
    # cloud's coverage is not a reconstruction error, so the fraction of points
    # with a counterpart within 1/2 mm is reported alongside the percentiles.
    out["frac_nn_lt_1mm"] = float(0.5 * (np.mean(d_ba < 1.0) + np.mean(d_ab < 1.0)))
    out["frac_nn_lt_2mm"] = float(0.5 * (np.mean(d_ba < 2.0) + np.mean(d_ab < 2.0)))
    # Plate-frame plane: in the plate frame the surface should be exactly z = 0.
    out["z_median_a_mm"] = float(np.median(a[:, 2]))
    out["z_median_b_mm"] = float(np.median(b[:, 2]))
    out["dz_mm"] = float(out["z_median_a_mm"] - out["z_median_b_mm"])
    if fa is not None and fb is not None:
        out["plane_offset_a_mm"] = float(fa[1] @ (fa[0] - np.zeros(3)))
        out["plane_offset_b_mm"] = float(fb[1] @ (fb[0] - np.zeros(3)))
        out["plane_normal_angle_deg"] = common.angle_between(fa[1], fb[1])
        out["plane_rms_a_mm"] = float(fa[2])
        out["plane_rms_b_mm"] = float(fb[2])
    return out


def run_repeatability(records, plate_points: dict, pairs: dict,
                      max_pairs: int = 400) -> dict:
    legs = assign_legs(records)
    idx, R, T = pose_arrays(records)
    pos = {int(records[k]["group"]): k for k in idx}
    gidx = {int(g): i for i, g in enumerate(idx)}

    def one(a_group, b_group):
        pa = plate_points.get(int(a_group))
        pb = plate_points.get(int(b_group))
        if pa is None or pb is None:
            return None
        c = compare_pair(pa, pb)
        if not c.get("ok"):
            return None
        ia, ib = gidx[int(a_group)], gidx[int(b_group)]
        dR = common.rotation_angle_deg(R[ia], R[ib])
        dT = float(np.linalg.norm(T[ia] - T[ib]))
        c.update({
            "a": int(a_group), "b": int(b_group),
            "dR_deg": float(dR), "dT_mm": dT,
            "leg_a": int(legs[idx[ia]]), "leg_b": int(legs[idx[ib]]),
            "range_a_mm": float(np.linalg.norm(T[ia])),
            "range_b_mm": float(np.linalg.norm(T[ib])),
        })
        return c

    out = {"legs": legs, "n_legs": int(max(legs) + 1) if legs else 0}
    for key in ("similar", "different_angle"):
        rows = []
        for p in pairs.get(key, [])[:max_pairs]:
            c = one(p["a"], p["b"])
            if c is not None:
                rows.append(c)
        out[key] = _summarise(rows, key)
        out[key]["pairs"] = rows
    return out


def _summarise(rows, label) -> dict:
    out = {"label": label, "n_pairs": len(rows)}
    if not rows:
        return out
    for field_name, unit in (("nn_p50_mm", "_mm"), ("nn_p95_mm", "_mm"),
                             ("frac_nn_lt_1mm", ""), ("frac_nn_lt_2mm", ""),
                             ("dz_mm", "_mm"), ("plane_normal_angle_deg", "_deg"),
                             ("dR_deg", "_deg"), ("dT_mm", "_mm")):
        vals = common.finite([r.get(field_name, np.nan) for r in rows])
        p = common.pct(vals, (5, 50, 95))
        out[f"{field_name}_p05{unit}"] = p[0]
        out[f"{field_name}_p50{unit}"] = p[1]
        out[f"{field_name}_p95{unit}"] = p[2]
    out["dz_std_mm"] = float(np.std(common.finite([r.get("dz_mm", np.nan) for r in rows])))
    out["nn_p50_mean_mm"] = float(np.mean(common.finite([r["nn_p50_mm"] for r in rows])))
    for other in ("dR_deg", "dT_mm"):
        a = np.array([r.get("dz_mm", np.nan) for r in rows], dtype=float)
        b = np.array([r.get(other, np.nan) for r in rows], dtype=float)
        m = np.isfinite(a) & np.isfinite(b)
        if m.sum() > 5 and a[m].std() > 0 and b[m].std() > 0:
            out[f"corr_dz_{other}"] = float(np.corrcoef(a[m], b[m])[0, 1])
    return out


def bias_trend(records) -> dict:
    """Per-group plate-frame bias vs pose: regression of bias on range/tilt.

    ``bias_median_mm`` is the signed distance from the PnP plane, so this is the
    same quantity the repeatability pairs measure, but across the whole sweep.
    A significant slope would mean the depth error is pose dependent.
    """
    rows = []
    for r in records:
        p = r.get("pose")
        s = r.get("plate", {}).get("surface", {})
        if not p or not s.get("ok"):
            continue
        rows.append({
            "group": int(r["group"]),
            "range_mm": float(p["range_mm"]),
            "bias_mm": float(s["bias_median_mm"]),
            "tilt_deg": float(s["plane_tilt_deg"]),
            "range_ratio": float(s["range_ratio"]),
            "rvec_deg": r.get("pose", {}).get("rvec_deg"),
            "plate_angle_deg": r.get("pose", {}).get("plate_angle_deg"),
        })
    out = {"n": len(rows)}
    if len(rows) < 8:
        out["rows"] = rows
        return out
    rng = np.array([r["range_mm"] for r in rows])
    bias = np.array([r["bias_mm"] for r in rows])
    ratio = np.array([r["range_ratio"] for r in rows])
    for name, x in (("range_mm", rng), ("tilt_deg", np.array([r["tilt_deg"] for r in rows]))):
        A = np.column_stack([np.ones(len(x)), x])
        coef = np.linalg.solve(A.T @ A + 1e-9 * np.eye(2), A.T @ bias)
        pred = A @ coef
        ss_res = float(np.sum((bias - pred) ** 2))
        ss_tot = float(np.sum((bias - bias.mean()) ** 2))
        out[f"bias_vs_{name}"] = {
            "slope_mm_per_unit": float(coef[1]),
            "intercept_mm": float(coef[0]),
            "r2": float(1.0 - ss_res / ss_tot) if ss_tot > 0 else None,
        }
    plate_ang = np.array([r.get("plate_angle_deg", np.nan) for r in rows], dtype=float)
    if np.isfinite(plate_ang).sum() > 8:
        m = np.isfinite(plate_ang) & np.isfinite(bias)
        A = np.column_stack([np.ones(m.sum()), plate_ang[m]])
        coef = np.linalg.solve(A.T @ A + 1e-9 * np.eye(2), A.T @ bias[m])
        pred = A @ coef
        ss_res = float(np.sum((bias[m] - pred) ** 2))
        ss_tot = float(np.sum((bias[m] - bias[m].mean()) ** 2))
        out["bias_vs_plate_angle_deg"] = {
            "slope_mm_per_unit": float(coef[1]),
            "intercept_mm": float(coef[0]),
            "r2": float(1.0 - ss_res / ss_tot) if ss_tot > 0 else None,
        }
    out["bias_mean_mm"] = float(bias.mean())
    out["bias_std_mm"] = float(bias.std())
    out["range_ratio_mean"] = float(ratio.mean())
    out["range_ratio_std"] = float(ratio.std())
    out["rows"] = rows
    return out
