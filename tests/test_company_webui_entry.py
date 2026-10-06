"""WebUI's deployed Function source must use the canonical implementation."""
from __future__ import annotations

import ast
import importlib.util
import unittest
from pathlib import Path
from unittest.mock import AsyncMock
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


class WebUIEntryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        path = ROOT / 'openwebui/functions/company_agent_pipe.py'
        spec = importlib.util.spec_from_file_location('company_entry_test', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.pipe = module.Pipe()

    async def test_forwards_context_to_canonical_pipe(self):
        body, user, metadata = {'messages': []}, {'id': 'alice'}, {'chat_id': 'chat'}
        emitter = AsyncMock()
        with patch(
            'company_agent.api.pipe.run_pipe', new=AsyncMock(return_value='ready'),
        ) as run:
            result = await self.pipe.pipe(body, user, metadata, emitter, 'task')
        self.assertEqual(result, 'ready')
        run.assert_awaited_once_with(body, user, metadata, emitter, 'task')

    async def test_native_errors_propagate_unchanged(self):
        for failure in (ValueError('請登入'), RuntimeError('provider failure')):
            with patch('company_agent.api.pipe.run_pipe', new=AsyncMock(side_effect=failure)) as run:
                with self.assertRaises(type(failure)) as raised:
                    await self.pipe.pipe({})
                self.assertIs(raised.exception, failure)
                run.assert_awaited_once()

    def test_launcher_installs_canonical_router(self):
        source = (ROOT / 'official_document_workflow/api/app.py').read_text()
        imports = [
            node.module
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.ImportFrom)
        ]
        self.assertIn('company_agent.api.router', imports)
        self.assertNotIn('company_agent.web_router', imports)

    def test_relocated_deployment_reads_the_actual_function_template(self):
        from company_agent.ops.deploy_webui import function_source

        self.assertEqual(
            function_source(),
            (ROOT / 'openwebui/functions/company_agent_pipe.py').read_text(
                encoding='utf-8',
            ),
        )

    async def test_missing_function_template_fails_before_deployment(self):
        from company_agent.ops.deploy_webui import deploy

        with patch(
            'company_agent.ops.deploy_webui.function_source',
            side_effect=FileNotFoundError('template'),
        ):
            with patch.dict('os.environ', {}, clear=True):
                with self.assertRaises(FileNotFoundError):
                    await deploy()
                import os

                self.assertNotIn('FROM_INIT_PY', os.environ)
