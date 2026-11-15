"""Check B's real OIDC session and disposable device; retire only test artifacts."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import secrets
import socket
from datetime import datetime
from datetime import timezone
from html import unescape
from pathlib import Path
from urllib.parse import parse_qs
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
import jwt
import redis
from cryptography.fernet import Fernet

from examples.shared.config import config_path
from examples.shared.config import ConfigurationError
from examples.shared.config import read_services

STAGE = Path(__file__).resolve().parent
ORIGIN = 'https://changdar-server.mooo.com'
BASE = ORIGIN + '/keycloak'
PREFIX = '/bff/ai_access/api/company-ai'


def smoke_configuration(path):
    sections = read_services('company_ai_smoke', 'litellm', path=path)
    credentials = sections['company_ai_smoke']
    if not all(credentials.get(key, '').strip() for key in ('KC_BOOTSTRAP_ADMIN_USERNAME', 'KC_BOOTSTRAP_ADMIN_PASSWORD')):
        raise ConfigurationError(
            'company_ai_smoke requires KC_BOOTSTRAP_ADMIN_USERNAME and KC_BOOTSTRAP_ADMIN_PASSWORD.',
        )
    return sections


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--direct', action='store_true')
    parser.add_argument('--require-codex', action='store_true')
    parser.add_argument('--model-call', action='store_true')
    parser.add_argument('--source-root', type=Path, default=STAGE)
    parser.add_argument('--operator-file', type=Path, default=STAGE / 'operator.json')
    parser.add_argument('--output-dir', type=Path, default=STAGE)
    parser.add_argument(
        '--config', type=Path,
        help='Central TOML configuration with a dedicated company_ai_smoke section.',
    )
    args = parser.parse_args()
    configuration = smoke_configuration(config_path(args.config))
    credentials = configuration['company_ai_smoke']
    # Preserve HTTPS/SNI/certificate verification while avoiding router hairpin.
    original_lookup = socket.getaddrinfo

    def local_lookup(host, port, *a, **kw):
        return original_lookup('127.0.0.1' if host == 'changdar-server.mooo.com' else host, port, *a, **kw)
    socket.getaddrinfo = local_lookup
    home = Path.home()
    migration = home / 'service-migration-phase5'
    env = json.loads((migration / 'production-bff-config.json').read_text())['environment']
    access = json.loads((home / '.config/company-ai-access/production.json').read_text())
    operator = json.loads(args.operator_file.read_text())['id']
    store = redis.Redis(
        host=env['REDIS_HOST'], port=int(env['REDIS_PORT']),
        password=env.get('REDIS_PASSWORD') or None, socket_timeout=10,
    )
    cipher = Fernet(base64.urlsafe_b64encode(hashlib.sha256(env['BFF_TOKEN_ENCRYPTION_KEY'].encode()).digest()))
    checks = {}
    session_key = sid = device = raw_key = None
    browser = {}
    with httpx.Client(timeout=40, trust_env=False, follow_redirects=False) as client:
        def check(method, path, status=200, **kw):
            response = client.request(method, path if path.startswith('http') else ORIGIN + path, **kw)
            if response.status_code != status:
                raise RuntimeError(
                    method + ' ' + urlsplit(path).path + ': HTTP ' +
                    str(response.status_code) + ', expected ' + str(status),
                )
            return response
        admin_token = check(
            'POST', BASE + '/realms/master/protocol/openid-connect/token', data={
                'client_id': 'admin-cli', 'grant_type': 'password',
                'username': credentials['KC_BOOTSTRAP_ADMIN_USERNAME'],
                'password': credentials['KC_BOOTSTRAP_ADMIN_PASSWORD'],
            },
        ).json()['access_token']
        admin = {'Authorization': 'Bearer ' + admin_token}
        prefix = BASE + '/admin/realms/visionnaire'
        impersonation = check('POST', prefix + '/users/' + operator + '/impersonation', headers=admin)
        for cookie in impersonation.cookies.jar:
            if cookie.name == 'KEYCLOAK_SESSION':
                sid = cookie.value.rsplit('/', 1)[-1]
        try:
            return_to = '/company-ai/setup?request_id=00000000-0000-0000-0000-000000000000&user_code=ABCD-1234'
            anonymous_page = check('GET', return_to)
            login_path = unescape(re.search(r'<a id="login" href="([^"]+)"', anonymous_page.text).group(1))
            assert parse_qs(urlsplit(login_path).query)['return_to'] == [return_to]
            login = check('GET', login_path, 307)
            authorized = client.get(login.headers['location'])
            callback = urlsplit(authorized.headers.get('location', ''))
            if callback.hostname != 'changdar-server.mooo.com' or 'code' not in parse_qs(callback.query):
                raise RuntimeError('Unexpected production OIDC callback.')
            completed = check('GET', callback.path + '?' + callback.query, 303)
            assert completed.headers['location'] == return_to
            checks['anonymous_setup_login_returns_to_original_request_and_code'] = True
            cookie = completed.cookies.get('__Host-vn_session')
            if not cookie:
                raise RuntimeError('Missing secure production BFF cookie.')
            session_key = 'bff:session:' + hashlib.sha256(cookie.encode()).hexdigest()
            saved = json.loads(store.get(session_key))
            bearer = cipher.decrypt(saved['access_token_encrypted'].encode()).decode()
            claims = jwt.decode(bearer, options={'verify_signature': False})
            sid = claims.get('sid') or sid
            assert claims['sub'] == operator
            aud = claims['aud']
            assert 'visionnaire-api' in ([aud] if isinstance(aud, str) else aud)
            checks['real_production_oidc_pkce_session'] = True
            csrf = check('GET', '/bff/auth/csrf').json()['csrf_token']
            browser = {'Origin': ORIGIN, 'X-CSRF-Token': csrf, 'X-Company-AI-Action': '1'}
            direct_headers = {
                'Authorization': 'Bearer ' + bearer,
                'X-Company-AI-Network-Proxy': access['AI_ACCESS_NETWORK_PROXY_KEY'],
                'X-Company-AI-Client-IP': '127.0.0.1',
            }
            control = 'http://127.0.0.1:8013/api/company-ai' if args.direct else ORIGIN + PREFIX
            native = 'http://127.0.0.1:8013' if args.direct else ORIGIN

            def api(method, suffix, status=200, headers=None, **kw):
                return check(method, control + suffix, status, headers={**(direct_headers if args.direct else {}), **(headers or {})}, **kw)
            policy = api('GET', '/policy').json()
            assert policy['environment'] == 'production' and policy['models'] == access['AI_ACCESS_MODELS'].split(',')
            checks['production_policy_and_current_ai_membership'] = True
            if args.require_codex:
                assert policy['codex_enabled'] is True
                assert policy['codex_models'] == policy['models']
                assert policy['codex_release'] == access['AI_ACCESS_CODEX_RELEASE']
                checks['codex_enabled_and_shared_three_model_policy'] = True
                bootstrap = check('GET', native + '/company-ai/v1/codex/install-policy').json()
                assert bootstrap == {
                    'allowed': True, 'codex_enabled': True,
                    'codex_release': access['AI_ACCESS_CODEX_RELEASE'],
                }
                checks['codex_installation_policy_available_before_enrollment'] = True
            api('GET', '/admin/devices')
            checks['current_production_admin_role'] = True
            listing = api('GET', '/devices').json()
            assert all(not {'key', 'key_box', 'key_hash'}.intersection(row) for row in listing['devices'])
            checks['device_list_contains_no_credentials'] = True
            if not args.direct:
                with httpx.Client(timeout=20, trust_env=False) as anonymous:
                    assert anonymous.get(ORIGIN + PREFIX + '/policy').status_code == 401
                api(
                    'POST', '/devices/' + str(uuid4()) + '/revoke', 403,
                    headers={**browser, 'X-CSRF-Token': 'wrong'}, json={},
                )
                checks['anonymous_and_invalid_csrf_denied'] = True
            api(
                'POST', '/devices/' + str(uuid4()) + '/revoke', 403,
                headers={**browser, 'Origin': 'https://invalid.example'}, json={},
            )
            checks['cross_origin_mutation_denied'] = True
            verifier = secrets.token_urlsafe(48)
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
            enrollment = check(
                'POST', native + '/api/company-ai/device/start', json={
                    'device_name': 'PRODUCTION-DEPLOY-SMOKE-' + uuid4().hex[:8], 'code_challenge': challenge,
                },
            ).json()
            assert enrollment['verification_uri'].startswith(ORIGIN + '/company-ai/setup?')
            request_id = enrollment['request_id']
            if args.require_codex:
                assert enrollment['codex_enabled'] is True
                checks['native_start_reports_codex_readiness'] = True
            preview = api('GET', '/enrollments/' + request_id).json()
            assert preview['code'] == enrollment['user_code']
            proof = {'request_id': request_id, 'code_verifier': verifier}
            assert check('POST', native + '/api/company-ai/device/poll', json=proof).json()['status'] == 'pending'
            device = api(
                'POST', '/enrollments/' + request_id + '/approve', headers=browser,
                json={'confirm': True, 'user_code': enrollment['user_code']},
            ).json()['device_id']
            ready = check('POST', native + '/api/company-ai/device/poll', json=proof).json()
            assert ready['status'] == 'ready'
            raw_key = ready['key']
            assert ready['base_url'] == ORIGIN + '/company-ai'
            check('POST', native + '/api/company-ai/device/ack', json=proof)
            check('POST', native + '/api/company-ai/device/poll', 410, json=proof)
            checks['native_enrollment_approval_key_delivery_ack_replay_denial'] = True
            models = check(
                'GET', native + '/company-ai/v1/models',
                headers={'Authorization': 'Bearer ' + raw_key},
            ).json()
            assert [row['id'] for row in models['data']] == policy['models']
            check(
                'POST', native + '/company-ai/v1/messages', 403,
                headers={'Authorization': 'Bearer ' + raw_key}, json={'model': 'not-allowed'},
            )
            checks['device_model_catalog_and_model_allowlist'] = True
            if args.require_codex:
                config = check(
                    'GET', native + '/company-ai/v1/codex/config',
                    headers={'Authorization': 'Bearer ' + raw_key},
                ).json()
                assert config['models'] == policy['models']
                assert config['release'] == access['AI_ACCESS_CODEX_RELEASE']
                assert [item['slug'] for item in config['catalog']['models']] == policy['models']
                assert config['base_url'] == ORIGIN + '/company-ai/v1'
                check(
                    'POST', native + '/company-ai/v1/responses', 403,
                    headers={'Authorization': 'Bearer ' + raw_key}, json={'model': 'not-allowed', 'input': []},
                )
                check(
                    'POST', native + '/company-ai/v1/responses', 400,
                    headers={'Authorization': 'Bearer ' + raw_key}, json={
                        'model': policy['models'][0], 'input': [], 'tools': [{'type': 'web_search'}],
                    },
                )
                checks['codex_catalog_and_responses_scope'] = True
            if args.model_call:
                assert args.require_codex, '--model-call requires --require-codex'
                response = check(
                    'POST', native + '/company-ai/v1/responses',
                    headers={'Authorization': 'Bearer ' + raw_key}, json={
                        'model': 'gemini-3.5-flash-lite', 'input': 'Reply only CODEX_PRODUCTION_OK.',
                        'max_output_tokens': 32, 'store': False,
                    },
                ).json()
                assert response.get('object') == 'response'
                text = ' '.join(
                    str(part.get('text', '')) for item in response.get('output', [])
                    for part in item.get('content', []) if isinstance(part, dict)
                )
                assert 'CODEX_PRODUCTION_OK' in text
                checks['real_codex_responses_model_call'] = True
            api('POST', '/devices/' + device + '/revoke', headers=browser, json={})
            check('GET', native + '/company-ai/v1/models', 401, headers={'Authorization': 'Bearer ' + raw_key})
            checks['revoked_device_denied'] = True
            page = check('GET', native + '/company-ai/setup')
            assert 'data-api-prefix="' + PREFIX + '"' in page.text
            script = check('GET', native + '/company-ai/setup.js')
            assert 'X-CSRF-Token' in script.text
            download = check('GET', native + '/company-ai/downloads/Setup-CompanyAI.ps1')
            local = (args.source_root / 'examples/company_ai_access/windows/Setup-CompanyAI.ps1').read_bytes()
            assert download.content == local
            assert "[string]$Portal = 'https://changdar-server.mooo.com'" in download.text
            assert 'daemon_auto_start = false' in download.text
            checks['windows_setup_disables_daemon_auto_start_for_long_homes'] = True
            assert "[string]$Tools = 'Both'" in download.text
            assert 'Get-CompanyToolPlan $Tools $PSBoundParameters' in download.text
            assert 'Install-CompanyClaude $state $toolPlan.install_claude' in download.text
            assert '<option value="Both" selected disabled>' in page.text
            assert "tool === 'Both' ? ''" in script.text
            checks['default_both_tools_and_existing_installation_skips_shipped'] = True
            checks['bff_approval_page_and_production_windows_download'] = True
            if (args.source_root / 'examples/company_ai_access/windows/changdar-ai-setup.exe').is_file():
                assert policy['product_name'] == 'changdar-ai'
                assert policy['windows_installer_url'] == ORIGIN+'/company-ai/downloads/changdar-ai-setup.exe'
                assert 'changdar-ai 電腦設定與綁定' in page.text
                assert 'href="/company-ai/downloads/changdar-ai-setup.exe"' in page.text
                exe = check('GET', native+'/company-ai/downloads/changdar-ai-setup.exe')
                assert exe.content == (
                    args.source_root/'examples/company_ai_access/windows/changdar-ai-setup.exe'
                ).read_bytes()
                assert exe.content.startswith(b'MZ') and exe.headers['cache-control'] == 'no-store'
                assert exe.headers['content-disposition'] == 'attachment; filename="changdar-ai-setup.exe"'
                checks['changdar_ai_brand_policy_and_exact_exe_download'] = True
            if args.require_codex:
                for name in ('Get-CompanyAIKey.ps1', 'Start-CompanyCodex.ps1'):
                    downloaded = check('GET', native + '/company-ai/downloads/' + name)
                    assert downloaded.content == (
                        args.source_root / 'examples/company_ai_access/windows' / name
                    ).read_bytes()
                assert 'Assert-CompanyCodexAvailability $started' in download.text
                assert 'Company Codex: run company-codex' not in download.text
                checks['all_three_windows_downloads_match_release'] = True
            for address in ('203.0.113.25', '192.168.1.1'):
                check(
                    'GET', 'http://127.0.0.1:8013/company-ai/network-access', 403,
                    headers={'X-Forwarded-For': address},
                )
            check(
                'GET', 'http://127.0.0.1:8013/api/company-ai/policy', 403,
                headers={'Authorization': 'Bearer ' + bearer, 'X-Company-AI-Network-Proxy': 'spoofed'},
            )
            checks['public_router_and_spoofed_provenance_denied'] = True
        finally:
            if device:
                api('POST', '/devices/' + device + '/revoke', headers=browser, json={})
            if raw_key:
                llm = configuration['litellm']
                check(
                    'POST', 'http://127.0.0.1:4000/key/delete',
                    headers={'Authorization': 'Bearer ' + llm['LITELLM_MASTER_KEY']}, json={'keys': [raw_key]},
                )
            if session_key:
                store.delete(session_key)
            if sid:
                removed = client.delete(prefix + '/sessions/' + sid, headers=admin)
                assert removed.status_code in {204, 404}, 'Temporary SSO cleanup failed.'
            store.close()
    result = {
        'environment': 'production', 'mode': 'direct' if args.direct else 'public_bff',
        'at': datetime.now(timezone.utc).isoformat(), 'checks': checks,
        'model_calls': int(args.model_call), 'disposable_device_revoked_and_key_deleted': True,
    }
    args.output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    (
        args.output_dir / ('direct-validation.json' if args.direct else 'production-validation.json')
    ).write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # HTTP response bodies and credentials stay out of console/error output.
        raise SystemExit(
            'Production smoke failed: ' + type(error).__name__ +
            (': ' + str(error) if isinstance(error, (RuntimeError, AssertionError)) else ''),
        ) from None
