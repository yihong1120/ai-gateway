"""Live dev-only enrollment and revocation check; no model calls by default."""
from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import sys
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path

import httpx

import examples.company_ai_access.services.devices as _devices_imports
from examples.shared.runtime.modules import ModuleReference

_examples_deployment_dev_run = ModuleReference('examples.deployment.dev.run')
_webui_auths = ModuleReference('open_webui.models.auths')
_webui_users = ModuleReference('open_webui.models.users')
_webui_auth = ModuleReference('open_webui.utils.auth')

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
CONFIG, load_environment = _examples_deployment_dev_run.attributes('CONFIG', 'load_environment')


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--model-call', action='store_true',
        help='Issue one tiny Claude request; may consume dev model quota.',
    )
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    web = load_environment('webui')
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    Auths = _webui_auths.attribute('Auths')
    Users = _webui_users.attribute('Users')
    create_token = _webui_auth.attribute('create_token')

    operator = json.loads((CONFIG / 'operator.json').read_text())
    user = await Users.get_user_by_email(operator['email'])
    if not user or (user.oauth or {}).get('oidc', {}).get('sub') != operator['id']:
        raise RuntimeError('Expected the existing isolated dev operator.')
    auth = await Auths.get_auth_by_id(user.id)
    token = create_token(
        {
            'id': user.id, 'session_stamp': auth.session_stamp,
            'typ': 'automation',
        }, timedelta(minutes=5),
    )
    browser = {'Authorization': 'Bearer ' + token, 'Origin': web['WEBUI_URL'], 'X-Company-AI-Action': '1'}
    verifier = secrets.token_urlsafe(48)
    result = {
        'environment': 'dev', 'at': datetime.now(timezone.utc).isoformat(), 'checks': {
        }, 'model_request': 'not_requested',
    }
    device_id = None
    async with httpx.AsyncClient(base_url='http://127.0.0.1:18082', timeout=45, trust_env=False) as client:
        for attempt in range(45):
            try:
                response = await client.get('/health', timeout=2)
                if response.status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            await asyncio.sleep(1)
        else:
            raise RuntimeError('Isolated dev WebUI did not become ready.')
        response = await client.get('/api/company-ai/policy', headers=browser)
        if response.status_code != 200:
            raise RuntimeError('Dev policy check failed: HTTP ' + str(response.status_code))
        policy = response.json()
        assert policy['environment'] == 'dev' and policy['confirmation_seconds'] == 90 * 86400
        result['checks']['authenticated_policy'] = True
        if user.role == 'admin':
            response = await client.get('/api/company-ai/admin/devices', headers=browser)
            assert response.status_code == 200, 'Current dev IdP administrator role check failed.'
            result['checks']['current_idp_admin_role'] = True
        assert (await client.get('/api/company-ai/devices')).status_code == 401
        result['checks']['anonymous_control_denied'] = True
        listing = await client.get('/api/company-ai/devices', headers=browser)
        for stale in listing.json()['devices']:
            if stale['label'] == 'DEV-SMOKE-REMOVE' and stale['status'] != 'revoked':
                cleaned = await client.post('/api/company-ai/devices/' + stale['id'] + '/revoke', headers=browser, json={})
                cleaned.raise_for_status()
        started = await client.post('/api/company-ai/device/start', json={'device_name': 'DEV-SMOKE-REMOVE', 'code_challenge': _devices_imports.challenge(verifier)})
        started.raise_for_status()
        enrollment = started.json()
        proof = {'request_id': enrollment['request_id'], 'code_verifier': verifier}
        try:
            pending = await client.post('/api/company-ai/device/poll', json=proof)
            assert pending.json() == {'status': 'pending'}
            approved = await client.post(
                '/api/company-ai/enrollments/' + enrollment['request_id'] + '/approve',
                headers=browser, json={'confirm': True, 'user_code': enrollment['user_code']},
            )
            approved.raise_for_status()
            device_id = approved.json()['device_id']
            ready = await client.post('/api/company-ai/device/poll', json=proof)
            if ready.status_code != 200:
                raise RuntimeError('Native OSS key creation failed: HTTP ' + str(ready.status_code))
            data = ready.json()
            assert data['status'] == 'ready'
            key_headers = {'Authorization': 'Bearer ' + data['key']}
            ack = await client.post('/api/company-ai/device/ack', json=proof)
            ack.raise_for_status()
            assert (await client.post('/api/company-ai/device/poll', json=proof)).status_code == 410
            result['checks']['enrollment_delivery_ack_and_replay_denial'] = True
            models = await client.get('/company-ai/v1/models', headers=key_headers)
            models.raise_for_status()
            assert {m['id'] for m in models.json()['data']} == set(policy['models'])
            result['checks']['native_device_key_models'] = True
            # Direct management is checked only on dev loopback, never with a
            # management key in an employee or browser request.
            llm = json.loads((CONFIG / 'litellm.json').read_text())
            admin = {'Authorization': 'Bearer ' + llm['LITELLM_MASTER_KEY']}
            response = await client.post(
                'http://127.0.0.1:14002/key/update', headers=admin,
                json={'key': data['key'], 'duration': '30d'},
            )
            assert response.status_code == 200, 'OSS expiration update rejected.'
            result['checks']['oss_expiration_update'] = True
            if args.model_call:
                response = await client.post(
                    '/company-ai/v1/messages', headers={**key_headers, 'anthropic-version': '2023-06-01'},
                    json={
                        'model': policy['default_model'], 'messages': [{'role': 'user', 'content': 'Say OK.'}], 'max_tokens': 1,
                        'metadata': {'user_id': 'attempted-spoof'},
                    },
                )
                result['model_request'] = {'status': response.status_code, 'success': response.is_success}
                if response.is_success:
                    assert response.json().get('id')
                else:
                    native = await client.post(
                        'http://127.0.0.1:14002/v1/messages',
                        headers={
                            **key_headers, 'anthropic-version': '2023-06-01',
                            'x-litellm-customer-id': operator['id'],
                        },
                        json={
                            'model': policy['default_model'], 'messages': [{'role': 'user', 'content': 'Say OK.'}],
                            'max_tokens': 1, 'metadata': {'user_id': operator['id']},
                        },
                    )
                    text = native.text.lower()
                    result['model_request']['native_status'] = native.status_code
                    try:
                        result['model_request']['native_error_type'] = native.json().get('error', {}).get('type')
                        details = native.json().get('detail')
                        if isinstance(details, list):
                            result['model_request']['validation'] = [
                                {'loc': item.get('loc'), 'type': item.get('type')} for item in details
                            ]
                    except (ValueError, AttributeError):
                        pass
                    result['model_request']['diagnostic_markers'] = [
                        marker for marker in
                        ('quota', '429', 'permission', 'not found', 'unsupported', 'allowed route', 'rate limit', 'resource exhausted', 'vertex') if marker in text
                    ]
            forbidden = await client.post('/company-ai/v1/messages', headers=key_headers, json={'model': 'not-allowed'})
            assert forbidden.status_code == 403
            result['checks']['model_allowlist'] = True
            listing = await client.get('/api/company-ai/devices', headers=browser)
            assert data['key'] not in listing.text and 'key_box' not in listing.text
            result['checks']['no_key_in_browser_device_list'] = True
            revoked = await client.post('/api/company-ai/devices/' + device_id + '/revoke', headers=browser)
            revoked.raise_for_status()
            assert not revoked.json()['upstream_block_pending']
            assert (await client.get('/company-ai/v1/models', headers=key_headers)).status_code == 401
            result['checks']['revoked_key_denied'] = True
        finally:
            if device_id:
                response = await client.post('/api/company-ai/devices/' + device_id + '/revoke', headers=browser)
                if response.status_code != 200:
                    raise RuntimeError('Smoke device cleanup failed; revoke the labeled dev device.')
    if args.output:
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    asyncio.run(main())
