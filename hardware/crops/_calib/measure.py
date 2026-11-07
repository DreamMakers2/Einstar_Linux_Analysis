"""Sub-pixel edge measurement of the CYUSB3014 (121-FBGA) package in P1_top_side.webp.
Model: bright "bevel" ridge (Gaussian on a linear background) on the left/top/bottom,
and a luminance step (erfc) on the shadowed right side."""
import numpy as np
from PIL import Image
from math import erfc, sqrt

IMG = '<repo>/hardware/photos/P1_top_side.webp'
im = np.asarray(Image.open(IMG).convert('RGB')).astype(np.float64)
H, W = im.shape[:2]
Y = 0.2126*im[:,:,0]+0.7152*im[:,:,1]+0.0722*im[:,:,2]
D = im[:,:,0]-im[:,:,1]

def _grid_solve(X, p):
    """batched least squares: X (n,3,G), p (n,) -> coefs (G,3), ss (G,)"""
    XtX = np.einsum('gkn,gln->gkl', X, X)
    Xtp = np.einsum('gkn,n->gk', X, p)
    c = np.linalg.solve(XtX, Xtp[..., None])[..., 0]
    ss = (p*p).sum() - 2*np.einsum('gk,gk->g', c, Xtp) + np.einsum('gk,gkl,gl->g', c, XtX, c)
    return c, np.maximum(ss, 0)

def ridge_fit(t, p, tc0, win, sigmas=np.arange(0.8, 4.05, 0.1), half=4.0, coarse=0.1):
    m = (t >= win[0]) & (t <= win[1]); tt, pp = t[m], p[m]
    tcs = np.arange(tc0-half, tc0+half+1e-9, coarse)
    TC, SG = np.meshgrid(tcs, sigmas, indexing='ij')
    tc = TC.ravel(); sg = SG.ravel()
    G = np.exp(-((tt[None, :]-tc[:, None])**2)/(2*sg[:, None]**2))
    X = np.stack([np.ones_like(G), (tt[None, :]-tc[:, None])*np.ones_like(G), G], axis=1)
    c, ss = _grid_solve(X, pp)
    i = int(np.argmin(ss))
    # second, finer pass
    tcs2 = np.arange(tc[i]-0.15, tc[i]+0.15001, 0.01)
    sgs2 = np.arange(max(0.5, sg[i]-0.2), sg[i]+0.20001, 0.02)
    TC, SG = np.meshgrid(tcs2, sgs2, indexing='ij')
    tc2 = TC.ravel(); sg2 = SG.ravel()
    G = np.exp(-((tt[None, :]-tc2[:, None])**2)/(2*sg2[:, None]**2))
    X = np.stack([np.ones_like(G), (tt[None, :]-tc2[:, None])*np.ones_like(G), G], axis=1)
    c2, ss2 = _grid_solve(X, pp)
    j = int(np.argmin(ss2))
    return dict(tc=tc2[j], sigma=sg2[j], b=c2[j, 0], m=c2[j, 1], A=c2[j, 2],
                rms=float(np.sqrt(ss2[j]/len(tt))))

def step_fit(t, p, x00, win, sigmas=np.arange(0.6, 4.05, 0.1), half=4.0, coarse=0.1):
    m = (t >= win[0]) & (t <= win[1]); tt, pp = t[m], p[m]
    xs = np.arange(x00-half, x00+half+1e-9, coarse)
    XS, SG = np.meshgrid(xs, sigmas, indexing='ij')
    xs = XS.ravel(); sg = SG.ravel()
    Z = (tt[None, :]-xs[:, None])/(sqrt(2)*sg[:, None])
    S = 0.5*np.array([[erfc(v) for v in row] for row in Z])
    X = np.stack([np.ones_like(S), (tt[None, :]-xs[:, None])*np.ones_like(S), S], axis=1)
    c, ss = _grid_solve(X, pp)
    i = int(np.argmin(ss))
    xs2 = np.arange(xs[i]-0.15, xs[i]+0.15001, 0.01)
    sgs2 = np.arange(max(0.4, sg[i]-0.2), sg[i]+0.20001, 0.02)
    XS, SG = np.meshgrid(xs2, sgs2, indexing='ij')
    xs2 = XS.ravel(); sg2 = SG.ravel()
    Z = (tt[None, :]-xs2[:, None])/(sqrt(2)*sg2[:, None])
    S = 0.5*np.array([[erfc(v) for v in row] for row in Z])
    X = np.stack([np.ones_like(S), (tt[None, :]-xs2[:, None])*np.ones_like(S), S], axis=1)
    c2, ss2 = _grid_solve(X, pp)
    j = int(np.argmin(ss2))
    return dict(x0=xs2[j], sigma=sg2[j], B=c2[j, 0], m=c2[j, 1], dA=c2[j, 2],
                rms=float(np.sqrt(ss2[j]/len(tt))))

def measure():
    out = {}
    L, R, T, B = [], [], [], []
    for y in range(645, 961):
        r = ridge_fit(np.arange(1162, 1194, 1.0), Y[y,1162:1194], 1176.5, (1166, 1189))
        L.append((y, r['tc'], r['sigma'], r['A'], r['rms']))
    for y in range(655, 941):
        r = step_fit(np.arange(1528, 1552, 1.0), Y[y,1528:1552], 1540.0, (1529, 1550))
        R.append((y, r['x0'], r['sigma'], r['dA'], r['rms']))
    for x in range(1195, 1531):
        r = ridge_fit(np.arange(616, 640, 1.0), Y[616:640,x], 626.3, (618, 636))
        T.append((x, r['tc'], r['sigma'], r['A'], r['rms']))
    for x in range(1195, 1531):
        r = ridge_fit(np.arange(978, 1002, 1.0), Y[978:1002,x], 988.5, (981, 997))
        B.append((x, r['tc'], r['sigma'], r['A'], r['rms']))
    out = dict(L=np.array(L), R=np.array(R), T=np.array(T), B=np.array(B))
    return out

if __name__ == '__main__':
    r = measure()
    np.savez('<repo>/hardware/crops/_calib/edges.npz', **r)
    names = dict(L='left(x)', R='right(x)', T='top(y)', B='bottom(y)')
    for k, a in r.items():
        good = a[:, 4] < 3.0
        print(f"{k:2s} {names[k]:11s} n={len(a):4d} ok={good.sum():4d} "
              f"mean={a[good,1].mean():9.3f} sd={a[good,1].std():6.3f} "
              f"p16={np.percentile(a[good,1],16):9.3f} p84={np.percentile(a[good,1],84):9.3f} "
              f"sig={a[good,2].mean():5.2f} amp={np.abs(a[good,3]).mean():6.1f} rms={a[good,4].mean():5.2f}")
