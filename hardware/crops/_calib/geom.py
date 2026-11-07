"""Geometry: edge lines -> corners -> homography -> ball grid."""
import numpy as np
from PIL import Image

IMG = '<repo>/hardware/photos/P1_top_side.webp'
im = np.asarray(Image.open(IMG).convert('RGB')).astype(np.float64)
Y = 0.2126*im[:,:,0]+0.7152*im[:,:,1]+0.0722*im[:,:,2]

def _cross(prof, i0, level, rising):
    for i in range(len(prof)-1):
        a, b = prof[i], prof[i+1]
        if (rising and a < level <= b) or (not rising and a >= level > b):
            return i0 + i + (level-a)/(b-a)
    return None

def edge_fit(scans, win, axis):
    P = np.array([Y[s, win[0]:win[1]+1] if axis=='x' else Y[win[0]:win[1]+1, s] for s in scans])
    T = np.median(P, axis=0)
    t = np.arange(win[0], win[1]+1, 1.0)
    pk = int(np.argmax(T))
    a,b,c = T[pk-1],T[pk],T[pk+1]; den = a-2*b+c
    ref = t[pk] + (0.5*(a-c)/den if den else 0.0)
    Tn = T - T.mean(); pos=[]
    for k,s in enumerate(scans):
        p = P[k]-P[k].mean()
        cc = np.correlate(p, Tn, mode='full'); mid = len(T)-1
        lag = max(range(-3,4), key=lambda L: cc[mid+L])
        y0,y1,y2 = cc[mid+lag-1],cc[mid+lag],cc[mid+lag+1]
        den = y0-2*y1+y2
        pos.append((s, ref + lag + (0.5*(y0-y2)/den if den else 0.0)))
    pos = np.array(pos)
    for _ in range(3):
        A = np.column_stack([np.ones(len(pos)), pos[:,0]])
        coef,*_ = np.linalg.lstsq(A,pos[:,1],rcond=None)
        r = pos[:,1]-A@coef
        keep = np.abs(r) < 2.0*r.std()
        pos, resid = pos[keep], (pos[:,1]-A@coef)[keep]
    A = np.column_stack([np.ones(len(pos)), pos[:,0]])
    coef,*_ = np.linalg.lstsq(A,pos[:,1],rcond=None)
    r = pos[:,1]-A@coef
    n = len(pos); s2 = (r@r)/(n-2)
    cov = s2*np.linalg.inv(A.T@A)
    # silhouette offsets on the template
    bgA = np.median(T[:3]); bgB = np.median(T[-3:])
    if axis=='x':   # left: outside is low index; right: outside is high index
        pass
    return dict(T=T, t=t, ref=ref, pos=pos, coef=coef, cov=cov, rms=r.std(), bgA=bgA, bgB=bgB, peak=b)

def silhouette(T, t, side):
    """side in {'left','right','top','bottom'} - package interior direction."""
    peak = T.max(); pk = int(np.argmax(T))
    if side in ('left','top'):
        bg_out = np.median(T[:3]); lvl = bg_out + 0.10*(peak-bg_out)
        x = _cross(T[:pk+1], t[0], lvl, True)
    else:
        bg_out = np.median(T[-3:]); lvl = bg_out + 0.10*(peak-bg_out)
        x = _cross(T[pk:], t[pk], lvl, False)
    return x, bg_out, lvl, peak

def line_from(coef):
    return coef  # value = c0 + c1*s

def intersect(vline, hline):
    """vline: x = a + b*y ; hline: y = c + d*x"""
    a,b = vline; c,d = hline
    y = (c + d*a)/(1 - d*b); x = a + b*y
    return np.array([x,y])
