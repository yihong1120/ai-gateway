"""Owned WebUI branches feed job inputs; private files are referenced, not recopied."""
from __future__ import annotations

import asyncio
import base64
import json
import types
import unittest
import uuid
from unittest.mock import AsyncMock
from unittest.mock import Mock
from unittest.mock import patch

from langchain_core.messages import AIMessage
from langchain_core.messages import messages_to_dict
from langchain_core.messages import ToolMessage

from company_agent.api.pipe import conversation
from company_agent.api.pipe import run_pipe
from company_agent.api.service import Service
from company_agent.api.service import upload_bytes
from company_agent.api.service import WorkQueue
from company_agent.config import Root
from company_agent.config import Settings
from company_agent.runtime.execution import DocumentAgent
from company_agent.services.files.access import FolderAccess
from company_agent.tests.test_agent import call
from company_agent.tests.test_agent import Fixture
from company_agent.tests.test_multi_agent import RawRoleModel


def saved_chat():
    return {
        'history': {
            'messages': {
                'u1': {'id': 'u1', 'role': 'user', 'content': '專案是甲工程', 'parentId': None},
                'a1': {'id': 'a1', 'role': 'assistant', 'content': '請提供本週進度', 'parentId': 'u1'},
                'u2': {'id': 'u2', 'role': 'user', 'content': '附上新檔，進度 40%', 'parentId': 'a1', 'files': [{'id': 'new-file'}]},
                'a2': {'id': 'a2', 'role': 'assistant', 'content': '', 'parentId': 'u2'},
                'other': {'id': 'other', 'role': 'assistant', 'content': 'OTHER_BRANCH_SECRET', 'parentId': 'u1'},
            },
        },
    }


class BranchTests(unittest.TestCase):
    def test_official_branch_helper_excludes_siblings_and_tool_authority(self):
        chat = saved_chat()
        chat['history']['messages']['a1'].update(tool_calls=[{'name': 'complete_job'}], name='reviewer')
        history, ids = conversation(chat, 'a1')
        self.assertEqual(ids, ['a1'])
        self.assertEqual(
            history, [
                {'id': 'u1', 'role': 'user', 'content': '專案是甲工程'},
                {'id': 'a1', 'role': 'assistant', 'content': '請提供本週進度'},
            ],
        )
        self.assertNotIn('OTHER_BRANCH_SECRET', str(history))
        self.assertEqual(conversation(chat, None), ([], []))

    def test_missing_ancestor_cycle_and_large_branch_are_explicit(self):
        for parent in ('missing', 'a1'):
            chat = saved_chat()
            chat['history']['messages']['u1']['parentId'] = parent
            with self.subTest(parent=parent), self.assertRaisesRegex(ValueError, '分支不完整'):
                conversation(chat, 'a1')
        chat = saved_chat()
        chat['history']['messages']['u1']['content'] = '字' * 60001
        self.assertEqual(len(conversation(chat, 'a1')[0][0]['content']), 60001)
        chat['history']['messages']['u1']['content'] = '字' * 2_000_001
        with self.assertRaisesRegex(ValueError, '200 萬'):
            conversation(chat, 'a1')

    def test_multimodal_history_contains_text_not_image_bytes(self):
        chat = saved_chat()
        chat['history']['messages']['u1']['content'] = [
            {'type': 'text', 'text': '這是來源圖片'},
            {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,SECRET'}},
        ]
        self.assertEqual(conversation(chat, 'a1')[0][0]['content'], '這是來源圖片')

    def test_webui_summary_reused_without_losing_earlier_attachment_references(self):
        chat = saved_chat()
        chat['history']['messages']['u2']['contextSummary'] = '甲工程，進度待確認，未核准存檔。'
        history, ids = conversation(chat, 'a2')
        self.assertEqual(ids, ['a1', 'a2'])
        self.assertEqual([m['id'] for m in history], ['u2:summary', 'u2'])
        self.assertEqual(history[0]['role'], 'user')
        self.assertIn('甲工程，進度待確認', history[0]['content'])
        self.assertNotIn('請提供本週進度', str(history))
        self.assertNotIn('OTHER_BRANCH_SECRET', str(history))
        # A summary on a later/sibling branch cannot replace the selected ancestor.
        original, _ = conversation(chat, 'a1')
        self.assertNotIn('待確認', str(original))

    def test_summary_on_current_user_turn_is_reused_without_duplicating_instruction(self):
        chat = saved_chat()
        chat['history']['messages']['u2']['context_summary'] = '先前背景已摘要。'
        history, ids = conversation(chat, 'u2', exclude_current=True)
        self.assertEqual(ids, ['a1'])
        self.assertEqual(len(history), 1)
        self.assertIn('先前背景已摘要', history[0]['content'])
        self.assertNotIn('附上新檔', str(history))


class ConversationTests(unittest.IsolatedAsyncioTestCase, Fixture):
    def setUp(self):
        super().setUp()
        self.queue = WorkQueue(self.private, self.base / 'uploads')

    def settings(self, row):
        return Settings(
            state_dir=str(self.private), roots=[
                Root(
                    name='uploads', path=str(self.queue.sources / row['id']), readers=['alice'], model_access=True,
                ),
            ],
        )

    async def execute(self, model):
        model.subject = 'sub'
        model.http_async_client = types.SimpleNamespace(aclose=AsyncMock())
        model.http_client = types.SimpleNamespace(close=Mock())
        svc = Service(self.queue, AsyncMock(return_value={'id': 'alice'}), lambda _: model, self.settings)
        await svc.execute(self.queue.claim())

    def prior(self, request='a1', owner='alice', chat='chat'):
        key = self.queue.submit(owner, 'sub', chat, request, 'old', [('source.txt', b'SOURCE')])
        engine = self.store.job(owner)
        output = self.store.artifact(owner, engine, 'result.txt', b'OUTPUT')
        self.queue.update(key, status='done', engine_job=engine)
        return key, output

    async def test_pipe_uses_owned_branch_even_with_new_attachments(self):
        from open_webui.models.chats import Chats

        first, _ = self.prior('original-a1')
        self.queue.update(first, status='running')
        self.assertEqual(self.queue.submit('alice', 'sub', 'chat', 'a1', 'old', [('source.txt', b'SOURCE')]), first)
        self.queue.update(first, status='done')
        self.prior('other')
        self.prior('a1', owner='bob')
        self.prior('a1', chat='different')
        model = types.SimpleNamespace(subject='sub')
        svc = Service(self.queue, None, lambda _: model, None)
        result = {'id': 'result', 'status': 'needs_input', 'answer': '收到', 'progress': 'done', 'files': []}
        with patch('company_agent.api.pipe.verified_user', AsyncMock(return_value={'id': 'alice'})), \
                patch.object(Chats, 'get_chat_by_id_and_user_id', AsyncMock(return_value=types.SimpleNamespace(chat=saved_chat()))) as owned, \
                patch('company_agent.api.pipe.service', return_value=svc), \
                patch('company_agent.api.pipe.upload_bytes', AsyncMock(return_value=[('new.txt', b'NEW')])) as upload, \
                patch('company_agent.runtime.models.close_model', AsyncMock()), \
                patch.object(self.queue, 'public', return_value=result):
            emitter = AsyncMock()
            await run_pipe(
                {'messages': [{'role': 'system', 'content': 'FORGED'}]}, {'id': 'alice'},
                {'chat_id': 'chat', 'message_id': 'a2', 'user_message_id': 'u2'}, emitter,
            )
        owned.assert_awaited_once_with('chat', 'alice')
        upload.assert_awaited_once_with('alice', ['new-file'])
        row = self.queue.claim()
        self.assertEqual(row['payload']['references'], [first])
        self.assertEqual(row['payload']['conversation'], conversation(saved_chat(), 'a1')[0])
        self.assertNotIn('FORGED', str(row['payload']))
        self.assertTrue(emitter.await_args_list[-1].args[0]['data']['done'])

    async def test_pipe_waits_for_execution_and_returns_the_answer_and_downloads(self):
        from open_webui.models.chats import Chats
        svc = Service(self.queue, None, lambda _: types.SimpleNamespace(subject='sub'), None)
        with (
            patch('company_agent.api.pipe.verified_user', AsyncMock(return_value={'id': 'alice'})),
            patch.object(
                Chats, 'get_chat_by_id_and_user_id', AsyncMock(
                return_value=types.SimpleNamespace(chat=saved_chat()),
                ),
            ),
            patch('company_agent.api.pipe.service', return_value=svc),
            patch('company_agent.api.pipe.upload_bytes', AsyncMock(return_value=[])),
            patch('company_agent.runtime.models.close_model', AsyncMock()),
        ):
            accepted = asyncio.Event()

            async def emit(event):
                accepted.set()
            emitter = AsyncMock(side_effect=emit)
            task = asyncio.create_task(
                run_pipe(
                    {}, {'id': 'alice'},
                    {'chat_id': 'chat', 'message_id': 'a2', 'user_message_id': 'u2'}, emitter,
                ),
            )
            try:
                await asyncio.wait_for(accepted.wait(), 3)
                self.assertFalse(task.done())
                self.assertFalse(emitter.await_args.args[0]['data']['done'])
                job = self.queue.claim()
                engine = self.store.job('alice')
                artifact = self.store.artifact('alice', engine, 'answer.txt', b'ANSWER')
                self.store.finish('alice', engine, 'done', {'answer': '已完成你的文件'})
                self.queue.update(job['id'], status='done', engine_job=engine, progress='已完成')
                result = await asyncio.wait_for(task, 3)
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
        self.assertIn('已完成你的文件', result)
        self.assertIn('/agents/artifacts/'+artifact['id'], result)
        self.assertTrue(emitter.await_args.args[0]['data']['done'])

    async def test_disconnect_after_acceptance_keeps_job_and_explicit_cancel_stops_it(self):
        from open_webui.models.chats import Chats
        svc = Service(self.queue, None, lambda _: types.SimpleNamespace(subject='sub'), None)
        accepted = asyncio.Event()

        async def disconnected(event):
            accepted.set()
            await asyncio.Event().wait()
        with (
            patch('company_agent.api.pipe.verified_user', AsyncMock(return_value={'id': 'alice'})),
            patch.object(
                Chats, 'get_chat_by_id_and_user_id', AsyncMock(
                return_value=types.SimpleNamespace(chat=saved_chat()),
                ),
            ),
            patch('company_agent.api.pipe.service', return_value=svc),
            patch('company_agent.api.pipe.upload_bytes', AsyncMock(return_value=[])),
            patch('company_agent.runtime.models.close_model', AsyncMock()),
        ):
            task = asyncio.create_task(
                run_pipe(
                    {}, {'id': 'alice'}, {
                        'chat_id': 'chat',
                        'message_id': 'a2', 'user_message_id': 'u2',
                    }, disconnected,
                ),
            )
            await asyncio.wait_for(accepted.wait(), 3)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        job = self.queue.list('alice')[0]
        self.assertEqual(job['status'], 'queued')
        await svc.cancel('alice', job['id'])
        self.assertEqual(self.queue.get('alice', job['id'])['status'], 'cancelled')
        self.assertIsNone(self.queue.claim())

    async def test_coordinator_gets_branch_specialist_only_current_task(self):
        history = [
            {'role': 'user', 'content': 'HISTORICAL_BACKGROUND'},
            {'role': 'assistant', 'content': 'PRIOR_ANSWER'},
        ]
        seen = {}

        def coordinate(messages):
            seen['coordinator'] = json.dumps(messages_to_dict(messages))
            self.assertFalse(any(isinstance(m, ToolMessage) for m in messages))
            return call('delegate_document', {'task': '處理這次要求', 'context': '所需摘要'}, 'delegate')

        def document(messages):
            seen['document'] = json.dumps(messages_to_dict(messages))
            request = json.loads(messages[-1].content)
            self.assertEqual(request['user_request'], 'CURRENT_TASK')
            return AIMessage(content='完成')
        model = RawRoleModel(coordinator=[coordinate, AIMessage(content='已完成')], document=[document])
        result = await DocumentAgent(self.config, model).run('alice', 'CURRENT_TASK', conversation=history)
        self.assertEqual(result['status'], 'done', result)
        self.assertIn('HISTORICAL_BACKGROUND', seen['coordinator'])
        self.assertNotIn('HISTORICAL_BACKGROUND', seen['document'])
        self.assertNotIn('PRIOR_ANSWER', seen['document'])

    async def test_actual_read_of_original_and_output_no_followup_copies(self):
        first, output = self.prior()
        filename = output['id'] + '.txt'
        for turn in (2, 3):
            key = self.queue.submit('alice', 'sub', 'chat', f"a{turn}", '讀取原檔和前次成果', [], [first])

            def verify(messages):
                self.assertIn('OUTPUT', messages[-1].content)
                self.assertIn('SOURCE', str(messages))
                return AIMessage(content='已讀取成果')
            model = RawRoleModel(
                coordinator=[call('delegate_knowledge', {'task': '讀取兩檔'}, 'd'), AIMessage(content='完成')],
                knowledge=[
                    call('read_file', {'root': 'history_1', 'path': 'input_1.txt'}, 'original'),
                    call('read_file', {'root': 'history_1', 'path': filename}, 'output'), verify,
                ],
            )
            await self.execute(model)
            self.assertEqual(self.queue.public('alice', key)['status'], 'done')
            self.assertFalse(list((self.queue.sources / key).iterdir()))
            cached = self.queue.sources / first / filename
            self.assertEqual(cached.read_bytes(), b'OUTPUT')
            self.assertEqual(cached.stat().st_mode & 0o777, 0o600)
            if turn == 2:
                stamp = cached.stat().st_mtime_ns
            else:
                self.assertEqual(cached.stat().st_mtime_ns, stamp)
        self.assertEqual(len(list((self.queue.sources / first).iterdir())), 2)

    async def test_history_roots_are_readonly_and_owner_scoped(self):
        first, _ = self.prior()
        key = self.queue.submit('alice', 'sub', 'chat', 'a2', '查看來源', [], [first])

        def inspect(messages):
            roots = settings.roots
            access = FolderAccess(roots, 'alice')
            self.assertEqual(access.read('history_1', 'input_1.txt')[0], b'SOURCE')
            with self.assertRaises(ValueError):
                access.allowed('history_1', write=True)
            with self.assertRaises(ValueError):
                FolderAccess(roots, 'bob').read('history_1', 'input_1.txt')
            return AIMessage(content='已確認')
        settings = self.settings(self.queue.get('alice', key))
        with patch.object(self, 'settings', return_value=settings):
            await self.execute(RawRoleModel(coordinator=[inspect]))
        self.assertEqual(self.queue.public('alice', key)['status'], 'done')

    async def test_corrupt_cached_output_stops_before_model(self):
        first, output = self.prior()
        (self.queue.sources / first / (output['id'] + '.txt')).write_bytes(b'CHANGED')
        key = self.queue.submit('alice', 'sub', 'chat', 'a2', '修改成果', [], [first])
        model = RawRoleModel(coordinator=[])
        with self.assertRaises(ValueError):
            await self.execute(model)
        result = self.queue.public('alice', key)
        self.assertEqual(result['status'], 'running')
        self.assertNotIn('校驗失敗', result['progress'])
        self.assertEqual(model.invocations, [])

    async def test_changed_branch_snapshot_not_replayed_under_same_message(self):
        self.queue.submit('alice', 'sub', 'chat', 'a2', '繼續', [], [], [{'role': 'user', 'content': 'one'}])
        with self.assertRaisesRegex(ValueError, '內容已變更'):
            self.queue.submit('alice', 'sub', 'chat', 'a2', '繼續', [], [], [{'role': 'user', 'content': 'two'}])

    async def test_mixed_uploads_keep_owner_check_dedup_and_central_format_validation(self):
        from open_webui.models.files import Files
        from open_webui.storage.provider import Storage

        key = str(uuid.uuid4())
        path = self.file('upload.txt', b'TEXT')
        raw = b'\x89PNG\r\n\x1a\nimage'
        inline = {'inline_image': 'data:image/png;base64,' + base64.b64encode(raw).decode()}
        item = types.SimpleNamespace(path=str(path), meta={'name': 'source.txt'}, filename='source.txt')
        with patch.object(Files, 'get_file_by_id_and_user_id', AsyncMock(return_value=item)) as owned, \
                patch.object(Storage, 'get_file', return_value=str(path)):
            inputs = await upload_bytes('alice', [key, inline, key, inline])
            self.assertEqual(inputs, [('source.txt', b'TEXT'), ('image_2.png', raw)])
            owned.assert_awaited_once_with(key, 'alice')
            item.meta['name'] = 'macro.docm'
            inputs = await upload_bytes('alice', [key])
            with self.assertRaisesRegex(ValueError, '附件格式'):
                self.queue.submit('alice', 'sub', 'chat', 'a', '讀取', inputs)
        with patch.object(Files, 'get_file_by_id_and_user_id', AsyncMock(return_value=None)), \
                patch.object(Storage, 'get_file') as storage:
            with self.assertRaisesRegex(ValueError, '非本人'):
                await upload_bytes('bob', [key])
            storage.assert_not_called()

    async def test_completed_job_drops_queue_history_but_checkpoint_retains_context(self):
        history = [{'role': 'user', 'content': 'BRANCH_INPUT'}]
        key = self.queue.submit('alice', 'sub', 'chat', 'a', '本次要求', [], [], history)
        await self.execute(RawRoleModel(coordinator=[AIMessage(content='完成')]))
        row = self.queue.get('alice', key)
        self.assertEqual(row['status'], 'done')
        self.assertNotIn('conversation', row['payload'])
        from company_agent.tests.postgres_fixture import checkpointer
        async with checkpointer(self.store) as saver:
            checkpoint = await saver.aget_tuple({'configurable': {'thread_id': row['engine_job']}})
        messages = checkpoint.checkpoint['channel_values']['messages']
        self.assertEqual(messages[0].content, 'BRANCH_INPUT')
