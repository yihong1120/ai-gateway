"""Measure concurrent synthetic requests through a configured gateway or WebUI.

The workload supplies its model, requests and profile; no inference backend is
selected by this tool. Reports contain timings and usage, never credentials.
"""
from __future__ import annotations

import argparse
import json
import os
import threading
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from examples.shared.config import config_path, read_service


def percentile(values, fraction):
    values = sorted(values)
    if not values:
        return None
    index = (len(values) - 1) * fraction
    lower = int(index)
    upper = min(lower + 1, len(values) - 1)
    return round(values[lower] + (values[upper] - values[lower]) * (index - lower), 3)


def run(workload, root):
    model = workload['model']
    config = read_service('webui', config_path(os.getenv('AI_GATEWAY_CONFIG', root / 'config.local.toml')))
    target = workload.get('target', 'gateway')
    key = config['OPENAI_API_KEYS'].split(';')[0]
    base = config['OPENAI_API_BASE_URLS'].split(';')[0]
    scope = 'Independent synthetic user IDs through LiteLLM; not distinct browser sessions.'
    if target == 'webui':
        import jwt
        import psycopg
        with psycopg.connect(config['DATABASE_URL']) as conn, conn.cursor() as cursor:
            cursor.execute('SELECT id FROM "user" WHERE role=%s ORDER BY created_at LIMIT 1', ('admin',))
            user_id = cursor.fetchone()[0]
        now = int(time.time())
        key = jwt.encode(
            {'id': user_id, 'iat': now, 'exp': now + 1800, 'jti': uuid.uuid4().hex},
            config['WEBUI_SECRET_KEY'], algorithm='HS256',
        )
        base = 'http://127.0.0.1:8080/api'
        scope = 'Independent requests through Open WebUI using one admin session; no browser or multi-account ACL test.'
    elif target != 'gateway':
        raise ValueError('Unknown target.')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    run_id = uuid.uuid4().hex
    barrier = threading.Barrier(len(workload['requests']) + 1)

    def request(index, item):
        body = {
            'model': model, 'messages': item['messages'],
            'temperature': 0, 'max_tokens': item['max_tokens'],
            'stream': True, 'stream_options': {'include_usage': True},
            'extra_body': {'min_tokens': item.get('min_tokens', item['max_tokens'] - 1)},
        }
        # Open WebUI injects the verified Keycloak identity itself and rejects
        # a caller-supplied synthetic user. Only direct gateway tests set it.
        if target == 'gateway':
            body['user'] = f'capacity-{run_id}-{index}'
        req = urllib.request.Request(
            base + '/chat/completions', data=json.dumps(body).encode(),
            headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'},
        )
        result = {'index': index, 'label': item['label'],
                  'input_tokens_tokenizer': item['input_tokens_tokenizer'],
                  'max_tokens': item['max_tokens']}
        barrier.wait()
        time.sleep(item.get('delay_seconds', 0))
        started = time.monotonic()
        output = ''
        done = False
        try:
            with opener.open(req, timeout=workload.get('timeout', 900)) as response:
                result['http_status'] = response.status
                result['headers_seconds'] = round(time.monotonic() - started, 3)
                for line in response:
                    if not line.startswith(b'data: '):
                        continue
                    payload = line[6:].strip()
                    if payload == b'[DONE]':
                        done = True
                        break
                    data = json.loads(payload)
                    if data.get('error'):
                        raise RuntimeError(json.dumps(data['error']))
                    if data.get('usage'):
                        result['usage'] = data['usage']
                    for choice in data.get('choices', []):
                        piece = choice.get('delta', {}).get('content') or ''
                        if piece and 'ttft_seconds' not in result:
                            result['ttft_seconds'] = round(time.monotonic() - started, 3)
                        output += piece
                        if choice.get('finish_reason'):
                            result['finish_reason'] = choice['finish_reason']
            result['success'] = bool(done and output and result.get('finish_reason') in ('stop', 'length'))
        except Exception as error:  # noqa: BLE001 - Record each probe without aborting the concurrent workload.
            result['success'] = False
            result['error'] = str(error).replace(key, '[REDACTED]')[:500]
            if isinstance(error, urllib.error.HTTPError):
                result['http_status'] = error.code
                result['error_body'] = error.read().decode(errors='replace').replace(key, '[REDACTED]')[:500]
        result['elapsed_seconds'] = round(time.monotonic() - started, 3)
        result['response_characters'] = len(output)
        return result

    with ThreadPoolExecutor(max_workers=len(workload['requests'])) as executor:
        futures = [executor.submit(request, i, item) for i, item in enumerate(workload['requests'])]
        started = time.monotonic()
        barrier.wait()
        results = [future.result() for future in futures]
    elapsed = time.monotonic() - started
    success = [r for r in results if r['success']]
    totals = [r.get('usage', {}).get('completion_tokens', 0) for r in success]
    return {
        'profile': workload['profile'], 'model': model, 'target': target,
        'concurrent_requests': len(results), 'successes': len(success),
        'failures': len(results) - len(success), 'wall_seconds': round(elapsed, 3),
        'ttft_p50_seconds': percentile([r['ttft_seconds'] for r in success], .5),
        'ttft_p95_seconds': percentile([r['ttft_seconds'] for r in success], .95),
        'ttft_max_seconds': max((r['ttft_seconds'] for r in success), default=None),
        'completion_p50_seconds': percentile([r['elapsed_seconds'] for r in success], .5),
        'completion_p95_seconds': percentile([r['elapsed_seconds'] for r in success], .95),
        'completion_max_seconds': max((r['elapsed_seconds'] for r in success), default=None),
        'output_tokens_total': sum(totals),
        'aggregate_output_tokens_per_second': round(sum(totals) / elapsed, 3),
        'requests': results,
        'scope': scope,
    }


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('workload', type=Path)
    parser.add_argument('--root', type=Path, default=Path.cwd())
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    report = run(json.loads(args.workload.read_text()), args.root)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({k: v for k, v in report.items() if k != 'requests'}, ensure_ascii=False), flush=True)
