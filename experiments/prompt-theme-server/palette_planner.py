"""Create and freeze request-scoped decoration palettes."""
import base64
import hashlib
import io
import json
import os
import re
from collections import Counter

import requests
import numpy as np
from PIL import Image, ImageDraw


VERSION = 'palette-plan-v1'
PROMPT_VERSION = 'decoration-palette-json-v1'
HEX_COLOR = re.compile(r'#[0-9a-fA-F]{6}(?![0-9a-fA-F])|#[0-9a-fA-F]{3}(?![0-9a-fA-F])')
RGB_COLOR = re.compile(r'rgb\s*\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})\s*\)', re.I)
DECORATION_TERMS = {
    'snow': ('snow', '눈'), 'lights': ('lights', 'light', 'bulb', '전구'),
    'garland': ('garland', 'wreath', '리스', '가랜드', '화환'),
    'pumpkins': ('pumpkin', '호박'), 'leaves': ('leaves', 'leaf', '낙엽', '잎'),
}


class PaletteUnavailableError(ValueError):
    pass


def normalize_hex(value):
    value = str(value).strip()
    if re.fullmatch(r'#[0-9a-fA-F]{3}', value):
        value = '#' + ''.join(ch * 2 for ch in value[1:])
    if not re.fullmatch(r'#[0-9a-fA-F]{6}', value):
        raise ValueError(f'Invalid palette color: {value}')
    return value.upper()


def _entry(value, source, role='base'):
    if isinstance(value, dict):
        role = str(value.get('role') or role)
        value = value.get('hex') or value.get('rgb')
    if isinstance(value, (list, tuple)) and len(value) == 3:
        channels = [int(channel) for channel in value]
        if any(channel < 0 or channel > 255 for channel in channels):
            raise ValueError('Palette RGB channels must be between 0 and 255')
        value = '#%02X%02X%02X' % tuple(channels)
    return {'hex': normalize_hex(value), 'role': role, 'source': source}


def _source_image(data, region=None):
    if not data:
        return None, None
    encoded = data.split(',', 1)[1] if data.startswith('data:image/') and ',' in data else data
    try:
        raw = base64.b64decode(encoded, validate=True)
        image = Image.open(io.BytesIO(raw)).convert('RGB')
    except Exception as error:
        raise PaletteUnavailableError('reference_image_invalid') from error
    if max(image.size) > 4096:
        raise PaletteUnavailableError('reference_image_too_large')
    if region:
        if len(region) != 4:
            raise PaletteUnavailableError('reference_region_invalid')
        x0, y0, x1, y1 = [int(value) for value in region]
        if x0 < 0 or y0 < 0 or x1 > image.width or y1 > image.height or x1 <= x0 or y1 <= y0:
            raise PaletteUnavailableError('reference_region_invalid')
        image = image.crop((x0, y0, x1, y1))
    return image, hashlib.sha256(raw).hexdigest()


def _dominant_colors(image, limit):
    if image is None:
        return []
    quantized = image.convert('RGB').quantize(colors=limit, method=Image.Quantize.MEDIANCUT).convert('RGB')
    colors = [color for _, color in sorted(quantized.getcolors(quantized.width * quantized.height), reverse=True)]
    unique = []
    for color in colors:
        value = '#%02X%02X%02X' % color
        if value not in unique:
            unique.append(value)
    return unique[:limit]


def _original_colors(images, limit):
    counts = Counter()
    for image in images:
        rgba = image.convert('RGBA')
        quantized = rgba.convert('RGB').quantize(colors=limit, method=Image.Quantize.MEDIANCUT).convert('RGB')
        pixels = np.asarray(quantized)
        opaque = np.asarray(rgba.getchannel('A')) > 0
        colors, frequencies = np.unique(pixels[opaque], axis=0, return_counts=True)
        for color, count in zip(colors, frequencies):
            counts[tuple(map(int, color))] += int(count)
    return ['#%02X%02X%02X' % color for color, _ in counts.most_common(limit)]


def _context_decoration(prompt, start, decorations):
    left = max(0, start - 64)
    right = min(len(prompt), start + 64)
    context = prompt[left:right].lower()
    anchor = start - left
    matches = []
    for name in decorations:
        for term in DECORATION_TERMS.get(name, (name,)):
            for match in re.finditer(re.escape(term.lower()), context):
                matches.append((abs(match.start() - anchor), name))
    return min(matches)[1] if matches else None


def _explicit_colors(prompt, decorations, supplied):
    palettes = {name: [] for name in decorations}
    shared = []
    for name, colors in (supplied or {}).items():
        target = palettes.get(name) if name != 'shared' else shared
        if target is None:
            continue
        values = colors if isinstance(colors, list) else [colors]
        target.extend(_entry(value, 'explicit', 'base' if index == 0 else 'highlight')
                      for index, value in enumerate(values))
    candidates = [(match.start(), match.group(0)) for match in HEX_COLOR.finditer(prompt)]
    for match in re.finditer(r'(?<![\w])#([0-9a-fA-F]{4,8})(?![\w])', prompt):
        if len(match.group(1)) not in (6,):
            raise PaletteUnavailableError('palette_invalid_explicit_color: use 3 or 6 hexadecimal digits')
    for match in RGB_COLOR.finditer(prompt):
        channels = [int(channel) for channel in match.groups()]
        if any(channel > 255 for channel in channels):
            raise PaletteUnavailableError('palette_invalid_explicit_color: RGB channels must be between 0 and 255')
        candidates.append((match.start(), '#%02X%02X%02X' % tuple(channels)))
    for position, color in sorted(candidates):
        name = _context_decoration(prompt, position, decorations)
        target = palettes[name] if name else shared
        if all(item['hex'] != normalize_hex(color) for item in target):
            target.append(_entry(color, 'explicit', 'base' if not target else 'highlight'))
    return palettes, shared


def _model_palette(prompt, theme, missing, explicit, reference_colors, reference_image, timeout):
    url, model = os.environ.get('THEME_VLM_URL'), os.environ.get('THEME_VLM_MODEL')
    if not url or not model:
        raise PaletteUnavailableError('palette_unavailable: set THEME_VLM_URL and THEME_VLM_MODEL')
    request_text = (
        'Propose pixel-art decoration colors for the missing requested decoration palettes. '
        'Return JSON only with keys decoration_palettes and shared_palette. Each palette is a list '
        'of objects with hex and role (base, shadow, highlight, outline, or wire). Use 2 to 4 '
        'colors per missing decoration and up to 3 shared outline/wire colors. Do not include '
        'colors for palettes already supplied. Colors are proposals, not measured facts.\n'
        f'Theme: {theme}\nRequest: {prompt}\nMissing decorations: {json.dumps(missing)}\n'
        f'Existing explicit/reference colors: {json.dumps(explicit, ensure_ascii=False)}\n'
        f'Reference colors: {json.dumps(reference_colors)}'
    )
    content = [{'type': 'text', 'text': request_text}]
    if reference_image is not None:
        buffer = io.BytesIO()
        reference_image.save(buffer, format='PNG')
        content.insert(0, {'type': 'image_url', 'image_url': {
            'url': 'data:image/png;base64,' + base64.b64encode(buffer.getvalue()).decode('ascii')}})
    try:
        response = requests.post(url.rstrip('/') + '/v1/chat/completions', json={
            'model': model, 'temperature': 0, 'max_tokens': 512,
            'messages': [{'role': 'user', 'content': content}],
            'chat_template_kwargs': {'enable_thinking': False}}, timeout=timeout)
        response.raise_for_status()
        text = response.json()['choices'][0]['message']['content']
    except Exception as error:
        raise PaletteUnavailableError(f'palette_unavailable: VLM request failed ({type(error).__name__})') from error
    if isinstance(text, list):
        text = ''.join(part.get('text', '') for part in text if isinstance(part, dict))
    match = re.search(r'\{.*\}', text, re.S)
    if not match:
        raise PaletteUnavailableError('palette_unavailable: model returned no JSON palette')
    try:
        return json.loads(match.group(0)), model
    except json.JSONDecodeError as error:
        raise PaletteUnavailableError('palette_unavailable: invalid JSON palette') from error


def build_palette_plan(prompt, theme, decorations, originals=(), explicit_palette=None,
                       reference_image=None, reference_region=None, timeout=120):
    decorations = list(dict.fromkeys(decorations))
    if not decorations:
        raise PaletteUnavailableError('palette_unavailable: no supported decoration was requested')
    limit = 8
    try:
        explicit, explicit_shared = _explicit_colors(prompt, decorations, explicit_palette)
    except PaletteUnavailableError:
        raise
    except (TypeError, ValueError) as error:
        raise PaletteUnavailableError(f'palette_invalid_explicit_color: {error}') from error
    reference, reference_hash = _source_image(reference_image, reference_region)
    reference_colors = _dominant_colors(reference, limit)
    original_colors = _original_colors(originals, limit)
    original_hashes = []
    for image in originals:
        rgba = image.convert('RGBA')
        digest = hashlib.sha256(rgba.width.to_bytes(4, 'big') + rgba.height.to_bytes(4, 'big') + rgba.tobytes())
        original_hashes.append(digest.hexdigest())
    source_plan = {}
    missing = []
    for name in decorations:
        if explicit[name]:
            # Explicit user colors are authoritative; the demo palette budget
            # applies to generated/reference suggestions, never user input.
            source_plan[name] = explicit[name]
        elif reference_colors:
            source_plan[name] = [_entry(color, 'reference', 'base' if index == 0 else 'shade')
                                 for index, color in enumerate(reference_colors)]
        else:
            missing.append(name)
    model_name = None
    warnings = []
    if missing:
        proposal, model_name = _model_palette(prompt, theme, missing, explicit, reference_colors, reference, timeout)
        proposed = proposal.get('decoration_palettes', {})
        for name in missing:
            values = proposed.get(name)
            if not isinstance(values, list) or not values:
                raise PaletteUnavailableError(f'palette_unavailable: no palette proposed for {name}')
            source_plan[name] = [_entry(value, 'ai_proposed') for value in values[:limit]]
        if not explicit_shared and proposal.get('shared_palette'):
            explicit_shared = [_entry(value, 'ai_proposed', 'outline') for value in proposal['shared_palette'][:3]]
    if not explicit_shared:
        dark = [color for color in original_colors if sum(int(color[index:index+2], 16) * weight
                for index, weight in zip((1, 3, 5), (0.2126, 0.7152, 0.0722))) < 110]
        explicit_shared = [_entry(color, 'original', 'outline') for color in dark[:3]]
    if any(explicit[name] and reference_colors for name in decorations):
        warnings.append('explicit_reference_conflict: explicit colors retained ahead of reference colors')
    if reference is not None and reference_region is None:
        warnings.append('reference_region_unset_uses_full_image')
    plan = {
        'schema_version': VERSION,
        'theme': theme,
        'requested_decorations': decorations,
        'original_palette': {'source': 'original', 'colors_hex': original_colors},
        'original_sha256': original_hashes,
        'decoration_palettes': source_plan,
        'shared_palette': explicit_shared,
        'source_priority': ['explicit', 'reference', 'ai_proposed'],
        'palette_config_version': 'palette-plan-v1',
        'initial_colors_per_decoration': limit,
        'freeze_per_request': True,
        'reference_sha256': reference_hash,
        'reference_region': list(reference_region) if reference is not None and reference_region is not None else None,
        'reference_scope': ('region' if reference_region is not None else 'full_image') if reference is not None else None,
        'model': model_name,
        'prompt_version': PROMPT_VERSION if model_name else None,
        'warnings': warnings,
        'status': 'ready'
    }
    plan_hash = hashlib.sha256(json.dumps(plan, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode('utf-8')).hexdigest()
    plan['plan_hash'] = plan_hash
    return plan


def save_palette_swatch(plan, path):
    """Write a compact color swatch with palette roles and source provenance."""
    rows = [('original', [{'hex': value, 'role': 'source', 'source': 'original'}
                          for value in plan.get('original_palette', {}).get('colors_hex', [])])]
    rows.extend((name, values) for name, values in plan.get('decoration_palettes', {}).items())
    rows.append(('shared', plan.get('shared_palette', [])))
    rows = [(name, values) for name, values in rows if values]
    cell_width, row_height = 112, 70
    max_colors = max((len(values) for _, values in rows), default=1)
    image = Image.new('RGB', (190 + cell_width * max_colors, max(48, 18 + row_height * len(rows))), 'white')
    draw = ImageDraw.Draw(image)
    draw.text((8, 6), f"{plan.get('theme', 'theme')} · {plan.get('plan_hash', '')[:12]}", fill='#202020')
    for row, (name, values) in enumerate(rows):
        y = 28 + row * row_height
        draw.text((8, y + 10), name[:24], fill='#202020')
        width = max(42, min(cell_width, (image.width - 190) // max(1, len(values))))
        for index, item in enumerate(values):
            x = 180 + index * width
            if x + width > image.width:
                break
            draw.rectangle((x, y, x + width - 4, y + 24), fill=item['hex'], outline='#555555')
            draw.text((x, y + 27), item['hex'], fill='#202020')
            draw.text((x, y + 43), f"{item.get('role', 'base')} · {item.get('source', '?')}"[:20], fill='#555555')
    image.save(path)
