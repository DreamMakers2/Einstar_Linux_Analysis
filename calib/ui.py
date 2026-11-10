"""Visual feedback prototype for the guided calibration session.

The live product would draw these panels over the camera stream.  This module
renders the same information without a display (matplotlib Agg), so it can be
used in headless tests and embedded in reports, and it can optionally show a
window when a display is available.

Panels
------
1. marker overlay on the current left frame (or the last accepted one);
2. image-plane coverage heat maps for the three cameras;
3. accepted board poses in distance/tilt space + the recommended next pose;
4. 3-D uncertainty vs depth (median and p95) with the expected after-capture
   curve;
5. a text panel with the next instruction, its predicted benefit, the reasons
   for the last rejection and the stopping status.
"""

from __future__ import annotations

import os
from typing import Optional

import numpy as np

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import Circle  # noqa: E402

from . import guidance, objective  # noqa: E402
from .calibrate import CAMERAS  # noqa: E402


def _sigma_profile(result, points, cache=None):
    rep = objective.evaluate_task(result, "A", points, cache=cache)
    z = points[:, 2]
    order = np.argsort(z)
    z = z[order]
    sig = rep.sigma_mm[order]
    # median per depth slice
    zs = np.unique(np.round(z, 3))
    med = np.array([np.median(sig[z == zz]) for zz in zs])
    p95 = np.array([np.percentile(sig[z == zz], 95) for zz in zs])
    return zs, med, p95


def dashboard(session, path: str, frame: Optional[np.ndarray] = None,
              grids: Optional[dict] = None, recommendation: Optional[dict] = None,
              last_rejection: Optional[dict] = None) -> str:
    """Render one dashboard frame to ``path`` (PNG)."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    rec = recommendation if recommendation is not None else session.recommend()
    st = session.status()
    fig = plt.figure(figsize=(16, 10), dpi=110)
    gs = fig.add_gridspec(3, 3, height_ratios=[1.25, 1.0, 0.85], hspace=0.35, wspace=0.25)

    # 1. marker overlay
    ax = fig.add_subplot(gs[0, 0])
    if frame is not None:
        ax.imshow(frame, cmap="gray")
        if grids is not None:
            o = grids.get("left")
            if o is not None:
                if isinstance(o.uv, dict):
                    items = o.uv.items()
                else:
                    items = zip([tuple(x) for x in o.uv], o.pixels)
                for (u, v), uv in items:
                    big = (float(u), float(v)) in {(2.0, 1.0), (4.0, 1.0), (5.0, 1.0), (3.5, 3.0)}
                    ax.plot(uv[0], uv[1], "o", ms=7 if big else 3,
                            mfc="none", mec="#ff3b30" if big else "#34c759", mew=1.0)
        ax.set_title("detected target (red = large markers)")
    else:
        ax.text(0.5, 0.5, "no frame", ha="center", va="center")
        ax.set_title("detected target")
    ax.set_xticks([]); ax.set_yticks([])

    # 2. coverage
    ax = fig.add_subplot(gs[0, 1])
    cov = st.get("coverage", {})
    if cov:
        grid = np.zeros((4, 4))
        for c in CAMERAS:
            if c in cov:
                grid += np.asarray(cov[c]["occupancy"], float)
        ax.imshow(grid, cmap="magma")
        for c in CAMERAS:
            if c in cov:
                ax.text(0.02, 0.05 + 0.12 * CAMERAS.index(c),
                        f"{c}: {100 * cov[c]['fraction']:.0f}% cells", color="w",
                        transform=ax.transAxes, fontsize=8)
    ax.set_title("image-plane coverage (all cameras)")
    ax.set_xticks([]); ax.set_yticks([])

    # 3. pose coverage
    ax = fig.add_subplot(gs[0, 2])
    poses = st.get("accepted", [])
    if poses:
        d = [p["distance_mm"] for p in poses]
        t = [p["tilt_deg"] for p in poses]
        ax.scatter(d, t, s=45, c="#0a84ff", label="accepted", zorder=3)
    if rec.get("target"):
        tg = rec["target"]
        ax.scatter([tg.get("distance_mm", np.nan)], [abs(tg.get("tilt_x_deg", 0)) + abs(tg.get("tilt_y_deg", 0))],
                   s=140, marker="*", c="#ff9f0a", label="next", zorder=4)
    ax.axhline(0, color="k", lw=0.5)
    ax.set_xlabel("distance (mm)"); ax.set_ylabel("tilt (deg)")
    ax.set_title("captured poses")
    ax.legend(loc="upper right", fontsize=8)

    # 4. uncertainty vs depth
    ax = fig.add_subplot(gs[1, :2])
    if session.result is not None:
        pts = objective.measurement_volume(n_z=5, n_xy=3)
        zs, med, p95 = _sigma_profile(session.result, pts)
        ax.plot(zs, med, "-o", label="median 3-D sigma", color="#0a84ff")
        ax.plot(zs, p95, "--", label="p95", color="#ff453a")
        ax.set_xlabel("depth (mm)"); ax.set_ylabel("sigma (mm)")
        ax.set_title("predicted 3-D reconstruction uncertainty over the volume")
        ax.grid(alpha=0.3); ax.legend(fontsize=8)
    else:
        ax.text(0.5, 0.5, "bootstrap: collecting diverse views",
                ha="center", va="center", transform=ax.transAxes)
        ax.set_title("uncertainty")

    # 5. text panel
    ax = fig.add_subplot(gs[1, 2]); ax.axis("off")
    lines = [f"mode: {session.mode}", f"accepted: {len(session.accepted)}  rejected: {len(session.rejected)}"]
    if session.result is not None:
        lines += [f"baseline: {st.get('baseline_mm', 0):.2f} mm",
                  f"RMS: {st.get('rms_px', 0):.3f} px",
                  f"median sigma: {np.median(st.get('task', {}).get('sigma_mm', [0])):.3f} mm",
                  f"solve time: {session.solve_seconds:.1f} s"]
    ax.text(0.0, 1.0, "\n".join(lines), va="top", fontsize=9, family="monospace")

    # 6. instruction
    ax = fig.add_subplot(gs[2, :]); ax.axis("off")
    instr = rec.get("text", "")
    expl = rec.get("explanation", [])
    txt = [f">> {instr}"] if instr else [">> hold still"]
    if expl:
        txt.append("   " + "; ".join(expl[:3]))
    lr = last_rejection or st.get("last_rejection")
    if lr:
        txt.append("   rejected: " + "; ".join(lr.get("reasons", [])[:3]))
    stop = session.stop_decision()
    txt.append(("   stop: READY  " if stop["stop"] else "   stop: continue  ")
               + (stop.get("reason", "")))
    ax.text(0.0, 0.95, "\n".join(txt), va="top", fontsize=10, family="monospace",
            color="#1c1c1e")

    fig.suptitle("Einstar adaptive calibration — live guidance prototype", fontsize=13)
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)
    return path


def contact_sheet(paths: list[str], out: str, cols: int = 2) -> str:
    """Combine dashboards into one HTML page (no external assets)."""
    cells = "".join(
        f'<figure><img src="{os.path.basename(p)}" style="width:100%">'
        f'<figcaption>{os.path.basename(p)}</figcaption></figure>' for p in paths)
    html = ("<!doctype html><meta charset='utf-8'><title>Einstar calibration "
            "session</title><style>body{font-family:system-ui;margin:2em;background:#111;"
            "color:#eee}figure{display:inline-block;width:46%;margin:1%;}"
            "img{border-radius:8px}</style><h1>Einstar adaptive calibration</h1>"
            f"<p>{len(paths)} dashboard frames</p>{cells}")
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    return out
