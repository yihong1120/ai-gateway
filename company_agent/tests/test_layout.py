"""Layer boundaries and per-agent construction, without production services."""
from __future__ import annotations

import ast
import json
import unittest
from importlib.resources import files

from langgraph.checkpoint.memory import InMemorySaver

from company_agent.agents.registry import SPECS
from company_agent.config import Settings
from company_agent.graph import build_graph
from company_agent.runtime.storage import Store
from company_agent.tests import all_tools


class LayoutTests(unittest.TestCase):
    def test_root_contains_only_core_source_and_readme(self):
        names = {p.name for p in files('company_agent').iterdir() if p.is_file()}
        # VCS ignore files are not part of wheels.
        self.assertEqual(
            names - {'.gitignore'},
            {'README.md', '__init__.py', 'graph.py', 'state.py', 'config.py', 'schemas.py'},
        )

    def test_classified_resources_are_packaged_and_config_examples_validate(self):
        root = files('company_agent')
        for name in ('config.example.json', 'config.economy.example.json'):
            settings = Settings.model_validate(json.loads(root.joinpath('configs', name).read_text(encoding='utf-8')))
            self.assertTrue(settings.require_user_billing)
        self.assertTrue(root.joinpath('evals', 'README.md').read_bytes())

    def test_runtime_does_not_import_maintenance_evaluation_or_tests(self):
        def sources(directory):
            for path in directory.iterdir():
                if path.is_dir() and path.name != '__pycache__':
                    yield from sources(path)
                elif path.name.endswith('.py'):
                    yield path
        root = files('company_agent')
        paths = [root.joinpath(name) for name in ('graph.py', 'state.py', 'config.py', 'schemas.py')]
        for directory in ('agents', 'services', 'runtime', 'api'):
            paths.extend(sources(root.joinpath(directory)))
        forbidden = ('company_agent.ops', 'company_agent.evals', 'company_agent.tests')
        for path in paths:
            for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
                names = ([node.module or ''] if isinstance(node, ast.ImportFrom)
                         else [n.name for n in node.names] if isinstance(node, ast.Import) else [])
                for name in names:
                    self.assertFalse(name.startswith(forbidden), (str(path), name))

    def test_retired_root_modules_are_absent(self):
        retired = {
            'filesystem', 'nas', 'tools', 'documents', 'spreadsheet_edit', 'office', 'office_worker', 'ocr', 'worker', 'cad', 'research', 'eip_tools', 'eip_reading', 'eip_processing', 'eip_calendar', 'calendar_tools',
            'analysis_tools', 'review_tools', 'artifact_edit', 'context', 'langgraph_tools', 'model', 'storage', 'checkpoints', 'budget', 'read_repair', 'progress', 'completion', 'web_service', 'web_router', 'web_pipe', 'agent', 'contracts',
        }
        root = files('company_agent')
        for name in retired:
            self.assertFalse(root.joinpath(name + '.py').is_file(), name)

    def test_every_role_has_entry_prompt_and_no_schema_reexport(self):
        available = all_tools()
        for role, spec in SPECS.items():
            with self.subTest(role=role):
                root = files('company_agent.agents.' + role)
                for name in ('prompt.md', '__init__.py'):
                    self.assertTrue(root.joinpath(name).is_file())
                self.assertFalse(root.joinpath('schemas.py').is_file())
                self.assertEqual(
                    root.joinpath('tools.py').is_file(),
                    role in {'eip', 'research', 'reviewer'},
                )
                self.assertFalse(root.joinpath('agent.py').is_file())
                self.assertLessEqual(spec.tools, {tool.name for tool in available})

    def test_retired_runtime_layers_are_absent(self):
        root = files('company_agent.runtime')
        for name in (
            'agents', 'tool_catalog', 'tool_adapter', 'tool_registry',
            'policies', 'read_repair', 'capabilities', 'progress', 'history', 'persistence',
        ):
            self.assertFalse(root.joinpath(name + '.py').is_file(), name)
        self.assertTrue(files('company_agent.api').joinpath('progress.py').is_file())

    def test_ops_has_one_deployment_and_one_read_only_check(self):
        root = files('company_agent.ops')
        self.assertEqual(
            {p.name for p in root.iterdir() if p.name.endswith('.py')},
            {'__init__.py', 'deploy_webui.py', 'smoke_webui.py'},
        )

    def test_native_factory_defers_model_binding_until_role_invocation(self):
        class Model:
            def bind_tools(self, tools, **options):
                raise AssertionError('graph construction must not bind unused roles')
        graph = build_graph(Model(), InMemorySaver(), True)
        self.assertIn('model', graph.get_graph().nodes)

    def test_services_do_not_import_agent_or_graph_dispatch(self):
        for domain in ('files', 'office', 'cad', 'research', 'eip'):
            for path in files('company_agent.services.' + domain).iterdir():
                if not path.name.endswith('.py'):
                    continue
                for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
                    names = ([node.module or ''] if isinstance(node, ast.ImportFrom)
                             else [n.name for n in node.names] if isinstance(node, ast.Import) else [])
                    for name in names:
                        self.assertFalse(
                            name.startswith(('company_agent.agents', 'company_agent.api')),
                            (str(path), name),
                        )
                        # Only the files tool boundary imports LangGraph; operations stay independent.
                        if not (domain == 'files' and path.name == 'tools.py'):
                            self.assertFalse(name.startswith('langgraph'), (str(path), name))

    def test_config_and_persistence_have_canonical_implementations(self):
        self.assertEqual(Settings.__module__, 'company_agent.config')
        self.assertEqual(Store.__module__, 'company_agent.runtime.storage')

    def test_ui_resources_are_in_their_own_package(self):
        root = files('company_agent.ui')
        for name in (
            'web_dashboard.html', 'web_dashboard.js', 'web_dashboard.css',
            'connections.html', 'connections.js',
        ):
            self.assertTrue(root.joinpath(name).read_bytes())

    def test_injected_context_is_not_a_tool_argument(self):
        for tool in all_tools():
            schema = tool.tool_call_schema.model_json_schema()
            self.assertNotIn('runtime', schema.get('properties', {}), tool.name)
            self.assertNotIn('owner', schema.get('properties', {}), tool.name)
