import numpy as np
from PIL import Image

im=np.asarray(Image.open('hardware/photos/P1_top_side.webp').convert('RGB')).astype(np.float64)
R,G,B=im[:,:,0],im[:,:,1],im[:,:,2]
L=0.299*R+0.587*G+0.114*B

def peaks_row(y,x0=1240,x1=1470,r=20,thr=5.0):
    seg=L[y,x0:x1]
    pad=np.pad(seg,(r,r),mode='edge')
    med=np.array([np.median(pad[i:i+2*r+1]) for i in range(len(seg))])
    d=seg-med
    out=[]
    for i in range(2,len(d)-2):
        if d[i]==max(d[i-2:i+3]) and d[i]>thr:
            den=(d[i-1]-2*d[i]+d[i+1])
            off=0.5*(d[i-1]-d[i+1])/den if abs(den)>1e-9 else 0
            off=np.clip(off,-0.5,0.5)
            out.append((x0+i+off,float(d[i])))
    m=[]
    for p in out:
        if m and p[0]-m[-1][0]<4:
            if p[1]>m[-1][1]: m[-1]=p
        else: m.append(list(p))
    return m

P=[]
for y in range(1004,1102):
    for x,a in peaks_row(y):
        P.append((x,y,a))
P=np.array(P)
print("peaks:",len(P))

best=None
for m in np.arange(0.60,1.20,0.002):
    c=P[:,0]+m*P[:,1]
    hist,edges=np.histogram(c,bins=np.arange(c.min()-1,c.max()+2,1.0))
    score=hist.max()
    if best is None or score>best[0]: best=(score,m,edges[np.argmax(hist)]+0.5)
print("best slope m=%.3f c=%.1f score=%d"%(best[1],best[2],best[0]))

# refine: for the best slope, list histogram peaks
m=best[1]
c=P[:,0]+m*P[:,1]
hist,edges=np.histogram(c,bins=np.arange(c.min()-1,c.max()+2,1.0))
# smooth
sm=np.convolve(hist,[1,2,3,2,1],mode='same')
cands=[]
for i in range(3,len(sm)-3):
    if sm[i]==max(sm[i-3:i+4]) and sm[i]>8:
        cands.append((edges[i]+0.5, sm[i], hist[i]))
print("line candidates (c, smoothed count, raw count):")
for c0,s,h in cands:
    sel=np.abs(c-c0)<2.0
    ys=P[sel,1]
    print(f"  c={c0:7.1f}  score={s:5.1f}  n={h:4d}  yrange={ys.min():.0f}-{ys.max():.0f}  x@y1012={(c0-m*1012):.1f}  x@y1040={(c0-m*1040):.1f}  x@y1080={(c0-m*1080):.1f}")
