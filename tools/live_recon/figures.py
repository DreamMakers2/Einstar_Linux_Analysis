"""Figures (matplotlib Agg only; no display, no GPU)."""

from __future__ import annotations

import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

from . import common  # noqa: E402

CMAP = "viridis"


def _save(fig, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fig.savefig(path, dpi=130, bbox_inches="tight")
    plt.close(fig)
    return path


def _groups_ok(records):
    return [r for r in records if r.get("ok")]


def fig_valid_fraction(records, path, old_capture_pct=None):
    ok = _groups_ok(records)
    if not ok:
        return None
    g = np.array([r["group"] for r in ok])
    vf = np.array([r["depth"]["valid_fraction"] * 100 for r in ok])
    plate = np.array([r["plate"]["image"].get("plate_valid_fraction", np.nan) * 100
                      if r.get("plate", {}).get("image", {}).get("ok") else np.nan
                      for r in ok], dtype=float)
    fig, ax = plt.subplots(figsize=(10, 4.2))
    ax.plot(g, vf, lw=0.8, color="tab:blue", label="whole frame (depth valid)")
    ax.plot(g, plate, lw=0.8, color="tab:green", alpha=0.8,
            label="inside the plate outline")
    if old_capture_pct is not None:
        ax.axhline(old_capture_pct, color="tab:red", ls="--", lw=1,
                   label=f"previous capture (Nov): {old_capture_pct:.1f} %")
    ax.set_xlabel("group (10 Hz trigger)")
    ax.set_ylabel("valid depth pixels [% of area]")
    ax.set_title("Valid-depth fraction over the live sweep (recon_cli, scale 0.5)")
    ax.grid(alpha=0.3)
    ax.legend(loc="lower right", fontsize=8)
    return _save(fig, path)


def fig_depth_quality(records, path, old_depth_p50=None):
    ok = [r for r in _groups_ok(records) if r.get("pose")]
    if not ok:
        return None
    g = np.array([r["group"] for r in ok])
    p05 = np.array([r["depth"]["p05_mm"] for r in ok])
    p50 = np.array([r["depth"]["p50_mm"] for r in ok])
    p95 = np.array([r["depth"]["p95_mm"] for r in ok])
    rng = np.array([r["pose"]["range_mm"] for r in ok])
    fig, axes = plt.subplots(2, 1, figsize=(10, 6.4), sharex=True)
    ax = axes[0]
    ax.fill_between(g, p05, p95, color="tab:blue", alpha=0.25, label="all points p05-p95")
    ax.plot(g, p50, lw=0.8, color="tab:blue", label="all points p50")
    ax.plot(g, rng, lw=0.8, color="tab:orange", label="plate PnP range")
    if old_depth_p50 is not None:
        ax.axhline(old_depth_p50, color="tab:red", ls="--", lw=1,
                   label=f"previous capture p50 {old_depth_p50:.1f} mm")
    ax.set_ylabel("depth [mm]")
    ax.set_title("Depth distribution and plate range over the sweep")
    ax.grid(alpha=0.3)
    ax.legend(fontsize=8)
    ax = axes[1]
    bias = np.array([r["plate"]["surface"].get("depth_bias_median_mm", np.nan)
                     if r.get("plate", {}).get("surface", {}).get("ok") else np.nan
                     for r in ok], dtype=float)
    ax.plot(g, bias, lw=0.8, color="tab:purple",
            label="depth bias vs PnP plane (median)")
    ax.axhline(0, color="k", lw=0.6)
    ax.set_xlabel("group")
    ax.set_ylabel("bias [mm]")
    ax.grid(alpha=0.3)
    ax.legend(fontsize=8)
    return _save(fig, path)


def fig_plane_rms(records, path):
    ok = [r for r in _groups_ok(records)
          if r.get("plate", {}).get("surface", {}).get("ok")]
    if not ok:
        return None
    g = np.array([r["group"] for r in ok])
    rms = np.array([r["plate"]["surface"]["plane_rms_mm"] for r in ok])
    tilt = np.array([r["plate"]["surface"]["plane_tilt_deg"] for r in ok])
    rng = np.array([r["pose"]["range_mm"] for r in ok])
    fig, axes = plt.subplots(1, 3, figsize=(13, 3.8))
    ax = axes[0]
    ax.plot(g, rms, lw=0.8, color="tab:red")
    ax.set_xlabel("group")
    ax.set_ylabel("plane RMS [mm]")
    ax.set_title("Plate planarity (fit, thickness)")
    ax.grid(alpha=0.3)
    ax = axes[1]
    ax.scatter(rng, rms, s=4, alpha=0.4, color="tab:red")
    ax.set_xlabel("plate range [mm]")
    ax.set_ylabel("plane RMS [mm]")
    ax.set_title("Planarity vs range")
    ax.grid(alpha=0.3)
    ax = axes[2]
    ax.plot(g, tilt, lw=0.8, color="tab:brown")
    ax.set_xlabel("group")
    ax.set_ylabel("normal angle vs PnP [deg]")
    ax.set_title("Surface tilt relative to the PnP plane")
    ax.grid(alpha=0.3)
    fig.tight_layout()
    return _save(fig, path)


def fig_repeatability(rep, path):
    fig, axes = plt.subplots(1, 3, figsize=(13, 3.9))
    any_data = False
    for key, color in (("similar", "tab:blue"), ("different_angle", "tab:orange")):
        rows = rep.get(key, {}).get("pairs", [])
        if not rows:
            continue
        any_data = True
        dR = np.array([r["dR_deg"] for r in rows])
        nn = np.array([r["nn_p50_mm"] for r in rows])
        axes[0].scatter(dR, nn, s=12, alpha=0.6, color=color,
                        label=f"{key} (n={len(rows)})")
        axes[1].hist(np.array([r["nn_p95_mm"] for r in rows]), bins=30, alpha=0.5,
                     color=color, label=f"{key} p95")
    axes[0].set_xlabel("plate pose difference [deg]")
    axes[0].set_ylabel("NN distance p50 [mm]")
    axes[0].set_title("Repeatability vs pose difference")
    axes[0].legend(fontsize=7)
    axes[0].grid(alpha=0.3)
    axes[1].set_xlabel("NN distance p95 [mm]")
    axes[1].set_ylabel("pairs")
    axes[1].set_title("Pairwise NN distance (p95)")
    axes[1].legend(fontsize=7)
    axes[1].grid(alpha=0.3)
    rows = rep.get("similar", {}).get("pairs", [])
    if rows:
        dz = np.array([r["dz_mm"] for r in rows])
        axes[2].hist(dz, bins=30, color="tab:purple", alpha=0.8)
        axes[2].axvline(0, color="k", lw=0.8)
        axes[2].set_xlabel("plate-frame z difference [mm]")
        axes[2].set_ylabel("pairs")
        axes[2].set_title("Depth-bias consistency at similar pose")
    axes[2].grid(alpha=0.3)
    fig.tight_layout()
    if not any_data:
        plt.close(fig)
        return None
    return _save(fig, path)


def fig_cloud_plate(points_plate, path, title):
    p = np.asarray(points_plate, dtype=np.float64)
    if len(p) < 100:
        return None
    fig = plt.figure(figsize=(12, 4.6))
    ax = fig.add_subplot(1, 2, 1)
    z0 = float(np.median(p[:, 2]))
    # residual about the *fitted* plane: this is the same quantity as
    # ``plane_rms_mm`` in the report (a plane fit removes the PnP-vs-surface
    # tilt, which would otherwise dominate the colour scale).
    fit = common.fit_plane(p)
    if fit is not None:
        centroid, normal, _ = fit
        resid = (p - centroid) @ normal
    else:
        resid = p[:, 2] - z0
    vmax = max(0.1, float(np.percentile(np.abs(resid), 99)))
    sc = ax.scatter(p[:, 0], p[:, 1], c=resid, s=1.2, cmap="coolwarm",
                    vmin=-vmax, vmax=vmax)
    ax.set_aspect("equal")
    ax.set_xlabel("plate x [mm]")
    ax.set_ylabel("plate y [mm]")
    ax.set_title(f"Plate-frame surface, residual about the fitted plane\n"
                 f"(offset = {z0:.2f} mm, plane RMS = {np.sqrt(np.mean(resid**2)):.3f} mm, "
                 f"p95 |resid| = {np.percentile(np.abs(resid), 95):.3f} mm)")
    fig.colorbar(sc, ax=ax, label="point-to-plane residual [mm]")
    ax = fig.add_subplot(1, 2, 2, projection="3d")
    sub = p[:: max(1, len(p) // 12000)]
    sub_res = resid[:: max(1, len(p) // 12000)][:len(sub)]
    ax.scatter(sub[:, 0], sub[:, 1], sub_res,
               c=sub_res, s=1.0, cmap="coolwarm", vmin=-vmax, vmax=vmax)
    ax.set_xlabel("x [mm]")
    ax.set_ylabel("y [mm]")
    ax.set_zlabel("residual [mm]")
    ax.set_title("Point cloud (plate frame)")
    ax.view_init(elev=28, azim=-60)
    fig.suptitle(title, fontsize=9)
    fig.tight_layout()
    return _save(fig, path)


def fig_example_maps(group_dir, prefix, path, crop=None):
    import cv2
    d = os.path.join(group_dir, prefix + "_depth_mm.tiff")
    c = os.path.join(group_dir, prefix + "_confidence.tiff")
    s = os.path.join(group_dir, prefix + "_disparity.tiff")
    if not (os.path.exists(d) and os.path.exists(c) and os.path.exists(s)):
        return None
    depth = cv2.imread(d, cv2.IMREAD_UNCHANGED)
    conf = cv2.imread(c, cv2.IMREAD_UNCHANGED)
    disp = cv2.imread(s, cv2.IMREAD_UNCHANGED)
    rl = cv2.imread(os.path.join(group_dir, prefix + "_rectified_left.png"),
                    cv2.IMREAD_UNCHANGED)
    rr = cv2.imread(os.path.join(group_dir, prefix + "_rectified_right.png"),
                    cv2.IMREAD_UNCHANGED)
    if crop is None:
        valid = depth > 0
        ys, xs = np.nonzero(valid)
        if len(xs) == 0:
            return None
        pad = 40
        x0, x1 = max(0, xs.min() - pad), min(depth.shape[1], xs.max() + pad)
        y0, y1 = max(0, ys.min() - pad), min(depth.shape[0], ys.max() + pad)
        crop = (x0, y0, x1, y1)
    x0, y0, x1, y1 = crop

    def prep(img, vmin=None, vmax=None, cmap=CMAP):
        if img is None:
            return np.zeros((10, 10, 3), np.uint8)
        a = img.astype(np.float32)
        a = a[y0:y1, x0:x1]
        m = np.isfinite(a)
        if vmin is None:
            vmin = float(np.percentile(a[m], 2)) if m.any() else 0
        if vmax is None:
            vmax = float(np.percentile(a[m], 98)) if m.any() else 1
        if vmax <= vmin:
            vmax = vmin + 1
        g = np.clip((a - vmin) / (vmax - vmin), 0, 1)
        g = np.nan_to_num(g, nan=0.0)
        col = (plt.get_cmap(cmap)(g)[..., :3] * 255).astype(np.uint8)
        return col

    panels = [
        ("rectified left", cv2.cvtColor(rl[y0:y1, x0:x1], cv2.COLOR_GRAY2RGB) if rl is not None else None),
        ("rectified right", cv2.cvtColor(rr[y0:y1, x0:x1], cv2.COLOR_GRAY2RGB) if rr is not None else None),
        ("disparity [rectified px]", prep(np.where(disp > 0, disp, np.nan))),
        ("depth [mm]", prep(np.where(depth > 0, depth, np.nan), cmap="turbo")),
        ("confidence", prep(np.where(depth > 0, conf, np.nan))),
    ]
    fig, axes = plt.subplots(len(panels), 1, figsize=(3.6, 2.1 * len(panels)))
    for ax, (title, img) in zip(axes, panels):
        if img is None:
            ax.axis("off")
            continue
        if img.ndim == 3 and img.shape[2] == 3:
            ax.imshow(img)
        else:
            ax.imshow(img)
        ax.set_title(title, fontsize=8)
        ax.set_xticks([])
        ax.set_yticks([])
    fig.suptitle("recon_cli maps (rectified pair, disparity, depth, confidence), "
                 "cropped around the plate", fontsize=8)
    fig.tight_layout()
    return _save(fig, path)


def fig_experiments(exp, path):
    """Coverage vs verified accuracy for every config (the key trade-off plot)."""
    cfg = exp.get("configs", {})
    if not cfg:
        return None
    fig, ax = plt.subplots(figsize=(8.2, 5.2))
    families = {
        "baseline": ("tab:red", "o"),
        "exposure": ("tab:blue", "s"),
        "clip": ("tab:brown", "^"),
        "window": ("tab:green", "D"),
        "confidence": ("tab:purple", "v"),
        "lr_tolerance": ("tab:orange", "P"),
        "scale": ("tab:cyan", "*"),
    }
    for name in exp.get("order", []):
        s = cfg[name]["summary"]
        if s.get("plane_rms_p50_mm") is None or s.get("valid_fraction_p50_pct") is None:
            continue
        fam = "baseline" if name == "baseline" else next(
            (k for k in families if name.startswith(k)), "other")
        color, marker = families.get(fam, ("gray", "x"))
        x = s["valid_fraction_p50_pct"]
        y = s["plane_rms_p50_mm"]
        ax.scatter(x, y, color=color, marker=marker, s=70 if name == "baseline" else 42,
                   edgecolor="k", linewidth=0.4, zorder=3, label=fam if fam != "other" else None)
        ax.annotate(name, (x, y), fontsize=5.5, xytext=(3, 3),
                    textcoords="offset points", alpha=0.8)
    ax.set_xlabel("valid depth fraction [% of frame]  (more pixels ->)")
    ax.set_ylabel("plate plane RMS [mm]  (verified accuracy ->)")
    ax.set_title("Coverage vs verified planarity per experiment\n"
                 "(up-left is better; the annotation labels every config)")
    ax.grid(alpha=0.3)
    handles, labels = ax.get_legend_handles_labels()
    seen, h2, l2 = set(), [], []
    for h, l in zip(handles, labels):
        if l not in seen:
            seen.add(l)
            h2.append(h)
            l2.append(l)
    ax.legend(h2, l2, fontsize=7, loc="best")
    return _save(fig, path)
