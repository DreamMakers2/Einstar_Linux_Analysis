"""Fit the colour-camera extrinsic from recorded frames.

The shipped colour record is marked stale: measured on 120 real views it
reprojects the calibration plate with **461 px RMS**, which is not a small
calibration drift but a record that does not describe this rig.  The IR pair is
good, so the colour camera's pose relative to the left IR camera can be solved
directly from the plate:

1. detect the plate in the left IR frame and solve its pose with the IR
   intrinsics (the IR calibration is trusted);
2. detect the plate in the colour frame, **after the debayer and 180 deg
   rotation the reconstruction engine applies**, so the result is in the frame
   the engine samples colour in;
3. solve the colour camera pose from the same board points;
4. fit one relative transform (6 parameters) over all views by minimising the
   colour reprojection error, with a held-out split so the number quoted is not
   the training error.

The output is a report and, optionally, a profile document with the corrected
colour record.  Writing a profile is not activating it: the calibration
lifecycle still decides that.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import cv2
import numpy as np
from scipy.optimize import least_squares

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parents[2]))
sys.path.insert(0, str(_HERE.parents[1]))

from calib import detect, geometry, plate, recon_profile  # noqa: E402
from wizard import types  # noqa: E402

SCHEMA = "einstar.colour-fit/1"


# ---------------------------------------------------------------------------
# detection helpers
# ---------------------------------------------------------------------------

def _board_index():
    points = plate.board_points_mm()
    index = {(float(u), float(v)): i for i, (u, v) in enumerate(plate.lattice_uv())}
    return points, index


def _detect(gray: np.ndarray, index) -> dict:
    obs = detect.detect_grid_auto(np.ascontiguousarray(gray))
    if obs is None:
        return {}
    return {k: v for k, v in obs.points.items()
            if (float(k[0]), float(k[1])) in index}


def _colour_grey(path: Path) -> np.ndarray:
    """Debayer + 180 deg, exactly as the reconstruction engine does."""
    raw = types.read_pgm(path)
    bgr = types.debayer_bgr(raw)
    return types.grey_from_bgr(np.ascontiguousarray(np.rot90(bgr, 2)))


def _pose(obj: np.ndarray, img: np.ndarray, cam):
    return geometry.solve_pnp(obj, img, np.asarray(cam.K, float),
                              np.asarray(cam.kc, float))


# ---------------------------------------------------------------------------
# fit
# ---------------------------------------------------------------------------

def collect_views(frames_dir: Path, profile, *, max_views: int = 200,
                  stride: int = 1) -> list[dict]:
    points, index = _board_index()
    left = profile.camera("ir_left").intrinsics()
    views: list[dict] = []
    ids = sorted({int(p.stem.split("_")[1])
                  for p in Path(frames_dir).glob("frame_*_cam1.pgm")})
    for i in ids[::max(1, int(stride))]:
        ir_path = Path(frames_dir) / f"frame_{i:06d}_cam1.pgm"
        col_path = Path(frames_dir) / f"frame_{i:06d}_cam2.pgm"
        if not (ir_path.is_file() and col_path.is_file()):
            continue
        d_left = _detect(types.orient_left(types.read_pgm(ir_path)), index)
        d_col = _detect(_colour_grey(col_path), index)
        common = [k for k in d_col if k in d_left]
        if len(common) < 12:
            continue
        obj = np.asarray([points[index[(float(u), float(v))]] for u, v in common])
        pose = _pose(obj, np.asarray([d_left[k] for k in common], float), left)
        views.append({"frame": int(i), "pose": pose, "object": obj,
                      "image": np.asarray([d_col[k] for k in common], float),
                      "n_markers": len(common)})
        if len(views) >= max_views:
            break
    return views


def _residuals(params: np.ndarray, views, Kc) -> np.ndarray:
    R = cv2.Rodrigues(np.asarray(params[:3], float))[0]
    T = np.asarray(params[3:6], float)
    out = []
    for view in views:
        pose = view["pose"]
        Xc = (R @ ((pose.R @ view["object"].T).T + pose.t).T).T + T
        pred = geometry.project(np.asarray(Kc.K, float), np.asarray(Kc.kc, float), Xc)
        out.append((pred - view["image"]).ravel())
    return np.concatenate(out) if out else np.zeros(0)


def _rms(params, views, Kc) -> float:
    r = _residuals(params, views, Kc)
    return float(np.sqrt(np.mean(r ** 2))) if r.size else float("nan")


def fit_colour_extrinsic(views, profile, *, holdout: float = 0.5,
                         max_nfev: int = 300) -> dict:
    if len(views) < 8:
        raise ValueError(f"need at least 8 usable views, got {len(views)}")
    cam = profile.camera("color")
    Kc = cam.intrinsics()
    R0 = np.asarray(cam.R, float).reshape(3, 3)
    T0 = np.asarray(cam.T, float).reshape(3)
    x0 = np.concatenate([cv2.Rodrigues(R0)[0].ravel(), T0])
    n_hold = max(1, int(round(len(views) * float(holdout))))
    train = views[: len(views) - n_hold]
    test = views[len(views) - n_hold:]
    before = _rms(x0, views, Kc)
    before_test = _rms(x0, test, Kc)
    fit = least_squares(lambda p: _residuals(p, train, Kc), x0, method="lm",
                        max_nfev=int(max_nfev))
    R1 = cv2.Rodrigues(fit.x[:3])[0]
    T1 = np.asarray(fit.x[3:6], float)
    return {
        "schema": SCHEMA,
        "n_views": len(views),
        "n_train": len(train),
        "n_heldout": len(test),
        "before_rms_px": before,
        "before_heldout_rms_px": before_test,
        "train_rms_px": _rms(fit.x, train, Kc),
        "heldout_rms_px": _rms(fit.x, test, Kc),
        "improvement": (before_test / max(_rms(fit.x, test, Kc), 1e-9)
                        if np.isfinite(before_test) else None),
        "rotation": [[float(x) for x in row] for row in R1],
        "translation": [float(x) for x in T1],
        "stored_rotation": [[float(x) for x in row] for row in R0],
        "stored_translation": [float(x) for x in T0],
        "translation_delta_mm": float(np.linalg.norm(T1 - T0)),
        "rotation_delta_deg": float(np.degrees(np.arccos(np.clip(
            (np.trace(R1 @ R0.T) - 1.0) / 2.0, -1.0, 1.0)))),
        "marker_counts": [v["n_markers"] for v in views],
        "frames": [v["frame"] for v in views],
    }


#: a fitted colour record may be marked usable only if it clears this on
#: held-out views; the number comes from the fit, never from the caller
VALIDATION_MAX_HELDOUT_PX = 1.0
VALIDATION_MIN_HELDOUT_VIEWS = 10


def write_profile(profile_path: Path, fit: dict, out_path: Path,
                  *, validated_status: bool = False) -> Path:
    """Copy the profile with the corrected colour record (not activated).

    ``validated_status`` promotes ``color_status`` to ``ok`` - the only value
    the reconstruction engine accepts for sampling colour - and only when the
    held-out measurement clears the thresholds above.  The evidence is written
    into the profile, so the claim can be audited rather than taken on trust.
    """
    doc = json.loads(Path(profile_path).read_text())
    cameras = doc.get("cameras") or {}
    if "color" not in cameras:
        raise ValueError("profile has no colour camera record")
    cameras["color"]["R"] = [float(x) for x in np.asarray(fit["rotation"]).ravel()]
    cameras["color"]["T"] = [float(x) for x in fit["translation"]]
    status = "fitted"
    if validated_status:
        ok = (fit.get("heldout_rms_px") is not None
              and float(fit["heldout_rms_px"]) <= VALIDATION_MAX_HELDOUT_PX
              and int(fit.get("n_heldout") or 0) >= VALIDATION_MIN_HELDOUT_VIEWS)
        if ok:
            status = "ok"
    doc["color_status"] = status
    provenance = doc.setdefault("provenance", [])
    provenance.append(
        f"colour_extrinsic={status} views={fit['n_views']}"
        f" heldout_rms_px={fit['heldout_rms_px']:.3f}")
    if status == "ok":
        provenance.append(
            f"colour_validation=heldout_rms_px<={VALIDATION_MAX_HELDOUT_PX:g}"
            f" heldout_views>={VALIDATION_MIN_HELDOUT_VIEWS}"
            f" measured={fit['heldout_rms_px']:.3f}/{fit['n_heldout']}")
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(doc, indent=1))
    return out_path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--frames", required=True)
    ap.add_argument("--profile", required=True)
    ap.add_argument("--max-views", type=int, default=200)
    ap.add_argument("--stride", type=int, default=1)
    ap.add_argument("--holdout", type=float, default=0.4)
    ap.add_argument("--out", help="report JSON")
    ap.add_argument("--write-profile", help="write a corrected profile here")
    ap.add_argument("--validated-status", action="store_true",
                    help="mark color_status=ok, but only if the held-out "
                         f"measurement is <= {VALIDATION_MAX_HELDOUT_PX:g} px "
                         f"on >= {VALIDATION_MIN_HELDOUT_VIEWS} views")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    profile = recon_profile.load(args.profile)
    views = collect_views(Path(args.frames), profile, max_views=args.max_views,
                          stride=args.stride)
    if len(views) < 8:
        print(f"only {len(views)} usable views; need at least 8", file=sys.stderr)
        return 2
    fit = fit_colour_extrinsic(views, profile, holdout=args.holdout)
    if args.write_profile:
        path = write_profile(Path(args.profile), fit, Path(args.write_profile),
                             validated_status=bool(args.validated_status))
        fit["profile"] = str(path)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(fit, indent=1))
    if args.json:
        print(json.dumps(fit, indent=1))
    else:
        print(f"views {fit['n_views']} (train {fit['n_train']}, held-out "
              f"{fit['n_heldout']})")
        print(f"stored colour extrinsic: {fit['before_rms_px']:.1f} px  "
              f"(held-out {fit['before_heldout_rms_px']:.1f} px)")
        print(f"fitted: train {fit['train_rms_px']:.2f} px  held-out "
              f"{fit['heldout_rms_px']:.2f} px  "
              f"({fit['improvement']:.0f}x better)")
        print(f"delta: {fit['translation_delta_mm']:.1f} mm / "
              f"{fit['rotation_delta_deg']:.1f} deg")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
