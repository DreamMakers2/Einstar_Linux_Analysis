"""Matched-filter sub-pixel edge localisation for the FX3 package.
Template = median profile across the edge (high SNR). Per-scanline position =
template reference + best shift (parabolic interpolation of the correlation)."""
import numpy as np
from PIL import Image

IMG = '<repo>/hardware/photos/P1_top_side.webp'
im = np.asarray(Image.open(IMG).convert('RGB')).astype(np.float64)
Y = 0.2126*im[:,:,0]+0.7152*im[:,:,1]+0.0722*im[:,:,2]

def half_cross(prof, i0, lo, hi, rising):
    """linear interpolation of the crossing of level L between samples."""
    for i in range(len(prof)-1):
        a, b = prof[i], prof[i+1]
        if (rising and a < hi <= b) or (not rising and a >= hi > b):
            f = (hi-a)/(b-a)
            return i0+i+f
    return None

def analyse(name, axis, scans, win, ref=None, peakwin=None):
    """axis: 'x' scans along y (edge is vertical) ; 'y' scans along x."""
    t = np.arange(win[0], win[1]+1, 1.0)
    # build template (median over scans)
    P = []
    for s in scans:
        row = Y[s, win[0]:win[1]+1] if axis == 'x' else Y[win[0]:win[1]+1, s]
        P.append(row)
    P = np.array(P)
    T = np.median(P, axis=0)
    if peakwin is None:
        peak_i = int(np.argmax(T))
    else:
        peak_i = peakwin - win[0]
    # reference position from the peak (parabolic)
    a, b, c = T[peak_i-1], T[peak_i], T[peak_i+1]
    den = (a - 2*b + c)
    d = 0.5*(a-c)/den if den != 0 else 0.0
    ref_pos = t[peak_i] + d
    # background levels on both sides of the ridge (outer / inner ends of window)
    bg_out = np.median(T[:3]); bg_in = np.median(T[-3:])
    lvl_half = 0.5*(bg_out + b)
    out_cross = half_cross(T, t[0], 0, lvl_half, rising=True)
    in_cross = half_cross(T, t[0], 0, lvl_half, rising=False)
    Tn = T - T.mean()
    pos = []
    for k, s in enumerate(scans):
        p = P[k] - P[k].mean()
        cc = np.correlate(p, Tn, mode='full')
        # valid lags: -(n-1)..(n-1) ; restrict to +-3 samples around 0 lag
        n = len(T)
        mid = n-1
        best = None
        for lag in range(-3, 4):
            v = cc[mid+lag]
            if best is None or v > best[0]:
                best = (v, lag)
        lag = best[1]
        y0, y1, y2 = cc[mid+lag-1], cc[mid+lag], cc[mid+lag+1]
        den = (y0 - 2*y1 + y2)
        dd = 0.5*(y0-y2)/den if den != 0 else 0.0
        pos.append((s, ref_pos + lag + dd))
    pos = np.array(pos)
    A = np.column_stack([np.ones(len(pos)), pos[:,0]])
    coef, *_ = np.linalg.lstsq(A, pos[:,1], rcond=None)
    # robust: 2-pass sigma clip
    for _ in range(3):
        r = pos[:,1] - A@coef
        s_ = r.std()
        keep = np.abs(r) < 2.0*s_
        coef, *_ = np.linalg.lstsq(A[keep], pos[keep,1], rcond=None)
        pos = pos[keep]
        A = np.column_stack([np.ones(len(pos)), pos[:,0]])
    r = pos[:,1] - A@coef
    print(f"{name}: ref={ref_pos:.2f} outcross={out_cross if out_cross is None else round(out_cross,2)} "
          f"incross={in_cross if in_cross is None else round(in_cross,2)} "
          f"bg_out={bg_out:.1f} bg_in={bg_in:.1f} peak={b:.1f} fwhm={(in_cross-out_cross) if None not in (in_cross,out_cross) else -1:.2f}")
    print(f"      line: {name}_pos = {coef[0]:.3f} + {coef[1]:+.5f}*s   n={len(pos)} rms={r.std():.3f}px "
          f"max|r|={np.abs(r).max():.2f}")
    return dict(name=name, coef=coef, ref=ref_pos, out=out_cross, inn=in_cross,
                T=T, t=t, rms=float(r.std()), n=len(pos), bg_out=bg_out, bg_in=bg_in, peak=b)

if __name__ == '__main__':
    np.set_printoptions(precision=2, suppress=True)
    L = analyse('left',   'x', range(650, 956), (1166, 1189))
    R = analyse('right',  'x', range(660, 936), (1528, 1550))
    Tp= analyse('top',    'y', range(1200, 1526), (616, 636))
    B = analyse('bottom', 'y', range(1200, 1526), (980, 1000))
    np.savez('<repo>/hardware/crops/_calib/edges2.npz',
             L=L['coef'], R=R['coef'], T=Tp['coef'], B=B['coef'])
    for nm, d in [('L',L),('R',R),('T',Tp),('B',B)]:
        print(nm, "template:", np.round(d['T'],1))
