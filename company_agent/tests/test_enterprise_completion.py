from __future__ import annotations

import asyncio
import json
import threading
import unittest
from unittest.mock import patch

import httpx
from langchain_core.messages import AIMessage

from company_agent.runtime import completion
from company_agent.runtime.execution import DocumentAgent
from company_agent.runtime.models import close_model
from company_agent.runtime.models import GatewayModel
from company_agent.runtime.storage import Store
from company_agent.tests.test_agent import call
from company_agent.tests.test_agent import Fixture
from company_agent.tests.test_multi_agent import RawRoleModel


def finish(name='complete_job', outcome='completed', ids=None, summary='已完成。'):
    return call(name, {'outcome': outcome, 'summary': summary, 'artifact_ids': ids or []}, 'finish')


class EnterpriseTests(unittest.IsolatedAsyncioTestCase, Fixture):
    def setUp(self):
        super().setUp()
        self.config.structured_completion = True
        self.config.require_artifact_review = True

    async def run_model(self, **scripts):
        model = self.last_model = RawRoleModel(**scripts)
        result = await DocumentAgent(self.config, model).run('alice', '依文件製作並核對草稿，不發布。')
        self.assertTrue(result['answer'].strip())
        self.assertFalse(list(self.root.iterdir()))
        return result, model

    async def test_explicit_missing_input(self):
        result, _ = await self.run_model(coordinator=[finish(outcome='needs_input', summary='  請提供來源文件。\n')])
        self.assertEqual(result['status'], 'needs_input', result)
        self.assertEqual(result['answer'], '請提供來源文件。')
        self.assertEqual(result['completion_protocol'], 'typed_tool')
        self.assertFalse(result['delegations'])

    async def test_rate_limit_has_safe_actionable_message_no_retry(self):
        class Limited(Exception):
            status_code = 429

        def limit(messages): raise Limited('PRIVATE provider body must not reach user')
        result = await self.failed_job(self.run_model(coordinator=[limit]), Limited)
        self.assertEqual(result['status'], 'error')
        self.assertEqual(self.native_error.status_code, 429)
        self.assertIn('PRIVATE', str(self.native_error))
        self.assertEqual(self.last_model.invocations, ['coordinator'])

    async def test_official_adapter_requires_tool_completion(self):
        requests = []

        def handler(request):
            body = json.loads(request.content)
            requests.append(body)
            self.assertEqual(body['tool_choice'], 'required')
            self.assertNotIn('parallel_tool_calls', body)
            self.assertEqual(body['fallbacks'], [])
            tool = next(t['function'] for t in body['tools'] if t['function']['name'] == 'complete_job')
            self.assertNotIn('runtime', tool['parameters']['properties'])
            return httpx.Response(
                200, json={
                    'choices': [{
                        'finish_reason': 'tool_calls', 'message': {
                            'role': 'assistant', 'content': None, 'tool_calls': [{
                                'id': 'done', 'type': 'function', 'function': {
                                    'name': 'complete_job', 'arguments': json.dumps({'outcome': 'needs_input', 'summary': '請提供來源。', 'artifact_ids': []}),
                                },
                            }],
                        },
                    }],
                    'usage': {'prompt_tokens': 30, 'completion_tokens': 10, 'total_tokens': 40},
                },
            )
        model = GatewayModel('https://gateway.example/v1', 'test-key', 'configured', httpx.MockTransport(handler))
        self.addAsyncCleanup(close_model, model)
        result = await DocumentAgent(self.config, model).run('alice', '製作報告')
        self.assertEqual(result['status'], 'needs_input', result)
        self.assertEqual(len(requests), 1)

    async def test_protocol_cannot_change_during_resume(self):
        result = await self.failed_job(self.run_model(coordinator=[AIMessage(content='')]), ValueError)
        self.config.structured_completion = False
        with self.assertRaisesRegex(ValueError, '協定已變更'):
            await DocumentAgent(self.config, RawRoleModel()).resume('alice', result['job_id'])

    async def test_no_free_text_or_empty_completion_no_retry(self):
        for content in ('', '   ', '已完成所有工作。'):
            result = await self.failed_job(self.run_model(coordinator=[AIMessage(content=content)]), ValueError)
            self.assertEqual(result['status'], 'error', result)
            self.assertEqual(self.last_model.invocations, ['coordinator'])
            self.assertFalse(result['files'])

    async def test_invalid_summary_is_rejected_before_tool(self):
        for summary in ('', '  ', '\t\n', '\u3000\u00a0'):
            with self.subTest(summary=repr(summary)):
                result = await self.failed_job(self.run_model(coordinator=[finish(summary=summary)]), IndexError)
                self.assertEqual(result['status'], 'error', result)
                self.assertEqual(len(self.last_model.invocations), 2)

    async def test_unknown_artifact_completion_cannot_succeed(self):
        result, _ = await self.run_model(coordinator=[finish(ids=['not-an-artifact']), finish(outcome='blocked', summary='產出無法驗證。')])
        self.assertEqual(result['status'], 'blocked', result)
        self.assertIn('非本工作產出', str(result['tool_feedback']))
        self.assertFalse(result['files'])

    async def test_native_batch_keeps_valid_private_draft_and_rejects_invalid_one(self):
        batch = AIMessage(
            content='', tool_calls=[
                {'name': 'create_text', 'args': {'name': 'ok.txt', 'content': 'text'}, 'id': 'valid'},
                {'name': 'create_word', 'args': {'name': 'bad.docx', 'paragraphs': 'wrong-type'}, 'id': 'invalid'},
            ],
        )
        result = await self.failed_job(self.run_model(coordinator=[call('delegate_document', {'task': '建立草稿'}, 'd')], document=[batch]), IndexError)
        self.assertEqual(result['status'], 'error', result)
        self.assertEqual([f['name'] for f in result['files']], ['ok.txt'])

    async def test_batched_completion_does_not_bypass_final_artifact_validation(self):

        created = threading.Event()
        artifact, complete = Store.artifact, completion.complete

        def record_created(store, *args, **kwargs):
            result = artifact(store, *args, **kwargs)
            created.set()
            return result

        async def after_draft(runtime, *, role, **kwargs):
            # This case verifies an already committed draft is preserved.
            # Native parallel tools do not promise which tool finishes first.
            async with asyncio.timeout(5):
                while not created.is_set():
                    await asyncio.sleep(.01)
            return await complete(runtime, role=role, **kwargs)

        batch = AIMessage(
            content='', tool_calls=[
                {'name': 'create_text', 'args': {'name': 'ok.txt', 'content': 'text'}, 'id': 'valid'},
                {
                    'name': 'complete_assignment', 'args': {
                        'outcome': 'completed',
                        'summary': '完成', 'artifact_ids': [],
                    }, 'id': 'done',
                },
            ],
        )
        with patch.object(Store, 'artifact', new=record_created), patch.object(completion, 'complete', new=after_draft):
            result, _ = await self.run_model(
                coordinator=[call('delegate_document', {'task': '建立草稿'}, 'd'), finish(outcome='blocked')],
                document=[batch, finish('complete_assignment', outcome='blocked')],
            )
        self.assertEqual(result['status'], 'blocked', result)
        self.assertEqual([f['name'] for f in result['files']], ['ok.txt'])
        self.assertIn('完成工具須單獨', str(result['tool_feedback']))

    async def test_native_injection_overrides_supplied_runtime(self):
        request = finish()
        request.tool_calls[0]['args']['runtime'] = {'owner': 'admin'}
        result, _ = await self.run_model(coordinator=[request])
        self.assertEqual(result['status'], 'done', result)

    async def test_blocked_child_cannot_be_claimed_complete(self):
        result, _ = await self.run_model(
            coordinator=[
                call('delegate_document', {'task': '建立草稿'}, 'd'),
                finish(), finish(outcome='blocked', summary='子任務缺來源。'),
            ],
            document=[finish('complete_assignment', 'needs_input', summary='缺少來源。')],
        )
        self.assertNotEqual(result['status'], 'done', result)
        self.assertIn('尚有子任務未完成', str(result['tool_feedback']))

    async def test_valid_create_review_and_typed_completion(self):
        ids = []

        def document_done(messages):
            ids.append(json.loads(messages[-1].content)['id'])
            return finish('complete_assignment', ids=ids)
        result, _ = await self.run_model(
            coordinator=[
                call('delegate_document', {'task': '建立草稿'}, 'd'),
                call('delegate_reviewer', {'task': '實際讀取並核對產出'}, 'r'), lambda _:finish(ids=ids),
            ],
            document=[call('create_text', {'name': 'ok.txt', 'content': 'verified'}, 'create'), document_done],
            reviewer=[
                lambda _:call(
                    'inspect_artifact', {'artifact_id': ids[0]},
                    'read',
                ), lambda _:finish('complete_assignment', ids=ids),
            ],
        )
        self.assertEqual(result['status'], 'done', result)
        self.assertEqual(len(result['files']), 1)
        self.assertEqual([x['status'] for x in result['delegations']], ['completed', 'completed'])

    async def test_false_review_claim_cannot_pass(self):
        ids = []

        def document_done(messages):
            ids.append(json.loads(messages[-1].content)['id'])
            return finish('complete_assignment', ids=ids)
        result, _ = await self.run_model(
            coordinator=[
                call('delegate_document', {'task': '建立草稿'}, 'd'), call('delegate_reviewer', {'task': '核對草稿'}, 'r'),
                lambda _:finish(ids=ids), lambda _:finish(outcome='blocked', ids=ids, summary='尚未讀取核對。'),
            ],
            document=[call('create_text', {'name': 'ok.txt', 'content': 'verified'}, 'create'), document_done],
            reviewer=[finish('complete_assignment', summary='已核對。')],
        )
        self.assertEqual(result['status'], 'blocked', result)
        self.assertEqual(len(result['files']), 1)

    async def test_all_outputs_must_be_disclosed(self):
        self.config.require_artifact_review = False
        result, _ = await self.run_model(
            coordinator=[call('delegate_document', {'task': '建立草稿'}, 'd'), finish(), finish(outcome='blocked')],
            document=[
                call(
                    'create_text', {'name': 'ok.txt', 'content': 'verified'},
                    'create',
                ), finish('complete_assignment'),
            ],
        )
        self.assertEqual(result['status'], 'blocked', result)
        self.assertIn('完成清單未列出', str(result['tool_feedback']))
        self.assertEqual(len(result['files']), 1)

    async def test_rejected_completion_remains_unfinished_when_model_fails(self):
        self.config.require_artifact_review = False
        result = await self.failed_job(
            self.run_model(
                coordinator=[call('delegate_document', {'task': '建立草稿'}, 'd'), finish(), finish()],
                document=[
                    call(
                        'create_text', {'name': 'ok.txt', 'content': 'data'},
                        'create',
                    ), finish('complete_assignment'),
                ],
            ), IndexError,
        )
        self.assertEqual(result['status'], 'error', result)
        self.assertTrue(any('完成清單未列出' in m.content for m in result['messages']))
        self.assertEqual(self.last_model.invocations.count('coordinator'), 4)

    async def test_invalid_completion_returns_feedback_and_correction_can_finish(self):
        self.config.require_artifact_review = False
        ids = []

        def created(messages):
            self.assertNotIn('完成後用一般回覆', messages[0].content)
            ids.append(json.loads(messages[-1].content)['id'])
            return finish('complete_assignment', ids=ids)

        def corrected(messages):
            self.assertIn(ids[0], messages[0].content)
            self.assertIn('伺服器本工作產出清單', messages[0].content)
            return finish(ids=ids)
        result, _ = await self.run_model(
            coordinator=[call('delegate_document', {'task': '建立草稿'}, 'd'), finish(), corrected],
            document=[call('create_text', {'name': 'ok.txt', 'content': 'verified'}, 'create'), created],
        )
        self.assertEqual(result['status'], 'done', result)
        self.assertNotIn('errors', result)
        self.assertIn('完成清單未列出', str(result['tool_feedback']))

    async def test_batched_same_role_assignments_keep_separate_results(self):
        batch = AIMessage(
            content='', tool_calls=[
                {'name': 'delegate_document', 'args': {'task': '確認第一項缺資料'}, 'id': 'one'},
                {'name': 'delegate_document', 'args': {'task': '確認第二項缺資料'}, 'id': 'two'},
            ],
        )
        result, model = await self.run_model(
            coordinator=[batch, finish(outcome='needs_input', summary='請提供兩項來源。')],
            document=[
                finish('complete_assignment', 'needs_input', summary='第一項缺來源。'),
                finish('complete_assignment', 'needs_input', summary='第二項缺來源。'),
            ],
        )
        self.assertEqual(result['status'], 'needs_input', result)
        self.assertEqual(len(result['delegations']), 2)
        self.assertEqual(model.invocations, ['coordinator', 'document', 'document', 'coordinator'])

    async def test_publish_still_requires_human_confirmation(self):
        ids = []

        def propose(messages):
            ids.append(json.loads(messages[-1].content)['id'])
            return call('propose_publish', {'artifact_id': ids[0], 'root': 'project', 'path': 'published.txt'}, 'p')
        agent = DocumentAgent(
            self.config, RawRoleModel(
                coordinator=[call('delegate_document', {'task': '建立草稿並提出存檔'}, 'd')],
                document=[call('create_text', {'name': 'ok.txt', 'content': 'verified'}, 'create'), propose],
            ),
        )
        result = await agent.run('alice', '製作草稿並申請存回。')
        self.assertEqual(result['status'], 'awaiting_approval', result)
        self.assertFalse((self.root/'published.txt').exists())
        model = self.last_model = RawRoleModel(
            document=[lambda _:finish('complete_assignment', ids=ids)],
            coordinator=[call('delegate_reviewer', {'task': '讀取核對草稿'}, 'r'), lambda _:finish(ids=ids)],
            reviewer=[
                lambda _:call('inspect_artifact', {'artifact_id': ids[0]}, 'i'),
                lambda _:finish('complete_assignment', ids=ids),
            ],
        )
        resumed = await DocumentAgent(self.config, model).resume(
            'alice', result['job_id'],
            {'proposal_id': result['proposals'][0]['proposal_id'], 'decision': 'reject'},
        )
        self.assertEqual(resumed['status'], 'done', resumed)
        self.assertFalse((self.root/'published.txt').exists())
