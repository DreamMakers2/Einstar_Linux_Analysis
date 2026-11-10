"""Tests for :mod:`calib.localize` (sub-pixel marker localisation).

Synthetic ground truth: a planar circular marker (or the whole 41-marker plate)
is rendered analytically by supersampled polygon rasterisation, so the exact
edge position is known before the PSF, and the ground truth is the projection
of the 3-D circle centre.  Bias bounds are asserted against that truth, not
against another localiser.

Run with the repo venv::

    HOME=/tmp OPENBLAS_NUM_THREADS=1 .venv/bin/python -m pytest -q \
        tests/test_localize.py
"""

from __future__ import annotations

import math

import cv2
import numpy as np
import pytest

from calib import detect, localize as L, plate
from calib.geometry import project


# --------------------------------------------------------------------------
# minimal analytic renderer (single marker and full plate)
# --------------------------------------------------------------------------

SS = 4
K0 = np.array([[1159.115, 0.072, 623.912],
               [0.0, 1159.021, 534.563],
               [0.0, 0.0, 1.0]])
KC0 = np.array([-0.16203, 0.1733, 0.00019, 0.00015, 0.01846])
BIG_UV = ((2.0, 1.0), (4.0, 1.0), (5.0, 1.0), (3.5, 3.0))


def _rot(tilt_deg, roll_deg=0.0):
    t = math.radians(tilt_deg)
    r = math.radians(roll_deg)
    Rt = np.array([[1, 0, 0], [0, math.cos(t), -math.sin(t)],
                   [0, math.sin(t), math.cos(t)]])
    Rr = np.array([[math.cos(r), -math.sin(r), 0], [math.sin(r), math.cos(r), 0],
                   [0, 0, 1]])
    return Rr @ Rt


def _plate_mm(pitch=27.72, r_small=1.77, big_scale=1.75):
    uv = [(float(u), float(v)) for v in range(5) for u in range(8)] + [(3.5, 3.0)]
    xy = np.array([[(u - 3.5) * pitch, (v - 2.0) * pitch] for u, v in uv])
    r = np.full(len(uv), r_small)
    for i, k in enumerate(uv):
        if k in BIG_UV:
            r[i] *= big_scale
    return uv, np.column_stack([xy, np.zeros(len(uv))]), r


def _raster(P, gt, shape):
    """Supersampled coverage map of one projected polygon (absolute pixels)."""
    P = np.asarray(P, float)
    x0 = max(0, int(np.floor(P[:, 0].min())) - 2)
    x1 = min(shape[1], int(np.ceil(P[:, 0].max())) + 3)
    y0 = max(0, int(np.floor(P[:, 1].min())) - 2)
    y1 = min(shape[0], int(np.ceil(P[:, 1].max())) + 3)
    if x1 <= x0 or y1 <= y0:
        return None
    big = np.zeros(((y1 - y0) * SS, (x1 - x0) * SS), np.float32)
    pts = ((P - np.array([x0, y0]) + 0.5) * SS).astype(np.int32)
    cv2.fillPoly(big, [pts.reshape(-1, 1, 2)], 1.0)
    add = big.reshape(y1 - y0, SS, x1 - x0, SS).mean(axis=(1, 3))
    return (x0, x1, y0, y1), add


def _finish(cov, shape, blur, contrast, bg, noise, sat, rng):
    img = bg + contrast * cov
    if blur > 0:
        img = cv2.GaussianBlur(img, (0, 0), float(blur))
    img = np.clip(img, 0.0, sat)
    if noise > 0:
        img = img + rng.normal(0.0, noise, img.shape)
    return np.clip(np.round(img), 0, 255).astype(np.uint8)


def _render(R, t, K, kc, shape=(360, 480), r_mm=1.77, big_scale=1.75,
            blur=1.2, contrast=200.0, bg=55.0, noise=1.0, sat=255.0,
            thickness_mm=0.0, seed=0, uv_subset=None, pitch=27.72):
    """Render the plate; returns (image, gt pixel positions per uv)."""
    rng = np.random.default_rng(seed)
    uv, X, radii = _plate_mm(pitch, r_mm, big_scale)
    if uv_subset is not None:
        keep = [i for i, k in enumerate(uv) if k in uv_subset]
        uv = [uv[i] for i in keep]
        X = X[keep]
        radii = radii[keep]
    Xc = X @ np.asarray(R, float).T + np.asarray(t, float)
    gt = project(K, kc, Xc)
    cov = np.zeros(shape, np.float32)
    ph = np.linspace(0, 2 * math.pi, 97)[:-1]
    e1 = np.asarray(R, float)[:, 0]
    e2 = np.asarray(R, float)[:, 1]
    n = np.asarray(R, float)[:, 2]
    for i in range(len(X)):
        if Xc[i, 2] <= 1.0:
            continue
        C = Xc[i]
        top = C[None, :] + radii[i] * (np.cos(ph)[:, None] * e1[None, :]
                                       + np.sin(ph)[:, None] * e2[None, :])
        P = project(K, kc, top)
        if thickness_mm > 0:
            bot = project(K, kc, top - thickness_mm * n[None, :])
            _a, inter = cv2.intersectConvexConvex(P.astype(np.float32),
                                                  bot.astype(np.float32))
            if inter is not None and len(inter) >= 3:
                P = inter.reshape(-1, 2)
        out = _raster(P, gt[i], shape)
        if out is None:
            continue
        (x0, x1, y0, y1), add = out
        cov[y0:y1, x0:x1] = np.maximum(cov[y0:y1, x0:x1], add)
    return _finish(cov, shape, blur, contrast, bg, noise, sat, rng), \
        {tuple(u): g for u, g in zip(uv, gt)}, radii


def _one_marker(tilt_deg=0.0, r_px=5.0, Z=450.0, offset_px=(0.0, 0.0),
                r_mm=None, **kw):
    """Render a single marker whose 3-D centre projects to the target point."""
    patch = int(max(41, 4.2 * r_px + 21))
    K = K0.copy()
    K[0, 2] = patch / 2.0 - 0.5
    K[1, 2] = patch / 2.0 - 0.5
    u0 = K[0, 2] + offset_px[0]
    v0 = K[1, 2] + offset_px[1]
    und = cv2.undistortPoints(np.array([[[u0, v0]]], np.float32), K, KC0).reshape(2)
    C = np.array([und[0] * Z, und[1] * Z, Z], float)
    if r_mm is None:
        r_mm = r_px * Z / (K[0, 0] * 0.5 *
                           (1.0 + abs(math.cos(math.radians(tilt_deg)))))
    R = _rot(tilt_deg)
    e1, e2, n = R[:, 0], R[:, 1], R[:, 2]
    ph = np.linspace(0, 2 * math.pi, 97)[:-1]
    top = C[None, :] + r_mm * (np.cos(ph)[:, None] * e1[None, :]
                               + np.sin(ph)[:, None] * e2[None, :])
    P = project(K, KC0, top)
    if kw.get("thickness_mm", 0.0) > 0:
        bot = project(K, KC0, top - kw["thickness_mm"] * n[None, :])
        _a, inter = cv2.intersectConvexConvex(P.astype(np.float32),
                                              bot.astype(np.float32))
        if inter is not None and len(inter) >= 3:
            P = inter.reshape(-1, 2)
    gt = project(K, KC0, C.reshape(1, 3))[0]
    out = _raster(P, gt, (patch, patch))
    cov = np.zeros((patch, patch), np.float32)
    if out is not None:
        (x0, x1, y0, y1), add = out
        cov = np.zeros((patch, patch), np.float32)
        cov[y0:y1, x0:x1] = add
    rng = np.random.default_rng(kw.pop("seed", 0))
    img = _finish(cov, (patch, patch), kw.pop("blur", 1.2),
                  kw.pop("contrast", 200.0), kw.pop("bg", 55.0),
                  kw.pop("noise", 1.0), kw.pop("sat", 255.0), rng)
    return img, gt, K, KC0, R, C


def _run(img, gt, K, kc, r_px, method, plane=None):
    return L.localize_marker(img, gt[0], gt[1], r_px, method=method, K=K, kc=kc,
                             plane=plane)


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------

def test_api_fields_and_types():
    img, gt, K, kc, R, t = _one_marker(20.0, 5.0)
    for m in L.METHODS:
        r = _run(img, gt, K, kc, 5.0, m, plane={"R": R, "t": t})
        for key in ("u", "v", "sigma_px", "method", "quality", "n_edges", "ok",
                    "reason"):
            assert key in r, (m, key)
        assert r["method"] in L.METHODS
        assert isinstance(r["ok"], (bool, np.bool_))
        assert isinstance(r["reason"], str)
        assert 0.0 <= float(r["quality"]) <= 1.0
        assert int(r["n_edges"]) >= 0
        assert np.isfinite(r["u"]) and np.isfinite(r["v"])
        if r["ok"]:
            assert np.isfinite(r["sigma_px"]) and r["sigma_px"] > 0


def test_unknown_method_and_bad_arguments():
    img, gt, K, kc, R, t = _one_marker(10.0, 5.0)
    with pytest.raises(ValueError):
        L.localize_marker(img, 20, 20, 5.0, method="nope")
    with pytest.raises(ValueError):
        L.localize_marker(img, 20, 20, 0.0, method="centroid")
    with pytest.raises(ValueError):
        L.localize_marker(img, float("nan"), 20, 5.0, method="centroid")


def test_centroid_is_bit_identical_to_production():
    """The ``centroid`` method must reproduce detect._weighted_centroid."""
    rng = np.random.default_rng(3)
    img, gt, K, kc, R, t = _one_marker(25.0, 6.0, noise=2.0)
    for x, y in ((gt[0], gt[1]), (gt[0] + 0.4, gt[1] - 0.3)):
        ref = detect._weighted_centroid(img, int(round(x)), int(round(y)), 13, False)
        res = L.localize_marker(img, x, y, 6.0, method="centroid", window=13)
        assert res["u"] == pytest.approx(ref[0], abs=1e-12)
        assert res["v"] == pytest.approx(ref[1], abs=1e-12)


def test_plane_normal_from_homography():
    """The normal recovered from a projective grid homography is the true one."""
    R = _rot(30.0, 10.0)
    t = np.array([5.0, -4.0, 500.0])
    uv, X, _r = _plate_mm()
    uv = np.array(uv)
    gt = project(K0, KC0, X @ R.T + t)
    H, _ = cv2.findHomography(uv, gt, 0)
    und = cv2.undistortPoints(gt.reshape(-1, 1, 2).astype(np.float32),
                              K0, KC0).reshape(-1, 2)
    Hn, _ = cv2.findHomography(uv, und, 0)
    n = L.plane_normal_from_homography(Hn)
    assert n is not None
    cos = abs(float(np.dot(n / np.linalg.norm(n), R[:, 2])))
    assert cos > 0.9999


# --------------------------------------------------------------------------
# synthetic ground truth: single marker
# --------------------------------------------------------------------------

@pytest.mark.parametrize("tilt", [0.0, 20.0, 40.0])
def test_single_marker_bias_bounds(tilt):
    """|bias| over 6 sub-pixel phases must stay sub-0.1 px up to 40 deg."""
    r_px = 5.0
    errs = {m: [] for m in ("centroid", "gradient", "conic",
                            "conic_perspective", "auto")}
    for k in range(6):
        img, gt, K, kc, R, t = _one_marker(tilt, r_px, seed=k,
                                           offset_px=((k % 3) * 0.35,
                                                      (k // 3) * 0.35))
        for m in errs:
            r = _run(img, gt, K, kc, r_px, m, plane={"R": R, "t": t})
            assert r["ok"], (m, r["reason"])
            errs[m].append([r["u"] - gt[0], r["v"] - gt[1]])
    for m, e in errs.items():
        e = np.array(e)
        bias = float(np.linalg.norm(e.mean(0)))
        rms = float(np.sqrt(np.mean((e ** 2).sum(1))))
        assert bias < 0.08, (m, bias)
        assert rms < 0.12, (m, rms)


def test_perspective_correction_removes_large_bias():
    """At r=24 px / 250 mm the conic centre is biased by >0.1 px and the
    perspective correction removes it (the effect is physical, not noise)."""
    r_px, Z, tilt = 24.0, 250.0, 40.0
    r_mm = r_px * Z / (K0[0, 0] * 0.5 * (1 + math.cos(math.radians(tilt))))
    img, gt, K, kc, R, t = _one_marker(tilt, r_px, Z, r_mm=r_mm, seed=1,
                                       offset_px=(0.2, 0.35))
    plane = {"normal": R[:, 2]}
    con = _run(img, gt, K, kc, r_px, "conic", plane=plane)
    per = _run(img, gt, K, kc, r_px, "conic_perspective", plane=plane)
    assert con["ok"] and per["ok"]
    e_con = np.array([con["u"] - gt[0], con["v"] - gt[1]])
    e_per = np.array([per["u"] - gt[0], per["v"] - gt[1]])
    assert np.linalg.norm(e_con) > 0.10
    assert np.linalg.norm(e_per) < 0.05
    assert per["correction_px"] > 0.10
    # the applied correction points along the predicted eccentricity (+v here)
    assert per["correction"][1] > 0.05
    assert per["circularity"] < 0.02


def test_polarity_both_ways():
    """Dark markers on a bright background must localise as well as bright."""
    img, gt, K, kc, R, t = _one_marker(25.0, 5.0, seed=2)
    inv = 255 - img
    for img_i, invert in ((img, None), (inv, True)):
        r = L.localize_marker(img_i, gt[0], gt[1], 5.0, method="auto", K=K,
                              kc=kc, invert=invert, plane={"R": R, "t": t})
        assert r["ok"], r["reason"]
        assert r["polarity"] == "dark" if invert else r["polarity"] == "bright"
        assert np.hypot(r["u"] - gt[0], r["v"] - gt[1]) < 0.1


def test_low_contrast_and_saturation():
    r_px = 5.0
    # 12 DN contrast, 1 DN noise: hard but must still work and flag the noise
    img, gt, K, kc, R, t = _one_marker(20.0, r_px, contrast=12.0, seed=4)
    r = _run(img, gt, K, kc, r_px, "auto", plane={"R": R, "t": t})
    assert r["ok"]
    assert np.hypot(r["u"] - gt[0], r["v"] - gt[1]) < 0.15
    # a saturated marker still localises (the flat top is symmetric)
    img, gt, K, kc, R, t = _one_marker(20.0, r_px, contrast=250.0, seed=5)
    r = _run(img, gt, K, kc, r_px, "conic", plane={"R": R, "t": t})
    assert r["ok"] and r["saturated"]
    assert np.hypot(r["u"] - gt[0], r["v"] - gt[1]) < 0.1
    # no signal at all: an explicit failure, not a silent wrong answer
    flat = np.full((41, 41), 60, np.uint8)
    r = L.localize_marker(flat, 20.0, 20.0, 5.0, method="auto", K=K, kc=kc)
    assert r["ok"] is False and r["reason"] == "low_contrast"


def test_partial_visibility_near_border():
    """A marker clipped by the image border must be flagged, not crash."""
    img, gt, K, kc, R, t = _one_marker(20.0, 6.0, seed=6)
    # shift the marker so that ~40 % of it is outside the image
    M = np.float32([[1, 0, 12], [0, 1, 10]])
    shifted = cv2.warpAffine(img, M, (img.shape[1], img.shape[0]),
                             borderValue=55)
    g = np.array([gt[0] + 12, gt[1] + 10])
    r = L.localize_marker(shifted, g[0], g[1], 6.0, method="conic", K=K, kc=kc)
    assert r["ok"] or r["reason"] in ("few_edges", "conic_rejected", "no_edges")
    if r["ok"]:
        assert r["partial"] or np.hypot(r["u"] - g[0], r["v"] - g[1]) < 0.5


def test_through_hole_is_a_physical_bias_not_a_localiser_bug():
    """A 1 mm through-hole biases *every* localiser by ~tan(tilt): documented
    limitation, but the localisers must agree with each other."""
    tilt = 35.0
    img, gt, K, kc, R, t = _one_marker(tilt, 6.0, 400.0, thickness_mm=1.0,
                                       seed=7, blur=1.0)
    res = {m: _run(img, gt, K, kc, 6.0, m) for m in
           ("centroid", "conic", "conic_perspective")}
    pos = np.array([[r["u"], r["v"]] for r in res.values()])
    assert np.max(np.linalg.norm(pos - pos.mean(0), axis=1)) < 0.35
    # the aperture bias is real and large (this is the honest limitation)
    assert np.hypot(res["conic"]["u"] - gt[0], res["conic"]["v"] - gt[1]) > 0.3


# --------------------------------------------------------------------------
# grid level
# --------------------------------------------------------------------------

def _grid_scene(tilt=25.0, Z=450.0, seed=0, **kw):
    R = _rot(tilt)
    t = np.array([0.0, 0.0, Z])
    img, gt, radii = _render(R, t, K0, KC0, shape=(1024, 1280), seed=seed, **kw)
    return img, gt, R, t


def test_refine_grid_recovers_ground_truth():
    img, gt, R, t = _grid_scene(25.0)
    blobs = detect.detect_blobs(img, invert=False, refine=False)
    obs = detect.assign_grid(blobs)
    assert obs is not None and obs.n_lattice >= 36
    for m, bound in (("centroid", 0.08), ("conic", 0.08),
                     ("conic_perspective", 0.06), ("auto", 0.06)):
        ref = L.refine_grid(img, obs, method=m, K=K0, kc=KC0, loo=False)
        assert ref.n >= 36
        err = np.array([ref.points[k] - gt[tuple(k)] for k in ref.points])
        rms = float(np.sqrt(np.mean((err ** 2).sum(1))))
        bias = float(np.linalg.norm(err.mean(0)))
        assert rms < 0.25, (m, rms)
        assert bias < bound, (m, bias)
        assert ref.rms < 0.5
        assert ref.homography.shape == (3, 3)
        assert ref.stats["runtime_ms"] > 0
        rec = next(iter(ref.records.values()))
        assert "sigma_px" in rec and "quality" in rec


def test_refine_grid_input_forms_agree():
    img, gt, R, t = _grid_scene(15.0, seed=1)
    blobs = detect.detect_blobs(img, invert=False, refine=False)
    obs = detect.assign_grid(blobs)
    keys = sorted(obs.points.keys())
    as_dict = {k: obs.points[k] for k in keys}
    arr4 = np.array([[k[0], k[1], *obs.points[k]] for k in keys])
    r1 = L.refine_grid(img, obs, method="conic", K=K0, kc=KC0, loo=False)
    r2 = L.refine_grid(img, as_dict, method="conic", K=K0, kc=KC0, loo=False)
    r3 = L.refine_grid(img, arr4, method="conic", K=K0, kc=KC0, loo=False)
    for r in (r2, r3):
        common = sorted(set(r1.points) & set(r.points))
        assert len(common) > 30
        d = max(np.linalg.norm(r1.points[k] - r.points[k]) for k in common)
        assert d < 1e-6


def test_refine_observation_returns_grid_observation():
    img, gt, R, t = _grid_scene(10.0, seed=2)
    obs = detect.assign_grid(detect.detect_blobs(img, invert=False, refine=False))
    new = L.refine_observation(img, obs, method="conic", K=K0, kc=KC0, loo=False)
    assert isinstance(new, detect.GridObservation)
    assert new.n_lattice == len(new.points)
    assert new.detector == "localize-v1"
    assert set(new.points) <= set(obs.points)
    err = np.array([new.points[k] - gt[tuple(k)] for k in new.points])
    assert float(np.sqrt(np.mean((err ** 2).sum(1)))) < 0.25


def test_refine_grid_off_lattice_marker():
    img, gt, R, t = _grid_scene(20.0, seed=3)
    obs = detect.assign_grid(detect.detect_blobs(img, invert=False, refine=False))
    ref = L.refine_grid(img, obs, method="conic", K=K0, kc=KC0, loo=False,
                        include_off_lattice=True)
    key = (3.5, 3.0)
    assert key in ref.records
    if ref.records[key]["ok"]:
        assert key in ref.points
        d = np.linalg.norm(ref.points[key] - gt[key])
        assert d < 0.5, d
