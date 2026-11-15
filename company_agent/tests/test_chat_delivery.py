"""Real durable queue changes delivered to owned WebUI messages without model calls."""
from __future__ import annotations

import copy
import time
import json
import types
import unittest
from unittest.mock import AsyncMock
from unittest.mock import patch

from company_agent.tests.webui_fixture import modules as host_modules
from company_agent.api.chat import publish_pending
from company_agent.api.queue import WorkQueue
from company_agent.api.service import Service
from company_agent.tests.test_agent import Fixture


class ChatDeliveryTests(unittest.IsolatedAsyncioTestCase, Fixture):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.dict('sys.modules', host_modules()))
        self.queue = WorkQueue(self.private, self.base/'uploads')
        self.key = self.queue.submit('alice', 'subject', 'chat', 'assistant', '請處理', [])
        self.chat = {
            'history': {
                'messages': {
                    'user': {'id': 'user', 'role': 'user', 'content': '請處理'},
                    'assistant': {'id': 'assistant', 'role': 'assistant', 'parentId': 'user', 'content': ''},
                },
            },
        }
        self.events = AsyncMock()

        async def owned(chat_id, owner):
            return types.SimpleNamespace(chat=copy.deepcopy(self.chat)) if owner == 'alice' and chat_id == 'chat' else None

        async def save(chat_id, message_id, data):
            self.chat['history']['messages'][message_id].update(copy.deepcopy(data))
            return types.SimpleNamespace(chat=self.chat)
        from open_webui.models.chats import Chats
        from open_webui.socket import main as socket
        for target, name, value in (
            (Chats, 'get_chat_by_id_and_user_id', AsyncMock(side_effect=owned)),
            (Chats, 'upsert_message_to_chat_by_id_and_message_id', AsyncMock(side_effect=save)),
            (Chats, 'add_message_status_to_chat_by_id_and_message_id', AsyncMock()),
            (socket, 'get_event_emitter', AsyncMock(return_value=self.events)),
        ):
            p = patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)

    async def publish(self):
        await publish_pending(self.queue, Service._io)

    def finish(self, status='done', answer='這是完整回答'):
        engine = self.store.job('alice')
        file = self.store.artifact('alice', engine, '成果.txt', b'RESULT')
        self.store.finish('alice', engine, status, {'answer': answer})
        self.queue.update(self.key, status=status, engine_job=engine, progress='執行已結束')
        return file

    async def test_progress_then_result_and_file_are_saved_in_same_message(self):
        await self.publish()
        message = self.chat['history']['messages']['assistant']
        self.assertFalse(message['done'])
        self.assertIn('等待執行', message['content'])
        self.queue.update(self.key, status='running', progress='正在核對成果')
        await self.publish()
        self.assertEqual(self.events.await_args.args[0]['data']['done'], False)
        self.assertIn('正在核對成果', message['content'])
        file = self.finish()
        await self.publish()
        self.assertTrue(message['done'])
        self.assertIn('這是完整回答', message['content'])
        self.assertIn('/agents/artifacts/'+file['id'], message['content'])
        self.assertEqual(message['output'][0]['content'][0]['text'], message['content'])
        self.assertEqual(self.events.await_args.args[0]['type'], 'chat:completion')
        self.assertEqual(self.queue.chat_pending(), [])

    async def test_finished_work_is_delivered_after_disconnect_and_outbox_restart(self):
        from open_webui.socket import main as socket
        self.chat['history']['messages']['assistant']['content'] = (
            f'工作已接收，會在背景執行；關閉聊天或頁面後仍會繼續。\n\n[查看進度](/agents?job={self.key})'
        )
        self.finish()
        restored = WorkQueue(self.private, self.base/'uploads')
        with patch.object(socket, 'get_event_emitter', AsyncMock(return_value=None)):
            await publish_pending(restored, Service._io)
        self.assertIn('這是完整回答', self.chat['history']['messages']['assistant']['content'])
        self.assertTrue(self.chat['history']['messages']['assistant']['done'])
        self.assertEqual(restored.chat_pending(), [])

    async def test_delivery_failure_retries_without_replaying_or_duplicating_job(self):
        self.finish()
        self.events.side_effect = RuntimeError('disconnected')
        await self.publish()
        self.assertEqual(self.queue.chat_pending(), [])
        self.events.side_effect = None
        with patch('company_agent.api.queue.time.time', return_value=time.time()+3):
            await self.publish()
        self.assertEqual(self.queue.chat_pending(), [])
        self.assertEqual(len(self.queue.list('alice')), 1)
        self.assertEqual(self.queue.get('alice', self.key)['status'], 'done')

    async def test_regenerated_alias_receives_result_without_creating_another_job(self):
        alias = self.queue.submit('alice', 'subject', 'chat', 'alias', '請處理', [])
        self.assertEqual(alias, self.key)
        self.chat['history']['messages']['alias'] = {'id': 'alias', 'role': 'assistant', 'content': ''}
        self.finish()
        await self.publish()
        for name in ('assistant', 'alias'):
            self.assertIn('這是完整回答', self.chat['history']['messages'][name]['content'])
        self.assertEqual(len(self.queue.list('alice')), 1)

    async def test_cross_owner_missing_and_unrelated_messages_are_never_overwritten(self):
        original = '使用者已編輯的其他內容'
        self.chat['history']['messages']['assistant']['content'] = original
        self.queue.submit('bob', 'subject-bob', 'chat', 'assistant', '別人的資料', [])
        self.finish()
        await self.publish()
        self.assertEqual(self.chat['history']['messages']['assistant']['content'], original)
        self.events.assert_not_awaited()
        self.assertEqual(self.queue.chat_pending(), [])

    async def test_approval_is_presented_as_waiting_for_user_instead_of_auto_resuming(self):
        self.finish('awaiting_approval', '請先确认行程內容')
        with patch.object(self.queue, 'resume') as resume:
            await self.publish()
        resume.assert_not_called()
        message = self.chat['history']['messages']['assistant']
        self.assertTrue(message['done'])
        self.assertIn('等待您確認', message['content'])
        self.assertIn('/agents?job='+self.key, message['content'])

    async def document(self):
        await self.publish()
        self.key = self.queue.submit_document('alice', 'chat', 'document', [{'id': 'source'}], {}, 'hash')
        self.chat['history']['messages']['document'] = {
            'id': 'document', 'role': 'assistant', 'parentId': 'user', 'content': '',
        }
        return self.chat['history']['messages']['document']

    async def test_document_progress_partial_result_and_downloads_are_delivered(self):
        message = await self.document()
        await self.publish()
        self.assertFalse(message['done'])
        self.queue.update(self.key, status='running', progress='正在處理第 2/3 份公文')
        await self.publish()
        self.assertIn('第 2/3 份', message['content'])
        self.queue.update(self.key, status='partial', document_result=json.dumps({
            'answer': '成功兩份；第三份需要重試',
            'files': [{'id': 'docx', 'name': '公文.docx', 'url': '/api/v1/files/docx/content?attachment=true'}],
        }))
        await self.publish()
        self.assertTrue(message['done'])
        self.assertIn('第三份需要重試', message['content'])
        self.assertIn('/api/v1/files/docx/content?attachment=true', message['content'])
        self.assertEqual(self.queue.chat_pending(), [])

    async def test_existing_document_receipt_is_recovered_after_outbox_restart(self):
        from open_webui.socket import main as socket

        message = await self.document()
        message['content'] = f'公文工作已接收，會在背景執行；\n\n[查看進度](/agents?job={self.key})'
        self.queue.update(self.key, status='done', document_result=json.dumps({'answer': '已完成公文', 'files': []}))
        restored = WorkQueue(self.private, self.base/'uploads')
        with patch.object(socket, 'get_event_emitter', AsyncMock(return_value=None)):
            await publish_pending(restored, Service._io)
        self.assertIn('已完成公文', message['content'])
        self.assertTrue(message['done'])
        self.assertEqual(restored.chat_pending(), [])

    async def test_document_file_changes_invalidate_delivered_revision(self):
        message = await self.document()
        self.queue.update(self.key, status='interrupted', progress='已中斷')
        await self.publish()
        self.assertEqual(self.queue.chat_pending(), [])
        self.queue.update(self.key, document_result=json.dumps({
            'answer': '', 'files': [{'url': '/api/v1/files/kept/content?attachment=true'}],
        }))
        self.assertEqual(len(self.queue.chat_pending()), 1)
        await self.publish()
        self.assertIn('/api/v1/files/kept/content?attachment=true', message['content'])

    async def test_document_receipt_for_another_job_and_edited_answer_are_preserved(self):
        message = await self.document()
        self.events.reset_mock()
        for content in ['使用者已編輯的公文回答', '公文工作已接收，\n[查看](/agents?job=another-job)']:
            message['content'] = content
            self.queue.update(self.key, progress=content)
            await self.publish()
            self.assertEqual(message['content'], content)
        self.events.assert_not_awaited()
