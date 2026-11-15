"""Compare local OCR pipelines on a frozen private corpus using local inference.

Run orientation preparation with the Paddle environment, GLM with its SDK on
PYTHONPATH, and MinerU with its isolated environment. Model downloads must finish
before inference: workers force offline mode and disable MinerU's cloud features.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from pathlib import Path

import yaml

from examples.deployment.diagnostics.ocr_worker import GPU
from examples.deployment.diagnostics.ocr_worker import pipeline_backend
from examples.deployment.diagnostics.ocr_worker import production
from examples.shared.runtime.modules import ModuleReference

_mineru_config = ModuleReference('mineru.config')
_mineru_parser = ModuleReference('mineru.parser')
_cv2 = ModuleReference('cv2')
_paddlex = ModuleReference('paddlex')
_paddlex_inference_models_runners_paddle_static_config_pp_option = ModuleReference(
    'paddlex.inference.models.runners.paddle_static.config.pp_option',
)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prepare(root):
    cv2 = _cv2.load()
    create_pipeline = _paddlex.attribute('create_pipeline')
    PaddlePredictorOption = _paddlex_inference_models_runners_paddle_static_config_pp_option.attribute(
        'PaddlePredictorOption',
    )

    config = {
        'pipeline_name': 'doc_preprocessor',
        'use_doc_orientation_classify': True,
        'use_doc_unwarping': False,
        'SubModules': {
            'DocOrientationClassify': {
                'module_name': 'doc_text_orientation',
                'model_name': 'PP-LCNet_x1_0_doc_ori',
                'model_dir': '/home/changdar/.cache/paddleocr-vl/official_models/PP-LCNet_x1_0_doc_ori',
            },
        },
    }
    started = time.monotonic()
    pipeline = create_pipeline(
        config=config, device='cpu', pp_option=PaddlePredictorOption(run_mode='paddle', cpu_threads=4),
    )
    manifest = json.loads((root / 'corpus/manifest.json').read_text())
    output = root / 'oriented'
    output.mkdir(exist_ok=True)
    report = {'startup_seconds': time.monotonic() - started, 'cases': {}}
    for case in manifest['cases']:
        path = Path(case['image'])
        assert digest(path) == case['sha256']
        started = time.monotonic()
        prediction = next(pipeline.predict(str(path)))
        target = output / path.name
        assert cv2.imwrite(str(target), prediction['output_img'])
        row = {
            'angle': int(prediction['angle']),
            'seconds': time.monotonic() - started,
            'image': str(target),
            'sha256': digest(target),
        }
        report['cases'][case['id']] = row
        print(json.dumps({'case': case['id'], 'angle': row['angle'], 'seconds': round(row['seconds'], 3)}), flush=True)
    (output / 'report.json').write_text(json.dumps(report, indent=2))


def backend(name, root):
    if name == 'paddle':
        return production
    if name == 'glm':

        config = yaml.safe_load((root / 'glm.yaml').read_text())['pipeline']
        assert config['maas']['enabled'] is False, 'Disable cloud MaaS before inference'
        api = config['ocr_api']
        assert api['api_host'] in {'127.0.0.1', 'localhost', '::1'}, 'Use a loopback GLM server'
        assert not api.get('api_url'), 'Do not override the local GLM endpoint'
        return pipeline_backend('glm', root)
    config = _mineru_config.attribute('config')
    parse = _mineru_parser.attribute('parse')

    assert not config.model.vlm.server_url, 'Use a local engine only'
    assert config.model.vlm.engine == 'llama-cpp'
    assert not config.llm_aided.features.title_leveling
    assert not config.llm_aided.features.cross_page_table_cell_merge

    def infer(path):
        result = parse(path, tier='standard', ocr_mode='ocr', image_analysis=True)
        markdown = result.markdown()
        return markdown, markdown, result.to_dict()

    return infer


def run(args):
    os.environ.update(HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', TOKENIZERS_PARALLELISM='false')
    manifest_path = args.root / 'corpus/manifest.json'
    expected = manifest_path.with_suffix('.sha256').read_text().strip()
    assert digest(manifest_path) == expected, 'Frozen reference changed'
    cases = json.loads(manifest_path.read_text())['cases']
    if args.only:
        cases = [c for c in cases if c['id'] in args.only.split(',')]
    orientation = json.loads((args.root / 'oriented/report.json').read_text()) if args.orient else None
    folder = args.root / 'results' / args.label
    folder.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    infer = backend(args.backend, args.root)
    report = {
        'label': args.label,
        'backend': args.backend,
        'orientation': args.orient,
        'manifest_sha256': expected,
        'startup_seconds': time.monotonic() - started,
        'warmup_seconds': None,
        'concurrency': 1,
        'results': [],
    }
    with GPU() as gpu:
        for repeat in range(args.repeat):
            for case in cases:
                meta = orientation['cases'][case['id']] if orientation else case
                path = Path(meta['image'])
                assert digest(path) == meta['sha256']
                started = time.monotonic()
                row = {
                    'case': case['id'],
                    'kind': case['kind'],
                    'repeat': repeat,
                    'orientation_angle': meta.get('angle'),
                    'preprocess_seconds': meta['seconds'] if orientation else 0,
                }
                try:
                    text, markdown, raw = infer(path)
                    if not text.strip() and not case.get('expected_blank'):
                        raise RuntimeError('OCR returned no text on a nonblank page')
                    row.update(success=True, text=text, markdown=markdown)
                    (folder / f'{case["id"]}-{repeat}.raw.json').write_text(
                        json.dumps(raw, ensure_ascii=False, default=str),
                    )
                except Exception as error:
                    row.update(success=False, text='', markdown='', error=repr(error)[:1500])
                row['inference_seconds'] = time.monotonic() - started
                row['seconds'] = round(row['inference_seconds'] + row['preprocess_seconds'], 4)
                samples = [s for s in gpu.samples if s['time'] >= started]
                row['gpu_peak_MiB'] = max((s['used_MiB'] for s in samples), default=None)
                row['gpu_min_free_MiB'] = min((s['free_MiB'] for s in samples), default=None)
                report['results'].append(row)
                report['gpu_peak_MiB'] = max((s['used_MiB'] for s in gpu.samples), default=None)
                report['gpu_min_free_MiB'] = min((s['free_MiB'] for s in gpu.samples), default=None)
                (folder / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
                print(
                    json.dumps(
                        {k: row.get(k) for k in ('case', 'repeat', 'success', 'seconds', 'error')}, ensure_ascii=False,
                    ),
                    flush=True,
                )


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--prepare-orientation', action='store_true')
    parser.add_argument('--backend', choices=['paddle', 'glm', 'mineru'])
    parser.add_argument('--label')
    parser.add_argument('--orient', action='store_true')
    parser.add_argument('--repeat', type=int, default=1)
    parser.add_argument('--only')
    args = parser.parse_args()
    if args.prepare_orientation:
        prepare(args.root)
    else:
        if not args.backend or not args.label:
            parser.error('--backend and --label are required for inference')
        run(args)
