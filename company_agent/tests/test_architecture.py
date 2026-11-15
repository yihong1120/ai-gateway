"""Graph behavior and role boundaries (no services required)."""
from __future__ import annotations

import subprocess
import sys
import unittest
from importlib.resources import files
from pathlib import Path
from unittest.mock import patch

from langgraph.checkpoint.memory import InMemorySaver

from company_agent.agents.delegation import delegation_tools
from company_agent.agents.registry import PROMPT
from company_agent.agents.registry import ROLE_PROMPTS
from company_agent.agents.registry import ROLE_TOOLS
from company_agent.agents.registry import SPECS
from company_agent.graph import build_graph
from company_agent.runtime.execution import DocumentAgent
from company_agent.state import GRAPH_VERSION
from company_agent.state import merge_roots
from company_agent.state import merge_sources
from company_agent.state import State


class ArchitectureTests(unittest.TestCase):
    def test_importing_model_definitions_does_not_initialize_application_limits(self):
        root = Path(__file__).resolve().parents[2]
        code = (
            'import sys; sys.path.insert(0, ' + repr(str(root)) + '); '
            'import company_agent.runtime.models; '
            'assert "examples.shared.runtime.concurrency" not in sys.modules'
        )
        subprocess.run([sys.executable, '-I', '-c', code], cwd='/tmp', check=True, capture_output=True)

    def test_runtime_and_graph_have_one_canonical_implementation(self):
        self.assertEqual(DocumentAgent.__module__, 'company_agent.runtime.execution')
        self.assertEqual(build_graph.__module__, 'company_agent.graph')
        self.assertEqual(State.__module__, 'company_agent.state')

    def test_registry_is_immutable(self):
        with self.assertRaises(TypeError):
            ROLE_TOOLS['document'] = frozenset()
        with self.assertRaises(TypeError):
            ROLE_PROMPTS['document'] = 'changed'
        with self.assertRaises(AttributeError):
            SPECS['document'].tools.add('delete')
        with self.assertRaises(AttributeError):
            SPECS['document'].name = 'other'

    def test_all_roles_have_package_resource_prompts(self):
        for role, spec in SPECS.items():
            with self.subTest(role=role):
                self.assertEqual(spec.name, role)
                resource = files(f"company_agent.agents.{role}").joinpath('prompt.md')
                self.assertTrue(resource.is_file())
                self.assertEqual(
                    resource.read_text(encoding='utf-8').rstrip('\n'), ROLE_PROMPTS[role],
                )
                self.assertTrue(ROLE_PROMPTS[role].strip())
        self.assertEqual(
            files('company_agent.agents').joinpath('common.md').read_text(encoding='utf-8'),
            PROMPT,
        )

    def test_delegations_match_registered_specialists(self):
        expected = {'delegate_' + name for name in SPECS if name != 'coordinator'}
        with patch('company_agent.graph.delegation_tools', wraps=delegation_tools) as construct:
            build_graph(None, InMemorySaver())
        construct.assert_called_once()
        specialists = construct.call_args.args[0]
        self.assertEqual(set(specialists), set(SPECS) - {'coordinator'})
        for role, agent in specialists.items():
            self.assertEqual(agent.name, role)
            self.assertIn('tools', agent.get_graph().nodes)
        self.assertEqual({tool.name for tool in delegation_tools(specialists)}, expected)
        self.assertEqual(
            ROLE_TOOLS['coordinator'], expected | {
                'plan_delivery', 'supersede_artifact', 'validate_delivery',
            },
        )

    def test_unregistered_tool_in_role_definition_fails_at_graph_construction(self):
        with patch('company_agent.graph.ROLE_TOOLS', {'document': {'misspelled_tool'}}):
            with self.assertRaisesRegex(KeyError, 'misspelled_tool'):
                build_graph(None, InMemorySaver())

    def test_native_factory_owns_the_loop_and_uses_new_checkpoint_version(self):
        self.assertEqual(GRAPH_VERSION, 31)
        for typed in (False, True):
            graph = build_graph(None, InMemorySaver(), typed).get_graph()
            self.assertEqual(
                set(graph.nodes), {
                    '__start__', '__end__', 'model', 'tools',
                    'CompanyPolicy.before_agent', 'CompanyPolicy.before_model',
                },
            )
        self.assertFalse({
            'read_repair', 'read_repairs_used', 'used_call_ids',
            'eip_pending_calls', 'agent_visits', 'decision', 'active_role', 'assignment_id',
        } & State.__annotations__.keys())

    def test_source_reducers_preserve_identity_and_latest_version(self):
        old = {'root': 'project', 'path': 'a.txt', 'sha256': 'old'}
        new = {**old, 'sha256': 'new'}
        other = {**old, 'root': 'shared'}
        self.assertEqual(merge_sources([old, other], [new]), [new, other])
        self.assertEqual(merge_roots(['shared', 'project'], ['project']), ['project', 'shared'])
