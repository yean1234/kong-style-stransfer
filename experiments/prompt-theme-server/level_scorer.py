"""One-token visual scores from the configured OpenAI-compatible VLM endpoint."""
import base64
import io
import math

import requests
from PIL import Image


LEVELS = {'excellent': 5, 'good': 4, 'fair': 3, 'poor': 2, 'bad': 1}
YES_NO = {'yes': 1, 'no': 0}
DECORATION_TEXT = {
    'snow': 'snow on its top surfaces',
    'lights': 'a string of small glowing Christmas light bulbs',
    'garland': 'a green garland or wreath',
    'pumpkins': 'small orange jack-o-lantern pumpkins',
    'leaves': 'orange autumn leaves'
}


def data_url(image, side=384):
    rgba = image.convert('RGBA')
    background = Image.new('RGBA', rgba.size, (128, 128, 128, 255))
    background.alpha_composite(rgba)
    scale = max(1, side // max(background.size))
    rgb = background.convert('RGB')
    if scale > 1:
        rgb = rgb.resize((rgb.width * scale, rgb.height * scale), Image.Resampling.NEAREST)
    buffer = io.BytesIO()
    rgb.save(buffer, format='PNG')
    return 'data:image/png;base64,' + base64.b64encode(buffer.getvalue()).decode('ascii')


def level_score(url, model, images, question, prefix, levels=LEVELS, timeout=120):
    content = [{'type': 'image_url', 'image_url': {'url': data_url(image)}} for image in images]
    content.append({'type': 'text', 'text': question})
    response = requests.post(url.rstrip('/') + '/v1/chat/completions', json={
        'model': model, 'max_tokens': 1, 'logprobs': True, 'top_logprobs': 20,
        'messages': [{'role': 'user', 'content': content}, {'role': 'assistant', 'content': prefix}],
        'add_generation_prompt': False, 'continue_final_message': True,
        'chat_template_kwargs': {'enable_thinking': False}}, timeout=timeout)
    response.raise_for_status()
    top = response.json()['choices'][0]['logprobs']['content'][0]['top_logprobs']
    probabilities = {key: 0.0 for key in levels}
    for token in top:
        word = token['token'].strip().lower()
        if word in probabilities:
            probabilities[word] += math.exp(token['logprob'])
    mass = sum(probabilities.values())
    if mass <= 0:
        return {'score': None, 'level_mass': 0.0}
    score = sum(levels[word] * probability for word, probability in probabilities.items()) / mass
    return {'score': score, 'level_mass': mass}


def sc_scores(url, model, composite, decorations, timeout=120):
    results = {}
    for decoration in decorations:
        description = DECORATION_TEXT.get(decoration, f'the requested {decoration} decoration')
        try:
            result = level_score(url, model, [composite],
                f'Does this pixel-art sprite have {description}? Answer yes or no.', 'Answer:', YES_NO, timeout)
            results[decoration] = result
        except Exception as error:
            results[decoration] = {'score': None, 'level_mass': 0.0, 'error': f'{type(error).__name__}: {error}'}
    return results


def pq_scores(url, model, original, composite, theme, timeout=120):
    questions = [
        ('style', [original, composite],
         f'Image 1 is the original pixel-art sprite. Image 2 is the same sprite with added {theme} decorations. '
         'Rate how well the added decorations match the pixel-art style of image 1 (palette, outline, pixel size, shading). '
         'Answer with one word: excellent, good, fair, poor, or bad.',
         'The style match is'),
        ('clean_composite', [composite],
         'Rate the visual quality of this pixel-art sprite: clean pixels, nothing smeared, broken or distorted. '
         'Answer with one word: excellent, good, fair, poor, or bad.',
         'The quality of the sprite is'),
        ('clean_original', [original],
         'Rate the visual quality of this pixel-art sprite: clean pixels, nothing smeared, broken or distorted. '
         'Answer with one word: excellent, good, fair, poor, or bad.',
         'The quality of the sprite is')
    ]
    results = {}
    for name, images, question, prefix in questions:
        try:
            results[name] = level_score(url, model, images, question, prefix, timeout=timeout)
        except Exception as error:
            results[name] = {'score': None, 'level_mass': 0.0, 'error': f'{type(error).__name__}: {error}'}
    return results
