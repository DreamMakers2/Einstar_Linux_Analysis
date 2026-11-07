import numpy as np
from PIL import Image, ImageFilter, ImageOps, ImageDraw

def load(name):
    return np.asarray(Image.open(f'hardware/photos/{name}.webp').convert('RGB')).astype(np.float64)

def boxblur(a, r):
    # separable box blur via cumulative sums, edge-replicated
    a = np.pad(a, ((r,r),(r,r)), mode='edge')
    c = np.cumsum(np.cumsum(a, axis=0), axis=1)
    c = np.pad(c, ((1,0),(1,0)))
    k = 2*r+1
    out = (c[k:,k:] - c[:-k,k:] - c[k:,:-k] + c[:-k,:-k])/(k*k)
    return out[:a.shape[0]-2*r, :a.shape[1]-2*r]

def stretch(a, lo=1, hi=99):
    v=np.percentile(a,[lo,hi]); return np.clip((a-v[0])/(v[1]-v[0]+1e-9),0,1)

def save_rgb(a, path, scale=4, grid=None, nearest=False):
    a=np.clip(a,0,255).astype(np.uint8)
    im=Image.fromarray(a).convert('RGB')
    if scale!=1:
        im=im.resize((im.width*scale, im.height*scale), Image.NEAREST if nearest else Image.LANCZOS)
    if grid:
        d=ImageDraw.Draw(im); x0,y0,x1,y1,step=grid
        for x in range(x0,x1+1,step):
            d.line([((x-x0)*scale,0),((x-x0)*scale,im.height)],fill=(255,0,0),width=1)
            d.text(((x-x0)*scale+2,2),str(x),fill=(255,255,0))
        for y in range(y0,y1+1,step):
            d.line([(0,(y-y0)*scale),(im.width,(y-y0)*scale)],fill=(255,0,0),width=1)
            d.text((2,(y-y0)*scale+1),str(y),fill=(255,255,0))
    im.save(path)
    return im

if __name__=='__main__':
    im=load('P1_top_side')
    x0,y0,x1,y1=1120,930,1700,1170
    crop=im[y0:y1, x0:x1]
    R,G,B=crop[:,:,0],crop[:,:,1],crop[:,:,2]
    L=0.299*R+0.587*G+0.114*B
    # raw
    save_rgb(crop,'hardware/analysis/reopen/figs/roi_raw_6x.png',6,(x0,y0,x1,y1,10))
    # high pass on L
    hp=L-boxblur(L,10)
    out=np.zeros_like(crop); s=np.clip((hp+6)/24,0,1)*255
    out[:,:,0]=out[:,:,1]=out[:,:,2]=s
    save_rgb(out,'hardware/analysis/reopen/figs/roi_hp_6x.png',6,(x0,y0,x1,y1,10))
    # green channel stretched
    g=stretch(G,1,99)*255
    out=np.zeros_like(crop); out[:,:,0]=out[:,:,1]=out[:,:,2]=g
    save_rgb(out,'hardware/analysis/reopen/figs/roi_green_6x.png',6,(x0,y0,x1,y1,10))
    # sobel
    from PIL import ImageFilter
    gi=Image.fromarray(np.clip(g,0,255).astype(np.uint8)).filter(ImageFilter.FIND_EDGES)
    save_rgb(np.asarray(gi).astype(np.float64),'hardware/analysis/reopen/figs/roi_edge_6x.png',6,(x0,y0,x1,y1,10))
    print('done', crop.shape)
