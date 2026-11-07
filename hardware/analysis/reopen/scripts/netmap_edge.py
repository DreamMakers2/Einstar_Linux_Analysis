import numpy as np
from PIL import Image, ImageDraw, ImageFont
F=ImageFont.truetype('/usr/share/fonts/TTF/DejaVuSans.ttf',22)
FS=ImageFont.truetype('/usr/share/fonts/TTF/DejaVuSans.ttf',18)

im=Image.open('hardware/photos/P1_top_side.webp').convert('RGB')
x0,y0,x1,y1=1240,930,1490,1110
sc=8
crop=im.crop((x0,y0,x1,y1))
big=crop.resize((crop.width*sc,crop.height*sc), Image.LANCZOS).convert('RGBA')
ov=Image.new('RGBA',big.size,(0,0,0,0))
d=ImageDraw.Draw(ov)
def P(x,y): return ((x-x0)*sc,(y-y0)*sc)
m=0.85
tracks=[('A6',2174.1,(230,40,40,230)),('A5',2190.6,(255,140,0,230)),
        ('A4',2234.1,(30,120,220,230)),('A3',2249.1,(30,170,80,230))]
for name,c,col in tracks:
    ys=np.arange(955,1078)
    xs=c-m*ys
    ok=(xs>=x0)&(xs<=x1)
    d.line([P(x,y) for x,y in zip(xs[ok],ys[ok])],fill=col,width=2)
balls={'A1':1504.49,'A2':1474.9,'A3':1445.32,'A4':1415.71,'A5':1386.09,'A6':1356.46}
for b,x in balls.items():
    px,py=P(x,954.8); r=10
    d.ellipse([px-r,py-r,px+r,py+r],outline=(255,230,0,255),width=2)
    d.text((px+r+3,py-11),b,fill=(255,230,0,255),font=F)
# package edge
d.line([P(1240,991.9),P(1490,992.0)],fill=(255,0,255,220),width=2)
# vias
for vx,vy,lab in [(1428,1040,'via 1428,1040'),(1348,1085,'via 1348,1085')]:
    px,py=P(vx,vy); r=14
    d.ellipse([px-r,py-r,px+r,py+r],outline=(255,255,255,255),width=2)
    d.text((px+r+3,py-9),lab,fill=(255,255,255,255),font=FS)
# scale bar 0.8mm = 29.6px
sx,sy=P(1250,1100); L=29.6*sc
d.line([(sx,sy),(sx+L,sy)],fill=(255,255,255,255),width=3)
d.text((sx,sy-26),"0.8 mm (ball pitch)",fill=(255,255,255,255),font=FS)
# legend
lx,ly=P(1245,940)
legend=[("A6 -> y=1029 (TX)",(230,40,40,255)),("A5 -> y=1041 (TX)",(255,140,0,255)),
        ("A4 -> y=1084 (RX)",(30,120,220,255)),("A3 -> y=1096 (RX)",(30,170,80,255))]
for i,(t,c) in enumerate(legend):
    yy=ly+14+i*13
    d.line([(lx,yy),(lx+18,yy)],fill=c,width=2)
    d.text((lx+24,yy-11),t,fill=c,font=FS)
out=Image.alpha_composite(big,ov).convert('RGB')
out.save('hardware/analysis/reopen/figs/N1_edge_netmap.png')
print(out.size)
