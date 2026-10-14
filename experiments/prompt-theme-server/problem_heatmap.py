"""RichHF-style problem heatmaps for gate candidates.

The output follows the RichHF shape: a per-pixel problem map plus a list of
missing requested words (decorations). The maps are computed from the gate's
own source, generated and mask data, not from a trained model. They are
diagnostic only: they never change a gate decision, and their thresholds are
uncalibrated defaults until human labels exist.

Usage: python problem_heatmap.py RUN_DIR   (re-analyze a saved run)
"""
import json
import sys
from collections import Counter
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageEnhance
from scipy import ndimage
from skimage.color import deltaE_ciede2000, rgb2lab

from decoration_extractor import _explained_by_source, _rgb


SCHEMA = 'problem-heatmap-v1'
# Drawing order: later classes are painted on top of earlier ones.
CLASSES = {
    'crop_seam': ((52, 199, 89), '조각 경계에 걸친 장식'),
    'small_support': ((50, 173, 230), '너무 작거나 얇은 곳에 붙은 장식'),
    'fragment': ((255, 214, 10), '떨어진 부스러기 픽셀'),
    'cut_edge': ((191, 90, 242), '허용 영역에서 잘린 장식 끝'),
    'background_fringe': ((255, 149, 0), '배경 회색이 섞인 테두리'),
    'cutout_leak': ((255, 59, 48), '원본 픽셀이 장식으로 잘못 잡힘(누끼 번짐)'),
}
FILES = ('problem-heatmap.png', 'problem-classes.png', 'problem-map.png', 'problems.json')


def _regions(mask, limit=5):
    labels, count = ndimage.label(mask, structure=np.ones((3, 3)))
    if not count:
        return []
    sizes = ndimage.sum(mask, labels, range(1, count + 1))
    boxes = ndimage.find_objects(labels)
    order = np.argsort(-sizes)[:limit]
    return [[int(boxes[i][1].start), int(boxes[i][0].start), int(boxes[i][1].stop), int(boxes[i][0].stop)] for i in order]


def _decoration_pixels(generated_rgb, mask, palette_plan, decorations, max_delta_e):
    """Count overlay pixels per decoration by nearest decoration-only palette color."""
    own = {name: [value['hex'].upper() for value in palette_plan.get('decoration_palettes', {}).get(name, [])]
           for name in decorations}
    shared = Counter(color for colors in own.values() for color in set(colors))
    unique = {name: [color for color in colors if shared[color] == 1] for name, colors in own.items()}
    # A decoration whose colors are all shared with another one cannot be counted.
    counts = {name: (0 if unique[name] else None) for name in decorations}
    names = [name for name in decorations if unique[name]]
    if not names or not mask.any():
        return counts
    pixels = rgb2lab(generated_rgb[mask][None].astype(np.float32) / 255)[0]
    distances = []
    for name in names:
        rgb = np.array([[int(color[i:i + 2], 16) for i in (1, 3, 5)] for color in unique[name]], dtype=np.float32)
        lab = rgb2lab(rgb[None] / 255)[0]
        distances.append(deltaE_ciede2000(pixels[:, None, :], lab[None, :, :]).min(axis=1))
    distances = np.stack(distances)
    nearest = distances.argmin(axis=0)
    close = distances.min(axis=0) <= max_delta_e
    for index, name in enumerate(names):
        counts[name] = int(np.count_nonzero(close & (nearest == index)))
    return counts


def _render(composite, classes, summary):
    scale = max(1, min(8, 384 // max(composite.size)))
    background = Image.new('RGBA', composite.size, (128, 128, 128, 255))
    background.alpha_composite(composite)
    dim = ImageEnhance.Brightness(background.convert('RGB')).enhance(0.55).convert('RGBA')
    size = (composite.width * scale, composite.height * scale)
    view = Image.alpha_composite(dim, classes).resize(size, Image.Resampling.NEAREST)
    lines = [(CLASSES[name][0], f"{name} {item['px']}px") for name, item in summary['classes'].items() if item['px']]
    missing = ', '.join(f"{item['decoration']} ({item['source']})" for item in summary['missing_keywords'])
    if missing:
        lines.append(((255, 255, 255), 'missing: ' + missing))
    if not lines:
        lines.append(((180, 180, 180), 'no rule-based problem found'))
    image = Image.new('RGB', (max(size[0], 260), size[1] + 8 + 14 * len(lines)), (23, 27, 37))
    image.paste(view.convert('RGB'), (0, 0))
    draw = ImageDraw.Draw(image)
    for row, (color, text) in enumerate(lines):
        y = size[1] + 4 + row * 14
        draw.rectangle((4, y + 2, 12, y + 10), fill=color)
        draw.text((18, y), text, fill=(232, 237, 247))
    return image


def analyze(original, generated, overlay, clipped, palette_plan, decorations, scores, config, output_dir):
    """Write problem maps for one candidate and return a JSON-safe summary."""
    settings = config['problem_heatmap']
    original = original.convert('RGBA')
    if generated.size != original.size:
        generated = generated.resize(original.size, Image.Resampling.BOX)
    output_dir = Path(output_dir)
    opaque = np.asarray(original.getchannel('A')) > 0
    kept = np.asarray(overlay.convert('RGBA').getchannel('A')) > 0
    clipped = np.zeros_like(kept) if clipped is None else np.asarray(clipped, dtype=bool)
    source_rgb, generated_rgb = _rgb(original), _rgb(generated)

    explained = _explained_by_source(source_rgb, generated_rgb, settings['source_tolerance_rgb'])
    labels, count = ndimage.label(kept, structure=np.ones((3, 3)))
    sizes = ndimage.sum(kept, labels, range(1, count + 1)) if count else np.zeros(0)
    small = np.isin(labels, 1 + np.flatnonzero(sizes < settings['fragment_px']))
    # Support thickness is measured around the nearest source pixel, so snow resting just
    # above a thick roof is not mistaken for a thin support.
    local_thickness = ndimage.maximum_filter(ndimage.distance_transform_edt(opaque), size=5)
    if opaque.any():
        _, (near_y, near_x) = ndimage.distance_transform_edt(~opaque, return_indices=True)
        thickness = local_thickness[near_y, near_x]
    else:
        thickness = np.zeros(opaque.shape)
    small_object = int(opaque.sum()) < settings['small_object_px']
    frame = np.zeros_like(opaque)
    frame[0, :] = frame[-1, :] = frame[:, 0] = frame[:, -1] = True
    seam_zone = ndimage.binary_dilation(opaque & frame, iterations=settings['crop_seam_px'])
    masks = {
        'crop_seam': kept & seam_zone,
        'small_support': kept & ((thickness <= settings['thin_support_radius_px']) | small_object),
        'fragment': kept & small,
        'cut_edge': kept & ndimage.binary_dilation(clipped, structure=np.ones((3, 3))),
        'background_fringe': kept & ~opaque & explained,
        'cutout_leak': kept & opaque & explained,
    }

    classes = np.zeros((*kept.shape, 4), dtype=np.uint8)
    for name, mask in masks.items():
        classes[mask] = (*CLASSES[name][0], 235)
    flagged = np.logical_or.reduce(list(masks.values()))
    heat = ndimage.gaussian_filter(flagged.astype(np.float32), 1.0)
    heat = heat / heat.max() if heat.max() > 0 else heat

    material = kept & ~masks['cutout_leak'] & ~masks['background_fringe']
    decoration_px = _decoration_pixels(generated_rgb, material, palette_plan or {}, decorations,
                                       settings['decoration_class_delta_e'])
    missing = [{'decoration': name, 'source': 'palette_class', 'value': value}
               for name, value in decoration_px.items() if value is not None and value < settings['min_decoration_px']]
    sc_threshold = config.get('demo', {}).get('retry_below', {}).get('SC')
    if scores.get('status') == 'scored' and sc_threshold is not None:
        for name, item in scores.get('SC_d', {}).items():
            if item.get('score') is not None and item['score'] < sc_threshold:
                missing.append({'decoration': name, 'source': 'vlm_sc', 'value': round(float(item['score']), 3)})

    overlay_px = int(kept.sum())
    summary = {
        'schema_version': SCHEMA,
        'calibrated': bool(settings.get('calibrated', False)),
        'image_size': list(original.size),
        'overlay_px': overlay_px,
        'flagged_px': int(flagged.sum()),
        'flagged_ratio': float(flagged.sum() / overlay_px) if overlay_px else None,
        'source_opaque_px': int(opaque.sum()),
        'small_object': small_object,
        'classes': {name: {'px': int(mask.sum()), 'label': CLASSES[name][1], 'regions': _regions(mask)}
                    for name, mask in masks.items()},
        'decoration_class_px': decoration_px,
        'missing_keywords': missing,
        'thresholds': {key: value for key, value in settings.items() if key != 'enabled'},
        'files': dict(zip(('heatmap', 'classes', 'map', 'json'), FILES)),
    }
    classes_image = Image.fromarray(classes, 'RGBA')
    classes_image.save(output_dir / 'problem-classes.png')
    Image.fromarray((heat * 255).astype(np.uint8)).save(output_dir / 'problem-map.png')
    composite = Image.alpha_composite(original, overlay.convert('RGBA'))
    _render(composite, classes_image, summary).save(output_dir / 'problem-heatmap.png')
    (output_dir / 'problems.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return summary


def compose_map(folder, instances, base_name='preview.png'):
    """Paint each placed object's problem classes onto a dimmed map preview."""
    folder = Path(folder)
    base = Image.open(folder / base_name).convert('RGB')
    view = ImageEnhance.Brightness(base).enhance(0.55).convert('RGBA')
    layer = Image.new('RGBA', view.size)
    totals = Counter()
    objects = {}
    for instance in instances:
        name = instance['asset']
        classes_path = folder / name / 'problem-classes.png'
        if not classes_path.exists():
            continue
        layer.alpha_composite(Image.open(classes_path).convert('RGBA'), (instance['x'], instance['y']))
        if name not in objects:
            objects[name] = json.loads((folder / name / 'problems.json').read_text(encoding='utf-8'))
        for class_name, item in objects[name]['classes'].items():
            totals[class_name] += item['px']
    if not objects:
        return None
    # Widen marks by one pixel so they stay visible when the full map is scaled down.
    pixels = np.asarray(layer)
    grown = np.zeros_like(pixels)
    for color, _ in CLASSES.values():
        mask = np.all(pixels[..., :3] == color, axis=2) & (pixels[..., 3] > 0)
        grown[ndimage.binary_dilation(mask, structure=np.ones((3, 3)))] = (*color, 235)
    Image.alpha_composite(view, Image.fromarray(grown, 'RGBA')).save(folder / 'problem-heatmap-map.png')
    report = {'schema_version': SCHEMA, 'base': base_name, 'instances': len(instances),
              'class_px_per_placed_instance': dict(totals),
              'objects': {name: {'flagged_ratio': item['flagged_ratio'], 'small_object': item['small_object'],
                                 'missing_keywords': item['missing_keywords'],
                                 'classes': {key: value['px'] for key, value in item['classes'].items()}}
                          for name, item in objects.items()}}
    (folder / 'problems-map.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return report


def reanalyze_run(folder, config):
    """Recompute problem maps for each object's selected attempt in a saved run."""
    folder = Path(folder)
    plan_path = folder / 'palette-plan.json'
    palette_plan = json.loads(plan_path.read_text(encoding='utf-8')) if plan_path.exists() else {}
    analyzed = []
    for gate_path in sorted(folder.glob('*/gate.json')):
        target = gate_path.parent
        gate = json.loads(gate_path.read_text(encoding='utf-8'))
        if gate.get('selected_candidate') is None:
            continue
        attempt = target / f"attempt-{gate['selected_candidate']:02d}"
        original_path = folder / f'{target.name}-original.png'
        needed = [original_path, attempt / 'aligned-generated.png', attempt / 'overlay-fixed.png']
        if not all(path.exists() for path in needed):
            continue
        clipped_path = attempt / 'clipped-mask.png'
        clipped = np.asarray(Image.open(clipped_path).convert('L')) > 0 if clipped_path.exists() else None
        decorations = palette_plan.get('requested_decorations') or list(gate.get('scores', {}).get('SC_d', {}))
        summary = analyze(Image.open(original_path), Image.open(attempt / 'aligned-generated.png'),
                          Image.open(attempt / 'overlay-fixed.png'), clipped, palette_plan, decorations,
                          gate.get('scores', {}), config, attempt)
        for name in FILES:
            (target / name).write_bytes((attempt / name).read_bytes())
        analyzed.append((target.name, summary))
    sources_path = folder / 'sources.json'
    placed = []
    if sources_path.exists() and (folder / 'preview.png').exists():
        names = {name for name, _ in analyzed}
        placed = [i for i in json.loads(sources_path.read_text(encoding='utf-8'))['instances'] if i['asset'] in names]
    return analyzed, compose_map(folder, placed) if placed else None


if __name__ == '__main__':
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    from style_gate import gate_config
    run_folder = Path(sys.argv[1])
    if not run_folder.is_dir():
        from theme_pipeline import ROOT
        run_folder = ROOT / sys.argv[1]
    results, map_report = reanalyze_run(run_folder, gate_config())
    for asset, item in results:
        found = {name: value['px'] for name, value in item['classes'].items() if value['px']}
        print(asset, item['flagged_ratio'], found, [m['decoration'] for m in item['missing_keywords']])
    print('map:', run_folder / 'problem-heatmap-map.png' if map_report else 'not composed (no sources.json/preview.png)')
