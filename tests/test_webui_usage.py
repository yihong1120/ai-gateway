"""WebUI consumes the shared ledger with app-owned settings and transport."""
from __future__ import annotations

import asyncio
import importlib.util
import os
import unittest
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi import FastAPI
from starlette.requests import Request

from examples.ai_usage.adapters.webui import (
    install_webui_usage,
    settings_from_environment,
)


class WebUIUsageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {'PYTHON_DOTENV_DISABLED': '1'}, clear=True))
        path = Path(__file__).resolve().parents[1] / 'examples/openwebui/functions/my_daily_usage_pipe.py'
        spec = importlib.util.spec_from_file_location('webui_usage_entry_test', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.pipe = module.Pipe()
        self.clients = []
        self.client_type = httpx.AsyncClient

    def transport(self, handler):
        def create(**options):
            client = self.client_type(**options, transport=httpx.MockTransport(handler))
            self.clients.append(client)
            return client
        return patch('examples.ai_usage.adapters.webui.httpx.AsyncClient', side_effect=create)

    def application(self, *, key='reader-only', base='http://usage.test', budget='4.00'):
        with patch.dict(os.environ, {
            'LITELLM_USAGE_READER_KEY': key,
            'LITELLM_USAGE_BASE_URL': base,
            'LITELLM_DAILY_BUDGET_USD': budget,
        }):
            app = FastAPI()
            install_webui_usage(app)
        return app

    async def query(self, app, *, subject='verified-subject'):
        return await self.pipe.pipe(
            {'user': 'forged-browser-subject'},
            {'oauth': {'oidc': {'sub': subject}}},
            Request({'type': 'http', 'app': app}),
        )

    async def test_shared_totals_and_scope_reach_the_dynamic_function(self):
        observed = []

        def handler(request):
            observed.append(request)
            return httpx.Response(200, json={'total_pages': 1, 'data': [
                {'end_user': 'verified-subject', 'model_group': 'cloud-model',
                 'total_tokens': 20, 'spend': '0.5', 'startTime': datetime.now(UTC).isoformat()},
                {'end_user': 'verified-subject', 'model_group': 'local-model',
                 'api_base': 'http://private-model.test', 'total_tokens': 10, 'spend': '0.25',
                 'startTime': datetime.now(UTC).isoformat()},
                {'end_user': 'another-subject', 'model_group': 'private-other-model', 'spend': '999'},
            ]})

        with patch.dict(os.environ, {'AI_USAGE_LOCAL_MODEL_NAMES': 'local-model'}):
            app = self.application()
        with self.transport(handler):
            async with app.router.lifespan_context(app):
                result = await self.query(app)
                response = await app.state.daily_usage.daily_usage('verified-subject')
        self.assertIn('US$0.750000 / US$4.00', result)
        self.assertIn('US$3.250000', result)
        self.assertIn('本地模型：1 次、10 tokens、US$0.250000', result)
        self.assertEqual(response.budget.used_usd, 0.75)
        self.assertNotIn('private-other-model', result)
        for request in observed:
            self.assertEqual(request.url.path, '/spend/logs/v2')
            self.assertEqual(request.url.params['end_user'], 'verified-subject')
            self.assertEqual(request.headers['authorization'], 'Bearer reader-only')
        for secret in ('verified-subject', 'forged-browser-subject', 'reader-only'):
            self.assertNotIn(secret, result)
        self.assertTrue(all(client.is_closed for client in self.clients))
        self.assertIsNone(app.state.daily_usage)

    async def test_concurrent_apps_keep_settings_clients_and_budgets(self):
        observed = []

        def handler(request):
            observed.append((request.url.host, request.headers['authorization']))
            return httpx.Response(200, json={'data': [], 'total_pages': 1})

        first = self.application(key='first-reader', base='http://first.test/', budget='1.00')
        second = self.application(key='second-reader', base='http://second.test/', budget='4.00')
        with self.transport(handler):
            async with first.router.lifespan_context(first), second.router.lifespan_context(second):
                first_reader = first.state.daily_usage
                install_webui_usage(first)
                self.assertIs(first.state.daily_usage, first_reader)
                one, two = await asyncio.gather(self.query(first), self.query(second))
        self.assertIn('/ US$1.00', one)
        self.assertIn('/ US$4.00', two)
        self.assertCountEqual(observed, [('first.test', 'Bearer first-reader'), ('second.test', 'Bearer second-reader')])
        self.assertEqual(len(self.clients), 2)
        self.assertTrue(all(client.is_closed for client in self.clients))

    async def test_missing_identity_or_reader_performs_no_http(self):
        app = self.application(key='')
        with self.transport(lambda request: self.fail('No usage request should be sent')) as create:
            async with app.router.lifespan_context(app):
                missing_identity = await self.pipe.pipe(
                    {'user': 'forged-subject'}, {'oauth': {}}, Request({'type': 'http', 'app': app}),
                )
                missing_reader = await self.query(app)
        self.assertIn('Keycloak', missing_identity)
        self.assertIn('尚未完成伺服器端設定', missing_reader)
        create.assert_not_called()

    async def test_host_startup_failure_closes_the_reader_and_clears_state(self):
        @asynccontextmanager
        async def original(current):
            if current.state.reject_startup:
                raise RuntimeError('startup stopped')
            yield

        app = FastAPI(lifespan=original)
        app.state.reject_startup = True
        with patch.dict(os.environ, {'LITELLM_USAGE_READER_KEY': 'read-only'}):
            install_webui_usage(app)
        with (
            self.transport(lambda request: self.fail('Startup must not send usage queries')),
            self.assertRaisesRegex(RuntimeError, 'startup stopped'),
        ):
            async with app.router.lifespan_context(app):
                self.fail('The host must fail startup')
        self.assertEqual(len(self.clients), 1)
        self.assertTrue(self.clients[0].is_closed)
        self.assertIsNone(app.state.daily_usage)

    async def test_incomplete_or_unavailable_records_never_show_a_remaining_balance(self):
        payloads = (
            httpx.Response(200, json={'data': [], 'total_pages': 11}),
            httpx.Response(200, json={'data': [], 'total_pages': 1, 'total_is_capped': True}),
            httpx.Response(200, json={'data': [{'model': 'missing-owner'}], 'total_pages': 1}),
            httpx.Response(200, json={'data': [{'end_user': 'verified-subject'}], 'total_pages': 1}),
            httpx.Response(503, json={'error': 'reader-only'}),
        )
        for response in payloads:
            with self.subTest(response=response):
                app = self.application()
                with self.transport(lambda request, selected=response: selected):
                    async with app.router.lifespan_context(app):
                        result = await self.query(app)
                self.assertNotIn('US$', result)
                self.assertNotIn('reader-only', result)
                self.assertNotIn('missing-owner', result)

    def test_reader_defaults_need_no_api_oidc_or_management_configuration(self):
        self.assertIsNone(settings_from_environment())
        with patch.dict(os.environ, {'LITELLM_USAGE_READER_KEY': 'read-only'}):
            settings = settings_from_environment()
        self.assertEqual(settings.litellm_base_url, 'http://127.0.0.1:4000')
        self.assertEqual(str(settings.daily_budget_usd), '4.00')
        self.assertEqual(settings.max_log_pages, 10)
        self.assertNotIn('read-only', repr(settings))
