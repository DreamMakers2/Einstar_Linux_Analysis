"""Figures for a sweep analysis (matplotlib, Agg backend only)."""

from __future__ import annotations

import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402



def _save(fig, path):
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)


def _leg_spans(ax, legs, color="#eeeeee"):
    for k, (a, b, _d) in enumerate(legs):
        ax.axvspan(a - 0.5, b + 0.5, color=color if k % 2 == 0 else "#f7f7f7",
                   zorder=0, lw=0)


def _turnarounds(ax, turn):
    for t in turn:
        ax.axvline(t["index"], color="#c0392b", lw=1.0, ls="--", zorder=1)


LEG_COLORS = ["#2471a3", "#c0392b", "#117a65", "#8e44ad", "#b9770e", "#2c3e50"]


def trajectory(ctx, path):
    f = ctx["frames"]
    legs = ctx["legs"]
    turn = ctx["turnarounds"]
    if not f:
        return
    t = np.array([r["t"] for r in f], float)
    fig, ax = plt.subplots(figsize=(7.4, 5.4))
    for k, (a, b, d) in enumerate(legs):
        c = LEG_COLORS[k % len(LEG_COLORS)]
        ax.plot(t[a:b + 1, 0], t[a:b + 1, 2], "-", lw=1.4, color=c, alpha=0.9,
                label=f"leg {k} ({d})", zorder=2)
        ax.plot(t[a:b + 1, 0], t[a:b + 1, 2], ".", ms=3, color=c, zorder=3)
        # direction arrows: one third and two thirds along the leg
        for frac in (0.33, 0.66):
            i = a + int(frac * (b - a))
            j = min(i + max(1, (b - a) // 25), b)
            ax.annotate("", xy=(t[j, 0], t[j, 2]), xytext=(t[i, 0], t[i, 2]),
                        arrowprops=dict(arrowstyle="->", color=c, lw=1.8),
                        zorder=5)
    y_lo, y_hi = float(t[:, 2].min()), float(t[:, 2].max())
    for ta in turn:
        i = ta["index"]
        ax.plot(t[i, 0], t[i, 2], "o", ms=10, mfc="none", mec="#111111", mew=1.8,
                zorder=6)
        frac = (t[i, 2] - y_lo) / max(y_hi - y_lo, 1e-9)
        off = (12, -16) if frac > 0.7 else (12, 10)
        ax.annotate(f"{ta['kind']} @ g{ta['group']}", (t[i, 0], t[i, 2]),
                    textcoords="offset points", xytext=off, fontsize=8,
                    color="#111111")
    ax.set_xlabel("board origin x in left IR camera [mm]")
    ax.set_ylabel("board origin z (depth) [mm]")
    ax.set_title("plate trajectory, x vs z (colour = leg, arrows = direction)")
    ax.margins(0.05)
    ax.legend(fontsize=8, loc="best")
    ax.grid(alpha=0.3)
    _save(fig, os.path.join(path, "trajectory.png"))


def pose_vs_time(ctx, path):
    f = ctx["frames"]
    if not f:
        return
    g = np.array([r["group"] for r in f])
    t = np.array([r["t"] for r in f], float)
    fig, axes = plt.subplots(4, 1, figsize=(9.0, 8.4), sharex=True)
    series = [("x [mm]", t[:, 0]), ("z (depth) [mm]", t[:, 2]),
              ("plate tilt [deg]", np.array([r["tilt_deg"] for r in f])),
              ("in-plane rotation, rel. to median [deg]",
               np.array([r["roll_rel_deg"] for r in f]))]
    for ax, (label, y) in zip(axes, series):
        _leg_spans(ax, ctx["legs"])
        _turnarounds(ax, ctx["turnarounds"])
        ax.plot(g, y, "-", lw=1.0, color="#2c3e50")
        ax.set_ylabel(label, fontsize=8)
        ax.grid(alpha=0.3)
        for k, (a, b, d) in enumerate(ctx["legs"]):
            ax.annotate(f"leg {k} {d}", xy=(0.012, 0.03 + 0.055 * k),
                        xycoords="axes fraction", fontsize=7,
                        color="#34495e", va="bottom", ha="left")
    axes[-1].set_xlabel("group")
    axes[0].set_title("plate pose vs time (dashed = turnaround)")
    _save(fig, os.path.join(path, "pose_vs_time.png"))


def quality_vs_time(ctx, path):
    f = ctx["records"]
    if not f:
        return
    g = np.array([r["group"] for r in f])
    cams = ctx["cams"]
    fig, axes = plt.subplots(4, 1, figsize=(9.0, 8.4), sharex=True)
    keys = [("sat_frac", "saturation fraction (>=250)"),
            ("contrast", "contrast p99-p1"),
            ("blur_lapvar_norm", "Laplacian var / intensity var"),
            ("marker_sharp_px_inv", "marker edge sharpness [1/px]")]
    colors = {"0": "#c0392b", "1": "#2471a3", "2": "#117a65"}
    for ax, (key, label) in zip(axes, keys):
        for cam in cams:
            y = [r["quality"][cam].get(key) if r["quality"].get(cam) else None for r in f]
            y = np.array([np.nan if v is None else v for v in y], float)
            ax.plot(g, y, lw=1.0, color=colors.get(cam, "k"), label=f"stream {cam}")
        ax.set_ylabel(label, fontsize=8)
        ax.grid(alpha=0.3)
        if key in ("sat_frac", "marker_sharp_px_inv"):
            ax.legend(fontsize=7)
    axes[-1].set_xlabel("group")
    axes[0].set_title("image quality vs time")
    _save(fig, os.path.join(path, "quality_vs_time.png"))


def repeatability(ctx, path):
    rep = ctx["repeatability"]
    fig, axes = plt.subplots(1, 2, figsize=(9.4, 4.0))
    ax = axes[0]
    if rep.get("revisit_dt_mm"):
        ax.hist(rep["revisit_dt_mm"], bins=40, color="#2471a3", alpha=0.8,
                label="cross-leg revisit")
    if rep.get("adjacent_dt_mm"):
        ax.hist(rep["adjacent_dt_mm"], bins=40, color="#bdc3c7", alpha=0.7,
                label="adjacent frames")
    ax.set_xlabel("translation difference |dt| [mm]")
    ax.set_ylabel("count")
    ax.legend(fontsize=7)
    ax.set_title("translation")
    ax = axes[1]
    if rep.get("revisit_drot_deg"):
        ax.hist(rep["revisit_drot_deg"], bins=40, color="#2471a3", alpha=0.8,
                label="cross-leg revisit")
    if rep.get("adjacent_drot_deg"):
        ax.hist(rep["adjacent_drot_deg"], bins=40, color="#bdc3c7", alpha=0.7,
                label="adjacent frames")
    ax.set_xlabel("rotation difference [deg]")
    ax.set_ylabel("count")
    ax.legend(fontsize=7)
    ax.set_title("rotation")
    nf = rep.get("noise_floor") or {}
    if nf.get("ok"):
        fig.suptitle(
            f"repeatability; pose noise floor {nf['t_mm']['norm']:.3f} mm / "
            f"{nf['rot_deg']['norm']:.4f} deg (local quadratic detrend)", fontsize=9)
    _save(fig, os.path.join(path, "repeatability.png"))


def image_coverage(ctx, path):
    f = ctx["frames"]
    recs = ctx["records"]
    if not f:
        return
    W, H = ctx["image_size"]
    cams = [c for c in ctx["cams"] if c in ("0", "1")]
    fig, axes = plt.subplots(1, len(cams), figsize=(6.0 * len(cams), 4.6),
                             squeeze=False)
    for ax, cam in zip(axes[0], cams):
        raw = [recs[r["index"]] for r in f
               if (recs[r["index"]]["detect"].get(cam) or {}).get("pixels")]
        pts = (np.vstack([np.asarray(r["detect"][cam]["pixels"], float) for r in raw])
               if raw else np.zeros((0, 2)))
        if len(pts):
            ax.hist2d(pts[:, 0], pts[:, 1], bins=[96, 77],
                      range=[[0, W], [0, H]], cmap="magma", cmin=1)
            try:
                from scipy.spatial import ConvexHull
                hull = ConvexHull(pts)
                hp = pts[np.append(hull.vertices, hull.vertices[0])]
                ax.plot(hp[:, 0], hp[:, 1], "-", color="#7dffb0", lw=1.2,
                        label="convex hull of all markers")
            except Exception:
                pass
        uc = np.array([r["u_c"] for r in f], float)
        vc = np.array([r["v_c"] for r in f], float)
        ax.plot(uc, vc, "-", color="#00d1ff", lw=1.0, alpha=0.9,
                label="plate centre over sweep")
        ax.plot(uc[0], vc[0], "o", color="#00ff88", ms=5, label="first frame")
        ax.plot(uc[-1], vc[-1], "s", color="#ff5555", ms=4, label="last frame")
        ax.set_xlim(0, W)
        ax.set_ylim(H, 0)
        ax.set_xlabel("u [px]")
        ax.set_ylabel("v [px]")
        ax.set_title(f"stream {cam} ({ctx['cam_roles'][cam]}), all detected markers")
        ax.legend(fontsize=7, loc="upper right")
    _save(fig, os.path.join(path, "image_coverage.png"))


def residuals(ctx, path):
    f = ctx["records"]
    if not f:
        return
    g = np.array([r["group"] for r in f])
    fig, ax = plt.subplots(figsize=(9.0, 4.0))
    colors = {"0": "#c0392b", "1": "#2471a3"}
    for cam in ctx["cams"]:
        y = np.array([np.nan if not r["pose"].get(cam) else r["pose"][cam]["rms_px"]
                      for r in f], float)
        ax.plot(g, y, lw=1.0, color=colors.get(cam, "k"),
                label=f"stream {cam} PnP RMS")
        ax.axhline(np.nanmedian(y), color=colors.get(cam, "k"), ls=":", lw=1.0)
        yh = np.array([np.nan if not r["detect"].get(cam) or
                       r["detect"][cam].get("rms") is None
                       else r["detect"][cam]["rms"] for r in f], float)
        ax.plot(g, yh, lw=0.7, ls="--", color=colors.get(cam, "k"), alpha=0.6,
                label=f"stream {cam} homography RMS")
    ax.set_xlabel("group")
    ax.set_ylabel("reprojection RMS [px]")
    ax.set_title("plate reprojection residual per frame (dotted = median)")
    ax.legend(fontsize=7, ncol=2)
    ax.grid(alpha=0.3)
    _save(fig, os.path.join(path, "residuals.png"))
