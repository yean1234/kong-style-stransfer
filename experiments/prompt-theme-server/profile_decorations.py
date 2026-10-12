"""Explicit source-object profiles; generated pixels are located anew per run."""
import hashlib
import json
from pathlib import Path
import numpy as np
from PIL import Image, ImageDraw, ImageFilter
from skimage.color import deltaE_ciede2000, rgb2lab
from object_decorations import request_image
from style_gate import run_candidates
from town_profiles import extend_town_profiles

PROFILE_PATH=Path(__file__).with_name('decoration_profiles.json')
VERSION='object-profiles-v1'


def profiles():
    return extend_town_profiles(json.loads(PROFILE_PATH.read_text(encoding='utf8'))['objects'])


def get_profile(obj):
    p=profiles().get(obj['id'])
    if not p:raise ValueError(obj['id']+': 맞춤 설정이 없습니다. 먼저 영역·좌표를 등록해주세요.')
    if p['box']!=obj['box']:raise ValueError(obj['id']+': TMX 위치/크기가 변경됐습니다. 맞춤 설정을 다시 검수해주세요.')
    return p


def rectangles(size, boxes):
    image=Image.new('L',size);draw=ImageDraw.Draw(image)
    for x0,y0,x1,y1 in boxes:draw.rectangle((x0,y0,x1-1,y1-1),fill=255)
    return np.array(image)>0


def profile_masks(original,p):
    if list(original.size)!=p['box'][2:]:raise ValueError('Profile source size mismatch')
    a=np.array(original).astype(float);r,g,b,alpha=a.transpose(2,0,1)
    opaque=alpha>200;size=original.size;w,h=size
    protected=rectangles(size,p['protected_rects'])
    if p.get('surface_profile'):
        if p['kind']=='building':
            from object_decorations import surface_masks
            snow,lights=surface_masks(original,'building')
        else:
            # Only decorate existing upper surfaces, never supports or empty space.
            snow=opaque.copy();snow[int(h*.72):]=False
            if p['kind']=='lamp':snow[int(h*.45):]=False
            lights=snow.copy()
        ornaments=lights
    elif p['kind']=='building':
        area=Image.new('L',size);draw=ImageDraw.Draw(area)
        for polygon in p['snow_polygons']:draw.polygon([tuple(v) for v in polygon],fill=255)
        snow=(np.array(area)>0)&opaque&(b>r*1.15)&(b>g*1.02)
        line=Image.new('L',size);draw=ImageDraw.Draw(line)
        for path in p['eaves']:draw.line([tuple(v) for v in path],fill=255,width=p['light_band'])
        lights=(np.array(line)>0)&opaque
        ornaments=rectangles(size,[p['ornament_rect']])&opaque
    elif p['kind']=='tree':
        snow=opaque&(g>r*1.05)&(g>b*1.12)&rectangles(size,[p['foliage_rect']])
        lights=np.array(Image.fromarray((snow*255).astype('uint8')).filter(ImageFilter.MaxFilter(5)))>0
        lights&=opaque;ornaments=lights
    elif p['kind']=='fountain':
        yy,xx=np.indices((h,w));cx,cy,rx,ry=p['rim']
        distance=((xx-cx)/rx)**2+((yy-cy)/ry)**2
        rim=(distance>p['rim_range'][0])&(distance<p['rim_range'][1])
        stone=opaque&(np.abs(r-g)<22)&(np.abs(g-b)<22)&(a[:,:,:3].min(2)>70)
        snow=stone&(rim|rectangles(size,[p['cap']]))
        lights=opaque&(distance>.78)&(distance<1.65)&(yy>h*.22)&(yy<h*.87)
        ornaments=lights
    else:
        snow=rectangles(size,[p['snow_rect']]);lights=rectangles(size,[p['wire_rect']]);ornaments=lights
    return {k:v&~protected for k,v in {'snow':snow,'lights':lights,'garland':ornaments}.items()},protected


def grow(seed,candidate,steps):
    mask=seed.copy()
    for _ in range(steps):
        expanded=np.array(Image.fromarray((mask*255).astype('uint8')).filter(ImageFilter.MaxFilter(3)))>0
        next_mask=mask|(expanded&candidate)
        if np.array_equal(next_mask,mask):break
        mask=next_mask
    return mask


def extract_profile(original,generated,p,names):
    regions,protected=profile_masks(original,p)
    rgb=np.array(generated.convert('RGB')).astype(float);ref=np.array(original).astype(float)
    r,g,b=rgb.transpose(2,0,1);low=rgb.min(2);high=rgb.max(2)
    delta=np.max(np.abs(rgb-ref[:,:,:3]),axis=2)
    mask=np.zeros(low.shape,bool);counts={}
    for name in names:
        region=regions[name]
        if name=='snow':
            seed=(low>185)&((high-low)<45)&(delta>24)&region
            candidate=(low>65)&((high-low)/np.maximum(high,1)<.4)&(delta>15)&region
        elif name=='lights':
            seed=(r>165)&(g>85)&(b<135)&(r>g*1.12)&(delta>24)&region
            nearby=grow(seed,region,2)
            # Retain sockets/cables connected to bulbs, rather than yellow pixels only.
            seed=nearby&((high<100)|seed)
            candidate=region&(delta>18)&((high<100)|((r>150)&(g>75)&(b<140)))
        else:
            seed=region&(delta>24)&((g>r*1.15)|(r>g*1.5))&((high-low)>60)
            candidate=region&(delta>18)&((high-low)>30)
        selected=grow(seed,candidate,32)&region&~protected
        counts[name]=int(selected.sum());mask|=selected
    a=np.where(mask,ref[:,:,3],0).astype('uint8')
    return Image.fromarray(np.dstack((rgb.astype('uint8'),a))),regions,protected,counts


def find_strip(raw,name,key_strip):
    keyed=key_strip(raw);a=np.array(keyed);rgb=a[:,:,:3].astype(float);r,g,b=rgb.transpose(2,0,1)
    if name=='snow':seed=(rgb.min(2)>185)&(rgb.max(2)-rgb.min(2)<45)&(a[:,:,3]>0)
    elif name=='lights':seed=(r>165)&(g>85)&(b<165)&(r>g*1.08)&(a[:,:,3]>0)
    else:return keyed
    rows=np.flatnonzero(seed.any(axis=1))
    if not len(rows):raise ValueError(name+': 장식 색상 검출 실패')
    groups=np.split(rows,np.flatnonzero(np.diff(rows)>max(4,keyed.height//50))+1)
    group=groups[-1] if name=='lights' else max(groups,key=lambda ys:int(seed[ys].sum()))
    top=max(0,int(group[0])-(max(8,keyed.height//12) if name=='lights' else 3))
    bottom=min(keyed.height,int(group[-1])+5)
    if name=='lights':
        # Snow may be generated above/below the wire in any order.
        a[(rgb.min(2)>170)&(rgb.max(2)-rgb.min(2)<50),3]=0
    out=Image.fromarray(a).crop((0,top,keyed.width,bottom))
    return out.crop(out.getbbox())


def _palette_prompt(palette_plan, names):
    colors=[]
    for name in names:
        values=palette_plan['decoration_palettes'][name]
        colors.append(name+': '+', '.join(f"{value['role']} {value['hex']}" for value in values))
    shared=', '.join(f"{value['role']} {value['hex']}" for value in palette_plan.get('shared_palette',[]))
    return ' Use only these frozen decoration palette colors where practical: '+ '; '.join(colors)+'. Shared outline/wire colors: '+shared+'.'


def _resize(image,size):
    resample=Image.Resampling.BOX if image.width>=size[0] and image.height>=size[1] else Image.Resampling.LANCZOS
    return image.resize(size,resample)


def _integer_nearest_fit(image, size):
    """Fit a strip with an integer pixel-art scale, preserving its aspect ratio."""
    width, height = image.size
    target_width, target_height = size
    if width <= target_width and height <= target_height:
        scale = max(1, min(target_width // width, target_height // height))
        output_size = (width * scale, height * scale)
    else:
        divisor = max(1, int(np.ceil(max(width / target_width, height / target_height))))
        output_size = (max(1, width // divisor), max(1, height // divisor))
    return image.resize(output_size, Image.Resampling.NEAREST)


def _strip_candidate(raw,source,names,p,original,folder):
    raw=_resize(raw.convert('RGB'),source.size)
    source_rgb=np.asarray(source.convert('RGB'),dtype=np.uint8)
    raw_rgb=np.asarray(raw,dtype=np.uint8)
    delta=deltaE_ciede2000(rgb2lab(source_rgb.astype(np.float32)/255),rgb2lab(raw_rgb.astype(np.float32)/255))
    changed=delta>5.0
    Image.fromarray(np.clip(delta*8,0,255).astype('uint8')).save(folder/'strip-change-map.png')
    rows=np.flatnonzero(changed.any(axis=1))
    gap=max(4,source.height//50)
    groups=np.split(rows,np.flatnonzero(np.diff(rows)>gap)+1) if len(rows) else []
    groups=[group for group in groups if len(group)]
    groups=sorted(groups,key=lambda group:(-int(changed[group].any(axis=0).sum()),int(group[0])))[:len(names)]
    groups.sort(key=lambda group:int(group[0]))
    generated=np.asarray(original.convert('RGB')).copy()
    for index,(name,group) in enumerate(zip(names,groups)):
        ys,xs=np.where(changed[group[0]:group[-1]+1])
        if not len(xs):continue
        x0,x1=int(xs.min()),int(xs.max())+1
        y0,y1=int(group[0]),int(group[-1])+1
        alpha=changed[y0:y1,x0:x1]
        pixels=np.dstack((raw_rgb[y0:y1,x0:x1],alpha.astype('uint8')*255))
        strip=Image.fromarray(pixels,'RGBA')
        strip.save(folder/f'{name}-strip.png')
        rect=p['snow_rect'] if name=='snow' else p['wire_rect']
        tx0,ty0,tx1,ty1=rect;target=(tx1-tx0,ty1-ty0)
        if name=='snow':
            tile=_integer_nearest_fit(strip,target)
            pixels=np.asarray(tile)
            mask=pixels[:,:,3]>0
            px=tx0+(target[0]-tile.width)//2;py=ty0+(target[1]-tile.height)//2
            destination=generated[py:py+tile.height,px:px+tile.width]
            destination[mask]=pixels[:,:,:3][mask]
        else:
            factor=max(1,int(np.ceil(max(strip.width/target[0],strip.height/target[1]))))
            if factor>1:
                strip=strip.resize((max(1,strip.width//factor),max(1,strip.height//factor)),Image.Resampling.NEAREST)
            for x in range(tx0,tx1,strip.width):
                piece=strip.crop((0,0,min(strip.width,tx1-x),min(strip.height,ty1-ty0)))
                piece_pixels=np.asarray(piece)
                mask=piece_pixels[:,:,3]>0
                destination=generated[ty0:ty0+piece.height,x:x+piece.width]
                destination[mask]=piece_pixels[:,:,:3][mask]
    return Image.fromarray(generated,'RGB')


def generate_profile(folder,original,obj,spec,flux,key_strip,on_candidate=None):
    p=get_profile(obj)
    unsupported=set(spec['decorations'])-set(p['supported'])
    if unsupported:raise ValueError(obj['id']+': 장식 설정 없음: '+', '.join(sorted(unsupported)))
    palette_plan=spec['palette_plan']
    source=Image.new('RGB',tuple(p['canvas']),(128,128,128))
    scaled=original.resize((original.width*p['scale'],original.height*p['scale']),Image.Resampling.NEAREST)
    source.paste(scaled,tuple(p['offset']),scaled)
    labels={'snow':'thin settled snow caps with shaded edges','lights':'Christmas bulb strings with their connecting cables','garland':'evergreen wreaths and garlands'}
    names=spec['decorations']
    if p['mode']=='attached':
        prompt=f'Keep this {p["kind"]} at exactly the same position, size and shape. Add only '+', '.join(labels[k] for k in names)+f'. Placement guide for requested decorations: {p["placement_prompt"]}. Theme: {spec["theme"]}. Pixel art, solid gray background, no new objects, no cast shadows.'
    else:
        source=Image.new('RGB',tuple(p['canvas']),'magenta');source.paste(scaled,tuple(p['offset']),scaled)
        prompt=f'Replace the stall with {len(names)} separate horizontal pixel-art decoration strips: '+ '; '.join(labels[k] for k in names)+'. Solid magenta background. No stall, no cloth, no shadows, no text.'
    snapshot=dict(p,profile_version=VERSION,source_rgba_sha256=hashlib.sha256(original.tobytes()).hexdigest())
    (folder/'profile.json').write_text(json.dumps(snapshot,ensure_ascii=False,indent=2),encoding='utf8')
    for name,region in profile_masks(original,p)[0].items():Image.fromarray((region*255).astype('uint8')).save(folder/(name+'-region.png'))
    regions,protected=profile_masks(original,p)
    palette_text=_palette_prompt(palette_plan,names)
    def generate(attempt_folder,attempt,feedback,frozen_palette):
        current_prompt=prompt+palette_text
        if feedback:current_prompt+=' '+feedback
        raw=request_image(attempt_folder,source,current_prompt,flux,p['strength'],pipeline=VERSION)
        if p['mode']=='attached':
            canvas=_resize(raw,tuple(p['canvas']))
            ox,oy=p['offset'];sw,sh=scaled.size
            high=canvas.crop((ox,oy,ox+sw,oy+sh))
            aligned=_resize(high,original.size)
            high.save(attempt_folder/'aligned-high.png');aligned.save(attempt_folder/'aligned-reference.png')
            generated=aligned
        else:
            generated=_strip_candidate(raw,source,names,p,original,attempt_folder)
            high=generated
        timing=attempt_folder/'request-timing.json'
        metadata=json.loads(timing.read_text(encoding='utf8')) if timing.exists() else {}
        return generated,high,metadata
    result,overlay=run_candidates(folder,original,names,spec['theme'],palette_plan,regions,protected,generate,on_candidate)
    result['profile']=obj['id']
    result['source_rgba_sha256']=hashlib.sha256(original.tobytes()).hexdigest()
    (folder/'extraction.json').write_text(json.dumps({'method':'change_mask_with_palette_guidance',
        'palette_hash':palette_plan['plan_hash'],'gate':result},ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    if overlay is None:
        return result,None
    counts=result.get('metrics',{}).get('decoration_px',{})
    result['extraction_counts']=counts
    (folder/'gate.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    Image.fromarray((protected*255).astype('uint8')).save(folder/'protected-region.png')
    return result,overlay
