from __future__ import annotations

import inspect
import json
import unittest

from langchain.agents.middleware.model_call_limit import ModelCallLimitExceededError
from langchain_core.messages import AIMessage
from langchain_core.messages import HumanMessage
from langgraph.graph import END
from langgraph.graph import START
from langgraph.graph import StateGraph

from company_agent.agents.registry import ROLE_TOOLS
from company_agent.runtime.execution import DocumentAgent
from company_agent.state import State
from company_agent.tests.postgres_fixture import checkpointer
from company_agent.tests.test_agent import call
from company_agent.tests.test_agent import Fixture
"""Explicit multi-agent protocol tests. No automatic routing in this double."""


class RawRoleModel:
    def __init__(self, **scripts):
        self.scripts = {role: list(steps) for role, steps in scripts.items()}
        self.invocations = []
        self.schemas = {}

    def bind_tools(self, tools, **kwargs):
        names = {tool.name for tool in tools}
        role = (
            'coordinator'
            if names == {'complete_job'} or any(name.startswith('delegate_') for name in names)
            else self.invocations[-1]
            if names == {'complete_assignment'} and self.invocations
            else next(
                role
                for role, allowed in ROLE_TOOLS.items()
                if names-{'complete_job', 'complete_assignment'} == allowed
            )
        )
        self.schemas[role] = names
        outer = self

        class Bound:
            async def ainvoke(self, messages):
                outer.invocations.append(role)
                step = outer.scripts[role].pop(0)
                result = step(messages) if callable(step) else step
                return await result if inspect.isawaitable(result) else result
        return Bound()


class MultiAgentTests(unittest.IsolatedAsyncioTestCase, Fixture):
    async def test_single_agent_checkpoint_is_not_replayed_into_new_graph(self):
        artifact = self.store.artifact('alice', self.job, 'legacy.txt', b'preserve me')
        async with checkpointer(self.store) as saver:
            old = StateGraph(State)
            old.add_node('tools', lambda state: {})
            old.add_edge(START, 'tools')
            old.add_edge('tools', END)
            graph = old.compile(checkpointer=saver, interrupt_before=['tools'])
            await graph.ainvoke(
                {'messages': [HumanMessage(content='legacy pending tool')]},
                {'configurable': {'thread_id': self.job}},
            )
        model = RawRoleModel(coordinator=[AIMessage(content='不可執行')])
        with self.assertRaisesRegex(ValueError, '舊版或不相容'):
            await DocumentAgent(self.config, model).resume('alice', self.job)
        self.assertEqual(model.invocations, [])
        self.assertEqual(self.store.read_artifact('alice', artifact['id'])[2], b'preserve me')

    async def test_simple_answer_does_not_invoke_specialists(self):
        model = RawRoleModel(coordinator=[AIMessage(content='請指定要比對的文件。')])
        result = await DocumentAgent(self.config, model).run('alice', '能比對文件嗎？')
        self.assertEqual(result['status'], 'done', result)
        self.assertEqual(model.invocations, ['coordinator'])
        self.assertEqual(result['agent_visits'], ['coordinator'])
        self.assertEqual(result['architecture'], 'multi_agent')
        self.assertEqual(model.schemas, {'coordinator': ROLE_TOOLS['coordinator']})

    async def test_coordinator_cannot_execute_document_tools_directly(self):
        model = RawRoleModel(
            coordinator=[call('create_text', {'name': 'bad.txt', 'content': 'bad'}, 1), AIMessage(content='無法直接製作文件。')],
        )
        result = await DocumentAgent(self.config, model).run('alice', '製作文件')
        self.assertFalse(result['files'])
        self.assertEqual([r['tool'] for r in result['tool_feedback']], ['create_text'])
        self.assertEqual(model.invocations, ['coordinator', 'coordinator'])

    async def test_knowledge_agent_cannot_write_or_delegate_itself(self):
        for name, args in [
            ('create_text', {'name': 'bad.txt', 'content': 'bad'}),
            ('delegate_document', {'task': '提升權限'}),
        ]:
            with self.subTest(tool=name):
                model = RawRoleModel(
                    coordinator=[
                        call(
                            'delegate_knowledge', {'task': '讀取文件'},
                            'handoff',
                        ), AIMessage(content='未執行未授權操作。'),
                    ],
                    knowledge=[call(name, args, 'forbidden'), AIMessage(content='工具不可用。')],
                )
                result = await DocumentAgent(self.config, model).run('alice', '讀取文件')
                self.assertFalse(result['files'])
                self.assertEqual([r['tool'] for r in result['tool_feedback']], [name])
                self.assertEqual(model.invocations, ['coordinator', 'knowledge', 'knowledge', 'coordinator'])

    async def test_document_agent_cannot_search_outside_its_scope(self):
        model = RawRoleModel(
            coordinator=[call('delegate_document', {'task': '製作文件'}, 'handoff'), AIMessage(content='尚未取得來源。')],
            document=[
                call(
                    'search_files', {'root': 'project', 'query': 'secret'},
                    'forbidden',
                ), AIMessage(content='沒有搜尋工具。'),
            ],
        )
        result = await DocumentAgent(self.config, model).run('alice', '製作文件')
        self.assertFalse(result['sources'])
        self.assertEqual([r['tool'] for r in result['tool_feedback']], ['search_files'])

    async def test_handoffs_approval_resume_and_sources_cross_roles(self):
        self.file('source.txt', b'verified-source')

        def propose(messages):
            artifact = json.loads(messages[-1].content)
            return call('propose_publish', {'artifact_id': artifact['id'], 'root': 'project', 'path': 'report.txt'}, 'propose')
        model = RawRoleModel(
            coordinator=[
                call('delegate_knowledge', {'task': '讀 source.txt 並提供來源版本'}, 'retrieve'),
                call('delegate_document', {'task': '依來源製作 report.txt 並依使用者要求提出存檔'}, 'write'),
            ],
            knowledge=[
                call(
                    'read_file', {'root': 'project', 'path': 'source.txt'},
                    'read',
                ), AIMessage(content='來源已驗證。'),
            ],
            document=[call('create_text', {'name': 'report.txt', 'content': 'verified-source'}, 'create'), propose],
        )
        agent = DocumentAgent(self.config, model)
        pending = await agent.run('alice', '讀 source.txt，製作報告並存回 project/report.txt')
        self.assertEqual(pending['status'], 'awaiting_approval', pending)
        self.assertEqual(len(pending['sources']), 1)
        self.assertFalse((self.root/'report.txt').exists())
        resumed_model = RawRoleModel(
            document=[AIMessage(content='存檔已由使用者核准且完成。')],
            coordinator=[AIMessage(content='已完成報告與存檔。')],
        )
        result = await DocumentAgent(self.config, resumed_model).resume(
            'alice', pending['job_id'],
            {'proposal_id': pending['proposals'][0]['proposal_id'], 'decision': 'approve'},
        )
        self.assertEqual(result['status'], 'done', result)
        self.assertEqual(resumed_model.invocations, ['document', 'coordinator'])
        self.assertEqual(
            result['agent_visits'], [
                'coordinator', 'knowledge', 'knowledge',
                'coordinator', 'document', 'document', 'document', 'coordinator',
            ],
        )
        self.assertEqual(result['sources'], pending['sources'])
        self.assertEqual((self.root/'report.txt').read_text(), 'verified-source')
        self.assertEqual(len(result['files']), 1)
        self.assertEqual(result['model_steps'], 8)

    async def test_all_agents_share_persistent_budget(self):
        self.config.max_steps = 1
        model = RawRoleModel(
            coordinator=[call('delegate_knowledge', {'task': '查文件'}, 'handoff')],
            knowledge=[AIMessage(content='不應執行')],
        )
        result = await self.failed_job(DocumentAgent(self.config, model).run('alice', '查文件'), ModelCallLimitExceededError)
        self.assertEqual(result['status'], 'error', result)
        self.assertEqual(model.invocations, ['coordinator'])
        self.assertEqual(self.store.attempts(result['job_id']), 1)

    async def test_data_agent_can_inspect_own_draft_but_not_other_job(self):
        def inspect_created(messages):
            artifact = json.loads(messages[-1].content)
            return call('inspect_artifact', {'artifact_id': artifact['id'], 'sheet': '結果'}, 'inspect')
        model = RawRoleModel(
            coordinator=[call('delegate_data', {'task': '建立並讀取草稿'}, 'handoff'), AIMessage(content='完成。')],
            data=[
                call(
                    'create_spreadsheet', {
                        'name': 'result.xlsx',
                        'sheets': [{'name': '結果', 'rows': [[30]]}],
                    }, 'create',
                ),
                inspect_created, AIMessage(content='已實際讀取草稿。'),
            ],
        )
        result = await DocumentAgent(self.config, model).run('alice', '建立 Excel 草稿並核對，不存回。')
        self.assertEqual(result['status'], 'done', result)
        self.assertFalse((self.root/'result.xlsx').exists())
        other = RawRoleModel(
            coordinator=[call('delegate_data', {'task': '讀取其他工作'}, 'handoff'), AIMessage(content='拒絕。')],
            data=[
                call(
                    'inspect_artifact', {
                        'artifact_id': result['files']
                        [0]['id'],
                    }, 'inspect',
                ), AIMessage(content='無權讀取。'),
            ],
        )
        denied = await DocumentAgent(self.config, other).run('alice', '讀取草稿')
        self.assertEqual(denied['answer'], '拒絕。')
        self.assertEqual(denied['tool_feedback'][0]['tool'], 'inspect_artifact')
        self.assertNotIn('result.xlsx', str(denied))
        self.assertFalse(denied['files'])

    async def test_delegation_schema_does_not_grant_identity(self):
        model = RawRoleModel(
            coordinator=[
                call('delegate_document', {'task': '製作文件', 'owner': 'admin'}, 'bad'),
                AIMessage(content='無法委派。'),
            ],
        )
        result = await DocumentAgent(self.config, model).run('alice', '製作文件')
        self.assertTrue(result['tool_feedback'])
        self.assertFalse(result['delegations'])
        self.assertEqual(model.invocations, ['coordinator', 'coordinator'])
        self.assertFalse(result['files'])
