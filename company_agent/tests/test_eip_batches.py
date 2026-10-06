from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock
from unittest.mock import patch

from langchain_core.messages import AIMessage
from langchain_core.messages import ToolMessage
from langchain_core.tools import ToolException

from company_agent.api.progress import progress_text
from company_agent.runtime.execution import DocumentAgent
from company_agent.tests.test_agent import call
from company_agent.tests.test_agent import Fixture
from company_agent.tests.test_enterprise_completion import finish
from company_agent.tests.test_enterprise_completion import RawRoleModel


def batch(folders):
    return AIMessage(
        content='', tool_calls=[
            {
                'name': 'eip_query', 'id': str(i), 'args': {
                    'folder': folder, 'start': '2026-09-01', 'end': '2026-09-07',
                },
            } for i, folder in enumerate(folders)
        ],
    )


class EIPBatchTests(unittest.IsolatedAsyncioTestCase, Fixture):
    def setUp(self):
        super().setUp()
        self.config.structured_completion = True

    async def execute(self, request, query):
        def done(messages):
            calls = [c['id'] for m in messages if isinstance(m, AIMessage) for c in m.tool_calls]
            results = [m.tool_call_id for m in messages if isinstance(m, ToolMessage)]
            self.assertEqual(calls, results)
            return finish('complete_assignment')
        model = self.last_model = RawRoleModel(
            coordinator=[call('delegate_eip', {'task': '查近期各文件匣'}, 'delegate'), finish()], eip=[request, done],
        )
        with patch('company_agent.services.eip.client.query', new=query), patch('company_agent.services.eip.client.binding', new=AsyncMock()):
            result = await DocumentAgent(self.config, model).run('alice', '幫我查 EIP 最近任務')
        return result, model

    async def test_native_queries_run_concurrently_without_extra_model_calls(self):
        order = []
        active = 0
        peak = 0

        async def query(ctx, name, args, call_id):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(.01)
            order.append(args.folder)
            active -= 1
            return {'documents': []}
        result, model = await self.execute(batch(['inbox', 'sign', 'notify', 'sent']), query)
        self.assertEqual(result['status'], 'done', result)
        self.assertCountEqual(order, ['inbox', 'sign', 'notify', 'sent'])
        self.assertGreater(peak, 1)
        self.assertEqual(model.invocations, ['coordinator', 'eip', 'eip', 'coordinator'])
        self.assertTrue(any('EIP 查詢／下載' in notice for notice in result['notices']))

    async def test_native_batch_supports_more_than_four_queries(self):
        request = batch(['inbox'] * 5)
        for i, item in enumerate(request.tool_calls):
            item['args']['start'] = f'2026-09-{i + 1:02d}'
        query = AsyncMock(return_value={'documents': []})
        result, _ = await self.execute(request, query)
        self.assertEqual(result['status'], 'done', result)
        self.assertEqual(query.await_count, 5)

    async def test_native_batch_executes_only_valid_parameters(self):
        request = batch(['inbox', 'sent'])
        request.tool_calls[-1]['args']['end'] = 'invalid'
        query = AsyncMock(return_value={'documents': []})
        result, _ = await self.execute(request, query)
        self.assertEqual(result['status'], 'done')
        self.assertEqual(query.await_count, 1)
        self.assertEqual(len(result['tool_feedback']), 1)

    async def test_invalid_query_does_not_report_eip_access(self):
        request = batch(['inbox'])
        request.tool_calls[0]['args']['end'] = 'invalid'
        query = AsyncMock()
        result, _ = await self.execute(request, query)
        query.assert_not_awaited()
        self.assertEqual(len(result['tool_feedback']), 1)
        self.assertFalse(any('EIP 查詢／下載' in notice for notice in result['notices']))

    async def test_independent_query_and_known_attachment_can_share_native_batch(self):
        request = AIMessage(
            content='', tool_calls=[
                *batch(['inbox']).tool_calls,
                {'name': 'eip_read_attachment', 'id': 'attachment', 'args': {'handle': 'a'*32}},
            ],
        )
        query = AsyncMock(return_value={'documents': []})
        read = AsyncMock(return_value={'blocks': [{'text': '內容'}]})
        with patch('company_agent.services.eip.reading.read_attachment', new=read):
            result, _ = await self.execute(request, query)
        self.assertEqual(result['status'], 'done', result)
        self.assertEqual(query.await_count, 1)
        self.assertEqual(read.await_count, 1)

    async def test_native_batch_reports_failure_without_dropping_other_results(self):
        query = AsyncMock(side_effect=[{'documents': []}, ValueError('EIP 登入失效'), {'documents': []}])
        result = await self.failed_job(self.execute(batch(['inbox', 'sign', 'sent']), query), ValueError)
        self.assertEqual(result['status'], 'running', result)
        self.assertEqual(query.await_count, 3)
        self.assertIn('登入失效', str(str(self.native_error)))

    async def test_batched_completions_reject_premature_signoff(self):
        request = AIMessage(
            content='', tool_calls=[
                {**finish('complete_assignment').tool_calls[0], 'id': str(i)} for i in range(2)
            ],
        )
        with self.assertRaisesRegex(ToolException, '完成工具須單獨'):
            await self.execute(request, AsyncMock())

    async def test_successful_query_links_survive_invalid_model_completion(self):
        url = '/agents/eip/11111111-1111-4111-8111-111111111111/'+'a'*32
        query = AsyncMock(
            return_value={
                'documents': [{
                    'subject': '待簽公文', 'date': '2026-09-13 08:00:00', 'view_url': url,
                }],
            },
        )
        model = self.last_model = RawRoleModel(
            coordinator=[call('delegate_eip', {'task': '列出待簽公文與可點擊連結'}, 'delegate')],
            eip=[batch(['inbox']), AIMessage(content='')],
        )
        with patch('company_agent.services.eip.client.query', new=query), \
                patch('company_agent.services.eip.client.binding', new=AsyncMock()):
            result = await self.failed_job(DocumentAgent(self.config, model).run('alice', '列出 EIP 待簽公文，讓我點進去看內文'), ValueError)
        self.assertEqual(result['status'], 'running', result)
        self.assertTrue(str(self.native_error))
        self.assertNotIn(url, str(result['messages']))
        self.assertIn(url, str(result['subgraphs']))
        self.assertEqual(query.await_count, 1)

    async def test_clickable_query_preserves_model_completion_status(self):
        url = '/agents/eip/11111111-1111-4111-8111-111111111111/'+'b'*32
        query = AsyncMock(
            return_value={
                'documents': [{
                    'subject': '待簽公文', 'date': '2026-09-13 08:00:00', 'view_url': url,
                }],
            },
        )
        model = self.last_model = RawRoleModel(
            coordinator=[
                call('delegate_eip', {'task': '列出待簽公文與可點擊連結'}, 'delegate'),
                finish(outcome='needs_input', summary='[待簽公文]('+url+')；請補充查詢範圍。'),
            ],
            eip=[batch(['inbox']), finish('complete_assignment')],
        )
        with patch('company_agent.services.eip.client.query', new=query), \
                patch('company_agent.services.eip.client.binding', new=AsyncMock()):
            result = await DocumentAgent(self.config, model).run(
                'alice', '列出 EIP 待簽公文，讓我點進去看內文',
            )
        self.assertEqual(result['status'], 'needs_input', result)
        self.assertIn('[待簽公文]('+url+')', result['answer'])
        self.assertEqual(query.await_count, 1)

    async def test_query_does_not_append_unselected_documents_to_answer(self):
        from company_agent.api.pipe import markdown
        from company_agent.tests.postgres_fixture import checkpointer

        prefix = '/agents/eip/11111111-1111-4111-8111-111111111111/'
        related = {'subject': '測試甲工程施工日誌', 'date': '2026-09-18', 'view_url': prefix + 'a' * 32}
        unrelated = {'subject': '測試乙工程採購', 'date': '2026-09-18', 'view_url': prefix + 'b' * 32}
        selected = f"[施工日誌]({related['view_url']})"
        # The final answer may contain selected citations, or just the requested count.
        for answer in (selected, '測試甲工程共有 1 件公文。'):
            with self.subTest(answer=answer):
                query = AsyncMock(return_value={'documents': [related, unrelated]})
                model = RawRoleModel(
                    coordinator=[call('delegate_eip', {'task': '查測試甲工程'}, 'delegate'), finish(summary=answer)],
                    eip=[batch(['inbox']), finish('complete_assignment', summary=selected)],
                )
                with patch('company_agent.services.eip.client.query', query), \
                        patch('company_agent.services.eip.client.binding', AsyncMock()):
                    result = await DocumentAgent(self.config, model).run('alice', '查測試甲工程')
                self.assertEqual(result['answer'], answer)
                rendered = markdown({**result, 'id': result['job_id']})
                self.assertNotIn(unrelated['subject'], rendered)
                self.assertNotIn(unrelated['view_url'], rendered)
                self.assertNotIn('可直接開啟的 EIP 公文', rendered)
                if answer == selected:
                    self.assertEqual(rendered.count(related['view_url']), 1)
                # Filtering presentation does not erase the actual child tool receipt.
                async with checkpointer(self.store) as saver:
                    checkpoints = [
                        saved async for saved in saver.alist(
                            {'configurable': {'thread_id': result['job_id']}},
                        )
                    ]
                self.assertTrue(
                    any(
                        unrelated['view_url'] in str(
                            saved.checkpoint['channel_values'],
                        ) for saved in checkpoints
                    ),
                )
                query.assert_awaited_once()

    async def test_attachment_batch_keeps_each_native_result(self):
        request = AIMessage(
            content='', tool_calls=[
                {
                    'name': 'eip_read_attachment', 'id': str(
                        i,
                    ), 'args': {'handle': c*32},
                } for i, c in enumerate('abc')
            ],
        )
        for fail in (False, True):
            order = []
            active = 0

            async def read(ctx, args):
                nonlocal active
                active += 1
                order.append(args.handle)
                await asyncio.sleep(.01)
                active -= 1
                if fail and args.handle == 'b'*32:
                    raise ValueError('附件無法讀取')
                return {'blocks': [{'text': '內容'}]}
            with patch('company_agent.services.eip.reading.read_attachment', new=read):
                if fail:
                    result = await self.failed_job(self.execute(request, AsyncMock()), ValueError)
                else:
                    result, _model = await self.execute(request, AsyncMock())
            self.assertEqual(len(order), 3)
            self.assertEqual(result['status'], 'running' if fail else 'done', result)

    async def test_attachment_limit_corrects_once_before_read(self):
        read = AsyncMock(return_value={'handle': 'a'*32, 'name': 'a.pdf', 'sha256': 'a'*64, 'blocks': [{'text': '內容'}]})

        def correction(messages):
            self.assertEqual(read.await_count, 0)
            self.assertIn('5', messages[-1].content)
            self.assertEqual(messages[-1].status, 'error')
            return call('eip_read_attachment', {'handle': 'a'*32, 'limit': 5}, 'good')
        model = self.last_model = RawRoleModel(
            coordinator=[call('delegate_eip', {'task': '讀附件'}, 'd'), finish()], eip=[
                call(
                    'eip_read_attachment', {'handle': 'a'*32, 'limit': 30},
                    'bad',
                ), correction, finish('complete_assignment'),
            ],
        )
        with patch('company_agent.services.eip.reading.read_attachment', new=read), patch('company_agent.services.eip.client.binding', new=AsyncMock()):
            result = await DocumentAgent(self.config, model).run('alice', '讀附件內容')
        self.assertEqual(result['status'], 'done', result)
        self.assertEqual(read.await_count, 1)
        self.assertEqual(len(result['tool_feedback']), 1)


class ProgressTests(unittest.TestCase):
    def test_safe_labels_and_plain_progress(self):
        for value in ({'stage': 'model', 'agent': 'eip', 'detail': 'PRIVATE'},):
            self.assertEqual(progress_text(value), '正在查詢 EIP')
        self.assertEqual(progress_text({'stage': 'tool', 'tool': 'eip_query'}), '正在查詢 EIP 文件清單')
        self.assertNotIn('PRIVATE', progress_text({'stage': 'PRIVATE', 'agent': 'PRIVATE'}))
