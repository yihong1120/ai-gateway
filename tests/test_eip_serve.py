"""Verify launcher configuration before Uvicorn loads the WebUI application."""
from __future__ import annotations

import ast
import importlib.util
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import ModuleType
from types import SimpleNamespace
from unittest.mock import Mock
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
API = ROOT / 'official_document_workflow/api'


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
        with patch.dict(
            'sys.modules', {'open_webui': self.webui, 'uvicorn': self.uvicorn},
        ):
            spec.loader.exec_module(self.launcher)

    def test_existing_environment_is_ready_before_application_load(self):
        def run(target, **kwargs):
            self.assertEqual(target, 'official_document_workflow.api.app:create_app')
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

    def test_factory_installs_both_routers_on_returned_application(self):
        original_lifespan = object()
        host_app = SimpleNamespace(
            router=SimpleNamespace(lifespan_context=original_lifespan, routes=['spa']),
        )
        webui_main = ModuleType('open_webui.main')
        webui_main.app = host_app
        company_router = ModuleType('company_agent.api.router')
        company_router.install = Mock()
        lifespan = ModuleType('official_document_workflow.api.lifespan')
        lifespan.WorkflowLifespan = Mock(return_value='combined-lifespan')
        routes = ModuleType('official_document_workflow.api.routes')
        routes.router = SimpleNamespace(routes=['eip'])
        spec = importlib.util.spec_from_file_location(
            'official_document_workflow.api.test_app', API / 'app.py',
        )
        factory = importlib.util.module_from_spec(spec)
        with patch.dict(
            'sys.modules',
            {
                'open_webui.main': webui_main,
                'company_agent.api.router': company_router,
                'official_document_workflow.api.lifespan': lifespan,
                'official_document_workflow.api.routes': routes,
            },
        ):
            spec.loader.exec_module(factory)
            self.assertIs(factory.create_app(), host_app)
        self.assertEqual(host_app.router.lifespan_context, 'combined-lifespan')
        self.assertEqual(host_app.router.routes, ['eip', 'spa'])
        lifespan.WorkflowLifespan.assert_called_once_with(original_lifespan)
        company_router.install.assert_called_once_with(host_app)

    def test_launcher_and_factory_have_only_top_level_imports(self):
        for filename in ('serve.py', 'app.py'):
            tree = ast.parse((API / filename).read_text())
            imports = [
                node
                for node in ast.walk(tree)
                if isinstance(node, (ast.Import, ast.ImportFrom))
            ]
            self.assertTrue(all(node in tree.body for node in imports), filename)
