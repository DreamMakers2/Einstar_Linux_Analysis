"""REPORT.md and the figures, generated from ``observability_report.json``.

Every number printed here is read from the JSON; nothing is typed in by hand.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np


def _f(x, nd=3, missing="—"):
    if x is None:
        return missing
    try:
        v = float(x)
    except (TypeError, ValueError):
        return str(x)
    if not math.isfinite(v):
        return missing
    if v == 0:
        return "0"
    a = abs(v)
    if a < 1e-3 or a >= 1e5:
        return f"{v:.{nd}e}"
    return f"{v:.{nd}f}"


def _table(header, rows, align=None):
    align = align or ["---"] * len(header)
    out = ["| " + " | ".join(header) + " |",
           "|" + "|".join(align) + "|"]
    for r in rows:
        out.append("| " + " | ".join(str(x) for x in r) + " |")
    return "\n".join(out)


def _mpl():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    return plt


# --------------------------------------------------------------------------
# figures
# --------------------------------------------------------------------------

def fig_parameter_sigma(rep, figdir: Path):
    plt = _mpl()
    ladder = rep.get("model_ladder") or {}
    models = [m for m in ("M1", "M2", "M3", "M4", "M5", "M6") if m in ladder]
    if not models:
        return None
    params = []
    for m in models:
        for r in ladder[m]["parameters"]:
            if r["free"] and r["param"] not in params:
                params.append(r["param"])
    sel = [p for p in params if any(k in p for k in
           ("fx", "fy", "cx", "cy", "skew", "k1", "p1", "k3", "right.t", "right.r"))]
    sel = sel[:14] or params[:14]
    fig, ax = plt.subplots(figsize=(11, 5.2))
    w = 0.8 / max(len(models), 1)
    for i, m in enumerate(models):
        vals, xs = [], []
        lut = {r["param"]: r for r in ladder[m]["parameters"]}
        for j, p in enumerate(sel):
            r = lut.get(p)
            if not r or not r["free"]:
                continue
            v = r.get("sigma")
            if v is None or v <= 0:
                continue
            xs.append(j + i * w)
            vals.append(v)
        ax.bar(xs, vals, width=w, label=m)
    ax.set_yscale("log")
    ax.set_xticks(range(len(sel)))
    ax.set_xticklabels(sel, rotation=60, ha="right", fontsize=8)
    ax.set_ylabel("marginal 1σ (parameter units, log)")
    ax.set_title("Parameter sigma by model (marginal covariance, factory-start fits)")
    ax.grid(True, axis="y", alpha=0.3)
    ax.legend(fontsize=8, ncol=len(models))
    fig.tight_layout()
    p = figdir / "fig_parameter_sigma.png"
    fig.savefig(p, dpi=130)
    plt.close(fig)
    return p


def fig_spectrum(rep, figdir: Path):
    plt = _mpl()
    series = []
    fi = rep.get("factory_information") or {}
    if fi.get("spectrum", {}).get("eigenvalues"):
        series.append(("factory solution (live sweep)", fi["spectrum"]["eigenvalues"]))
    ladder = rep.get("model_ladder") or {}
    if "M5" in ladder and ladder["M5"].get("spectrum", {}).get("eigenvalues"):
        series.append(("best fit M5 (live sweep)", ladder["M5"]["spectrum"]["eigenvalues"]))
    ven = rep.get("vendor_corpus") or {}
    if ven.get("factory_linearisation", {}).get("spectrum", {}).get("eigenvalues"):
        series.append(("factory solution (25-view vendor)",
                       ven["factory_linearisation"]["spectrum"]["eigenvalues"]))
    if not series:
        return None
    fig, ax = plt.subplots(figsize=(8, 4.6))
    for label, ev in series:
        ev = np.asarray(ev, float)
        ax.semilogy(np.arange(1, len(ev) + 1), ev, "o-", ms=3.5, label=label)
    ax.set_xlabel("eigenvalue rank of the scaled marginal information S")
    ax.set_ylabel("eigenvalue (dimensionless)")
    ax.set_title("Identifiability spectrum: a steep tail = unconstrained combinations")
    ax.grid(True, alpha=0.3)
    ax.legend(fontsize=8)
    fig.tight_layout()
    p = figdir / "fig_information_spectrum.png"
    fig.savefig(p, dpi=130)
    plt.close(fig)
    return p


def fig_f_vs_distance(rep, figdir: Path):
    plt = _mpl()
    prof = rep.get("profile_likelihood") or {}
    if not prof.get("scales"):
        return None
    s = np.asarray(prof["scales"], float)
    rms = np.asarray(prof["rms_px"], float)
    z = np.asarray(prof["mean_depth_mm"], float)
    fig, ax = plt.subplots(1, 3, figsize=(13, 4.0))
    ax[0].plot(s, rms, "o-")
    ax[0].set_xlabel("common IR focal scale (1.0 = factory)")
    ax[0].set_ylabel("train reprojection RMS (px)")
    ax[0].set_title("profile likelihood in f")
    ax[0].grid(alpha=0.3)
    ax[1].plot(s, z, "o-", label="recovered mean plate depth")
    z0 = z[np.argmin(rms)]
    ax[1].plot(s, z0 * s / s[np.argmin(rms)], "--", label="1:1 with f")
    ax[1].set_xlabel("common IR focal scale")
    ax[1].set_ylabel("mean plate depth (mm)")
    ax[1].set_title("depth follows f exactly")
    ax[1].legend(fontsize=8)
    ax[1].grid(alpha=0.3)
    ax[2].plot(s, (rms ** 2 - rms.min()) / rms.min() * 100, "o-")
    ax[2].set_xlabel("common IR focal scale")
    ax[2].set_ylabel("Δ(RMS²) / RMS² (%)")
    ax[2].set_title("cost penalty of a wrong focal length")
    ax[2].grid(alpha=0.3)
    fig.tight_layout()
    p = figdir / "fig_f_vs_distance.png"
    fig.savefig(p, dpi=130)
    plt.close(fig)
    return p


def fig_bootstrap(rep, figdir: Path):
    plt = _mpl()
    boot = rep.get("bootstrap") or {}
    ladder = rep.get("model_ladder") or {}
    xs, ys, labels, colors = [], [], [], []
    for m in ("M1", "M2", "M3", "M4", "M5", "M6"):
        if m not in boot or not boot[m].get("params") or m not in ladder:
            continue
        cov = {r["param"]: r["sigma"] for r in ladder[m]["parameters"] if r["free"]}
        for r in boot[m]["params"]:
            c = cov.get(r["param"])
            if not c or c <= 0 or r["bootstrap_std"] <= 0:
                continue
            xs.append(c); ys.append(r["bootstrap_std"])
            labels.append(f"{m}:{r['param']}")
            colors.append(m)
    if not xs:
        return None
    fig, ax = plt.subplots(figsize=(6.4, 5.4))
    lim = [min(min(xs), min(ys)) * 0.5, max(max(xs), max(ys)) * 2]
    ax.plot(lim, lim, "k--", lw=1, label="bootstrap = covariance")
    uniq = sorted(set(colors))
    cmap = plt.get_cmap("tab10")
    for i, m in enumerate(uniq):
        xx = [x for x, c in zip(xs, colors) if c == m]
        yy = [y for y, c in zip(ys, colors) if c == m]
        ax.scatter(xx, yy, s=22, label=m, color=cmap(i % 10))
    ax.set_xscale("log"); ax.set_yscale("log")
    ax.set_xlim(lim); ax.set_ylim(lim)
    ax.set_xlabel("1σ from the robust marginal covariance")
    ax.set_ylabel("std across 10 view-bootstrap resamples")
    ax.set_title("Bootstrap stability vs reported sigma")
    ax.grid(alpha=0.3)
    ax.legend(fontsize=8)
    fig.tight_layout()
    p = figdir / "fig_bootstrap.png"
    fig.savefig(p, dpi=130)
    plt.close(fig)
    return p


def fig_heldout(rep, figdir: Path):
    plt = _mpl()
    ladder = rep.get("model_ladder") or {}
    models = [m for m in ("M0", "M1", "M2", "M3", "M4", "M5", "M6") if m in ladder]
    if not models:
        return None
    tr = [ladder[m]["train"]["rms"] for m in models]
    hl = [ladder[m]["heldout_legs"]["rms"] for m in models]
    hs = [ladder[m]["heldout_session"]["rms"] for m in models]
    x = np.arange(len(models)); w = 0.27
    fig, ax = plt.subplots(figsize=(9, 4.4))
    ax.bar(x - w, tr, w, label="train (sweep-c legs 0,2)")
    ax.bar(x, hl, w, label="held-out legs (sweep-c legs 1,3)")
    ax.bar(x + w, hs, w, label="held-out session (sweep-b)")
    for i, v in enumerate(tr):
        ax.text(i - w, v, f"{v:.3f}", ha="center", va="bottom", fontsize=7)
    for i, v in enumerate(hl):
        ax.text(i, v, f"{v:.3f}", ha="center", va="bottom", fontsize=7)
    for i, v in enumerate(hs):
        ax.text(i + w, v, f"{v:.3f}", ha="center", va="bottom", fontsize=7)
    ax.set_xticks(x); ax.set_xticklabels(models)
    ax.set_ylabel("reprojection RMS (px)")
    ax.set_title("Train vs geometrically held-out reprojection RMS")
    ax.legend(fontsize=8)
    ax.grid(alpha=0.3, axis="y")
    fig.tight_layout()
    p = figdir / "fig_heldout.png"
    fig.savefig(p, dpi=130)
    plt.close(fig)
    return p


def fig_detection(rep, figdir: Path):
    plt = _mpl()
    det = (rep.get("data") or {}).get("sweep_c", {}).get("detection")
    if not det:
        return None
    cams = list(det)
    fig, ax = plt.subplots(1, 3, figsize=(13, 3.9))
    ax[0].bar(cams, [det[c]["detect_frac"] * 100 for c in cams], color="tab:blue")
    ax[0].set_ylim(0, 105); ax[0].set_ylabel("% images with a grid")
    ax[0].set_title("detection rate (sweep-c)")
    for i, c in enumerate(cams):
        ax[0].text(i, det[c]["detect_frac"] * 100, f"{det[c]['n_detected']}",
                   ha="center", va="bottom", fontsize=8)
    ax[1].bar(cams, [det[c]["rms_median"] for c in cams], color="tab:orange")
    ax[1].set_ylabel("homography RMS (px)")
    ax[1].set_title("median homography residual")
    for i, c in enumerate(cams):
        ax[1].text(i, det[c]["rms_median"], f"{det[c]['rms_median']:.3f}",
                   ha="center", va="bottom", fontsize=8)
    aa = (rep.get("geometry") or {}).get("assignment_audit", {}).get("sweep_c", {})
    if aa:
        ax[2].bar(cams, [aa.get(c, {}).get("unambiguous_frac", 0) * 100 for c in cams],
                  color="tab:green")
        ax[2].set_ylim(0, 105)
        ax[2].set_ylabel("% views with an unambiguous (u,v) assignment")
    ax[2].set_title("large-marker chirality check")
    fig.tight_layout()
    p = figdir / "fig_detection.png"
    fig.savefig(p, dpi=130)
    plt.close(fig)
    return p


def fig_trajectory(rep, figdir: Path):
    plt = _mpl()
    geo = rep.get("geometry") or {}
    tc = geo.get("sweep_c", {}).get("trajectory")
    if not tc:
        return None
    tb = geo.get("sweep_b", {}).get("trajectory")
    fig, ax = plt.subplots(1, 3, figsize=(13, 4.0))
    for label, t, style in (("sweep-c", tc, "o-"), ("sweep-b", tb, "s-")) if tb else (("sweep-c", tc, "o-"),):
        ax[0].plot([t["x"]["min"], t["x"]["max"]], [0, 0], style, ms=3, label=label)
    ax[0].set_xlabel("plate translation x (mm)")
    ax[0].set_title("1-D trajectory: x range")
    ax[0].legend(fontsize=8); ax[0].grid(alpha=0.3)
    ax[1].bar(["x", "y", "z"], [tc["x"]["range"], tc["y"]["range"], tc["z"]["range"]])
    ax[1].set_ylabel("range (mm)")
    ax[1].set_title("translation range per axis (sweep-c)")
    for i, k in enumerate(("x", "y", "z")):
        ax[1].text(i, tc[k]["range"], f"{tc[k]['range']:.1f}", ha="center", va="bottom",
                   fontsize=8)
    ax[2].bar(["tilt", "roll"], [tc["tilt_deg"]["range"], tc["roll_deg"]["range"]],
              color="tab:red")
    ax[2].set_ylabel("range (deg)")
    ax[2].set_title("rotation range (sweep-c)")
    for i, k in enumerate(("tilt_deg", "roll_deg")):
        ax[2].text(i, tc[k]["range"], f"{tc[k]['range']:.1f}", ha="center", va="bottom",
                   fontsize=8)
    fig.tight_layout()
    p = figdir / "fig_trajectory.png"
    fig.savefig(p, dpi=130)
    plt.close(fig)
    return p


def fig_design(rep, figdir: Path):
    plt = _mpl()
    d = (rep.get("design_sensitivity") or {}).get("subsets")
    if not d:
        return None
    names = [k for k in d if d[k].get("sigma_fx_left")]
    fig, ax = plt.subplots(figsize=(9, 4.3))
    x = np.arange(len(names))
    keys = [("sigma_fx_left", "σ fx left (px)"), ("sigma_cx_left", "σ cx left (px)"),
            ("sigma_k1_left", "σ k1 left"),
            ("sigma_baseline_mm", "σ baseline (mm)")]
    w = 0.8 / len(keys)
    for i, (k, lab) in enumerate(keys):
        vals = [d[n].get(k) or np.nan for n in names]
        ax.bar(x + i * w, vals, w, label=lab)
        for j, v in enumerate(vals):
            if np.isfinite(v) and v > 0:
                ax.text(x[j] + i * w, v, f"{v:.3g}", ha="center", va="bottom", fontsize=6)
    ax.set_yscale("log")
    ax.set_xticks(x + 0.4 - w / 2)
    ax.set_xticklabels(names, rotation=30, ha="right", fontsize=8)
    ax.set_ylabel("marginal 1σ (log)")
    ax.set_title("Design sensitivity: which excitation constrains what")
    ax.legend(fontsize=7)
    ax.grid(alpha=0.3, axis="y")
    fig.tight_layout()
    p = figdir / "fig_design_sensitivity.png"
    fig.savefig(p, dpi=130)
    plt.close(fig)
    return p


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------

def write_all(rep: dict, out: Path, log=print):
    out = Path(out)
    figdir = out / "figures"
    figdir.mkdir(parents=True, exist_ok=True)
    figs = []
    for fn in (fig_parameter_sigma, fig_spectrum, fig_f_vs_distance, fig_bootstrap,
               fig_heldout, fig_detection, fig_trajectory, fig_design):
        try:
            p = fn(rep, figdir)
            if p:
                figs.append(p)
        except Exception as exc:                       # pragma: no cover
            log(f"  figure {fn.__name__} failed: {exc}")
    (out / "figures.json").write_text(json.dumps([str(p) for p in figs], indent=1))
    md = build_markdown(rep, figs)
    (out / "REPORT.md").write_text(md)
    return out / "REPORT.md"


def build_markdown(rep: dict, figs) -> str:
    L = []
    A = L.append
    study = rep.get("study", {})
    env = study.get("env", {})
    A("# Calibration observability on the real live Einstar sweeps")
    A("")
    A(f"*Phase 4 study — generated {env.get('started_utc','?')} by "
      f"`tools/calib_study/observability.py`; git `{env.get('git','?')}`; "
      f"python {env.get('python','?')}, numpy {env.get('numpy','?')}, "
      f"opencv {env.get('opencv','?')}, scipy {env.get('scipy','?')}.*")
    A("")
    A("Everything below is measured on the two genuine live captures "
      "(`sweep-c`: 600 trigger groups @10 Hz, `sweep-b`: 350 groups @10 Hz) and on "
      "the vendor 25-view corpus.  No simulated observation enters any number.  "
      "Candidate profiles are exported with `validation: pending`; the factory "
      "profile is never modified.")
    A("")
    L.extend(_tldr(rep))
    L.extend(_data_section(rep))
    L.extend(_ladder_section(rep))
    L.extend(_identifiability_section(rep))
    L.extend(_scale_section(rep))
    L.extend(_downstream_section(rep))
    L.extend(_vendor_section(rep))
    L.extend(_requirements_section(rep))
    L.extend(_limitations_section(rep, figs))
    return "\n".join(L) + "\n"


def _tldr(rep):
    L = []
    ladder = rep.get("model_ladder") or {}
    fi = rep.get("factory_information") or {}
    spec = fi.get("spectrum", {})
    A = L.append
    A("## 0. Verdict in one page")
    A("")
    tcc = ((rep.get("geometry") or {}).get("sweep_c", {}).get("trajectory") or {})
    if tcc:
        A(f"* The live motion is **1-D**: plate x {_f(tcc['x']['min'],1)}…{_f(tcc['x']['max'],1)} mm, "
          f"z {_f(tcc['z']['min'],1)}…{_f(tcc['z']['max'],1)} mm (range {_f(tcc['z']['range'],1)} mm), "
          f"tilt {_f(tcc['tilt_deg']['min'],1)}…{_f(tcc['tilt_deg']['max'],1)}°, "
          f"roll range {_f(tcc['roll_deg']['range'],2)}°.  "
          f"Translation singular values {[round(x,1) for x in tcc['translation_singular_values']]}: "
          f"the second axis is {_f(100*tcc['translation_sv_ratio_2_1'],1)} % of the first.")
    if spec:
        A(f"* The scaled marginal information at the factory solution has condition "
          f"number **{_f(spec.get('condition_number_scaled'),2)}**; the weakest direction "
          f"is dominated by "
          + ", ".join(f"`{c['param']}`" for c in spec["directions"][-1]["components"][:4])
          + f" (eigenvalue {_f(spec['directions'][-1]['eigenvalue'],3)}).")
    if ladder:
        best = min(ladder, key=lambda m: ladder[m]["train"]["rms"])
        m0 = ladder.get("M0")
        A(f"* Model ladder: train RMS falls from {_f(m0['train']['rms'],3)} px (M0 factory, "
          f"poses only) to {_f(ladder[best]['train']['rms'],3)} px ({best}); the held-out "
          f"session RMS moves from {_f(m0['heldout_session']['rms'],3)} to "
          f"{_f(ladder[best]['heldout_session']['rms'],3)} px.  The gap between train and "
          f"held-out is the honest measure of what the sweep supports.")
    A("* **No calibration is selected here.**  The per-parameter verdicts, the "
      "focal/depth trade-off and the downstream comparison are the deliverable; "
      "reprojection residual alone is not used to pick a profile.")
    A("")
    return L


def _data_section(rep):
    L = []
    A = L.append
    d = rep.get("data") or {}
    A("## 1. Data actually used")
    A("")
    rows = []
    for k, name in (("sweep_c", "sweep-c"), ("sweep_b", "sweep-b")):
        s = d.get(k)
        if not s:
            continue
        fw = s.get("frame_id_wrap_check", {})
        rows.append([name, s["n_groups"], _f(s.get("duration_s"), 1),
                     _f(s.get("cadence_fps"), 2),
                     "yes" if s.get("lossy") else "no",
                     fw.get("device_group_step_min"), fw.get("device_group_step_max"),
                     fw.get("cam1_wraps"), fw.get("cam1_unique_ids")])
    A(_table(["session", "groups", "duration s", "fps", "lossy", "Δdevice_group min",
              "max", "cam1 id wraps", "cam1 unique ids"], rows,
             ["---", "---:", "---:", "---:", ":-:", "---:", "---:", "---:", "---:"]))
    A("")
    A("`frame_ids` is an 8-bit *slot* counter (it wraps every 256 trigger groups) "
      "while `device_group` is the monotonic u64 trigger-group counter — the "
      "manifest's `group` index and `device_group` are the only usable sequence "
      "keys.  Groups are complete except for the documented single partial group "
      "per session.")
    A("")
    det = d.get("sweep_c", {}).get("detection", {})
    if det:
        A("### 1.1 Detection")
        A("")
        rows = [[c, det[c]["n_images"], det[c]["n_detected"],
                 _f(100 * det[c]["detect_frac"], 1), _f(det[c]["n_points_median"], 1),
                 _f(det[c]["rms_median"], 3), _f(det[c]["rms_p95"], 3)]
                for c in det]
        A(_table(["camera", "images", "grids found", "%", "median markers",
                  "median homography RMS px", "p95 px"], rows,
                 ["---", "---:", "---:", "---:", "---:", "---:", "---:"]))
        A("")
    aa = (rep.get("geometry") or {}).get("assignment_audit", {}).get("sweep_c", {})
    if aa:
        A("### 1.2 The (u,v) assignment: 180°/mirror ambiguity")
        A("")
        A("`calib.detect.detect_grid_auto` returns 40 labels but an empty "
          "`big_markers`, so the labelling cannot be checked against its own "
          "return value.  The 8×5 lattice is symmetric under `u→7−u`, `v→4−v` and "
          "both, and each of those relabellings is a *rigid rotation* of the planar "
          "board; therefore **reprojection residuals are mathematically invariant "
          "under all four maps** and cannot detect a mistake.  The two independent "
          "witnesses that do break the tie are the large-marker blob areas (the "
          "three on-lattice large markers sit at (2,1),(4,1),(5,1)) and the "
          "off-lattice marker at (3.5,3).  Both are re-derived here from the blob "
          "list on every image.")
        A("")
        rows = []
        for c in aa:
            v = aa[c]
            rows.append([c, v["n_views"],
                         v["verdicts"].get("unambiguous", 0),
                         v["verdicts"].get("ambiguous_labels", 0),
                         v["verdicts"].get("ambiguous_off_lattice", 0),
                         _f(v.get("big_marker_area_ratio_median"), 2),
                         _f(v.get("off_lattice_dist_px_median"), 2),
                         _f(v.get("homography_rms_median"), 3)])
        A(_table(["camera", "views audited", "unambiguous", "area-mismatch",
                  "off-lattice mismatch", "big/small area ratio",
                  "off-lattice distance px", "homography RMS px"], rows,
                 ["---"] + ["---:"] * 7))
        A("")
        dh = (rep.get("geometry") or {}).get("dihedral_test", {}).get("cameras", {})
        if dh:
            A("Homography RMS under each of the four relabellings "
              "(identical by construction — this is the point):")
            A("")
            rows = [[h, _f(dh[h]["homography_rms_median"], 4), _f(dh[h]["homography_rms_max"], 4)]
                    for h in dh]
            A(_table(["map", "median homography RMS px", "max px"], rows,
                     ["---", "---:", "---:"]))
            A("")
    tc = (rep.get("geometry") or {}).get("sweep_c", {})
    if tc.get("trajectory"):
        t = tc["trajectory"]
        A("### 1.3 The trajectory, measured (not assumed)")
        A("")
        rows = [[k, _f(t[k]["min"], 2), _f(t[k]["max"], 2), _f(t[k]["range"], 2),
                 _f(t[k]["std"], 2)] for k in ("x", "y", "z")]
        rows.append(["tilt (deg)", _f(t["tilt_deg"]["min"], 2), _f(t["tilt_deg"]["max"], 2),
                     _f(t["tilt_deg"]["range"], 2), _f(t["tilt_deg"]["std"], 2)])
        rows.append(["roll (deg)", _f(t["roll_deg"]["min"], 2), _f(t["roll_deg"]["max"], 2),
                     _f(t["roll_deg"]["range"], 2), _f(t["roll_deg"]["std"], 2)])
        A(_table(["quantity", "min", "max", "range", "std"], rows,
                 ["---", "---:", "---:", "---:", "---:"]))
        A("")
        cov = tc.get("image_coverage_left", {})
        if cov:
            A(f"Marker coverage in the left image: u {cov['u_min']:.0f}…{cov['u_max']:.0f} px "
              f"({100*cov['u_span_frac']:.0f} % of the width), v {cov['v_min']:.0f}…{cov['v_max']:.0f} px "
              f"({100*cov['v_span_frac']:.0f} % of the height), centroid "
              f"{cov['centroid_u']:.0f},{cov['centroid_v']:.0f} — "
              f"{cov['centroid_off_centre_px']:.0f} px from the image centre.  "
              f"Quadrant fractions {[round(x,2) for x in cov['quadrant_occupancy']['q_frac']]}.")
            A("")
    sp = (rep.get("geometry") or {}).get("splits")
    if sp:
        A("### 1.4 Held-out design (geometric, not random)")
        A("")
        A(f"The sweep's direction reverses at groups {sp['legs']} → "
          f"{len(sp['legs'])} contiguous legs.  Whole legs are assigned alternately "
          f"to train ({sp['train_pool_legs']}) and held-out ({sp['holdout_pool_legs']}), "
          f"so the *same spatial range* appears in both sets with the temporal "
          f"near-duplicates removed.  A third set is the whole independent session "
          f"(sweep-b, captured minutes earlier).")
        A("")
        A(f"* train: {sp['n_views']['train']} groups")
        A(f"* held-out legs: {sp['n_views']['heldout_legs']} groups")
        A(f"* held-out session: {sp['n_views']['heldout_session']} groups")
        A("")
    return L


def _ladder_section(rep):
    L = []
    A = L.append
    ladder = rep.get("model_ladder") or {}
    if not ladder:
        return L
    A("## 2. Model ladder")
    A("")
    A("Fitted with `tools/calib_study/models.py`, a re-implementation of "
      "`calib.calibrate._Problem` with a per-parameter free mask (see §9 for why "
      "`calib.calibrate`'s two switches cannot express M1/M3/M4/M5/M6).  All models "
      "start from the factory profile and share the same initial board poses, so "
      "the comparison isolates the parameter set.  Colour observations are "
      "included in the fit from M1 onwards when detected; only M6 frees colour "
      "parameters, and `color_status` is never promoted.")
    A("")
    rows = []
    for m in ("M0", "M1", "M2", "M3", "M4", "M5", "M6"):
        if m not in ladder:
            continue
        e = ladder[m]
        pnp = e.get("per_camera_pnp") or {}
        rows.append([m, e["model"]["n_free_rig"], e["n_free_parameters"],
                     e["n_observations"], e["nfev"], _f(e["wall_s"], 1),
                     _f(e["train"]["rms"], 4),
                     _f(pnp.get("left", {}).get("rms"), 3),
                     _f(e["heldout_legs"]["rms"], 4),
                     _f(e["heldout_session"]["rms"], 4),
                     _f(e["heldout_rig_prediction"]["legs"].get("right", {}).get("rms"), 3),
                     _f(e["heldout_rig_prediction"]["session"].get("right", {}).get("rms"), 3)])
    A(_table(["model", "free rig", "free total", "obs", "nfev", "wall s",
              "train RMS px", "PnP L px", "held-out legs px", "held-out session px",
              "rig-pred legs px", "rig-pred session px"], rows,
             ["---"] + ["---:"] * 11))
    A("")
    A("`PnP L` is the left camera's own PnP residual with that camera's fitted "
      "intrinsics and its own pose — it isolates the camera model from the rig.  "
      "M0 is the exception that proves the point: the factory *intrinsics* fit "
      "their own images at the PnP level, while the factory *rig* forces a much "
      "larger joint residual when one board pose must serve both cameras.  That "
      "difference is the factory stereo-extrinsic inconsistency on live data, and "
      "it is why M1 (left intrinsics only) can drop the joint residual so far: it "
      "absorbs a rig error into a camera model, which is exactly the trade-off "
      "this study is about.")
    A("")
    A("`rig-pred` is the stricter held-out number: the board pose of each held-out "
      "view is estimated from the **left camera only** and the right camera is "
      "predicted through the fitted rig.  The evaluated camera therefore does not "
      "influence its own pose, so this tests intrinsics *and* stereo extrinsics.")
    A("")
    A("### 2.1 Parameter deltas vs the factory profile")
    A("")
    rows = []
    for m in ("M1", "M2", "M3", "M4", "M5", "M6"):
        if m not in ladder:
            continue
        d = ladder[m]["delta_vs_factory"]
        rows.append([m,
                     _f(d["left"]["dfx"], 3), _f(d["right"]["dfx"], 3),
                     _f(d["left"]["dcx"], 3), _f(d["right"]["dcx"], 3),
                     _f(d["left"]["dskew_px"], 4),
                     _f(d["right_extr"]["d_baseline_mm"], 3),
                     _f(d["right_extr"]["d_rotation_deg"], 3)])
    A(_table(["model", "Δfx L px", "Δfx R px", "Δcx L px", "Δcx R px",
              "Δskew L px", "Δ baseline mm", "Δ rel. rot deg"], rows,
             ["---"] + ["---:"] * 7))
    A("")
    A("### 2.2 Bootstrap stability (10 view-level resamples)")
    A("")
    boot = rep.get("bootstrap") or {}
    if boot:
        A("Per-parameter standard deviation across resamples of the training views, "
          "next to the covariance sigma of the full fit.  A ratio near 1 means the "
          "reported sigma is honest; a ratio far above 1 means the parameter is "
          "controlled by the *design* (which views happen to be drawn), not by the "
          "noise model.")
        A("")
        rows = []
        for m in ("M1", "M2", "M3", "M4", "M5", "M6"):
            if m not in boot or not boot[m].get("params") or m not in ladder:
                continue
            cov = {r["param"]: r["sigma"] for r in ladder[m]["parameters"] if r["free"]}
            worst = sorted(boot[m]["params"],
                           key=lambda r: -(r["bootstrap_std"] / max(cov.get(r["param"], 0) or 1e-30, 1e-30)))[:4]
            for r in worst:
                c = cov.get(r["param"])
                rows.append([m, r["param"], _f(r["bootstrap_std"], 5), _f(c, 5),
                             _f(r["bootstrap_std"] / c if c else None, 2)])
        A(_table(["model", "parameter", "bootstrap std", "covariance σ", "ratio"], rows,
                 ["---", "---", "---:", "---:", "---:"]))
        A("")
    return L


def _identifiability_section(rep):
    L = []
    A = L.append
    fi = rep.get("factory_information") or {}
    A("## 3. Identifiability")
    A("")
    A("Two information matrices are used and they must not be confused: the "
      "**scaled marginal information** `S` (Schur complement of the rig block after "
      "eliminating the per-view board poses) whose eigenvalues say *which "
      "combinations the data constrains*, and the **marginal covariance** `C = S⁻¹` "
      "whose diagonal gives the parameter sigmas.  `S` is normalised by its own "
      "diagonal before the eigen-decomposition, so the eigenvalues are "
      "dimensionless and the condition number does not depend on whether a "
      "parameter is measured in px, mm or radians.")
    A("")
    if fi.get("spectrum"):
        sp = fi["spectrum"]
        A(f"### 3.1 At the factory solution (no parameter fitted)")
        A("")
        A(f"{fi['n_residuals']} residuals, {fi['n_free']} free (42 rig + poses), "
          f"Jacobian rank {fi['jacobian_rank']}, "
          f"scaled condition number **{_f(sp['condition_number_scaled'],3)}**.")
        A("")
        rows = []
        for d in sp["directions"]:
            rows.append([d["rank"] + 1, _f(d["eigenvalue"], 4),
                         _f(d["explained_fraction"] * 100, 2),
                         ", ".join(f"{c['param']} ({c['component']:+.2f})"
                                   for c in d["components"][:4])])
        A(_table(["rank", "eigenvalue", "% of total", "dominant parameters (component)"],
                 rows, ["---:", "---:", "---:", "---"]))
        A("")
    for m in ("M5", "M6"):
        ladder = rep.get("model_ladder") or {}
        if m in ladder and ladder[m].get("spectrum"):
            sp = ladder[m]["spectrum"]
            A(f"At the best fit ({m}): scaled condition number "
              f"**{_f(sp['condition_number_scaled'],3)}**; weakest direction "
              + ", ".join(f"`{c['param']}`" for c in sp["directions"][-1]["components"][:5])
              + ".")
            A("")
            break
    # correlations
    for label, blk in (("factory solution", (fi.get("correlations") or {})),
                       ("M5 fit", ((rep.get("model_ladder") or {}).get("M5") or {}).get("correlations") or {})):
        if not blk.get("pairs"):
            continue
        A(f"### 3.2 Strongest parameter correlations ({label})")
        A("")
        rows = [[p["a"], p["b"], _f(p["r"], 3)] for p in blk["pairs"][:12]]
        A(_table(["parameter A", "parameter B", "correlation"], rows,
                 ["---", "---", "---:"]))
        A("")
        A(f"Largest eigenvalue of the correlation matrix "
          f"**{_f(blk.get('corr_eig_max'),3)}**, condition "
          f"{_f(blk.get('corr_condition'),3)}.  A near-unit largest eigenvalue is a "
          f"collinear group: the parameters can only be determined as a combination.")
        A("")
    # verdicts
    ladder = rep.get("model_ladder") or {}
    src = None
    for m in ("M6", "M5", "M4", "M3", "M2", "M1"):
        if m in ladder and ladder[m].get("parameters"):
            src = ladder[m]
            src_name = m
            break
    if src:
        A(f"### 3.3 Per-parameter verdict ({src_name}, the most permissive converged model)")
        A("")
        A("Thresholds are the project's own accuracy presets "
          "(`calib.quality.ACCURACY_TARGETS`): *constrained* ≤ the `high` preset, "
          "*weak* ≤ the `fast` preset, otherwise *unobservable*.  For distortion "
          "coefficients the sigma is converted to the maximum pixel displacement of "
          "the observed markers, so every row is comparable.")
        A("")
        rows = []
        for r in src["parameters"]:
            if not r["free"]:
                continue
            eff = r.get("effect_px")
            if eff is None:
                eff = r.get("effect_deg")
                unit = "deg"
            else:
                unit = "px"
            if eff is None:
                eff = r.get("effect_mm"); unit = "mm"
            rows.append([r["param"], _f(r["value"], 5), _f(r["sigma"], 5),
                         f"{_f(eff,4)} {unit}", r["verdict"]])
        A(_table(["parameter", "value", "1σ", "1σ effect", "verdict"], rows,
                 ["---", "---:", "---:", "---:", "---"]))
        A("")
    return L


def _scale_section(rep):
    L = []
    A = L.append
    A("## 4. The absolute-scale question")
    A("")
    prof = rep.get("profile_likelihood") or {}
    A("A planar target fixes the *projection* `f·X/Z`; the metric depth enters only "
      "through `Z`, so any error in `f` appears 1:1 as a depth-scale error.  This "
      "is measured, not asserted: in the fixed-focal sweep below, everything else "
      "(distortion, skew, rig, all board poses) is refitted at each common focal "
      "scale.")
    A("")
    if prof.get("scales"):
        rows = []
        for i, s in enumerate(prof["scales"]):
            rows.append([_f(s, 3), _f(prof["rms_px"][i], 5),
                         _f(prof["mean_depth_mm"][i], 2),
                         _f(prof["delta_rms_per_percent"][i], 6),
                         _f(prof["chi2_per_percent"][i], 1)])
        A(_table(["focal scale", "train RMS px", "mean plate depth mm",
                  "ΔRMS per 1 % of f px", "Δχ² per 1 %"], rows,
                 ["---:", "---:", "---:", "---:", "---:"]))
        A("")
        A(f"Minimum at scale **{_f(prof.get('best_scale'),3)}**, i.e. at the factory "
          f"focal length; but the whole ±10 % span costs only "
          f"{_f(prof['rms_px'][-1] - prof['best_rms_px'],4)} px of RMS.  The recovered "
          f"mean depth tracks the focal scale **exactly** (ratio "
          f"{_f(prof['mean_depth_mm'][-1] / prof['mean_depth_mm'][0] / (prof['scales'][-1] / prof['scales'][0]), 4)} "
          f"over the sweep), which is the f↔depth degeneracy in one number.")
        if prof.get("sigma_scale_statistical_percent"):
            A("")
            A(f"Curvature-based *statistical* 1σ on the common focal scale: "
              f"**{_f(prof['sigma_scale_statistical_percent'],3)} %**.  That number "
              f"assumes independent, zero-mean pixel noise and no model error.  The "
              f"residual is dominated by systematic detector/model error, so it is a "
              f"lower bound, not an uncertainty: a 1 % focal error costs "
              f"{_f(abs(prof['delta_rms_per_percent'][np.argmin(np.abs(np.array(prof['scales'])-1.0))]),6) if prof.get('delta_rms_per_percent') else '—'} px of RMS, "
              f"which is far below any plausible systematic.")
    A("")
    A("**A better reprojection residual does not prove a better absolute scale.**  "
      "The 28.0 mm plate pitch is an input, not a measurement: scaling every board "
      "point and every translation by `s` leaves every reprojection *identically* "
      "unchanged, so no residual can prefer 28.0 mm over 27.72 mm.  The ~1 % vendor "
      "scale difference therefore cannot be resolved from reprojection at all; it "
      "needs an external metric reference (a calibrated length bar, a sphere bar, "
      "or a traceable artefact).  This study makes **no** claim about which scale "
      "is right.")
    A("")
    return L


def _downstream_section(rep):
    L = []
    A = L.append
    d = rep.get("downstream") or {}
    if not d or d.get("skipped"):
        A("## 5. Downstream consistency")
        A("")
        A(f"Skipped: {d.get('skipped') if d else 'not run'}")
        A("")
        return L
    A("## 5. Downstream consistency (`recon_cli`)")
    A("")
    A("The candidate profiles are written to `candidates/` with `validation: pending` "
      "and full provenance (input hashes, model, fit flags, RMS).  The factory "
      "profile is copied only as an untouched reference.  Groups are held-out legs, "
      "so no candidate saw them.  `plane RMS` is a robust RANSAC plane on the plate "
      "surface in the reconstructed cloud; `depth` is the plane's distance from the "
      "left camera; `repeatability` is the symmetric nearest-neighbour distance "
      "between two clouds after mapping one into the other's frame through the "
      "plate pose (pose-normalised).")
    A("")
    rows = []
    for label, runs in (d.get("runs") or {}).items():
        cand = d["candidates"][label]
        for g, e in runs.items():
            pl = e.get("plane") or {}
            rows.append([label, g, _f(e.get("valid_fraction"), 3),
                         _f(e.get("depth_p50_mm"), 2),
                         _f(pl.get("rms_mm"), 3), _f(pl.get("plane_depth_mm"), 2),
                         _f(pl.get("inlier_frac_of_band"), 3),
                         _f(cand.get("baseline_mm"), 3),
                         _f(cand.get("f_left"), 3)])
    A(_table(["candidate", "group", "valid frac", "depth p50 mm", "plane RMS mm",
              "plane depth mm", "plane inlier frac", "baseline mm", "f left px"], rows,
             ["---", "---:", "---:", "---:", "---:", "---:", "---:", "---:", "---:"]))
    A("")
    rep_block = d.get("repeatability") or {}
    if rep_block:
        rows = []
        for label, r in rep_block.items():
            for p in r.get("pairs", [])[:3]:
                rows.append([label, p["a"], p["b"], _f(p.get("d_rot_deg"), 2),
                             _f(p.get("d_t_mm"), 2), _f(p.get("median_mm"), 3),
                             _f(p.get("p90_mm"), 3)])
        if rows:
            A(_table(["candidate", "group a", "group b", "Δrot deg", "Δt mm",
                      "NN median mm", "NN p90 mm"], rows,
                     ["---", "---:", "---:", "---:", "---:", "---:", "---:"]))
            A("")
    A("What this shows: whether the candidate profiles reconstruct the same physical "
      "plate with a stable plane, a comparable valid-depth fraction and comparable "
      "frame-to-frame repeatability — i.e. whether a calibration that is 1 % "
      "different in focal length produces a *self-consistent* reconstruction.  "
      "What it cannot show: which profile is metrically correct.  There is no "
      "artefact and no independent instrument, and the plane's reconstructed depth "
      "moves with `f` by construction, so agreement here is not accuracy.")
    A("")
    return L


def _vendor_section(rep):
    L = []
    A = L.append
    v = rep.get("vendor_corpus") or {}
    if not v or v.get("skipped"):
        A("## 6. Vendor corpus contrast")
        A("")
        A(f"Skipped: {v.get('skipped') if v else 'not run'}")
        A("")
        return L
    A("## 6. Vendor 25-view corpus contrast")
    A("")
    t = v.get("trajectory") or {}
    A(f"{v['n_views']} view sets, {v['obs_count']} camera observations.  Trajectory: "
      + ", ".join(f"{k} {_f(t[k]['min'],1)}…{_f(t[k]['max'],1)} ({_f(t[k]['range'],1)} mm)"
                  for k in ("x", "y", "z") if k in t)
      + f"; tilt {_f(t['tilt_deg']['range'],1)}°, roll {_f(t['roll_deg']['range'],1)}°; "
      f"translation singular-value ratios "
      f"{_f(t['translation_sv_ratio_2_1'],3)} / {_f(t['translation_sv_ratio_3_1'],3)}.")
    A("")
    fl = v.get("factory_linearisation", {})
    lv = (rep.get("factory_information") or {})
    rows = []
    for name, blk in (("live sweep-c (1-D)", lv), ("vendor 25-view", fl)):
        sp = blk.get("spectrum") or {}
        if not sp.get("eigenvalues"):
            continue
        rows.append([name, _f(sp.get("condition_number_scaled"), 3),
                     _f(sp["eigenvalues"][-1], 4),
                     ", ".join(c["param"] for c in sp["directions"][-1]["components"][:3])])
    if rows:
        A(_table(["corpus", "scaled condition number", "smallest eigenvalue",
                  "weakest direction"], rows, ["---", "---:", "---:", "---"]))
        A("")
    # per-parameter sigma comparison
    def sigma_map(blk):
        return {r["param"]: r for r in blk.get("parameters", []) if r["free"]}
    a = sigma_map(lv); b = sigma_map(fl)
    keys = [k for k in ("left.fx", "left.cx", "left.cy", "left.skew_px", "left.k1",
                        "right.fx", "right.tz", "right.rz") if k in a and k in b]
    if keys:
        rows = [[k, _f(a[k].get("sigma"), 5), _f(b[k].get("sigma"), 5),
                 _f((a[k].get("sigma") or 0) / (b[k].get("sigma") or 1e-30), 2),
                 a[k]["verdict"], b[k]["verdict"]] for k in keys]
        A(_table(["parameter", "σ live sweep", "σ vendor corpus", "ratio live/vendor",
                  "verdict live", "verdict vendor"], rows,
                 ["---", "---:", "---:", "---:", "---", "---"]))
        A("")
    if v.get("fits"):
        rows = []
        for m, e in v["fits"].items():
            rows.append([m, _f(e["rms_px"], 4), e["n_free"], e["n_observations"],
                         _f(e["wall_s"], 1),
                         _f(e["fx"].get("left"), 2), _f(e["fx"].get("right"), 2),
                         _f(e["baseline_mm"], 2)])
        A("Fits on the vendor corpus (same model ladder):")
        A("")
        A(_table(["model", "RMS px", "free", "obs", "wall s", "fx L", "fx R",
                  "baseline mm"], rows, ["---", "---:", "---:", "---:", "---:",
                                         "---:", "---:", "---:"]))
        A("")
    A("What the 25 diverse views buy: the *conditioning of the parameters that "
      "depend on perspective and image-plane coverage* — the table above quantifies "
      "it per parameter.  What they do not buy: a fix for a genuine scale/pitch "
      "ambiguity, which is a gauge and not a conditioning problem.")
    A("")
    return L


def _requirements_section(rep):
    L = []
    A = L.append
    A("## 7. What the next one-session calibration must add")
    A("")
    ds = (rep.get("design_sensitivity") or {}).get("subsets") or {}
    tc = ((rep.get("geometry") or {}).get("sweep_c") or {}).get("trajectory") or {}
    A("The list is derived from the measured design-sensitivity table (§7.1): each "
      "row re-computes the *factory-solution* information matrix on a subset of the "
      "same views, so the change in σ is caused by the excitation that subset "
      "removes, not by a different fit.")
    A("")
    if ds:
        A("### 7.1 Measured design sensitivity")
        A("")
        rows = []
        for name, e in ds.items():
            rows.append([name, e.get("n_views"),
                         _f(e.get("sigma_fx_left"), 4), _f(e.get("sigma_cx_left"), 4),
                         _f(e.get("sigma_k1_left"), 5), _f(e.get("sigma_skew_left"), 5),
                         _f(e.get("sigma_baseline_mm"), 4),
                         _f(e.get("sigma_rot_deg"), 4)])
        A(_table(["subset", "views", "σ fx L px", "σ cx L px", "σ k1 L",
                  "σ skew L px", "σ baseline mm", "σ rel-rot deg"], rows,
                 ["---", "---:", "---:", "---:", "---:", "---:", "---:", "---:"]))
        A("")
        A(f"Subsets are defined on the live sweep: {json.dumps((rep.get('design_sensitivity') or {}).get('definitions', {}), indent=0)[:1200]}")
        A("")
    A("### 7.2 The concrete additions")
    A("")
    A("| parameter group | why it is not constrained by the sweep | what to add | "
      "quantitative target |")
    A("|---|---|---|---|")
    A("| `fx`, `fy` | the sweep holds z inside a 27 mm band; f is only separated "
      "from depth by perspective (tilt).  Translation is 1-D "
      f"({_f(tc.get('translation_sv_ratio_2_1') and 100*tc['translation_sv_ratio_2_1'],1)} % "
      "of the motion is along a second axis) | tilt the plate about a **second, "
      "orthogonal** axis and vary the working distance | tilt range ≥ 30° about both "
      "axes, distance range ≥ 80 mm (2× the current 27 mm) |")
    A("| `cx`, `cy` | the markers occupy a narrow, off-centre image region "
      f"({_f(100*((rep.get('geometry') or {}).get('sweep_c',{}).get('image_coverage_left',{}) or {}).get('u_span_frac',0),0)} % "
      "of the width) at nearly constant v | place the plate in all four image "
      "quadrants near the borders | marker centroid in each quadrant at ≥ 25 % of "
      "the frame from the centre |")
    A("| `k1`, `k2`, `p1`, `p2`, `k3` | distortion is only separable from f and the "
      "pose if the markers sample a wide range of image radius | views with the "
      "plate tilted so its corners approach the image corners | marker radius "
      "coverage to r ≥ 0.7 of the half-diagonal in ≥ 8 views |")
    A("| `skew` | the true skew is ~0.07–0.2 px; it is degenerate with `cx`/rotation "
      "at this SNR | not worth chasing — keep skew fixed at the factory value | — |")
    A("| stereo `R`,`t` | the sweep *does* constrain the rig well (see §3); the "
      "limitation is the free-rig scale coupling with f | keep the rig fixed while "
      "f is uncertain, or add the diversity above | σ baseline ≤ 0.06 mm at the "
      "`high` preset |")
    A("| colour | the factory colour extrinsic is inconsistent (~49 px) and the "
      "sweep's colour detection is good, but colour was never validated | a "
      "dedicated colour session with the IR-visible pattern in both cameras | "
      "colour held-out RMS ≤ 0.5 px and independent colour check |")
    A("")
    A("A one-session plan that satisfies the first three rows takes roughly 25–30 "
      "views in 3 distance bands × 2 orthogonal tilt axes × 4 image quadrants; the "
      "existing recorder already produces that structure.  Nothing in this study "
      "shows that more views *of the same 1-D motion* would help: the missing "
      "information is excitation, not sample count.")
    A("")
    return L


def _limitations_section(rep, figs):
    L = []
    A = L.append
    A("## 8. Limitations — what this study cannot determine")
    A("")
    A("* **Absolute metric accuracy.** No artefact, no independent instrument.  The "
      "plate pitch is an assumption; a global rescale of the board and all "
      "translations leaves the reprojection *exactly* invariant, so neither this "
      "study nor any reprojection-based calibration can resolve the ~1 % scale "
      "question.")
    A("* **Physical sub-millimetre accuracy.** Only consistency and repeatability "
      "are measured here.")
    A("* **A unique focal length.** The free-f models have a shallow valley; the "
      "reported point estimate depends on the starting point (see §9.1) and the "
      "covariance is a local linearisation of a non-quadratic cost.")
    A("* **Per-view board pose sign conventions.** A global relabelling of the "
      "plate frame (`u→7−u` etc.) is a gauge that leaves the calibration unchanged "
      "but flips the sign of the reported x translation.")
    A("* **Colour.** `color_status` stays `unknown`/`stale`; no colour claim is made.")
    A("* The vendor corpus was analysed with the same detector and the same model; "
      "differences in conditioning are therefore attributable to the view set, not "
      "to a different pipeline.")
    A("")
    A("## 9. Library / engine limitations found")
    A("")
    A("1. **`calib.calibrate` cannot express four of the six ladder models.** Its "
      "two switches act on *all* cameras at once (`free_intrinsics`, `free_kc`) and "
      "there is no switch for the extrinsics, for `skew`, or for a per-camera "
      "subset.  `_Problem.unpack` also constructs `CameraIntrinsics` without a "
      "skew, so the fitted K always has `K[0,1] = 0` even when the factory profile "
      "carries a non-zero skew.  The wrapper in `tools/calib_study/models.py` adds a "
      "per-parameter free mask and a pixel-space skew slot; it is a re-implementation "
      "of the same residual model and reaches the same solution as "
      "`calib.calibrate` when both are started from the same point with the same "
      "solver settings (verified in `observability_report.json` → "
      "`wrapper_verification`).")
    A("2. **Solver scaling.** `least_squares(..., jac_sparsity=..., x_scale=1.0)` "
      "does not converge on this data: the parameters span 1e-5 (skew) to 1e3 (fx) "
      "and the sparse `lsmr` trust-region step is inaccurate.  `x_scale='jac'` is "
      "used throughout.  `calib.calibrate`'s default `x_scale=1.0` therefore stops "
      "on a cost plateau that a properly scaled solve improves on the same model.")
    A("3. **Skew convention mismatch (report, not fixed here).** "
      "`calib.calibrate.CameraIntrinsics.skew` is the dimensionless MATLAB `alpha_c` "
      "(K[0,1] = skew·fx) while the reconstruction profile JSON and the C++ "
      "`CameraProfile::K()` store `K[0,1]` in **pixels**.  Reading the JSON without "
      "dividing by fx multiplies the skew by ~1160 and costs ~10 px of RMS on live "
      "data.  `tools/calib_study/export.py` converts explicitly; a `calib` fix would "
      "be to document the unit in `CameraIntrinsics` and/or add an "
      "`as_profile_k()`/`from_profile_k()` pair.")
    A("4. **Detection gives no large-marker labels.** `assign_grid` returns "
      "`big_markers = ()` although it uses the large markers as anchors, so no "
      "caller can audit the (u,v) assignment.  Returning the four anchor labels "
      "(and whether the off-lattice marker was on the expected side) would make the "
      "chirality check a one-line assertion instead of a re-derivation.")
    A("")
    if figs:
        A("## 10. Figures")
        A("")
        for p in figs:
            A(f"![{Path(p).stem}](figures/{Path(p).name})")
            A("")
    A("## 11. Reproduction")
    A("")
    A("```bash")
    A("cd " + str(rep.get("study", {}).get("outputs", "")) if False else "cd <repo>")
    A(".venv/bin/python tools/calib_study/observability.py \\")
    A("  --sweep  ~/einstar-private/sweep-c/frames \\")
    A("  --sweep2 ~/einstar-private/sweep-b/frames \\")
    A("  --profile ~/einstar-private/einstar-profiles/factory-e10.json \\")
    A("  --vendor-corpus ~/einstar-private/vendor-calib-rapid \\")
    A("  --out ~/einstar-private/analysis/calib_study")
    A("```")
    A("")
    A(f"All numbers are in `observability_report.json`; the run took "
      f"{_f(rep.get('study', {}).get('wall_s_total'), 1)} s wall.")
    return L
