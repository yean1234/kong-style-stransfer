import math
import os
import base64
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import Image

from decoration_extractor import extract_changes, fix_overlay
from level_scorer import level_score
from palette_planner import PaletteUnavailableError, build_palette_plan, save_palette_swatch
from style_gate import GateConfigurationError, gate_config, run_candidates
import profile_decorations


def palette(colors=None):
    colors = colors or {'lights': '#256BFF'}
    return {
        'schema_version': 'palette-plan-v1',
        'theme': 'winter',
        'requested_decorations': list(colors),
        'original_palette': {'source': 'original', 'colors_hex': ['#202020']},
        'decoration_palettes': {name: [{'hex': value, 'role': 'base', 'source': 'explicit'}]
                                for name, value in colors.items()},
        'shared_palette': [{'hex': '#202020', 'role': 'outline', 'source': 'original'}],
        'freeze_per_request': True,
        'plan_hash': 'a' * 64,
        'warnings': [],
        'status': 'ready'
    }


def source_and_change(color=(37, 107, 255)):
    original = Image.new('RGBA', (12, 12), (32, 32, 32, 255))
    generated = original.convert('RGB')
    generated.paste(color, (4, 4, 8, 8))
    return original, generated


class PalettePlanTests(unittest.TestCase):
    def test_explicit_hex_is_used_without_a_model_call(self):
        image = Image.new('RGBA', (4, 4), (20, 30, 40, 255))
        with patch.dict(os.environ, {}, clear=True):
            plan = build_palette_plan('Winter lights #1E6BFF', 'winter', ['lights'], [image])
        self.assertEqual(plan['decoration_palettes']['lights'][0]['hex'], '#1E6BFF')
        self.assertEqual(plan['decoration_palettes']['lights'][0]['source'], 'explicit')
        self.assertEqual(len(plan['plan_hash']), 64)
        self.assertTrue(plan['freeze_per_request'])

    def test_text_only_palette_fails_when_real_vlm_is_not_configured(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(PaletteUnavailableError):
                build_palette_plan('blue winter lights', 'winter', ['lights'])

    def test_explicit_palette_keeps_more_than_the_suggestion_budget(self):
        colors = [f'#{value:06X}' for value in range(0x100, 0x10A)]
        plan = build_palette_plan('winter lights', 'winter', ['lights'], explicit_palette={'lights': colors})
        self.assertEqual(len(plan['decoration_palettes']['lights']), 10)
        self.assertTrue(all(item['source'] == 'explicit' for item in plan['decoration_palettes']['lights']))

    def test_reference_and_original_hashes_and_full_image_warning_are_recorded(self):
        reference = Image.new('RGB', (3, 2), '#276BEE')
        buffer = io.BytesIO(); reference.save(buffer, format='PNG')
        data = 'data:image/png;base64,' + base64.b64encode(buffer.getvalue()).decode('ascii')
        original = Image.new('RGBA', (4, 4), (20, 30, 40, 255))
        plan = build_palette_plan('winter lights', 'winter', ['lights'], [original],
                                  reference_image=data)
        self.assertEqual(len(plan['reference_sha256']), 64)
        self.assertEqual(len(plan['original_sha256'][0]), 64)
        self.assertIn('reference_region_unset_uses_full_image', plan['warnings'])
        self.assertEqual(plan['decoration_palettes']['lights'][0]['source'], 'reference')
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'palette-swatch.png'
            save_palette_swatch(plan, path)
            with Image.open(path) as swatch:
                self.assertTrue(swatch.size[0] > 0)

    def test_invalid_explicit_rgb_is_not_silently_replaced_by_model_palette(self):
        with self.assertRaisesRegex(PaletteUnavailableError, 'RGB channels'):
            build_palette_plan('lights rgb(300, 20, 30)', 'winter', ['lights'])


class ChangeExtractionTests(unittest.TestCase):
    def test_blue_decoration_is_found_and_unchanged_white_source_is_not(self):
        original = Image.new('RGBA', (12, 12), (32, 32, 32, 255))
        original.paste((245, 245, 245, 255), (0, 0, 4, 4))
        generated = original.convert('RGB')
        generated.paste((37, 107, 255), (6, 6, 10, 10))
        allowed = {'lights': np.ones((12, 12), dtype=bool)}
        result = extract_changes(original, generated, allowed, np.zeros((12, 12), bool),
            palette(), ['lights'])
        self.assertEqual(result['extraction']['candidate_px'], 16)
        self.assertEqual(result['extraction']['palette_mismatch_px'], 0)
        self.assertEqual(np.asarray(result['overlay'].getchannel('A'))[:4, :4].sum(), 0)
        fixed, report = fix_overlay(result, original)
        self.assertEqual(fixed.getpixel((6, 6)), (37, 107, 255, 255))
        self.assertEqual(report['protected_overlap_px'], 0)

    def test_profile_generation_uses_the_shared_change_gate(self):
        original = Image.new('RGBA', (4, 4), (32, 32, 32, 255))
        profile = {'kind':'tree','mode':'attached','canvas':[16,16],'scale':2,'offset':[4,4],
            'strength':1.0,'supported':['lights'],'placement_prompt':'upper foliage','box':[0,0,4,4]}
        def fake_request(folder, source, prompt, flux, strength, pipeline=None):
            generated = np.asarray(source).copy()
            generated[6:10,6:10] = [47,120,224]
            result = Image.fromarray(generated)
            result.save(folder/'flux-raw.png')
            return result
        allowed = {'lights':np.ones((4,4),dtype=bool)}
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {}, clear=True), \
                patch.object(profile_decorations,'get_profile',return_value=profile), \
                patch.object(profile_decorations,'profile_masks',return_value=(allowed,np.zeros((4,4),bool))), \
                patch.object(profile_decorations,'request_image',side_effect=fake_request):
            result, overlay = profile_decorations.generate_profile(Path(temp),original,
                {'id':'tree-demo','box':[0,0,4,4]},
                {'theme':'winter','decorations':['lights'],'palette_plan':palette()},
                'http://flux',lambda image,kind=None:image)
            self.assertTrue((Path(temp)/'attempt-00'/'candidate-mask.png').is_file())
        self.assertEqual(result['status'],'unscored')
        self.assertIsNotNone(overlay)
        self.assertEqual(overlay.size,original.size)

    def test_palette_distance_warns_without_dropping_candidates_and_snaps_color(self):
        original, generated = source_and_change((47, 120, 224))
        allowed = {'lights': np.ones((12, 12), dtype=bool)}
        result = extract_changes(original, generated, allowed, palette_plan=palette(), decorations=['lights'])
        self.assertEqual(result['extraction']['candidate_px'], 16)
        self.assertEqual(result['extraction']['palette_mismatch_px'], 16)
        fixed, _ = fix_overlay(result, original)
        self.assertEqual(fixed.getpixel((6, 6)), (37, 107, 255, 255))

    def test_protected_region_is_removed_by_final_fix(self):
        original, generated = source_and_change()
        protected = np.zeros((12, 12), dtype=bool)
        protected[4:8, 4:8] = True
        result = extract_changes(original, generated, {'lights': np.ones((12, 12), bool)},
            protected, palette(), ['lights'])
        self.assertEqual(result['extraction']['candidate_px'], 0)
        fixed, report = fix_overlay(result, original)
        self.assertIsNone(fixed.getbbox())
        self.assertEqual(report['protected_overlap_px'], 0)


class ScoringTests(unittest.TestCase):
    def test_level_score_weights_candidate_token_probabilities(self):
        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {'choices': [{'logprobs': {'content': [{'top_logprobs': [
                    {'token': ' excellent', 'logprob': math.log(.4)},
                    {'token': ' fair', 'logprob': math.log(.2)},
                    {'token': ' unrelated', 'logprob': math.log(.1)}
                ]}]}}]}

        with patch('level_scorer.requests.post', return_value=Response()):
            result = level_score('http://vlm', 'configured-model', [Image.new('RGBA', (8, 8))],
                'Rate the image', 'The style match is')
        self.assertAlmostEqual(result['score'], (5*.4 + 3*.2)/.6)
        self.assertAlmostEqual(result['level_mass'], .6)

    def test_demo_retries_low_score_and_keeps_the_frozen_palette(self):
        low = {'status':'scored','SC':.3,'PQ':.4,'O':math.sqrt(.12),
               'SC_d':{'lights':{'score':.3}}}
        high = {'status':'scored','SC':.8,'PQ':.9,'O':math.sqrt(.72),
                'SC_d':{'lights':{'score':.8}}}
        original, generated = source_and_change()
        calls=[]

        def generate(folder, attempt, feedback, frozen_palette):
            calls.append((attempt, feedback, frozen_palette['plan_hash']))
            return generated, None, {'seed': attempt}

        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {'THEME_GATE_MODE':'demo'}), \
                patch('style_gate._score', side_effect=[low, high]):
            result, overlay = run_candidates(Path(temp), original, ['lights'], 'winter', palette(),
                {'lights':np.ones((12,12),bool)}, np.zeros((12,12),bool), generate)
        self.assertEqual(result['status'], 'applied')
        self.assertEqual(result['selected_candidate'], 1)
        self.assertEqual(result['retries'], 1)
        self.assertEqual([call[2] for call in calls], ['a'*64, 'a'*64])
        self.assertTrue(calls[1][1])
        self.assertIsNotNone(overlay)

    def test_unscored_candidate_is_applied_with_warning_without_retry(self):
        original, generated = source_and_change()
        calls=[]

        def generate(folder, attempt, feedback, frozen_palette):
            calls.append(attempt)
            return generated

        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {'THEME_GATE_MODE':'demo'}), \
                patch('style_gate._score', return_value={'status':'unscored','SC':None,'PQ':None,'O':None}):
            result, overlay = run_candidates(Path(temp), original, ['lights'], 'winter', palette(),
                {'lights':np.ones((12,12),bool)}, np.zeros((12,12),bool), generate)
        self.assertEqual(result['status'], 'unscored')
        self.assertEqual(calls, [0])
        self.assertIsNotNone(overlay)

    def test_shadow_records_a_valid_candidate_without_retry(self):
        original, generated = source_and_change()
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {'THEME_GATE_MODE':'shadow'}), \
                patch('style_gate._score', return_value={'status':'scored','SC':.9,'PQ':.9,'O':.9}):
            result, overlay = run_candidates(Path(temp), original, ['lights'], 'winter', palette(),
                {'lights':np.ones((12,12),bool)}, np.zeros((12,12),bool), lambda *args: generated)
        self.assertEqual(result['status'], 'recorded')
        self.assertEqual(result['retries'], 0)
        self.assertIsNotNone(overlay)

    def test_enforce_requires_calibrated_metrics(self):
        with patch.dict(os.environ, {'THEME_GATE_MODE':'enforce'}):
            with self.assertRaises(GateConfigurationError):
                gate_config()

    def test_enforce_missing_required_score_holds_without_retry(self):
        config = json.loads((Path(__file__).with_name('gate_config.json')).read_text(encoding='utf-8'))
        config.update(mode='enforce', adopted_metrics=['SC'], thresholds={'SC': .8})
        original, generated = source_and_change()
        calls = []
        def generate(folder, attempt, feedback, frozen_palette):
            calls.append(attempt)
            return generated
        with tempfile.TemporaryDirectory() as temp, patch('style_gate.gate_config', return_value=config), \
                patch('style_gate._score', return_value={'status':'unscored','SC':None,'PQ':None,'O':None}):
            result, overlay = run_candidates(Path(temp), original, ['lights'], 'winter', palette(),
                {'lights':np.ones((12,12),bool)}, np.zeros((12,12),bool), generate)
        self.assertEqual(result['status'], 'unscored')
        self.assertEqual(result['retries'], 0)
        self.assertEqual(calls, [0])
        self.assertIn('required_gate_metric_unavailable', result['warnings'])
        self.assertIsNotNone(overlay)


if __name__ == '__main__':
    unittest.main()
