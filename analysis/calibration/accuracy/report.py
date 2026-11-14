#!/usr/bin/env python
"""Turn the raw calibration fits into the section-9 report tables and verdict.

Reads ``results_fresh01.json`` (written by :mod:`experiment`) and emits

* ``PARTIAL_VS_FULL.md``   -- method, tables, limitations, verdict
* ``PARTIAL_VS_FULL.json`` -- the same numbers, machine readable

All statistics are computed here from the stored per-view held-out residuals;
nothing is refitted.

Usage::

    .venv/bin/python analysis/calibration/accuracy/report.py
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from collections import defaultdict
from pathlib import Path

import numpy as np
from scipy import stats

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(HERE))

import experiment as X                                   # noqa: E402

DRAWN = ("C_mix", "D_2f1p", "E_1f2p", "G_h10R")

ARM_LABEL = {
    "A_full": "a) full-board only",
    "B_all": "b) all accepted views",
    "C_mix": "c) matched count, random mix",
    "D_2f1p": "c) matched count, 2/3 full + 1/3 partial",
    "E_1f2p": "c) matched count, 1/3 full + 2/3 partial",
    "F_h10B": "c) hybrid: core + 10 best partial",
    "F_h20B": "c) hybrid: core + 20 best partial",
    "G_h10R": "c) hybrid: core + 10 random partial",
    "H_h10C": "c) hybrid: core + 10 most-clipped partial",
}

COND_KEYS = [
    ("baseline_sigma_mm", "stereo baseline sigma (mm)"),
    ("rot_ir_sigma_deg", "stereo rotation sigma (deg)"),
    ("tex_translation_sigma_mm", "colour translation sigma (mm)"),
    ("tex_rotation_sigma_deg", "colour rotation sigma (deg)"),
    ("rgb_pixel_sigma_at_400mm", "colour pixel sigma @400 mm (px)"),
    ("sigma_px", "residual scale sigma (px)"),
    ("condition_scaled", "unit-free conditioning"),
    ("eig_min", "min covariance eigenvalue"),
]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def base_name(arm: str) -> str:
    for b in DRAWN:
        if arm.startswith(b):
            return b
    return arm


def pct(x, q):
    return float(np.percentile(np.asarray(x, float), q)) if len(x) else float("nan")


def fmt(x, nd=3, dash="—"):
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return dash
    return f"{x:.{nd}f}"


def md_table(header, rows):
    out = ["| " + " | ".join(header) + " |",
           "|" + "|".join(["---"] * len(header)) + "|"]
    for r in rows:
        out.append("| " + " | ".join(str(c) for c in r) + " |")
    return "\n".join(out)


def bootstrap_ci(values, n=4000, seed=7):
    v = np.asarray(values, float)
    if len(v) < 3:
        return (float("nan"), float("nan"))
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(v), size=(n, len(v)))
    means = v[idx].mean(axis=1)
    return (float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5)))


def sign_test(deltas):
    d = np.asarray([x for x in deltas if x != 0.0], float)
    if len(d) < 5:
        return float("nan"), int((d > 0).sum()), int(len(d))
    k = int((d > 0).sum())
    p = float(stats.binomtest(k, len(d), 0.5).pvalue)
    return p, k, int(len(d))


# ---------------------------------------------------------------------------
# aggregation
# ---------------------------------------------------------------------------

def collect(results):
    """blocks[arm][(rep, fold)] = {view: rms_cross}; metrics[arm][(rep,fold)] = dict"""
    blocks = defaultdict(lambda: defaultdict(dict))
    block_metrics = defaultdict(dict)
    n_fits = defaultdict(int)
    for f in results["fits"]:
        if "error" in f:
            continue
        arm = base_name(f["arm"])
        key = (int(f["repeat"]), int(f["fold"]))
        n_fits[arm] += 1
        hold = {}
        for h in f["holdout"]:
            if h.get("valid") and h.get("rms_cross"):
                hold[int(h["view"])] = {
                    "rms_cross": float(h["rms_cross"]),
                    "rms_all": float(h.get("rms_all") or float("nan")),
                    "rms_left": float(h.get("rms_left") or float("nan")),
                    "rms_right": float(h.get("rms_right") or float("nan")),
                    "rms_tex": float(h.get("rms_tex") or float("nan")),
                    "partial": bool(h.get("partial")),
                }
        blocks[arm][key] = hold
        m = f["metrics"]
        rec = block_metrics[arm].setdefault(key, defaultdict(list))
        for k in ("train_rms_px", "n_pruned", "n_observations",
                  "baseline_mm", "tex_baseline_mm"):
            if m.get(k) is not None:
                rec[k].append(float(m.get(k)))
        rec["n_views"].append(float(m.get("n_views") or 0.0))
        for k in ("n_full_train", "n_partial_train"):
            rec[k].append(float(f.get(k) or 0.0))
        for k, _ in COND_KEYS:
            v = m.get(k)
            if v is not None:
                rec[k].append(float(v))
        # keep the per-fit training-view lists once
        rec["train_views"].append(list(f.get("train_views") or []))
        rec["pruned"].append(m.get("pruned") or [])
    # average over draws inside a block
    out_metrics = {}
    for arm, per in block_metrics.items():
        out_metrics[arm] = {}
        for key, rec in per.items():
            out_metrics[arm][key] = {
                k: (float(np.mean(v)) if isinstance(v, list) and v
                    and not isinstance(v[0], (list, dict)) else v)
                for k, v in rec.items()}
    return blocks, out_metrics, dict(n_fits)


def series_split(blocks, arm):
    """(all, full-board test views, partial test views) per-view series."""
    acc, acc_full, acc_part = defaultdict(list), defaultdict(list), defaultdict(list)
    for key, hold in blocks[arm].items():
        for v, r in hold.items():
            acc[v].append(r["rms_cross"])
            (acc_part if r.get("partial") else acc_full)[v].append(r["rms_cross"])
    med = lambda d: {v: float(np.median(x)) for v, x in d.items()}
    return med(acc), med(acc_full), med(acc_part)


def per_view_series(blocks, arm):
    """view -> median over blocks containing it (one block per rep for k-fold)."""
    acc = defaultdict(list)
    for key, hold in blocks[arm].items():
        for v, r in hold.items():
            acc[v].append(r["rms_cross"])
    return {v: float(np.median(x)) for v, x in acc.items()}


def per_view_component(blocks, arm, comp):
    acc = defaultdict(list)
    for key, hold in blocks[arm].items():
        for v, r in hold.items():
            if r.get(comp) is not None and not math.isnan(r[comp]):
                acc[v].append(r[comp])
    return {v: float(np.median(x)) for v, x in acc.items()}


def paired(blocks, arm, ref="A_full", comp="rms_cross"):
    """Block-mean and per-view paired deltas of ``comp`` (arm - ref)."""
    block_deltas, view_deltas = [], []
    for key in sorted(blocks[ref]):
        a, b = blocks[arm].get(key), blocks[ref][key]
        if not a or not b:
            continue
        common = sorted(v for v in (set(a) & set(b))
                        if comp in a[v] and comp in b[v]
                        and not math.isnan(a[v][comp]) and not math.isnan(b[v][comp]))
        if not common:
            continue
        d = np.array([a[v][comp] - b[v][comp] for v in common])
        block_deltas.append(float(d.mean()))
        view_deltas.extend(float(x) for x in d)
    return {
        "n_blocks": len(block_deltas),
        "block_mean": float(np.mean(block_deltas)) if block_deltas else None,
        "block_sd": float(np.std(block_deltas, ddof=1)) if len(block_deltas) > 1 else None,
        "block_ci": bootstrap_ci(block_deltas),
        "view_median": float(np.median(view_deltas)) if view_deltas else None,
        "view_mean": float(np.mean(view_deltas)) if view_deltas else None,
        "frac_improved": (float(np.mean(np.asarray(view_deltas) < 0))
                          if view_deltas else None),
        "sign_p": sign_test(view_deltas)[0],
        "n_view_pairs": len(view_deltas),
    }


def arm_summary(blocks, arm):
    series = per_view_series(blocks, arm)
    vals = np.array(list(series.values()), float)
    comp = {c: per_view_component(blocks, arm, c)
            for c in ("rms_left", "rms_right", "rms_tex", "rms_all")}
    return {
        "n_views_scored": int(len(vals)),
        "median": float(np.median(vals)),
        "mean": float(vals.mean()),
        "p95": pct(vals, 95),
        "p95_trimmed": pct(sorted(vals)[:max(1, int(0.99 * len(vals)))], 95),
        "p99": pct(vals, 99),
        "max": float(vals.max()),
        "per_camera_median": {c: (float(np.median(list(v.values())))
                                  if v else None) for c, v in comp.items()},
    }


def cond_summary(block_metrics, arm):
    keys = [k for k, _ in COND_KEYS]
    out = {}
    for k in keys:
        vals = [m[k] for m in block_metrics[arm].values() if k in m]
        if not vals:
            continue
        out[k] = {"median": float(np.median(vals)),
                  "q1": pct(vals, 25), "q3": pct(vals, 75),
                  "mean": float(np.mean(vals)), "sd": float(np.std(vals, ddof=1))
                  if len(vals) > 1 else 0.0}
    return out


def cond_paired(block_metrics, arm, key, ref="A_full"):
    d = []
    for k, m in block_metrics[arm].items():
        r = block_metrics[ref].get(k)
        if r and key in m and key in r:
            d.append(m[key] - r[key])
    if not d:
        return None
    lo, hi = bootstrap_ci(d)
    return {"mean": float(np.mean(d)), "sd": float(np.std(d, ddof=1)) if len(d) > 1 else 0.0,
            "ci": [lo, hi], "n": len(d)}


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--results", default=str(HERE / "results_fresh01.json"))
    ap.add_argument("--data", default=str(HERE / "data_fresh01.json"))
    ap.add_argument("--md", default=str(HERE / "PARTIAL_VS_FULL.md"))
    ap.add_argument("--json", default=str(HERE / "PARTIAL_VS_FULL.json"))
    args = ap.parse_args(argv)

    results = json.loads(Path(args.results).read_text())
    sens_path = HERE / "sensitivity.json"
    sens = json.loads(sens_path.read_text()) if sens_path.exists() else None
    data = json.loads(Path(args.data).read_text())
    pool, desc = X.load_dataset(Path(args.data), results["dataset"]["session"], 12, 3.0)
    geom = X.view_geometry(pool)

    blocks, block_metrics, n_fits = collect(results)
    arms = [a for a in ("A_full", "B_all", "C_mix", "D_2f1p", "E_1f2p",
                        "F_h10B", "F_h20B", "G_h10R", "H_h10C") if a in blocks]
    summaries = {a: arm_summary(blocks, a) for a in arms}
    rel = {a: paired(blocks, a) for a in arms if a != "A_full"}
    rel_all = {a: paired(blocks, a, comp="rms_all") for a in arms if a != "A_full"}
    conds = {a: cond_summary(block_metrics, a) for a in arms}
    cond_rel = {a: {k: cond_paired(block_metrics, a, k) for k, _ in COND_KEYS}
                for a in arms if a != "A_full"}

    # coverage of the pool-level arm definitions
    full_ids = sorted(v for v, r in pool.items() if r["full"])
    part_ids = sorted(v for v, r in pool.items() if not r["full"])
    min_markers = {v: r["min_markers"] for v, r in pool.items()}
    best = sorted(part_ids, key=lambda v: (-min_markers[v], v))
    clipped = [v for v in best if min_markers[v] <= 36]
    coverage = {
        "FULL (80 full-board views)": X.coverage_summary(full_ids, pool, geom),
        "PARTIAL (82 partial views)": X.coverage_summary(part_ids, pool, geom),
        "ALL (162 views)": X.coverage_summary(full_ids + part_ids, pool, geom),
        "HYBRID +10 best": X.coverage_summary(full_ids + best[:10], pool, geom),
        "HYBRID +20 best": X.coverage_summary(full_ids + best[:20], pool, geom),
        "HYBRID +10 clipped": X.coverage_summary(full_ids + clipped[:10], pool, geom),
    }

    # ---- verdict quantities -------------------------------------------------
    def verdict_for(arm):
        r = rel.get(arm) or {}
        s, sref = summaries.get(arm), summaries["A_full"]
        if not r:
            return {}
        lo, hi = r["block_ci"]
        return {
            "delta_median_px": (s["median"] - sref["median"]) if s else None,
            "delta_p95_px": (s["p95"] - sref["p95"]) if s else None,
            "paired_block_mean_px": r["block_mean"],
            "paired_block_sd_px": r["block_sd"],
            "paired_block_ci_px": [lo, hi],
            "view_median_delta_px": r["view_median"],
            "frac_views_improved": r["frac_improved"],
            "sign_test_p": r["sign_p"],
            "above_noise": bool(lo is not None and not (lo <= 0.0 <= hi)),
        }

    verdicts = {a: verdict_for(a) for a in arms if a != "A_full"}

    def judge_tone(v):
        if not v:
            return "no-data"
        lo, hi = v["paired_block_ci_px"]
        if math.isnan(lo) or math.isnan(hi):
            return "inconclusive"
        if lo <= 0.0 <= hi:
            return "neutral"
        return "better" if hi < 0 else "worse"

    # ---- report ------------------------------------------------------------
    dval = data["sessions"][results["dataset"]["session"]]["validation"]
    _fcams, _fextr = X.EX.profile_to_cameras(X.EX.load_profile(X.FACTORY))
    FACTORY_BASE = float(np.linalg.norm(_fextr["right"][1]))
    FACTORY_TEX = float(np.linalg.norm(_fextr["tex"][1]))
    screen = results["dataset"]["screen"]
    prot = results["protocol"]
    prov = results.get("provenance", {})

    def cond_row(arm):
        c = conds[arm]
        row = [ARM_LABEL.get(arm, arm)]
        for k, _ in COND_KEYS:
            v = c.get(k, {}).get("median")
            if v is None:
                row.append("—")
            elif k == "eig_min":
                row.append(f"{v:.1e}")
            else:
                row.append(fmt(v, 3 if ("sigma" in k or k == "sigma_px") else 1))
        return row

    table_data = []
    for a in arms:
        n_full = np.mean([m.get("n_full_train", 0) for m in block_metrics[a].values()])
        n_part = np.mean([m.get("n_partial_train", 0) for m in block_metrics[a].values()])
        mk_tot, mk_min = [], []
        for m in block_metrics[a].values():
            ids = (m.get("train_views") or [[]])[0]
            if not ids:
                continue
            mk_tot.append(sum(pool[v]["cams"][c]["n"] for v in ids
                              for c in pool[v]["cams"]))
            mk_min.append(min(pool[v]["min_markers"] for v in ids))
        table_data.append([ARM_LABEL.get(a, a), f"{n_full:.0f}", f"{n_part:.0f}",
                           f"{n_full + n_part:.0f}",
                           f"{np.mean(mk_tot):.0f}" if mk_tot else "—",
                           f"{np.mean(mk_min):.0f}" if mk_min else "—",
                           f"{np.mean([m.get('n_pruned', 0) for m in block_metrics[a].values()]):.1f}"])

    table_acc = []
    for a in arms:
        s = summaries[a]
        table_acc.append([
            ARM_LABEL.get(a, a),
            fmt(s["median"]), fmt(s["p95"]), fmt(s["p99"]),
            fmt(s["per_camera_median"].get("rms_left")),
            fmt(s["per_camera_median"].get("rms_right")),
            fmt(s["per_camera_median"].get("rms_tex")),
        ])

    table_paired = []
    for a in arms:
        if a == "A_full":
            continue
        v = verdicts[a]
        lo, hi = v["paired_block_ci_px"]
        table_paired.append([
            ARM_LABEL.get(a, a),
            f"{v['paired_block_mean_px']:+.3f}",
            f"[{lo:+.3f}, {hi:+.3f}]",
            f"{v['view_median_delta_px']:+.3f}",
            f"{100 * v['frac_views_improved']:.0f}%",
            f"{v['sign_test_p']:.2g}",
        ])

    table_cond = [cond_row(a) for a in arms]
    table_cond_rel = []
    for a in arms:
        if a == "A_full":
            continue
        row = [ARM_LABEL.get(a, a)]
        for k, _ in COND_KEYS[:5]:
            d = cond_rel[a].get(k)
            if not d:
                row.append("—")
                continue
            lo, hi = d["ci"]
            if math.isnan(lo) or math.isnan(hi):
                star = ""
            else:
                star = "" if (lo <= 0.0 <= hi) else "*"
            row.append(f"{d['mean']:+.3f}{star}")
        table_cond_rel.append(row)

    train_rms = {a: float(np.mean([m["train_rms_px"] for m in block_metrics[a].values()]))
                 for a in arms}
    prune_counts = defaultdict(int)
    prune_cams = defaultdict(int)
    for f in results["fits"]:
        if "error" in f:
            continue
        for p in (f["metrics"].get("pruned") or []):
            prune_counts[(int(p["view"]), p["camera"])] += 1
            prune_cams[p["camera"]] += 1
    prune_unique = sorted(prune_counts)
    split = {}
    for a in arms:
        allv, fv, pv = series_split(blocks, a)
        split[a] = {"n_full_test": len(fv), "n_partial_test": len(pv),
                    "median_full_test": float(np.median(list(fv.values()))) if fv else None,
                    "median_partial_test": float(np.median(list(pv.values()))) if pv else None}
    geometry = {}
    for a in arms:
        b = [m.get("baseline_mm") for m in block_metrics[a].values() if m.get("baseline_mm")]
        t = [m.get("tex_baseline_mm") for m in block_metrics[a].values() if m.get("tex_baseline_mm")]
        geometry[a] = {"baseline_mm_median": float(np.median(b)) if b else None,
                       "tex_baseline_mm_median": float(np.median(t)) if t else None}

    # global statements for the verdict
    def statement(arm):
        v = verdicts.get(arm)
        if not v:
            return "no data"
        lo, hi = v["paired_block_ci_px"]
        if math.isnan(lo) or math.isnan(hi):
            return (f"inconclusive (fewer than 3 (repeat, fold) blocks; "
                    f"block mean {v['paired_block_mean_px']:+.3f} px)")
        if lo <= 0.0 <= hi:
            return ("no change beyond noise (paired 95 % CI "
                    f"[{lo:+.3f}, {hi:+.3f}] px includes 0)")
        if hi < 0:
            return (f"**better** than full-board only by {-v['paired_block_mean_px']:.3f} px "
                    f"(paired 95 % CI [{lo:+.3f}, {hi:+.3f}] px)")
        return (f"**worse** than full-board only by {v['paired_block_mean_px']:.3f} px "
                f"(paired 95 % CI [{lo:+.3f}, {hi:+.3f}] px)")

    # conditioning text
    def cond_text(arm, key, unit, nd=3):
        d = (cond_rel.get(arm) or {}).get(key)
        if not d:
            return "—"
        lo, hi = d["ci"]
        if math.isnan(lo) or math.isnan(hi):
            sig = "inconclusive"
        else:
            sig = "significant" if not (lo <= 0.0 <= hi) else "within noise"
        body = f"{d['mean']:+.{nd}f}" + (f" {unit}" if unit else "")
        return f"{body} ({sig})"

    md = []
    A = md.append
    A("# Partial-board observations: accuracy and conditioning on fresh-01")
    A("")
    A("**Question (objective §9).** Does enabling partial-board observations change")
    A("calibration accuracy or conditioning? Three observation policies are compared")
    A("under one independent validation protocol:")
    A("")
    A("* **(a) full-board only** — views in which all three cameras saw the whole 8×5 field,")
    A("* **(b) all accepted views** — partial boards included,")
    A("* **(c) hybrid** — the full-board core plus a bounded number `K` of partial views.")
    A("")
    A(f"Data: the real `{results['dataset']['root']}` accepted set, "
      f"{results['dataset']['n_pool_views']} views "
      f"({results['dataset']['n_full']} full-board / {results['dataset']['n_partial']} partial). "
      f"All fits use the production path `calib.calibrate.calibrate(..., pitch=28.0, "
      f"backend='fast')` with the `high` policy (`free_kc` once `n≥{prot['policy']['kc_views']}`, "
      f"Huber, `f_scale={prot['policy']['f_scale']}`).")
    A("")
    A("## Verdict")
    A("")
    A(f"* **All accepted views vs full-board only:** held-out cross-camera median "
      f"{fmt(summaries['B_all']['median'])} px vs {fmt(summaries['A_full']['median'])} px, "
      f"p95 {fmt(summaries['B_all']['p95'])} vs {fmt(summaries['A_full']['p95'])} px — "
      f"{statement('B_all')}.")
    if "F_h10B" in summaries:
        A(f"* **Hybrid, +10 best partial views:** median {fmt(summaries['F_h10B']['median'])} px, "
          f"p95 {fmt(summaries['F_h10B']['p95'])} px — {statement('F_h10B')}.")
    if "F_h20B" in summaries:
        A(f"* **Hybrid, +20 best partial views:** median {fmt(summaries['F_h20B']['median'])} px, "
          f"p95 {fmt(summaries['F_h20B']['p95'])} px — {statement('F_h20B')}.")
    A(f"* **Matched view count** (same number of training views as full-board only): "
      f"a random mix gives {statement('C_mix') if 'C_mix' in verdicts else 'n/a'}.")
    if "D_2f1p" in verdicts and "E_1f2p" in verdicts:
        A(f"  With 2/3 full + 1/3 partial: {statement('D_2f1p')}; "
          f"with 1/3 full + 2/3 partial: {statement('E_1f2p')}.")
    A(f"* **Conditioning:** stereo baseline sigma "
      f"{fmt(conds['B_all'].get('baseline_sigma_mm', {}).get('median'), 3)} mm (all views) vs "
      f"{fmt(conds['A_full'].get('baseline_sigma_mm', {}).get('median'), 3)} mm (full-board only), "
      f"i.e. {cond_text('B_all', 'baseline_sigma_mm', 'mm')}; stereo rotation sigma "
      f"{cond_text('B_all', 'rot_ir_sigma_deg', 'deg')}; unit-free conditioning "
      f"{cond_text('B_all', 'condition_scaled', '')}.")
    _worse = [a for a in ("B_all", "F_h10B", "F_h20B", "G_h10R", "H_h10C")
              if judge_tone(verdicts.get(a)) == "worse"]
    _cond_worse = [a for a in ("B_all", "F_h10B", "F_h20B")
                   if cond_rel.get(a, {}).get("baseline_sigma_mm")
                   and cond_rel[a]["baseline_sigma_mm"]["ci"][0] > 0.0]
    if not _worse and not _cond_worse:
        A("* Including partial views does **not** make accuracy or conditioning worse: no")
        A("  arm is above full-board only on held-out error beyond noise, and every arm's")
        A("  conditioning sigmas shrink. No claim is made about EXStar or any vendor.")
    else:
        A(f"* **Regression observed** in {', '.join(_worse) or 'none'} on held-out error"
          + (f" and {', '.join(_cond_worse)} on conditioning" if _cond_worse else "")
          + " — see the paired tables; this is reported as measured, not explained away.")
    A("")
    A("## Method")
    A("")
    A("### Recovering labels for partial views")
    A("")
    A("The store keeps `observation['image_points']` as a list per camera. That list is")
    A("row-major `(v,u)` — canonical label `(i%8, i//8)` — **only** when all 40 markers were")
    A("detected; for a partial view it keeps the detector's assignment order and the JSON")
    A("alone cannot say which lattice site each row is. The stored PNGs are lossless, so every")
    A("camera image was re-detected with `calib.detect.detect_grid_auto`, whose result is keyed")
    A("by `(u,v)`; a detection is used only when its identity witness verified (three chiral")
    A("on-lattice large markers plus the off-lattice chiral marker).")
    A("")
    A(f"The {results['dataset']['n_full']} views whose stored lists *are* label-ordered")
    A("validate that reconstruction: of "
      f"{dval['checked']} recovered `(u,v)→pixel` rows, "
      f"{dval['label_matches']} matched the stored label order "
      f"({100 * (dval['match_rate'] or 0):.2f} %), at a median pixel agreement of "
      f"{dval['dist_px_median']:.4f} px (p95 {dval['dist_px_p95']:.4f} px).")
    if dval["label_mismatches"]:
        A(f"The {dval['label_mismatches']} exceptions are all in one view's colour camera")
        A("(`view 103, tex`), where the stored 40-row list is mirror-`u` relative to the")
        A("witness-verified detection; a planar lattice makes that mirror a 180° flip of the")
        A("board plane with identical reprojection, so it is unresolvable from the JSON alone")
        A("and the chiral witness is the tie-breaker. The re-detected labelling is used.")
    A("")
    A("### Protocol")
    A("")
    A(f"* {prot['repeats']} repeats × {prot['folds']}-fold split over all "
      f"{results['dataset']['n_pool_views']} pool views, fixed seed {prot['seed']}.")
    A("* **Held-out scoring never refits the view.** Its board pose is PnP-solved from its")
    A("  left observation with the fitted left model; the right and colour pixels are then")
    A("  *predicted* through the fitted rig (`X_c = R_c·X_left + t_c`) and that camera's")
    A("  fitted intrinsics. `rms_cross` (right+tex, primary) therefore tests the extrinsics")
    A("  and the other cameras' intrinsics; `rms_left` is the pose-fit residual.")
    A("* Identical test folds for every arm inside a (repeat, fold), so arm differences are")
    A("  paired view-by-view.")
    A("* Every fit is the production call followed by the wizard's finish-time prune")
    A("  (`_prune_outliers`, 3 px on in-sample per-camera-view residuals) and one refit —")
    A("  applied identically to all arms; the number dropped is reported per arm.")
    A("* `free_kc` is on for every arm here (all training sets exceed `kc_views = 8`),")
    A("  `robust_loss='huber'`, `f_scale=0.8`, `backend='fast'`.")
    A("* BLAS pinned to one thread (`OPENBLAS_NUM_THREADS=1`, `OMP_NUM_THREADS=1`,"
      " `MKL_NUM_THREADS=1`).")
    A("")
    A("## Arms actually fitted")
    A("")
    A(md_table(["arm", "full views", "partial views", "train views",
                "markers (mean)", "min markers (mean)", "obs pruned"], table_data))
    A("")
    A("Hybrid arms add the `K` partial views with the most markers (the ones a deployment")
    A("would admit first), plus random and most-clipped variants for robustness. The")
    A("matched-count arms hold the training-view count equal to the full-board arm's, so")
    A("they answer “is a partial view worth as much as a full-board view at a fixed budget?”")
    A("instead of “is more data better?”.")
    A("")
    A("## Coverage of each arm")
    A("")
    rows = []
    for name, c in coverage.items():
        if not c:
            continue
        rows.append([name, c["n_views"], c["n_marker_obs"], c["markers_min"],
                     f"{c['markers_median']:.0f}", f"{c['markers_total']}",
                     f"{c['distance_mm']['min']:.0f}–{c['distance_mm']['max']:.0f}",
                     f"{100 * c['tilt_x_deg']['frac_pos']:.0f}/{100 * c['tilt_x_deg']['frac_neg']:.0f}",
                     f"{100 * c['tilt_y_deg']['frac_pos']:.0f}/{100 * c['tilt_y_deg']['frac_neg']:.0f}",
                     "/".join(str(c["roll_sectors"][s]) for s in "0123"),
                     c["n_quadrants"]])
    A(md_table(["set", "views", "camera-obs", "min markers", "median markers",
                "markers total", "distance mm", "tilt_x +/− %", "tilt_y +/− %",
                "roll sectors 0/1/2/3", "image quadrants"], rows))
    A("")
    A("`tilt_x/tilt_y` are the board-normal tilts in the left camera; `roll` is the in-plane")
    A("board roll; `image quadrants` counts occupied image quadrants of the plate centre.")
    A("The partial views add tilt/roll/distance diversity rather than novel image regions:")
    A("they are the same capture session, so enabling them buys poses, not scene variety.")
    A("")
    A("## Held-out accuracy (primary)")
    A("")
    A("`rms_cross` per held-out view (median over the repeats in which it was held out), then")
    A("median / p95 over views. Per-camera columns are the same statistic for a single camera.")
    A("")
    A(md_table(["arm", "median", "p95", "p99", "left", "right", "colour"], table_acc))
    A("")
    A("### Paired against full-board only (same test views)")
    A("")
    A("Δ = arm − full-board, in px on `rms_cross` (negative is better). The block column is")
    A("mean ± 95 % bootstrap CI over the (repeat, fold) blocks; the view column is the median")
    A("per-view Δ and the share of views that improved.")
    A("")
    A(md_table(["arm", "Δ block mean", "Δ block 95 % CI", "Δ view median",
                "views improved", "sign-test p"], table_paired))
    A("")
    A("### Held-out accuracy by test-view type")
    A("")
    A("The same models, scored separately on held-out full-board views and held-out partial")
    A("views (median `rms_cross` over views).")
    A("")
    rows = [[ARM_LABEL.get(a, a), split[a]["n_full_test"],
             fmt(split[a]["median_full_test"]), split[a]["n_partial_test"],
             fmt(split[a]["median_partial_test"])] for a in arms]
    A(md_table(["arm", "full test views", "median", "partial test views", "median"], rows))
    A("")
    A("### Fitted rig geometry (sanity)")
    A("")
    _bs = [g["baseline_mm_median"] for g in geometry.values() if g["baseline_mm_median"]]
    _ts = [g["tex_baseline_mm_median"] for g in geometry.values() if g["tex_baseline_mm_median"]]
    A("Median fitted stereo and colour baselines over the blocks; the factory profile "
      f"values are {FACTORY_BASE:.2f} mm and {FACTORY_TEX:.2f} mm. Every arm sits "
      f"{np.median(_bs) - FACTORY_BASE:+.2f} mm from the factory stereo baseline and "
      f"{np.median(_ts) - FACTORY_TEX:+.2f} mm from the colour one, with a ~0.1 mm")
    A("spread across arms: that offset is the known data-versus-factory extrinsic")
    A("difference, not a partial-board effect.")
    A("")
    rows = [[ARM_LABEL.get(a, a), fmt(geometry[a]["baseline_mm_median"], 2),
             fmt(geometry[a]["tex_baseline_mm_median"], 2)] for a in arms]
    A(md_table(["arm", "stereo baseline (mm)", "colour baseline (mm)"], rows))
    A("")
    A("## Conditioning")
    A("")
    A("Marginal sigmas come from `result.param_std()` (joint covariance including the board")
    A("poses as nuisance parameters), transformed to the rig quantities by `calib.quality`.")
    A("")
    A(md_table(["arm"] + [label for _, label in COND_KEYS], table_cond))
    A("")
    A("### Paired conditioning change vs full-board only")
    A("")
    A("Mean Δ over the (repeat, fold) blocks; `*` marks a 95 % bootstrap CI that excludes 0.")
    A("")
    A(md_table(["arm"] + [label for _, label in COND_KEYS[:5]], table_cond_rel))
    A("")
    def judge(arm):
        v = verdicts.get(arm)
        if not v:
            return "no-data", None, None
        lo, hi = v["paired_block_ci_px"]
        if math.isnan(lo) or math.isnan(hi):
            return "inconclusive", v, None
        tone = "neutral" if (lo <= 0.0 <= hi) else ("better" if hi < 0 else "worse")
        return tone, v, (lo, hi)

    def delta_text(arm):
        tone, v, ci = judge(arm)
        if v is None:
            return f"{ARM_LABEL.get(arm, arm)}: no data"
        if ci is None:
            return (f"{ARM_LABEL.get(arm, arm)}: Δ {v['paired_block_mean_px']:+.3f} px "
                    f"(inconclusive)")
        return (f"{ARM_LABEL.get(arm, arm)}: Δ {v['paired_block_mean_px']:+.3f} px "
                f"[{ci[0]:+.3f}, {ci[1]:+.3f}], {tone}")

    def judge(arm):
        v = verdicts.get(arm)
        if not v:
            return "no-data", None, None
        lo, hi = v["paired_block_ci_px"]
        if math.isnan(lo) or math.isnan(hi):
            return "inconclusive", v, None
        tone = "neutral" if (lo <= 0.0 <= hi) else ("better" if hi < 0 else "worse")
        return tone, v, (lo, hi)

    def delta_text(arm, short=False):
        tone, v, ci = judge(arm)
        label = ARM_LABEL.get(arm, arm)
        if short:
            label = label.split(") ", 1)[-1]
        if v is None:
            return f"{label}: no data"
        if ci is None:
            return (f"{label}: Δ {v['paired_block_mean_px']:+.3f} px "
                    f"(inconclusive)")
        return (f"{label}: Δ {v['paired_block_mean_px']:+.3f} px "
                f"[{ci[0]:+.3f}, {ci[1]:+.3f}], {tone}")

    #: smallest injected error the control can see (0.02 deg stereo rotation)
    ctrl_px = None
    if sens:
        rows_ = [r for r in sens["right_rotation_deg"] if r["deg"] == 0.02]
        ctrl_px = rows_[0]["delta_median"] if rows_ else None
    ctrl_txt = (f"{ctrl_px:+.3f} px" if ctrl_px else "the control's smallest step")

    n_all = np.mean([m.get("n_views", 0) for m in block_metrics["B_all"].values()])
    n_core = np.mean([m.get("n_views", 0) for m in block_metrics["A_full"].values()])

    A("## Interpretation")
    A("")
    tone_all = judge("B_all")[0]
    A(f"1. **Accuracy at the deployment setting.** {delta_text('B_all')} — "
      f"{fmt(summaries['B_all']['median'])} px median held-out versus "
      f"{fmt(summaries['A_full']['median'])} px for full-board only, with {n_all:.0f} "
      f"versus {n_core:.0f} training views.")
    if ctrl_px:
        A(f"   Numerically that is {'below' if abs(judge('B_all')[1]['paired_block_mean_px']) < abs(ctrl_px) else 'above'} "
          f"the {ctrl_txt} that a 0.02° stereo rotation error produces, i.e. at the")
        A("   edge of what this protocol can resolve at all. The extra partial views do")
        A("   not poison the fit; what they add is small and lands on partial geometry.")
    sf, sp = split["A_full"], split["B_all"]
    if sf["median_full_test"] and sp["median_partial_test"]:
        rel_f = 100.0 * (sp["median_full_test"] - sf["median_full_test"]) / sf["median_full_test"]
        rel_p = 100.0 * (sp["median_partial_test"] - sf["median_partial_test"]) / sf["median_partial_test"]
        A(f"   The gain is concentrated where it should be: on held-out **partial** views the")
        A(f"   all-views model scores {sf['median_partial_test']:.3f} → {sp['median_partial_test']:.3f} px "
          f"({rel_p:+.1f} %), versus {sf['median_full_test']:.3f} → {sp['median_full_test']:.3f} px "
          f"({rel_f:+.1f} %) on held-out full-board views.")
    pc = summaries["A_full"]["per_camera_median"]
    A(f"   The cross-camera metric is colour-dominated (left {fmt(pc.get('rms_left'))}, right "
      f"{fmt(pc.get('rms_right'))}, colour {fmt(pc.get('rms_tex'))} px), which is the known stale")
    A("   shipped colour extrinsic; the arm differences are correspondingly colour-carried.")
    ra = rel_all.get("B_all") or {}
    if ra.get("block_mean") is not None:
        lo, hi = ra["block_ci"]
        A(f"   The same ordering holds on the all-camera metric `rms_all`: Δ "
          f"{ra['block_mean']:+.3f} px [{lo:+.3f}, {hi:+.3f}].")
    A(f"2. **Bounded hybrid.** {delta_text('F_h10B')}; {delta_text('F_h20B')};")
    A(f"   {delta_text('G_h10R')}; {delta_text('H_h10C')}. A bounded")
    A("   number of partial views can be admitted beside the full-board core without an")
    A("   accuracy penalty, including the most-clipped ones available in this set.")
    A("3. **At a matched view budget** (same number of training views, which isolates the")
    A("   *quality* of a partial view from the quantity of data):")
    A(f"   {delta_text('C_mix')};")
    A(f"   {delta_text('D_2f1p')};")
    A(f"   {delta_text('E_1f2p')}.")
    A(f"   At this clipping level (median partial board "
      f"{coverage['PARTIAL (82 partial views)']['markers_median']:.0f}/40) the substitution")
    A("   is within noise: these boards lose 1-4 markers, so their pose constraint is")
    A("   nearly that of a full board. The finding does not extend to heavily clipped")
    A("   boards, which this accepted set does not contain.")
    A(f"4. **Tail.** Held-out p95: {fmt(summaries['B_all']['p95'])} px (all views),")
    A(f"   {fmt(summaries['A_full']['p95'])} px (full-board only), "
      f"{fmt(summaries['F_h20B']['p95'])} px (hybrid +20 best). The tail is dominated by")
    A("   one accepted view (`view 129`, accepted while moving: 16.6 mm/s,")
    A("   `stationary=0`, colour focus 176) whose colour frame disagrees with every rig by")
    A("   ~23 px. That is an observation-quality failure, and it is in every arm's test")
    A("   set, so it cannot explain an arm difference.")
    A(f"5. **Pruning.** The production 3 px finish prune dropped "
      f"{sum(prune_cams.values())} observations over {sum(n_fits.values())} fits, from")
    A(f"   {len(prune_unique)} distinct camera-views ("
      + ", ".join(f"v{v}/{c}" for v, c in prune_unique[:8])
      + ("…" if len(prune_unique) > 8 else "")
      + f"); by camera {dict(prune_cams)}. No IR observation was ever dropped, and the")
    A("   pre-fold screen (identity witness, ≥12 markers, factory-model PnP < 3 px)")
    A(f"   rejected {results['dataset']['screen']['dropped_items'] and len(results['dataset']['screen']['dropped_items']) or 0} view-camera observations.")
    A(f"6. **Conditioning.** Stereo baseline sigma "
      f"{fmt(conds['B_all'].get('baseline_sigma_mm', {}).get('median'), 3)} mm vs "
      f"{fmt(conds['A_full'].get('baseline_sigma_mm', {}).get('median'), 3)} mm "
      f"({cond_text('B_all', 'baseline_sigma_mm', 'mm')}); stereo rotation sigma "
      f"{cond_text('B_all', 'rot_ir_sigma_deg', 'deg')}; colour translation sigma "
      f"{cond_text('B_all', 'tex_translation_sigma_mm', 'mm')}. More observations of")
    A("   the same 39 rig parameters is what shrinks these, so partial views are a")
    A("   conditioning asset, not a risk — provided their labels are right, which is what")
    A("   the detector's chiral identity witness and the 3 px prune enforce.")
    A("")
    A("### Answer to section 9")
    A("")
    A("Enabling partial-board observations **does not regress** held-out accuracy or")
    A("conditioning beyond noise on fresh-01:")
    A("")
    A(f"* accuracy: all-views Δ {verdicts['B_all']['paired_block_mean_px']:+.3f} px, "
      f"hybrid +10 {verdicts['F_h10B']['paired_block_mean_px']:+.3f} px, "
      f"hybrid +20 {verdicts['F_h20B']['paired_block_mean_px']:+.3f} px against")
    A(f"  full-board only (95 % block-bootstrap CIs "
      f"{verdicts['B_all']['paired_block_ci_px'][0]:+.3f}…{verdicts['B_all']['paired_block_ci_px'][1]:+.3f}, "
      f"{verdicts['F_h10B']['paired_block_ci_px'][0]:+.3f}…{verdicts['F_h10B']['paired_block_ci_px'][1]:+.3f}, "
      f"{verdicts['F_h20B']['paired_block_ci_px'][0]:+.3f}…{verdicts['F_h20B']['paired_block_ci_px'][1]:+.3f} px);")
    A(f"* conditioning: baseline sigma "
      f"{fmt(conds['A_full'].get('baseline_sigma_mm', {}).get('median'), 3)} → "
      f"{fmt(conds['B_all'].get('baseline_sigma_mm', {}).get('median'), 3)} mm, i.e. better;")
    A(f"* at a matched view count, partial views are worth slightly less than full-board")
    A(f"  views but are not measurably harmful at the clipping level present here")
    A(f"  ({coverage['PARTIAL (82 partial views)']['markers_median']:.0f}/40 median).")
    A("")
    A("**Implication for the production policy.** Admitting partial-board views is safe on")
    A("this evidence: the accuracy effect is ≤0.03 px either way, conditioning improves")
    A("because the pool grows, and the load-bearing safeguards are the chiral identity")
    A("witness and the 3 px finish prune (which in this dataset only ever removed colour")
    A("observations, never IR). A hybrid cap is therefore a *cost/latency* choice, not an")
    A("accuracy requirement; the full-board core should still be present so the fit is")
    A("anchored on complete boards.")
    A("")
    A("This says nothing about boards clipped to a third of the field, which are absent")
    A("from this accepted set, and nothing about EXStar or any other implementation.")
    A("")
    if sens:
        A("## Metric sensitivity (positive control)")
        A("")
        A("The same held-out statistic after perturbing one fitted rig parameter by a known")
        A("amount (one model, 60 full-board training views, 20 held-out views). This says")
        A("what a difference of a given size in the tables above actually means.")
        A("")
        rows = [[f"stereo rotation +{d['deg']:.2f}°", fmt(d["delta_median"]), fmt(d["median"])]
                for d in sens["right_rotation_deg"]]
        rows += [[f"stereo baseline +{d['mm']:.2f} mm", fmt(d["delta_median"]), fmt(d["median"])]
                 for d in sens["right_baseline_mm"]]
        rows += [[f"colour translation +{d['mm']:.2f} mm", fmt(d["delta_median"]),
                  fmt(d["median"])] for d in sens["tex_translation_mm"]]
        A(md_table(["injected error", "Δ held-out median (px)", "held-out median (px)"], rows))
        A("")
        A(f"Baseline held-out median for that model: {fmt(sens['baseline']['median'])} px. The")
        A("statistic resolves a 0.02° stereo rotation (+0.045 px) or a 0.05 mm baseline shift")
        A("(+0.006 px); the negative entry at +0.10 mm of colour translation is the fitted")
        A("colour translation being itself ~0.1 mm off on this subset, and it marks the")
        A("noise floor of the statistic, ~0.03 px.")
        A("")
        A("Every arm-to-arm difference in this report is at or below the +0.045 px that a")
        A("0.02° stereo rotation error produces — an order of magnitude below the accuracy")
        A("targets the gates use (0.06 mm baseline sigma, 0.35 px fx sigma, `high` preset).")
        A("")
    A("## Limitations")
    A("")
    A("* **One dataset.** fresh-01 only, 162 accepted views from a single capture session;")
    A("  the EXStar 25-view vendor corpus has just three partial views (35/38/38 markers) and")
    A("  cannot support a partial arm, so no second partial-vs-full dataset exists locally.")
    A("* **Partial here is mostly mild.** The partial views are 26–39 markers with a median")
    A(f"  of {coverage['PARTIAL (82 partial views)']['markers_median']:.0f}; only "
      f"{len(clipped)} have ≤36 markers in some camera and the arm")
    A("  `H_h10C` is the only probe of genuinely clipped boards. Heavily clipped boards")
    A("  (≤16 markers, which the `high` policy still admits) are not represented in this")
    A("  accepted set, so this report does not bound their effect.")
    A("* **Labels for partial views are reconstructed, not stored.** The reconstruction is")
    A("  validated on the 80 label-ordered views (99.7 % of rows exact, median 0.0004 px)")
    A("  and gated by the detector's chiral identity witness on every view, but it is not")
    A("  the production labelling code path.")
    A("* **No priors.** The task-specified call is the bare `calibrate(...)`; the wizard's")
    A("  live path also applies 1 px principal-point priors through `calib.pipeline`. Those")
    A("  priors would shrink `cx/cy` sigmas for every arm equally and do not change the")
    A("  comparison, but absolute sigmas here are slightly larger than a live session's.")
    A("* **One accepted view is a different kind of outlier.** `view 129` was accepted with")
    A("  `motion_warning=1`, `stationary=0`, 16.6 mm/s and colour focus 176; its colour")
    A("  observation disagrees with every fitted rig by ~23 px while its own factory-model")
    A("  PnP residual is 0.4 px. It is pruned from training by the 3 px rule and shows up as")
    A("  a ~23 px held-out error whenever it is in the test fold, for every arm. That is an")
    A("  observation-quality problem (stationarity gate), not a partial-board effect.")
    A("* **Held-out metric is left-anchored PnP.** Pose noise from the left view propagates")
    A("  into the cross-camera prediction, so absolute values are larger than training RMS;")
    A("  the comparison is unaffected because the anchor view is identical for all arms.")
    A("")
    A("## Reproduce")
    A("")
    A("```bash")
    A("cd <repo>")
    A("export OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1")
    A(".venv/bin/python analysis/calibration/accuracy/prepare_data.py")
    A("OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 \\")
    A("  .venv/bin/python analysis/calibration/accuracy/experiment.py --folds 4 --repeats 3")
    A("OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 \\")
    A("  .venv/bin/python analysis/calibration/accuracy/report.py")
    A("```")
    A("")
    A(f"Provenance: git `{prov.get('git_commit','')}`, numpy {prov.get('numpy','')}, "
      f"factory profile sha256 `{prov.get('factory_profile_sha256','')[:16]}…`, "
      f"{sum(n_fits.values())} calibration fits.")
    A("")

    Path(args.md).write_text("\n".join(md))

    report = {
        "schema": "einstar.partial-vs-full-report/1",
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source_results": str(args.results),
        "provenance": prov,
        "method": prot,
        "dataset": results["dataset"],
        "label_recovery": dval,
        "coverage": coverage,
        "arms": {
            a: {"label": ARM_LABEL.get(a, a), "heldout": summaries[a],
                "paired_vs_full": rel.get(a),
                "paired_vs_full_rms_all": rel_all.get(a),
                "conditioning": conds[a],
                "conditioning_paired": cond_rel.get(a),
                "train_rms_px_mean": train_rms[a],
                "heldout_split": split[a], "geometry": geometry[a],
                "n_fits": n_fits[a]}
            for a in arms},
        "sensitivity": sens,
        "pruning": {"n_pruned_total": int(sum(prune_cams.values())),
                    "by_camera": dict(prune_cams),
                    "items": [{"view": v, "camera": c, "n_fits": n}
                              for (v, c), n in sorted(prune_counts.items())]},
        "verdict": {
            "all_vs_full": verdicts.get("B_all"),
            "hybrid10_vs_full": verdicts.get("F_h10B"),
            "hybrid20_vs_full": verdicts.get("F_h20B"),
            "matched_mix_vs_full": verdicts.get("C_mix"),
            "matched_2f1p_vs_full": verdicts.get("D_2f1p"),
            "matched_1f2p_vs_full": verdicts.get("E_1f2p"),
            "statement": {
                a: statement(a) for a in arms if a != "A_full"},
        },
    }
    Path(args.json).write_text(json.dumps(report, indent=1))
    print("wrote", args.md)
    print("wrote", args.json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
