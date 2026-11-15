"""Application-owned configuration, HTTP profiles and complete BFF deployment."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import httpx
from fastapi import Request
from jwt.exceptions import InvalidTokenError

from examples.shared.runtime.http_clients import HttpClientPool, get_application_http_client
from examples.identity.config import Settings
from examples.identity.runtime import IdentityResources, get_settings, identity_scope
from examples.bff.deployment import BUNDLE, MANIFEST, build, read_bundle
from examples.bff.proxy_headers import _proxy_request_headers, _proxy_response_headers
from examples.bff.proxy_responses import rewrite_response


class ApplicationIsolation(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(
            patch.dict(
                os.environ,
                {
                    'PYTHON_DOTENV_DISABLED': '1',
                    'JWT_SECRET_KEY': 'synthetic-bootstrap-key',
                },
                clear=True,
            )
        )

    async def test_concurrent_apps_keep_settings_clients_and_cookie_policies(self):
        from examples.db_management.app import create_app

        with patch.dict(os.environ, {'BFF_SESSION_COOKIE_NAME': 'one'}):
            first = create_app(Settings(authjwt_secret_key='one', app_public_url='https://one.test'))
        with patch.dict(os.environ, {'BFF_SESSION_COOKIE_NAME': 'two'}):
            second = create_app(Settings(authjwt_secret_key='two', app_public_url='https://two.test'))

        async def probe():
            before = get_settings()
            client = await get_application_http_client('bff-oidc')
            await asyncio.sleep(0.01)
            self.assertIs(get_settings(), before)
            self.assertIs(client, await get_application_http_client('bff-oidc'))
            return {'url': before.app_public_url, 'client': id(client)}

        first.add_api_route('/scope', probe)
        second.add_api_route('/scope', probe)
        try:
            async with (
                httpx.AsyncClient(transport=httpx.ASGITransport(app=first), base_url='https://one.test') as a,
                httpx.AsyncClient(transport=httpx.ASGITransport(app=second), base_url='https://two.test') as b,
            ):
                one, two = await asyncio.gather(a.get('/scope'), b.get('/scope'))
            self.assertEqual(one.json()['url'], 'https://one.test')
            self.assertEqual(two.json()['url'], 'https://two.test')
            self.assertNotEqual(one.json()['client'], two.json()['client'])
            self.assertEqual(first.state.identity.bff.cookie_name, 'one')
            self.assertEqual(second.state.identity.bff.cookie_name, 'two')
            self.assertIsNone(await get_application_http_client('bff-oidc'))
            with self.assertRaises(RuntimeError):
                get_settings()
        finally:
            await first.state.identity.http_clients.close()
            await second.state.identity.http_clients.close()

    async def test_named_profiles_reject_mismatch_and_unknown_names(self):
        options = {'sample': {'timeout': 5.0}}
        pool = HttpClientPool(options)
        options['sample']['timeout'] = 99
        try:
            client = await pool.get('sample')
            self.assertEqual(client.timeout.read, 5.0)
            with self.assertRaises(ValueError):
                await pool.get('sample', timeout=99)
            with self.assertRaises(KeyError):
                await pool.get('typo')
        finally:
            await pool.close()

    async def test_jwt_and_cipher_are_bound_to_the_application_snapshot(self):
        one = IdentityResources(Settings(authjwt_secret_key='a' * 32))
        two = IdentityResources(Settings(authjwt_secret_key='b' * 32))
        token = one.jwt_access.create_access_token(
            {'username': 'alice', 'user_id': 1, 'role': 'user', 'jti': 'a', 'features': []}
        )
        self.assertEqual(one.jwt_access.decode_token(token)['sub'], 'alice')
        with self.assertRaises(InvalidTokenError):
            two.jwt_access.decode_token(token)
        encrypted = one.session_cipher.encrypt(b'synthetic-refresh')
        with identity_scope(one):
            from examples.identity.sessions.store import _fernet

            self.assertEqual(_fernet().decrypt(encrypted), b'synthetic-refresh')
        self.assertIsNone(one.database.engine)
        self.assertIsNone(two.database.engine)
        await one.http_clients.close()
        await two.http_clients.close()

    async def test_settings_read_current_environment_once_and_are_immutable(self):
        with patch.dict(os.environ, {'APP_PUBLIC_URL': 'https://one.test'}):
            one = Settings()
        with patch.dict(os.environ, {'APP_PUBLIC_URL': 'https://two.test'}):
            two = Settings()
        self.assertEqual(one.app_public_url, 'https://one.test')
        self.assertEqual(two.app_public_url, 'https://two.test')
        with self.assertRaises(ValueError):
            one.app_public_url = 'changed'

    def test_importing_authentication_modules_does_not_read_configuration(self):
        code = """from unittest.mock import patch
with patch('dotenv.load_dotenv') as load:
 import examples.identity.config
 import examples.identity.sessions.jwt
 import examples.bff.oidc_services
 import examples.identity.sessions.authentication
 assert not load.called
"""
        result = subprocess.run(
            [sys.executable, '-c', code],
            capture_output=True,
            text=True,
            env={
                **os.environ,
                'JWT_SECRET_KEY': '',
                'PYTHONPATH': os.pathsep.join(str(Path(p).resolve()) for p in sys.path if p),
            },
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


class BffContracts(unittest.TestCase):
    def setUp(self):
        self.enterContext(
            patch.dict(
                os.environ,
                {
                    'JWT_SECRET_KEY': 'synthetic-key',
                    'PYTHON_DOTENV_DISABLED': '1',
                    'BFF_COMPANY_AI_NETWORK_PROXY_KEY': 'p' * 32,
                },
                clear=True,
            )
        )
        self.resources = IdentityResources(Settings())
        self.enterContext(identity_scope(self.resources))

    @staticmethod
    def request(path='/bff/files/versions/1/generate_temp_url'):
        return Request(
            {
                'type': 'http',
                'method': 'GET',
                'path': path,
                'query_string': b'',
                'scheme': 'https',
                'server': ('site.test', 443),
                'client': ('192.0.2.1', 123),
                'headers': [
                    (b'host', b'site.test'),
                    (b'cookie', b'browser=secret'),
                    (b'authorization', b'Bearer browser'),
                ],
            }
        )

    def test_proxy_headers_keep_credentials_server_scoped(self):
        headers = _proxy_request_headers(
            self.request('/bff/ai_access/api/company-ai/devices'),
            'server-token',
            upstream_url='http://127.0.0.1:8013/api/company-ai/devices',
        )
        self.assertNotIn('cookie', {name.lower() for name in headers})
        self.assertEqual(headers['Authorization'], 'Bearer server-token')
        self.assertEqual(headers['X-Company-AI-Client-IP'], '192.0.2.1')
        self.assertEqual(headers['X-Company-AI-Network-Proxy'], 'p' * 32)

    def test_file_capabilities_stay_behind_bff_and_changed_content_loses_etag(self):
        data, changed = rewrite_response(
            httpx.Response(200, content=b'{"temp_url":"/download/temp/a.b.c"}',
                           headers={'content-type': 'application/json'}),
            self.request(),
            None,
            'files',
            'versions/1/generate_temp_url',
        )
        self.assertTrue(changed)
        self.assertEqual(json.loads(data)['temp_url'], '/bff/files/download/temp/a.b.c')
        headers = _proxy_response_headers(
            httpx.Response(200, headers={'etag': 'old', 'content-type': 'application/json'}), content_rewritten=True
        )
        self.assertNotIn('etag', headers)
        self.assertEqual(headers['Cache-Control'], 'no-store')


class DeploymentBundle(unittest.TestCase):
    def test_reproducible_complete_bundle_and_tampering(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            one, two = root / 'one', root / 'two'
            first = build(one)
            self.assertEqual(first, build(two))
            self.assertEqual((one / BUNDLE).read_bytes(), (two / BUNDLE).read_bytes())
            manifest, files = read_bundle(one)
            for name in (
                'examples/shared/config.py',
                'examples/deployment/cli.py',
                'examples/bff/proxy_tokens.py',
                'examples/bff/proxy_streams.py',
                'examples/identity/runtime.py',
                'examples/db_management/app.py',
                'examples/deployment/__init__.py',
                'examples/deployment/registry/router.py',
                'examples/deployment/registry/service.py',
            ):
                self.assertIn(name, files)
            self.assertNotIn('examples/identity/models.py', files)
            self.assertNotIn('examples/configuration.py', files)
            self.assertFalse(any(name.startswith('examples/deployment_registry/') for name in files))
            self.assertFalse(any(name.startswith('examples/deployment/') and
                                 not name.startswith('examples/deployment/registry/') and
                                 name not in {'examples/deployment/__init__.py', 'examples/deployment/cli.py'} for name in files))
            self.assertEqual(manifest['obsolete_files'], ['examples/identity/models.py', 'examples/configuration.py'])
            verified = read_bundle(one)
            release = verified.stage(root / 'versions')
            self.assertEqual(verified.stage(root / 'versions'), release)
            self.assertTrue((release / 'examples/bff/serve.py').is_file())
            (release / 'examples/bff/proxy.py').write_bytes(b'changed')
            with self.assertRaises(ValueError):
                verified.stage(root / 'versions')
            data = (one / BUNDLE).read_bytes() + b'tamper'
            (one / BUNDLE).write_bytes(data)
            with self.assertRaises(ValueError):
                read_bundle(one)
            manifest['sha256'] = hashlib.sha256(data).hexdigest()
            manifest['bytes'] = len(data)
            manifest['files'][0]['name'] = '../escape.py'
            (one / MANIFEST).write_text(json.dumps(manifest))
            with self.assertRaises(ValueError):
                read_bundle(one)


class ReleaseEntrypoint(unittest.TestCase):
    def test_private_configuration_controls_loopback_server(self):
        from examples.bff.serve import main
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / 'config.json'
            config.write_text(json.dumps({'environment': {'BFF_TEST_RELEASE': 'selected'}, 'port': 8006}))
            config.chmod(0o600)
            with patch.object(sys, 'argv', ['serve', '--config', str(config)]), patch.dict(os.environ), \
                    patch.object(sys, 'dont_write_bytecode', False), patch('uvicorn.run') as run:
                main()
                self.assertEqual(os.environ['BFF_TEST_RELEASE'], 'selected')
                self.assertTrue(sys.dont_write_bytecode)
                run.assert_called_once_with(
                    'examples.db_management.app:app', host='127.0.0.1', port=8006, workers=1,
                    proxy_headers=True, forwarded_allow_ips='127.0.0.1', access_log=False)
                config.chmod(0o644)
                with self.assertRaises(RuntimeError):
                    main()
                self.assertEqual(run.call_count, 1)
