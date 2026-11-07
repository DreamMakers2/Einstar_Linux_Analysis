import numpy as np
from PIL import Image
im=np.asarray(Image.open('hardware/photos/P1_top_side.webp').convert('RGB')).astype(float)
R,G,B=im[:,:,0],im[:,:,1],im[:,:,2]
L=0.299*R+0.587*G+0.114*B

def peaks(y,x0=1270,x1=1460,r=15,thr=8):
    seg=L[y,x0:x1]
    pad=np.pad(seg,(r,r),mode='edge')
    med=np.array([np.median(pad[i:i+2*r+1]) for i in range(len(seg))])
    d=seg-med
    out=[]
    for i in range(2,len(d)-2):
        if d[i]==max(d[i-2:i+3]) and d[i]>thr:
            den=d[i-1]-2*d[i]+d[i+1]
            off=0.5*(d[i-1]-d[i+1])/den if abs(den)>1e-9 else 0
            out.append((x0+i+np.clip(off,-.5,.5),d[i]))
    m=[]
    for p in out:
        if m and p[0]-m[-1][0]<5:
            if p[1]>m[-1][1]: m[-1]=p
        else: m.append(list(p))
    return m

# collect rows 1008-1026 between the two silk lines
rows=range(1008,1027)
tracks=[]
for y in rows:
    for x,a in peaks(y):
        best=None
        for t in tracks:
            pred=t['a']+t['b']*y
            if abs(x-pred)<3.0 and y-t['ys'][-1]<=2:
                if best is None or abs(x-pred)<best[0]: best=(abs(x-pred),t)
        if best:
            t=best[1]; t['xs'].append(x); t['ys'].append(y); t['amps'].append(a)
            A=np.polyfit(t['ys'],t['xs'],1); t['b'],t['a']=A[0],A[1]
        else:
            tracks.append({'xs':[x],'ys':[y],'amps':[a],'a':x,'b':0.0})
tracks=[t for t in tracks if len(t['ys'])>=10]
tracks.sort(key=lambda t:t['a']+t['b']*1017)
print(f"{len(tracks)} tracks over {len(list(rows))} rows")
for i,t in enumerate(tracks):
    ys=np.array(t['ys']); xs=np.array(t['xs'])
    A=np.polyfit(ys,xs,1); b,a=A
    resid=xs-(a+b*ys)
    print(f"T{i}: x= {a:.2f} + ({b:+.4f})*y   n={len(ys)} rms={resid.std():.2f}px  amp={np.mean(t['amps']):.1f}")
    for yy in [992,955,925]:
        print(f"      x(y={yy}) = {a+b*yy:.1f}")
print("\nBall x for reference: A6=1356.5 A5=1386.1 A4=1415.7 A3=1445.3  B3=1445.4")
