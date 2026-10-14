"""Locate decoration candidates by source change and permitted geometry."""
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter
from scipy import ndimage
from skimage.color import deltaE_ciede2000, rgb2lab

from palette_planner import normalize_hex


GRAY = (128, 128, 128)


def _rgb(image):
    if image.mode == 'RGBA':
        background = Image.new('RGBA', image.size, (*GRAY, 255))
        background.alpha_composite(image)
        return np.asarray(background.convert('RGB'), dtype=np.uint8)
    return np.asarray(image.convert('RGB'), dtype=np.uint8)


def _lab(rgb):
    return rgb2lab(np.asarray(rgb, dtype=np.float32) / 255.0)


def _mask(value, size, default=False):
    if value is None:
        return np.full((size[1], size[0]), default, dtype=bool)
    if isinstance(value, Image.Image):
        value = np.asarray(value.convert('L')) > 0
    result = np.asarray(value, dtype=bool)
    if result.shape != (size[1], size[0]):
        raise ValueError('Decoration mask size does not match the source image')
    return result


def _plan_colors(palette_plan, decorations):
    output = {}
    for name in decorations:
        values = palette_plan.get('decoration_palettes', {}).get(name, [])
        output[name] = [normalize_hex(value['hex']) for value in values]
    shared = [normalize_hex(value['hex']) for value in palette_plan.get('shared_palette', [])]
    if not shared and not any(output.values()):
        raise ValueError('Palette plan has no colors for decoration correction')
    return output, shared


def _rgb_from_hex(value):
    value = normalize_hex(value)
    return np.asarray([int(value[i:i + 2], 16) for i in (1, 3, 5)], dtype=np.uint8)


def _closest_palette_distance(rgb, colors):
    if not colors:
        return np.full(rgb.shape[:2], np.inf, dtype=np.float32)
    labs = _lab(rgb)
    palette_lab = _lab(np.stack([_rgb_from_hex(value) for value in colors]))
    distances = deltaE_ciede2000(labs[..., None, :], palette_lab[None, None, :, :])
    return distances.min(axis=2).astype(np.float32)


def _save_mask(path, mask):
    Image.fromarray(np.where(mask, 255, 0).astype(np.uint8)).save(path)


def _explained_by_source(source_rgb, generated_rgb, tolerance):
    """True where the generated color stays inside the local 3x3 source color range.

    Such a pixel is a resampled or drifted copy of nearby source colors, not new
    decoration material.
    """
    low = ndimage.minimum_filter(source_rgb, size=(3, 3, 1)).astype(int) - tolerance
    high = ndimage.maximum_filter(source_rgb, size=(3, 3, 1)).astype(int) + tolerance
    generated = generated_rgb.astype(int)
    return np.all((generated >= low) & (generated <= high), axis=2)


def _white_balance_gains(source_rgb, generated_rgb, opaque, max_change):
    """Von Kries diagonal gains: per-channel median of generated/source over opaque source pixels.

    A gain beyond max_change more likely means decorations cover most of the sprite
    than a color cast, so no correction is returned then.
    """
    valid = opaque & (source_rgb.min(axis=2) > 10)
    if not valid.any():
        return None
    gains = np.median(generated_rgb[valid].astype(np.float32) / source_rgb[valid], axis=0)
    return gains if np.all(np.abs(gains - 1) <= max_change) else None


def _grid_sharpness(high, source_size):
    width, height = source_size
    if high.width % width or high.height % height:
        return None
    sx, sy = high.width // width, high.height // height
    if sx == 1 and sy == 1:
        return None
    pixels = np.asarray(high.convert('RGB'), dtype=np.uint8)
    cells = pixels.reshape(height, sy, width, sx, 3)
    representatives = np.median(cells, axis=(1, 3)).astype(np.uint8)
    representative_lab = _lab(representatives)[:, None, :, None, :]
    pixel_lab = _lab(pixels).reshape(height, sy, width, sx, 3)
    similarity = deltaE_ciede2000(pixel_lab, representative_lab) < 5
    return float(similarity.mean())


def extract_changes(original, generated, allowed_masks, protected=None, palette_plan=None,
                    decorations=(), change_threshold=5.0, palette_threshold=5.0,
                    output_dir=None, high_resolution=None, strong_change_threshold=20.0,
                    source_tolerance=20, white_balance_max_change=0.25):
    """Return a pre-snap overlay and measurements for one generated candidate.

    Before thresholding, a global color cast is removed (white balance). A weak change
    counts only when 8-connected to a strong one (Canny-style hysteresis), and a change
    whose color stays inside the local source color range is dropped. Pass None to
    disable each step.
    """
    decorations = list(decorations)
    size = original.size
    if generated.size != size:
        resample = Image.Resampling.BOX if generated.width >= size[0] and generated.height >= size[1] else Image.Resampling.LANCZOS
        generated = generated.resize(size, resample)
    high = high_resolution or generated
    source_rgb, generated_rgb = _rgb(original), _rgb(generated)
    source_alpha = np.asarray(original.convert('RGBA').getchannel('A')) > 0
    gains = None if white_balance_max_change is None else _white_balance_gains(
        source_rgb, generated_rgb, source_alpha, white_balance_max_change)
    # Only change detection uses the balanced colors; the overlay keeps the generated ones.
    balanced_rgb = generated_rgb if gains is None else np.clip(generated_rgb / gains, 0, 255).astype(np.uint8)
    difference = deltaE_ciede2000(_lab(source_rgb), _lab(balanced_rgb)).astype(np.float32)
    weak = difference > float(change_threshold)
    changed = weak
    if strong_change_threshold is not None:
        labels, _ = ndimage.label(weak, structure=np.ones((3, 3), dtype=bool))
        changed = weak & np.isin(labels, np.unique(labels[difference > float(strong_change_threshold)]))
    isolated = weak & ~changed
    explained = np.zeros_like(changed)
    if source_tolerance is not None:
        explained = changed & _explained_by_source(source_rgb, balanced_rgb, source_tolerance)
        changed = changed & ~explained
    masks = {name: _mask((allowed_masks or {}).get(name), size, default=False) for name in decorations}
    allowed = np.logical_or.reduce(list(masks.values())) if masks else np.zeros(changed.shape, dtype=bool)
    protected_mask = _mask(protected, size)
    candidate = changed & allowed & ~protected_mask
    nearby_source = np.asarray(original.convert('RGBA').getchannel('A').filter(ImageFilter.MaxFilter(5))) > 0
    clipped = changed & nearby_source & (~allowed | protected_mask)
    ambiguous = any(np.any(masks[left] & masks[right]) for i, left in enumerate(decorations) for right in decorations[i + 1:])
    rgb_candidates = generated_rgb[candidate]
    palette_colors, shared_colors = _plan_colors(palette_plan or {}, decorations)
    all_colors = list(dict.fromkeys(shared_colors + [color for name in decorations for color in palette_colors[name]]))
    distance = _closest_palette_distance(generated_rgb, all_colors) if all_colors else np.full(changed.shape, np.nan)
    measured = distance[candidate]
    if not ambiguous and candidate.any():
        for name, mask in masks.items():
            eligible_colors = list(dict.fromkeys(shared_colors + palette_colors[name]))
            if eligible_colors:
                name_distance = _closest_palette_distance(generated_rgb, eligible_colors)
                selected = candidate & mask
                distance[selected] = name_distance[selected]
        measured = distance[candidate]
    compliance = float(np.mean(measured <= palette_threshold)) if measured.size else None
    decoration_px = {name: (None if ambiguous else int((candidate & masks[name]).sum())) for name in decorations}
    clip_denominator = int(clipped.sum() + candidate.sum())
    clip_ratio = float(clipped.sum() / clip_denominator) if clip_denominator else None
    extraction = {
        'method': 'change_mask_with_palette_guidance',
        'change_delta_e': float(change_threshold),
        'change_delta_e_strong': None if strong_change_threshold is None else float(strong_change_threshold),
        'source_tolerance_rgb': source_tolerance,
        'white_balance_gains': None if gains is None else [round(float(value), 4) for value in gains],
        'dropped_isolated_px': int((isolated & allowed & ~protected_mask).sum()),
        'dropped_source_explained_px': int((explained & allowed & ~protected_mask).sum()),
        'palette_distance_warn_delta_e': float(palette_threshold),
        'changed_px': int(changed.sum()),
        'candidate_px': int(candidate.sum()),
        'clipped_px': int(clipped.sum()),
        'clip_ratio': clip_ratio,
        'protected_overlap_px_before_fix': int((changed & protected_mask).sum()),
        'coverage': float(candidate.sum() / max(1, source_alpha.sum())),
        'decoration_px': decoration_px,
        'palette_distance_before': float(np.mean(measured)) if measured.size else None,
        'palette_compliance_before': compliance,
        'palette_mismatch_px': int(np.count_nonzero(measured > palette_threshold)) if measured.size else None,
        'colors_before': int(len(np.unique(rgb_candidates, axis=0))) if rgb_candidates.size else 0,
        'grid_sharpness': _grid_sharpness(high, size),
        'empty': not bool(candidate.any())
    }
    alpha = np.where(candidate, 255, 0).astype(np.uint8)
    overlay = Image.fromarray(np.dstack((generated_rgb, alpha)), mode='RGBA')
    if output_dir:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        generated.save(output_dir / 'aligned-generated.png')
        overlay.save(output_dir / 'overlay-extracted.png')
        _save_mask(output_dir / 'candidate-mask.png', candidate)
        Image.fromarray(np.clip(difference * 8, 0, 255).astype(np.uint8)).save(output_dir / 'change-map.png')
        if np.isfinite(distance).any():
            Image.fromarray(np.clip(distance * 8, 0, 255).astype(np.uint8)).save(output_dir / 'palette-distance-map.png')
        _save_mask(output_dir / 'clipped-mask.png', clipped)
    return {'overlay': overlay, 'aligned': generated, 'high_resolution': high,
            'changed': changed, 'candidate': candidate, 'allowed': allowed,
            'allowed_masks': masks, 'protected': protected_mask, 'clipped': clipped, 'extraction': extraction,
            'palette_colors': palette_colors, 'shared_colors': shared_colors,
            'generated_rgb': generated_rgb, 'palette_distance': distance}


def fix_overlay(candidate, original, output_dir=None):
    """Snap only candidate pixels to the frozen palette and enforce final masks."""
    allowed, protected = candidate['allowed'], candidate['protected']
    before_mask = candidate['candidate']
    final_mask = before_mask & allowed & ~protected
    clipped = candidate['clipped']
    rgb = candidate['generated_rgb'].copy()
    before_rgb = rgb[final_mask].copy()
    palette_shift = None
    plan_colors, shared = candidate['palette_colors'], candidate['shared_colors']
    if final_mask.any():
        indices_y, indices_x = np.where(final_mask)
        all_entries = list(dict.fromkeys(shared + [color for values in plan_colors.values() for color in values]))
        if not all_entries:
            raise ValueError('Palette plan has no colors to snap decoration pixels')
        rgb_palettes = {name: list(dict.fromkeys(shared + values)) for name, values in plan_colors.items()}
        palette_rgb = {color: _rgb_from_hex(color) for values in rgb_palettes.values() for color in values}
        for y, x in zip(indices_y, indices_x):
            names = [name for name, mask in candidate['allowed_masks'].items() if mask[y, x]]
            eligible = list(dict.fromkeys(color for name in names for color in rgb_palettes.get(name, []))) or all_entries
            labs = _lab(np.stack([palette_rgb[color] for color in eligible]))
            pixel_lab = _lab(rgb[y, x][None, None, :])[0, 0]
            distances = deltaE_ciede2000(np.broadcast_to(pixel_lab, labs.shape), labs)
            rgb[y, x] = palette_rgb[eligible[int(np.argmin(distances))]]
        if before_rgb.size:
            palette_shift = float(deltaE_ciede2000(_lab(before_rgb), _lab(rgb[final_mask])).mean())
    alpha = np.where(final_mask, 255, 0).astype(np.uint8)
    overlay = Image.fromarray(np.dstack((rgb, alpha)), mode='RGBA')
    fixed = {
        'palette_shift': palette_shift,
        'snapped_px': int(final_mask.sum()),
        'final_out_of_bounds_px': int((final_mask & ~allowed).sum()),
        'protected_overlap_px': int((final_mask & protected).sum()),
        'final_protected_overlap_px': int((final_mask & protected).sum()),
        'empty': not bool(final_mask.any())
    }
    if output_dir:
        output_dir = Path(output_dir)
        overlay.save(output_dir / 'overlay-fixed.png')
        _save_mask(output_dir / 'clipped-mask.png', clipped)
        Image.alpha_composite(original.convert('RGBA'), overlay).save(output_dir / 'composite.png')
        (output_dir / 'fix.json').write_text(__import__('json').dumps(fixed, indent=2) + '\n', encoding='utf-8')
    return overlay, fixed
