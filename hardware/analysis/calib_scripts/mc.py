import numpy as np, geom as G, model as M
rng = np.random.default_rng(7)
E = M.fit_edges()
SIDES = {'L':'left','R':'right','T':'top','B':'bottom'}
offs = {k: G.sil_at(E[k], SIDES[k]) - E[k]['ref'] for k in 'LRTB'}
C0,H0 = M.build(E,offs)
# grid order: row-major (row i, col j) ; k = i*11 + (j-1)
ST = np.array([M.ball_st(j,i) for i in range(11) for j in range(1,12)])
P0 = M.apply(H0, ST)
def idx(row, col): return M.ROWS.index(row)*11 + (col-1)
sig_edge = {'L':0.8,'R':1.0,'T':0.8,'B':0.8}
N=4000; S=[]
for n in range(N):
    off = dict(offs); bias = rng.normal(0.7,0.5)
    for k in 'LRTB': off[k] += rng.normal(0, sig_edge[k])
    C,H = M.build(E, off, bias=bias)
    S.append(M.apply(H, ST))
S = np.array(S); mu=S.mean(axis=0); sd=S.std(axis=0)
np.savez('/tmp/calib/mc.npz', S=S, P0=P0, sd=sd, mu=mu, ST=ST)
print("1-sigma per ball: x %.2f..%.2f  y %.2f..%.2f px"%(sd[:,0].min(),sd[:,0].max(),sd[:,1].min(),sd[:,1].max()))
print("A1 base %s  MC mean %s"%(np.round(P0[idx('A',1)],2), np.round(mu[idx('A',1)],2)))
sides=[np.linalg.norm(C0[b]-C0[a]) for a,b in [('TL','TR'),('BL','BR'),('TL','BL'),('TR','BR')]]
print("sides", np.round(sides,2), "mean %.2f -> %.3f px/mm"%(np.mean(sides), np.mean(sides)/10))
for nm in ['A1','A3','A4','A5','A6','A9','A10']:
    k = idx(nm[0], int(nm[1:]))
    print(f"{nm:4s} x={P0[k,0]:8.2f} ±{sd[k,0]:.2f}   y={P0[k,1]:8.2f} ±{sd[k,1]:.2f}")
# dot in package mm
DOT = np.array([1509.2, 955.35])
Hinv = np.linalg.inv(H0)
def to_mm(p):
    q = Hinv@np.array([p[0],p[1],1.0]); return 10*np.array([q[0]/q[2], q[1]/q[2]])
mm = to_mm(DOT)
print("dot in package mm (u from col-1 edge, v from row-A edge): %s"%np.round(mm,3))
print("A1 in mm:", np.round(to_mm(P0[idx('A',1)]),3))
print("dot-A1 in mm:", np.round(mm-to_mm(P0[idx('A',1)]),3))
print("dot dist to right edge %.2f mm, to bottom edge %.2f mm"%(mm[0], mm[1]))
