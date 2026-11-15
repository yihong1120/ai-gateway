"""Small live Argon checks through LiteLLM and Open WebUI; never print secrets.

Run with webui_env Python. Defaults to isolated dev; --production uses the
deployment's existing connections. Calls a synthetic tool, never an external
system. The requests consume a small amount of the existing cloud budget.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import time
import uuid

from examples.shared.config import config_path, read_services
import httpx
import jwt
import psycopg

MODEL = 'gemini-4-argon'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--production', action='store_true')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.production:
        sections = read_services('webui', 'litellm', path=config_path())
        web, llm = sections['webui'], sections['litellm']
        web_base = 'http://127.0.0.1:8080'
        environment = 'production'
    else:
        config = Path.home() / '.config/ai-gateway-dev'
        web = json.loads((config / 'webui.json').read_text())
        llm = json.loads((config / 'litellm.json').read_text())
        web_base = 'http://127.0.0.1:18082'
        environment = 'dev'
        if not web['DATABASE_URL'].endswith('/openwebui_dev') or ':15432/' not in web['DATABASE_URL']:
            raise RuntimeError('Refusing a non-dev database.')
        if web['OPENAI_API_BASE_URLS'] != 'http://127.0.0.1:14002/v1':
            raise RuntimeError('Refusing a non-dev gateway.')
    gateway = web['OPENAI_API_BASE_URLS'].split(';')[0].rstrip('/')
    key = web['OPENAI_API_KEYS'].split(';')[0]
    started = datetime.now(timezone.utc)
    test_user = 'argon-smoke-' + uuid.uuid4().hex
    results = {'environment': environment, 'model': MODEL, 'started_at': started.isoformat(), 'checks': []}

    def record(name, status, data=None):
        row = {'name': name, 'http_status': status}
        if data and data.get('usage'):
            row['usage'] = data['usage']
        results['checks'].append(row)
        print('Passed:', name, flush=True)

    with httpx.Client(timeout=180, trust_env=False) as client:
        headers = {'Authorization': 'Bearer ' + key}
        response = client.get(gateway + '/models', headers=headers)
        response.raise_for_status()
        assert MODEL in {x['id'] for x in response.json()['data']}, 'Argon not visible to the WebUI key.'
        record('gateway_model_list', response.status_code)

        def complete(messages, **extra):
            response = client.post(gateway + '/chat/completions', headers=headers, json={
                'model': MODEL, 'messages': messages, 'max_tokens': 2048, 'user': test_user, **extra,
            })
            response.raise_for_status()
            data = response.json()
            assert data['choices'][0]['finish_reason'] != 'length', 'Output budget exhausted.'
            return data

        prompt = [{'role': 'user', 'content': 'Reply only OK.'}]
        data = complete(prompt)  # Exercise the config's default medium reasoning.
        assert data['choices'][0]['message']['content'].strip() == 'OK'
        record('chat_default_medium', 200, data)

        def streaming(url, auth, **extra):
            content, done, usage = '', False, None
            with client.stream('POST', url, headers=auth, json={
                'model': MODEL, 'messages': prompt, 'stream': True, 'max_tokens': 2048,
                'stream_options': {'include_usage': True}, **extra,
            }) as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    if not line.startswith('data:'):
                        continue
                    value = line[5:].strip()
                    if value == '[DONE]':
                        done = True
                        break
                    chunk = json.loads(value)
                    assert 'error' not in chunk, 'Provider returned an SSE error.'
                    for choice in chunk.get('choices', []):
                        content += choice.get('delta', {}).get('content') or ''
                    usage = chunk.get('usage') or usage
            assert done and content.strip() == 'OK', 'Streaming output or completion missing.'
            return {'usage': usage}

        data = streaming(gateway + '/chat/completions', headers, reasoning_effort='high', user=test_user)
        record('gateway_stream_high', 200, data)

        tool = {'type': 'function', 'function': {'name': 'report_status',
                'description': 'Return the synthetic test status.',
                'parameters': {'type': 'object', 'properties': {}, 'required': []}}}
        messages = [{'role': 'user', 'content': 'Call report_status once, then reply with its status only.'}]
        data = complete(messages, tools=[tool], tool_choice='auto', reasoning_effort='low')
        assistant = data['choices'][0]['message']
        calls = assistant.get('tool_calls', [])
        assert len(calls) == 1 and calls[0]['function']['name'] == 'report_status'
        assert json.loads(calls[0]['function']['arguments']) == {}
        record('tool_call_auto_low', 200, data)
        # Preserve provider_specific_fields, including Gemini thought signatures.
        messages += [assistant, {'role': 'tool', 'tool_call_id': calls[0]['id'],
                                 'content': json.dumps({'status': 'OK'})}]
        data = complete(messages, tools=[tool], tool_choice='auto', reasoning_effort='low')
        assert data['choices'][0]['message']['content'].strip() == 'OK'
        record('tool_result_round_trip', 200, data)

        data = complete([{'role': 'user', 'content': 'Return status OK.'}], reasoning_effort='low',
            response_format={'type': 'json_schema', 'json_schema': {'name': 'status', 'strict': True,
                'schema': {'type': 'object', 'properties': {'status': {'type': 'string', 'enum': ['OK']}},
                           'required': ['status'], 'additionalProperties': False}}})
        assert json.loads(data['choices'][0]['message']['content']) == {'status': 'OK'}
        record('structured_output', 200, data)

        with psycopg.connect(web['DATABASE_URL']) as conn, conn.cursor() as cursor:
            cursor.execute('SELECT id FROM "user" WHERE role=%s ORDER BY created_at LIMIT 1', ('admin',))
            row = cursor.fetchone()
            assert row, 'No existing WebUI admin available for the API smoke check.'
        now = int(time.time())
        token = jwt.encode({'id': row[0], 'iat': now, 'exp': now + 300, 'jti': uuid.uuid4().hex},
                           web['WEBUI_SECRET_KEY'], algorithm='HS256')
        web_headers = {'Authorization': 'Bearer ' + token}
        response = client.get(web_base + '/api/models', headers=web_headers)
        response.raise_for_status()
        assert MODEL in {x['id'] for x in response.json()['data']}, 'Argon not visible in WebUI.'
        record('webui_model_list', response.status_code)
        data = streaming(web_base + '/api/chat/completions', web_headers)
        record('webui_stream_default_medium', 200, data)

        # Check persisted accounting, not only a response cost header. Only the
        # synthetic gateway user is queried; no personal usage is included.
        master = {'Authorization': 'Bearer ' + llm['LITELLM_MASTER_KEY']}
        deadline = time.monotonic() + 45
        logs = []
        while time.monotonic() < deadline:
            response = client.get(gateway.removesuffix('/v1') + '/spend/logs/v2', headers=master,
                params={'end_user': test_user, 'start_date': str(started.date()),
                        'end_date': str((started + timedelta(days=1)).date())})
            response.raise_for_status()
            payload = response.json()
            logs = payload if isinstance(payload, list) else payload.get('data', [])
            if len(logs) >= 5 and all(float(x.get('spend') or 0) > 0 for x in logs):
                break
            time.sleep(2)
        assert len(logs) >= 5 and all(float(x.get('spend') or 0) > 0 for x in logs), 'Persisted Argon costs missing.'
        results['gateway_spend_usd'] = sum(float(x['spend']) for x in logs)
        results['gateway_spend_rows'] = len(logs)
        record('persisted_nonzero_cost', response.status_code)
    results['completed_at'] = datetime.now(timezone.utc).isoformat()
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(results, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
