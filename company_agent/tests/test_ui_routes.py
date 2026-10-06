"""Actual ASGI static routes; no real login, remote I/O or queue workers."""
from __future__ import annotations

import io
import sys
import unittest
from importlib.resources import files
from types import ModuleType
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import Mock
from unittest.mock import patch

import httpx
from fastapi import Depends
from fastapi import FastAPI
from fastapi import HTTPException


class UIRoutesTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Import the actual router without bootstrapping Open WebUI's database
        # or migrations. Authentication is an explicit dependency test double.
        auth = ModuleType('open_webui.utils.auth')
        def default_denied(): raise HTTPException(401, 'login required')

        def verified(user=Depends(default_denied)):
            if user.role not in {'user', 'admin'}:
                raise HTTPException(401, 'not active')
            return user
        auth.get_verified_user = verified
        for name in ('company_agent.api.router', 'company_agent.api.http', 'company_agent.api.identity'):
            sys.modules.pop(name, None)
        with patch.dict(sys.modules, {'open_webui.utils.auth': auth}):
            from company_agent.api.router import router
            self.routes_module = sys.modules['company_agent.api.router']
            self.identity_module = sys.modules['company_agent.api.identity']
        self.dependency = default_denied
        self.app = FastAPI()
        self.app.include_router(router)
        self.app.dependency_overrides[self.dependency] = lambda: SimpleNamespace(role='user', id='test')

    async def test_pages_and_assets_serve_package_bytes_with_security_headers(self):
        paths = {
            '/agents': 'web_dashboard.html', '/connections': 'connections.html', '/connections/': 'connections.html',
            '/agents/app.js': 'web_dashboard.js', '/agents/app.css': 'web_dashboard.css',
            '/connections/app.js': 'connections.js', '/connections/app.css': 'web_dashboard.css',
            '/agents/eip-document.js': 'eip_document.js',
            '/agents/eip-document.css': 'eip_document.css',
        }
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app, raise_app_exceptions=False), base_url='http://test') as client:
            for url, name in paths.items():
                with self.subTest(url=url):
                    response = await client.get(url)
                    self.assertEqual(response.status_code, 200, response.text[:100])
                    self.assertEqual(response.content, files('company_agent.ui').joinpath(name).read_bytes())
                    self.assertEqual(response.headers['cache-control'], 'no-store')
                    self.assertEqual(response.headers['x-content-type-options'], 'nosniff')
                    expected_type = 'text/html' if name.endswith(
                        '.html',
                    ) else 'text/css' if name.endswith('.css') else 'application/javascript'
                    self.assertEqual(response.headers['content-type'].split(';')[0], expected_type)
                    if name.endswith('.html'):
                        self.assertIn("frame-ancestors 'none'", response.headers['content-security-policy'])

            document = await client.get('/agents/eip/job/0123456789abcdef0123456789abcdef')
            self.assertEqual(document.status_code, 200)
            self.assertEqual(document.content, files('company_agent.ui').joinpath('eip_document.html').read_bytes())
            self.assertIn("frame-ancestors 'none'", document.headers['content-security-policy'])

    async def test_logged_out_user_cannot_read_static_assets(self):
        def denied(): raise HTTPException(401, 'login required')
        self.app.dependency_overrides[self.dependency] = denied
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app, raise_app_exceptions=False), base_url='http://test') as client:
            page = await client.get('/agents')
            self.assertEqual(page.status_code, 401)
            management = await client.get('/connections/users')
            self.assertEqual(management.status_code, 401)
            asset = await client.get('/agents/app.js')
            self.assertEqual(asset.status_code, 401)

    async def test_eip_document_page_reads_live_content_and_proxies_pdf(self):
        store = Mock()
        svc = SimpleNamespace(queue=SimpleNamespace(store=store))
        view = AsyncMock(
            return_value={
                'subject': '測試公文', 'date': '2026-09-11 15:14:19',
                'folder_name': '等待我處理的文件', 'due_date': '2026-09-18 23:59:59',
                'flow_number': '2609110013', 'status': '2/4',
                'fields': [{'label': '本文主旨', 'value': '真正內文'}],
                'attachments': [{
                    'name': '附件.pdf', 'url':
                    '/agents/eip/job/0123456789abcdef0123456789abcdef/attachments/0',
                }],
            },
        )
        download = AsyncMock(return_value=('附件.pdf', io.BytesIO(b'%PDF-1.7 test')))
        with patch.object(self.routes_module, 'service', return_value=svc), \
                patch('company_agent.services.eip.client.view_document', view), \
                patch('company_agent.services.eip.client.view_attachment', download):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app, raise_app_exceptions=False), base_url='http://test') as client:
                data = await client.get('/agents/eip/job/0123456789abcdef0123456789abcdef/data')
                self.assertEqual(data.status_code, 200, data.text)
                self.assertEqual(data.json()['fields'], [{'label': '本文主旨', 'value': '真正內文'}])
                self.assertEqual(
                    data.json()['attachments'][0]['url'],
                    '/agents/eip/job/0123456789abcdef0123456789abcdef/attachments/0',
                )
                pdf = await client.get('/agents/eip/job/0123456789abcdef0123456789abcdef/attachments/0')
                self.assertEqual(pdf.status_code, 200, pdf.text)
                self.assertEqual(pdf.content, b'%PDF-1.7 test')
                self.assertTrue(pdf.headers['content-disposition'].startswith('inline;'))
        view.assert_awaited_once_with(store, 'test', 'job', '0123456789abcdef0123456789abcdef')
        download.assert_awaited_once_with(store, 'test', 'job', '0123456789abcdef0123456789abcdef', 0)

    async def test_pending_user_is_rejected(self):
        self.app.dependency_overrides[self.dependency] = lambda: SimpleNamespace(role='pending', id='test')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app, raise_app_exceptions=False), base_url='http://test') as client:
            self.assertEqual((await client.get('/connections')).status_code, 401)

    async def test_identity_management_exposes_admin_links_only_to_admin(self):
        with patch.dict(
            'os.environ', {
                'OPENID_PROVIDER_URL':
                'https://sso.example/keycloak/realms/visionnaire/.well-known/openid-configuration',
            },
        ):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app, raise_app_exceptions=False), base_url='http://test') as client:
                regular = (await client.get('/connections/identity')).json()
                self.assertFalse(regular['admin'])
                self.assertEqual(regular['source_of_truth'], 'visionnaire')
                self.assertEqual(regular['conflict_policy'], 'reject_email_without_matching_oidc_sub')
                self.assertIsNone(regular['visionnaire_users_url'])
                self.assertIsNone(regular['openwebui_users_url'])
                self.assertTrue(regular['account_url'].startswith('https://sso.example/keycloak/'))
                self.app.dependency_overrides[self.dependency] = lambda: SimpleNamespace(role='admin', id='admin')
                admin = (await client.get('/connections/identity')).json()
                self.assertTrue(admin['admin'])
                self.assertEqual(admin['visionnaire_users_url'], '/connections/users')
                self.assertNotIn('admin_url', admin)
                self.assertEqual(admin['openwebui_users_url'], '/admin/users')

                management = await client.get('/connections/users', follow_redirects=False)
                self.assertEqual(management.status_code, 303)
                self.assertEqual(management.headers['location'], 'https://sso.example/users')

                upstream = AsyncMock()
                upstream.__aenter__.return_value = upstream
                upstream.__aexit__.return_value = None
                upstream.post.return_value = httpx.Response(
                    201, json={
                        'user': {'id': 27}, 'keycloak_subject': 'subject-27',
                    },
                )
                self.app.state.oauth_manager = SimpleNamespace(
                    get_oauth_token=AsyncMock(return_value={'access_token': 'oidc-token'}),
                )
                client.cookies.set('oauth_session_id', 'session-1')
                created = {'id': 'webui-27', 'email': 'new@example.com', 'role': 'user'}
                insert = AsyncMock(return_value=created)
                auths = ModuleType('open_webui.models.auths')
                auths.Auths = SimpleNamespace(insert_new_auth=insert)
                users = ModuleType('open_webui.models.users')
                users.Users = SimpleNamespace(
                    get_user_by_email=AsyncMock(return_value=None),
                )
                auth = ModuleType('open_webui.utils.auth')
                auth.get_current_user = self.dependency
                auth.get_password_hash = AsyncMock(return_value='random-hash')
                with patch.dict(
                    sys.modules, {
                        'open_webui.models.auths': auths,
                        'open_webui.models.users': users,
                        'open_webui.utils.auth': auth,
                    },
                ), patch.object(
                    self.identity_module.httpx,
                    'AsyncClient',
                    return_value=upstream,
                ):
                    response = await client.post(
                        '/api/v1/auths/add', json={
                            'name': '新使用者', 'email': 'NEW@example.com',
                            'password': 'initial-password', 'role': 'user',
                        },
                    )
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json(), created)
                self.assertIsNone(upstream.post.await_args.kwargs['json']['group_id'])
                self.assertEqual(
                    upstream.post.await_args.kwargs['json']['openwebui_role'],
                    'user',
                )
                self.assertFalse(
                    upstream.post.await_args.kwargs['json']['force_password_change'],
                )
                self.assertEqual(
                    insert.await_args.kwargs['oauth'],
                    {'oidc': {'sub': 'subject-27'}},
                )

    async def test_native_update_synchronizes_before_local_shadow(self):
        self.app.dependency_overrides[self.dependency] = lambda: SimpleNamespace(
            role='admin', id='admin',
        )
        target = SimpleNamespace(
            id='webui-27', email='old@example.com',
            oauth={'oidc': {'sub': 'subject-27'}},
        )
        updated = {'id': 'webui-27', 'email': 'new@example.com', 'role': 'pending'}
        users_table = SimpleNamespace(
            get_user_by_id=AsyncMock(return_value=target),
            update_user_by_id=AsyncMock(return_value=updated),
        )
        auths_table = SimpleNamespace(update_email_by_id=AsyncMock(return_value=True))
        upstream = AsyncMock()
        upstream.__aenter__.return_value = upstream
        upstream.__aexit__.return_value = None
        upstream.patch.return_value = httpx.Response(200, json={'message': 'ok'})
        self.app.state.oauth_manager = SimpleNamespace(
            get_oauth_token=AsyncMock(return_value={'access_token': 'oidc-token'}),
        )
        modules = {
            'open_webui.models.auths': ModuleType('open_webui.models.auths'),
            'open_webui.models.users': ModuleType('open_webui.models.users'),
        }
        modules['open_webui.models.auths'].Auths = auths_table
        modules['open_webui.models.users'].Users = users_table
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app, raise_app_exceptions=False), base_url='http://test',
            cookies={'oauth_session_id': 'session-1'},
        ) as client:
            with patch.dict(sys.modules, modules), patch.object(
                self.identity_module.httpx, 'AsyncClient', return_value=upstream,
            ):
                response = await client.post(
                    '/api/v1/users/webui-27/update', json={
                        'name': 'New Name', 'email': 'NEW@example.com',
                        'password': 'new-password', 'role': 'pending', 'group_id': 8,
                    },
                )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            upstream.patch.await_args.kwargs['json'], {
                'role': 'pending', 'password': 'new-password', 'group_id': 8,
                'name': 'New Name', 'email': 'new@example.com',
            },
        )
        auths_table.update_email_by_id.assert_awaited_once_with(
            'webui-27', 'new@example.com',
        )
        local_update = users_table.update_user_by_id.await_args.args[1]
        self.assertNotIn('password', local_update)
        self.assertEqual(local_update['role'], 'pending')

    async def test_management_requires_the_current_browser_oidc_session(self):
        manager = SimpleNamespace(
            get_oauth_token=AsyncMock(return_value=None),
        )
        request = SimpleNamespace(
            cookies={'oauth_session_id': 'expired-session'},
            app=SimpleNamespace(state=SimpleNamespace(oauth_manager=manager)),
        )
        with self.assertRaises(HTTPException) as raised:
            await self.identity_module.oidc_access_token(
                request,
                SimpleNamespace(id='admin'),
            )
        self.assertEqual(raised.exception.status_code, 401)
        manager.get_oauth_token.assert_awaited_once_with(
            'admin',
            'expired-session',
        )

        request.cookies = {}
        manager.get_oauth_token.reset_mock()
        with self.assertRaises(HTTPException) as missing:
            await self.identity_module.oidc_access_token(
                request,
                SimpleNamespace(id='admin'),
            )
        self.assertEqual(missing.exception.status_code, 401)
        manager.get_oauth_token.assert_not_awaited()

    async def test_native_password_change_verifies_keycloak_and_updates_identity_source(self):
        user = SimpleNamespace(
            role='user', id='webui-27',
            oauth={'oidc': {'sub': 'subject-27'}},
        )
        self.app.dependency_overrides[self.dependency] = lambda: user
        self.app.state.oauth_manager = SimpleNamespace(
            get_oauth_token=AsyncMock(return_value={'access_token': 'session-token'}),
        )
        upstream = AsyncMock()
        upstream.__aenter__.return_value = upstream
        upstream.__aexit__.return_value = None
        upstream.get.side_effect = [
            httpx.Response(
                200, json={
                    'sub': 'subject-27', 'preferred_username': 'alice',
                },
            ),
            httpx.Response(200, json={'sub': 'subject-27'}),
        ]
        upstream.post.return_value = httpx.Response(
            200, json={'access_token': 'verified-token'},
        )
        upstream.put.return_value = httpx.Response(200, json={'message': 'ok'})
        revoke = AsyncMock()
        auth = ModuleType('open_webui.utils.auth')
        auth.get_current_user = self.dependency
        auth.revoke_user_tokens = revoke
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app, raise_app_exceptions=False),
            base_url='http://test',
            cookies={'oauth_session_id': 'session-1'},
        ) as client:
            with patch.dict(
                'os.environ', {
                    'OPENID_PROVIDER_URL': (
                        'https://sso.example/realms/visionnaire/'
                        '.well-known/openid-configuration'
                    ),
                    'OAUTH_CLIENT_ID': 'open-webui',
                    'OAUTH_CLIENT_SECRET': 'client-secret',
                },
            ), patch.dict(
                sys.modules, {'open_webui.utils.auth': auth},
            ), patch.object(
                self.identity_module.httpx, 'AsyncClient', return_value=upstream,
            ):
                response = await client.post(
                    '/api/v1/auths/update/password',
                    json={'password': 'old-password', 'new_password': 'new-password'},
                )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json())
        self.assertEqual(upstream.post.await_args.kwargs['data']['username'], 'alice')
        self.assertEqual(upstream.post.await_args.kwargs['data']['password'], 'old-password')
        self.assertEqual(
            upstream.put.await_args.kwargs['json'],
            {'new_password': 'new-password'},
        )
        revoke.assert_awaited_once()

    async def test_native_password_change_rejects_wrong_current_password(self):
        user = SimpleNamespace(
            role='user', id='webui-27',
            oauth={'oidc': {'sub': 'subject-27'}},
        )
        self.app.dependency_overrides[self.dependency] = lambda: user
        self.app.state.oauth_manager = SimpleNamespace(
            get_oauth_token=AsyncMock(return_value={'access_token': 'session-token'}),
        )
        upstream = AsyncMock()
        upstream.__aenter__.return_value = upstream
        upstream.__aexit__.return_value = None
        upstream.get.return_value = httpx.Response(
            200, json={
                'sub': 'subject-27', 'preferred_username': 'alice',
            },
        )
        upstream.post.return_value = httpx.Response(
            400, json={'error': 'invalid_grant'},
        )
        auth = ModuleType('open_webui.utils.auth')
        auth.get_current_user = self.dependency
        auth.revoke_user_tokens = AsyncMock()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app, raise_app_exceptions=False),
            base_url='http://test',
            cookies={'oauth_session_id': 'session-1'},
        ) as client:
            with patch.dict(
                'os.environ', {
                    'OPENID_PROVIDER_URL': (
                        'https://sso.example/realms/visionnaire/'
                        '.well-known/openid-configuration'
                    ),
                    'OAUTH_CLIENT_ID': 'open-webui',
                    'OAUTH_CLIENT_SECRET': 'client-secret',
                },
            ), patch.dict(
                sys.modules, {'open_webui.utils.auth': auth},
            ), patch.object(
                self.identity_module.httpx, 'AsyncClient', return_value=upstream,
            ):
                response = await client.post(
                    '/api/v1/auths/update/password',
                    json={'password': 'wrong', 'new_password': 'new-password'},
                )
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()['detail'], '目前密碼不正確。')
        upstream.put.assert_not_awaited()

    async def test_native_delete_removes_canonical_identity_first(self):
        self.app.dependency_overrides[self.dependency] = lambda: SimpleNamespace(
            role='admin', id='admin',
        )
        target = SimpleNamespace(
            id='webui-27', oauth={'oidc': {'sub': 'subject-27'}},
        )
        auths_table = SimpleNamespace(delete_auth_by_id=AsyncMock(return_value=True))
        users_table = SimpleNamespace(get_user_by_id=AsyncMock(return_value=target))
        upstream = AsyncMock()
        upstream.__aenter__.return_value = upstream
        upstream.__aexit__.return_value = None
        upstream.delete.return_value = httpx.Response(200, json={'message': 'ok'})
        self.app.state.oauth_manager = SimpleNamespace(
            get_oauth_token=AsyncMock(return_value={'access_token': 'oidc-token'}),
        )
        modules = {
            'open_webui.models.auths': ModuleType('open_webui.models.auths'),
            'open_webui.models.users': ModuleType('open_webui.models.users'),
        }
        modules['open_webui.models.auths'].Auths = auths_table
        modules['open_webui.models.users'].Users = users_table
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app, raise_app_exceptions=False), base_url='http://test',
            cookies={'oauth_session_id': 'session-1'},
        ) as client:
            with patch.dict(sys.modules, modules), patch.object(
                self.identity_module.httpx, 'AsyncClient', return_value=upstream,
            ):
                response = await client.delete('/api/v1/users/webui-27')
        self.assertEqual(response.status_code, 200, response.text)
        upstream.delete.assert_awaited_once()
        auths_table.delete_auth_by_id.assert_awaited_once_with('webui-27')

    async def test_resources_are_not_a_catchall_or_filesystem_browser(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app, raise_app_exceptions=False), base_url='http://test') as client:
            for path in ('/agents/prompt.md', '/connections/missing.js', '/agents/%2e%2e/config.py'):
                self.assertEqual((await client.get(path)).status_code, 404)

    async def test_job_actions_use_verified_billing_and_list_of_source_jobs(self):
        key = '11111111-1111-4111-8111-111111111111'
        request_id = '22222222-2222-4222-8222-222222222222'
        queue = Mock()
        queue.get.return_value = {'chat': 'owned-chat', 'kind': 'company'}
        queue.submit.return_value = 'new-job'
        queue.public.return_value = {'status': 'queued'}
        model = SimpleNamespace(subject='verified-subject')
        svc = SimpleNamespace(queue=queue, model_factory=Mock(return_value=model))
        verified = {'id': 'test'}
        with patch.object(self.routes_module, 'service', return_value=svc), \
                patch.object(self.routes_module, 'verified_user', AsyncMock(return_value=verified)) as identity, \
                patch.object(self.routes_module, 'close_model', AsyncMock()) as close, \
                patch.dict('os.environ', {'EIP_WEBUI_ORIGIN': 'http://test'}):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self.app, raise_app_exceptions=False), base_url='http://test',
                headers={'origin': 'http://test', 'x-company-action': '1'},
            ) as client:
                for action, payload in (
                    ('resume', {}),
                    ('followup', {'request_id': request_id, 'instruction': '修改文件'}),
                ):
                    response = await client.post(f'/agents/jobs/{key}/{action}', json=payload)
                    self.assertEqual(response.status_code, 200, response.text)
            queue.resume.assert_called_once_with('test', 'verified-subject', key, None)
            queue.get.assert_called_once_with('test', key)
            queue.submit.assert_called_once_with(
                'test', 'verified-subject',
                'owned-chat', request_id, '修改文件', [], [key],
            )
            self.assertEqual(identity.await_count, 2)
            identity.assert_awaited_with('test')
            self.assertEqual(close.await_count, 2)
            close.assert_awaited_with(model)
            svc.model_factory.assert_called_with(verified)

    async def test_invalid_actions_do_not_create_billing_clients(self):
        with patch.object(self.routes_module, 'service') as svc, \
                patch.dict('os.environ', {'EIP_WEBUI_ORIGIN': 'http://test'}):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self.app, raise_app_exceptions=False), base_url='http://test',
                headers={'origin': 'http://test', 'x-company-action': '1'},
            ) as client:
                for action, payload in (
                    ('resume', {'owner': 'someone-else'}),
                    ('followup', {'request_id': 'invalid', 'instruction': '修改'}),
                ):
                    self.assertEqual((await client.post(f'/agents/jobs/job/{action}', json=payload)).status_code, 500)
                self.assertEqual(
                    (
                        await client.post(
                            '/agents/jobs/job/resume', json={},
                            headers={'origin': 'http://foreign'},
                        )
                    ).status_code, 403,
                )
            svc.assert_not_called()

    async def test_billing_client_closed_when_queue_rejects_action(self):
        queue = Mock()
        queue.resume.side_effect = ValueError('工作不存在或沒有權限。')
        model = SimpleNamespace(subject='verified-subject')
        with patch.object(self.routes_module, 'service', return_value=SimpleNamespace(queue=queue, model_factory=Mock(return_value=model))), \
                patch.object(self.routes_module, 'verified_user', AsyncMock(return_value={'id': 'test'})), \
                patch.object(self.routes_module, 'close_model', AsyncMock()) as close, \
                patch.dict('os.environ', {'EIP_WEBUI_ORIGIN': 'http://test'}):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self.app, raise_app_exceptions=False), base_url='http://test',
                headers={'origin': 'http://test', 'x-company-action': '1'},
            ) as client:
                response = await client.post('/agents/jobs/foreign-job/resume', json={})
                self.assertEqual(response.status_code, 500)
                self.assertEqual(response.text, 'Internal Server Error')
            close.assert_awaited_once_with(model)
