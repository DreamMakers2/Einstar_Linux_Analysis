#!/usr/bin/env python3
"""Headless driver: identifiability / observability / coverage report.

Three offline jobs, no device access:

``--sweep-report PATH``
    Replay a ``tools/sweep`` analysis JSON as *accepted views* and run the
    coverage gate + guidance on it (this is how the one-axis sweep is shown to
    be not ready).  Only the metrics the sweep report actually measured are
    used; colour is marked "unknown, not required" because that analysis ran
    the IR pair only.

``--result-npz PATH --result-meta PATH``
    Rebuild a :class:`calib.calibrate.CalibrationResult` from
    :func:`save_result` artefacts and report identifiability, marginal σ and
    the observability gate.

``--corpus DIR --profile PATH``
    Run the full pipeline on a vendor corpus (``imageLeft*.bmp`` /
    ``imageRight*.bmp`` / ``imageTex*.bmp``), optionally reusing a pickled
    detection cache produced by ``tools/calib_study``.  This is the only path
    that detects markers and fits, so it is the slow one.

Examples
--------
    .venv/bin/python -m tools.wizard.observability_cli \\
        --result-npz ~/einstar-private/analysis/wizard/vendor25_result.npz \\
        --result-meta ~/einstar-private/analysis/wizard/vendor25_meta.json \\
        --target standard --json /tmp/observability.json

    .venv/bin/python -m tools.wizard.observability_cli \\
        --sweep-report ~/einstar-private/analysis/sweep-b/sweep_report.json

Nothing is written outside the path given to ``--json``.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import pickle
import sys
import time
from typing import Any, Mapping, Sequence

import numpy as np

from calib import plate, recon_profile
from calib.calibrate import CAMERAS, CalibrationResult, CameraIntrinsics, Observation, calibrate

from . import coverage, observability

_PROFILE_NAMES = (("ir_left", "left"), ("ir_right", "right"), ("color", "tex"))
_IMAGE_STEMS = (("Left", "imageLeft"), ("Right", "imageRight"), ("Tex", "imageTex"))


# ---------------------------------------------------------------------------
# sweep report -> accepted views
# ---------------------------------------------------------------------------

def sweep_accepted_views(report_path: str | Path) -> list[dict]:
    """Convert a ``einstar-sweep-analysis/1`` report into accepted views.

    Uses the left IR stream (``stream 1``) pose as ``pose``; ``image_points``
    are the detected marker pixels of each stream; ``shared_markers_ir`` is the
    size of the common ``(u, v)`` set; ``assignment_ok`` is the orientation
    witness of both IR streams.  Colour is reported as *absent from the
    analysis*, not as absent from the scene: ``n_markers["tex"]`` is 0 because
    that analysis never ran the colour detector.
    """
    doc = json.loads(Path(report_path).read_text())
    out = []
    for f in doc.get("frames", []):
        det = f.get("detect") or {}
        left = det.get("1") or {}
        right = det.get("0") or {}
        tex = det.get("2") or {}
        if "1" not in (f.get("R") or {}) or "1" not in (f.get("t") or {}):
            continue
        R = np.asarray(f["R"]["1"], float).reshape(3, 3)
        t = np.asarray(f["t"]["1"], float).reshape(3)

        def pts(node):
            return np.asarray(node.get("pixels") or [], float).reshape(-1, 2)

        uv_l = {tuple(u) for u in (left.get("uv") or [])}
        uv_r = {tuple(u) for u in (right.get("uv") or [])}
        q = (f.get("quality") or {}).get("1") or {}
        out.append({
            "view_id": int(f.get("group", len(out))),
            "pose": {"R": R, "t": t},
            "n_markers": {"left": int(left.get("n_lattice") or 0),
                          "right": int(right.get("n_lattice") or 0),
                          "tex": int(tex.get("n_lattice") or 0)},
            "quality": {"mean": q.get("mean"),
                        "saturation_frac": q.get("sat_frac"),
                        # the sweep's ROI Laplacian variance is a focus measure
                        "focus": q.get("blur_lapvar_roi", q.get("blur_lapvar")),
                        "blur": q.get("blur_lapvar_roi")},
            "image_points": {"left": pts(left), "right": pts(right), "tex": pts(tex)},
            "shared_markers_ir": len(uv_l & uv_r),
            "assignment_ok": bool((left.get("orientation") or {}).get("ok", False)
                                  and (right.get("orientation") or {}).get("ok", False)),
            "timestamp": float(f.get("time_s", 0.0)),
            "colour_measured": bool(tex),
        })
    return out


# ---------------------------------------------------------------------------
# corpus -> observations (the slow path)
# ---------------------------------------------------------------------------

def corpus_observations(corpus_dir: str | Path,
                        detection_cache: str | Path | None = None
                        ) -> tuple[list[Observation], dict]:
    """Detect (or load) the vendor corpus as calibration observations."""
    corpus_dir = Path(corpus_dir)
    info: dict[str, Any] = {"corpus": str(corpus_dir)}
    if detection_cache:
        cache = pickle.loads(Path(detection_cache).read_bytes())
        info["detection_cache"] = str(detection_cache)
        info["n_grids"] = len(cache)
    else:
        from calib.detect import detect_grid_auto
        import cv2
        cache = {}
        failures = []
        t0 = time.perf_counter()
        for title, stem in _IMAGE_STEMS:
            for p in sorted(corpus_dir.glob(f"{stem}*.bmp")):
                view = int(p.stem.replace(stem, ""))
                img = cv2.imread(str(p), cv2.IMREAD_GRAYSCALE)
                if img is None:
                    failures.append(str(p))
                    continue
                obs = detect_grid_auto(img)
                if obs is None:
                    failures.append(str(p))
                    continue
                cache[(title, view)] = dict(obs.points)
        info["failures"] = failures
        info["detection_s"] = time.perf_counter() - t0
    obs = []
    for title, cam in ((t, c) for t, c in (("Left", "left"), ("Right", "right"),
                                           ("Tex", "tex"))):
        for (t2, view), points in sorted(cache.items()):
            if t2 != title:
                continue
            obs.append(Observation(cam, int(view),
                                   {k: np.asarray(v, float) for k, v in points.items()}))
    info["n_observations"] = len(obs)
    info["views"] = sorted({int(o.view) for o in obs})
    return obs, info


def factory_init(profile_path: str | Path) -> dict[str, CameraIntrinsics]:
    prof = recon_profile.load(profile_path)
    return {dst: prof.camera(src).intrinsics() for src, dst in _PROFILE_NAMES}


def fit(observations: Sequence[Observation], profile_path: str | Path,
        pitch: float = plate.DEFAULT_PITCH_MM, *, free_kc: bool = True,
        max_nfev: int = 800) -> CalibrationResult:
    return calibrate(list(observations), board_pitch_mm=pitch,
                     init_cameras=factory_init(profile_path),
                     free_kc=free_kc, free_intrinsics=True, max_nfev=max_nfev)


# ---------------------------------------------------------------------------
# result persistence (so the report is reproducible without re-fitting)
# ---------------------------------------------------------------------------

def save_result(result: CalibrationResult, meta_path: str | Path,
                npz_path: str | Path, extra: Mapping[str, Any] | None = None) -> dict:
    meta = {
        "rms_px": result.residual_rms, "max_px": result.residual_max,
        "n_observations": result.n_observations,
        "n_parameters": result.n_parameters,
        "pitch_mm": result.info.get("pitch_mm", plate.DEFAULT_PITCH_MM),
        "cameras": {c: result.cameras[c].as_dict() for c in CAMERAS},
        "extrinsics": {c: {"R": result.extrinsics[c][0].tolist(),
                           "t": result.extrinsics[c][1].tolist()} for c in CAMERAS},
        "info": {k: v for k, v in result.info.items()
                 if k not in ("free_mask", "param_names", "view_remap")},
        "free_mask": result.info.get("free_mask"),
        "param_names": result.info.get("param_names"),
        "views_used": result.info.get("views_used"),
        "views": [{"R": R.tolist(), "t": t.tolist()} for R, t in result.views],
        "extra": dict(extra or {}),
    }
    npz_path = Path(npz_path)
    npz_path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        npz_path,
        covariance=result.covariance if result.covariance is not None else np.zeros((0, 0)),
        information=result.information if result.information is not None else np.zeros((0, 0)),
        marginal_information=(result.marginal_information
                              if result.marginal_information is not None
                              else np.zeros((0, 0))))
    Path(meta_path).write_text(json.dumps(meta, indent=1))
    return meta


def load_result(meta_path: str | Path, npz_path: str | Path) -> CalibrationResult:
    meta = json.loads(Path(meta_path).read_text())
    z = np.load(npz_path, allow_pickle=True)

    def arr(name):
        a = z[name]
        return None if a.size == 0 else np.array(a, copy=True)

    cams = {}
    for c in CAMERAS:
        d = meta["cameras"][c]
        cams[c] = CameraIntrinsics(d["fx"], d["fy"], d["cx"], d["cy"],
                                   np.asarray(d["kc"], float), float(d.get("skew", 0.0)))
    extr = {c: (np.asarray(meta["extrinsics"][c]["R"], float).reshape(3, 3),
                np.asarray(meta["extrinsics"][c]["t"], float).reshape(3))
            for c in CAMERAS}
    views = [(np.asarray(v["R"], float).reshape(3, 3),
              np.asarray(v["t"], float).reshape(3)) for v in meta.get("views", [])]
    info = dict(meta.get("info") or {})
    info["param_names"] = meta.get("param_names")
    info["free_mask"] = meta.get("free_mask")
    info["views_used"] = meta.get("views_used")
    return CalibrationResult(cams, extr, views, arr("covariance"),
                             float(meta.get("rms_px", float("nan"))),
                             float(meta.get("max_px", float("nan"))),
                             int(meta.get("n_observations", 0)),
                             int(meta.get("n_parameters", 0)),
                             "huber", info,
                             information=arr("information"),
                             marginal_information=arr("marginal_information"))


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------

def report(result: CalibrationResult, *, coverage_measurement: Mapping[str, Any] | None,
           target: str = "standard", observations: Sequence[Observation] | None = None,
           compute_model_sensitivity: bool = False) -> dict:
    sig = observability.marginal_sigmas(result)
    idf = observability.identifiability(result)
    gate = observability.observability_gate(
        result, coverage_measurement, target=target, observations=observations,
        compute_model_sensitivity=compute_model_sensitivity)
    explain: dict = {}
    nxt = observability.next_parameter_release(result, gate,
                                               coverage=coverage_measurement,
                                               explain=explain)
    out = {
        "schema": "einstar-wizard-observability/1",
        "n_views": len(result.info.get("views_used") or result.views),
        "residual_rms_px": result.residual_rms,
        "free_rig_parameters": sig.n_free,
        "sigmas": dict(sig),
        "sigma_worst": sig.worst,
        "sigma_source": sig.source,
        "sigma_cross_check": sig.cross_check_param_std_max_rel_diff,
        "identifiability": idf,
        "gate": gate,
        "next_release": nxt,
        "release": explain,
        "runtime": {"marginal_sigmas_ms": sig.runtime_ms,
                    "identifiability_ms": idf.get("runtime_ms"),
                    "gate_ms": gate.get("runtime_ms")},
    }
    if coverage_measurement is not None:
        out["coverage"] = {"readiness": coverage.readiness(coverage_measurement),
                           "dimensions": {d: {"satisfied": coverage_measurement[d]["satisfied"],
                                              "hint": coverage_measurement[d]["hint"]}
                                          for d in coverage.DIMENSIONS}}
    return out


def _print_human(rep: Mapping[str, Any]) -> None:
    print(f"views={rep['n_views']}  rms={rep['residual_rms_px']:.4f} px  "
          f"free rig params={rep['free_rig_parameters']}")
    idf = rep["identifiability"]
    print(f"condition_scaled={idf.get('condition_scaled'):.4g}  "
          f"rank={idf.get('rank')}/{idf.get('n_free')}  "
          f"eigen tail={['%.3g' % x for x in idf.get('eigen_tail', [])[-4:]]}")
    for p in idf.get("top_correlations", []):
        print(f"  corr {p['a']:>12s} <-> {p['b']:<12s} {p['corr']:+.4f}")
    wk = idf.get("weakest") or {}
    if wk.get("dominant"):
        print("  weakest direction: " + ", ".join(f"{d['param']}({d['component']:+.2f})"
                                                   for d in wk["dominant"][:4]))
    g = rep["gate"]
    print(f"gate passed={g['passed']}  counts={g['counts']}  "
          f"runtime={g['runtime_ms']:.1f} ms")
    for name, node in g["gates"].items():
        print(f"  gate {name:<18s} {node['status']:<14s} pass={node['pass']}")
    for r in g["reasons"]:
        print("  reason:", r)
    if rep.get("coverage"):
        print("coverage:", rep["coverage"]["readiness"]["summary"])
        for line in rep["coverage"]["readiness"]["unsatisfied"]:
            print("  -", line)
    print("next release:", rep["next_release"])
    if rep.get("release"):
        print("  justify:", rep["release"]["justify"])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sweep-report", type=Path)
    ap.add_argument("--sweep-max-frames", type=int, default=0,
                    help="only the first N frames of the sweep report (0 = all)")
    ap.add_argument("--result-npz", type=Path)
    ap.add_argument("--result-meta", type=Path)
    ap.add_argument("--corpus", type=Path)
    ap.add_argument("--profile", type=Path)
    ap.add_argument("--detections-cache", type=Path)
    ap.add_argument("--pitch", type=float, default=plate.DEFAULT_PITCH_MM)
    ap.add_argument("--target", default="standard",
                    choices=sorted(observability.ACCURACY_TARGETS))
    ap.add_argument("--model-sensitivity", action="store_true",
                    help="re-fit with/without free distortion (slow)")
    ap.add_argument("--json", type=Path)
    args = ap.parse_args(argv)

    if args.sweep_report:
        views = sweep_accepted_views(args.sweep_report)
        if args.sweep_max_frames:
            views = views[:int(args.sweep_max_frames)]
        spec = coverage.CoverageSpec(require_colour=False)
        m = coverage.measure(views, spec)
        ready = coverage.readiness(m)
        out = {"schema": "einstar-wizard-coverage/1",
               "source": str(args.sweep_report), "n_views": len(views),
               "readiness": ready,
               "dimensions": {d: {"satisfied": m[d]["satisfied"],
                                  "measured": m[d]["measured"],
                                  "hint": m[d]["hint"]} for d in coverage.DIMENSIONS},
               "guidance": coverage.guidance_target(m, views[-1]["pose"] if views else None)}
        print(f"sweep report: {len(views)} accepted views")
        print("ready:", ready["ready"])
        for r in ready["reasons"]:
            print(f"  - {r['dimension']} {r['label']}: {r['hint']}")
        print("guidance:", out["guidance"]["mode"], "|", out["guidance"]["instruction"])
        if args.json:
            args.json.parent.mkdir(parents=True, exist_ok=True)
            args.json.write_text(json.dumps(out, indent=1, default=str))
            print("wrote", args.json)
        return 0

    if args.result_npz and args.result_meta:
        result = load_result(args.result_meta, args.result_npz)
    elif args.corpus and args.profile:
        obs, info = corpus_observations(args.corpus, args.detections_cache)
        t0 = time.perf_counter()
        result = fit(obs, args.profile, args.pitch)
        print(f"fit: {time.perf_counter() - t0:.2f} s  rms={result.residual_rms:.4f} px")
        print("detections:", json.dumps(info)[:400])
    else:
        ap.error("give --sweep-report, or --result-npz/--result-meta, "
                 "or --corpus/--profile")

    cov = None
    if args.sweep_report:
        cov = coverage.measure(sweep_accepted_views(args.sweep_report),
                               coverage.CoverageSpec(require_colour=False))
    obs = None
    if args.model_sensitivity and args.corpus and args.profile:
        obs = corpus_observations(args.corpus, args.detections_cache)[0]
    rep = report(result, coverage_measurement=cov, target=args.target,
                 observations=obs, compute_model_sensitivity=args.model_sensitivity)
    _print_human(rep)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(rep, indent=1, default=str))
        print("wrote", args.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
