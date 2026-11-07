"""Annotated figures for the SHINING 3D Einstar USB-3.0 feasibility study.

All coordinates are source-photo pixels.  Scale: 36 px ~= 1 mm (FX3 10 mm body
measured at 370 px, Pango PGL25G 22x22 ball field used as cross-check).
"""
import sys, math, os
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from annotate import *
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageFilter

ROOT=os.path.dirname(HERE); OUT=f'{ROOT}/hardware/annotated'
os.makedirs(OUT,exist_ok=True)

def load(name):
    im=Image.open(f'{ROOT}/hardware/photos/{name}.webp').convert('RGB')
    a=np.asarray(im).astype(np.float32)
    m=a.reshape(-1,3).mean(0)
    a=np.clip(a*(m[1]/m),0,255).astype(np.uint8)     # gray-world white balance
    return Image.fromarray(a)

p1=load('P1_top_side'); p2=load('P2_bottom_side')
p1.save(f'{OUT}/_P1_wb.png'); p2.save(f'{OUT}/_P2_wb.png')

def up(im,s): return im.resize((int(im.width*s),int(im.height*s)),Image.LANCZOS).filter(
    ImageFilter.UnsharpMask(radius=2,percent=80,threshold=2))

def panel(d,x,y,w,title,sub,items,fT,fB,h=None):
    hh = h or (150 + 34*len(items))
    d.rectangle([x-26,y-26,x+w+26,y+hh], fill=(14,14,20,255), outline=(70,70,92,255), width=2)
    d.text((x,y),title,font=fT,fill=C_WHITE)
    d.text((x,y+40),sub,font=fB,fill=(158,158,172))
    yy=y+84
    for it in items:
        if isinstance(it,tuple) and it[0]:
            c,t=it
            d.rectangle([x,yy+6,x+14,yy+20], fill=c)
            d.text((x+26,yy),t,font=fB,fill=(222,222,230))
        else:
            d.text((x,yy),it,font=fB,fill=(222,222,230))
        yy+=34

def region(d,box,n,color,fN,w=4):
    d.rectangle(box, outline=color, width=w)
    r=17; cx,cy=box[0]+20,box[1]+20
    d.ellipse([cx-r,cy-r,cx+r,cy+r], fill=(0,0,0,235), outline=color, width=3)
    bb=d.textbbox((0,0),n,font=fN)
    d.text((cx-(bb[2]-bb[0])/2, cy-(bb[3]-bb[1])/2-2), n, font=fN, fill=color)

# ------------------------------------------------------------------ A1 top overview
def fig_top():
    W,H=3200,1290
    c=Image.new('RGB',(W,H),(12,12,18)); c.paste(p1,(0,60)); d=ImageDraw.Draw(c,'RGBA')
    fN=font(24); fB=font(21,False)
    d.text((26,10),"A1 — Einstar mainboard, component side: architecture map",font=font(32),fill=C_WHITE)
    for box,n,col in [
        ((365,215,595,880),"1",(255,255,255)),
        ((1185,625,1560,985),"2",C_SS),
        ((1680,55,2340,720),"3",(205,145,255)),
        ((1055,85,1595,430),"4",(140,195,255)),
        ((975,815,1125,1010),"5",(255,190,90)),
        ((630,700,950,960),"6",(255,190,90))]:
        region(d,box,n,col,fN)
    items=[(C_SS,"2  CYUSB3014-BZX  —  121-ball BGA, SS-capable"),
           ((205,145,255),"3  Pango Logos PGL25G, 484-ball (frame cuts it)"),
           ((140,195,255),"4  Micron D9PTK DDR3L — FPGA frame buffer"),
           "",
           "1  40-pin connector J4 — the only connector in frame.",
           "    The DP/DN test vias sit next to it (back side).",
           "5  FX3 supply: inductor, buck, decoupling.",
           "6  Second regulator cluster (core / I-O rails).",
           "",
           "The board's right-hand edge is outside the photo,",
           "so the external 7-pin cable connector is NOT visible.",
           "",
           "Scale: 36 px ≈ 1 mm.  Photo is rotated 180° vs the",
           "chip marking (see A2)."]
    panel(d,2400,80,740,"Component-side map","Numbers match the cyan boxes.",items,fB,fB)
    c.save(f'{OUT}/A1_top_side_overview.png'); print('A1',c.size)

# ------------------------------------------------------------------ A3 bottom overview
def fig_bottom():
    W,H=3140,1330
    c=Image.new('RGB',(W,H),(12,12,18)); c.paste(p2,(0,60)); d=ImageDraw.Draw(c,'RGBA')
    fN=font(24); fB=font(21,False)
    d.text((26,10),"A3 — Einstar mainboard, back side: where the USB 2.0 evidence lives",font=font(32),fill=C_WHITE)
    for box,n,col in [
        ((85,375,215,835),"1",(255,255,255)),
        ((520,700,645,775),"2",C_USB2),
        ((515,585,635,650),"3",(110,215,255)),
        ((680,85,960,310),"4",(205,145,255)),
        ((338,618,432,848),"5",(255,150,90))]:
        region(d,box,n,col,fN)
    items=[(C_USB2,"2  DP / DN test vias — the only USB data test points"),
           ((110,215,255),"3  CLK / PCLK test vias"),
           ((205,145,255),"4  Winbond W25Q64JVSIQ — 8 MB SPI boot flash"),
           ((255,150,90),"5  4× 0603 capacitor bank beside J4 (rail decoupling)"),
           "",
           "No SSTX / SSRX test pads exist on this side.",
           "No 4-channel low-capacitance ESD array is fitted.",
           "No 0.1 µF series AC-coupling pair is fitted.",
           "",
           "1  J4, 40 pins (2×20, 0.5 mm). The USB 2.0 pair leaves",
           "   the FX3, is broken out on the DP/DN vias, and is",
           "   routed through J4 to the plug-in board that carries",
           "   the external 7-pin cable connector."]
    panel(d,2360,100,740,"Back-side features","Two labelled test points are the key structural clue.",items,fB,fB)
    c.save(f'{OUT}/A3_bottom_side_overview.png'); print('A3',c.size)

# ------------------------------------------------------------------ A2 FX3 ball field
def fig_fx3():
    box=(1060,540,1650,1080); S=3.2
    im=up(p1.crop(box),S)
    W,H=2560,1930
    c=Image.new('RGB',(W,H),(12,12,18)); ox,oy=0,175; c.paste(im,(ox,oy))
    d=ImageDraw.Draw(c,'RGBA')
    fN=font(21); fB=font(20,False); fS=font(16,False)
    def T(p): return (ox+(p[0]-box[0])*S, oy+(p[1]-box[1])*S)
    d.polygon([T((1185,630)),T((1555,630)),T((1555,980)),T((1185,980))],outline=(255,255,255,235),width=3)
    for i,row in enumerate(ROWS):
        for j in range(1,12):
            x,y=ball_xy(row,j); X,Y=T((x,y))
            if row=='A' and 1<=j<=6: col,r,wd=C_SS,12,3
            elif row=='A' and j in (9,10): col,r,wd=C_USB2,12,3
            elif (row=='B' and j==3) or (row=='C' and j==8): col,r,wd=(255,120,255),12,3
            elif (row=='B' and j==5) or (row=='A' and j==2): col,r,wd=(255,255,255),9,2
            else: col,r,wd=(190,190,190,140),6,1
            d.ellipse([X-r,Y-r,X+r,Y+r],outline=col,width=wd)
    for i,row in enumerate(ROWS):
        X,Y=T(ball_xy(row,1)); d.text((X+20,Y-9),row,font=fS,fill=(225,225,225,225))
    for j in range(1,12):
        X,Y=T(ball_xy('A',j)); d.text((X-8,Y+17),str(j),font=fS,fill=(225,225,225,225))
    X,Y=T((1505,958)); d.ellipse([X-22,Y-22,X+22,Y+22],outline=C_OK,width=4)
    # direction arrows seen in silkscreen
    X0,Y0=T((1200,1010)); X1,Y1=T((1470,1010))
    arrow(d,(X1,Y1),(X0,Y0),(255,255,255,220),4,16)
    d.text(T((1206,1030)),"silk arrow → pin 1 at right",font=fS,fill=(255,255,255))
    px=1935
    d.rectangle([px-30,160,W-20,1180],fill=(16,16,24,255),outline=(64,64,84,255),width=2)
    d.text((px,180),"Documented ball field (reconstructed)",font=fN,fill=C_WHITE)
    d.text((px,210),"Infineon 001-52136 Rev.*Z, Fig. 6 + Table 7",font=fS,fill=(150,150,165))
    y=252
    for col,t in [(C_SS,"A1 U3VSSQ · A2 U3RXVDDQ · A3/A4 SSRX−/+"),
                  (C_SS,"A5/A6 SSTX+ / SSTX−   (row A, outer edge)"),
                  (C_USB2,"A9 DP · A10 DM  —  USB 2.0 pair"),
                  ((255,120,255),"B3 R_USB3 (200 Ω) · C8 R_USB2 (6.04 kΩ)"),
                  ((255,255,255),"all other balls (GPIF II, power, clock, JTAG)"),
                  (C_OK,"pin-1 laser dot, on ball A1")]:
        d.ellipse([px,y+5,px+20,y+25],outline=col,width=3)
        d.text((px+30,y),t,font=fS,fill=(225,225,225)); y+=36
    y+=14
    for t in ["Row A is the outer row: a top-layer escape from A3–A6",
              "would emerge along the package edge at the bottom of",
              "this picture between the A6 and A3 markers.",
              "",
              "What is actually seen there: the ground pour, two",
              "silkscreen lines, and no bundle of four traces.",
              "That is consistent with the SS pins being left open",
              "(AN70707 Rev.*Q App. C) — but it is NOT proof: a",
              "dog-bone via under the package would be invisible.",
              "",
              "The 180° rotation is fixed by three independent cues:",
              "the laser dot at A1, the two silkscreen direction",
              "arrows that both start at that corner, and the",
              "upside-down chip marking."]:
        d.text((px,y),t,font=fS,fill=(215,215,225)); y+=30
    d.text((30,26),"A2 — FX3 orientation and SuperSpeed ball locations",font=font(34),fill=C_WHITE)
    d.text((30,82),"Positions are reconstructed from the 10×10 mm package outline; the balls themselves are under the package and not visible.",font=font(21,False),fill=(185,185,198))
    c.save(f'{OUT}/A2_fx3_ball_field.png'); print('A2',c.size)

fig_top(); fig_bottom(); fig_fx3()

# ------------------------------------------------------------------ A4 signal path
def fig_path():
    W,H=2600,1560
    c=Image.new('RGB',(W,H),(14,14,20)); d=ImageDraw.Draw(c,'RGBA')
    fT=font(34); fH=font(24); fS=font(19,False)
    d.text((60,36),"A4 — Signal-path audit: what exists today and what a SuperSpeed link would need",font=fT,fill=C_WHITE)
    d.text((60,80),"Left of the dashed line: the photographed mainboard. Right of it: the plug-in board and cable (not photographed).",font=fS,fill=(170,170,185))

    def box(x,y,w,h,title,lines,col,tc=None):
        d.rectangle([x,y,x+w,y+h],fill=(24,24,32),outline=col,width=3)
        d.text((x+16,y+12),title,font=fH,fill=tc or col)
        yy=y+52
        for l in lines:
            d.text((x+16,yy),l,font=fS,fill=(222,222,230)); yy+=27
    def arr(p0,p1,col,w=4):
        d.line([p0,p1],fill=col,width=w)
        ang=math.atan2(p1[1]-p0[1],p1[0]-p0[0])
        for s in (0.5,-0.5):
            d.line([p1,(p1[0]-18*math.cos(ang+s),p1[1]-18*math.sin(ang+s))],fill=col,width=w)

    d.rectangle([50,140,1550,1120],outline=(90,90,110),width=2)
    d.text((70,152),"PHOTOGRAPHED MAINBOARD  (figures A1 / A2 / A3)",font=fS,fill=(150,150,165))

    box(90,210,380,150,"Camera / sensor head",["connects through J4","(40-pin, 2x20)"],(200,200,210))
    box(560,210,380,150,"Pango PGL25G",["FPGA + DDR3L frame buffer","image pipeline"],(205,145,255))
    box(90,520,380,170,"J4 - 40-pin","only connector in frame|USB 2.0 pair passes here".split("|"),(255,255,255))
    box(560,520,380,200,"CYUSB3014-BZX",["USB 3.0-capable PHY","firmware enables SS","A3/A4 SSRX . A5/A6 SSTX"],C_SS)
    box(1040,520,470,200,"USB 2.0 pins",["A9 DP . A10 DM -> DP/DN vias","-> J4 -> daughterboard","working today at 480 Mb/s"],C_USB2)
    box(1040,820,470,170,"SuperSpeed pins A3-A6",["no AC caps . no ESD array","no test pads . no traces seen"],C_WARN)

    arr((470,285),(560,285),(200,200,210))
    arr((470,600),(560,600),(255,255,255))
    arr((940,620),(1040,620),C_USB2)
    arr((1275,720),(1275,820),C_WARN)

    for yy in range(140,1240,26):
        d.line([(1600,yy),(1600,yy+13)],fill=(220,90,90),width=3)
    d.text((1620,140),"not photographed",font=fS,fill=(220,120,120))

    box(1660,210,380,150,"7-pin connector",["12 V . GND . D+ . D- . ...","1 pin unconnected"],(255,150,120))
    box(1660,520,380,170,"cable (USB 2.0)","4 USB 2.0 conductors|no SS pairs".split("|"),(255,150,120))
    box(1660,820,380,170,"Host PC",["USB 3.x port, SS-capable","(host side is not the limit)"],(140,200,255))
    arr((1550,600),(1660,600),C_USB2)
    arr((2040,600),(2140,600),C_USB2)
    d.text((2150,586),"480 Mb/s",font=fS,fill=C_USB2)
    d.text((2150,614),"today",font=fS,fill=C_USB2)

    d.rectangle([50,1180,2550,1520],fill=(20,20,28),outline=(90,90,110),width=2)
    d.text((70,1200),"A SuperSpeed link would need all five of these - and the mainboard supplies none of them:",font=fH,fill=C_WHITE)
    need=["1   four SS conductors brought out of balls A3-A6 (BGA rework, or vias that are proven to exist)",
          "2   0.1 uF AC-coupling pair on SSTX, at the receptacle, with a ground cut-out (AN70707 12.1)",
          "3   <= 0.5 pF ESD array on all four lines (FX3 gives only +/-2.2 kV HBM on these pins)",
          "4   90 ohm +/-7 % differential routing over solid ground, zero vias, < 76 mm (AN70707 12.1)",
          "5   a connector + cable with four SS conductors and a shield - the 7-pin connector has one spare pin"]
    for i,t in enumerate(need):
        d.text((80,1246+i*46),t,font=fS,fill=(228,228,236))
    c.save(f'{OUT}/A4_signal_path_audit.png'); print('A4',c.size)

# ------------------------------------------------------------------ A5 ball map
def fig_ballmap():
    CW,CH=176,96
    W,H=600+11*CW, 260+12*CH+220
    c=Image.new('RGB',(W,H),(14,14,20)); d=ImageDraw.Draw(c,'RGBA')
    d.text((40,30),"A5 — CYUSB3014-BZX documented ball map (Infineon 001-52136 Rev.*Z, Fig. 6 / Table 7)",font=font(30),fill=C_WHITE)
    d.text((40,74),"Rows A–L (letter I skipped), columns 1–11, 0.8 mm pitch. Top view as printed in the datasheet; on the photograph the package is rotated 180°.",font=font(19,False),fill=(170,170,182))
    X0,Y0=200,150
    cells={
     "A1":("U3VSSQ","pwr"),"A2":("U3RXVDDQ","pwr"),"A3":("SSRXM","ss"),"A4":("SSRXP","ss"),"A5":("SSTXP","ss"),"A6":("SSTXM","ss"),"A7":("AVDD","pwr"),"A8":("VSS","pwr"),"A9":("DP","usb"),"A10":("DM","usb"),"A11":("NC","nc"),
     "B3":("R_USB3","ref"),"B5":("U3TXVDDQ","pwr"),"C8":("R_USB2","ref"),"C5":("RESET#","ctl"),"C6":("XTALIN","ctl"),"C7":("XTALOUT","ctl"),
    }
    colmap={"ss":C_SS,"usb":C_USB2,"pwr":(150,170,200),"ref":(255,120,255),"ctl":(150,220,150),"nc":(110,110,120),"gpio":(120,120,130)}
    for i,row in enumerate(ROWS):
        for j in range(1,12):
            x=X0+(j-1)*CW; y=Y0+i*CH
            key=f"{row}{j}"
            if key in cells:
                name,kind=cells[key]; col=colmap[kind]; fill=(30,44,50) if kind=="ss" else (26,26,34)
            else:
                name=""; col=(80,80,95); fill=(20,20,26)
            d.rectangle([x,y,x+CW-4,y+CH-4],fill=fill,outline=col,width=2)
            d.text((x+8,y+8),key,font=font(20),fill=col if name else (110,110,125))
            if name: d.text((x+8,y+40),name,font=font(17,False),fill=(235,235,240))
    for j in range(1,12):
        d.text((X0+(j-1)*CW+CW/2-8,Y0-34),str(j),font=font(22),fill=(220,220,230))
    for i,row in enumerate(ROWS):
        d.text((X0-44,Y0+i*CH+28),row,font=font(22),fill=(220,220,230))
    ly=Y0+12*CH+40
    leg=[(C_SS,"SuperSpeed / SS-PHY"),(C_USB2,"USB 2.0"),((255,120,255),"precision reference resistor"),
         ((150,220,150),"clock / reset"),((150,170,200),"power / ground"),((110,110,120),"no connect")]
    for k,(col,t) in enumerate(leg):
        x=60+(k%3)*640; y=ly+(k//3)*40
        d.rectangle([x,y+4,x+22,y+24],fill=col)
        d.text((x+32,y),t,font=font(19,False),fill=(225,225,232))
    d.text((60,ly+100),"Balls A3–A6 are the only SuperSpeed copper. They sit in row A, the outermost row, next to the U3 PHY supply balls A1/A2 and the AVDD ball A7.",font=font(18,False),fill=(180,180,192))
    d.text((60,ly+130),"U3TXVDDQ (B5) and U3RXVDDQ (A2) must stay powered at 1.2 V even in a USB 2.0-only design (AN70707 App. C); R_USB3 (B3) may be left open.",font=font(18,False),fill=(180,180,192))
    c.save(f'{OUT}/A5_ball_map.png'); print('A5',c.size)

fig_path(); fig_ballmap()
