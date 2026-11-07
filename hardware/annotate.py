"""Annotate Einstar mainboard photographs for the USB 3.0 feasibility study."""
from PIL import Image, ImageDraw, ImageFont, ImageFilter
import math, os

FB = "/usr/share/fonts/liberation/LiberationSans-Bold.ttf"
FR = "/usr/share/fonts/liberation/LiberationSans-Regular.ttf"
FM = "/usr/share/fonts/liberation/LiberationMono-Regular.ttf"

C_SS   = (0, 255, 255)     # cyan - SuperSpeed
C_USB2 = (255, 210, 0)     # amber - USB 2.0
C_WARN = (255, 70, 70)     # red - blocked / absent
C_OK   = (80, 255, 120)    # green - confirmed
C_WHITE= (255, 255, 255)
C_GREY = (200, 200, 200)

def font(sz, bold=True):
    return ImageFont.truetype(FB if bold else FR, sz)

def text_box(d, xy, lines, fnt, fg=C_WHITE, bg=(0, 0, 0, 200), pad=6, anchor="la"):
    """Draw multi-line text with a translucent背景 panel. xy = top-left."""
    tmp = Image.new("RGBA", (1, 1))
    td = ImageDraw.Draw(tmp)
    w = 0; h = 0
    for ln in lines:
        bb = td.textbbox((0, 0), ln, font=fnt)
        w = max(w, bb[2]); h += (bb[3] - bb[1]) + 4
    x, y = xy
    d.rectangle([x - pad, y - pad, x + w + pad, y + h + pad], fill=bg)
    yy = y
    for ln in lines:
        d.text((x, yy), ln, font=fnt, fill=fg)
        bb = td.textbbox((0, 0), ln, font=fnt)
        yy += (bb[3] - bb[1]) + 4
    return (x - pad, y - pad, x + w + pad, y + h + pad)

def arrow(d, p0, p1, color=C_WHITE, w=4, head=14):
    d.line([p0, p1], fill=color, width=w)
    ang = math.atan2(p1[1]-p0[1], p1[0]-p0[0])
    for s in (0.5, -0.5):
        d.line([p1, (p1[0]-head*math.cos(ang+s), p1[1]-head*math.sin(ang+s))], fill=color, width=w)

def circle(d, c, r, color, w=4):
    d.ellipse([c[0]-r, c[1]-r, c[0]+r, c[1]+r], outline=color, width=w)

ROWS = "ABCDEFGHJKL"          # JEDEC: the letter I is skipped

def ball_xy(row, col, A1=(1523.0, 948.0), dx=(-30.6, 0.0), dy=(0.0, -28.5)):
    i = ROWS.index(row); j = col - 1
    return (A1[0] + i*dy[0] + j*dx[0], A1[1] + i*dy[1] + j*dx[1])
