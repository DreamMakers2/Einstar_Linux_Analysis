"""Quick real-corpus held-out comparison (fixed prefix / uniform / random).

This is a reduced, independent check used while the full view-count audit runs.
It does NOT use held-out views for selection or fitting.
"""
import json, pickle, sys
import numpy as np
sys.path.insert(0, '.')
from calib.calibrate import calibrate, Observation, CameraIntrinsics
from calib.ccf import load, relative_pose
from calib.evaluate import heldout_reprojection
from calib.geometry import fundamental, epipolar_residual

det = pickle.load(open('analysis/calibration/engine/vendor_detections.pkl', 'rb'))
allobs = [Observation(cam, i - 1, det[(s, i)])
          for s, cam in [('Left', 'left'), ('Right', 'right'), ('Tex', 'tex')]
          for i in range(1, 26) if (s, i) in det]
cfgs = {c: load(f'sources/calibration/res_Einscan-E10_200x150/{n}CCF.txt')
        for c, n in [('left', 'Left'), ('right', 'Right'), ('tex', 'Tex')]}
init = {c: CameraIntrinsics.from_ccf(r) for c, r in cfgs.items()}
Rccf, tccf = relative_pose(cfgs['left'], cfgs['right'])
base_ccf = float(np.linalg.norm(tccf))
pool = list(range(0, 20))          # 20 training views
heldout = list(range(20, 25))      # 5 independent validation views

def run(obs, label):
    res = calibrate(obs, 27.72, init, free_kc=False, free_intrinsics=True,
                    max_nfev=700, robust_covariance=True)
    R, t = relative_pose(cfgs['left'], cfgs['right'])
    Rir = res.extrinsics['right'][0] @ res.extrinsics['left'][0].T
    ang = float(np.degrees(np.arccos(np.clip((np.trace(Rir.T @ R) - 1) / 2, -1, 1))))
    ho = [heldout_reprojection(allobs, res, 27.72, v) for v in heldout]
    ho = [h['rms_px'] for h in ho if 'rms_px' in h]
    # epipolar Sampson on held-out IR pairs with the fitted rig
    F = fundamental(res.cameras['left'].K, res.cameras['right'].K, Rir,
                    res.extrinsics['right'][1] - Rir @ res.extrinsics['left'][1])
    ds = []
    for v in heldout:
        lo = [o for o in allobs if o.view == v and o.camera == 'left']
        ro = [o for o in allobs if o.view == v and o.camera == 'right']
        if not lo or not ro: continue
        common = sorted(set(lo[0].uv) & set(ro[0].uv))
        if len(common) < 10: continue
        x1 = np.array([lo[0].uv[k] for k in common]); x2 = np.array([ro[0].uv[k] for k in common])
        ds.append(epipolar_residual(F, x1, x2))
    epi = float(np.median(np.concatenate(ds))) if ds else float('nan')
    return {"label": label, "n_views": len(set(o.view for o in obs)),
            "fx": [res.cameras[c].fx for c in ['left', 'right', 'tex']],
            "dfx": [res.cameras[c].fx - init[c].fx for c in ['left', 'right', 'tex']],
            "baseline": float(np.linalg.norm(res.baseline())),
            "dbase": float(np.linalg.norm(res.baseline()) - base_ccf),
            "ir_rot_vs_ccf": ang,
            "rms": res.residual_rms,
            "sigma_fx": [float(np.sqrt(res.covariance[i, i])) for i in [0, 9, 18]],
            "heldout_rms": float(np.mean(ho)) if ho else float('nan'),
            "epipolar_med": epi}

rows = []
for k in (8, 12, 20):
    rows.append(run([o for o in allobs if o.view in pool[:k]], f"prefix{k}"))
    idx = sorted(np.linspace(0, 19, k).round().astype(int).tolist())
    rows.append(run([o for o in allobs if o.view in idx], f"uniform{k}"))
    for seed in range(5):
        rng = np.random.default_rng(seed)
        idx = sorted(rng.choice(pool, size=k, replace=False).tolist())
        rows.append(run([o for o in allobs if o.view in idx], f"random{k}_{seed}"))
rows.append(run([o for o in allobs if o.view < 25], "full25"))
print(json.dumps(rows, indent=1))
json.dump(rows, open('analysis/calibration/audit/real_holdout_quick.json', 'w'), indent=1)
