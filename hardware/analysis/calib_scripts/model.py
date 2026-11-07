import numpy as np, json, geom as G

ROWS = list('ABCDEFGHJK L'.replace(' ',''))   # A..L, no I
COLS = list(range(1,12))

def homography(src, dst):
    A=[]; b=[]
    for (x,y),(u,v) in zip(src,dst):
        A.append([x,y,1,0,0,0,-u*x,-u*y]); b.append(u)
        A.append([0,0,0,x,y,1,-v*x,-v*y]); b.append(v)
    h,*_ = np.linalg.lstsq(np.array(A),np.array(b),rcond=None)
    return np.append(h,1.0).reshape(3,3)

def apply(H, pts):
    p = np.hstack([np.asarray(pts), np.ones((len(pts),1))])
    q = p@H.T
    return q[:,:2]/q[:,2:3]

def affine_fit(src,dst):
    A=[]; b=[]
    for (x,y),(u,v) in zip(src,dst):
        A.append([x,y,1,0,0,0]); b.append(u)
        A.append([0,0,0,x,y,1]); b.append(v)
    c,*_ = np.linalg.lstsq(np.array(A),np.array(b),rcond=None)
    return c.reshape(2,3)

def fit_edges():
    L=G.edge_fit(range(650,956),(1166,1189),'x'); R=G.edge_fit(range(660,936),(1528,1550),'x')
    T=G.edge_fit(range(1200,1526),(616,636),'y'); B=G.edge_fit(range(1200,1526),(982,996),'y')
    return dict(L=L,R=R,T=T,B=B)

def lines(E, offs, bias=0.0):
    """offs: per-side silhouette offset; bias: extra outward shift (px)"""
    L,R,T,B = E['L'],E['R'],E['T'],E['B']
    lw=np.array([L['coef'][0]+offs['L']-bias, L['coef'][1]])
    rw=np.array([R['coef'][0]+offs['R']+bias, R['coef'][1]])
    tw=np.array([T['coef'][0]+offs['T']-bias, T['coef'][1]])
    bw=np.array([B['coef'][0]+offs['B']+bias, B['coef'][1]])
    return lw,rw,tw,bw

def corners(E, offs, bias=0.0):
    lw,rw,tw,bw = lines(E,offs,bias)
    return dict(TL=G.line_pt(lw,tw), TR=G.line_pt(rw,tw), BR=G.line_pt(rw,bw), BL=G.line_pt(lw,bw))

def build(E, offs, bias=0.0):
    C = corners(E,offs,bias)
    src = [(0,0),(1,0),(0,1),(1,1)]
    dst = [C['BR'],C['BL'],C['TR'],C['TL']]
    H = homography(src,dst)
    return C,H

def ball_st(j,i):
    return ((1.0+0.8*(j-1))/10.0, (1.0+0.8*i)/10.0)

if __name__=='__main__':
    E = fit_edges()
    offs = {k: G.sil_at(E[k], {'L':'left','R':'right','T':'top','B':'bottom'}[k]) - E[k]['ref'] for k in 'LRTB'}
    C,H = build(E,offs)
    print("offsets", {k:round(v,3) for k,v in offs.items()})
    for k in ['TL','TR','BR','BL']: print(k, np.round(C[k],3))
    # affine test
    src=[(0,0),(1,0),(0,1),(1,1)]; dst=[C['BR'],C['BL'],C['TR'],C['TL']]
    A = affine_fit(src,dst)
    pred = np.array([A@np.array([s,t,1.0]) for s,t in src])
    res = pred-np.array(dst)
    print("affine residual per corner (px):", np.round(res,3))
    print("affine RMS residual: %.3f px   max %.3f"%(np.sqrt((res**2).sum(axis=1).mean()), np.abs(res).max()))
    # homography exactness
    hp = apply(H,src)
    print("homography corner residual max %.3g px"%np.abs(hp-np.array(dst)).max())
    # affine vs projective at ball points
    st = np.array([ball_st(i,j) for i in range(11) for j in range(11)])
    ph = apply(H,st); pa = np.array([A@np.array([s,t,1.0]) for s,t in st])
    d = np.linalg.norm(ph-pa,axis=1)
    print("affine-vs-projective over grid: mean %.3f  max %.3f px"%(d.mean(), d.max()))
    np.savez('/tmp/calib/model.npz', H=H, A=A, TL=C['TL'],TR=C['TR'],BR=C['BR'],BL=C['BL'])
    print()
    print("scale check: sides", [round(np.linalg.norm(C[b]-C[a]),2) for a,b in [('TL','TR'),('BL','BR'),('TL','BL'),('TR','BR')]])
    for nm in ['A1','A3','A4','A5','A6','A9','A10','L11']:
        j = int(nm[1:]); i = ROWS.index(nm[0])
        s,t = ball_st(j,i)
        p = apply(H,[(s,t)])[0]
        print(f"  {nm}: ({p[0]:8.2f}, {p[1]:8.2f})")
