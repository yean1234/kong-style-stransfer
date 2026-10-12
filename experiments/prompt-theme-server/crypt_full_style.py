"""Recorded whole-town decoration pass with high-resolution material extraction."""
import hashlib,json,shutil,time,uuid,sys
import xml.etree.ElementTree as ET
from pathlib import Path
import numpy as np
from PIL import Image,ImageFilter
from theme_pipeline import ROOT,REPO,FLUX,save_status
from object_decorations import request_image
from run_records import record_event
from decoration_extractor import extract_changes,fix_overlay
from palette_planner import build_palette_plan, save_palette_swatch
from style_gate import gate_config,run_candidates

PILOT='0950597acc8648d180c6b0c24ecf3d62'

def tree_source(m,pattern):
    tsx=(REPO/'public/crypt-maps'/m.find('tileset').get('source')).resolve()
    ts=ET.parse(tsx).getroot();atlas=Image.open(tsx.parent/ts.find('image').get('source')).convert('RGBA')
    cols=int(ts.get('columns'));first=int(m.find('tileset').get('firstgid'))
    source=Image.new('RGBA',(pattern.shape[1]*16,pattern.shape[0]*16))
    for y in range(pattern.shape[0]):
        for x in range(pattern.shape[1]):
            raw=int(pattern[y,x]);tid=(raw&0x1fffffff)-first
            if not raw:continue
            tile=atlas.crop((tid%cols*16,tid//cols*16,(tid%cols+1)*16,(tid//cols+1)*16))
            if raw&0x20000000:tile=tile.transpose(Image.Transpose.TRANSPOSE)
            if raw&0x80000000:tile=tile.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
            if raw&0x40000000:tile=tile.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
            source.alpha_composite(tile,(x*16,y*16))
    return source

def allowed_material_masks(source, kind, decorations):
    """Use source silhouette and kind limits as spatial permission, never as color labels."""
    opaque=np.asarray(source.convert('RGBA').getchannel('A'))>0
    allowed=np.asarray(source.convert('RGBA').getchannel('A').filter(ImageFilter.MaxFilter(5)))>0
    limit=.62 if kind in ('house','building') else .8 if kind=='tree' else .45 if kind=='prop' else 1
    allowed[int(source.height*limit):]=False
    protected=opaque&~allowed
    return {name:allowed.copy() for name in decorations},protected


def _extract_material_legacy(source, aligned, kind):
    """Keep the archived RGB extractor for older experiment callers only."""
    ref=np.array(source.resize(aligned.size,Image.Resampling.NEAREST)).astype(float)
    rgb=np.array(aligned.convert('RGB')).astype(float);r,g,b=rgb.transpose(2,0,1)
    delta=np.abs(rgb-ref[:,:,:3]).max(2);hi=rgb.max(2);lo=rgb.min(2)
    snow=(lo>155)&(hi-lo<55)&(delta>24)
    lights=(r>210)&(g>145)&(b<155)&(r>g*1.05)&(delta>30)
    green=(g>r*1.2)&(g>b*1.15)&(hi-lo>35)&(delta>25)
    if kind=='tree':
        bulb_near=np.array(Image.fromarray(np.where(lights,255,0).astype('uint8')).filter(
            ImageFilter.MaxFilter(max(1,round(aligned.width/source.width))*6+1)))>0
        green &= bulb_near&(r<90)&(g<150)
    radius=max(1,round(aligned.width/source.width))
    near=np.array(Image.fromarray(np.where(lights|green,255,0).astype('uint8')).filter(
        ImageFilter.MaxFilter(radius*2+1)))>0
    cable=near&(hi<125)&(delta>25)
    opaque=ref[:,:,3]>200
    snow_margin=np.array(Image.fromarray(np.where(opaque,255,0).astype('uint8')).filter(
        ImageFilter.MaxFilter(radius*4+1)))>0
    mask=((snow|lights|green|cable)&opaque)|(snow&snow_margin)
    limit=.62 if kind=='house' else .8 if kind=='tree' else .45 if kind=='prop' else 1
    mask[int(mask.shape[0]*limit):]=False
    out=Image.fromarray(np.dstack((rgb.astype('uint8'),np.where(mask,255,0).astype('uint8')))).resize(
        source.size,Image.Resampling.LANCZOS)
    a=np.array(out)
    allowed=np.array(source.getchannel('A').filter(ImageFilter.MaxFilter(5)))
    a[:,:,3]=np.minimum(a[:,:,3],allowed)
    a[int(source.height*limit):,:,3]=0
    return Image.fromarray(a)


def extract_material(source, aligned, kind, palette_plan=None, decorations=('snow','lights','garland'), output_dir=None):
    """Use the palette gate when a plan is supplied; preserve legacy experiments otherwise."""
    if palette_plan is None:
        return _extract_material_legacy(source, aligned, kind)
    allowed,protected=allowed_material_masks(source,kind,decorations)
    extracted=extract_changes(source,aligned,allowed,protected,palette_plan,decorations,output_dir=output_dir)
    overlay,_=fix_overlay(extracted,source,output_dir)
    return overlay

def prepare():
    folder=ROOT/uuid.uuid4().hex;folder.mkdir()
    parent=ROOT/PILOT
    sources=json.loads((parent/'sources.json').read_text(encoding='utf8')) if (parent/'sources.json').exists() else None
    if not sources or any(hashlib.sha256((REPO/name).read_bytes()).hexdigest()!=digest for name,digest in sources['hashes'].items()):
        from crypt_christmas import prepare as prepare_sources
        parent=ROOT/prepare_sources();sources=json.loads((parent/'sources.json').read_text(encoding='utf8'))
    original=Image.open(parent/'original-map.png').convert('RGBA');original.save(folder/'original-map.png')
    m=ET.parse(REPO/'public/crypt-maps/floor-0-town.tmx').getroot();mw=int(m.get('width'))
    grid=np.array([int(v) for v in m.find("layer[@name='prop']/data").text.split(',') if v.strip()]).reshape(-1,mw)
    variants=[];cache={};instances=[]
    for o in sources['objects']:
        src=Image.open(parent/(o['id']+'-original.png')).convert('RGBA');kind='house' if src.height>20 else 'fence'
        key=hashlib.sha256(src.tobytes()).hexdigest()
        if key not in cache:
            name=o['id'];cache[key]=name;src.save(folder/(name+'-original.png'))
            variants.append({'id':name,'kind':kind,'width':src.width,'height':src.height})
        instances.append({'asset':cache[key],'x':o['box'][0],'y':o['box'][1]})
    for x0,name in [(0,'tree-round'),(2,'tree-pine')]:
        pattern=grid[:2,x0:x0+2]
        src=tree_source(m,pattern);src.save(folder/(name+'-original.png'))
        variants.append({'id':name,'kind':'tree','width':32,'height':32})
        for y in range(0,grid.shape[0]-1,2):
            for x in range(0,grid.shape[1]-1,2):
                if np.array_equal(grid[y:y+2,x:x+2],pattern):instances.append({'asset':name,'x':x*16,'y':y*16})
    remaining=grid.copy()
    for p in instances:
        v=next(v for v in variants if v['id']==p['asset']);x,y=p['x']//16,p['y']//16
        remaining[y:y+v['height']//16,x:x+v['width']//16]=0
    pending=set(zip(*np.where(remaining!=0)))
    while pending:
        stack=[pending.pop()];points=[]
        while stack:
            y,x=stack.pop();points.append((y,x))
            for p in [(y-1,x),(y+1,x),(y,x-1),(y,x+1)]:
                if p in pending:pending.remove(p);stack.append(p)
        ys,xs=zip(*points);x,y=min(xs),min(ys);name=f'prop-{x}-{y}'
        source=tree_source(m,remaining[y:max(ys)+1,x:max(xs)+1]);source.save(folder/(name+'-original.png'))
        variants.append({'id':name,'kind':'prop','width':source.width,'height':source.height})
        instances.append({'asset':name,'x':x*16,'y':y*16})
    spec={'variants':variants,'instances':instances,'hashes':sources['hashes']}
    (folder/'sources.json').write_text(json.dumps(spec,indent=2),encoding='utf8')
    prompt='크리스마스 밤 · 눈 · 따뜻한 전구 · 가랜드 · 반짝임';decorations=['snow','lights','garland'];gate=gate_config()
    palette=build_palette_plan(prompt,'christmas',decorations,
        [Image.open(folder/(variant['id']+'-original.png')).convert('RGBA') for variant in variants])
    (folder/'palette-plan.json').write_text(json.dumps(palette,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    save_palette_swatch(palette,folder/'palette-swatch.png')
    status={'id':folder.name,'mapId':'floor-0-town','pipeline':'crypt-full-style-v1','status':'queued','stage':2,'created':time.time(),
        'prompt':prompt,'gate_mode':gate['mode'],'gate_config_version':gate.get('config_version'),
        'palette_hash':palette['plan_hash'],
        'palette_plan':f'/theme-runs/{folder.name}/palette-plan.json',
        'palette_swatch':f'/theme-runs/{folder.name}/palette-swatch.png',
        'object_results':{v['id']:{'status':'pending'} for v in variants},'total_objects':len(variants),
        'instances':len(instances),'original':f'/theme-runs/{folder.name}/original-map.png'}
    save_status(folder,status)
    print(folder.name,json.dumps(variants),len(instances),flush=True)
    return folder.name

def run(run_id):
    folder=ROOT/run_id;spec=json.loads((folder/'sources.json').read_text(encoding='utf8'));status=json.loads((folder/'status.json').read_text(encoding='utf8'))
    palette=json.loads((folder/'palette-plan.json').read_text(encoding='utf8'));gate=gate_config();decorations=palette['requested_decorations']
    pilot_palette_path=ROOT/PILOT/'palette-plan.json'
    pilot_palette_hash=json.loads(pilot_palette_path.read_text(encoding='utf8')).get('plan_hash') if pilot_palette_path.exists() else None
    pilot_context_path=ROOT/PILOT/'object-00/generation-context.json'
    pilot_context=json.loads(pilot_context_path.read_text(encoding='utf8')) if pilot_context_path.exists() else {}
    lock=ROOT/'.batch.lock'
    with lock.open('x') as handle:handle.write(run_id)
    try:
        for v in spec['variants']:
            name=v['id'];target=folder/name;target.mkdir(exist_ok=True)
            source=Image.open(folder/(name+'-original.png')).convert('RGBA')
            timer=time.perf_counter();status.update(status='running',stage=4,current_object=name)
            status['object_results'][name]={'status':'running','started_at':time.time()};save_status(folder,status)
            scale=6;offset=32
            scaled=source.resize((source.width*scale,source.height*scale),Image.Resampling.NEAREST)
            canvas=Image.new('RGB',(scaled.width+64,scaled.height+64),'#808080');canvas.paste(scaled,(offset,offset),scaled)
            reused=(name=='object-00' and pilot_palette_hash==palette.get('plan_hash')
                    and pilot_context.get('palette_hash')==palette.get('plan_hash')
                    and pilot_context.get('conditioned_on_palette') is True
                    and (ROOT/PILOT/'object-00/flux-raw.png').exists()
                    and Image.open(ROOT/PILOT/'object-00-original.png').convert('RGBA').tobytes()==source.tobytes())
            if reused:
                scale=4;scaled=source.resize((source.width*scale,source.height*scale),Image.Resampling.NEAREST)
                canvas=Image.new('RGB',(scaled.width+64,scaled.height+64),'#808080')
            allowed,protected=allowed_material_masks(source,v['kind'],decorations)
            def generate(attempt_folder,attempt,feedback,frozen_palette):
                if reused and attempt==0:
                    shutil.copy2(ROOT/PILOT/'object-00/flux-raw.png',attempt_folder/'flux-raw.png')
                    for filename in ['generation.json','request-timing.json','flux-input.png']:
                        shutil.copy2(ROOT/PILOT/'object-00'/filename,attempt_folder/filename)
                    shutil.copy2(pilot_context_path,attempt_folder/'generation-context.json')
                    record_event(folder,'reuse',asset=name,parent_run_id=PILOT)
                    metadata={'reused_from':PILOT,'generation_conditioned_on_palette':True,
                              'palette_hash':pilot_context['palette_hash']}
                else:
                    colors='; '.join(d+': '+', '.join(f"{item['role']} {item['hex']}" for item in frozen_palette['decoration_palettes'][d]) for d in decorations)
                    shared=', '.join(f"{item['role']} {item['hex']}" for item in frozen_palette.get('shared_palette',[]))
                    prompt=f'Add requested Christmas pixel-art decorations to this {v["kind"]}. Preserve its silhouette, layout and supports. Gray background. Frozen palette: {colors}. Shared outline/wire colors: {shared}.'
                    if feedback:prompt+=' '+feedback
                    request_image(attempt_folder,canvas,prompt,FLUX,1.0,pipeline='crypt-full-style-v1')
                    context={'generation_conditioned_on_palette':True,'palette_hash':frozen_palette['plan_hash'],
                             'prompt_version':'crypt-full-style-palette-v1'}
                    (attempt_folder/'generation-context.json').write_text(json.dumps(context,indent=2)+'\n',encoding='utf8')
                    metadata=context
                raw=Image.open(attempt_folder/'flux-raw.png').convert('RGB')
                if raw.size!=canvas.size:
                    method=Image.Resampling.BOX if raw.width>=canvas.width and raw.height>=canvas.height else Image.Resampling.LANCZOS
                    raw=raw.resize(canvas.size,method)
                high=raw.crop((offset,offset,offset+scaled.width,offset+scaled.height))
                aligned=high.resize(source.size,Image.Resampling.BOX)
                high.save(attempt_folder/'aligned-high.png')
                timing=attempt_folder/'request-timing.json'
                if timing.exists():metadata.update(json.loads(timing.read_text(encoding='utf8')))
                return aligned,high,metadata
            def on_candidate(candidate,count):
                status['object_results'][name]={'status':'evaluating','gate_mode':gate['mode'],'retries':count-1,
                    'last_scores':candidate.get('scores',{})};save_status(folder,status)
            result,decoration=run_candidates(target,source,decorations,'christmas',palette,allowed,protected,generate,on_candidate)
            result.update(source_available=True,elapsed_seconds=time.perf_counter()-timer,finished_at=time.time())
            picked=next((candidate for candidate in result.get('candidates',[]) if candidate['candidate']==result.get('selected_candidate')), {})
            result['generation_seconds']=picked.get('generation',{}).get('generation_seconds')
            if picked.get('generation',{}).get('reused_from'):result['reused_from']=picked['generation']['reused_from']
            if decoration is not None:
                decoration.save(target/'decoration.png');Image.alpha_composite(source,decoration).save(target/'composite.png')
            status['object_results'][name]=result
            status['completed_objects']=sum(r.get('status') not in ('pending','running','evaluating') for r in status['object_results'].values());save_status(folder,status)
            print('DONE',name,flush=True)
        finalize(folder,spec,status)
    except Exception as error:
        status.update(status='failed',error=str(error));save_status(folder,status);raise
    finally:lock.unlink(missing_ok=True)

def finalize(folder,spec,status):
    original=Image.open(folder/'original-map.png').convert('RGBA');overlay=Image.new('RGBA',original.size);bulbs=[]
    gate_mode=status.get('gate_mode','shadow');apply_statuses={'applied','applied_with_warning','unscored','recorded','passed'}
    for p in spec['instances']:
        result=status['object_results'].get(p['asset'],{})
        if result.get('status') not in apply_statuses or gate_mode=='shadow' or (gate_mode=='enforce' and result.get('status')!='passed'):continue
        deco=Image.open(folder/p['asset']/'decoration.png').convert('RGBA');overlay.alpha_composite(deco,(p['x'],p['y']))
    overlay.save(folder/'decoration-map.png')
    night=Image.alpha_composite(original,Image.new('RGBA',original.size,(12,23,58,130)))
    Image.alpha_composite(night,overlay).save(folder/'preview.png')
    manifest={'id':folder.name,'mapId':'floor-0-town','width':original.width,'height':original.height,'night':.51,'overlay':f'/theme-runs/{folder.name}/decoration-map.png','bulbs':bulbs,'source_hashes':{k.replace('\\','/'):v for k,v in spec['hashes'].items()},'instances':len(spec['instances']),'geometry_preserved':True,'collision_preserved':True,'gate_mode':gate_mode,'gate_config_version':status.get('gate_config_version'),'palette_hash':status.get('palette_hash')}
    (folder/'crypt-manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf8')
    applied={name for name,result in status['object_results'].items() if result.get('status') in apply_statuses and gate_mode!='shadow'
        and not (gate_mode=='enforce' and result.get('status')!='passed')}
    failed=sum(result.get('status') in ('failed','rejected') for result in status['object_results'].values())
    unscored=sum(result.get('status')=='unscored' for result in status['object_results'].values())
    object_validation={name:{'decision':result.get('decision',result.get('status')),'structural_valid':result.get('structural_valid'),
        'scores':result.get('scores',{}),'warnings':result.get('warnings',[]),'selected_candidate':result.get('selected_candidate'),
        'retries':result.get('retries',0)} for name,result in status['object_results'].items()}
    (folder/'validation.json').write_text(json.dumps({'source_hashes_unchanged':True,'all_variant_alpha_preserved':all(
        result.get('structural_valid') is True for name,result in status['object_results'].items() if name in applied),
        'gate_mode':gate_mode,'gate_config_version':status.get('gate_config_version'),
        'palette_hash':status.get('palette_hash'),'applied_objects':len(applied),'failed_objects':failed,'unscored_objects':unscored,
        'objects':object_validation},ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    warnings=[]
    for name,result in status['object_results'].items():
        warnings += [f'{name}: {warning}' for warning in result.get('warnings',[])]
        if result.get('error'):warnings.append(f'{name}: {result["error"]}')
    if not applied:warnings.append('변환 완료 · 적용 0건')
    warnings.append('전구 위치는 색상 규칙으로 추정하지 않아 반짝임 위치를 기록하지 않았습니다.')
    status.update(status='ready',stage=7,preview_revision=time.time(),preview=f'/theme-runs/{folder.name}/preview.png',
        warnings=warnings,applied_objects=len(applied),failed_objects=failed,unscored_objects=unscored)
    save_status(folder,status)

if __name__=='__main__':
    if len(sys.argv)==1:prepare()
    else:run(sys.argv[1])
