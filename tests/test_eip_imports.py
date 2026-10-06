"""Import ordering and module-scope imports across the document workflow."""
from __future__ import annotations

import ast
import subprocess
import sys
import unittest
from pathlib import Path


# Integration boundaries need cleanup/finally, native HTTP errors and lazy
# imports. Keep the original constraints everywhere else, including new files.
# Exceptions name exact enclosing functions rather than exempt whole modules.
BOUNDARIES = {
    'imports': {
        ('infrastructure/storage/processing_cache.py', 'cached_upload'),
        ('application/multi_agent.py', 'write_pdf_pages'),
        ('application/upload_batch.py', 'unique_uploads'),
        ('application/upload_batch.py', 'group_pdf'),
        ('application/background_upload.py', 'submit'),
        ('application/background_upload.py', 'execute'),
    },
    'try': {
        ('api/routes.py', 'BrowserPageRoute.get_route_handler.handler'),
        ('infrastructure/concurrency.py', 'OwnerSlots.slot'),
        ('infrastructure/concurrency.py', 'run_ocr'),
        ('infrastructure/storage/processing_cache.py', 'cached_upload'),
        ('application/automation.py', '_run_due'),
        ('application/automation.py', 'scheduler'),
        ('application/upload_batch.py', 'process_unique_uploads'),
        ('domain/calendar/preparation.py', 'preparation_rows'),
        ('application/background_upload.py', 'pipe_module'),
        ('application/background_upload.py', 'execute'),
        ('application/background_upload.py', 'execute.store'),
    },
    'isinstance': {
        ('infrastructure/concurrency.py', 'document_request.limited'),
        ('application/background_upload.py', 'submit'),
    },
    'nested': {
        ('api/routes.py', 'BrowserPageRoute.get_route_handler'),
        ('infrastructure/concurrency.py', 'document_request'),
        ('infrastructure/concurrency.py', 'model_request'),
        ('application/upload_batch.py', 'process_unique_uploads'),
        ('application/background_upload.py', 'execute'),
        ('application/background_upload.py', 'execute.store'),
    },
}


def permitted(tree, node, relative_path, rule):
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    names, parent = [], parents.get(node)
    while parent is not None:
        if isinstance(parent, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            names.insert(0, parent.name)
        parent = parents.get(parent)
    return (relative_path.as_posix(), '.'.join(names)) in BOUNDARIES[rule]


class ImportOrderTests(unittest.TestCase):
    def test_frontend_is_separate_from_fastapi_package(self):
        root = Path(__file__).resolve().parents[1] / 'official_document_workflow'
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
                    'n.startswith("official_document_workflow.application") for n in sys.modules)',
                    'official_document_workflow.infrastructure.eip.' + name,
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
                        'official_document_workflow.' + name,
                    ],
                    cwd=Path(__file__).resolve().parents[1],
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_all_workflow_imports_are_at_module_scope(self):
        root = Path(__file__).resolve().parents[1] / 'official_document_workflow'
        for path in root.rglob('*.py'):
            tree = ast.parse(path.read_text())
            nested = [
                node.lineno
                for node in ast.walk(tree)
                if isinstance(node, (ast.Import, ast.ImportFrom))
                and node not in tree.body
                and not permitted(tree, node, path.relative_to(root), 'imports')
            ]
            self.assertEqual(nested, [], str(path.relative_to(root)))

    def test_workflow_has_no_try_statements(self):
        root = Path(__file__).resolve().parents[1] / 'official_document_workflow'
        for path in root.rglob('*.py'):
            tree = ast.parse(path.read_text())
            statements = [
                node.lineno
                for node in ast.walk(tree)
                if isinstance(node, (ast.Try, ast.TryStar))
                and not permitted(tree, node, path.relative_to(root), 'try')
            ]
            self.assertEqual(statements, [], str(path.relative_to(root)))

    def test_workflow_has_no_isinstance_calls(self):
        root = Path(__file__).resolve().parents[1] / 'official_document_workflow'
        for path in root.rglob('*.py'):
            tree = ast.parse(path.read_text())
            calls = [
                node.lineno
                for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == 'isinstance'
                and not permitted(tree, node, path.relative_to(root), 'isinstance')
            ]
            self.assertEqual(calls, [], str(path.relative_to(root)))

    def test_workflow_has_no_custom_error_wrappers(self):
        root = Path(__file__).resolve().parents[1] / 'official_document_workflow'
        forbidden = {
            'EIPError',
            'EIPSessionExpired',
            'EIPRetryableError',
            'WorkflowPlanningError',
            'http_error',
            'route_error',
            'request_error',
            'SafeRoute',
            'SafeHandler',
            'HTTPException',
        }
        for path in root.rglob('*.py'):
            tree = ast.parse(path.read_text())
            names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
            definitions = {
                node.name
                for node in ast.walk(tree)
                if isinstance(
                    node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef),
                )
            }
            self.assertEqual(
                (names | definitions) & forbidden,
                {'HTTPException'} if path.relative_to(root).as_posix() == 'api/routes.py' else set(),
                str(path.relative_to(root)),
            )
            custom_errors = {
                node.name
                for node in ast.walk(tree)
                if isinstance(node, ast.ClassDef)
                and node.name.endswith(('Error', 'Exception'))
            }
            allowed_errors = {'DocumentInputError'} if path.relative_to(
                root,
            ).as_posix() == 'application/background_upload.py' else set()
            self.assertEqual(custom_errors, allowed_errors, str(path.relative_to(root)))

    def test_workflow_has_no_nested_function_definitions(self):
        root = Path(__file__).resolve().parents[1] / 'official_document_workflow'
        for path in root.rglob('*.py'):
            tree = ast.parse(path.read_text())
            parents = {
                child: node
                for node in ast.walk(tree)
                for child in ast.iter_child_nodes(node)
            }
            nested = []
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if permitted(tree, node, path.relative_to(root), 'nested'):
                    continue
                parent = parents.get(node)
                while parent is not None:
                    if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        nested.append((node.lineno, node.name))
                        break
                    parent = parents.get(parent)
            self.assertEqual(nested, [], str(path.relative_to(root)))
