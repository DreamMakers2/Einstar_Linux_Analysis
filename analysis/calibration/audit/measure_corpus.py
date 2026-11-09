"""Per-image corpus measurements (offline, no calibration, no baseline).

For every vendor corpus image: 8x5 lattice fit, occupancy, plate margins in
pitch units, hole diameters in pitch units, large/small ratio.  Writes
corpus_metrics.json next to this script.
"""
import sys, os, json, statistics as st
sys.path.insert(0, os.path.dirname(__file__))
import numpy as np, cv2
from ransac_grid import search

CORPUS = '<private-inputs>/vendor-calib-rapid/imageLeft%d.bmp'

def edge(g, Hi, start, d, lo=0.03, hi=1.6, nn=200):
    ts = np.linspace(lo, hi, nn)
    pts = np.array([[start[0] + d[0] * t, start[1] + d[1] * t] for t in ts], np.float32)[None]
    im = cv2.perspectiveTransform(pts, Hi)[0]
    if not ((im[:, 0] >= 1) & (im[:, 0] < g.shape[1] - 2) & (im[:, 1] >= 1) & (im[:, 1] < g.shape[0] - 2)).all():
        return None
    vals = cv2.remap(g, im[:, 0].reshape(-1, 1).astype(np.float32),
                     im[:, 1].reshape(-1, 1).astype(np.float32), cv2.INTER_LINEAR,
                     borderMode=cv2.BORDER_CONSTANT, borderValue=-1).ravel().astype(float)
    vals = cv2.GaussianBlur(vals.reshape(-1, 1), (1, 7), 0).ravel()
    base = np.median(vals[(ts > 0.05) & (ts < 0.35) & (vals > 25) & (vals < 250)])
    if base < 25:
        return None
    half = 0.55 * base
    for k in range(len(vals) - 3):
        if ts[k] > 0.06 and vals[k] < half and vals[k + 1] < half and vals[k + 2] < half:
            return ts[k]
    return None

def main():
    out = {}
    for n in range(1, 26):
        r = search(CORPUS % n, thr=180, amin=10, amax=700)
        if not r.get('ok'):
            continue
        g, H, nc, nr = r['g'], r['H'], r['nc'], r['nr']
        Hi = np.linalg.inv(H)
        rec = {'nc': nc, 'nr': nr, 'coverage': float(r['coverage']),
               'n_detected': int(r['n']), 'n_nodes': int(r['inl'].sum())}
        def side(samples, d):
            v = [edge(g, Hi, s, d) for s in samples]
            v = [x for x in v if x is not None]
            return float(np.median(v)) if len(v) >= 2 else None
        mL = side([(0.0, v) for v in np.linspace(1, nr - 2, 3)], (-1, 0))
        mR = side([(nc - 1, v) for v in np.linspace(1, nr - 2, 3)], (1, 0))
        mU = side([(u, 0.0) for u in np.linspace(1, nc - 2, 3)], (0, -1))
        mD = side([(u, nr - 1.0) for u in np.linspace(1, nc - 2, 3)], (0, 1))
        rec.update(mL=mL, mR=mR, mU=mU, mD=mD)
        if None not in (mL, mR, mU, mD):
            rec['plate_W_pitch'] = (nc - 1) + mL + mR
            rec['plate_H_pitch'] = (nr - 1) + mU + mD
            rec['pitch_mm_if_200x150'] = 0.5 * (200 / rec['plate_W_pitch'] + 150 / rec['plate_H_pitch'])
        # hole diameters in pitch units
        S = 200
        u0, u1, v0, v1 = -1.0, nc + 0.0, -1.8, nr + 0.8
        M = np.array([[S, 0, -S * u0], [0, S, -S * v0], [0, 0, 1]], float) @ H
        warp = cv2.warpPerspective(g, M, (int((u1 - u0) * S), int((v1 - v0) * S)), flags=cv2.INTER_CUBIC)
        bw = (warp > 140).astype(np.uint8)
        n2, lab, stats, cent = cv2.connectedComponentsWithStats(bw, 8)
        small, large, cents = [], [], []
        for k in range(1, n2):
            a = stats[k, cv2.CC_STAT_AREA]
            um = cent[k][0] / S + u0
            vm = cent[k][1] / S + v0
            if not (-0.4 < um < nc - 0.6 and -0.4 < vm < nr - 0.6) or a < 50 or a > 6000:
                continue
            cnts, _ = cv2.findContours((lab == k).astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
            if not cnts:
                continue
            c = max(cnts, key=cv2.contourArea)
            if len(c) >= 5:
                (_, _), (MA, ma), _ = cv2.fitEllipse(c)
                dia = (MA + ma) / 2 / S
                ratio = (ma / MA) if MA > ma else (MA / ma)
                (small if a < 2000 else large).append((dia, ratio))
                cents.append((um, vm, a, 1 if a >= 2000 else 0))
        if small and large:
            rec['d_small_pitch'] = float(np.median([s[0] for s in small]))
            rec['d_large_pitch'] = float(np.median([l[0] for l in large]))
            rec['d_ratio'] = rec['d_large_pitch'] / rec['d_small_pitch']
            rec['roundness_small'] = float(np.median([s[1] for s in small]))
            rec['n_small'] = len(small)
            rec['n_large'] = len(large)
            rec['large_uv'] = [[round(float(c[0]), 3), round(float(c[1]), 3)] for c in cents if c[3] == 1]
        out[n] = rec
    json.dump(out, open(os.path.join(os.path.dirname(__file__), 'corpus_metrics.json'), 'w'), indent=1)
    W = [v['plate_W_pitch'] for v in out.values() if 'plate_W_pitch' in v]
    Hh = [v['plate_H_pitch'] for v in out.values() if 'plate_H_pitch' in v]
    d = [v for v in out.values() if 'd_small_pitch' in v]
    print('images', len(out), 'full-plate', len(W))
    if W:
        print('plate W pitch med %.3f  H pitch med %.3f  aspect %.4f' %
              (st.median(W), st.median(Hh), st.median(W) / st.median(Hh)))
        print('pitch if 200x150: %.2f (W) %.2f (H)' % (200 / st.median(W), 150 / st.median(Hh)))
    if d:
        print('d_small %.3f pitch  d_large %.3f  ratio %.2f  roundness %.2f' %
              (st.median([x['d_small_pitch'] for x in d]), st.median([x['d_large_pitch'] for x in d]),
               st.median([x['d_ratio'] for x in d]), st.median([x['roundness_small'] for x in d])))

if __name__ == '__main__':
    main()
