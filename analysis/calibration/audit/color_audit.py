#!/usr/bin/env python3
"""Offline audit of the colour ("Tex") CCF record vs the IR pair and our joint fit.

Read-only w.r.t. vendor artefacts.  Writes only ``color_audit.json`` next to it.

Everything here is derived from:
  * the three shipped CCF records (sources/calibration/res_Einscan-E10_200x150),
  * the 6568 B device dump at <device-dump.bin>,
  * the second CCF set shipped with EXStar (sources/calibration/ccf_200x150),
  * our own joint fit (analysis/calibration/engine/end_to_end.json).

Run:  python3 analysis/calibration/audit/color_audit.py
"""
from __future__ import annotations

import json
import pathlib
import struct
import sys

import numpy as np

ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from calib import ccf as CCF          # noqa: E402  (existing core code, read-only)
from calib import plate as PLATE      # noqa: E402

BIN = pathlib.Path("<device-dump.bin>")
SET_A = ROOT / "sources/calibration/res_Einscan-E10_200x150"
SET_B = ROOT / "sources/calibration/ccf_200x150"
FIT = ROOT / "analysis/calibration/engine/end_to_end.json"
OUT = pathlib.Path(__file__).with_suffix(".json")

B_CCF = 0x39B          # CCF block inside the device dump
TS_OFF = 0xA14         # "yyyy-MM-dd hh:mm" stamp written by FUN_180003940
FQFQ_OFF = 0xF1C


def rx(a):
    c, s = np.cos(a), np.sin(a)
    return np.array([[1, 0, 0], [0, c, -s], [0, s, c]])


def ry(a):
    c, s = np.cos(a), np.sin(a)
    return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]])


def rz(a):
    c, s = np.cos(a), np.sin(a)
    return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]])


def rot_angle(R):
    return float(np.degrees(np.arccos(np.clip((np.trace(R) - 1.0) / 2.0, -1.0, 1.0))))


def load_set(d):
    out = {}
    for n in ("Left", "Right", "Tex"):
        out[n] = CCF.decode_record((d / f"{n}CCF.txt").read_bytes(), n, ir=(n != "Tex"))
    return out


def ccf_pair(recs, src, dst, hyp="H1"):
    """Return (R_rel, t_rel, C_src, C_dst) for the given storage hypothesis.

    H1  X_cam = R X_w + T,  R row-major as stored,  C = -R^T T   (documented)
    H2  X_w   = R X_cam + T (R stored = camera->world),  C = T
    H3  X_cam = R^T X_w + T (stored block is R^T),      C = -R T
    H4  X_w   = R^T X_cam + T,                          C = T
    """
    Rs, Ts = recs[src].R(), recs[src].T()
    Rd, Td = recs[dst].R(), recs[dst].T()
    if hyp == "H1":
        Rrel = Rd @ Rs.T
        trel = Td - Rrel @ Ts
        return Rrel, trel, -Rs.T @ Ts, -Rd.T @ Td
    if hyp == "H2":
        Rrel = Rd @ Rs.T
        trel = Td - Rrel @ Ts
        return Rrel, trel, Ts.copy(), Td.copy()
    if hyp == "H3":
        Rrel = Rd.T @ Rs
        trel = Td - Rrel @ Ts
        return Rrel, trel, -Rs @ Ts, -Rd @ Td
    if hyp == "H4":
        Rrel = Rd.T @ Rs
        trel = Td - Rrel @ Ts
        return Rrel, trel, Ts.copy(), Td.copy()
    raise ValueError(hyp)


def factory_records():
    b = BIN.read_bytes()
    out = []
    for off in (0x00C, 0x104, 0x1FC):
        d = np.array(struct.unpack_from("<22d", b, off))
        out.append(dict(offset=off, K=d[0:4].copy(), dist=d[4:10].copy(),
                        R=d[10:19].reshape(3, 3).copy(), T=d[19:22].copy()))
    return out


def main():
    res = {}

    # ---------------------------------------------------------------- files
    files = {}
    for n in ("Left", "Right", "Tex"):
        p = SET_A / f"{n}CCF.txt"
        st = p.stat()
        files[n] = dict(path=str(p.relative_to(ROOT)), size=st.st_size,
                        mtime=st.st_mtime_ns, sha256=CCF.sha256(p.read_bytes())
                        if hasattr(CCF, "sha256") else None)
    res["files"] = files

    # ------------------------------------------------------- device block
    b = BIN.read_bytes()
    blk = b[B_CCF:B_CCF + 0xF21]
    res["device_block"] = dict(
        offset=B_CCF, size=len(blk), type=struct.unpack_from("<I", blk, 0)[0],
        timestamp=blk[TS_OFF:TS_OFF + 16].split(b"\0")[0].decode("ascii", "replace"),
        fqfq=blk[FQFQ_OFF:FQFQ_OFF + 4].decode("ascii", "replace"),
        slot_len=[struct.unpack_from("<I", blk, o)[0] for o in (0x108, 0x610, 0xB18)],
        bytes_match_txt={
            "Left": blk[0x10C:0x10C + 520] == (SET_A / "LeftCCF.txt").read_bytes(),
            "Right": blk[0x614:0x614 + 520] == (SET_A / "RightCCF.txt").read_bytes(),
            "Tex": blk[0xB1C:0xB1C + 264] == (SET_A / "TexCCF.txt").read_bytes(),
        },
    )

    # ------------------------------------------------------------- records
    A = load_set(SET_A)
    B = load_set(SET_B)
    res["records_A"] = {n: dict(K=A[n].K().tolist(), dist=A[n].dist().tolist(),
                                R=A[n].R().tolist(), T=A[n].T().tolist(),
                                error=float(A[n].values[32])) for n in A}
    res["records_B"] = {n: dict(K=B[n].K().tolist(), dist=B[n].dist().tolist(),
                                R=B[n].R().tolist(), T=B[n].T().tolist(),
                                error=float(B[n].values[32])) for n in B}

    # orthonormality / det
    res["orthonormality"] = {
        name: {n: dict(det=float(np.linalg.det(S[n].R())),
                       orth=float(np.abs(S[n].R() @ S[n].R().T - np.eye(3)).max()))
               for n in S}
        for name, S in (("A", A), ("B", B))
    }

    # ------------------------------------------ convention hypotheses (IR control)
    fa = factory_records()
    hyp = {}
    for h in ("H1", "H2", "H3", "H4"):
        tri = {}
        for s in ("A", "B"):
            S = A if s == "A" else B
            cent = {n: (ccf_pair(S, "Left", n, h)[3] if n != "Left" else
                        (ccf_pair(S, "Left", "Tex", h)[2])) for n in ("Left", "Right", "Tex")}
            d = lambda x, y: float(np.linalg.norm(cent[x] - cent[y]))
            tri[s] = dict(LR=d("Left", "Right"), LT=d("Left", "Tex"), RT=d("Right", "Tex"))
        fcent = [-f["R"].T @ f["T"] for f in fa]
        d = lambda x, y: float(np.linalg.norm(fcent[x] - fcent[y]))
        tri["factory"] = dict(LR=d(0, 1), LT=d(0, 2), RT=d(1, 2))
        hyp[h] = tri
    res["convention_hypotheses"] = hyp

    # ------------------------------------------------------------- our fit
    fit = json.loads(FIT.read_text())["result"]
    ex = fit["extrinsics"]
    Rf = {n: np.array(ex[n]["R"]) for n in ex}
    Tf = {n: np.array(ex[n]["t"]) for n in ex}
    Cf = {n: -Rf[n].T @ Tf[n] for n in ex}
    d = lambda x, y: float(np.linalg.norm(Cf[x] - Cf[y]))
    res["fit_triangle"] = dict(LR=d("left", "right"), LT=d("left", "tex"), RT=d("right", "tex"))
    res["fit_relative"] = {}
    for src, dst in (("left", "right"), ("left", "tex"), ("right", "tex")):
        Rrel = Rf[dst] @ Rf[src].T
        trel = Tf[dst] - Rrel @ Tf[src]
        res["fit_relative"][f"{src}->{dst}"] = dict(R=Rrel.tolist(), t=trel.tolist(),
                                                    angle=rot_angle(Rrel), norm=float(np.linalg.norm(trel)))

    # tex in the left-camera frame, three independent sources
    def tex_in_left(Rl, Tl, Rt, Tt):
        R = Rt @ Rl.T
        t = Tt - R @ Tl
        return R, t, -R.T @ t      # R/t of the left->tex transform, tex centre in left frame

    res["tex_in_left_frame"] = {}
    for tag, (Rl, Tl, Rt, Tt) in {
        "ccf_A": (A["Left"].R(), A["Left"].T(), A["Tex"].R(), A["Tex"].T()),
        "ccf_B": (B["Left"].R(), B["Left"].T(), B["Tex"].R(), B["Tex"].T()),
        "factory": (fa[0]["R"], fa[0]["T"], fa[2]["R"], fa[2]["T"]),
        "our_fit": (Rf["left"], Tf["left"], Rf["tex"], Tf["tex"]),
    }.items():
        R, t, C = tex_in_left(Rl, Tl, Rt, Tt)
        res["tex_in_left_frame"][tag] = dict(R=R.tolist(), t=t.tolist(),
                                             angle=rot_angle(R), centre=C.tolist(),
                                             dist=float(np.linalg.norm(C)))
    base = res["tex_in_left_frame"]["our_fit"]
    for tag in ("ccf_A", "ccf_B", "factory"):
        v = res["tex_in_left_frame"][tag]
        res["tex_in_left_frame"][tag]["delta_vs_fit_t"] = (np.array(v["t"]) - base["t"]).tolist()
        res["tex_in_left_frame"][tag]["delta_vs_fit_centre"] = (
            np.array(v["centre"]) - base["centre"]).tolist()
        res["tex_in_left_frame"][tag]["delta_angle_vs_fit"] = rot_angle(
            np.array(v["R"]) @ np.array(base["R"]).T)

    # ------------------------------------- reference-view sensitivity (code model)
    # What would a *different* reference view cost?  For every pair of views in our
    # fit, a per-camera reference-frame mismatch adds exactly (R_j R_i^T, t_j - R t_i)
    # between the two records -> quantify the smallest such effect.
    views = fit["views"]
    Rv = [np.array(v["R"]) for v in views]
    tv = [np.array(v["t"]) for v in views]
    pair = []
    for i in range(len(views)):
        for j in range(i + 1, len(views)):
            dR = rot_angle(Rv[j] @ Rv[i].T)
            dt = float(np.linalg.norm(tv[j] - tv[i]))
            pair.append((dR, dt, i, j))
    pair.sort(key=lambda x: x[0])
    res["view_pair_sensitivity"] = dict(
        n_views=len(views),
        min_angle_deg=pair[0][0], min_angle_pair=[pair[0][2], pair[0][3]],
        min_angle_dt=pair[0][1],
        median_angle_deg=float(np.median([p[0] for p in pair])),
        min_dt=float(min(p[1] for p in pair)),
        median_dt=float(np.median([p[1] for p in pair])),
        view_distance_range=[float(min(np.linalg.norm(t) for t in tv)),
                             float(max(np.linalg.norm(t) for t in tv))],
    )

    # ------------------------------------------------------ RGB impact (px / mm)
    pts = PLATE.board_points_mm()          # 40 holes, z = 0, board-centred
    # reference pose: the view whose board distance is closest to the median
    dists = np.array([np.linalg.norm(t) for t in tv])
    k = int(np.argsort(np.abs(dists - np.median(dists)))[0])
    Rb, tb = Rv[k], tv[k]
    Xl = (Rb @ pts.T).T + tb               # board points in the left camera

    Kt = A["Tex"].K()
    def project(R_tl, t_tl, X):
        Xc = (R_tl @ X.T).T + t_tl
        uvw = (Kt @ Xc.T).T
        return uvw[:, :2] / uvw[:, 2:3], Xc

    R_ccf = np.array(res["tex_in_left_frame"]["ccf_A"]["R"])
    t_ccf = np.array(res["tex_in_left_frame"]["ccf_A"]["t"])
    uv_ccf, Xc_ccf = project(R_ccf, t_ccf, Xl)
    uv_fit, Xc_fit = project(Rf["tex"], Tf["tex"], Xl)
    dpx = np.linalg.norm(uv_ccf - uv_fit, axis=1)
    z = Xc_fit[:, 2]
    dmm = dpx * z / Kt[0, 0]
    res["rgb_impact"] = dict(
        view_used=k, view_distance_mm=float(np.linalg.norm(tv[k])),
        board_point_distance_mm=[float(z.min()), float(z.max())],
        px_mean=float(dpx.mean()), px_median=float(np.median(dpx)),
        px_max=float(dpx.max()), px_min=float(dpx.min()),
        mm_mean=float(dmm.mean()), mm_max=float(dmm.max()),
        px_span=[float(uv_ccf[:, 0].min()), float(uv_ccf[:, 0].max())],
        image_wh=[1280, 1024],
        f_px=float(Kt[0, 0]),
    )
    # per-view spread
    per_view = []
    for v in range(len(views)):
        X = (Rv[v] @ pts.T).T + tv[v]
        a = project(R_ccf, t_ccf, X)[0]
        c = project(Rf["tex"], Tf["tex"], X)[0]
        dc = np.linalg.norm(a - c, axis=1)
        per_view.append(dict(view=v, px_mean=float(dc.mean()), px_max=float(dc.max()),
                             z_mm=float(np.linalg.norm(tv[v]))))
    res["rgb_impact_per_view"] = per_view

    OUT.write_text(json.dumps(res, indent=1))
    print(f"wrote {OUT}")
    for k_, v in res["device_block"].items():
        print("device_block", k_, v)
    print("tex_in_left_frame:")
    for k_, v in res["tex_in_left_frame"].items():
        print("  %-8s t=%s |t|=%.3f angle=%.3f  d_vs_fit=%s" % (
            k_, np.round(v["t"], 3), np.linalg.norm(v["t"]), v["angle"],
            np.round(v.get("delta_vs_fit_t", [0, 0, 0]), 3)))
    print("rgb_impact:", {k_: (round(v, 3) if isinstance(v, float) else v)
                          for k_, v in res["rgb_impact"].items()})
    print("view_pair_sensitivity:", res["view_pair_sensitivity"])


if __name__ == "__main__":
    main()
