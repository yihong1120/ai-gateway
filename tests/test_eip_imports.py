"""Import ordering and module-scope imports across the document workflow."""
from __future__ import annotations

import ast
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from examples.official_document_workflow.services import version


class ImportOrderTests(unittest.TestCase):
    def test_frontend_is_separate_from_fastapi_package(self):
        root = Path(__file__).resolve().parents[1] / 'examples/official_document_workflow'
        api = root / 'api'
        frontend = root / 'frontend'
        self.assertFalse(any(api.glob('*.html')))
        self.assertFalse(any(api.glob('*.js')))
        self.assertFalse(any(api.glob('*.css')))
        self.assertTrue((frontend / 'index.html').is_file())
        self.assertTrue((frontend / 'assets' / 'app.js').is_file())
        self.assertTrue((frontend / 'assets' / 'app.css').is_file())

    def test_csv_and_calendar_connectors_do_not_load_application_or_webui(self):
        for name in ('calendar_csv', 'calendar_sync', 'calendar_identity'):
            result = subprocess.run(
                [
                    sys.executable,
                    '-c',
                    'import importlib,sys; importlib.import_module(sys.argv[1]); '
                    'assert not any(n.startswith("open_webui") or '
                    'n.startswith("examples.official_document_workflow.application") for n in sys.modules)',
                    'examples.official_document_workflow.infrastructure.eip.' + name,
                ],
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_each_dependency_entry_point_imports_in_a_fresh_process(self):
        for name in (
            'application.integration',
            'application.automation',
            'infrastructure.eip.calendar_identity',
            'infrastructure.eip.calendar_sync',
            'infrastructure.eip.netask',
            'infrastructure.eip.calendar_read',
            'infrastructure.eip.calendar_repair',
            'infrastructure.office.office_input',
        ):
            with self.subTest(module=name):
                result = subprocess.run(
                    [
                        sys.executable,
                        '-c',
                        'import runpy, sys; '
                        'runpy.run_path("tests/eip_webui.py")["load_application_modules"](sys.argv[1]); '
                        'assert "open_webui.env" not in sys.modules',
                        'examples.official_document_workflow.' + name,
                    ],
                    cwd=Path(__file__).resolve().parents[1],
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_domain_dependencies_remain_framework_independent(self):
        root = Path(__file__).resolve().parents[1] / 'examples/official_document_workflow/domain'
        for path in root.rglob('*.py'):
            tree = ast.parse(path.read_text())
            imports = [node.module or '' for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
            for name in imports:
                self.assertFalse(
                    name.startswith((
                        'examples.official_document_workflow.application',
                        'examples.official_document_workflow.services',
                        'company_agent', 'open_webui',
                    )), str(path),
                )

    def test_workflow_does_not_import_company_agent(self):
        root = Path(__file__).resolve().parents[1] / 'examples/official_document_workflow'
        for path in root.rglob('*.py'):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    self.assertFalse((node.module or '').startswith('company_agent'), str(path))

    def test_shared_runtime_does_not_import_product_services(self):
        root = Path(__file__).resolve().parents[1] / 'examples/shared/runtime'
        self.assertTrue(root.is_dir())
        for path in root.glob('*.py'):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.ImportFrom):
                    self.assertFalse(
                        (node.module or '').startswith((
                            'company_agent', 'examples.official_document_workflow',
                            'examples.company_ai_access',
                        )), str(path),
                    )

    def test_core_does_not_load_the_webui_adapter_source(self):
        root = Path(__file__).resolve().parents[1] / 'examples/official_document_workflow/services'
        for path in root.glob('*.py'):
            source = path.read_text()
            self.assertNotIn('spec_from_file_location', source)
            self.assertNotIn('openwebui/functions', source)

    def test_application_uses_public_document_operations(self):
        root = Path(__file__).resolve().parents[1] / 'examples/official_document_workflow'
        private = {
            node.name for path in (root / 'services').glob('*.py')
            for node in ast.walk(ast.parse(path.read_text()))
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name.startswith('_') and not node.name.startswith('__')
        }
        for path in (root / 'application').glob('*.py'):
            calls = {
                node.func.attr for node in ast.walk(ast.parse(path.read_text()))
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            }
            self.assertFalse(private & calls, str(path))


class WorkflowRevisionTests(unittest.TestCase):
    def test_shared_runtime_changes_invalidate_revision(self):

        with TemporaryDirectory() as directory:
            root = Path(directory)
            workflow = root / 'workflow'
            (workflow / 'services').mkdir(parents=True)
            implementation = workflow / 'services/version.py'
            implementation.write_text('# workflow')
            runtime = root / 'runtime'
            runtime.mkdir()
            (runtime / '__init__.py').write_text('# runtime')
            source = runtime / 'database.py'
            source.write_text('# first')
            with patch.object(version, '__file__', str(implementation)), patch.object(
                version.runtime, '__file__', str(runtime / '__init__.py'),
            ):
                first = version.implementation_revision.__wrapped__()
                source.write_text('# changed')
                second = version.implementation_revision.__wrapped__()
            self.assertNotEqual(first, second)
