#!/usr/bin/env python
"""Rescue vendor-corpus views whose grid assignment is missing or degenerate.

Two independent failure modes of ``calib.detect`` appear on the 2026-11-14
corpus and are *both* invisible to its own ``rms`` acceptance test:

  * **assignment failure** -- ``imageLeft19`` / ``imageLeft24``: the large-marker
    pattern bootstrap finds no candidate (markers big and bright against a
    bright plate), so no lattice is returned.
  * **degenerate homography** -- ``imageRight15``: ``assign_grid`` returns 40
    lattice "assignments" that collapse onto 8 distinct blobs (all five rows of
    a column land on the same pixel), with a *smaller* rms (0.26 px) than a good
    view.  The homography is rank-deficient and nothing checks it.

Both are repaired the same way, without touching the detector: the same board
pose is seen by the other cameras of that view, whose detections are healthy;
a PnP pose from the best healthy camera, mapped through the *factory* rig,
predicts the failing camera's pixel for every lattice point to a few pixels, and
the prediction is matched to the nearest detected blob.  Only blob centroids
measured in the failing image are written as observations; the rig is used for
correspondence only.

Usage::

    .venv/bin/python analysis/exstar_reference/rescue_views.py
"""
from __future__ import annotations

import json
import os
import sys

import cv2
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

from calib import detect, plate  # noqa: E402
import vendor_lib as V  # noqa: E402

DET = os.path.join(HERE, "vendor_20261114_detections.json")
OUT = os.path.join(HERE, "vendor_20261114_detections_rescued.json")


def tophat(g: np.ndarray, k: int = 25) -> np.ndarray:
    se = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
    return cv2.morphologyEx(g, cv2.MORPH_TOPHAT, se)


def blobs_of(gray: np.ndarray) -> list:
    """All plausible marker blobs: plain p99 and top-hat p99 (union)."""
    out = []
    for img in (gray, tophat(gray)):
        out.extend(detect.detect_blobs(img, invert=False, percentile=99.0))
    out.sort(key=lambda b: -b.area)
    kept = []
    for b in out:
        if all((b.x - q.x) ** 2 + (b.y - q.y) ** 2 > 25.0 for q in kept):
            kept.append(b)
    return kept


def is_degenerate(rec: dict) -> bool:
    pts = rec.get("points") or {}
    if not pts:
        return True
    uniq = {tuple(np.round(v, 3)) for v in pts.values()}
    return len(uniq) < len(pts)


def main() -> int:
    det = json.load(open(DET))
    ccf = V.decode_ccf_set(V.FACTORY_REPO, "best")
    obj = plate.board_points_mm(V.PITCH)
    uv_index = {(float(u), float(v)): i
                for i, (u, v) in enumerate(plate.lattice_uv())}
    by = {(r["camera"], r["index"]): r for r in det["images"]}
    root = os.path.expanduser("~/einstar-private/exstar-20261114")

    failures = [r for r in det["images"]
                if r.get("n_lattice", 0) < 35 or is_degenerate(r)]
    print(f"{len(failures)} view/camera failures: "
          f"{[(r['camera'], r['index'], r.get('n_lattice'), is_degenerate(r)) for r in failures]}")

    rescued = []
    for rec in failures:
        cam, idx = rec["camera"], rec["index"]
        healthy = [by[(c, idx)] for c in ("left", "right", "tex")
                   if c != cam and by[(c, idx)].get("n_lattice", 0) >= 35
                   and not is_degenerate(by[(c, idx)])]
        if not healthy:
            rescued.append({"camera": cam, "index": idx, "ok": False,
                            "reason": "no healthy camera in this view"})
            continue
        src = healthy[0]
        # board pose in the source camera
        keys = sorted(src["points"])
        X = np.array([obj[uv_index[(float(k.split(",")[0]), float(k.split(",")[1]))]]
                      for k in keys], np.float64)
        uv = np.array([src["points"][k] for k in keys], np.float64)
        m_src = ccf[src["camera"]]
        Ks = np.array([[m_src["fx"], 0, m_src["cx"]],
                       [0, m_src["fy"], m_src["cy"]], [0, 0, 1]], float)
        ok, rvec, tvec = cv2.solvePnP(X, uv, Ks, np.asarray(m_src["kc"], float),
                                      flags=cv2.SOLVEPNP_ITERATIVE)
        if not ok:
            rescued.append({"camera": cam, "index": idx, "ok": False,
                            "reason": "source PnP failed"})
            continue
        Rb_s = cv2.Rodrigues(rvec)[0]
        tb_s = tvec.ravel()
        # board -> vendor world -> target camera
        R_bw = m_src["R"].T @ Rb_s
        t_bw = m_src["R"].T @ (tb_s - m_src["T"])
        m_dst = ccf[cam]
        Rb_d = m_dst["R"] @ R_bw
        tb_d = m_dst["R"] @ t_bw + m_dst["T"]
        Kd = np.array([[m_dst["fx"], 0, m_dst["cx"]],
                       [0, m_dst["fy"], m_dst["cy"]], [0, 0, 1]], float)
        proj, _ = cv2.projectPoints(obj, cv2.Rodrigues(Rb_d)[0], tb_d, Kd,
                                    np.asarray(m_dst["kc"], float))
        proj = proj.reshape(-1, 2)
        gray = cv2.imread(os.path.join(root, rec["file"]), cv2.IMREAD_GRAYSCALE)
        blobs = blobs_of(gray)
        P = np.array([b.xy for b in blobs], float)
        pts, d1s, used = {}, [], set()
        for i, (u, v) in enumerate(plate.lattice_uv()):
            d = np.linalg.norm(P - proj[i], axis=1)
            j = int(np.argmin(d))
            if j in used or d[j] > 25.0:
                continue
            used.add(j)
            pts[f"{int(u)},{int(v)}"] = [float(P[j, 0]), float(P[j, 1])]
            d1s.append(float(d[j]))
        if len(pts) >= 6:
            kk = list(pts)
            uvs = np.array([[float(t) for t in k.split(",")] for k in kk], float)
            pix = np.array([pts[k] for k in kk], float)
            H, _ = cv2.findHomography(uvs.astype(np.float64),
                                      pix.astype(np.float64), cv2.RANSAC, 3.0)
            pred = cv2.perspectiveTransform(uvs.reshape(-1, 1, 2), H).reshape(-1, 2)
            hom_rms = float(np.sqrt(np.mean(np.sum((pred - pix) ** 2, 1))))
        else:
            hom_rms = 999.0
        good = len(pts) >= 30 and hom_rms < 1.5
        rescued.append({"camera": cam, "index": idx, "ok": good,
                        "source_camera": src["camera"],
                        "n_matched": len(pts),
                        "pred_median_px": float(np.median(d1s)) if d1s else None,
                        "pred_max_px": float(np.max(d1s)) if d1s else None,
                        "homography_rms_px": round(hom_rms, 4),
                        "points": pts if good else {}})
        print(f"{cam}{idx}: {len(pts)}/40 matched from {src['camera']}, "
              f"prediction median {np.median(d1s) if d1s else float('nan'):.2f} px, "
              f"homography rms {hom_rms:.3f} px -> {'ACCEPT' if good else 'REJECT'}")

    for rec in rescued:
        if not rec.get("ok"):
            continue
        tgt = by[(rec["camera"], rec["index"])]
        tgt["points"] = rec["points"]
        tgt["n_lattice"] = len(rec["points"])
        tgt["rescue"] = {k: v for k, v in rec.items() if k != "points"}
    det["rescue"] = rescued
    for cam in V.CAMERAS:
        ns = [r["n_lattice"] for r in det["images"] if r["camera"] == cam]
        det["cameras"][cam]["n_lattice_per_view"] = {
            str(r["index"]): r["n_lattice"] for r in det["images"]
            if r["camera"] == cam}
        det["cameras"][cam]["n_lattice_mean"] = float(np.mean(ns))
        det["cameras"][cam]["n_fully_detected_40"] = int(sum(n == 40 for n in ns))
        det["cameras"][cam]["n_failed"] = int(sum(n < 30 for n in ns))
    V.save_json(det, OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
