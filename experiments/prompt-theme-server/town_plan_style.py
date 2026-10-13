"""Town vertical slice: dsl.json + group-manifest.json + plan.json -> one edit per decorate group ->
decoration extracted from the diff -> placed at every identical instance -> town placements (existing runtime).
Usage: python town_plan_style.py prepare <dsl.json> <plan.json>   -> prints run id
       python town_plan_style.py run <run-id>"""
import json, sys, time, uuid
from pathlib import Path
import numpy as np
from PIL import Image, ImageFilter
from theme_pipeline import ROOT, REPO, FLUX, save_status, sources, layers, recolor
from object_decorations import request_image
from crypt_full_style import allowed_material_masks
from run_records import record_event
from contracts import Dsl, Plan, check_folder
import groups_tsx
from palette_planner import build_palette_plan, save_palette_swatch
from style_gate import gate_config, run_candidates
from problem_heatmap import compose_map

MAP = 'src/games/my-sample-rpg/assets/maps/town.tmx'
PIPELINE = 'town-plan-v1'
NIGHT = .58
# extract_material keeps only the top part for these kinds; roofs, fountains etc. use the full height.
LIMIT_KIND = {'tree': 'tree'}
DECORATION_WORDS = {'snow': 'thin soft white snow caps on the upper edges', 'lights': 'small warm golden Christmas bulbs on a dark cable',
                    'garland': 'a thin green garland'}


def read(folder, name):
    return json.loads((folder/name).read_text(encoding='utf8'))


def write(folder, name, data):
    (folder/name).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf8')


def default_prompt(row):
    added = ', '.join(DECORATION_WORDS[d] for d in row['decorations'] if d in DECORATION_WORDS) or 'winter decorations'
    return (f'Add {added} to this pixel-art {row["kind"]}. Preserve its exact silhouette, tiles, colours and position. '
            'Keep the gray background empty. No new objects, no text, no cast shadows.')


def prepare(dsl_path, plan_path):
    return prepare_data(read(Path(dsl_path).parent, Path(dsl_path).name), read(Path(plan_path).parent, Path(plan_path).name))


def prepare_data(dsl, plan, reference_image=None, explicit_palette=None, reference_region=None):
    dsl = Dsl.model_validate(dsl).model_dump(exclude_none=True)
    plan = [row.model_dump(exclude_none=True) for row in Plan.model_validate(plan).root]
    if dsl['target_maps'] != ['town']: raise ValueError('This integration slice supports town only')
    if not plan or len({row['asset'] for row in plan}) != len(plan): raise ValueError('Plan needs unique assets')
    if any(row['candidates'] != 1 or row.get('seed') is not None for row in plan):
        raise ValueError('MVP supports one candidate and service-selected seeds only')
    manifest = groups_tsx.build(REPO, 'town', MAP)
    groups = {group['group_id']: group for group in manifest['groups']}
    unknown = [row['asset'] for row in plan if row['asset'] not in groups]
    if unknown: raise ValueError(f'plan.json asset not in group manifest: {unknown}')
    for row in plan:
        row['decorations'] = row.get('decorations') or dsl.get('decorations', [])
    decorated = [row for row in plan if row['action'] == 'decorate']
    if any(not row['decorations'] for row in decorated):
        raise ValueError('Every decorate plan row needs at least one supported decoration')
    decorations = list(dict.fromkeys(name for row in decorated for name in row['decorations']))
    gate = gate_config()
    palette_plan = None
    if decorations:
        reference_image = reference_image or dsl.get('reference_image')
        reference_region = reference_region or dsl.get('reference_region')
        palette_plan = build_palette_plan(dsl.get('source_text') or dsl['theme'], dsl['theme'], decorations,
            [groups_tsx.crop(REPO, MAP, groups[row['asset']], groups[row['asset']]['instances'][0]) for row in decorated],
            explicit_palette or dsl.get('palette'), reference_image, reference_region)
    folder = ROOT/uuid.uuid4().hex
    folder.mkdir()
    color = dsl.get('color', {'gain': [1, 1, 1], 'bias': [0, 0, 0]})
    original, _, _, hashes = layers(color)
    original.save(folder/'original-map.png')
    variants, instances, skipped = [], [], []
    for row in plan:
        group = groups[row['asset']]
        first = groups_tsx.crop(REPO, MAP, group, group['instances'][0])
        first.save(folder/f'{row["asset"]}-original.png')
        variants.append({'id': row['asset'], 'kind': row['kind'], 'width': first.width, 'height': first.height})
        for instance in group['instances']:
            # The edit is made on the first instance, so it is placed only where the source pixels are identical.
            same = instance is group['instances'][0] or groups_tsx.crop(REPO, MAP, group, instance).tobytes() == first.tobytes()
            (instances if same else skipped).append({'asset': row['asset'], 'x': instance['x'], 'y': instance['y']})
        row['instances'] = sum(i['asset'] == row['asset'] for i in instances)
    write(folder, 'dsl.json', dsl)
    write(folder, 'group-manifest.json', manifest)
    write(folder, 'labels.json', {})  # TSX labels are in the group manifest; no CC cache entries.
    write(folder, 'plan.json', plan)
    write(folder, 'sources.json', {'mapId': 'town', 'hashes': hashes, 'variants': variants, 'instances': instances, 'skipped_instances': skipped})
    if palette_plan:
        write(folder, 'palette-plan.json', palette_plan)
        save_palette_swatch(palette_plan,folder/'palette-swatch.png')
    (folder/'review.html').write_text('<!doctype html><html lang="ko"><meta charset="utf-8"><body><script type="module" src="/src/editor/cryptResultGallery.ts"></script></body></html>', encoding='utf8')
    spec = {'theme': dsl['theme'], 'decorations': dsl['decorations'], 'night': dsl['night'], 'twinkle': dsl.get('twinkle', False),
            'color': color, 'warnings': dsl.get('warnings', [])}
    mode = gate['mode']
    save_status(folder, {'id': folder.name, 'mapId': 'town', 'pipeline': PIPELINE, 'status': 'queued', 'stage': 2, 'created': time.time(),
                         'prompt': dsl.get('source_text', ''), 'spec': spec, 'total_objects': len(plan), 'completed_objects': 0,
                         'instances': len(instances), 'gate_mode': mode,'gate_config_version':gate.get('config_version'),
                         'palette_hash': palette_plan.get('plan_hash') if palette_plan else None,
                         'palette_plan': f'/theme-runs/{folder.name}/palette-plan.json' if palette_plan else None,
                         'palette_swatch': f'/theme-runs/{folder.name}/palette-swatch.png' if palette_plan else None,
                         'object_results': {row['asset']: {'status': 'pending'} for row in plan},
                         'original': f'/theme-runs/{folder.name}/original-map.png'})
    record_event(folder, 'prepared', plan_rows=len(plan), instances=len(instances), skipped_instances=len(skipped))
    return folder.name


def edit(folder, row, source, pipeline=PIPELINE, palette_plan=None, theme='winter', on_candidate=None):
    target = folder/row['asset']
    target.mkdir(exist_ok=True)
    decorations = row.get('decorations', [])
    if not palette_plan:
        raise ValueError('Palette plan is required for decoration generation')
    scale = max(1, min(6, 1024 // max(source.size)))
    scaled = source.resize((source.width*scale, source.height*scale), Image.Resampling.NEAREST)
    canvas = Image.new('RGB', (scaled.width+64, scaled.height+64), '#808080')
    canvas.paste(scaled, (32, 32), scaled)
    allowed, protected = allowed_material_masks(source, LIMIT_KIND.get(row['kind'], row['kind']), decorations)
    palette_text = '; '.join(name+': '+', '.join(f"{value['role']} {value['hex']}" for value in palette_plan['decoration_palettes'][name]) for name in decorations)
    shared_text = ', '.join(f"{value['role']} {value['hex']}" for value in palette_plan.get('shared_palette', []))
    def generate(attempt_folder, attempt, feedback, frozen_palette):
        prompt = (row.get('prompt') or default_prompt(row)) + f' Frozen requested palette: {palette_text}. Shared outline/wire palette: {shared_text}.'
        if feedback: prompt += ' ' + feedback
        raw = request_image(attempt_folder, canvas, prompt, FLUX, 1.0, pipeline=pipeline)
        if raw.size != canvas.size:
            method = Image.Resampling.BOX if raw.width >= canvas.width and raw.height >= canvas.height else Image.Resampling.LANCZOS
            raw = raw.resize(canvas.size, method)
        high = raw.crop((32, 32, 32+scaled.width, 32+scaled.height))
        aligned = high.resize(source.size, Image.Resampling.BOX) if high.size != source.size else high
        high.save(attempt_folder/'aligned-high.png')
        timing = attempt_folder/'request-timing.json'
        generation = json.loads(timing.read_text(encoding='utf8')) if timing.exists() else {}
        return aligned, high, generation
    result, overlay = run_candidates(target, source, decorations, theme, palette_plan, allowed, protected, generate, on_candidate)
    result['action'] = row['action']
    if overlay is not None:
        Image.alpha_composite(source, overlay).save(target/'composite.png')
    (target/'extraction.json').write_text(json.dumps({'method':'change_mask_with_palette_guidance',
        'palette_hash':palette_plan['plan_hash'],'gate':result},ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    (target/'gate.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    return result, overlay


def run(run_id):
    folder = ROOT/run_id
    status, plan, spec = read(folder, 'status.json'), read(folder, 'plan.json'), read(folder, 'sources.json')
    palette_plan = read(folder, 'palette-plan.json') if (folder/'palette-plan.json').exists() else None
    lock = ROOT/'.batch.lock'
    with lock.open('x') as handle: handle.write(run_id)
    try:
        for row in plan:
            name = row['asset']
            source = Image.open(folder/f'{name}-original.png').convert('RGBA')
            status.update(status='running', stage=4, current_object=name)
            status['object_results'][name] = {'status': 'running', 'started_at': time.time()}
            save_status(folder, status)
            result = {'status': 'ready', 'action': row['action'], 'source_available': True}
            try:
                if row['action'] == 'decorate':
                    def on_candidate(candidate, count):
                        status['object_results'][name] = {'status':'evaluating','gate_mode':status['gate_mode'],
                            'retries':count-1,'last_scores':candidate.get('scores',{})}
                        save_status(folder,status)
                    result, overlay = edit(folder, row, source, palette_plan=palette_plan,
                        theme=status['spec']['theme'], on_candidate=on_candidate)
                    timing = result.get('candidates', [])
                    selected = next((candidate for candidate in timing if candidate['candidate'] == result.get('selected_candidate')), {})
                    result['generation_seconds'] = selected.get('generation', {}).get('generation_seconds')
                    if overlay is None: result['status'] = 'failed'
                elif row['action'] == 'recolor':
                    (folder/name).mkdir(exist_ok=True)
                    recolor(source, status['spec']['color']).save(folder/name/'composite.png')
                    result.update(status='applied',structural_valid=True)
                else:
                    result['status'] = 'skipped'  # add / cover / skip are recorded only in this slice
            except Exception as error:
                result = {'status':'failed','decision':'failed','structural_valid':False,
                          'action':row['action'],'error':f'{type(error).__name__}: {error}','warnings':['object_processing_failed']}
            status['object_results'][name] = result
            status['completed_objects'] = sum(r['status'] not in ('pending','running','evaluating') for r in status['object_results'].values())
            save_status(folder, status)
        finalize(folder, status, plan, spec)
    except Exception as error:
        status.update(status='failed', error=f'{type(error).__name__}: {error}')
        save_status(folder, status)
        raise
    finally:
        lock.unlink(missing_ok=True)


def finalize(folder, status, plan, spec):
    status.update(stage=6, current_object=None)
    save_status(folder, status)
    original = Image.open(folder/'original-map.png').convert('RGBA')
    decoration_map, recolor_map = Image.new('RGBA', original.size), Image.new('RGBA', original.size)
    out_of_bounds, warnings, placed = 0, [], []
    actions = {row['asset']: row['action'] for row in plan}
    rows_by_name = {row['asset']: row for row in plan}
    mode = status.get('gate_mode', 'shadow')
    apply_statuses = {'applied', 'applied_with_warning', 'unscored', 'recorded', 'passed', 'ready'}
    def can_apply(name):
        result = status['object_results'].get(name, {})
        if result.get('status') not in apply_statuses or result.get('structural_valid') is False:
            return False
        if mode == 'shadow' and actions.get(name) == 'decorate':
            return False
        return not (mode == 'enforce' and actions.get(name) == 'decorate' and result.get('status') != 'passed')
    for instance in spec['instances']:
        name, x, y = instance['asset'], instance['x'], instance['y']
        if not can_apply(name):
            continue
        if actions[name] == 'decorate':
            overlay_path=folder/name/'decoration.png'
            if not overlay_path.exists():continue
            overlay = Image.open(overlay_path).convert('RGBA')
            source=Image.open(folder/f'{name}-original.png').convert('RGBA')
            allowed,protected=allowed_material_masks(source,LIMIT_KIND.get(rows_by_name[name]['kind'],rows_by_name[name]['kind']),rows_by_name[name]['decorations'])
            permitted=np.logical_or.reduce(list(allowed.values()))
            out_of_bounds += int(((np.array(overlay)[:,:,3]>0)&(~permitted|protected)).sum())
            decoration_map.alpha_composite(overlay, (x, y))
            placed.append(instance)
        elif actions[name] == 'recolor':
            recolor_map.alpha_composite(Image.open(folder/name/'composite.png').convert('RGBA'), (x, y))
    decoration_map.save(folder/'decoration-map.png')
    recolor_map.save(folder/'recolor-map.png')
    preview = Image.alpha_composite(original, recolor_map)
    if status['spec']['night']: preview = Image.alpha_composite(preview, Image.new('RGBA', preview.size, (7, 19, 46, 148)))
    Image.alpha_composite(preview, decoration_map).save(folder/'preview.png')
    run_id = folder.name
    problem_map = compose_map(folder, placed)
    settings = {'runId': run_id, 'night': NIGHT if status['spec']['night'] else 0, 'twinkle': status['spec']['twinkle'], 'color': {'gain': [1, 1, 1], 'bias': [0, 0, 0]}}
    Image.new('RGBA', (1, 1)).save(folder/'settings.png')
    placement = lambda pid, url: {'id': f'{run_id}-{pid}', 'kind': 'object', 'col': 0, 'row': 0, 'imageUrl': f'/theme-runs/{run_id}/{url}',
                                  'anchor': 'top-left', 'renderLayer': 'decoration', 'sourceGroup': 'prompt-theme', 'visible': True, 'sourceAssetId': pid}
    placements = [dict(placement('settings', 'settings.png'), themeSettings=settings)]
    if recolor_map.getbbox():
        runtime_recolor = recolor_map.copy()
        if status['spec']['night']:
            shade = Image.new('RGBA', runtime_recolor.size, (7, 19, 46, 148))
            runtime_recolor = Image.alpha_composite(runtime_recolor, shade)
            runtime_recolor.putalpha(recolor_map.getchannel('A'))
        runtime_recolor.save(folder/'recolor-runtime.png')
        placements.append(placement('recolor-map', 'recolor-runtime.png'))
    placements.append(placement('decoration-map', 'decoration-map.png'))
    boxes = {}
    for instance in spec['instances']: boxes.setdefault(instance['asset'], instance)
    objects = [{'id': v['id'], 'category': v['kind'], 'box': [boxes[v['id']]['x'], boxes[v['id']]['y'], v['width'], v['height']]}
               for v in spec['variants'] if v['id'] in boxes and can_apply(v['id'])]
    unchanged = sources()[3] == spec['hashes']
    applied_names = [name for name, result in status['object_results'].items()
                     if can_apply(name) and actions.get(name) in ('decorate','recolor')]
    failed_objects = sum(result.get('status') in ('failed','rejected') for result in status['object_results'].values())
    unscored_objects = sum(result.get('status') == 'unscored' for result in status['object_results'].values())
    object_validation = {name:{'decision':result.get('decision',result.get('status')),
        'structural_valid':result.get('structural_valid'), 'scores':result.get('scores',{}),
        'warnings':result.get('warnings',[]), 'selected_candidate':result.get('selected_candidate'),
        'retries':result.get('retries',0)} for name,result in status['object_results'].items()}
    write(folder, 'manifest.json', {'pipeline': PIPELINE, 'spec': status['spec'], 'source_hashes': spec['hashes'], 'objects': objects,
                                    'placements': placements, 'geometry_preserved': True, 'alpha_preserved': out_of_bounds == 0,
                                    'gate_mode':mode,'gate_config_version':status.get('gate_config_version'),
                                    'object_results':object_validation, 'applied_objects':len(applied_names)})
    write(folder, 'validation.json', {'source_hashes_unchanged': unchanged, 'alpha_preserved': out_of_bounds == 0,
                                      'tiles_covered': len(spec['instances']), 'skipped_instances': len(spec['skipped_instances']),
                                      'changed_pixels': int((np.array(decoration_map)[:, :, 3] > 0).sum() + (np.array(recolor_map)[:, :, 3] > 0).sum()),
                                      'out_of_bounds_pixels': out_of_bounds, 'protected_overlap_px':out_of_bounds,
                                      'gate_mode':mode,'gate_config_version':status.get('gate_config_version'),
                                      'applied_objects':len(applied_names),
                                      'failed_objects':failed_objects, 'unscored_objects':unscored_objects,
                                      'objects':object_validation})
    write(folder, 'crypt-manifest.json', {'id': run_id, 'mapId': 'town', 'width': original.width, 'height': original.height,
                                        'night': settings['night'], 'bulbs': [], 'source_hashes': spec['hashes'],
                                        'layers': {'decoration': f'/theme-runs/{run_id}/decoration-map.png',
                                                   'recolor': f'/theme-runs/{run_id}/recolor-map.png'}})
    if spec['skipped_instances']: warnings.append(f'원본 픽셀이 다른 인스턴스 {len(spec["skipped_instances"])}곳은 이번 결과에서 제외했습니다.')
    for name,result in status['object_results'].items():
        warnings += [f'{name}: {warning}' for warning in result.get('warnings',[])]
        if result.get('error'):warnings.append(f'{name}: {result["error"]}')
    if not applied_names:warnings.append('변환 완료 · 적용 0건')
    problems = {name: error for name, error in check_folder(folder).items() if error}
    if problems or not unchanged or out_of_bounds: raise ValueError(f'contract/source/alpha check failed: {problems}, unchanged={unchanged}, overflow={out_of_bounds}')
    status.update(status='ready', stage=7, preview=f'/theme-runs/{run_id}/preview.png', preview_revision=time.time(), warnings=warnings,
        applied_objects=len(applied_names),failed_objects=failed_objects,unscored_objects=unscored_objects,
        problem_heatmap_map=f'/theme-runs/{run_id}/problem-heatmap-map.png' if problem_map else None)
    save_status(folder, status)
    record_event(folder, 'finalized', instances=len(spec['instances']), out_of_bounds_pixels=out_of_bounds)


if __name__ == '__main__':
    if sys.argv[1:2] == ['prepare'] and len(sys.argv) == 4:
        print(prepare(sys.argv[2], sys.argv[3]), flush=True)
    elif sys.argv[1:2] == ['run'] and len(sys.argv) == 3:
        run(sys.argv[2])
    else:
        print(__doc__)
        sys.exit(2)
