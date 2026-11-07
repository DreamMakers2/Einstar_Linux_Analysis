"""Figures for the addendum: what is really below the FX3."""
import os, sys, math
HERE=os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0,HERE)
from annotate import *
from PIL import Image, ImageDraw, ImageFilter, ImageOps
import numpy as np

ROOT=os.path.dirname(HERE); OUT=f'{ROOT}/hardware/annotated'
p1=Image.open(f'{ROOT}/hardware/photos/P1_top_side.webp').convert('RGB')

# measured geometry
A1=(1519.5,952.5); PITCH=30.0; EDGE_Y=990.0
BALLS={f'A{j}':A1[0]-(j-1)*PITCH for j in range(1,12)}
# fitted diagonal pair (from row peaks 1048..1080)
def fit(pts):
    ys=np.array([p[1] for p in pts]); xs=np.array([p[0] for p in pts])
    A=np.stack([ys,np.ones(len(ys))],1); m,b=np.linalg.lstsq(A,xs,rcond=None)[0]
    return m,b
L=fit([(1338,1048),(1335,1052),(1331,1056),(1326,1060),(1322,1064),(1319,1068),(1313,1072),(1310,1076),(1306,1080)])
R=fit([(1353,1048),(1350,1052),(1345,1056),(1340,1060),(1338,1064),(1334,1068),(1329,1072),(1324,1076),(1320,1080)])
print("left  slope %.3f  x@985 %.1f"%(L[0],L[0]*985+L[1]))
print("right slope %.3f  x@985 %.1f"%(R[0],R[0]*985+R[1]))

def fig_b1():
    box=(1240,955,1500,1115); S=9
    im=p1.crop(box)
    l=np.asarray(im.convert('L')).astype(np.float32)
    lo,hi=np.percentile(l,2),np.percentile(l,97)
    l=np.clip((l-lo)/(hi-lo+1e-6)*255,0,255)       # clipped stretch, highlights stay white
    g=Image.fromarray(l.astype(np.uint8))
    im=Image.merge('RGB',(g,g,g)).resize(((box[2]-box[0])*S,(box[3]-box[1])*S),Image.LANCZOS)
    W=im.width+700; H=im.height+200
    c=Image.new('RGB',(W,H),(12,12,18)); c.paste(im,(0,170)); d=ImageDraw.Draw(c,'RGBA')
    fT=font(32); fH=font(23); fB=font(19,False); fS=font(16,False)
    def T(x,y): return ((x-box[0])*S,(y-box[1])*S+170)
    # ball columns
    for name,x in BALLS.items():
        j=int(name[1:])
        if j<2 or j>10: continue
        col=(0,255,255) if j in (3,4,5,6) else ((255,210,0) if j in (9,10) else (110,110,120))
        X,_=T(x,955); d.line([(X,170),(X,H-60)],fill=col,width=1)
        d.text((X+2,176),name,font=fS,fill=col)
    # package edge
    X0,Y0=T(box[0],EDGE_Y); d.line([(0,Y0),(im.width,Y0)],fill=(255,80,80),width=2)
    d.text((6,Y0-22),"package edge y=990",font=fS,fill=(255,80,80))
    # fitted traces + extrapolation
    for (m,b),col in [(L,(0,255,255)),(R,(0,220,235))]:
        pts=[T(m*y+b,y) for y in range(1040,1105,2)]
        d.line(pts,fill=col,width=3)
        for y in range(988,1040,6):               # dashed extrapolation
            d.line([T(m*y+b,y),T(m*(y+3)+b,y+3)],fill=col,width=2)
        # dashed extrapolation to package edge
        for y in range(985,1005,4):
            x=m*y+b; a=T(x,y); bb=T(x,y+2)
            d.line([a,bb],fill=(0,255,255,140),width=2)
    # landing markers
    for (m,b),lbl in [(L,"x=%.0f"%(m*985+b)),(R,"x=%.0f"%(m*985+b))]:
        x=m*985+b; X,Y=T(x,EDGE_Y)
        d.ellipse([X-7,Y-7,X+7,Y+7],outline=(255,255,255),width=2)
        d.text((X-24,Y-34),lbl,font=fS,fill=(255,255,255))
    # uncertainty band
    X1,_=T(1395,955); X2,_=T(1445,955)
    d.rectangle([X1,170,X2,H-60],fill=(255,255,255,22),outline=None)
    d.text((X1+4,H-96),"A4–A5 band",font=fS,fill=(230,230,230))
    # title
    d.text((24,24),"B1 — The one genuine escape pair, measured",font=fT,fill=C_WHITE)
    d.text((24,70),"Contrast-stretched crop. Cyan = fitted trace centrelines; white rings = where they would cross the package edge.",font=fB,fill=(185,185,198))
    # panel
    px=im.width+24
    d.rectangle([px-10,170,W-14,H-30],fill=(16,16,24,255),outline=(64,64,84,255),width=2)
    lines=[("Measurement",C_WHITE),
           ("two parallel traces, 45.0° and 45.9°",(220,220,228)),
           ("centre spacing 15–17 px = 0.41–0.46 mm",(220,220,228)),
           ("width ≈ 3 px = 0.08 mm (mask-covered)",(220,220,228)),
           ("contrast +30 luminance over mask",(220,220,228)),
           ("",None),
           ("Extrapolated landing at y=990",C_WHITE),
           ("left  x = 1401",C_SS),("right x = 1418",C_SS),
           ("",None),
           ("Ball centres in that row",C_WHITE),
           ("A6 1369.5   A5 1399.5",(200,200,210)),
           ("A4 1429.5   A3 1459.5",(200,200,210)),
           ("",None),
           ("Read this carefully",(255,190,90)),
           ("the left trace lands 1.5 px from A5,",(255,220,150)),
           ("but the pair's centre (1409) is 25 px",(255,220,150)),
           ("from the A5/A6 midpoint (1384) and",(255,220,150)),
           ("35 px from A3/A4 (1444).",(255,220,150)),
           ("No valid SS pair matches both traces.",(255,120,120))]
    yy=186
    for t,col in lines:
        if t: d.text((px,yy),t,font=fS,fill=col or (220,220,228))
        yy+=29
    c.save(f'{OUT}/B1_escape_pair_measured.png'); print('B1',c.size)

def fig_b2():
    box=(560,940,1760,1140); S=2.2
    im=p1.crop(box)
    g=ImageOps.autocontrast(im.convert('L'),cutoff=0.2)
    im=Image.merge('RGB',(g,g,g)).resize((int(im.width*S),int(im.height*S)),Image.LANCZOS)
    W=im.width; H=im.height+260
    c=Image.new('RGB',(W,H),(12,12,18)); c.paste(im,(0,200)); d=ImageDraw.Draw(c,'RGBA')
    fT=font(32); fB=font(19,False); fS=font(15,False)
    def T(x,y): return ((x-box[0])*S,(y-box[1])*S+200)
    # package footprint
    x0,y0=T(1176,940); x1,y1=T(1560,990)
    d.rectangle([x0,y0,x1,y1],outline=(255,255,255),width=2)
    d.text((x0+6,y0-22),"FX3 package (bottom edge)",font=fS,fill=(255,255,255))
    # FPGA edge
    xf,_=T(1690,940); d.line([(xf,200),(xf,H-70)],fill=(200,140,255),width=2)
    d.text((xf+4,206),"FPGA",font=fS,fill=(200,140,255))
    # J4
    xj,_=T(595,940); d.line([(xj,200),(xj,H-70)],fill=(255,210,0),width=2)
    d.text((xj+4,206),"J4",font=fS,fill=(255,210,0))
    # horizontal traces (measured)
    for y,lab in [(978,'978'),(989,'989'),(1030,'1030'),(1041,'1041'),(1085,'1085'),(1096,'1096')]:
        a=T(box[0],y); b=T(box[2],y)
        d.line([a,b],fill=(0,255,255,120),width=1)
        d.text((a[0]+4,a[1]-16),lab,font=fS,fill=(0,220,220))
    d.text((24,24),"B2 — The strip below the FX3 is a routing channel, not an FX3 escape",font=fT,fill=C_WHITE)
    d.text((24,70),"Six copper traces (three pairs, 11–12 px = 0.30–0.32 mm apart) run the full width and continue past the package on BOTH sides.",font=fB,fill=(185,185,198))
    d.text((24,100),"A trace that continues to the FPGA (right) and to J4 (left) cannot originate at the FX3's row-A balls: it is a through-route.",font=fB,fill=(255,190,90))
    c.save(f'{OUT}/B2_routing_channel.png'); print('B2',c.size)

fig_b1(); fig_b2()
