"""Configure AI access on A dev; create only an owned read-only IdP client."""
from __future__ import annotations

import json
import os
import secrets
import sys
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from examples.shared.runtime.modules import ModuleReference

_examples_deployment_dev_files = ModuleReference('examples.deployment.dev.files')

ROOT = Path(__file__).resolve().parents[3]
if __package__ in (None, ''):
    sys.path.insert(0, str(ROOT))
save = _examples_deployment_dev_files.attribute('save')

CONFIG = Path.home() / '.config/ai-gateway-dev'
CLIENT = 'company-ai-access-dev-directory'
MARKER = 'company-ai-access-dev-v1'
BASE = 'https://changdar-server.mooo.com/keycloak'


def configure():
    if os.geteuid() == 0:
        raise RuntimeError('Run as the dev service owner, without sudo.')
    web_path = CONFIG / 'webui.json'
    web = json.loads(web_path.read_text())
    llm = json.loads((CONFIG / 'litellm.json').read_text())
    parsed = urlsplit(web['DATABASE_URL'])
    if (
        web['WEBUI_URL'] != 'https://ai-dev.changdar-server.mooo.com'
        or parsed.hostname != '127.0.0.1' or parsed.port != 15432 or parsed.path != '/openwebui_dev'
        or llm['LITELLM_MASTER_KEY'] == '' or '/litellm_dev' not in llm['LITELLM_DATABASE_URL']
    ):
        raise RuntimeError('Refusing to configure a non-dev environment.')
    bootstrap = json.loads((Path.home() / 'service-migration-phase5/source-keycloak-env.json').read_text())
    with httpx.Client(timeout=20, trust_env=False) as client:
        response = client.post(
            BASE + '/realms/master/protocol/openid-connect/token', data={
                'client_id': 'admin-cli', 'grant_type': 'password',
                'username': bootstrap['KC_BOOTSTRAP_ADMIN_USERNAME'], 'password': bootstrap['KC_BOOTSTRAP_ADMIN_PASSWORD'],
            },
        )
        response.raise_for_status()
        client.headers['Authorization'] = 'Bearer ' + response.json()['access_token']
        prefix = BASE + '/admin/realms/visionnaire'

        def api(method, path, **kwargs):
            result = client.request(method, prefix + path, **kwargs)
            result.raise_for_status()
            return result.json() if result.content else None

        role_clients = api('GET', '/clients', params={'clientId': 'open-webui-dev'})
        if len(role_clients) != 1:
            raise RuntimeError('The isolated dev role client is required.')
        existing = api('GET', '/clients', params={'clientId': CLIENT})
        desired = {
            'clientId': CLIENT, 'name': 'Company AI DEV read-only membership checks',
            'enabled': True, 'protocol': 'openid-connect', 'publicClient': False,
            'standardFlowEnabled': False, 'directAccessGrantsEnabled': False,
            'serviceAccountsEnabled': True, 'implicitFlowEnabled': False, 'fullScopeAllowed': False,
            'attributes': {'ai-gateway-managed': MARKER},
        }
        if existing:
            if len(existing) != 1 or existing[0].get('attributes', {}).get('ai-gateway-managed') != MARKER:
                raise RuntimeError('Refusing to modify an unowned Keycloak client.')
            cid = existing[0]['id']
            api('PUT', '/clients/' + cid, json={**existing[0], **desired})
        else:
            api('POST', '/clients', json=desired)
            cid = api('GET', '/clients', params={'clientId': CLIENT})[0]['id']
        account = api('GET', '/clients/' + cid + '/service-account-user')
        management = api('GET', '/clients', params={'clientId': 'realm-management'})[0]['id']
        role = api('GET', '/clients/' + management + '/roles/view-users')
        # Grant only read access to the newly created service account, never to
        # an employee. Existing shared realm users and their roles are untouched.
        api('POST', '/users/' + account['id'] + '/role-mappings/clients/' + management, json=[role])
        api('POST', '/clients/' + cid + '/scope-mappings/clients/' + management, json=[role])
        assigned = api('GET', '/users/' + account['id'] + '/role-mappings/clients/' + management)
        if {item['name'] for item in assigned} != {'view-users'}:
            raise RuntimeError('Directory account has unexpected roles; review it before enabling.')
        secret = api('GET', '/clients/' + cid + '/client-secret')['value']
        role_uuid = role_clients[0]['id']
    if not (CONFIG / 'webui.before-ai-access.json').exists():
        save(CONFIG / 'webui.before-ai-access.json', web)
    access_path = CONFIG / 'ai-access.json'
    old_access = json.loads(access_path.read_text()) if access_path.exists() else {}
    bridge = old_access.get('AI_ACCESS_BRIDGE_KEY') or secrets.token_urlsafe(48)
    access = {
        'AI_ACCESS_ENABLED': 'true', 'AI_ACCESS_ENVIRONMENT': 'dev',
        'AI_ACCESS_ORIGIN': 'https://ai-dev.changdar-server.mooo.com',
        'AI_ACCESS_DATABASE_URL': old_access.get('AI_ACCESS_DATABASE_URL') or web['DATABASE_URL'].replace('postgresql+psycopg://', 'postgresql://').replace('postgresql+asyncpg://', 'postgresql://'),
        'AI_ACCESS_DB_SCHEMA': old_access.get('AI_ACCESS_DB_SCHEMA', 'company_ai_access_dev'),
        'AI_ACCESS_STATE_DIR': str(Path.home() / '.local/share/ai-gateway-dev/company-ai-access'),
        'AI_ACCESS_LITELLM_URL': 'http://127.0.0.1:14002',
        'AI_ACCESS_MASTER_KEY': llm['LITELLM_MASTER_KEY'],
        'AI_ACCESS_MODELS': 'claude-sonnet-5,claude-opus-5',
        # Loopback permits operator smoke checks only. Actual company/VPN
        # networks must be explicitly configured after confirming B's topology.
        'AI_ACCESS_INFERENCE_CIDRS': old_access.get('AI_ACCESS_INFERENCE_CIDRS', '127.0.0.0/8'),
        'AI_ACCESS_INFERENCE_DENY_CIDRS': old_access.get('AI_ACCESS_INFERENCE_DENY_CIDRS', ''),
        'AI_ACCESS_ISSUER': BASE + '/realms/visionnaire',
        'AI_ACCESS_DIRECTORY_CLIENT': CLIENT, 'AI_ACCESS_DIRECTORY_SECRET': secret,
        'AI_ACCESS_ROLE_CLIENT_UUID': role_uuid, 'AI_ACCESS_REQUIRED_ROLES': 'dev-user,dev-admin',
        'AI_ACCESS_ADMIN_ROLES': 'dev-admin',
        'AI_ACCESS_BRIDGE_KEY': bridge,
        'AI_ACCESS_APPROVAL_PATH': old_access.get('AI_ACCESS_APPROVAL_PATH', '/company-ai/setup'),
        'AI_ACCESS_OIDC_AUDIENCE': old_access.get('AI_ACCESS_OIDC_AUDIENCE', ''),
        'AI_ACCESS_CONTROL_ORIGINS': old_access.get('AI_ACCESS_CONTROL_ORIGINS', ''),
    }
    save(access_path, access)
    # Remove values written by any previous development iteration. WebUI
    # receives only the scoped bridge credential, never LiteLLM/IdP secrets.
    flutter_dir = web.get('AI_ACCESS_FLUTTER_DIR', str(Path.home() / '.local/share/ai-gateway-dev/flutter-company-ai'))
    web = {key: value for key, value in web.items() if not key.startswith('AI_ACCESS_')}
    web.update({'AI_ACCESS_ENABLED': 'true', 'AI_ACCESS_BRIDGE_URL': 'http://127.0.0.1:18083', 'AI_ACCESS_BRIDGE_KEY': bridge})
    web['AI_ACCESS_FLUTTER_DIR'] = flutter_dir
    save(web_path, web)
    unit_dir = Path.home() / '.config/systemd/user'
    unit_dir.mkdir(parents=True, exist_ok=True)
    unit_path = unit_dir / 'ai-gateway-dev-ai-access.service'
    unit = f'''# Managed by {MARKER}
[Unit]
Description=Company AI OSS access DEV on A
After=ai-gateway-dev-litellm.service
[Service]
WorkingDirectory={ROOT}
ExecStart={Path.home()}/.conda/envs/webui_env/bin/python {ROOT}/examples/deployment/dev/run.py ai-access
Restart=on-failure
RestartSec=5
UMask=0077
NoNewPrivileges=true
[Install]
WantedBy=default.target
'''
    if unit_path.exists() and not unit_path.read_text().startswith('# Managed by ' + MARKER):
        raise RuntimeError('Refusing to replace an unowned systemd unit.')
    unit_path.write_text(unit)
    print('Configured isolated dev AI access. Employee roles and production state were not modified.')
    print('Start ai-gateway-dev-ai-access and restart ai-gateway-dev-webui. Existing source allow/deny networks were preserved; a new setup defaults to loopback only.')


if __name__ == '__main__':
    try:
        configure()
    except Exception as exc:
        # Do not emit HTTP response bodies, request payloads or private config.
        print('AI access setup failed: ' + type(exc).__name__, file=sys.stderr)
        raise SystemExit(1)
