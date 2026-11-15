"""Verify deployment-wide end-user budgets with disposable dev-only records."""
from __future__ import annotations

import asyncio
import json
import sys
import uuid
from pathlib import Path

import httpx

from examples.shared.runtime.modules import ModuleReference

_examples_deployment_dev_run = ModuleReference('examples.deployment.dev.run')
if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
load_environment = _examples_deployment_dev_run.attribute('load_environment')


async def main():
    env = load_environment('litellm')  # Refuses anything except A's isolated dev DB.
    subject = 'company-ai-budget-smoke-' + str(uuid.uuid4())
    keys = []
    created = False
    async with httpx.AsyncClient(base_url='http://127.0.0.1:14002', timeout=45, trust_env=False) as client:
        admin = {'Authorization': 'Bearer ' + env['LITELLM_MASTER_KEY']}
        try:
            response = await client.post(
                '/customer/new', headers=admin,
                json={'user_id': subject, 'budget_id': 'openwebui-dev-daily-usd-1', 'spend': 2},
            )
            if response.status_code != 200:
                raise RuntimeError('Dev synthetic customer creation failed: HTTP ' + str(response.status_code))
            created = True
            for label in ('cli', 'webui-style'):
                response = await client.post(
                    '/key/generate', headers=admin,
                    json={
                        'key_alias': 'company-ai-budget-smoke-' + label,
                        'models': ['claude-sonnet-5'], 'duration': '120s',
                    },
                )
                response.raise_for_status()
                keys.append(response.json()['key'])
            cli = await client.post(
                '/v1/messages',
                headers={
                    'Authorization': 'Bearer ' +
                    keys[0], 'anthropic-version': '2023-06-01', 'x-litellm-customer-id': subject,
                },
                json={
                    'model': 'claude-sonnet-5',
                    'messages': [{'role': 'user', 'content': 'Budget denial test.'}], 'max_tokens': 1,
                },
            )
            webui = await client.post(
                '/v1/chat/completions', headers={'Authorization': 'Bearer ' + keys[1]},
                json={
                    'model': 'claude-sonnet-5', 'user': subject,
                    'messages': [{'role': 'user', 'content': 'Budget denial test.'}], 'max_tokens': 1,
                },
            )

            def denied(response):
                return response.status_code in {401, 403, 422, 429} and 'budget' in response.text.lower() and subject in response.text
            result = {
                'environment': 'dev', 'disposable_records': True, 'cli_status': cli.status_code,
                'webui_style_status': webui.status_code, 'same_end_user_budget_denied_both_keys': denied(cli) and denied(webui),
            }
            if not result['same_end_user_budget_denied_both_keys']:
                raise RuntimeError('Shared budget test did not reject both disposable keys for the synthetic customer.')
            print(json.dumps(result, indent=2))
        finally:
            if keys:
                response = await client.post('/key/delete', headers=admin, json={'keys': keys})
                if response.status_code != 200:
                    raise RuntimeError('Disposable dev key cleanup failed.')
            if created:
                response = await client.post('/customer/delete', headers=admin, json={'user_ids': [subject]})
                if response.status_code != 200:
                    raise RuntimeError('Disposable dev customer cleanup failed.')


if __name__ == '__main__':
    asyncio.run(main())
