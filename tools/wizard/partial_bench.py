"""Offline bench for partial-board and roll-sector plate recognition.

The live wizard must recognise a calibration plate that is only *partly*
inside the frame and that the user may hold at any in-plane roll.  This module
measures that, reproducibly, on real recorded pixels -- it never touches the
hardware and never writes into a session.

What is measured
----------------
``calib.detect.detect_grid_auto``  the strict, four-anchor detector
``calib.detect_partial``           the three-large-marker fallback
``tools.wizard.detection``         the production per-camera path (strict +
                                   ``prefer_partial``), i.e. what the controller
                                   actually receives

For every case the bench records detection success, the number of assigned
markers, the number of markers that the transform left visible, whether the
assigned labels are the *true* ones, the homography determinant (chirality),
the lattice rms and the wall time.

Ground truth
------------
The production store keeps the observation the controller accepted:
``image_points`` per camera, row-major in ``(v, u)``.  A view whose recorded
marker count is 40 in both IR cameras has a *verified* labelling -- acceptance
required ``det(H) > 0`` and a 4/4 large-marker witness -- so index ``i`` of
that list is canonical label ``(i % 8, i // 8)``.  Every transform moves the
pixels and the labels together, which keeps ground truth exact.

The rejected-frame family uses the raw ``frames/frame_*_cam*.pgm`` images the
capture tool retained (a live session prunes to a tail, so this is the *end*
of one real failed session).  There is no stored labelling for those, so the
bench measures detection success, chirality and stereo label consistency
(a wrong labelling in one camera breaks the epipolar agreement of the shared
labels) instead of identity.

Usage
-----
::

    .venv/bin/python -m tools.wizard.partial_bench            # default sweep
    .venv/bin/python -m tools.wizard.partial_bench --help
"""

from __future__ import annotations

from dataclasses import dataclass, field
import argparse
import json
import os
from pathlib import Path
import sys
import time
from typing import Any, Iterable, Iterator, Sequence

import cv2
import numpy as np

from calib import detect, detect_partial, plate

__all__ = [
    "Case", "bench_session", "bench_rejected_frames", "main",
    "clip_case", "rotate_case", "tilt_case", "identity_score",
]

#: production store of the last real failed session (read-only)
DEFAULT_SESSION = Path("<calibration-session>")
#: factory intrinsics used by the tilt transform; fall back to nominal values
DEFAULT_PROFILE = Path.home() / "einstar-private/einstar-profiles/factory-e10.json"
#: nominal E10 IR intrinsics (used when the factory profile is absent)
_NOMINAL_K = dict(fx=1159.115, fy=1159.021, cx=623.912, cy=534.563)

#: names of the cameras in the production store
CAMERAS = ("left", "right")


# ---------------------------------------------------------------------------
# cases
# ---------------------------------------------------------------------------

@dataclass
class Case:
    """One image plus the ground truth the transform preserved."""

    name: str
    family: str
    camera: str
    gray: np.ndarray
    uv: np.ndarray | None = None            # (n, 2) canonical labels
    pixels: np.ndarray | None = None        # (n, 2) ground-truth pixels
    meta: dict = field(default_factory=dict)

    @property
    def has_truth(self) -> bool:
        return self.uv is not None and self.pixels is not None and len(self.uv)

    def as_dict(self) -> dict:
        return {"name": self.name, "family": self.family, "camera": self.camera,
                "shape": [int(self.gray.shape[0]), int(self.gray.shape[1])],
                "expected": int(len(self.uv)) if self.has_truth else None,
                "meta": self.meta}


def _labels(n: int) -> np.ndarray:
    """Row-major ``(v, u)`` labels for a stored ``image_points`` list."""
    return np.array([[float(i % plate.GRID_COLS), float(i // plate.GRID_COLS)]
                     for i in range(n)], float)


def _in_bounds(pts: np.ndarray, shape: tuple[int, int], margin: float = 4.0) -> np.ndarray:
    h, w = shape
    return ((pts[:, 0] >= margin) & (pts[:, 0] <= w - 1 - margin)
            & (pts[:, 1] >= margin) & (pts[:, 1] <= h - 1 - margin))


def _subset(case: Case, keep: np.ndarray) -> Case:
    case.uv = None if case.uv is None else case.uv[keep]
    case.pixels = None if case.pixels is None else case.pixels[keep]
    return case


# -- transforms -------------------------------------------------------------

def clip_case(case: Case, side: str, frac: float, *, mode: str = "mask") -> Case:
    """Cut ``frac`` of the image off one edge or one corner.

    ``side`` is ``left|right|top|bottom`` or ``corner_<x><y>`` with ``x``/``y``
    in ``{0, 1}`` selecting which side is cut.  ``mode='mask'`` blanks the
    removed region in place (pixel coordinates unchanged); ``mode='crop'``
    crops the frame (coordinates shift).  Both are equivalent for the detector
    except that a crop removes the pixels entirely.
    """
    h, w = case.gray.shape
    dx, dy = int(round(frac * w)), int(round(frac * h))
    if side.startswith("corner_"):
        cx, cy = int(side[7]), int(side[8])
        x0, x1 = (dx, w) if cx else (0, w - dx)
        y0, y1 = (dy, h) if cy else (0, h - dy)
    else:
        x0, y0, x1, y1 = {"left": (dx, 0, w, h), "right": (0, 0, w - dx, h),
                          "top": (0, dy, w, h), "bottom": (0, 0, w, h - dy)}[side]
    out = Case(f"{case.name}|clip-{side}-{int(frac * 100)}-{mode}",
               "clip_corner" if side.startswith("corner_") else "clip_edge",
               case.camera, case.gray, case.uv, case.pixels,
               dict(case.meta, side=side, frac=frac, mode=mode))
    if mode == "mask":
        out.gray = case.gray.copy()
        out.gray[:, :x0] = 0
        out.gray[:, x1:] = 0
        out.gray[:y0, :] = 0
        out.gray[y1:, :] = 0
        pts = case.pixels
        if pts is None:
            return out
        keep = _in_bounds(pts, (h, w)) & (pts[:, 0] >= x0) & (pts[:, 0] < x1) \
            & (pts[:, 1] >= y0) & (pts[:, 1] < y1)
    else:
        out.gray = np.ascontiguousarray(case.gray[y0:y1, x0:x1])
        pts = case.pixels
        if pts is None:
            return out
        keep = _in_bounds(pts, (h, w)) & (pts[:, 0] >= x0) & (pts[:, 0] < x1) \
            & (pts[:, 1] >= y0) & (pts[:, 1] < y1)
        out.pixels = pts - np.array([x0, y0], float)
    return _subset(out, keep)


def rot_matrix(deg: float, centre: np.ndarray) -> np.ndarray:
    """3x3 rotation about ``centre`` (pixel in -> pixel out)."""
    th = np.radians(float(deg))
    c, s = float(np.cos(th)), float(np.sin(th))
    t = np.asarray(centre, float) - np.array([[c, -s], [s, c]]) @ np.asarray(centre, float)
    return np.array([[c, -s, t[0]], [s, c, t[1]], [0.0, 0.0, 1.0]])


def _warp(gray: np.ndarray, M: np.ndarray) -> np.ndarray:
    return cv2.warpAffine(gray, np.asarray(M, float)[:2], (gray.shape[1], gray.shape[0]),
                          flags=cv2.INTER_NEAREST, borderMode=cv2.BORDER_CONSTANT,
                          borderValue=0)


def _apply_h(pts: np.ndarray, H: np.ndarray) -> np.ndarray:
    p = np.hstack([pts, np.ones((len(pts), 1))]) @ np.asarray(H, float).T
    return p[:, :2] / p[:, 2:3]


def rotate_case(case: Case, deg: float) -> Case:
    """Rotate image and labels by ``deg`` about the plate centroid.

    90/180/270 leave the lattice on the pixel grid when the centroid is
    rounded to whole pixels, so the transform is exact (``INTER_NEAREST``).
    """
    if case.pixels is None:
        centre = np.array([case.gray.shape[1] / 2.0, case.gray.shape[0] / 2.0])
    else:
        centre = np.round(np.asarray(case.pixels, float).mean(0))
    M = rot_matrix(deg, centre)
    out = Case(f"{case.name}|rot{int(deg)}", "rotate", case.camera,
               _warp(case.gray, M), case.uv, case.pixels,
               dict(case.meta, deg=float(deg)))
    if case.pixels is not None:
        pts = _apply_h(case.pixels, M)
        out.pixels = pts
        out.uv = case.uv
    return out


def _intrinsics(profile_path: Path | None = None) -> np.ndarray:
    path = Path(profile_path or DEFAULT_PROFILE)
    K = _NOMINAL_K
    try:
        from calib import recon_profile  # local import: optional dependency
        cam = recon_profile.load(path).camera("ir_left").intrinsics()
        K = dict(fx=cam.fx, fy=cam.fy, cx=cam.cx, cy=cam.cy)
    except Exception:
        pass
    return np.array([[K["fx"], 0.0, K["cx"]], [0.0, K["fy"], K["cy"]], [0.0, 0.0, 1.0]])


def tilt_case(case: Case, deg: float, axis: str = "x",
              K: np.ndarray | None = None) -> Case:
    """Simulate a camera tilt of ``deg`` about ``axis`` (``x`` or ``y``).

    A pure camera rotation about the optical centre induces the exact plane-free
    homography ``H = K R K^-1``; warping with it is therefore a physically
    consistent simulation of tilting the *camera*, not an arbitrary stretch.
    """
    K = _intrinsics() if K is None else np.asarray(K, float)
    a = np.radians(float(deg))
    if axis == "x":
        R = np.array([[1, 0, 0], [0, np.cos(a), -np.sin(a)], [0, np.sin(a), np.cos(a)]])
    else:
        R = np.array([[np.cos(a), 0, np.sin(a)], [0, 1, 0], [-np.sin(a), 0, np.cos(a)]])
    H = K @ R @ np.linalg.inv(K)
    # bilinear: nearest-neighbour resampling of a 10-20 deg warp aliases the
    # small markers and would measure the interpolation, not the detector
    out = Case(f"{case.name}|tilt{axis}{int(deg)}", "tilt", case.camera,
               cv2.warpPerspective(case.gray, H, (case.gray.shape[1], case.gray.shape[0]),
                                   flags=cv2.INTER_LINEAR, borderValue=0),
               case.uv, case.pixels, dict(case.meta, deg=float(deg), axis=axis))
    if case.pixels is not None:
        out.pixels = _apply_h(case.pixels, H)
        out.uv = case.uv
    return out


# ---------------------------------------------------------------------------
# measurement
# ---------------------------------------------------------------------------

def _obs_points(obs) -> tuple[np.ndarray, np.ndarray]:
    keys = sorted(obs.points)
    return (np.array(keys, float),
            np.array([obs.points[k] for k in keys], float))


def identity_score(obs, pixels: np.ndarray, uv: np.ndarray,
                   tol: float = 2.5) -> dict:
    """Compare an observation's labels with the ground-truth labelling.

    Returns the number of detected points that correspond to a reference point
    (``matched``), of those how many carry the *right* label (``correct``) and
    how many the wrong one (``wrong``); ``shift`` is the dominant label offset
    of the wrong ones (a self-consistent shifted sub-lattice shows one offset
    shared by every wrong label).
    """
    out = {"n": 0, "matched": 0, "correct": 0, "wrong": 0, "missing": 0,
           "shift": None, "mirrored": None, "det_h": None, "rms": None}
    if obs is None:
        out["missing"] = int(len(uv))
        return out
    L, P = _obs_points(obs)
    out["n"] = int(len(P))
    out["det_h"] = float(np.linalg.det(np.asarray(obs.homography, float)))
    out["rms"] = float(obs.rms)
    out["missing"] = int(len(uv))
    if len(P) == 0 or len(pixels) == 0:
        return out
    d = np.linalg.norm(P[:, None, :] - np.asarray(pixels, float)[None, :, :], axis=2)
    near = d.argmin(1)
    ok = d.min(1) <= tol
    hit = np.where(ok)[0]
    right = hit[np.all(np.abs(L[hit] - uv[near[hit]]) < 0.01, axis=1)]
    wrong = np.setdiff1d(hit, right)
    out["matched"] = int(len(hit))
    out["correct"] = int(len(right))
    out["wrong"] = int(len(wrong))
    out["missing"] = int(len(uv) - len(right))
    if len(wrong):
        deltas = (L[wrong] - uv[near[wrong]]).round()
        vals, counts = np.unique(deltas, axis=0, return_counts=True)
        out["shift"] = [float(x) for x in vals[int(np.argmax(counts))]]
        out["shift_share"] = float(counts.max() / len(wrong))
    return out


def _stereo_consistency(left, right) -> dict:
    """Epipolar agreement of the labels two cameras assigned to the same marker.

    A labelling that is mirrored or shifted in *one* camera keeps the same
    marker count but destroys the epipolar relationship of the shared labels,
    so this is an identity check that needs no ground truth.
    """
    out = {"shared": 0, "inliers": 0, "median_sampson": None}
    if left is None or right is None:
        return out
    common = sorted(set(left.points) & set(right.points))
    out["shared"] = len(common)
    if len(common) < 8:
        return out
    P = np.array([left.points[k] for k in common], np.float32)
    Q = np.array([right.points[k] for k in common], np.float32)
    F, mask = cv2.findFundamentalMat(P, Q, cv2.FM_RANSAC, 1.5, 0.999, 5000)
    if F is None or mask is None:
        return out
    F = np.asarray(F, float).reshape(3, 3)
    hom = np.hstack([P, np.ones((len(P), 1))]) @ F.T
    Qh = np.hstack([Q, np.ones((len(Q), 1))])
    sampson = np.abs(np.sum(hom * Qh, axis=1)) / np.sqrt(
        np.maximum(hom[:, 0] ** 2 + hom[:, 1] ** 2 + Qh[:, 0] ** 2 + Qh[:, 1] ** 2, 1e-12))
    inl = mask.ravel().astype(bool)
    out["inliers"] = int(inl.sum())
    if inl.any():
        out["median_sampson"] = float(np.median(sampson[inl]))
    return out


def _orient(gray, obs, shape) -> dict:
    try:
        from tools.sweep import orient
    except Exception:                                    # pragma: no cover
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
            from tools.sweep import orient
        except Exception:
            return {"reason": "unavailable", "ok": False}
    if obs is None:
        return {"reason": "no_observation", "ok": False}
    try:
        live = orient.check_orientation(gray, obs).as_dict()          # controller rule
    except Exception as exc:                              # pragma: no cover
        live = {"reason": f"error: {exc}", "ok": False}
    try:
        aware = orient.check_orientation(gray, obs, image_shape=shape).as_dict()
    except Exception as exc:                              # pragma: no cover
        aware = {"reason": f"error: {exc}", "ok": False}
    return {"live": live, "visibility_aware": aware}


def _timed(fn):
    t0 = time.perf_counter()
    try:
        value = fn()
        err = None
    except Exception as exc:                              # pragma: no cover
        value, err = None, f"{type(exc).__name__}: {exc}"
    return value, (time.perf_counter() - t0), err


def evaluate_case(case: Case, variants: Sequence[str] = ("strict", "partial", "wizard"),
                  with_orientation: bool = True) -> dict:
    """Run every requested detector variant on one case and score it."""
    from tools.wizard import detection as wizard_detection

    gray = case.gray
    results: dict[str, Any] = {"case": case.as_dict(), "variants": {}}
    obs_strict = obs_wizard = None
    for variant in variants:
        if variant == "strict":
            fn = lambda: detect.detect_grid_auto(gray, early_lattice=40, early_rms=0.5)  # noqa: E731
        elif variant == "partial":
            fn = lambda: detect_partial.detect_grid_partial(gray).observation  # noqa: E731
        elif variant == "wizard":
            fn = lambda: wizard_detection.detect_one(gray).observation  # noqa: E731
        else:                                             # pragma: no cover
            raise ValueError(f"unknown variant {variant!r}")
        obs, seconds, err = _timed(fn)
        if variant == "strict":
            obs_strict = obs
        elif variant == "wizard":
            obs_wizard = obs
        entry: dict[str, Any] = {"seconds": round(seconds, 4), "error": err,
                                 "ok": obs is not None,
                                 "identity_ok": bool(getattr(obs, "identity_ok", False))
                                 if obs is not None else False,
                                 "witness_score": int(getattr(obs, "witness_score", -1))
                                 if obs is not None else -1,
                                 "detector": str(getattr(obs, "detector", ""))
                                 if obs is not None else ""}
        if case.has_truth:
            entry.update(identity_score(obs, case.pixels, case.uv))
        elif obs is not None:
            entry["n"] = int(len(obs.points))
            entry["det_h"] = float(np.linalg.det(np.asarray(obs.homography, float)))
        if with_orientation and obs is not None:
            entry["orient"] = _orient(gray, obs, gray.shape)
        results["variants"][variant] = entry

    if case.has_truth and not with_orientation:
        pass
    # the fallback decision the controller actually relies on, made explicit so
    # a bench run shows *why* a view was accepted or dropped
    try:
        chosen = wizard_detection.prefer_partial(gray, obs_strict)
    except Exception:                                     # pragma: no cover
        chosen = obs_strict
    results["chosen"] = {
        "is_strict": chosen is obs_strict,
        "n": int(len(chosen.points)) if chosen is not None else 0,
    }
    if case.has_truth and chosen is not None:
        results["chosen"].update(identity_score(chosen, case.pixels, case.uv))
    if with_orientation:
        results["chosen"]["orient"] = _orient(gray, chosen, gray.shape)
    results["wizard_matches_chosen"] = (
        (obs_wizard is None and chosen is None)
        or (obs_wizard is not None and chosen is not None
            and set(obs_wizard.points) == set(chosen.points)))
    return results


# ---------------------------------------------------------------------------
# case sources
# ---------------------------------------------------------------------------

def _store_cases(root: Path, max_views: int | None, cam_keys: Iterable[str],
                 families: Sequence[str], clip_fracs: Sequence[float],
                 rotations: Sequence[float], tilts: Sequence[float]) -> Iterator[Case]:
    from tools.wizard import session_io

    store = session_io.ViewStore(root)
    store.reload()
    views = list(store.iter_views())
    golden = []
    for v in views:
        obs = v.get("observation") or {}
        markers = obs.get("markers") or obs.get("n_markers") or {}
        pts = (obs.get("image_points") or {})
        if all(int(markers.get(c) or 0) == 40 for c in CAMERAS) and \
                all(len(pts.get(c) or []) == 40 for c in CAMERAS):
            golden.append(v)
    if max_views:
        # spread the sample over the recorded roll range instead of taking the
        # first N, which would sample one sector only
        golden.sort(key=lambda v: float((v["observation"] or {}).get("roll_deg") or 0.0))
        idx = np.linspace(0, len(golden) - 1, min(max_views, len(golden))).round().astype(int)
        golden = [golden[i] for i in sorted(set(idx.tolist()))]
    for v in golden:
        obs = v["observation"]
        for cam in cam_keys:
            if cam not in v["images"]:
                continue
            pts = np.asarray(obs["image_points"][cam], float)
            if len(pts) != 40:
                continue
            base = Case(f"view{v['record'].get('index', 0):03d}-{cam}", "base", cam,
                        np.ascontiguousarray(v["images"][cam]), _labels(len(pts)), pts,
                        {"roll_deg": obs.get("roll_deg"), "view": v["record"].get("index")})
            if "base" in families:
                yield base
            if "clip_edge" in families or "clip_corner" in families:
                for frac in clip_fracs:
                    if "clip_edge" in families:
                        for side in ("left", "right", "top", "bottom"):
                            for mode in ("mask", "crop"):
                                yield clip_case(base, side, frac, mode=mode)
                    if "clip_corner" in families:
                        for cx in (0, 1):
                            for cy in (0, 1):
                                yield clip_case(base, f"corner_{cx}{cy}", frac, mode="crop")
            if "rotate" in families:
                for deg in rotations:
                    yield rotate_case(base, deg)
            if "tilt" in families:
                K = _intrinsics()
                for deg in tilts:
                    for axis in ("x", "y"):
                        yield tilt_case(base, deg, axis, K=K)


def _decision_reasons(root: Path) -> dict[int, list[str]]:
    """Map a capture frame id (``frame_004651`` -> 4651) to its reasons."""
    path = root / "decisions.jsonl"
    out: dict[int, list[str]] = {}
    if not path.is_file():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
            n = int(str(d.get("label", "")).split()[-1])
        except Exception:
            continue
        out[n] = list(d.get("reasons") or [])
    return out


def bench_rejected_frames(root: Path, *, max_frames: int | None = None) -> Iterator[Case]:
    """The raw frame groups the capture tool retained (real rejected frames)."""
    frames = root / "frames"
    if not frames.is_dir():
        return
    ids = sorted({int(p.name.split("_")[1]) for p in frames.glob("frame_*_cam0.pgm")})
    if max_frames:
        ids = ids[-max_frames:]
    reasons = _decision_reasons(root)
    for fid in ids:
        for cam, key in ((1, "left"), (0, "right")):
            path = frames / f"frame_{fid:06d}_cam{cam}.pgm"
            if not path.is_file():
                continue
            img = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
            if img is None:                                # pragma: no cover
                continue
            yield Case(f"frame{fid}-{key}", "rejected", key,
                       np.ascontiguousarray(img), None, None,
                       {"frame": fid, "reasons": reasons.get(fid, [])})


# ---------------------------------------------------------------------------
# aggregation
# ---------------------------------------------------------------------------

def _family_key(case: dict) -> str:
    meta = case.get("meta") or {}
    fam = case["family"]
    if fam == "clip_edge":
        return f"clip_edge/{meta.get('side')}/{int(meta.get('frac', 0) * 100)}%/{meta.get('mode')}"
    if fam == "clip_corner":
        return f"clip_corner/{meta.get('side')}/{int(meta.get('frac', 0) * 100)}%"
    if fam == "rotate":
        return f"rotate/{int(meta.get('deg', 0))}"
    if fam == "tilt":
        return f"tilt/{meta.get('axis')}/{int(meta.get('deg', 0))}"
    return fam


def _bucket(entry: dict, *, has_truth: bool) -> str:
    if entry is None:
        return "no_run"
    if not entry.get("ok"):
        return "no_detection"
    if has_truth:
        if entry.get("wrong", 0) and entry.get("correct", 0) == 0:
            return "wrong_labels"
        if entry.get("wrong", 0):
            return "partly_wrong_labels"
        if entry.get("correct", 0) == 0:
            return "no_matching_points"
        return "ok"
    return "detected"


def summarise(results: list[dict]) -> dict:
    """Aggregate the per-case results into families and variants."""
    agg: dict[str, Any] = {}
    for r in results:
        case = r["case"]
        key = _family_key(case)
        fam = case["family"]
        slot = agg.setdefault(key, {"family": fam, "cases": 0, "cameras": {},
                                    "variants": {}})
        slot["cases"] += 1
        slot["cameras"][case["camera"]] = slot["cameras"].get(case["camera"], 0) + 1
        has_truth = case["expected"] is not None
        for variant, entry in r["variants"].items():
            v = slot["variants"].setdefault(variant, {
                "cases": 0, "ok": 0, "seconds": 0.0, "expected": 0, "correct": 0,
                "wrong": 0, "mirrored": 0, "n": 0, "buckets": {}})
            v["cases"] += 1
            v["ok"] += int(bool(entry.get("ok")))
            v["seconds"] += float(entry.get("seconds") or 0.0)
            if has_truth:
                v["expected"] += int(case["expected"] or 0)
                v["correct"] += int(entry.get("correct") or 0)
                v["wrong"] += int(entry.get("wrong") or 0)
            else:
                v["n"] += int(entry.get("n") or 0)
            det = entry.get("det_h")
            if det is not None and float(det) <= 0:
                v["mirrored"] += 1
            b = _bucket(entry, has_truth=has_truth)
            v["buckets"][b] = v["buckets"].get(b, 0) + 1
    return agg


def _print_table(agg: dict[str, Any], variants: Sequence[str]) -> None:
    print(f"{'family':34s} {'variant':8s} {'n':>4s} {'det':>4s} {'id-ok':>7s} "
          f"{'wrong':>6s} {'mirror':>6s} {'s/case':>7s}  buckets")
    for key in sorted(agg):
        slot = agg[key]
        for variant in variants:
            v = slot["variants"].get(variant)
            if not v:
                continue
            expected = v["expected"] or 0
            idok = f"{v['correct']}/{expected}" if expected else "-"
            buckets = ",".join(f"{k}:{n}" for k, n in sorted(v["buckets"].items()))
            print(f"{key:34s} {variant:8s} {v['cases']:4d} {v['ok']:4d} {idok:>7s} "
                  f"{v['wrong']:6d} {v['mirrored']:6d} {v['seconds'] / max(v['cases'], 1):7.3f}  {buckets}")


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__ and __doc__.splitlines()[0])
    ap.add_argument("--session", default=str(DEFAULT_SESSION))
    ap.add_argument("--out", default=None, help="write the machine-readable JSON here")
    ap.add_argument("--max-views", type=int, default=8,
                    help="golden views sampled across the recorded roll range")
    ap.add_argument("--max-rejected", type=int, default=None,
                    help="cap the retained rejected frame groups")
    ap.add_argument("--families", default="base,clip_edge,clip_corner,rotate,tilt,rejected")
    ap.add_argument("--variants", default="strict,partial,wizard")
    ap.add_argument("--clip-fracs", default="0.1,0.2,0.3,0.4")
    ap.add_argument("--rotations", default="0,90,180,270")
    ap.add_argument("--tilts", default="10,20")
    ap.add_argument("--cameras", default="left,right")
    ap.add_argument("--no-orientation", action="store_true",
                    help="skip the (slow) witness scoring")
    args = ap.parse_args(argv)

    root = Path(args.session)
    families = [f.strip() for f in args.families.split(",") if f.strip()]
    variants = [v.strip() for v in args.variants.split(",") if v.strip()]
    clip_fracs = [float(x) for x in args.clip_fracs.split(",") if x.strip()]
    rotations = [float(x) for x in args.rotations.split(",") if x.strip()]
    tilts = [float(x) for x in args.tilts.split(",") if x.strip()]
    cams = [c.strip() for c in args.cameras.split(",") if c.strip()]

    results: list[dict] = []
    started = time.perf_counter()
    n = 0
    if root.is_dir() and any(f != "rejected" for f in families):
        for case in _store_cases(root, args.max_views, cams, families, clip_fracs,
                                 rotations, tilts):
            results.append(evaluate_case(case, variants,
                                         with_orientation=not args.no_orientation))
            n += 1
            if n % 50 == 0:
                print(f"  ... {n} cases, {time.perf_counter() - started:.1f}s",
                      file=sys.stderr)
    if "rejected" in families and root.is_dir():
        for case in bench_rejected_frames(root, max_frames=args.max_rejected):
            results.append(evaluate_case(case, variants,
                                         with_orientation=not args.no_orientation))
    agg = summarise(results)
    _print_table(agg, variants)
    payload = {
        "schema": "einstar.partial-bench/1",
        "session": str(root),
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "config": {"families": families, "variants": variants,
                   "clip_fracs": clip_fracs, "rotations": rotations,
                   "tilts": tilts, "cameras": cams,
                   "max_views": args.max_views, "max_rejected": args.max_rejected},
        "cases": results,
        "families": agg,
        "wall_seconds": round(time.perf_counter() - started, 2),
    }
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, indent=1, sort_keys=True))
        print(f"wrote {out}")
    else:
        print(json.dumps({"wall_seconds": payload["wall_seconds"],
                          "n_cases": len(results)}, indent=1))
    return 0


if __name__ == "__main__":                                 # pragma: no cover
    raise SystemExit(main())
