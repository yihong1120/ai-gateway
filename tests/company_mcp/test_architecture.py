"""Host-independent transport, explicit composition and integration lifetimes."""
from __future__ import annotations

import ast
import asyncio
import importlib
import subprocess
import sys
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import ModuleType
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import Mock
from unittest.mock import patch

import httpx
from fastapi import FastAPI

from company_agent.api.lifespan import ServiceLifespan
from company_agent.api.service import Service
from company_mcp.adapters.webui import install
from company_mcp.config import BACKEND_PATH
from company_mcp.config import METADATA_PATH
from company_mcp.config import PATH
from tests.company_mcp.test_resources import configuration

ROOT = Path(__file__).resolve().parents[2]


class RecordingContext:
    def __init__(self, events, name, *, case=None, fail=False):
        self.events, self.name, self.case, self.fail = events, name, case, fail

    @asynccontextmanager
    async def __call__(self, app, service=None):
        if self.fail:
            raise ValueError('synthetic-startup-failure')
        self.events.append(self.name + '-start')
        if self.case is not None:
            self.case.assertIs(app.state.job_service, service)
        try:
            yield
        finally:
            self.events.append(self.name + '-stop')


class RecordingWorker:
    def __init__(self, events):
        self.events = events

    async def loop(self):
        self.events.append('worker-start')
        try:
            await asyncio.Future()
        finally:
            self.events.append('worker-stop')


class DefinitionVisitor(ast.NodeVisitor):
    def __init__(self):
        self.depth = 0
        self.violations = []

    def visit_FunctionDef(self, node):
        if self.depth:
            self.violations.append((node.lineno, node.name))
        self.depth += 1
        self.generic_visit(node)
        self.depth -= 1

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Lambda(self, node):
        self.violations.append((node.lineno, '<lambda>'))


async def inactive_user():
    return None


class Boundaries(unittest.TestCase):
    def test_mcp_has_no_nested_function_or_lambda_definitions(self):
        paths = [
            *sorted((ROOT / 'company_mcp').rglob('*.py')),
            *sorted((ROOT / 'tests/company_mcp').glob('*.py')),
            ROOT / 'examples/shared/runtime/browser.py',
        ]
        for path in paths:
            visitor = DefinitionVisitor()
            visitor.visit(ast.parse(path.read_text()))
            self.assertFalse(visitor.violations, f'{path}: {visitor.violations}')
        tree = ast.parse((ROOT / 'company_agent/api/service.py').read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name in {'register_job_handler', 'unregister_job_handler'}:
                visitor = DefinitionVisitor()
                visitor.visit(node)
                self.assertFalse(visitor.violations, visitor.violations)

    def test_agent_never_imports_resource_server_and_transport_imports_without_host(self):
        for path in (ROOT / 'company_agent').rglob('*.py'):
            if 'tests' in path.parts:
                continue
            imports = [
                node.module for node in ast.walk(ast.parse(path.read_text()))
                if isinstance(node, ast.ImportFrom) and node.module
            ]
            self.assertFalse(any(name.startswith('company_mcp') for name in imports), str(path))
        code = (
            'import sys; sys.path.insert(0, ' + repr(str(ROOT)) + '); '
            'import company_mcp.server; '
            'assert not any(n.startswith(("company_agent", "open_webui")) for n in sys.modules)'
        )
        subprocess.run([sys.executable, '-I', '-c', code], cwd='/tmp', check=True, capture_output=True)
        self.assertFalse((ROOT / 'company_agent/mcp').exists())


class Composition(unittest.IsolatedAsyncioTestCase):
    async def test_registered_external_handler_is_explicit_and_cannot_replace_builtin(self):
        service = Service(None, None, None, None)
        handler = AsyncMock()
        remove = service.register_job_handler('resource_analysis', handler)
        row = {'kind': 'resource_analysis'}
        await service.execute(row)
        handler.assert_awaited_once_with(service, row)
        for kind in ('company', 'document', 'resource_analysis'):
            with self.assertRaises(ValueError):
                service.register_job_handler(kind, handler)
        remove()
        with self.assertRaises(ValueError):
            await service.execute(row)
        self.assertEqual(handler.await_count, 1)

    async def test_integrations_start_before_worker_and_stop_after_worker(self):
        events = []
        host = RecordingContext(events, 'host')
        integration = RecordingContext(events, 'integration', case=self)
        service = SimpleNamespace(loop=RecordingWorker(events).loop, mcp_servers=())
        app = FastAPI()
        app.state.job_integrations = (integration,)
        async with ServiceLifespan(host, Mock(return_value=service)).run(app):
            await asyncio.sleep(0)
        self.assertEqual(
            events, [
                'host-start', 'integration-start', 'worker-start',
                'worker-stop', 'integration-stop', 'host-stop',
            ],
        )
        self.assertFalse(hasattr(app.state, 'job_service'))

    async def test_partial_integration_failure_closes_previous_integrations_without_worker(self):
        events = []
        host = RecordingContext([], 'host')
        first = RecordingContext(events, 'first')
        broken = RecordingContext(events, 'broken', fail=True)
        service = SimpleNamespace(loop=AsyncMock(), mcp_servers=())
        app = FastAPI()
        app.state.job_integrations = (first, broken)
        with self.assertRaises(ValueError):
            async with ServiceLifespan(host, Mock(return_value=service)).run(app):
                self.fail('Startup must fail before accepting work.')
        service.loop.assert_not_awaited()
        self.assertEqual(events, ['first-start', 'first-stop'])
        self.assertFalse(hasattr(app.state, 'job_service'))

    async def test_host_install_is_idempotent_and_resource_lifetime_is_explicit(self):
        module = ModuleType('open_webui.utils.auth')
        module.get_verified_user = inactive_user
        sys.modules.pop('company_mcp.browser', None)
        with patch.dict(sys.modules, {'open_webui.utils.auth': module}):
            importlib.import_module('company_mcp.browser')
            app = FastAPI()
            with patch('company_mcp.config.Configuration.from_environment', return_value=configuration()):
                install(app)
                install(app)
            self.assertEqual(len(app.state.job_integrations), 1)
            browser_routes = list(app.routes)
            service = Service(None, None, None, None)
            async with app.state.job_integrations[0](app, service):
                self.assertIn('mcp_analysis', service.job_handlers)
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url=configuration().origin,
                ) as client:
                    reply = await client.post(BACKEND_PATH, json={'action': 'authorize'})
                self.assertEqual(reply.status_code, 403)
                self.assertIn(BACKEND_PATH, [route.path for route in app.routes])
                self.assertNotIn(PATH, [route.path for route in app.routes])
                self.assertNotIn(METADATA_PATH, [route.path for route in app.routes])
            self.assertEqual(app.routes, browser_routes)
            self.assertFalse(service.job_handlers)
            self.assertFalse(hasattr(app.state, 'company_mcp'))
