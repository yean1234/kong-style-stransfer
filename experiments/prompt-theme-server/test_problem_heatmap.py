import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import Image

from decoration_extractor import extract_changes, fix_overlay
from problem_heatmap import CLASSES, analyze, reanalyze_run
from style_gate import gate_config, run_candidates


ORANGE, WHITE, BULB = (200, 120, 60), (220, 220, 225), (255, 211, 74)


def plan(colors):
    return {'requested_decorations': list(colors), 'plan_hash': 'b' * 64,
            'decoration_palettes': {name: [{'hex': value, 'role': 'base'}] for name, value in colors.items()},
            'shared_palette': [{'hex': '#202020', 'role': 'outline'}]}


def two_tone_with_bulb():
    """Opaque source with an orange/white edge; the edit blurs that edge and adds one bulb."""
    source = np.zeros((16, 16, 4), np.uint8)
    source[:, :8] = (*ORANGE, 255)
    source[:, 8:] = (*WHITE, 255)
    generated = source[:, :, :3].copy()
    generated[:, 7] = (np.array(ORANGE) + np.array(WHITE)) // 2
    generated[2:4, 3:5] = BULB
    return Image.fromarray(source), Image.fromarray(generated)


def candidate(original, generated, palette_plan, decorations):
    allowed = {name: np.ones(original.size[::-1], bool) for name in decorations}
    extraction = extract_changes(original, generated, allowed, np.zeros(original.size[::-1], bool),
                                 palette_plan, decorations)
    overlay, _ = fix_overlay(extraction, original)
    return overlay, extraction['clipped']


def class_mask(folder, name):
    pixels = np.asarray(Image.open(Path(folder) / 'problem-classes.png'))
    return np.all(pixels[..., :3] == CLASSES[name][0], axis=2) & (pixels[..., 3] > 0)


class ProblemHeatmapTests(unittest.TestCase):
    def test_blurred_source_edge_is_a_cutout_leak_but_a_new_bulb_is_not(self):
        original, generated = two_tone_with_bulb()
        palette_plan = plan({'lights': '#FFD34A'})
        overlay, clipped = candidate(original, generated, palette_plan, ['lights'])
        with tempfile.TemporaryDirectory() as temp:
            summary = analyze(original, generated, overlay, clipped, palette_plan, ['lights'], {}, gate_config(), temp)
            leak = class_mask(temp, 'cutout_leak')
            for name in ('problem-heatmap.png', 'problem-map.png', 'problems.json'):
                self.assertTrue((Path(temp) / name).exists())
        self.assertTrue(leak[:, 7].all())
        self.assertFalse(leak[2:4, 3:5].any())
        self.assertEqual(summary['classes']['cutout_leak']['px'], 16)
        self.assertEqual(summary['decoration_class_px']['lights'], 4)
        self.assertFalse(summary['calibrated'])

    def test_missing_keywords_come_from_palette_classes_and_low_scene_scores(self):
        original, generated = two_tone_with_bulb()
        palette_plan = plan({'snow': '#F4F8FF', 'lights': '#FFD34A'})
        overlay, clipped = candidate(original, generated, palette_plan, ['snow', 'lights'])
        scores = {'status': 'scored', 'SC_d': {'snow': {'score': .9}, 'lights': {'score': .2}}}
        with tempfile.TemporaryDirectory() as temp:
            summary = analyze(original, generated, overlay, clipped, palette_plan, ['snow', 'lights'],
                              scores, gate_config(), temp)
        missing = {(item['decoration'], item['source']) for item in summary['missing_keywords']}
        self.assertEqual(missing, {('snow', 'palette_class'), ('lights', 'vlm_sc')})

    def test_thin_support_and_crop_seam_are_marked_where_they_happen(self):
        source = np.zeros((20, 20, 4), np.uint8)
        source[4:, 9:11] = (*ORANGE, 255)
        original = Image.fromarray(source)
        generated = np.full((20, 20, 3), 128, np.uint8)
        generated[4:, 9:11] = ORANGE
        generated[4:6, 9:11] = BULB
        generated[18:, 9:11] = BULB
        generated = Image.fromarray(generated)
        palette_plan = plan({'lights': '#FFD34A'})
        overlay, clipped = candidate(original, generated, palette_plan, ['lights'])
        config = copy.deepcopy(gate_config())
        config['problem_heatmap']['small_object_px'] = 0
        with tempfile.TemporaryDirectory() as temp:
            summary = analyze(original, generated, overlay, clipped, palette_plan, ['lights'], {}, config, temp)
        self.assertFalse(summary['small_object'])
        self.assertEqual(summary['classes']['small_support']['px'], 8)
        # Only the bulb touching the cut bottom edge is a seam risk.
        self.assertEqual(summary['classes']['crop_seam']['regions'], [[9, 18, 11, 20]])

    def test_gate_saves_problem_files_without_changing_the_decision(self):
        original, generated = two_tone_with_bulb()
        disabled = copy.deepcopy(gate_config())
        disabled['problem_heatmap']['enabled'] = False
        results = {}
        with tempfile.TemporaryDirectory() as temp:
            for label, config in (('on', gate_config()), ('off', disabled)):
                folder = Path(temp) / label
                with patch('style_gate.gate_config', return_value=config), \
                        patch('style_gate._score', return_value={'status': 'unscored', 'SC': None, 'PQ': None, 'O': None}):
                    results[label], _ = run_candidates(folder, original, ['lights'], 'winter', plan({'lights': '#FFD34A'}),
                        {'lights': np.ones((16, 16), bool)}, np.zeros((16, 16), bool), lambda *args: generated)
                results[label + '_files'] = sorted(path.name for path in folder.glob('problem*'))
        self.assertEqual(results['on']['status'], results['off']['status'])
        self.assertEqual(results['on']['selected_candidate'], results['off']['selected_candidate'])
        self.assertEqual(results['on']['problems']['classes']['cutout_leak']['px'], 16)
        self.assertIsNone(results['off']['problems'])
        self.assertEqual(results['on_files'], ['problem-classes.png', 'problem-heatmap.png', 'problem-map.png', 'problems.json'])
        self.assertEqual(results['off_files'], [])

    def test_saved_run_is_reanalyzed_and_composed_on_the_map(self):
        original, generated = two_tone_with_bulb()
        palette_plan = plan({'lights': '#FFD34A'})
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {'THEME_GATE_MODE': 'shadow'}), \
                patch('style_gate._score', return_value={'status': 'unscored', 'SC': None, 'PQ': None, 'O': None}):
            run = Path(temp)
            run_candidates(run / 'stall', original, ['lights'], 'winter', palette_plan,
                {'lights': np.ones((16, 16), bool)}, np.zeros((16, 16), bool), lambda *args: generated)
            for name in ('problem-heatmap.png', 'problem-classes.png', 'problem-map.png', 'problems.json'):
                (run / 'stall' / name).unlink()
            original.save(run / 'stall-original.png')
            Image.new('RGBA', (40, 16), (60, 60, 60, 255)).save(run / 'preview.png')
            (run / 'palette-plan.json').write_text(json.dumps(palette_plan), encoding='utf-8')
            instances = [{'asset': 'stall', 'x': 0, 'y': 0}, {'asset': 'stall', 'x': 20, 'y': 0}]
            (run / 'sources.json').write_text(json.dumps({'instances': instances}), encoding='utf-8')
            analyzed, report = reanalyze_run(run, gate_config())
            heat = np.asarray(Image.open(run / 'problem-heatmap-map.png').convert('RGB'))
            self.assertTrue((run / 'stall' / 'problems.json').exists())
        self.assertEqual([name for name, _ in analyzed], ['stall'])
        self.assertEqual(report['class_px_per_placed_instance']['cutout_leak'], 32)
        red = np.array(CLASSES['cutout_leak'][0])
        self.assertLess(np.abs(heat[8, 7].astype(int) - red).max(), 20)
        self.assertLess(np.abs(heat[8, 27].astype(int) - red).max(), 20)
        self.assertGreater(np.abs(heat[8, 3].astype(int) - red).max(), 60)


if __name__ == '__main__':
    unittest.main()
