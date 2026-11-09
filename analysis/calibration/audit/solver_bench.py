"""Solver-backend benchmark for the Einstar 3-camera calibration engine.

Every (case, solver) pair runs in a *fresh* interpreter so peak RSS and wall
time are isolated.  The orchestrator form (``--all``) spawns those children and
collects ``solver_results.json``; ``--one`` is the child mode.

Nothing here writes to ``calib/``; the engine is imported read-only.

Examples
--------
    OPENBLAS_NUM_THREADS=1 python analysis/calibration/audit/solver_bench.py \
        --one --case synth12 --solver scipy_analytic_sparse
    python analysis/calibration/audit/solver_bench.py --all --threads 1
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import resource
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))

import solver_lib as S  # noqa: E402
from calib import calibrate as engine  # noqa: E402
from calib import ccf, plate  # noqa: E402
from calib.calibrate import CAMERAS, CameraIntrinsics, Observation  # noqa: E402
from calib.geometry import project, rvec_from_R, rodrigues  # noqa: E402

MAX_NFEV = 1200
TOL = 1e-10
F_SCALE = 1.0


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def peak_rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def now() -> float:
    return time.perf_counter()


def observations_of(case: S.Case) -> list[Observation]:
    obs = []
    for cam, view, idx, uv in case.rows:
        d = {tuple(plate.lattice_uv()[i]): uv[j] for j, i in enumerate(idx)}
        obs.append(Observation(CAMERAS[cam], view, d))
    return obs


def rig_from_views(case: S.Case, per_cam: dict[int, dict[int, tuple]]) -> list:
    """Average rig extrinsics from per-camera PnP poses (engine's rig_from)."""
    extr = {0: (np.eye(3), np.zeros(3))}
    for c in (1, 2):
        Rs, ts = [], []
        for v, pc in per_cam.get(c, {}).items():
            pl = per_cam.get(0, {}).get(v)
            if pl is None:
                continue
            Rcl = pc[0] @ pl[0].T
            tcl = pc[1] - Rcl @ pl[1]
            Rs.append(Rcl); ts.append(tcl)
        if not Rs:
            extr[c] = (np.eye(3), np.zeros(3))
            continue
        A = np.mean(Rs, axis=0)
        U, _, Vt = np.linalg.svd(A)
        extr[c] = (U @ np.diag([1.0, 1.0, np.linalg.det(U @ Vt)]) @ Vt,
                   np.mean(ts, axis=0))
    return extr


def analyse(x: np.ndarray, case: S.Case) -> dict:
    """RMS/max on the identical observation set, plus readable parameters."""
    fp = S.FastProblem(case)
    r = fp.residuals(x)
    rms, mx = S.rms_of(r)
    out = {"rms_px": rms, "max_px": mx, "cost": float(0.5 * np.sum(r ** 2)),
           "params": S.unpack_rig(x, case.n_views),
           "baseline_mm": S.baseline_mm(x).tolist(),
           "baseline_norm_mm": float(np.linalg.norm(S.baseline_mm(x)))}
    if case.reference is not None:
        ref = {}
        for i, c in enumerate(CAMERAS):
            rec = case.reference[{"left": "Left", "right": "Right",
                                  "tex": "Tex"}[c]]
            K = rec.K()
            ref[c] = {"fx": float(K[0, 0]), "fy": float(K[1, 1]),
                      "cx": float(K[0, 2]), "cy": float(K[1, 2]),
                      "kc": [float(t) for t in rec.dist()]}
        out["reference_ccf"] = ref
        out["delta_vs_ccf"] = {
            c: {"dfx": out["params"]["cameras"][c]["fx"] - ref[c]["fx"],
                "dfy": out["params"]["cameras"][c]["fy"] - ref[c]["fy"],
                "dcx": out["params"]["cameras"][c]["cx"] - ref[c]["cx"],
                "dcy": out["params"]["cameras"][c]["cy"] - ref[c]["cy"]}
            for c in CAMERAS}
    return out


def truth_delta(x: np.ndarray, case: S.Case) -> dict:
    """Parameter error against the synthetic ground truth."""
    if case.truth is None:
        return {}
    out = {}
    for i, c in enumerate(CAMERAS):
        t = case.truth["cameras"][c]
        v = x[9 * i:9 * i + 9]
        out[c] = {"dfx": float(v[0] - t["fx"]), "dfy": float(v[1] - t["fy"]),
                  "dcx": float(v[2] - t["cx"]), "dcy": float(v[3] - t["cy"]),
                  "dkc": [float(a - b) for a, b in zip(v[4:9], t["kc"])],
                  "fx_err_px": float(abs(v[0] - t["fx"])),
                  "cx_err_px": float(abs(v[2] - t["cx"]))}
    for name, s in (("right", 27), ("tex", 33)):
        Rt = np.asarray(case.truth["extrinsics"][name]["R"])
        tt = np.asarray(case.truth["extrinsics"][name]["t"])
        Re = rodrigues(x[s:s + 3])
        te = np.asarray(x[s + 3:s + 6])
        dR = Re @ Rt.T
        ang = float(np.degrees(np.arccos(np.clip((np.trace(dR) - 1) / 2, -1, 1))))
        out[name] = {"rot_err_deg": ang,
                     "trans_err_mm": float(np.linalg.norm(te - tt)),
                     "baseline_err_mm": float(np.linalg.norm(
                         te - tt))}
    return out


# ---------------------------------------------------------------------------
# solvers
# ---------------------------------------------------------------------------

def solve_scipy_engine(case: S.Case) -> dict:
    """The shipped engine, called through its public API."""
    obs = observations_of(case)
    t0 = now()
    res = engine.calibrate(obs, board_pitch_mm=case.pitch_mm,
                           init_cameras=case.init, free_kc=True,
                           robust_loss="huber", f_scale=F_SCALE,
                           max_nfev=MAX_NFEV)
    dt = now() - t0
    x = res.vector()
    a = analyse(x, case)
    return {"solver": "scipy_engine", "runtime_s": dt, "x": x,
            "nfev": res.info["nfev"], "status": res.info["message"],
            "success": res.info["success"], "rms_px": a["rms_px"],
            "analysis": a, "truth_delta": truth_delta(x, case),
            "cov_diag": (np.diag(res.covariance).tolist()
                         if res.covariance is not None else None),
            "param_names": res.info.get("param_names")}


def solve_scipy_breakdown(case: S.Case) -> dict:
    """Engine internals with timers: init / solve / covariance."""
    from scipy.optimize import least_squares
    obs = observations_of(case)
    t0 = now()
    cams0 = engine._initial_intrinsics(obs, case.init, case.obj, True)
    views0, extr0 = engine._initial_poses(obs, cams0, case.obj)
    packer = engine._Packer(case.n_views, True)
    x0 = engine._pack_initial(cams0, extr0, views0, packer)
    prob = engine._Problem(obs, case.obj, case.n_views, True, "huber",
                           F_SCALE, True)
    t_init = now() - t0
    xf0 = x0[prob.free_idx]
    calls = [0]

    def fun(xf):
        calls[0] += 1
        return prob.residuals_free(xf, x0)

    t1 = now()
    res = least_squares(fun, xf0,
                        method="trf", loss="huber", f_scale=F_SCALE,
                        max_nfev=MAX_NFEV, xtol=TOL, ftol=TOL, gtol=TOL,
                        verbose=0)
    t_solve = now() - t1
    x = prob.pack(res.x, x0)
    t2 = now()
    cov, info = engine.covariance(prob, x, prob.residuals(x),
                                 free_intrinsics=True, free_kc=True,
                                 robust=True, f_scale=F_SCALE,
                                 return_info=True)
    t_cov = now() - t2
    a = analyse(x, case)
    return {"solver": "scipy_breakdown", "runtime_s": t_init + t_solve + t_cov,
            "t_init_s": t_init, "t_solve_s": t_solve, "t_cov_s": t_cov,
            "x": x, "nfev": int(res.nfev), "nit": int(getattr(res, "nit", -1)),
            "njev": int(getattr(res, "njev", -1)),
            "status": str(res.message), "success": bool(res.success),
            "rms_px": a["rms_px"], "analysis": a,
            "truth_delta": truth_delta(x, case),
            "cov_rank": info.get("rank"), "cov_condition": info.get("condition"),
            "cov_trace": info.get("trace"),
            "n_residual_evals": calls[0],
            "n_fd_evals_per_jac": int(len(prob.free_idx)) + 1}


def _solve_lsq(case: S.Case, dense: bool) -> dict:
    from scipy.optimize import least_squares
    fp = S.FastProblem(case)
    x0, prob, packer = S.engine_start(case)
    if dense:
        jac = lambda x: fp.jacobian(x).toarray()   # noqa: E731
        kw = dict(tr_solver="exact")
    else:
        jac = fp.jacobian                          # sparse CSR callable
        kw = dict(tr_solver="lsmr")
    t0 = now()
    res = least_squares(fp.residuals, x0, jac=jac, method="trf", loss="huber",
                        f_scale=F_SCALE, max_nfev=MAX_NFEV, xtol=TOL,
                        ftol=TOL, gtol=TOL, verbose=0, **kw)
    t_solve = now() - t0
    x = res.x
    a = analyse(x, case)
    # covariance from the analytic Jacobian: same Huber sandwich as the engine
    t1 = now()
    Ja = fp.jacobian(x)
    r = fp.residuals(x)
    w = np.where(np.abs(r) <= F_SCALE, 1.0,
                 F_SCALE / np.maximum(np.abs(r), 1e-12))
    Jw = Ja.multiply(np.sqrt(w)[:, None]).tocsr()
    A = (Jw.T @ Jw).toarray()
    Jwr = Ja.multiply((w * r)[:, None])   # psi(r) = w*r, matching calibrate.covariance.tocsr()
    B = (Jwr.T @ Jwr).toarray()
    Ainv = np.linalg.pinv(A, rcond=1e-12)
    cov = Ainv @ B @ Ainv
    t_cov = now() - t1
    return {"solver": "scipy_analytic_dense" if dense else "scipy_analytic_sparse",
            "runtime_s": t_solve, "t_solve_s": t_solve, "t_cov_s": t_cov,
            "x": x, "nfev": int(res.nfev), "nit": int(getattr(res, "nit", -1)),
            "njev": int(getattr(res, "njev", -1)), "status": str(res.message),
            "success": bool(res.success), "rms_px": a["rms_px"],
            "analysis": a, "truth_delta": truth_delta(x, case),
            "cov_diag": np.diag(cov).tolist(),
            "cov_trace": float(np.trace(cov))}


def solve_scipy_analytic_dense(case: S.Case) -> dict:
    return _solve_lsq(case, dense=True)


def solve_scipy_analytic_sparse(case: S.Case) -> dict:
    return _solve_lsq(case, dense=False)


def solve_cpp_ref(case: S.Case) -> dict:
    """Native C++ Levenberg-Marquardt (calib/ceres/calib_ref.cpp).

    Same problem, same initial values, same analytic Jacobian, Huber loss and
    Huber-sandwich covariance as the SciPy analytic variant -- but compiled and
    with dense normal equations.  It is the compiled-language data point that
    the (blocked) Ceres prototype cannot currently provide.
    """
    import tempfile
    exe = ROOT / "calib/ceres/calib_ref"
    if not exe.exists():
        raise RuntimeError(f"missing {exe}; run calib/ceres/build.sh")
    fd, path = tempfile.mkstemp(suffix=".txt", prefix="einstar_")
    os.close(fd)
    try:
        r = subprocess.run([sys.executable,
                            str(ROOT / "calib/ceres/export_problem.py"),
                            "--case", case.name, "--out", path],
                           capture_output=True, text=True, cwd=str(ROOT))
        if r.returncode != 0:
            raise RuntimeError("export failed: " + r.stderr[-500:])
        t0 = now()
        p = subprocess.run([str(exe), path], capture_output=True, text=True)
        wall = now() - t0
        if p.returncode != 0:
            raise RuntimeError("calib_ref failed: " + p.stderr[-500:])
        d = json.loads(p.stdout.strip().splitlines()[-1])
    finally:
        os.unlink(path)
    x = np.array(d["x"], float)
    a = analyse(x, case)
    return {"solver": "cpp_ref", "runtime_s": float(d["t_total_s"]),
            "wall_s": wall, "t_solve_s": float(d["t_total_s"]),
            "t_jac_s": float(d["t_jac_s"]), "x": x,
            "nfev": int(d["iterations"]), "nit": int(d["iterations"]),
            "status": str(d["status"]), "success": True,
            "rms_px": a["rms_px"], "analysis": a,
            "truth_delta": truth_delta(x, case),
            "cov_diag": d["cov_diag"],
            "rss_child_mb": resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss / 1024.0,
            "n_residual_evals": int(d["iterations"])}


def solve_scipy_num_sparse(case: S.Case) -> dict:
    """Engine residual + scipy's *sparse* finite-difference Jacobian (no analytic work)."""
    from scipy.optimize import least_squares
    fp = S.FastProblem(case)
    x0, prob, packer = S.engine_start(case)
    pat = prob.jac_sparsity()[:, prob.free_idx]
    t0 = now()
    res = least_squares(prob.residuals_free, x0[prob.free_idx],
                        args=(x0,), method="trf", loss="huber",
                        f_scale=F_SCALE, jac_sparsity=pat, tr_solver="lsmr",
                        max_nfev=MAX_NFEV, xtol=TOL, ftol=TOL, gtol=TOL)
    t = now() - t0
    x = prob.pack(res.x, x0)
    a = analyse(x, case)
    return {"solver": "scipy_num_sparse", "runtime_s": t, "t_solve_s": t,
            "x": x, "nfev": int(res.nfev), "nit": int(getattr(res, "nit", -1)),
            "status": str(res.message), "success": bool(res.success),
            "rms_px": a["rms_px"], "analysis": a,
            "truth_delta": truth_delta(x, case)}


# -- OpenCV -----------------------------------------------------------------

def _cv_points(case: S.Case, cam: int):
    """Per-camera (view -> (obj_idx, uv)) map plus OpenCV point lists."""
    by_view = {}
    for c, view, idx, uv in case.rows:
        if c != cam:
            continue
        by_view[int(view)] = (idx, uv)
    views = sorted(by_view)
    op = [case.obj[by_view[v][0]].astype(np.float32) for v in views]
    ip = [by_view[v][1].astype(np.float32) for v in views]
    return op, ip, views, by_view


def solve_opencv(case: S.Case, fix_pp: bool = False) -> dict:
    import cv2
    t0 = now()
    Ks, Ds, rms_cv, by_view = {}, {}, {}, {}
    rvecs, tvecs = {}, {}
    for cam, name in enumerate(CAMERAS):
        op, ip, views, bv = _cv_points(case, cam)
        by_view[cam] = bv
        K0 = case.init[name].K.copy(); K0[0, 1] = 0.0
        D0 = case.init[name].kc.reshape(1, -1).astype(np.float64)
        flags = cv2.CALIB_USE_INTRINSIC_GUESS
        if fix_pp:
            flags |= cv2.CALIB_FIX_PRINCIPAL_POINT
        rms, K, D, rv, tv = cv2.calibrateCamera(op, ip, S.IMG_SIZE, K0.copy(),
                                                D0.copy(), flags=flags)
        Ks[cam] = np.asarray(K, float); Ds[cam] = np.asarray(D, float).ravel()
        rvecs[cam] = rv; tvecs[cam] = tv
        rms_cv[name] = float(rms)
    t_percam = now() - t0

    # stereo for the IR pair on the views both cameras saw; a view may have a
    # different detected subset per camera, so use the per-view intersection.
    common = sorted(set(by_view[0]) & set(by_view[1]))
    op, lip, rip, n_pairs = [], [], [], []
    for v in common:
        i0, uv0 = by_view[0][v]
        i1, uv1 = by_view[1][v]
        shared = np.intersect1d(i0, i1)
        if len(shared) < 6:
            continue
        m0 = {int(i): uv0[j] for j, i in enumerate(i0)}
        m1 = {int(i): uv1[j] for j, i in enumerate(i1)}
        op.append(case.obj[shared].astype(np.float32))
        lip.append(np.array([m0[int(i)] for i in shared], np.float32))
        rip.append(np.array([m1[int(i)] for i in shared], np.float32))
        n_pairs.append(len(shared))
    t1 = now()
    (rms_st, K1, D1, K2, D2, R, T, E, F) = cv2.stereoCalibrate(
        op, lip, rip, Ks[0].copy(), Ds[0].reshape(1, -1).copy(),
        Ks[1].copy(), Ds[1].reshape(1, -1).copy(), S.IMG_SIZE,
        flags=cv2.CALIB_FIX_INTRINSIC)
    t_stereo = now() - t1
    Ks[0], Ks[1] = np.asarray(K1, float).reshape(3, 3), np.asarray(K2, float).reshape(3, 3)
    Ds[0] = np.asarray(D1, float).ravel()[:5]
    Ds[1] = np.asarray(D2, float).ravel()[:5]
    R = np.asarray(R, float).reshape(3, 3)
    T = np.asarray(T, float).ravel()

    # assemble the engine parameter vector
    x = np.zeros(case.n)
    for cam in range(3):
        K = Ks[cam]
        x[9 * cam:9 * cam + 9] = [K[0, 0], K[1, 1], K[0, 2], K[1, 2],
                                  *Ds[cam][:5]]
    x[27:30] = rvec_from_R(R); x[30:33] = T
    per_cam = {}
    for cam in range(3):
        per_cam[cam] = {int(v): (rodrigues(rvecs[cam][i]), np.asarray(tvecs[cam][i]).ravel())
                        for i, v in enumerate(_cv_points(case, cam)[2])}
    extr = {0: (np.eye(3), np.zeros(3)), 1: (R, T)}
    extr[2] = rig_from_views(case, per_cam)[2]
    x[33:36] = rvec_from_R(extr[2][0]); x[36:39] = extr[2][1]
    for v in range(case.n_views):
        pl = per_cam[0].get(v)
        if pl is not None:
            Rv, tv = pl
        else:
            cam = 1 if v in per_cam[1] else 2
            Rc, tc = extr[cam]
            pc = per_cam[cam][v]
            Rv = Rc.T @ pc[0]; tv = Rc.T @ (pc[1] - tc)
        x[39 + 6 * v:42 + 6 * v] = rvec_from_R(Rv)
        x[42 + 6 * v:45 + 6 * v] = tv

    a = analyse(x, case)
    return {"solver": "opencv_fixpp" if fix_pp else "opencv",
            "runtime_s": t_percam + t_stereo,
            "t_percam_s": t_percam, "t_stereo_s": t_stereo,
            "x": x, "rms_px": a["rms_px"], "analysis": a,
            "rms_cv_per_camera": rms_cv, "rms_cv_stereo": float(rms_st),
            "truth_delta": truth_delta(x, case),
            "opencv_flags": "CALIB_USE_INTRINSIC_GUESS" +
                            ("|CALIB_FIX_PRINCIPAL_POINT" if fix_pp else "") +
                            " ; stereo CALIB_FIX_INTRINSIC",
            "n_stereo_views": len(op), "n_stereo_points": int(sum(n_pairs))}


# ---------------------------------------------------------------------------
# registry / child mode
# ---------------------------------------------------------------------------

SOLVERS = {
    "scipy_engine": solve_scipy_engine,
    "scipy_breakdown": solve_scipy_breakdown,
    "scipy_num_sparse": solve_scipy_num_sparse,
    "scipy_analytic_dense": solve_scipy_analytic_dense,
    "scipy_analytic_sparse": solve_scipy_analytic_sparse,
    "cpp_ref": solve_cpp_ref,
    "opencv": solve_opencv,
    "opencv_fixpp": lambda c: solve_opencv(c, fix_pp=True),
}


def build_case(name: str) -> S.Case:
    if name == "synth12":
        return S.load_synth(n_views=12, seed=0, name="synth12")
    if name == "synth25":
        return S.load_synth(n_views=25, seed=0, name="synth25")
    if name == "vendor25":
        return S.load_vendor("vendor25")
    if name == "vendor9":
        return S.load_vendor("vendor9", max_views=9)
    raise SystemExit(f"unknown case {name}")


def run_one(case_name: str, solver: str, baseline: bool = False) -> dict:
    t_load = now()
    case = build_case(case_name)
    load = now() - t_load
    if baseline:
        return {"case": case_name, "solver": "baseline", "load_s": load,
                "rss_peak_mb": peak_rss_mb(), "m": case.m, "n": case.n,
                "n_views": case.n_views, "n_rows": len(case.rows)}
    fn = SOLVERS[solver]
    out = fn(case)
    out.pop("param_names", None)
    x = out.pop("x", None)
    out.update({"case": case_name, "m": case.m, "n": case.n,
                "n_views": case.n_views, "n_rows": len(case.rows),
                "load_s": load, "rss_peak_mb": peak_rss_mb(),
                "python": platform.python_version(),
                "blas_threads": os.environ.get("OPENBLAS_NUM_THREADS", "default")})
    if x is not None:
        out["x_final"] = [float(t) for t in x]
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--one", action="store_true")
    ap.add_argument("--case", default="synth12")
    ap.add_argument("--solver", default="scipy_engine")
    ap.add_argument("--baseline", action="store_true")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--cases", default="synth12,vendor25")
    ap.add_argument("--solvers", default=",".join(SOLVERS))
    ap.add_argument("--threads", default="1",
                    help="OPENBLAS_NUM_THREADS for children ('default' = inherit)")
    ap.add_argument("--out", default=str(HERE / "solver_results.json"))
    args = ap.parse_args()

    if args.one:
        print(json.dumps(run_one(args.case, args.solver, args.baseline)))
        return

    env = dict(os.environ)
    if args.threads != "default":
        env["OPENBLAS_NUM_THREADS"] = args.threads
        env["OMP_NUM_THREADS"] = args.threads
        env["MKL_NUM_THREADS"] = args.threads
    results = {"env": {"blas_threads": args.threads, "cpu_count": os.cpu_count(),
                       "python": platform.python_version(),
                       "platform": platform.platform()},
               "runs": [], "baselines": {}}
    for case in args.cases.split(","):
        for solver in args.solvers.split(","):
            cmd = [sys.executable, __file__, "--one", "--case", case,
                   "--solver", solver]
            t = now()
            p = subprocess.run(cmd, capture_output=True, text=True, env=env,
                               cwd=str(ROOT))
            dt = now() - t
            if p.returncode != 0:
                print(f"[FAIL] {case}/{solver}: {p.stderr.strip()[-600:]}")
                results["runs"].append({"case": case, "solver": solver,
                                        "error": p.stderr.strip()[-2000:]})
                continue
            rec = json.loads(p.stdout.strip().splitlines()[-1])
            rec["wall_s"] = dt
            results["runs"].append(rec)
            print(f"[ok] {case:9s} {solver:22s} {rec['runtime_s']:8.3f}s "
                  f"rms {rec['rms_px']:.4f} nfev {rec.get('nfev')} "
                  f"rss {rec['rss_peak_mb']:.0f}MB")
        base = subprocess.run([sys.executable, __file__, "--one", "--case", case,
                               "--baseline"], capture_output=True, text=True,
                              env=env, cwd=str(ROOT))
        if base.returncode == 0:
            results["baselines"][case] = json.loads(base.stdout.strip().splitlines()[-1])
    Path(args.out).write_text(json.dumps(results, indent=1))
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
