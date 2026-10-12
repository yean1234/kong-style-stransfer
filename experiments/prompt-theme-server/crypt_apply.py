"""Publish a validated visual-only Crypt overlay without modifying source assets."""
import hashlib, json, shutil, time
from PIL import Image
from run_records import record_event

def publish(repo, folder, expected_active_id=None):
    status=json.loads((folder/'status.json').read_text(encoding='utf8'))
    if status['status']!='ready': raise ValueError('검수 완료된 Crypt 결과만 적용할 수 있습니다.')
    if status.get('applied_objects',1)<1: raise ValueError('적용 가능한 오브젝트가 없습니다.')
    manifest=json.loads((folder/'crypt-manifest.json').read_text(encoding='utf8'))
    # Older Windows runs recorded native separators; runtime keys use URL paths.
    hashes=manifest['source_hashes']
    manifest['source_hashes']={name.replace('\\','/'):digest for name,digest in hashes.items()}
    if len(manifest['source_hashes'])!=len(hashes): raise ValueError('Duplicate source paths')
    map_id=manifest['mapId']
    if map_id not in {'floor-0-town','floor-1-ruins'} or manifest['id']!=folder.name:
        raise ValueError('Crypt 맵 계약 불일치')
    active=repo/'public/crypt-style'/('active.json' if map_id=='floor-0-town' else f'active-{map_id}.json')
    if expected_active_id is not None:
        active_id=json.loads(active.read_text(encoding='utf8'))['id'] if active.exists() else ''
        if active_id != expected_active_id:
            raise ValueError('다른 결과가 적용됐습니다. 새로고침 후 다시 선택해주세요.')
    if status.get('pipeline') == 'crypt-ruins-plan-v1':
        from contracts import check_folder
        report = check_folder(folder)
        if len(report) < 6 or any(report.values()): raise ValueError('통합 계약 검증 실패')
        validation = json.loads((folder/'validation.json').read_text(encoding='utf8'))
        if validation.get('all_variant_alpha_preserved') is not True: raise ValueError('알파 검증 실패')
        if validation.get('source_hashes_unchanged') is not True: raise ValueError('원본 해시 검증 실패')
        current = json.loads((repo/f'public/crypt-style/active-{map_id}.json').read_text(encoding='utf8'))
        if expected_active_id is None and current['id'] not in (manifest['parent_run_id'], manifest['id']):
            raise ValueError('생성 이후 적용 맵이 바뀌었습니다. 최신 결과에서 다시 시작해주세요.')
    expected={f'public/crypt-maps/{map_id}.tmx','src/games/crypt-crawler/assets/tilesets/ninja-dungeon-16.tsx','src/games/crypt-crawler/assets/tilesets/ninja-dungeon-16.png'}
    if set(manifest['source_hashes'])!=expected: raise ValueError('원본 검증 목록 불일치')
    for name,digest in manifest['source_hashes'].items():
        if hashlib.sha256((repo/name).read_bytes()).hexdigest()!=digest: raise ValueError('원본 변경: '+name)
    with Image.open(folder/'decoration-map.png') as image:
        if image.mode!='RGBA' or image.size!=(manifest['width'],manifest['height']): raise ValueError('레이어 크기/투명도 불일치')
    if manifest['overlay']!=f'/theme-runs/{folder.name}/decoration-map.png': raise ValueError('레이어 경로 불일치')
    for item in manifest.get('hires',[]):
        import re
        if not re.fullmatch('[a-zA-Z0-9-]+',item['asset']): raise ValueError('Invalid asset name')
        if item['url']!=f"/theme-runs/{folder.name}/{item['asset']}/decoration.png": raise ValueError('Invalid asset URL')
        scale=item['textureScale']
        if not isinstance(scale,int) or not 1<=scale<=6: raise ValueError('Invalid texture scale')
        with Image.open(folder/item['asset']/'decoration.png') as image:
            if image.mode!='RGBA' or image.size!=(item['width']*scale,item['height']*scale): raise ValueError('Invalid high-resolution texture')
    target=repo/'public/crypt-style';target.mkdir(exist_ok=True)
    active=target/('active.json' if map_id=='floor-0-town' else f'active-{map_id}.json')
    if active.exists():
        history=target/'selection-history';history.mkdir(exist_ok=True)
        shutil.copy2(active,history/f'{time.time_ns()}-{folder.name}.json')
        if not (folder/'previous-active.json').exists(): shutil.copy2(active,folder/'previous-active.json')
    pending=active.with_suffix('.tmp');payload=json.dumps(manifest);pending.write_text(payload,encoding='utf8')
    for attempt in range(20):
        try:
            pending.replace(active)
            break
        except PermissionError:
            if attempt<19:
                time.sleep(.1)
                continue
            # Windows dev-server watchers may deny replacing an open file.
            # Previous selection is backed up; readers retain their last valid
            # layer if a concurrent read sees incomplete JSON, then retry.
            active.write_text(payload,encoding='utf8')
            record_event(folder,'selection_write_fallback',reason='Windows replace lock')
    record_event(folder,'crypt_project_applied',map_id=manifest['mapId'],instances=manifest['instances'],manifest_sha256=hashlib.sha256((folder/'crypt-manifest.json').read_bytes()).hexdigest())
    return {'saved':True,'editorUrl':f'/editor.html?game=crypt&map={map_id}'}
