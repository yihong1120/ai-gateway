"""Two HTTP components, canonical backend identity and restart/failure behavior."""
from __future__ import annotations

import asyncio
import os
import unittest
from contextlib import asynccontextmanager
from dataclasses import replace
from unittest.mock import Mock
from unittest.mock import patch

import httpx
from starlette.applications import Starlette
from starlette.routing import Route

from company_agent.tests.test_nas import NASFixture
from company_mcp.adapters.backend import endpoint
from company_mcp.app import create_app
from company_mcp.backend import ASSERTION_HEADER
from company_mcp.backend import PROTOCOL
from company_mcp.config import BACKEND_PATH
from company_mcp.config import PATH
from company_mcp.remote import BackendClient
from company_mcp.settings import Settings
from tests.company_mcp import test_resources as fixture


class AppRunner:
    def __init__(self, app):
        self.app = app
        self.started = asyncio.get_running_loop().create_future()
        self.stop = asyncio.Event()

    async def lifecycle(self):
        try:
            async with self.app.router.lifespan_context(self.app):
                self.started.set_result(None)
                await self.stop.wait()
        except BaseException as failure:
            if not self.started.done():
                self.started.set_exception(failure)
            raise


@asynccontextmanager
async def running(app):
    runner = AppRunner(app)
    task = asyncio.create_task(runner.lifecycle())
    try:
        await runner.started
        yield app
    finally:
        runner.stop.set()
        await task


class ServiceTests(unittest.IsolatedAsyncioTestCase, NASFixture):
    async def asyncSetUp(self):
        await fixture.CompanyMCPTests.asyncSetUp(self)
        fixture.OIDCTests.setUp(self)
        self.settings = Settings(backend_key=self.configuration.backend_key)
        self.edge_configuration = replace(self.configuration, introspection_client='', introspection_secret='')
        self.authority = Starlette(
            routes=[
                Route(
                    BACKEND_PATH,
                    endpoint(self.configuration, self.verifier, Mock(return_value=self.resources)), methods=['POST'],
                ),
            ],
        )
        self.edge = create_app(
            self.edge_configuration, self.settings,
            transport=httpx.ASGITransport(app=self.authority),
        )
        await self.enterAsyncContext(running(self.edge))
        self.client = await self.enterAsyncContext(
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self.edge), base_url=self.configuration.origin,
            ),
        )
        self.private_client = await self.enterAsyncContext(
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self.authority), base_url='http://127.0.0.1:8080',
            ),
        )

    async def asyncTearDown(self):
        await self.http.aclose()

    token = fixture.OIDCTests.token

    async def rpc(self, name, arguments=None):
        reply = await self.client.post(
            PATH,
            headers={'accept': 'application/json, text/event-stream', 'authorization': 'Bearer ' + self.token()},
            json={
                'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                'params': {'name': name, 'arguments': arguments or {}},
            },
        )
        self.assertEqual(reply.status_code, 200, reply.text)
        return reply.json()['result']

    async def authority_request(self, action, *, token=True, key=True, headers=None, **values):
        supplied = {'authorization': 'Bearer ' + self.configuration.backend_key} if key else {}
        if token:
            supplied[ASSERTION_HEADER] = self.token()
        return await self.private_client.post(
            BACKEND_PATH, headers={**supplied, **(headers or {})},
            json={'protocol': PROTOCOL, 'action': action, **values},
        )

    async def test_independent_edge_health_catalog_and_real_owned_nas_read(self):
        self.bind()
        self.assertEqual((await self.client.get('/healthz')).status_code, 200)
        self.assertEqual((await self.client.get('/readyz')).json(), {'status': 'ready'})
        listing = await self.rpc('connections_list')
        self.assertEqual(len(listing['structuredContent']['nas']), 1)
        workspace = (await self.rpc('workspace_prepare', {'request_id': 'split-one'}))['structuredContent']['workspace']
        roots = (await self.rpc('nas_roots', {'workspace': workspace}))['structuredContent']['roots']
        root = next(item['name'] for item in roots if item['label'] == 'home')
        result = await self.rpc('nas_read', {'workspace': workspace, 'root': root, 'path': 'a.txt'})
        self.assertIn('alice secret', str(result))
        self.assertFalse(self.dsm.sessions)

    async def test_workload_key_alone_and_wrong_resource_audience_never_authorize_a_user(self):
        self.assertEqual((await self.authority_request('execute', token=False, tool='connections_list')).status_code, 401)
        self.assertEqual((await self.authority_request('execute', key=False, tool='connections_list')).status_code, 403)
        bad = await self.authority_request(
            'execute', headers={ASSERTION_HEADER: self.token(aud='model-device-api')},
            tool='connections_list',
        )
        self.assertEqual(bad.status_code, 401)
        self.assertFalse(self.calls)  # Rejected before IdP introspection or resource access.

    async def test_no_owner_impersonation_browser_cookie_or_incompatible_protocol(self):
        for value in ({'owner': 'bob'}, {'protocol': 999}, {'subject': 'bob-sub'}):
            payload = {'protocol': PROTOCOL, 'action': 'execute', 'tool': 'connections_list', **value}
            reply = await self.private_client.post(
                BACKEND_PATH,
                headers={'authorization': 'Bearer ' + self.configuration.backend_key, ASSERTION_HEADER: self.token()},
                json=payload,
            )
            self.assertEqual(reply.status_code, 409)
        for headers in ({'cookie': 'synthetic-user-session'}, {'origin': self.configuration.origin}):
            self.assertEqual((await self.authority_request('execute', headers=headers, tool='connections_list')).status_code, 403)
        self.assertFalse(self.calls)

    async def revoke_result(self, *args):
        self.resources.repository.set_grant('alice', 'company-codex', False)
        return {'content': 'synthetic-private-content'}

    async def test_private_authority_suppresses_revoked_results_before_crossing_http(self):
        with patch.object(self.resources, 'execute', side_effect=self.revoke_result):
            reply = await self.authority_request('execute', tool='connections_list')
        self.assertEqual(reply.status_code, 409)
        self.assertEqual(reply.json()['code'], 'grant_revoked')
        self.assertNotIn('synthetic-private-content', reply.text)

    async def test_private_endpoint_rejects_non_loopback_and_oversized_arguments(self):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.authority, client=('192.0.2.3', 100)),
            base_url='http://127.0.0.1:8080',
        ) as outsider:
            reply = await outsider.post(
                BACKEND_PATH,
                headers={'authorization': 'Bearer ' + self.configuration.backend_key, ASSERTION_HEADER: self.token()},
                json={'protocol': PROTOCOL, 'action': 'authorize'},
            )
        self.assertEqual(reply.status_code, 403)
        reply = await self.private_client.post(
            BACKEND_PATH,
            headers={'authorization': 'Bearer ' + self.configuration.backend_key}, content=b'x' * (256 * 1024 + 1),
        )
        self.assertEqual(reply.status_code, 413)
        self.assertFalse(self.calls)

    def record_cookie(self, request):
        self.seen_cookies.append(request.headers.get('cookie'))
        return httpx.Response(200, json={'ok': True}, headers={'set-cookie': 'host-session=synthetic; Path=/'})

    async def test_backend_client_never_replays_webui_response_cookies(self):
        self.seen_cookies = []
        async with httpx.AsyncClient(transport=httpx.MockTransport(self.record_cookie)) as client:
            backend = BackendClient(self.edge_configuration, self.settings, client)
            await backend.request('health')
            await backend.request('health')
        self.assertEqual(self.seen_cookies, [None, None])

    async def test_restart_reuses_backend_workspace_without_an_edge_database_or_worker(self):
        first = await self.rpc('workspace_prepare', {'request_id': 'restart'})
        workspace = first['structuredContent']['workspace']
        next_edge = create_app(
            self.edge_configuration, self.settings,
            transport=httpx.ASGITransport(app=self.authority),
        )
        async with next_edge.router.lifespan_context(next_edge):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=next_edge),
                base_url=self.configuration.origin,
            ) as next_client:
                reply = await next_client.post(
                    PATH,
                    headers={
                        'accept': 'application/json, text/event-stream',
                        'authorization': 'Bearer ' + self.token(),
                    },
                    json={
                        'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
                        'params': {'name': 'workspace_prepare', 'arguments': {'request_id': 'restart'}},
                    },
                )
        self.assertEqual(reply.json()['result']['structuredContent']['workspace'], workspace)
        self.assertFalse(hasattr(next_edge.state, 'job_service'))
        self.assertFalse(hasattr(next_edge.state, 'workflow'))

    def offline_backend(self, request):
        self.offline_calls.append(request)
        raise httpx.ConnectError('synthetic-secret-from-network-exception', request=request)

    async def test_backend_offline_is_not_ready_and_never_retried_or_replaced(self):
        self.offline_calls = []
        edge = create_app(self.edge_configuration, self.settings, transport=httpx.MockTransport(self.offline_backend))
        async with edge.router.lifespan_context(edge):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=edge),
                base_url=self.configuration.origin,
            ) as client:
                self.assertEqual((await client.get('/healthz')).status_code, 200)
                self.assertEqual((await client.get('/readyz')).status_code, 503)
                reply = await client.post(
                    PATH,
                    headers={
                        'accept': 'application/json, text/event-stream',
                        'authorization': 'Bearer ' + self.token(),
                    },
                    json={'jsonrpc': '2.0', 'id': 3, 'method': 'tools/list', 'params': {}},
                )
                self.assertEqual(reply.status_code, 503)
                self.assertNotIn('synthetic-secret', reply.text)
        self.assertEqual(len(self.offline_calls), 2)  # One readiness probe and one failed request; no retry.


class StandaloneSettingsTests(unittest.TestCase):
    def test_disabled_factory_does_not_require_or_load_backend_credentials(self):
        with patch.dict(os.environ, {'COMPANY_MCP_ENABLED': 'false'}, clear=True), \
                patch('examples.shared.config.load_service'):
            with self.assertRaisesRegex(RuntimeError, 'disabled'):
                create_app()

    def test_fixed_private_url_key_and_edge_secret_boundary(self):
        config = fixture.configuration()
        with self.assertRaises(ValueError):
            create_app(config, Settings(config.backend_key))
        for url in (
            'https://other.example/internal/company-mcp',
            'http://127.0.0.1:8080/arbitrary', 'http://127.0.0.1:8080/internal/company-mcp?token=x',
        ):
            with self.assertRaises(ValueError):
                Settings(config.backend_key, backend_url=url)
        with self.assertRaises(ValueError):
            Settings('short-key')
