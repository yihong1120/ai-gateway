"""Read-only isolation checks; --chat additionally sends one tiny dev model request."""
import argparse
import json
import os
from pathlib import Path
import subprocess
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import sys
if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from examples.deployment.dev.run import CONFIG
import httpx
import yaml


def verify_litellm(client, master_key, expected_parallel):
    headers = {'Authorization': 'Bearer ' + master_key}
    response = client.get('http://127.0.0.1:14002/health/readiness', headers=headers)
    assert response.status_code == 200, 'Dev LiteLLM database readiness failed: HTTP ' + str(response.status_code)
    response = client.get('http://127.0.0.1:14002/config/field/info', headers=headers,
                          params={'field_name': 'global_max_parallel_requests'})
    assert response.status_code == 200, 'Dev LiteLLM runtime settings failed: HTTP ' + str(response.status_code)
    assert response.json()['field_value'] == expected_parallel, 'Dev LiteLLM runtime concurrency differs from generated configuration.'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--chat', action='store_true')
    args = parser.parse_args()
    web = json.loads((CONFIG / 'webui.json').read_text())
    llm = json.loads((CONFIG / 'litellm.json').read_text())
    litellm_config = yaml.safe_load((CONFIG / 'litellm.yaml').read_text())
    credentials = json.loads((CONFIG / 'credentials.json').read_text())
    assert web['DATABASE_URL'].endswith('/openwebui_dev')
    assert llm['DATABASE_URL'].endswith('/litellm_dev')
    assert ':15432/' in web['DATABASE_URL'] and ':15432/' in llm['DATABASE_URL']
    assert web['DATA_DIR'] == str(Path.home() / '.local/share/ai-gateway-dev/webui')
    assert web['OAUTH_CLIENT_ID'] == 'open-webui-dev'
    assert web['OPENAI_API_BASE_URLS'] == 'http://127.0.0.1:14002/v1'
    assert llm['LITELLM_MASTER_KEY'] != web['OPENAI_API_KEYS']
    assert len(set(credentials['webui_keys'].values())) == 3
    for name in ('checkpoint.key', 'nas.key'):
        key = Path(web['DATA_DIR']) / 'company_agent' / name
        assert key.is_file() and not key.is_symlink() and len(key.read_bytes()) == 32
        assert key.stat().st_mode & 0o077 == 0
    units = ['ai-gateway-dev-' + name for name in ('db', 'litellm', 'webui', 'relay')]
    for unit in units:
        subprocess.run(['systemctl', '--user', 'is-active', '--quiet', unit], check=True)
        subprocess.run(['systemctl', '--user', 'is-enabled', '--quiet', unit], check=True)
    with httpx.Client(timeout=30, trust_env=False, follow_redirects=False) as client:
        verify_litellm(client, llm['LITELLM_MASTER_KEY'],
                       litellm_config['general_settings']['global_max_parallel_requests'])
        r = client.get('http://127.0.0.1:18082/health')
        assert r.status_code == 200 and r.headers['X-AI-Environment'] == 'dev'
        assert client.get('http://127.0.0.1:18082/').status_code == 200
        assert client.get('http://127.0.0.1:18082/api/models').status_code == 401
        for path, method in [('/api/v1/auths/add', 'POST'), ('/api/v1/users/example/update', 'POST'),
                             ('/api/v1/users/example', 'DELETE'), ('/api/v1/auths/update/password', 'POST')]:
            assert client.request(method, 'http://127.0.0.1:18082' + path).status_code == 403
        r = client.get('http://127.0.0.1:18082/oauth/oidc/login',
                       headers={'host': 'ai-dev.changdar-server.mooo.com', 'X-Forwarded-Proto': 'https'})
        assert r.status_code in (302, 307)
        query = parse_qs(urlparse(r.headers['location']).query)
        assert query['client_id'] == ['open-webui-dev']
        assert query['redirect_uri'] == [web['OPENID_REDIRECT_URI']]
        # Authenticate only to this dev database using its independent secret.
        script = '''import asyncio,json
from examples.deployment.dev.run import load_environment
load_environment('webui')
from open_webui.models.users import Users
from open_webui.utils.auth import create_token
from open_webui.utils.mfa import get_auth
from datetime import timedelta
async def main():
 u=await Users.get_first_user()
 a=await get_auth(u.id)
 print(json.dumps({'token':create_token({'id':u.id,'typ':'automation','session_stamp':a.session_stamp},timedelta(minutes=2)), 'sub':u.oauth['oidc']['sub']}))
asyncio.run(main())
'''
        output = subprocess.check_output([Path.home() / '.conda/envs/webui_env/bin/python', '-c', script],
                    cwd=Path(__file__).parent, env={**os.environ, 'PYTHONNOUSERSITE': '1'}, text=True)
        identity = json.loads(output.strip().splitlines()[-1])
        headers = {'Authorization': 'Bearer ' + identity['token']}
        banners = client.get('http://127.0.0.1:18082/api/v1/configs/banners', headers=headers)
        assert banners.status_code == 200
        expected_banners = {'company-account-connections': '/connections',
                            'ai-gateway-eip-workspace': '/eip', 'company-agent-workspace': '/agents'}
        for key, path in expected_banners.items():
            matches = [banner for banner in banners.json() if banner['id'] == key]
            assert len(matches) == 1 and matches[0]['type'] == 'info' and path in matches[0]['content']
            assert client.get('http://127.0.0.1:18082' + path, headers=headers).status_code == 200
        models = client.get('http://127.0.0.1:18082/api/models', headers=headers)
        assert models.status_code == 200
        ids = {row['id'] for row in models.json()['data']}
        assert {'company_work_assistant', 'official_document_processor', 'my_daily_usage'} <= ids
        r = client.get('http://127.0.0.1:14002/spend/logs/v2',
            params={'end_user': identity['sub'], 'start_date': str(datetime.now(timezone.utc).date()),
                    'end_date': str((datetime.now(timezone.utc) + timedelta(days=1)).date())},
            headers={'Authorization': 'Bearer ' + web['LITELLM_USAGE_READER_KEY']})
        assert r.status_code == 200, 'Dev usage reader failed: ' + str(r.status_code)
        r = client.get('http://127.0.0.1:14002/v1/models',
                       headers={'Authorization': 'Bearer ' + web['OPENAI_API_KEYS']})
        assert r.status_code == 200
        if args.chat:
            r = client.post('http://127.0.0.1:18082/api/chat/completions', headers=headers, json={
                'model': 'gemini-3.5-flash-lite', 'messages': [{'role': 'user', 'content': 'Reply only OK.'}],
                'stream': True, 'max_tokens': 16,
            })
            assert r.status_code == 200, 'Dev chat failed: ' + str(r.status_code)
            assert '[DONE]' in r.text and '"content"' in r.text, 'Dev streaming result missing.'
            print('One real dev chat returned streamed content and completion.')
    print('Passed: isolated state, four enabled services, LiteLLM database readiness and runtime concurrency, three info entries, OIDC login redirect, access gates, model list and private usage.')


if __name__ == '__main__':
    main()
