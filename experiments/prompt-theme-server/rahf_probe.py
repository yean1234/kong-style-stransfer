"""Optional RichHF (RAHF) comparison for a saved run. Not used by the gate.

Runs the community RAHF reimplementation (github.com/youweiliang/RichHF, CC BY-NC 4.0
weights, non-commercial only) on each object's composite.png and saves its
implausibility/misalignment heatmaps next to the rule-based problem heatmap.
A zero-shot probe on simulated pixel-art leaks was weak (see
docs/prompt-theme-integration.md), so treat the output as a comparison aid
until it is fine-tuned on our own labels.

Requires torch, transformers, sentencepiece, a local RichHF clone and its checkpoint:
  THEME_RAHF_REPO=/path/to/RichHF THEME_RAHF_CKPT=/path/to/rahf_model.pt \
  python rahf_probe.py RUN_DIR "pixel art christmas decorations with snow and lights"
"""
import json
import os
import sys
from pathlib import Path

import numpy as np
from PIL import Image


def fit(image, side=384):
    """Nearest-upscale a sprite onto the model's square gray canvas; return the mapping."""
    scale = max(1, side // max(image.size))
    large = image.resize((image.width * scale, image.height * scale), Image.Resampling.NEAREST)
    canvas = Image.new('RGBA', (side, side), (128, 128, 128, 255))
    left, top = (side - large.width) // 2, (side - large.height) // 2
    canvas.alpha_composite(large, (left, top))
    return canvas.convert('RGB'), (left, top, left + large.width, top + large.height)


def main(folder, caption):
    sys.path.insert(0, os.environ['THEME_RAHF_REPO'])
    import torch
    from model import RAHF
    from transformers import AutoImageProcessor
    processor = AutoImageProcessor.from_pretrained('google/vit-large-patch16-384')
    model = RAHF()
    model.load_state_dict(torch.load(os.environ['THEME_RAHF_CKPT'], map_location='cpu'))
    model.eval()
    for composite_path in sorted(Path(folder).glob('*/composite.png')):
        target = composite_path.parent
        image = Image.open(composite_path).convert('RGBA')
        canvas, box = fit(image)
        with torch.no_grad():
            output = model(processor(canvas, return_tensors='pt')['pixel_values'], [caption])
        report = {'caption': caption, 'scores': {name: float(value[0]) for name, value in output['scores'].items()},
                  'misaligned_text': output['output_seq'][0]}
        for name, heat in output['heatmaps'].items():
            values = heat[0].numpy()[box[1]:box[3], box[0]:box[2]]
            native = Image.fromarray(values).resize(image.size, Image.Resampling.BOX)
            peak = float(np.asarray(native).max())
            report[name + '_max'] = peak
            scaled = np.asarray(native) / peak if peak > 0 else np.asarray(native)
            Image.fromarray((scaled * 255).astype(np.uint8)).save(target / f'rahf-{name}.png')
        (target / 'rahf.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(target.name, json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    if len(sys.argv) != 3 or 'THEME_RAHF_REPO' not in os.environ or 'THEME_RAHF_CKPT' not in os.environ:
        print(__doc__)
        sys.exit(2)
    main(sys.argv[1], sys.argv[2])
