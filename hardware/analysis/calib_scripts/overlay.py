import numpy as np, geom as G, model as M
from PIL import Image, ImageDraw, ImageFont
E = M.fit_edges(); SIDES={'L':'left','R':'right','T':'top','B':'bottom'}
offs = {k: G.sil_at(E[k], SIDES[k]) - E[k]['ref'] for k in 'LRTB'}
C,H = M.build(E,offs)
lw,rw,tw,bw = M.lines(E,offs)
print("silhouette lines (x = a + b*y ; y = c + d*x):")
print("  left  a=%.3f b=%+.5f"%(lw[0],lw[1])); print("  right a=%.3f b=%+.5f"%(rw[0],rw[1]))
print("  top   c=%.3f d=%+.5f"%(tw[0],tw[1])); print("  bottom c=%.3f d=%+.5f"%(bw[0],bw[1]))
print("corners TL %s TR %s BR %s BL %s"%tuple(np.round(C[k],2) for k in ['TL','TR','BR','BL']))
P = np.array([M.apply(H,[M.ball_st(j,i)])[0] for i in range(11) for j in range(1,12)])
np.save('/tmp/calib/balls.npy', P)

src='%s/hardware/photos/P1_top_side.webp'%'<repo>'
im = Image.open(src).convert('RGB')
x0,y0,x1,y1 = 1090,560,1650,1050
S = 2
crop = im.crop((x0,y0,x1,y1)).resize(((x1-x0)*S,(y1-y0)*S), Image.LANCZOS)
dr = ImageDraw.Draw(crop)
def T(p): return ((p[0]-x0)*S, (p[1]-y0)*S)
# package outline
q = [T(C['TL']),T(C['TR']),T(C['BR']),T(C['BL'])]
dr.line(q+[q[0]], fill=(0,255,0), width=3)
# grid
for k,p in enumerate(P):
    x,y = T(p); i,j = divmod(k,11)
    col = (255,60,60) if i==0 else (255,210,0)
    r = 5 if i==0 else 4
    dr.ellipse([x-r,y-r,x+r,y+r], outline=col, width=2)
# row A line
dr.line([T(P[0]),T(P[10])], fill=(255,120,120), width=1)
# dot
dx,dy = T((1509.2,955.35))
dr.line([dx-10,dy,dx+10,dy], fill=(0,255,255), width=2); dr.line([dx,dy-10,dx,dy+10], fill=(0,255,255), width=2)
dr.ellipse([dx-17.4,dy-17.4,dx+17.4,dy+17.4], outline=(0,255,255), width=2)
# labels
try: fnt = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 22)
except: fnt = ImageFont.load_default()
for nm in ['A1','A3','A4','A5','A6','A9','A10']:
    k = M.ROWS.index(nm[0])*11+int(nm[1:])-1
    x,y = T(P[k]); dr.text((x-10,y+8), nm, fill=(255,255,255), font=fnt)
dr.text((12,10), "CYUSB3014 121-FBGA — measured body outline (green) + computed 121-ball grid (0.80 mm pitch)", fill=(255,255,255), font=fnt)
dr.text((12,36), "yellow = rows B-L, red = row A; cyan = laser pin-1 dot", fill=(255,255,255), font=fnt)
crop.save('/tmp/calib/calib_overlay.png')
print("saved overlay", crop.size)
