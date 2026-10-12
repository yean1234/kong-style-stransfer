"""Shared shadow/demo/enforce gate for profile and plan generation paths."""
import json
import os
from pathlib import Path

from PIL import Image

from decoration_extractor import extract_changes, fix_overlay
from level_scorer import pq_scores, sc_scores


CONFIG_PATH = Path(__file__).with_name('gate_config.json')
HIGHER_IS_BETTER = {'SC', 'PQ', 'O', 'grid_sharpness'}


class GateConfigurationError(ValueError):
    pass


def gate_config():
    config = json.loads(CONFIG_PATH.read_text(encoding='utf-8'))
    mode = os.environ.get('THEME_GATE_MODE', config['mode'])
    if mode not in ('shadow', 'demo', 'enforce'):
        raise GateConfigurationError(f'Unsupported THEME_GATE_MODE: {mode}')
    config['mode'] = mode
    if mode == 'enforce' and not config.get('adopted_metrics'):
        raise GateConfigurationError('enforce mode needs calibrated adopted_metrics and thresholds')
    for metric in config.get('adopted_metrics', []):
        if config.get('thresholds', {}).get(metric) is None:
            raise GateConfigurationError(f'enforce threshold is missing: {metric}')
    return config


def _score(original, composite, decorations, theme, config):
    url = os.environ.get(config['vlm']['url_env'])
    model = os.environ.get(config['vlm']['model_env'])
    if not url or not model:
        return {'status': 'unscored', 'reason': 'vlm_not_configured', 'SC_d': {}, 'SC': None,
                'PQ_style': None, 'PQ_clean_original': None, 'PQ_clean_composite': None,
                'deltaPQ_clean': None, 'PQ': None, 'O': None, 'level_mass': {}}
    timeout = config['vlm']['timeout']
    sc_values = sc_scores(url, model, composite, decorations, timeout)
    pq_values = pq_scores(url, model, original, composite, theme, timeout)
    minimum_mass = config['min_level_mass']
    valid_sc = {name: value.get('score') if value.get('score') is not None and value.get('level_mass', 0) >= minimum_mass else None
                for name, value in sc_values.items()}
    sc = min(valid_sc.values()) if valid_sc and all(value is not None for value in valid_sc.values()) else None
    style = pq_values['style']
    style_value = style.get('score') if style.get('score') is not None and style.get('level_mass', 0) >= minimum_mass else None
    pq = (style_value - 1) / 4 if style_value is not None else None
    clean_original = pq_values['clean_original']
    clean_composite = pq_values['clean_composite']
    clean_original_value = clean_original.get('score') if clean_original.get('score') is not None and clean_original.get('level_mass', 0) >= minimum_mass else None
    clean_composite_value = clean_composite.get('score') if clean_composite.get('score') is not None and clean_composite.get('level_mass', 0) >= minimum_mass else None
    delta_clean = clean_composite_value - clean_original_value if clean_original_value is not None and clean_composite_value is not None else None
    complete = sc is not None and pq is not None
    score = {
        'status': 'scored' if complete else 'unscored',
        'reason': None if complete else 'required_visual_score_unavailable',
        'SC_d': sc_values,
        'SC': sc,
        'PQ_style': style_value,
        'PQ_clean_original': clean_original_value,
        'PQ_clean_composite': clean_composite_value,
        'deltaPQ_clean': delta_clean,
        'PQ': pq,
        'O': (sc * pq) ** 0.5 if complete else None,
        'level_mass': {
            'SC_d': {name: value.get('level_mass', 0) for name, value in sc_values.items()},
            'PQ_style': style.get('level_mass', 0),
            'PQ_clean_original': clean_original.get('level_mass', 0),
            'PQ_clean_composite': clean_composite.get('level_mass', 0)
        },
        'model': model
    }
    return score


def _passes_demo(scores, config):
    if scores.get('SC') is None or scores.get('PQ') is None:
        return None
    thresholds = config['demo']['retry_below']
    return scores['SC'] >= thresholds['SC'] and scores['PQ'] >= thresholds['PQ']


def _passes_enforce(metrics, scores, config):
    values = {**metrics, **scores}
    for name in config['adopted_metrics']:
        value, threshold = values.get(name), config['thresholds'][name]
        if value is None:
            return False
        if name in HIGHER_IS_BETTER:
            if value < threshold:
                return False
        elif value > threshold:
            return False
    return True


def _enforce_unscored(metrics, scores, config):
    """A missing required measurement is a hold condition, not a retry signal."""
    values = {**metrics, **scores}
    return any(values.get(name) is None for name in config['adopted_metrics'])


def _retry_feedback(candidate, config):
    scores = candidate['scores']
    if scores.get('status') != 'scored':
        return None
    thresholds = config['demo']['retry_below']
    missing = [name for name, result in scores['SC_d'].items()
               if result.get('score') is not None and result['score'] < thresholds['SC']]
    if missing:
        return 'Regenerate only the requested decoration(s) estimated as missing: ' + ', '.join(missing) + '.'
    if scores.get('PQ') is not None and scores['PQ'] < thresholds['PQ']:
        return 'Keep the same frozen decoration palette. Improve pixel edges, outline, cell size and shading; preserve the source silhouette.'
    return None


def _evaluate_candidate(candidate_dir, original, generated, high_resolution, allowed_masks,
                        protected, palette_plan, decorations, theme, config):
    extraction = extract_changes(
        original, generated, allowed_masks, protected, palette_plan, decorations,
        config['extraction']['change_delta_e_initial'],
        config['extraction']['palette_distance_warn_delta_e_initial'],
        candidate_dir, high_resolution)
    overlay, fix = fix_overlay(extraction, original, candidate_dir)
    composite = Image.alpha_composite(original.convert('RGBA'), overlay)
    metrics = {**extraction['extraction'], **fix}
    structural_valid = (not metrics['empty'] and metrics['final_out_of_bounds_px'] == 0
                        and metrics['protected_overlap_px'] == 0 and overlay.size == original.size)
    scores = _score(original, composite, decorations, theme, config) if structural_valid else {
        'status': 'unscored', 'reason': 'structural_candidate_invalid', 'SC_d': {}, 'SC': None,
        'PQ_style': None, 'PQ_clean_original': None, 'PQ_clean_composite': None,
        'deltaPQ_clean': None, 'PQ': None, 'O': None, 'level_mass': {}
    }
    warnings = []
    if metrics.get('palette_mismatch_px'):
        warnings.append('palette_mismatch')
    if metrics.get('clip_ratio'):
        warnings.append('decoration_clipped')
    if not structural_valid:
        warnings.append('structural_invalid')
    if scores['status'] == 'unscored':
        warnings.append('visual_evaluation_unavailable')
    report = {
        'structural_valid': structural_valid,
        'scores': scores,
        'metrics': metrics,
        'warnings': warnings,
        'palette_hash': palette_plan.get('plan_hash'),
        'config_version': config.get('config_version'),
        'extraction_method': extraction['extraction']['method'],
        'palette_snap_applied': True,
        'composite_path': 'composite.png'
    }
    (candidate_dir / 'gate.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return report, overlay


def run_candidates(folder, original, decorations, theme, palette_plan, allowed_masks, protected,
                   generate, on_candidate=None):
    """Generate, correct, score, retry and choose one object-level result."""
    config = gate_config()
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    maximum_retries = config['retry']['max_retries'] if config['mode'] in ('demo', 'enforce') else 0
    candidates = []
    feedback = None
    for attempt in range(maximum_retries + 1):
        attempt_dir = folder / f'attempt-{attempt:02d}'
        attempt_dir.mkdir(parents=True, exist_ok=True)
        if candidates and palette_plan.get('plan_hash') != candidates[0]['palette_hash']:
            raise ValueError('Palette changed between generation candidates')
        generation = {}
        try:
            generated_result = generate(attempt_dir, attempt, feedback, palette_plan)
            if isinstance(generated_result, tuple):
                if len(generated_result) == 3:
                    generated, high_resolution, generation = generated_result
                elif len(generated_result) == 2:
                    generated, high_resolution = generated_result
                    generation = {}
                else:
                    generated, high_resolution, generation = generated_result[0], None, {}
            else:
                generated, high_resolution = generated_result, None
            report, overlay = _evaluate_candidate(attempt_dir, original, generated, high_resolution,
                allowed_masks, protected, palette_plan, decorations, theme, config)
        except Exception as error:
            report = {'structural_valid': False, 'scores': {'status': 'unscored', 'SC': None, 'PQ': None, 'O': None},
                      'metrics': {'empty': True}, 'warnings': ['candidate_error'],
                      'error': f'{type(error).__name__}: {error}', 'palette_hash': palette_plan.get('plan_hash')}
            overlay = None
            (attempt_dir / 'gate.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        candidate = {'candidate': attempt, **report, 'generation': generation}
        candidates.append(candidate)
        if on_candidate:
            on_candidate(candidate, len(candidates))
        if config['mode'] == 'shadow' or not report['structural_valid']:
            break
        if config['mode'] == 'demo':
            passed = _passes_demo(report['scores'], config)
            if passed is not False or attempt >= maximum_retries:
                break
            feedback = _retry_feedback(candidate, config)
            continue
        if config['mode'] == 'enforce':
            if _enforce_unscored(report['metrics'], report['scores'], config):
                break
            passed = _passes_enforce(report['metrics'], report['scores'], config)
            if passed or attempt >= maximum_retries:
                break
            feedback = 'Regenerate this object while preserving the frozen palette and source silhouette.'

    valid = [candidate for candidate in candidates if candidate['structural_valid']]
    mode = config['mode']
    chosen = None
    if valid:
        if mode == 'demo':
            scored = [candidate for candidate in valid if candidate['scores'].get('O') is not None]
            if scored:
                chosen = max(scored, key=lambda candidate: (candidate['scores']['O'], -candidate['candidate']))
            else:
                chosen = min(valid, key=lambda candidate: candidate['candidate'])
        elif mode == 'enforce':
            passing = [candidate for candidate in valid if _passes_enforce(candidate['metrics'], candidate['scores'], config)]
            if passing:
                if 'O' in config['adopted_metrics']:
                    chosen = max(passing, key=lambda candidate: (candidate['scores'].get('O', float('-inf')), -candidate['candidate']))
                else:
                    chosen = min(passing, key=lambda candidate: candidate['candidate'])
            else:
                unscored = [candidate for candidate in valid if _enforce_unscored(candidate['metrics'], candidate['scores'], config)]
                if unscored:
                    chosen = min(unscored, key=lambda candidate: candidate['candidate'])
        else:
            chosen = min(valid, key=lambda candidate: candidate['candidate'])
    if chosen is None:
        decision = 'failed' if mode != 'enforce' else 'rejected'
        overlay = None
        warnings = ['no_structurally_valid_candidate']
    else:
        picked_pass = _passes_demo(chosen['scores'], config) if mode == 'demo' else None
        warnings = list(chosen['warnings'])
        if mode == 'shadow':
            decision = 'recorded'
        elif mode == 'enforce':
            if _enforce_unscored(chosen['metrics'], chosen['scores'], config):
                decision = 'unscored'
                warnings.append('required_gate_metric_unavailable')
            else:
                decision = 'passed' if _passes_enforce(chosen['metrics'], chosen['scores'], config) else 'rejected'
        elif chosen['scores'].get('status') == 'unscored':
            decision = 'unscored'
        else:
            decision = 'applied' if picked_pass else 'applied_with_warning'
        if mode == 'demo' and decision in ('applied_with_warning', 'unscored'):
            warnings.append('demo_result_not_quality_calibrated')
        overlay = Image.open(folder / f"attempt-{chosen['candidate']:02d}" / 'overlay-fixed.png').convert('RGBA')
        overlay.save(folder / 'decoration.png')
        Image.alpha_composite(original.convert('RGBA'), overlay).save(folder / 'composite.png')
    result = {
        'status': decision,
        'decision': decision,
        'gate_mode': mode,
        'structural_valid': bool(chosen),
        'scores': chosen['scores'] if chosen else {'status': 'unscored', 'SC': None, 'PQ': None, 'O': None},
        'metrics': chosen['metrics'] if chosen else {},
        'warnings': list(dict.fromkeys(warnings + (chosen['warnings'] if chosen else []))),
        'selected_candidate': chosen['candidate'] if chosen else None,
        'retries': max(0, len(candidates) - 1),
        'candidate_count': len(candidates),
        'palette_hash': palette_plan.get('plan_hash'),
        'config_version': config.get('config_version'),
        'candidates': candidates
    }
    (folder / 'gate.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return result, overlay
