import numpy as np
from PIL import Image
import sys

im=np.asarray(Image.open('hardware/photos/P1_top_side.webp').convert('RGB')).astype(np.float64)
R,G,B=im[:,:,0],im[:,:,1],im[:,:,2]
L=0.299*R+0.587*G+0.114*B

def local_bg(a,r=18):
    # median of row segment excluding center? use box mean minus center weighting
    from numpy.lib.stride_tricks import sliding_window_view
    pad=np.pad(np.asarray(a),(r,r),mode='edge')
    w=sliding_window_view(pad,(2*r+1,))
    return np.median(w,axis=-1)

rows=range(1006,1101,2)
print("row | peaks (x, amplitude) using L - rolling median(41)")
for y in rows:
    seg=L[y,1240:1470]
    bg=local_bg(seg,20)
    d=seg-bg
    # find local maxima with prominence
    peaks=[]
    for i in range(3,len(d)-3):
        if d[i]==max(d[i-3:i+4]) and d[i]>6:
            # parabolic subpixel
            den=(d[i-1]-2*d[i]+d[i+1])
            off=0.5*(d[i-1]-d[i+1])/den if abs(den)>1e-9 else 0
            peaks.append((1240+i+off, d[i]))
    # merge closer than 4px
    merged=[]
    for p in peaks:
        if merged and p[0]-merged[-1][0]<4:
            if p[1]>merged[-1][1]: merged[-1]=p
        else: merged.append(list(p))
    print(f"{y}: " + " ".join(f"{p[0]:7.1f}({p[1]:4.1f})" for p in merged))
