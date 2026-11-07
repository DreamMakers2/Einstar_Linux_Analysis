import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch

fig, ax = plt.subplots(figsize=(16, 7.2), dpi=180)
ax.set_xlim(0, 16); ax.set_ylim(0, 7.2); ax.axis('off')

def box(x,y,w,h,txt,fc):
    ax.add_patch(FancyBboxPatch((x,y),w,h,boxstyle="round,pad=0.03,rounding_size=0.08",
                 fc=fc,ec='#333333',lw=1.2))
    ax.text(x+w/2,y+h/2,txt,ha='center',va='center',fontsize=9)

def arrow(x1,y1,x2,y2,c,lw=2.4):
    ax.add_patch(FancyArrowPatch((x1,y1),(x2,y2),arrowstyle='<|-|>',
                 mutation_scale=11,color=c,lw=lw,shrinkA=2,shrinkB=2))

box(13.0,2.2,2.7,2.7,"FX3 row A\nA6 1356.5\nA5 1386.1\nA4 1415.7\nA3 1445.3\n(y = 954.8 px)","#e8eef7")
box(8.3,2.0,3.1,3.1,"45° escapes\ncross edge y = 992 at\nA6 1330.9   A5 1347.4\nA4 1405.9   A3 1390.9\n(±1–2 px)","#eef7e8")
box(4.5,2.0,3.2,3.1,"horizontal runs\nTX  y = 1029 / 1041\nRX  y = 1084 / 1096\ncontinuous x 400–1300","#eef7e8")
box(2.3,3.6,1.9,1.4,"series parts\nTX only\n(375–465, 995–1075)","#ffe9d6")
box(0.2,2.0,1.8,3.1,"via field\nx 240–270\nleft corridor\nx 282 / 294\nx 324 / 335","#f2e6f7")
box(0.2,5.45,1.8,1.0,"component-side\n40-pin connector","#f7e6e6")
box(13.0,0.55,2.7,0.95,"B3 via (1428, 1040)\n→ R_USB3 200 Ω? → GND\nUNKNOWN","#fff2cc")

for y,c in [(3.95,'#d62728'),(3.55,'#ff7f0e')]:
    arrow(13.0,y,11.4,y,c); arrow(8.3,y,7.7,y,c); arrow(4.5,y,4.2,y,c); arrow(2.3,y,2.0,y,c)
for y,c in [(2.85,'#1f77b4'),(2.45,'#2ca02c')]:
    arrow(13.0,y,11.4,y,c); arrow(8.3,y,7.7,y,c); arrow(4.5,y,2.0,y,c)

ax.plot([1.1,1.1],[5.45,5.1],color='#555555',lw=2.2,ls='--')
ax.text(2.15,5.75,"final pin connection on inner layers — not visible",fontsize=8,color='#555555')

ax.text(0.25,6.95,"Optical net map — FX3 SuperSpeed channel (image px; not to scale)",
        fontsize=13,fontweight='bold')
ax.text(0.25,6.68,"solid = measured top copper; dashed = inferred / inner-layer / unverified",
        fontsize=9,color='#555555')

# legend bottom-left
ax.plot([0.35,0.75],[1.55,1.55],color='#d62728',lw=2.6); ax.text(0.85,1.48,"TX pair (A6/A5)",fontsize=9)
ax.plot([0.35,0.75],[1.25,1.25],color='#1f77b4',lw=2.6); ax.text(0.85,1.18,"RX pair (A4/A3)",fontsize=9)
ax.plot([0.35,0.75],[0.95,0.95],color='#555555',lw=2.2,ls='--'); ax.text(0.85,0.88,"inferred (inner layer)",fontsize=9)

ax.text(0.25,0.25,"Measured: 4 conductors, Hough r.m.s. 0.6–1.0 px over 18 rows; ball assignment ±8 px; connector pins unconfirmed.",
        fontsize=8,color='#555555')
plt.tight_layout()
plt.savefig('hardware/analysis/reopen/figs/N3_netmap_schematic.png',bbox_inches='tight')
print('saved')
