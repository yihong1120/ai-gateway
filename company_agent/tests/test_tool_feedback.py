from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from langchain.agents.middleware.model_call_limit import ModelCallLimitExceededError
from langchain_core.messages import AIMessage
from langchain_core.messages import ToolMessage

from company_agent.runtime.execution import DocumentAgent
from company_agent.runtime.results import execution_summary
from company_agent.tests.test_agent import call
from company_agent.tests.test_agent import Fixture
from company_agent.tests.test_enterprise_completion import finish
from company_agent.tests.test_enterprise_completion import RawRoleModel


def cad(limit=10, **kw):
    return call('read_cad', {'root': 'project', 'path': 'source.dwg', 'view': 'texts', 'limit': limit, **kw}, 'read')


def batch(*messages):
    calls = []
    for i, m in enumerate(messages):
        calls.extend([{**c, 'id': str(i)} for c in m.tool_calls])
    return AIMessage(content='', tool_calls=calls)


class ToolFeedbackTests(unittest.IsolatedAsyncioTestCase, Fixture):
    def setUp(self):
        super().setUp()
        self.config.structured_completion = True
        self.config.enable_cad = True
        self.invoked = []

    def read(self, box, args):
        self.invoked.append(args.model_dump())
        src = {'root': args.root, 'path': args.path, 'sha256': 'a'*64}
        box.sources[(args.root, args.path)] = src
        return {**src, 'rows': [{'text': '施工區'}], 'next_start': 10, 'layer_count': 433, 'block_count': 184, 'entity_count': 36319}

    async def run_steps(self, steps, coordinator=None):
        model = self.last_model = RawRoleModel(
            coordinator=coordinator or [
                call(
                    'delegate_engineering', {'task': '讀CAD'}, 'd',
                ), finish(),
            ], engineering=steps,
        )
        with patch('company_agent.services.files.operations.ToolBox.read_cad', new=lambda box, args: self.read(box, args)):
            result = await DocumentAgent(self.config, model).run('alice', '閱讀CAD檔案內容')
        return result, model

    async def test_limit_corrected_once_then_done_with_complete_tool_receipts(self):
        def correction(messages):
            self.assertEqual(self.invoked, [])
            self.assertEqual(messages[-1].status, 'error')
            self.assertIn('10', messages[-1].content)
            return cad(10)

        def done(messages):
            ids = [c['id'] for m in messages if isinstance(m, AIMessage) for c in m.tool_calls]
            self.assertEqual(ids, [m.tool_call_id for m in messages if isinstance(m, ToolMessage)])
            return finish('complete_assignment')
        result, model = await self.run_steps([cad(50), correction, done])
        self.assertEqual(result['status'], 'done', result)
        self.assertEqual(len(self.invoked), 1)
        self.assertEqual(self.invoked[0]['limit'], 10)
        self.assertEqual(len(result['tool_feedback']), 1)
        self.assertEqual(result['read_results'][0]['layer_count'], 433)
        self.assertEqual(model.invocations, ['coordinator', 'engineering', 'engineering', 'engineering', 'coordinator'])

    async def test_corrected_default_limit_is_valid(self):
        fixed = cad()
        fixed.tool_calls[0]['args'].pop('limit')
        result, _ = await self.run_steps([cad(30), fixed, finish('complete_assignment')])
        self.assertEqual(result['status'], 'done', result)
        self.assertEqual(self.invoked[0]['limit'], 10)

    async def test_native_batch_keeps_valid_results_and_repairs_only_failed_call(self):
        def correction(messages):
            self.assertEqual([x['start'] for x in self.invoked], [0])
            return cad(10, start=10)
        result, _ = await self.run_steps([batch(cad(10), cad(50, start=10)), correction, finish('complete_assignment')])
        self.assertEqual(result['status'], 'done', result)
        self.assertCountEqual([x['start'] for x in self.invoked], [0, 10])
        self.assertEqual(len(result['tool_feedback']), 1)

    async def test_repeated_invalid_arguments_stop_at_job_limit_without_io(self):
        self.config.max_steps = 4
        result = await self.failed_job(self.run_steps([cad(50), cad(30), cad(20), cad(10)]), ModelCallLimitExceededError)
        self.assertEqual(result['status'], 'error', result)
        self.assertEqual(self.invoked, [])
        self.assertEqual(self.store.attempts(result['job_id']), 4)

    async def test_correction_can_refine_authorized_query_without_special_paging_protocol(self):
        result, _ = await self.run_steps([cad(50), cad(10, path='other.dwg', start=10), finish('complete_assignment')])
        self.assertEqual(result['status'], 'done', result)
        self.assertEqual([(x['path'], x['start']) for x in self.invoked], [('other.dwg', 10)])

    async def test_any_schema_error_returns_feedback_without_executing_invalid_call(self):
        result, _ = await self.run_steps([cad(root=12), cad(), finish('complete_assignment')])
        self.assertEqual(result['status'], 'done', result)
        self.assertEqual(len(self.invoked), 1)
        self.assertEqual(len(result['tool_feedback']), 1)

    async def test_permission_failure_after_correction_is_not_cleared(self):
        def reject(box, args): raise ValueError('讀取權限已撤銷')
        self.read = reject
        result = await self.failed_job(self.run_steps([cad(50), cad(10), finish('complete_assignment')]), ValueError)
        self.assertNotEqual(result['status'], 'done', result)
        self.assertIn('權限', str(str(self.native_error)))
        self.assertEqual(
            len([
                m.content for s in result['subgraphs'].values()
                for m in s.get('messages', []) if isinstance(m, ToolMessage) and m.status == 'error'
            ]), 1,
        )
        self.assertEqual(self.store.attempts(result['job_id']), 3)  # Stop before the scripted completion.

    async def test_resume_uses_saved_tool_feedback(self):
        def interrupted(messages): raise RuntimeError('model unavailable')
        result = await self.failed_job(self.run_steps([cad(50), interrupted]), RuntimeError)
        self.assertEqual(result['status'], 'error')
        model = self.last_model = RawRoleModel(
            engineering=[cad(10), finish('complete_assignment')], coordinator=[finish()],
        )
        with patch('company_agent.services.files.operations.ToolBox.read_cad', new=lambda box, args: self.read(box, args)):
            resumed = await DocumentAgent(self.config, model).resume('alice', result['job_id'])
        self.assertEqual(resumed['status'], 'done', resumed)
        self.assertEqual(len(self.invoked), 1)
        self.assertEqual(len(resumed['tool_feedback']), 1)

    async def test_general_reader_routes_cad_without_parser_failure(self):
        def handoff(messages):
            value = json.loads(messages[-1].content)
            self.assertEqual(value['status'], 'needs_specialist')
            self.assertFalse(value['executed'])
            return finish('complete_assignment', summary='已找到CAD路徑，請交工程圖角色讀取。')
        model = self.last_model = RawRoleModel(
            coordinator=[
                call('delegate_knowledge', {'task': '找CAD'}, 'k'), call(
                    'delegate_engineering', {'task': '讀CAD'}, 'e',
                ), finish(),
            ],
            knowledge=[call('read_file', {'root': 'project', 'path': 'source.DWG'}, 'read'), handoff],
            engineering=[cad(), finish('complete_assignment')],
        )
        with patch('company_agent.services.files.operations.ToolBox.read_file') as plain, patch('company_agent.services.files.operations.ToolBox.read_cad', new=lambda box, args: self.read(box, args)):
            result = await DocumentAgent(self.config, model).run('alice', '看CAD')
        plain.assert_not_called()
        self.assertEqual(result['status'], 'done', result)
        self.assertNotIn('errors', result)
        self.assertEqual(len(result['sources']), 1)

    async def test_feedback_can_end_with_missing_input_without_a_repair_protocol(self):
        result, _model = await self.run_steps(
            [cad(50), finish('complete_assignment', 'needs_input', summary='需要確認閱讀範圍。')],
            coordinator=[
                call('delegate_engineering', {'task': '讀CAD'}, 'd'),
                finish(outcome='needs_input', summary='請確認閱讀範圍。'),
            ],
        )
        self.assertEqual(result['status'], 'needs_input', result)
        self.assertEqual(self.invoked, [])
        self.assertEqual(len(result['tool_feedback']), 1)
        self.assertNotIn('errors', result)
        self.assertNotIn(str(self.private), str(result['tool_feedback']))

    async def test_corrected_delegation_creates_only_one_assignment(self):
        result, _model = await self.run_steps(
            [cad(), finish('complete_assignment')],
            coordinator=[
                call('delegate_engineering', {'task': '讀CAD', 'owner': 'admin'}, 'bad'),
                call('delegate_engineering', {'task': '讀CAD'}, 'fixed'), finish(),
            ],
        )
        self.assertEqual(result['status'], 'done', result)
        self.assertEqual(len(result['delegations']), 1)
        self.assertEqual(len(result['tool_feedback']), 1)
        self.assertEqual(len(self.invoked), 1)

    async def test_operation_failure_keeps_prior_results_and_stops_model_loop(self):
        read = self.read

        def fail_second(box, args):
            if args.start:
                raise ValueError('來源版本已變更。')
            return read(box, args)
        self.read = fail_second
        result = await self.failed_job(self.run_steps([cad(), cad(start=10), finish('complete_assignment')]), ValueError)
        self.assertEqual(result['status'], 'error', result)
        self.assertEqual(sum(len(execution_summary(s)['read_results']) for s in result['subgraphs'].values()), 1)
        self.assertIn('來源版本已變更', str(self.native_error))
        self.assertEqual(self.last_model.invocations, ['coordinator', 'engineering', 'engineering'])
