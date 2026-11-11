"""Plate-trajectory measurement, sweep-leg segmentation and held-out splits.

The live sweep is a *1-D* motion: the operator slides the plate along one axis
while the tilt drifts.  A random train/test split would therefore leak (adjacent
frames differ by <1 mm), so every held-out set here is a contiguous *leg* of the
trajectory or the whole other session.
"""

from __future__ import annotations

from dataclasses import dataclass
import numpy as np

from calib import plate
from calib.calibrate import CameraIntrinsics
from calib.geometry import solve_pnp

from . import frames as F


@dataclass
class Trajectory:
    view: np.ndarray          # group index
    t: np.ndarray             # (N,3) board origin in the left camera (mm)
    tilt_deg: np.ndarray      # angle between the board normal and the optical axis
    roll_deg: np.ndarray      # in-plane rotation about the optical axis
    ok: np.ndarray            # bool, PnP succeeded

    def summary(self) -> dict:
        m = self.ok
        out = {"n": int(len(self.view)), "n_ok": int(m.sum())}
        for i, k in enumerate(("x", "y", "z")):
            c = self.t[m, i]
            out[k] = {"min": float(c.min()), "max": float(c.max()),
                      "range": float(c.max() - c.min()), "std": float(c.std()),
                      "median": float(np.median(c))}
        out["tilt_deg"] = {"min": float(self.tilt_deg[m].min()),
                           "max": float(self.tilt_deg[m].max()),
                           "range": float(self.tilt_deg[m].max() - self.tilt_deg[m].min()),
                           "std": float(self.tilt_deg[m].std())}
        out["roll_deg"] = {"min": float(self.roll_deg[m].min()),
                           "max": float(self.roll_deg[m].max()),
                           "range": float(self.roll_deg[m].max() - self.roll_deg[m].min()),
                           "std": float(self.roll_deg[m].std())}
        # effective dimensionality: singular values of the centred translation set
        X = self.t[m] - self.t[m].mean(0)
        sv = np.linalg.svd(X, compute_uv=False)
        out["translation_singular_values"] = [float(x) for x in sv]
        out["translation_sv_ratio_2_1"] = float(sv[1] / sv[0]) if sv[0] else float("nan")
        out["translation_sv_ratio_3_1"] = float(sv[2] / sv[0]) if sv[0] else float("nan")
        return out

    def legs(self, smooth: int = 9, min_len: int = 8) -> list[tuple[int, int]]:
        """Contiguous groups between direction reversals of the dominant axis."""
        m = self.ok
        v = self.view[m]
        x = self.t[m, 0]
        if len(x) < 2 * smooth:
            return [(int(v[0]), int(v[-1]))]
        ker = np.ones(smooth) / smooth
        xs = np.convolve(x, ker, mode="valid")
        vs = v[smooth // 2: smooth // 2 + len(xs)]
        d = np.diff(xs)
        sgn = np.sign(d)
        sgn[sgn == 0] = 1
        cut = [int(vs[0])]
        for i in range(1, len(sgn)):
            if sgn[i] != sgn[i - 1]:
                cut.append(int(vs[i]))
        cut.append(int(v[-1]) + 1)
        legs = []
        for a, b in zip(cut[:-1], cut[1:]):
            if b - a >= min_len:
                legs.append((a, b - 1))
        return legs


def trajectory(det: dict, camera: str, cam: CameraIntrinsics,
               pitch_mm: float = plate.DEFAULT_PITCH_MM) -> Trajectory:
    obj = plate.board_points_mm(pitch_mm)
    uv_index = {(float(u), float(v)): i for i, (u, v) in enumerate(plate.lattice_uv())}
    n = len(det[camera])
    view = np.arange(n)
    t = np.full((n, 3), np.nan)
    tilt = np.full(n, np.nan)
    roll = np.full(n, np.nan)
    ok = np.zeros(n, bool)
    for v in range(n):
        d = det[camera][v]
        if d is None or len(d.points) < 20:
            continue
        keys = [k for k in d.points if (float(k[0]), float(k[1])) in uv_index]
        if len(keys) < 20:
            continue
        idx = [uv_index[(float(k[0]), float(k[1]))] for k in keys]
        img = np.array([d.points[k] for k in keys], float)
        p = solve_pnp(obj[idx], img, cam.K, np.asarray(cam.kc, float))
        if p is None:
            continue
        t[v] = p.t
        nz = p.R[:, 2]
        tilt[v] = np.degrees(np.arccos(min(1.0, abs(float(nz[2])))))
        roll[v] = np.degrees(np.arctan2(p.R[1, 0], p.R[0, 0]))
        ok[v] = True
    return Trajectory(view, t, tilt, roll, ok)


def image_coverage(det: dict, camera: str, view_ids) -> dict:
    """Where in the image the markers actually are (principal-point leverage)."""
    P = []
    for v in view_ids:
        d = det[camera][int(v)]
        if d is None:
            continue
        P.extend([p for p in d.points.values()])
    if not P:
        return {}
    P = np.array(P, float)
    h, w = 1024, 1280
    return {
        "n_points": int(len(P)),
        "u_min": float(P[:, 0].min()), "u_max": float(P[:, 0].max()),
        "v_min": float(P[:, 1].min()), "v_max": float(P[:, 1].max()),
        "u_span_frac": float((P[:, 0].max() - P[:, 0].min()) / w),
        "v_span_frac": float((P[:, 1].max() - P[:, 1].min()) / h),
        "centroid_u": float(P[:, 0].mean()), "centroid_v": float(P[:, 1].mean()),
        "centroid_off_centre_px": float(np.hypot(P[:, 0].mean() - (w - 1) / 2,
                                                 P[:, 1].mean() - (h - 1) / 2)),
        "quadrant_occupancy": _quadrants(P, w, h),
    }


def _quadrants(P: np.ndarray, w: int, h: int) -> dict:
    q = np.zeros(4, int)
    for x, y in P:
        q[(1 if x >= w / 2 else 0) + (2 if y >= h / 2 else 0)] += 1
    tot = int(q.sum())
    return {f"q{i}": int(q[i]) for i in range(4)} | {
        "q_frac": [float(x / tot) for x in q] if tot else [0.0] * 4}


def even_views(ids: list[int], n: int) -> list[int]:
    """Evenly spaced subsample of a sorted id list."""
    if n >= len(ids):
        return list(ids)
    idx = np.linspace(0, len(ids) - 1, n).round().astype(int)
    return sorted({int(ids[i]) for i in idx})


def build_splits(traj_c: Trajectory, traj_b: Trajectory,
                 n_train: int = 72, n_val_leg: int = 36,
                 n_val_session: int = 36) -> dict:
    """Geometric splits: alternating legs for training, the rest held out.

    Legs are the contiguous monotone segments of the 1-D sweep.  Alternating
    legs (0,2,4 -> train; 1,3,5 -> validate) keep the *same* spatial range in
    both sets while removing the temporal near-duplicate leakage.  The second
    session is a third, independent held-out set.
    """
    legs = traj_c.legs()
    train_pool = []
    hold_pool = []
    for i, (a, b) in enumerate(legs):
        (train_pool if i % 2 == 0 else hold_pool).extend(range(a, b + 1))
    if not hold_pool:
        hold_pool = list(train_pool[::3])
    train = even_views(sorted(train_pool), n_train)
    hold = even_views(sorted(hold_pool), n_val_leg)
    if traj_b.ok.any():
        b_ids = [int(v) for v in traj_b.view[traj_b.ok]]
        session = even_views(b_ids, n_val_session)
    else:
        session = []
    return {"legs": [[int(a), int(b)] for a, b in legs],
            "train": train, "heldout_legs": hold, "heldout_session": session,
            "train_pool_legs": [i for i in range(len(legs)) if i % 2 == 0],
            "holdout_pool_legs": [i for i in range(len(legs)) if i % 2 == 1],
            "n_views": {"train": len(train), "heldout_legs": len(hold),
                        "heldout_session": len(session)}}


def make_obs(det: dict, view_ids, cameras=("left", "right", "tex")):
    from calib.calibrate import Observation
    out = []
    for v in view_ids:
        for c in cameras:
            d = det[c][int(v)]
            if d is None or len(d.points) < 8:
                continue
            out.append(Observation(c, int(v), dict(d.points)))
    return out


def detection_stats(det: dict, cameras=("left", "right", "tex")) -> dict:
    out = {}
    for c in cameras:
        n = len(det[c])
        ok = [d for d in det[c] if d is not None]
        rms = np.array([d.rms for d in ok], float)
        npc = np.array([d.n for d in ok], int)
        out[c] = {"n_images": int(n), "n_detected": int(len(ok)),
                  "detect_frac": float(len(ok) / n) if n else 0.0,
                  "n_points_median": float(np.median(npc)) if len(npc) else 0.0,
                  "n_points_min": int(npc.min()) if len(npc) else 0,
                  "rms_median": float(np.median(rms)) if len(rms) else float("nan"),
                  "rms_p95": float(np.percentile(rms, 95)) if len(rms) else float("nan"),
                  "rms_max": float(rms.max()) if len(rms) else float("nan")}
    return out


def assignment_stats(det: dict, cameras=("left", "right", "tex")) -> dict:
    out = {}
    for c in cameras:
        audits = [F.audit_assignment(d) for d in det[c] if d is not None]
        verdicts: dict[str, int] = {}
        for a in audits:
            verdicts[a["verdict"]] = verdicts.get(a["verdict"], 0) + 1
        # how many views would a *wrong* hypothesis have passed?
        hyp = {}
        for h in F.DIHEDRAL:
            hyp[h] = {"n_area_signature_ok": 0}
        # the residual is invariant, so report the spread of the homography RMS
        rms = np.array([a["hypotheses"]["id"]["homography_rms"] for a in audits], float)
        spread = np.array([max(a["hypotheses"][h]["homography_rms"]
                               for h in F.DIHEDRAL) - rms[i]
                           for i, a in enumerate(audits)], float)
        ratios = np.array([a.get("area_ratio") or np.nan for a in audits], float)
        out[c] = {
            "n_views": int(len(audits)),
            "verdicts": verdicts,
            "unambiguous_frac": float(verdicts.get("unambiguous", 0) / len(audits))
                                if audits else 0.0,
            "hypothesis_rms_spread_px": {"median": float(np.nanmedian(spread)) if len(spread) else None,
                                         "max": float(np.nanmax(spread)) if len(spread) else None},
            "homography_rms_median": float(np.median(rms)) if len(rms) else None,
            "big_marker_area_ratio_median": float(np.nanmedian(ratios)) if len(ratios) else None,
            "off_lattice_dist_px_median": float(np.nanmedian(
                [a.get("off_lattice_dist_px", np.nan) for a in audits])) if audits else None,
        }
    return out
