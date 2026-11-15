"""Browser-only approval cannot be replaced by an MCP token or foreign owner."""
from __future__ import annotations

import importlib
import os
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from fastapi import FastAPI, HTTPException, Request
import httpx

from company_agent.tests.test_nas import NASFixture
from tests.company_mcp import test_resources as fixture
from company_mcp.storage import ResourceError


async def authenticate(request: Request):
    name = request.headers.get('x-test-user')
    if name not in {'alice', 'bob', 'pending', 'local'}:
        raise HTTPException(401, 'Login required')
    return SimpleNamespace(id=name, role='pending' if name == 'pending' else 'user',
        oauth={} if name == 'local' else {'oidc': {'sub': name + '-sub'}})

class BrowserMCPTests(unittest.IsolatedAsyncioTestCase, NASFixture):
    invoke = fixture.CompanyMCPTests.invoke
    workspace = fixture.CompanyMCPTests.workspace

    async def asyncSetUp(self):
        await fixture.CompanyMCPTests.asyncSetUp(self)

        module = ModuleType('open_webui.utils.auth')
        module.get_verified_user = authenticate
        sys.modules.pop('company_mcp.browser', None)
        with patch.dict(sys.modules, {'open_webui.utils.auth': module}):
            routes = importlib.import_module('company_mcp.browser')
        self.enterContext(patch.dict(sys.modules, {
            'open_webui.utils.auth': module,
            'company_mcp.browser': routes,
        }))
        self.enterContext(patch.dict(os.environ, {'EIP_WEBUI_ORIGIN': self.configuration.origin}))
        self.app = FastAPI()
        self.app.include_router(routes.router)
        self.app.state.company_mcp = self.configuration, Mock(return_value=self.resources)
        self.client = await self.enterAsyncContext(httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url=self.configuration.origin,
        ))
        self.headers = {'x-test-user': 'alice', 'origin': self.configuration.origin, 'x-company-action': '1'}

    async def test_same_origin_grant_control_and_bearer_self_approval_denied(self):
        key = await self.workspace()
        self.assertEqual((await self.client.get('/agents/mcp/status')).status_code, 401)
        self.assertEqual((await self.client.get('/agents/mcp/status', headers={'x-test-user': 'local'})).status_code, 403)
        for headers in ({'x-test-user': 'alice'}, {**self.headers, 'authorization': 'Bearer synthetic-resource-token'}):
            reply = await self.client.post('/agents/mcp/access', headers=headers,
                json={'client_id': self.alice.client, 'enabled': False})
            self.assertEqual(reply.status_code, 403)
        reply = await self.client.post('/agents/mcp/access', headers=self.headers,
            json={'client_id': self.alice.client, 'enabled': False})
        self.assertEqual(reply.status_code, 200)
        with self.assertRaises(ResourceError):
            self.resources.repository.workspace(self.alice, key)

    async def test_paid_confirmation_requires_personal_browser_and_does_not_duplicate_queue(self):
        key = await self.workspace()
        docs = await self.invoke('eip_query', workspace=key, folder='inbox', start='2026-11-01', end='2026-11-30')
        attachments = await self.invoke('eip_attachments', workspace=key, handle=docs['documents'][0]['handle'])
        ready = await self.invoke('document_analysis_prepare', workspace=key, request_id='browser-one',
                                  handles=[attachments['attachments'][0]['handle']])
        path = '/agents/mcp/operations/' + ready['operation']
        self.assertEqual((await self.client.get(path, headers={'x-test-user': 'bob'})).status_code, 404)
        self.assertEqual((await self.client.post(path + '/start', headers=self.headers, json={'confirm': False})).status_code, 422)
        self.assertEqual((await self.client.post(path + '/start', headers={**self.headers, 'authorization': 'Bearer synthetic'}, json={'confirm': True})).status_code, 403)
        self.assertEqual(self.queue.list('alice'), [])
        first = await self.client.post(path + '/start', headers=self.headers, json={'confirm': True})
        second = await self.client.post(path + '/start', headers=self.headers, json={'confirm': True})
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json()['job'], second.json()['job'])
        with self.store.db() as db:
            db.execute('UPDATE mcp_workspaces SET expires=0 WHERE id=%s', (key,))
        old = await self.client.post(path + '/start', headers=self.headers, json={'confirm': True})
        self.assertEqual(first.json()['job'], old.json()['job'])
        self.assertEqual(len(self.queue.list('alice')), 1)
        self.assertEqual(self.queue.chat_pending(), [])

    async def test_confirmation_page_has_no_inline_scripts_or_secrets(self):
        reply = await self.client.get('/agents/mcp', headers={'x-test-user': 'alice'})
        self.assertEqual(reply.status_code, 200)
        self.assertIn("default-src 'none'", reply.headers['content-security-policy'])
        self.assertIn('no-store', reply.headers['cache-control'])
        self.assertNotIn('synthetic-eip-password', reply.text)
