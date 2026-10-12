"""Preserve source pixels/geometry; generate separate, reviewable FLUX decorations."""
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
import os, json, uuid, hashlib, threading, re, time
import xml.etree.ElementTree as ET
import numpy as np
import requests
from PIL import Image
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from run_records import record_event, review_html
from run_changes import changes as run_changes
from pydantic import BaseModel, Field, ValidationError
from profile_decorations import VERSION, profiles, get_profile, generate_profile, profile_masks
from object_decorations import BACKEND as STYLE_BACKEND
from palette_planner import build_palette_plan, PaletteUnavailableError, save_palette_swatch
from style_gate import gate_config, GateConfigurationError

REPO = Path(os.environ.get('THEME_PROJECT', Path(__file__).resolve().parents[2]))
ROOT = REPO / 'public/theme-runs'
ROOT.mkdir(parents=True, exist_ok=True)
FLUX = os.environ.get('THEME_FLUX_URL', 'http://127.0.0.1:8765')
STATE = os.environ.get('THEME_STATE_URL', '')
@asynccontextmanager
async def lifespan(_app):
    recover_runs()
    yield

app = FastAPI(lifespan=lifespan)


@app.exception_handler(RequestValidationError)
async def request_validation_error(_request,error):
    # Uploaded document bytes must never be echoed in validation responses.
    return JSONResponse(status_code=422,content={'detail':[
        {key:item[key] for key in ('loc','msg','type') if key in item} for item in error.errors()]})


pool = ThreadPoolExecutor(max_workers=1)
busy = threading.Lock()
PRESETS = {
    'christmas': {'gain': [1.03, .97, .98], 'bias': [-.015, .0, .045]},
    'halloween': {'gain': [1.06, .88, .98], 'bias': [.005, -.015, .045]},
    'autumn': {'gain': [1.08, .93, .78], 'bias': [.015, .0, -.005]},
}
LABELS = {'snow': 'thin uneven settled snow strip', 'lights': 'dark sagging cable connecting eight warm golden bulbs', 'garland': 'thin festive evergreen garland strip', 'pumpkins': 'three small orange jack-o-lanterns in a horizontal row', 'leaves': 'a thin horizontal garland of orange autumn leaves'}

def parse_prompt(prompt):
    p = prompt.strip().lower()
    theme = next((k for k, words in [('christmas', ['크리스마스','christmas','겨울','winter']), ('halloween',['할로윈','halloween']), ('autumn',['가을','autumn','fall'])] if any(w in p for w in words)), None)
    if not theme:
        raise ValueError('현재 지원 테마: 크리스마스/겨울, 할로윈, 가을. 테마를 명시해주세요.')
    decorations = []
    for key, terms in {'snow':['눈','snow'], 'lights':['전구','조명','bulb','light'], 'garland':['가랜드','화환','garland','wreath'], 'pumpkins':['호박','pumpkin'], 'leaves':['낙엽','단풍','leaves']}.items():
        if any(w in p for w in terms) and not any(re.search(re.escape(w)+r'\s*(?:은|는|을|를)?\s*(?:없이|빼|제외|없)', p) for w in terms) and not any('no '+w in p for w in terms):
            decorations.append(key)
    night = ('밤' in p or 'night' in p) and not any(w in p for w in ['밤 말고','밤이 아닌','낮','daytime','no night'])
    twinkle = any(w in p for w in ['반짝','twinkl','깜빡']) and not any(w in p for w in ['반짝임 없이','반짝이지','no twinkl'])
    color = not any(w in p for w in ['색 변경 없이','색은 그대로','원본 색','no recolor'])
    return {'parser':'rules-v1', 'theme':theme, 'decorations':decorations, 'night':night, 'twinkle':twinkle and 'lights' in decorations, 'color':PRESETS[theme] if color else {'gain':[1,1,1],'bias':[0,0,0]}, 'warnings':['규칙 기반 해석입니다. 아래 설정을 확인하세요. 세부 색상명·광원 방향은 아직 자동 해석하지 않습니다.', '장식은 자동 마스크/정렬 결과이며 원본과 겹치는 부분은 미리보기에서 확인해야 합니다.']}

def sources(map_id='town'):
    if map_id not in {'town','harvest-village'}:raise ValueError('Unsupported style map')
    tmx = REPO/f'src/games/my-sample-rpg/assets/maps/{map_id}.tmx'
    m = ET.parse(tmx).getroot()
    ref = m.find('tileset')
    tsx = (tmx.parent/ref.get('source')).resolve()
    ts = ET.parse(tsx).getroot()
    image = (tsx.parent/ts.find('image').get('source')).resolve()
    return m, ts, Image.open(image).convert('RGBA'), {str(p.relative_to(REPO)):hashlib.sha256(p.read_bytes()).hexdigest() for p in [tmx,tsx,image]}

def catalog(map_id='town'):
    if map_id!='town':
        from map_style_catalog import catalog as map_catalog
        return map_catalog(map_id)
    m,_,_,_ = sources()
    return [{'id':o.get('name'), 'category':o.get('type'), 'box':[int(float(o.get(k,0))) for k in ['x','y','width','height']]} for o in m.findall('.//object') if o.get('type') in ['building','tree','fountain','lamp','flower','prop'] and float(o.get('width','0'))>0]

def recolor(image, color):
    a = np.array(image.convert('RGBA'))
    rgb = a[:,:,:3].astype(float)/255
    mapped = np.uint8(np.clip(rgb*np.array(color['gain'])+np.array(color['bias']),0,1)*255)
    a[:,:,:3][a[:,:,3]>0] = mapped[a[:,:,3]>0]
    return Image.fromarray(a)

def layers(color,map_id='town'):
    m,ts,atlas,hashes = sources(map_id)
    tw,th,mw,mh = [int(m.get(k)) for k in ['tilewidth','tileheight','width','height']]
    first = int(m.find('tileset').get('firstgid'))
    cols = int(ts.get('columns'))
    original = Image.new('RGBA',(mw*tw,mh*th))
    tinted = Image.new('RGBA',original.size)
    object_only = Image.new('RGBA',original.size)
    for layer in m.findall('layer'):
        if layer.get('visible','1')=='0': continue
        is_object = layer.get('name') not in ['ground','shadow_lower','shadow_upper']
        values = [int(v) for v in layer.find('data').text.replace('\n','').split(',') if v.strip()]
        for index, raw in enumerate(values):
            if not raw: continue
            tid = (raw&0x1fffffff)-first
            x,y=(tid%cols)*tw,(tid//cols)*th
            tile=atlas.crop((x,y,x+tw,y+th))
            if raw&0x20000000: tile=tile.transpose(Image.Transpose.TRANSPOSE)
            if raw&0x80000000: tile=tile.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
            if raw&0x40000000: tile=tile.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
            at=((index%mw)*tw,(index//mw)*th)
            original.alpha_composite(tile,at)
            tinted.alpha_composite(recolor(tile,color) if is_object else tile,at)
            if is_object: object_only.alpha_composite(tile,at)
    return original,tinted,object_only,hashes

def key_strip(image, kind=None):
    a=np.array(image.convert('RGBA'))
    f=a[:,:,:3].astype(float)
    key=(f[:,:,0]>40)&(f[:,:,2]>40)&(f[:,:,0]>f[:,:,1]*1.6)&(f[:,:,2]>f[:,:,1]*1.6)
    a[key]=0
    mask=a[:,:,3]>0
    ratio=float(mask.mean())
    if ratio<.002 or ratio>.72:
        raise ValueError('장식 분리 품질 검사 실패: 마젠타 배경/장식 경계를 확인하고 다시 생성해주세요.')
    result=Image.fromarray(a)
    if kind in ['snow','lights']:
        rows=np.flatnonzero(mask.any(axis=1))
        groups=np.split(rows,np.flatnonzero(np.diff(rows)>4)+1)
        groups=[g for g in groups if len(g)>=3]
        if groups:
            group=groups[0] if kind=='snow' else groups[-1]
            result=result.crop((0,int(group[0]),result.width,int(group[-1])+1))
    return result.crop(result.getbbox())

def align(original, strips):
    w,h=original.size
    out=Image.new('RGBA',(w,h))
    alpha=np.array(original)[:,:,3]
    for name,strip in strips.items():
        if name=='snow':
            band=strip.resize((w,max(3,min(10,h//14))),Image.Resampling.NEAREST)
            for x in range(w):
                ys=np.flatnonzero(alpha[:,x]>180)
                if len(ys): out.alpha_composite(band.crop((x,0,x+1,band.height)),(x,int(ys[0])))
        else:
            span=max(8,w-8)
            sw=min(span,96); sh=max(5,min(18,h//5,round(strip.height*sw/strip.width)))
            band=strip.resize((sw,sh),Image.Resampling.NEAREST)
            wide_rows=np.flatnonzero((alpha>180).sum(axis=1)>w*.65)
            wide_rows=wide_rows[wide_rows<h*.7]
            awning_bottom=int(wide_rows[-1]) if len(wide_rows) else int(h*.45)
            for offset in range(0,span,sw):
                piece=band.crop((0,0,min(sw,span-offset),sh))
                out.alpha_composite(piece,((w-span)//2+offset,max(0,awning_bottom-sh+1)))
    # Keep additions inside the original object silhouette; never fill the door hole.
    a=np.array(out);a[:,:,3]=np.minimum(a[:,:,3],alpha)
    return Image.fromarray(a)

def save_status(folder, data):
    record_event(folder, 'status', snapshot=data)
    tmp=folder/'status.tmp'
    tmp.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf8')
    for attempt in range(10):
        try:
            tmp.replace(folder/'status.json')
            break
        except PermissionError:
            if attempt==9:raise
            time.sleep(.1)

class Request(BaseModel):
    prompt: str = Field(min_length=2,max_length=1200)
    targets: list[str] = Field(default_factory=list, max_length=64)
    palette: dict | None = None
    reference_image: str | None = Field(default=None,max_length=12000000)
    reference_region: list[int] | None = Field(default=None,min_length=4,max_length=4)

@app.get('/health')
def health():
    try:
        gate=gate_config();mode=gate['mode'];gate_error=None
    except GateConfigurationError as error:
        gate={};mode=os.environ.get('THEME_GATE_MODE');gate_error=str(error)
    return {'status':'ok','parser':'rules-v1','pipeline':VERSION,'backend':'ComfyUI' if STYLE_BACKEND=='comfy' else 'FLUX',
        'gate_mode':mode,'gate_config_version':gate.get('config_version'),'gate_configuration_error':gate_error,
        'vlm':{'url_configured':bool(os.environ.get('THEME_VLM_URL')),'model_configured':bool(os.environ.get('THEME_VLM_MODEL'))},
        'busy':busy.locked() or (ROOT/'.batch.lock').exists()}

@app.post('/plan')
def plan(req: Request):
    try:
        configured=profiles()
        return {'spec':parse_prompt(req.prompt), 'objects':[dict(o,profile_label=configured[o['id']]['label']) for o in catalog() if o['id'] in configured], 'profile_version':VERSION}
    except ValueError as e: raise HTTPException(422,str(e))

@app.get('/runs')
def runs():
    return [json.loads(p.read_text(encoding='utf8')) for p in sorted(ROOT.glob('*/status.json'), key=lambda p:p.stat().st_mtime,reverse=True)]


class DocumentPlanRequest(Request):
    mapId: str = 'town'
    document_base64: str | None = Field(default=None,max_length=86000000)
    document_name: str = Field(default='',max_length=200)
    reviewed_requirements: list[dict] | None = Field(default=None,max_length=16)


@app.post('/town-vision/plan')
def town_vision_plan(req: DocumentPlanRequest):
    from town_vision_pipeline import plan
    if (ROOT/'.batch.lock').exists() or not busy.acquire(blocking=False):raise HTTPException(409,'다른 작업이 진행 중입니다.')
    try:
        document=None
        if req.document_base64:
            from document_planner import extract_document
            document=extract_document(req.document_base64)
            document['name']=req.document_name
        return plan(req.prompt,document,req.reviewed_requirements,req.targets,map_id=req.mapId)
    except Exception as error:raise HTTPException(422,str(error))
    finally:busy.release()


class GroundPropRequest(BaseModel):
    plan_id: str
    asset: str = Field(min_length=1,max_length=80)
    prompt: str = Field(min_length=3,max_length=300)


@app.post('/town-vision/ground-prop-pilot')
def ground_prop_submit(req: GroundPropRequest):
    from ground_prop_pilot import prepare,run
    if (ROOT/'.batch.lock').exists() or not busy.acquire(blocking=False):raise HTTPException(409,'Another generation is running')
    try:run_id=prepare(req.plan_id,req.asset,req.prompt)
    except Exception as error:
        busy.release();raise HTTPException(422,str(error))
    def work():
        try:run(run_id)
        finally:busy.release()
    pool.submit(work)
    return {'id':run_id}


class TownVisionRun(BaseModel):
    plan_id: str
    targets: list[str] = Field(min_length=1,max_length=64)


@app.post('/town-vision/runs')
def town_vision_submit(req: TownVisionRun):
    from town_vision_pipeline import prepare,run
    if (ROOT/'.batch.lock').exists() or not busy.acquire(blocking=False):raise HTTPException(409,'다른 작업이 진행 중입니다.')
    try:run_id=prepare(req.plan_id,req.targets)
    except Exception as error:
        busy.release();raise HTTPException(422,str(error))
    def work():
        try:run(run_id)
        finally:busy.release()
    pool.submit(work)
    return {'id':run_id}


@app.post('/runs/{run_id}/visual-accept')
def visual_accept(run_id:str):
    from town_vision_pipeline import accept
    try:accept(get_folder(run_id))
    except (ValueError,KeyError,FileNotFoundError) as error:raise HTTPException(409,str(error))
    return {'id':run_id,'status':'ready'}


@app.get('/integration/sample')
def integration_sample(mapId: str = 'floor-1-ruins'):
    if mapId == 'floor-1-ruins':
        from crypt_plan_style import sample
        return sample()
    if mapId != 'town': raise HTTPException(422, '지원하지 않는 통합 맵입니다.')
    sample = REPO/'contracts/samples/town'
    return {name: json.loads((sample/f'{name}.json').read_text(encoding='utf8')) for name in ('dsl', 'plan')}


class IntegrationRequest(BaseModel):
    dsl: dict
    plan: list[dict] = Field(min_length=1, max_length=64)
    mapId: str = 'floor-1-ruins'
    parent_run_id: str | None = None
    palette: dict | None = None
    reference_image: str | None = Field(default=None,max_length=12000000)
    reference_region: list[int] | None = Field(default=None,min_length=4,max_length=4)


class AutomaticPlanRequest(BaseModel):
    prompt: str = Field(min_length=2, max_length=1200)
    mapId: str = 'floor-1-ruins'


@app.post('/integration/plan')
def automatic_plan(req: AutomaticPlanRequest):
    from dsl_general import parse
    from planner import build
    from crypt_plan_style import baseline
    try:
        folder, manifest, sources = baseline()
        dsl = parse(req.prompt, req.mapId, bool(manifest['night']))
        rows = build(dsl, sources)
        return {'dsl':dsl, 'plan':rows, 'parent_run_id':folder.name,
                'spec':dict(dsl, warnings=dsl['warnings']),
                'objects':[{'id':r['asset'],'category':r['kind'],'profile_label':f"{r['asset']} · 미분류 소품 · {r['action']} · {r['instances']}곳"} for r in rows]}
    except (ValueError, KeyError, FileNotFoundError) as error:
        raise HTTPException(422, str(error))


@app.post('/integration/runs')
def integration_submit(req: IntegrationRequest):
    if req.dsl.get('target_maps') != [req.mapId]: raise HTTPException(422, '화면의 맵과 DSL 대상 맵이 다릅니다.')
    if req.mapId == 'floor-1-ruins':
        from crypt_plan_style import prepare_data, run as execute_plan
    elif req.mapId == 'town':
        from town_plan_style import prepare_data, run as execute_plan
    else: raise HTTPException(422, '지원하지 않는 통합 맵입니다.')
    if (ROOT/'.batch.lock').exists() or not busy.acquire(blocking=False):
        raise HTTPException(409, '이미 생성 중입니다.')
    try:
        if req.mapId == 'floor-1-ruins' and req.parent_run_id:
            from crypt_plan_style import baseline
            if baseline()[0].name != req.parent_run_id:
                raise ValueError('해석 후 적용 상태가 바뀌었습니다. 프롬프트를 다시 해석해주세요.')
        run_id = prepare_data(req.dsl, req.plan, req.reference_image, req.palette, req.reference_region)
    except (ValueError, KeyError) as error:
        busy.release()
        raise HTTPException(422, str(error))
    except Exception:
        busy.release()
        raise
    def work():
        try: execute_plan(run_id)
        finally: busy.release()
    pool.submit(work)
    return {'id': run_id}

def run(folder, req):
    data={'id':folder.name,'status':'running','stage':1,'prompt':req.prompt,'created':time.time(),'pipeline':VERSION}
    try:
        config=gate_config()
        spec=parse_prompt(req.prompt)
        selected=[o for o in catalog() if o['id'] in req.targets]
        data.update(spec=spec,gate_mode=config['mode'],gate_config_version=config.get('config_version'),
                    total_objects=len(selected),completed_objects=0,
                    object_results={o['id']:{'status':'pending'} for o in selected})
        save_status(folder,data)
        if STATE:
            before=requests.get(STATE,timeout=20);before.raise_for_status()
            (folder/'state-before.json').write_text(json.dumps(before.json(),ensure_ascii=False),encoding='utf8')
        data.update(stage=2,current_object=None);save_status(folder,data)
        original,tinted,objects,hashes=layers(spec['color'])
        original.save(folder/'original-map.png')
        crops={}
        for obj in selected:
            x,y,w,h=obj['box'];crop=objects.crop((x,y,x+w,y+h))
            crop.save(folder/(obj['id']+'-original.png'))
            corrected=recolor(crop,spec['color'])
            if not np.array_equal(np.array(crop)[:,:,3],np.array(corrected)[:,:,3]):
                raise ValueError('Recolor changed source alpha')
            corrected.save(folder/(obj['id']+'-color.png'))
            crops[obj['id']]=crop
        palette_plan=None
        if spec['decorations']:
            palette_plan=build_palette_plan(req.prompt,spec['theme'],spec['decorations'],list(crops.values()),
                req.palette,req.reference_image,req.reference_region,config['vlm']['timeout'])
            (folder/'palette-plan.json').write_text(json.dumps(palette_plan,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
            save_palette_swatch(palette_plan,folder/'palette-swatch.png')
            data.update(palette_hash=palette_plan['plan_hash'],palette_plan=f'/theme-runs/{folder.name}/palette-plan.json',
                        palette_swatch=f'/theme-runs/{folder.name}/palette-swatch.png')
            spec['palette_plan_hash']=palette_plan['plan_hash']
        data.update(spec=spec,stage=3,total_objects=len(selected));save_status(folder,data)
        tinted.save(folder/'color-map.png')
        overlays={}
        for index,obj in enumerate(selected):
            name=obj['id'];target=folder/name;target.mkdir(exist_ok=True)
            data.update(stage=4,current_object=name,total_objects=len(selected));save_status(folder,data)
            if not spec['decorations']:
                result={'status':'applied','decision':'applied','structural_valid':True,'scores':{},'warnings':[],
                        'selected_candidate':None,'retries':0,'gate_mode':config['mode']}
            else:
                try:
                    def on_candidate(candidate,count):
                        data['object_results'][name]={'status':'evaluating','gate_mode':config['mode'],
                            'retries':count-1,'last_scores':candidate.get('scores',{})}
                        save_status(folder,data)
                    result,overlay=generate_profile(target,crops[name],obj,
                        dict(spec,palette_plan=palette_plan),FLUX,key_strip,on_candidate)
                    if overlay is not None and result['status'] in ('applied','applied_with_warning','unscored','recorded','passed'):
                        if config['mode'] not in ('enforce','shadow') or result['status']=='passed':overlays[name]=overlay
                    else:
                        result['status']='failed' if result['status']!='rejected' else 'rejected'
                except Exception as error:
                    result={'status':'failed','decision':'failed','structural_valid':False,
                        'error':f'{type(error).__name__}: {error}','warnings':['object_processing_failed'],
                        'scores':{'status':'unscored','SC':None,'PQ':None,'O':None},'gate_mode':config['mode']}
            data['object_results'][name]=result
            data['completed_objects']=sum(value.get('status') not in ('pending','running','evaluating')
                                          for value in data['object_results'].values())
            save_status(folder,data)
        data.update(stage=5,current_object=None);save_status(folder,data)
        decoration_map=Image.new('RGBA',original.size);placements=[]
        if not spec['decorations']:
            successful=selected
        else:
            successful=[obj for obj in selected if obj['id'] in overlays]
        for obj in successful:
            name=obj['id'];x,y,w,h=obj['box']
            overlay=overlays.get(name,Image.new('RGBA',(w,h)))
            if not overlay.getbbox():continue
            overlay.save(folder/(name+'-decoration.png'))
            decoration_map.alpha_composite(overlay,(x,y))
            url='/theme-runs/'+folder.name+'/'+name+'-decoration.png'
            placements.append({'id':folder.name+'-'+name,'kind':'object','col':x/32,'row':y/32,'imageUrl':url,
                'anchor':'top-left','renderLayer':'decoration','sourceGroup':'prompt-theme','visible':True})
        applied_objects=len(successful)
        if applied_objects:
            settings={'runId':folder.name,'night':.58 if spec['night'] else 0,'twinkle':spec['twinkle'],'color':spec['color']}
            Image.new('RGBA',(1,1)).save(folder/'settings.png')
            placements.insert(0,{'id':folder.name+'-settings','kind':'object','col':0,'row':0,
                'imageUrl':'/theme-runs/'+folder.name+'/settings.png','renderLayer':'decoration','sourceGroup':'prompt-theme',
                'visible':True,'themeSettings':settings})
        data.update(stage=6,applied_objects=applied_objects);save_status(folder,data)
        if spec['night']:tinted=Image.alpha_composite(tinted,Image.new('RGBA',tinted.size,(7,19,46,148)))
        preview=Image.alpha_composite(tinted,decoration_map);preview.save(folder/'preview.png')
        unchanged=sources()[3]==hashes
        protected_overlap=0;out_of_bounds=0
        for obj in successful:
            if obj['id'] not in overlays:continue
            profile=get_profile(obj);regions,protected=__import__('profile_decorations').profile_masks(crops[obj['id']],profile)
            allowed=np.logical_or.reduce([regions[name] for name in spec['decorations']])
            alpha=np.asarray(overlays[obj['id']].getchannel('A'))>0
            out_of_bounds+=int((alpha&~allowed).sum());protected_overlap+=int((alpha&protected).sum())
        applied_names={obj['id'] for obj in successful}
        object_validation={name:{'decision':result.get('decision',result.get('status')),
            'structural_valid':result.get('structural_valid'),'scores':result.get('scores',{}),
            'warnings':result.get('warnings',[]),'selected_candidate':result.get('selected_candidate'),
            'retries':result.get('retries',0)} for name,result in data['object_results'].items()}
        failed_objects=sum(result.get('status') in ('failed','rejected') for result in data['object_results'].values())
        unscored_objects=sum(result.get('status')=='unscored' for result in data['object_results'].values())
        (folder/'manifest.json').write_text(json.dumps({'pipeline':VERSION,'spec':spec,'source_hashes':hashes,
            'objects':[obj for obj in selected if obj['id'] in applied_names],'placements':placements,
            'geometry_preserved':True,'alpha_preserved':out_of_bounds==0 and protected_overlap==0,
            'gate_mode':config['mode'],'gate_config_version':config.get('config_version'),
            'palette_hash':palette_plan.get('plan_hash') if palette_plan else None,
            'object_results':object_validation,'applied_objects':applied_objects},ensure_ascii=False,indent=2),encoding='utf8')
        (folder/'validation.json').write_text(json.dumps({'source_hashes_unchanged':unchanged,
            'alpha_preserved':out_of_bounds==0 and protected_overlap==0,'out_of_bounds_pixels':out_of_bounds,
            'protected_overlap_px':protected_overlap,'gate_mode':config['mode'],
            'gate_config_version':config.get('config_version'),'applied_objects':applied_objects,
            'failed_objects':failed_objects,'unscored_objects':unscored_objects,'objects':object_validation},
            ensure_ascii=False,indent=2)+'\n',encoding='utf8')
        if not unchanged:raise ValueError('Source hash changed during generation')
        if out_of_bounds or protected_overlap:raise ValueError('Final decoration mask violated allowed/protected regions')
        warnings=list(spec['warnings'])
        for name,result in data['object_results'].items():
            warnings += [f'{name}: {warning}' for warning in result.get('warnings',[])]
            if result.get('error'):warnings.append(f'{name}: {result["error"]}')
        if not applied_objects:warnings.append('변환 완료 · 적용 0건')
        warnings.append('미리보기는 정적 이미지입니다. 캐릭터와 반짝임은 적용 후 게임에서 확인하세요.')
        data.update(status='ready',stage=7,preview='/theme-runs/'+folder.name+'/preview.png',
            original='/theme-runs/'+folder.name+'/original-map.png',warnings=warnings,
            failed_objects=failed_objects,unscored_objects=unscored_objects)
    except Exception as error:
        data.update(status='failed',error=str(error))
        if isinstance(error,PaletteUnavailableError):data['failure_kind']='palette_unavailable'
        if isinstance(error,GateConfigurationError):data['failure_kind']='gate_configuration_error'
    finally:
        save_status(folder,data);busy.release()

@app.post('/runs')
def submit(req: Request):
    if (ROOT/'.batch.lock').exists():raise HTTPException(409,'마을 전체 생성 중입니다. 완료 후 새 생성을 시작해주세요.')
    try:
        spec=parse_prompt(req.prompt)
        for o in catalog():
            if o['id'] not in req.targets:continue
            p=get_profile(o)
            unsupported=set(spec['decorations'])-set(p['supported'])
            if unsupported:raise ValueError(o['id']+': 장식 설정 없음: '+', '.join(sorted(unsupported)))
    except ValueError as e: raise HTTPException(422,str(e))
    allowed={o['id'] for o in catalog()}
    if not req.targets or len(set(req.targets))!=len(req.targets) or not set(req.targets)<=allowed:
        raise HTTPException(422,'TMX 대상 오브젝트를 1개 이상 선택해주세요.')
    if not busy.acquire(blocking=False): raise HTTPException(409,'이미 생성 중입니다. 실행 기록을 확인해주세요.')
    folder=ROOT/uuid.uuid4().hex;folder.mkdir()
    save_status(folder,{'id':folder.name,'status':'queued','stage':1,'prompt':req.prompt})
    pool.submit(run,folder,req)
    return {'id':folder.name}

def get_folder(run_id):
    if not re.fullmatch('[a-f0-9]{32}',run_id) or not (ROOT/run_id/'status.json').exists(): raise HTTPException(404,'실행 기록이 없습니다.')
    return ROOT/run_id

@app.get('/runs/{run_id}')
def status(run_id:str): return json.loads((get_folder(run_id)/'status.json').read_text(encoding='utf8'))

@app.get('/runs/{run_id}/changes')
def change_list(run_id:str):
    try: return run_changes(get_folder(run_id))
    except ValidationError as error: raise HTTPException(409,'plan.json 계약 위반: '+str(error))

@app.post('/runs/{run_id}/apply')
def apply(run_id:str):
    folder=get_folder(run_id);s=json.loads((folder/'status.json').read_text(encoding='utf8'))
    if s['status']!='ready': raise HTTPException(409,'완료된 미리보기만 적용할 수 있습니다.')
    if s.get('applied_objects',1)<1: raise HTTPException(409,'적용 가능한 오브젝트가 없습니다.')
    manifest=json.loads((folder/'manifest.json').read_text(encoding='utf8'))
    if s.get('mapId','town')!='town':raise HTTPException(409,'이 맵은 /approve 후 브라우저에 저장합니다.')
    if sources(s.get('mapId','town'))[3]!=manifest['source_hashes']: raise HTTPException(409,'원본 맵이 변경됐습니다. 다시 생성해주세요.')
    if not STATE: raise HTTPException(409,'이 에디터는 /approve 후 브라우저에 저장합니다.')
    response=requests.get(STATE,timeout=20);response.raise_for_status();state=response.json()
    if state.get('backgrounds',{}).get('town'): raise HTTPException(409,'전체 스타일 배경이 활성화되어 있습니다. 먼저 원본 배경으로 복원해주세요.')
    snapshot=json.dumps(state,ensure_ascii=False,indent=2)
    if not (folder/'state-before-apply.json').exists():
        (folder/'state-before-apply.json').write_text(snapshot,encoding='utf8')
    (folder/f'state-before-apply-{time.time_ns()}.json').write_text(snapshot)
    items=[i for i in state.get('placements',{}).get('town',[]) if i.get('renderLayer')!='decoration']+manifest['placements']
    response=requests.post(STATE,json={'action':'placements','mapId':'town','placements':items},timeout=20);response.raise_for_status()
    check=requests.get(STATE,timeout=20);check.raise_for_status()
    if check.json()['placements']['town']!=items: raise HTTPException(409,'저장 확인 실패. 다른 편집 작업과 충돌했습니다.')
    s['applied_at']=time.time();save_status(folder,s)
    return {'placements':items,'id':run_id}

@app.post('/runs/{run_id}/approve')
def approve(run_id:str):
    """Validate the result for browser-local application; never mutate live server state."""
    folder=get_folder(run_id)
    status=json.loads((folder/'status.json').read_text(encoding='utf8'))
    if status['status']!='ready': raise HTTPException(409,'완료된 미리보기만 승인할 수 있습니다.')
    if status.get('applied_objects',1)<1: raise HTTPException(409,'적용 가능한 오브젝트가 없습니다.')
    if status.get('pipeline') == 'town-plan-v1':
        from contracts import check_folder
        checks = check_folder(folder)
        required = {'dsl.json', 'group-manifest.json', 'labels.json', 'plan.json', 'validation.json', 'crypt-manifest.json'}
        validation = json.loads((folder/'validation.json').read_text(encoding='utf8')) if (folder/'validation.json').exists() else {}
        if not required <= checks.keys() or any(checks.values()) or validation.get('alpha_preserved') is not True:
            raise HTTPException(409, '통합 계약/알파 검증을 통과하지 못했습니다.')
    manifest=json.loads((folder/'manifest.json').read_text(encoding='utf8'))
    if sources(status.get('mapId','town'))[3]!=manifest['source_hashes']: raise HTTPException(409,'원본 맵이 변경됐습니다. 다시 생성해주세요.')
    validation_path=folder/'validation.json'
    if validation_path.exists():
        validation=json.loads(validation_path.read_text(encoding='utf8'))
        if validation.get('source_hashes_unchanged') is not True or validation.get('alpha_preserved',True) is not True:
            raise HTTPException(409,'원본 해시 또는 최종 마스크 검증에 실패했습니다.')
    image_urls=[item.get('imageUrl','') for item in manifest['placements']]
    image_urls += [item['themeSettings']['tilesetImageUrl'] for item in manifest['placements'] if item.get('themeSettings',{}).get('tilesetImageUrl')]
    for url in image_urls:
        prefix='/theme-runs/'+run_id+'/'
        if not url.startswith(prefix): raise HTTPException(409,'결과 경로가 일치하지 않습니다.')
        path=(folder/url[len(prefix):]).resolve()
        if not path.is_relative_to(folder.resolve()) or not path.is_file():
            raise HTTPException(409,'결과 이미지가 없습니다.')
    record_event(folder, 'approval_validated', placement_count=len(manifest['placements']))
    return {'id':run_id,'mapId':status.get('mapId','town'),'targets':[o['id'] for o in manifest['objects']], 'placements':manifest['placements'],
            'replaceTheme': status.get('pipeline') in ('town-plan-v1','town-vision-v1')}


class AppliedReceipt(BaseModel):
    placement_ids: list[str] = Field(max_length=1000)


@app.post('/runs/{run_id}/applied')
def applied(run_id:str, receipt:AppliedReceipt):
    folder=get_folder(run_id)
    manifest=json.loads((folder/'manifest.json').read_text(encoding='utf8'))
    expected=[item['id'] for item in manifest['placements']]
    if sorted(receipt.placement_ids)!=sorted(expected):raise HTTPException(409,'적용 항목이 현재 결과와 다릅니다.')
    record_event(folder, 'browser_applied', placement_ids=receipt.placement_ids,
                 manifest_sha256=hashlib.sha256((folder/'manifest.json').read_bytes()).hexdigest())
    return {'saved':True}


@app.get('/artifacts/{run_id}/review.html', response_class=HTMLResponse)
def result_page(run_id:str):
    return review_html(get_folder(run_id))

@app.post('/runs/{run_id}/crypt-apply')
def crypt_apply(run_id:str):
    from crypt_apply import publish
    try: return publish(REPO,get_folder(run_id))
    except (ValueError,KeyError,FileNotFoundError) as error: raise HTTPException(409,str(error))


class CryptSelection(BaseModel):
    expected_active_id: str


@app.post('/runs/{run_id}/crypt-select')
def crypt_select(run_id:str, selection:CryptSelection):
    from crypt_apply import publish
    try: return publish(REPO,get_folder(run_id),expected_active_id=selection.expected_active_id)
    except (ValueError,KeyError,FileNotFoundError) as error: raise HTTPException(409,str(error))


app.mount('/artifacts', StaticFiles(directory=ROOT), name='artifacts')

# Recover only when the service starts, not when tests import the module.
def recover_runs():
    for p in ROOT.glob('*/status.json'):
        data=json.loads(p.read_text(encoding='utf8'))
        if data.get('status') in ['queued','running']:
            data.update(status='failed',error='서비스가 재시작되어 중단됐습니다. 다시 생성해주세요.')
            save_status(p.parent,data)
