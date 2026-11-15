"""Verify launcher configuration before Uvicorn loads the WebUI application."""
from __future__ import annotations

import ast
import builtins
import importlib.util
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import ModuleType
from types import SimpleNamespace
from unittest.mock import Mock
from unittest.mock import patch

from fastapi import FastAPI

ROOT = Path(__file__).resolve().parents[1]
API = ROOT / 'examples/openwebui'


class ServeTests(unittest.TestCase):
    def setUp(self):
        self.webui = ModuleType('open_webui')
        self.webui.__file__ = '/opt/webui/open_webui/__init__.py'
        self.uvicorn = ModuleType('uvicorn')
        self.uvicorn.run = Mock()
        spec = importlib.util.spec_from_file_location(
            'eip_test_serve', API / 'serve.py',
        )
        self.launcher = importlib.util.module_from_spec(spec)
        self.enterContext(
            patch.dict(
                'sys.modules', {'open_webui': self.webui, 'uvicorn': self.uvicorn},
            ),
        )
        spec.loader.exec_module(self.launcher)
        self.enterContext(patch.object(self.launcher, 'load_service'))

    def test_existing_environment_is_ready_before_application_load(self):
        def run(target, **kwargs):
            self.assertEqual(target, 'examples.openwebui.app:create_app')
            self.assertTrue(kwargs['factory'])
            self.assertEqual(kwargs['workers'], 1)
            self.assertEqual(os.environ['FROM_INIT_PY'], 'true')
            self.assertEqual(os.environ['WEBUI_SECRET_KEY'], 'existing-key')
            self.assertEqual(os.environ['DATA_DIR'], '/persistent/data')
            self.assertFalse(kwargs['ws_per_message_deflate'])

        self.uvicorn.run.side_effect = run
        with patch.dict(
            os.environ,
            {
                'WEBUI_SECRET_KEY': 'existing-key',
                'DATA_DIR': '/persistent/data',
                'UVICORN_WS_PER_MESSAGE_DEFLATE': 'False',
            },
            clear=True,
        ):
            self.launcher.main()
        self.uvicorn.run.assert_called_once()

    def test_fallback_reuses_login_key_and_package_data_directory(self):
        with TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            (Path(directory) / '.webui_secret_key').write_text('saved-key')
            with patch.object(self.launcher.Path, 'cwd', return_value=Path(directory)):
                self.launcher.main()
            self.assertEqual(os.environ['WEBUI_SECRET_KEY'], 'saved-key')
            self.assertEqual(os.environ['DATA_DIR'], '/opt/webui/open_webui/data')
            self.assertTrue(self.uvicorn.run.call_args.kwargs['ws_per_message_deflate'])

    def test_missing_key_prevents_startup(self):
        with TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            with patch.object(self.launcher.Path, 'cwd', return_value=Path(directory)):
                with self.assertRaisesRegex(RuntimeError, 'WEBUI_SECRET_KEY'):
                    self.launcher.main()
            self.assertFalse((Path(directory) / '.webui_secret_key').exists())
        self.uvicorn.run.assert_not_called()

    def test_service_configuration_loads_before_first_host_import(self):

        events = []
        original_import = builtins.__import__

        def configured(service):
            self.assertEqual(service, 'webui')
            events.append('configuration')
            os.environ['WEBUI_SECRET_KEY'] = 'configured-key'

        def importing(name, *args, **kwargs):
            if name == 'open_webui':
                self.assertEqual(os.environ['PYTHON_DOTENV_DISABLED'], '1')
                self.assertEqual(events, ['configuration'])
                events.append('host')
                return self.webui
            return original_import(name, *args, **kwargs)

        self.launcher.load_service.side_effect = configured
        with patch.dict(os.environ, {}, clear=True), patch('builtins.__import__', side_effect=importing):
            self.launcher.main()
        self.assertEqual(events, ['configuration', 'host'])
        self.assertEqual(self.uvicorn.run.call_args.args[0], 'examples.openwebui.app:create_app')

    def test_factory_installs_both_routers_on_returned_application(self):
        original_lifespan = object()
        host_app = FastAPI()
        host_app.router.lifespan_context = original_lifespan
        host_app.router.routes[:] = ['spa']
        webui_main = ModuleType('open_webui.main')
        webui_main.app = host_app
        company_router = ModuleType('company_agent.api.router')
        company_router.install = Mock()
        status_ui = ModuleType('company_agent.api.status_ui')
        status_ui.install = Mock()
        resources = ModuleType('company_mcp.adapters.webui')
        resources.install = Mock()
        company_ai = ModuleType('examples.company_ai_access.adapters.webui')
        company_ai.install_webui = Mock()
        usage = ModuleType('examples.ai_usage.adapters.webui')
        usage.install_webui_usage = Mock()
        lifespan = ModuleType('examples.official_document_workflow.api.lifespan')
        lifespan.WorkflowLifespan = Mock(return_value='combined-lifespan')
        routes = ModuleType('examples.official_document_workflow.api.routes')
        routes.router = SimpleNamespace(routes=['eip'])
        spec = importlib.util.spec_from_file_location(
            'examples.openwebui.test_app', ROOT / 'examples/openwebui/app.py',
        )
        factory = importlib.util.module_from_spec(spec)
        with patch.dict(
            'sys.modules',
            {
                'open_webui.main': webui_main,
                'company_agent.api.router': company_router,
                'company_agent.api.status_ui': status_ui,
                'company_mcp.adapters.webui': resources,
                'examples.company_ai_access.adapters.webui': company_ai,
                'examples.ai_usage.adapters.webui': usage,
                'examples.official_document_workflow.api.lifespan': lifespan,
                'examples.official_document_workflow.api.routes': routes,
            },
        ), patch.dict(os.environ, {}, clear=True), patch('examples.shared.config.load_service'):
            spec.loader.exec_module(factory)
            self.assertIs(factory.create_app(), host_app)
            first_routes = list(host_app.router.routes)
            first_middleware = list(host_app.user_middleware)
            host_app.middleware_stack = object()  # Already installed must also work after startup.
            self.assertIs(factory.create_app(), host_app)
            self.assertEqual(host_app.router.routes, first_routes)
            self.assertEqual(host_app.user_middleware, first_middleware)
        self.assertEqual(host_app.router.lifespan_context, 'combined-lifespan')
        self.assertEqual(host_app.router.routes, ['eip', 'spa'])
        lifespan.WorkflowLifespan.assert_called_once_with(original_lifespan)
        company_router.install.assert_called_once_with(host_app)
        status_ui.install.assert_called_once_with(host_app)
        resources.install.assert_called_once_with(host_app)
        company_ai.install_webui.assert_called_once_with(host_app)
        usage.install_webui_usage.assert_called_once_with(host_app)

    def test_factory_has_only_top_level_imports(self):
        tree = ast.parse((API / 'app.py').read_text())
        imports = [
            node for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
        ]
        self.assertTrue(all(node in tree.body for node in imports))
