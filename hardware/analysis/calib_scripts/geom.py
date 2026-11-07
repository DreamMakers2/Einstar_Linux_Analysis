import numpy as np
from PIL import Image
IMG = '<repo>/hardware/photos/P1_top_side.webp'
im = np.asarray(Image.open(IMG).convert('RGB')).astype(np.float64)
Y = 0.2126*im[:,:,0]+0.7152*im[:,:,1]+0.0722*im[:,:,2]
D = im[:,:,0]-im[:,:,1]

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
    pk = int(np.argmax(T)); a,b,c = T[pk-1],T[pk],T[pk+1]; den=a-2*b+c
    ref = t[pk] + (0.5*(a-c)/den if den else 0.0)
    Tn = T-T.mean(); pos=[]
    for k,s in enumerate(scans):
        p = P[k]-P[k].mean(); cc = np.correlate(p,Tn,mode='full'); mid=len(T)-1
        lag = max(range(-3,4), key=lambda L: cc[mid+L])
        y0,y1,y2 = cc[mid+lag-1],cc[mid+lag],cc[mid+lag+1]; den=y0-2*y1+y2
        pos.append((s, ref+lag+(0.5*(y0-y2)/den if den else 0.0)))
    pos = np.array(pos)
    for _ in range(3):
        A = np.column_stack([np.ones(len(pos)), pos[:,0]])
        coef,*_ = np.linalg.lstsq(A,pos[:,1],rcond=None)
        r = pos[:,1]-A@coef; pos = pos[np.abs(r)<2.0*r.std()]
    A = np.column_stack([np.ones(len(pos)), pos[:,0]])
    coef,*_ = np.linalg.lstsq(A,pos[:,1],rcond=None)
    r = pos[:,1]-A@coef; n=len(pos); s2=(r@r)/(n-2)
    cov = s2*np.linalg.inv(A.T@A)
    return dict(T=T,t=t,ref=ref,pos=pos,coef=coef,cov=cov,rms=r.std())

def sil_at(d, side, frac=0.10):
    T_=d['T']; t_=d['t']; peak=T_.max(); pk=int(np.argmax(T_))
    if side in ('left','top'):
        bg=np.median(T_[:3]); lvl=bg+frac*(peak-bg); return _cross(T_[:pk+1],t_[0],lvl,True)
    else:
        bg=np.median(T_[-3:]); lvl=bg+frac*(peak-bg); return _cross(T_[pk:],t_[pk],lvl,False)

def line_pt(vline, hline):
    a,b = vline; c,dd = hline
    y = (c+dd*a)/(1-dd*b); return np.array([a+b*y, y])
