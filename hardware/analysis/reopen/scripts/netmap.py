import numpy as np
from PIL import Image, ImageDraw, ImageFont

im=Image.open('hardware/photos/P1_top_side.webp').convert('RGB')

def draw_map(box, scale, out, labels=True):
    x0,y0,x1,y1=box
    crop=im.crop(box)
    W,H=crop.size
    big=crop.resize((W*scale,H*scale), Image.LANCZOS)
    ov=Image.new('RGBA',big.size,(0,0,0,0))
    d=ImageDraw.Draw(ov)
    def P(x,y): return ((x-x0)*scale,(y-y0)*scale)
    def line(pts,color,w=2):
        d.line([P(x,y) for x,y in pts],fill=color,width=w)
    # measured tracks (c = x + 0.85*y)
    m=0.85
    tracks={
      'A6':(2174.1,(255,80,80,220)),
      'A5':(2190.6,(255,160,60,220)),
      'A4':(2234.1,(80,200,255,220)),
      'A3':(2249.1,(80,255,120,220)),
    }
    for name,(c,col) in tracks.items():
        ys=np.arange(955,1080)
        xs=c-m*ys
        ok=(xs>=x0)&(xs<=x1)
        line([(x,y) for x,y in zip(xs[ok],ys[ok])],col,2)
    # ball positions
    balls={'A1':1504.49,'A2':1474.9,'A3':1445.32,'A4':1415.71,'A5':1386.09,'A6':1356.46,'A7':1326.8,'B3':1445.4}
    yA=954.8
    for b,x in balls.items():
        px,py=P(x,yA); r=3*scale//2
        d.ellipse([px-r,py-r,px+r,py+r],outline=(255,255,0,255),width=2)
        if labels: d.text((px+r+2,py-6),b,fill=(255,255,0,255))
    d.text(P(1258,946),"row A (y=954.8)",fill=(255,255,0,255))
    # package edge
    line([(1170,991.6),(1542,992.1)],(255,0,255,200),2)
    if labels: d.text(P(1180,994),"package lower edge y=991.9",fill=(255,0,255,255))
    # via candidates
    for (vx,vy,lab) in [(1428,1040,'via 1428,1040'),(1348,1085,'via 1348,1085'),(1305,1040,'bend C5')]:
        px,py=P(vx,vy); r=4*scale
        d.ellipse([px-r,py-r,px+r,py+r],outline=(255,255,255,255),width=2)
        if labels: d.text((px+r+2,py),lab,fill=(255,255,255,255))
    out_im=Image.alpha_composite(big.convert('RGBA'),ov).convert('RGB')
    out_im.save(out)
    print(out,out_im.size)

draw_map((1240,930,1480,1110),8,'hardware/analysis/reopen/figs/N1_edge_netmap.png')
draw_map((240,950,1600,1140),3,'hardware/analysis/reopen/figs/N2_route_netmap.png')
