"""Run local document OCR comparisons; keep document text in the private output directory."""
from __future__ import annotations

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import threading
import time
import urllib.request


class GPU:
    def __init__(self):
        self.samples = []
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.sample, daemon=True)

    def sample(self):
        while not self.stop.is_set():
            try:
                output = subprocess.check_output([
                    'nvidia-smi', '--query-gpu=memory.used,memory.free,utilization.gpu',
                    '--format=csv,noheader,nounits'], text=True, timeout=5)
                used, free, gpu = map(int, output.strip().split(','))
                self.samples.append({'time': time.monotonic(), 'used_MiB': used,
                                     'free_MiB': free, 'utilization': gpu})
            except Exception:
                pass
            self.stop.wait(.5)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.stop.set()
        self.thread.join(timeout=6)


def production(image):
    data = {'file': base64.b64encode(image.read_bytes()).decode(), 'fileType': 1,
            'useLayoutDetection': True, 'restructurePages': False,
            'visualize': False, 'maxNewTokens': 4096}
    req = urllib.request.Request('http://127.0.0.1:8113/layout-parsing',
                                 data=json.dumps(data).encode(),
                                 headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=180) as response:
        result = json.load(response)
    if result.get('errorCode') != 0:
        raise RuntimeError(str(result.get('errorMsg')))
    entries = result['result']['layoutParsingResults']
    text = '\n'.join(block.get('block_content', '') for entry in entries
                     for block in entry['prunedResult'].get('parsing_res_list', []))
    markdown = '\n'.join(entry.get('markdown', {}).get('text', '') for entry in entries)
    return text, markdown, result


def pipeline_backend(backend, root):
    if backend == 'glm':
        import torch
        from glmocr import GlmOcr
        torch.set_num_threads(4)
        pipeline = GlmOcr(config_path=str(root / 'glm.yaml'), layout_device='cpu', mode='selfhosted')
        assert not pipeline.config_model.pipeline.maas.enabled

        def infer(image):
            result = pipeline.parse(str(image))
            raw = result.json_result
            if isinstance(raw, str):
                raw = json.loads(raw)
            markdown = getattr(result, 'markdown_result', None)
            if markdown is None:
                markdown = getattr(result, 'markdown', '')
            if not isinstance(markdown, str):
                markdown = json.dumps(markdown, ensure_ascii=False)
            return markdown, markdown, raw

        return infer

    from paddlex import create_pipeline
    if backend.startswith('paddle'):
        pipeline = create_pipeline(str(root / (backend + '.yaml')), device='gpu:0')
    else:
        from paddlex.inference.models.runners.paddle_static.config.pp_option import PaddlePredictorOption
        # Paddle 3.3's oneDNN PIR path fails on PP-OCRv6 on this host.
        option = PaddlePredictorOption(run_mode='paddle', cpu_threads=4)
        pipeline = create_pipeline('OCR', device='cpu' if backend == 'ppocr-cpu' else 'gpu:0', pp_option=option)
    lock = threading.Lock()

    def infer(image):
        with lock:
            if backend.startswith('paddle'):
                predictions = list(pipeline.predict(str(image), use_doc_orientation_classify=False,
                                                    use_doc_unwarping=False, max_new_tokens=4096))
            else:
                predictions = list(pipeline.predict(str(image), use_doc_orientation_classify=False,
                                                    use_doc_unwarping=False, use_textline_orientation=False))
        raw = [p.json['res'] for p in predictions]
        if backend.startswith('paddle'):
            text = '\n'.join(b.get('block_content', '') for p in raw for b in p.get('parsing_res_list', []))
            markdown = '\n'.join(p.markdown.get('markdown_text', '') for p in predictions)
        else:
            text = '\n'.join(t for p in raw for t in p.get('rec_texts', []))
            markdown = text
        return text, markdown, raw

    return infer


def run(args):
    manifest = json.loads((args.root / 'corpus/manifest.json').read_text())
    cases = manifest['cases']
    if args.only:
        cases = [case for case in cases if case['id'] in args.only.split(',')]
    start = time.monotonic()
    infer = production if args.backend == 'production' else pipeline_backend(args.backend, args.root)
    startup = time.monotonic() - start
    print(json.dumps({'backend': args.backend, 'loaded_seconds': round(startup, 3)}), flush=True)
    output = args.root / ('results/' + args.label)
    output.mkdir(parents=True, exist_ok=True)
    with GPU() as gpu:
        # One untimed warm-up is separately recorded; measured cases follow.
        before = time.monotonic()
        infer(Path(cases[0]['image']))
        warmup = time.monotonic() - before
        if args.ready_file:
            args.ready_file.write_text('ready')
            deadline = time.monotonic() + 120
            while not args.start_file.exists():
                if time.monotonic() > deadline:
                    raise TimeoutError('Benchmark start signal did not arrive')
                time.sleep(.05)
        results = []
        report = {'backend': args.backend, 'label': args.label,
                  'startup_seconds': round(startup, 3), 'warmup_seconds': round(warmup, 3),
                  'concurrency': args.concurrency, 'results': results}

        def one(case, repeat):
            started = time.monotonic()
            result = {'case': case['id'], 'kind': case['kind'], 'repeat': repeat}
            try:
                text, markdown, raw = infer(Path(case['image']))
                if not text.strip():
                    raise RuntimeError('OCR returned no text')
                result.update(success=True, text=text, markdown=markdown)
                (output / f'{case["id"]}-{repeat}.raw.json').write_text(
                    json.dumps(raw, ensure_ascii=False, default=str))
            except Exception as error:
                result.update(success=False, error=repr(error)[:1500], text='', markdown='')
            result['seconds'] = round(time.monotonic() - started, 4)
            relevant = [s for s in gpu.samples if s['time'] >= started]
            result['gpu_peak_MiB'] = max((s['used_MiB'] for s in relevant), default=None)
            result['gpu_min_free_MiB'] = min((s['free_MiB'] for s in relevant), default=None)
            return result

        for repeat in range(args.repeat):
            begin = time.monotonic()
            with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
                futures = [pool.submit(one, case, repeat) for case in cases]
                for future in futures:
                    result = future.result()
                    results.append(result)
                    (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
                    print(json.dumps({k: result.get(k) for k in ('case', 'repeat', 'success', 'seconds', 'error')},
                                     ensure_ascii=False), flush=True)
            report.setdefault('round_seconds', []).append(round(time.monotonic() - begin, 4))
            (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
        report['gpu_peak_MiB'] = max((s['used_MiB'] for s in gpu.samples), default=None)
        report['gpu_min_free_MiB'] = min((s['free_MiB'] for s in gpu.samples), default=None)
        (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print('Saved ' + str(output / 'report.json'), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend', choices=['production', 'paddle15', 'paddle16', 'glm', 'ppocr-cpu', 'ppocr-gpu'], required=True)
    parser.add_argument('--label', required=True)
    parser.add_argument('--root', type=Path, default=Path('/home/changdar/opt/ocr-comparison-20261014'))
    parser.add_argument('--repeat', type=int, default=2)
    parser.add_argument('--concurrency', type=int, default=1)
    parser.add_argument('--only')
    parser.add_argument('--ready-file', type=Path)
    parser.add_argument('--start-file', type=Path)
    run(parser.parse_args())
