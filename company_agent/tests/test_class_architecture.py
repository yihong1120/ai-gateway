"""Refactor contracts and instance reuse; no remote services or paid models."""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import unittest
from functools import partial
from importlib.resources import files
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import Mock
from unittest.mock import patch

from langchain_core.messages import AIMessage
from langchain_core.tools import ToolException

from company_agent.agents.research.tools import research_tools
from company_agent.runtime.completion import completion_tool
from company_agent.services.files.access import FolderScan
from company_agent.services.office.spreadsheet_edit import XMLParts
from company_agent.tests import all_tools


class ClassArchitectureTests(unittest.TestCase):
    def test_model_visible_tool_contracts_match_supported_schema(self):
        baseline = json.loads(
            files('company_agent.tests')
            .joinpath('fixtures/tool_contracts.json')
            .read_text(encoding='utf-8'),
        )
        contracts = {}
        for tool in all_tools() + [
            completion_tool('coordinator'),
            completion_tool('document'),
        ]:
            data = {
                'name': tool.name,
                'description': tool.description,
                'schema': tool.tool_call_schema.model_json_schema(),
            }
            contracts[tool.name] = hashlib.sha256(
                json.dumps(data, sort_keys=True, ensure_ascii=False).encode(),
            ).hexdigest()
        self.assertEqual(contracts, baseline)

    def test_tool_entrypoints_are_named_and_have_no_closure_state(self):
        for tool in all_tools() + [
            completion_tool('coordinator'),
            completion_tool('document'),
        ]:
            with self.subTest(tool=tool.name):
                invoke = tool.coroutine or tool.func
                self.assertNotIn('<locals>', inspect.unwrap(invoke).__qualname__)
                self.assertIsNone(inspect.unwrap(invoke).__closure__)
                if isinstance(invoke, partial):
                    self.assertLessEqual(set(invoke.keywords), {'tool_name', 'input_schema', 'role', 'agent'})
                if hasattr(invoke, '__self__'):
                    self.assertLessEqual(
                        set(vars(invoke.__self__)),
                        {'name', 'schema', 'description', 'role'},
                    )

    def test_recursive_scan_and_xml_cache_are_operation_local(self):
        first, second = FolderScan(2), FolderScan(10)
        first.found.append({'path': 'alice.txt'})
        first.visited = 9
        first.incomplete = True
        self.assertEqual(
            (second.found, second.visited, second.incomplete), ([], 0, False),
        )
        a = XMLParts(Mock(read=Mock(return_value=b'<part user="alice"/>')))
        b = XMLParts(Mock(read=Mock(return_value=b'<part user="bob"/>')))
        a.read('part.xml').set('changed', 'yes')
        self.assertEqual(b.read('part.xml').get('user'), 'bob')
        self.assertIsNone(b.read('part.xml').get('changed'))
        self.assertIs(a.read('part.xml'), a.trees['part.xml'])
        a.archive.read.assert_called_once_with('part.xml')
        b.archive.read.assert_called_once_with('part.xml')


class AdapterReuseTests(unittest.IsolatedAsyncioTestCase):
    def runtime(self, owner):
        store = Mock()
        store.begin_tool.return_value = None
        store.files.return_value = [{'id': owner + '-file'}]
        ctx = SimpleNamespace(
            owner=owner,
            job=owner + '-job',
            store=store,
            settings=SimpleNamespace(enable_web_research=True),
        )
        return SimpleNamespace(
            context=ctx,
            state={'messages': []},
            tool_call_id=owner + '-call',
            stream_writer=Mock(),
        )

    async def test_one_research_adapter_handles_interleaved_users_without_leaking_state(
        self,
    ):
        tool = research_tools()[0]
        original = dict(tool.coroutine.keywords)
        runtimes = [self.runtime(owner) for owner in ('alice', 'bob')]
        barrier = asyncio.Event()
        entered = []

        async def perform(name, args, config):
            entered.append(config['owner'])
            if len(entered) == 2:
                barrier.set()
            await asyncio.wait_for(barrier.wait(), 2)
            return {'results': [{'title': config['owner'], 'content': args['query']}]}

        async def config(owner):
            return {'owner': owner}

        with (
            patch(
                'company_agent.agents.research.tools.web.web_config',
                new=AsyncMock(side_effect=config),
            ),
            patch(
                'company_agent.agents.research.tools.web.perform',
                new=AsyncMock(side_effect=perform),
            ),
        ):
            results = await asyncio.gather(
                *(
                    tool.coroutine(
                        runtime=runtime, query=runtime.context.owner + '-question',
                    )
                    for runtime in runtimes
                ),
            )
        for runtime, result in zip(runtimes, results):
            owner, job = runtime.context.owner, runtime.context.job
            receipt = result.update['messages'][0]
            self.assertEqual(receipt.tool_call_id, owner + '-call')
            self.assertEqual(
                json.loads(receipt.content)['results'],
                [{'title': owner, 'content': owner + '-question'}],
            )
            self.assertEqual(result.update['web_sources'], [{'title': owner}])
            runtime.context.store.audit.assert_called_once_with(
                owner, job, 'search_web', 'done',
            )
            self.assertEqual(
                runtime.context.store.finish_tool.call_args.args[:3],
                (owner, job, owner + '-call'),
            )
        self.assertEqual(tool.coroutine.keywords, original)

    async def test_reused_completion_adapter_rechecks_job_ownership(self):
        tool = completion_tool('document')
        alice, bob = self.runtime('alice'), self.runtime('bob')
        for runtime in (alice, bob):
            owner = runtime.context.owner
            runtime.context.settings.office_execution_mode = 'flexible'
            runtime.state['messages'] = [
                AIMessage(
                    content='', tool_calls=[
                        {'id': runtime.tool_call_id, 'name': 'complete_assignment', 'args': {}},
                    ],
                ),
            ]
            result = await tool.coroutine(
                runtime=runtime,
                outcome='completed',
                summary='已完成',
                artifact_ids=[owner + '-file'],
            )
            self.assertEqual(
                result.update['completion']['artifact_ids'], [owner + '-file'],
            )
            self.assertEqual(result.update['messages'][0].tool_call_id, owner + '-call')
            runtime.context.store.verify_artifacts.assert_called_once_with(
                owner, owner + '-job', [owner + '-file'],
            )
        with self.assertRaises(ToolException):
            await tool.coroutine(
                runtime=bob,
                outcome='completed',
                summary='已完成',
                artifact_ids=['alice-file'],
            )
        self.assertEqual(bob.context.store.audit.call_count, 1)
