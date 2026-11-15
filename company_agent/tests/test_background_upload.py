"""Durable asynchronous uploads: no network, model calls or real calendar writes."""
from __future__ import annotations

import asyncio
from contextlib import nullcontext
import json
import types
import unittest
from unittest.mock import AsyncMock
from unittest.mock import Mock
from unittest.mock import patch

from pydantic import BaseModel

from company_agent.api.queue import WorkQueue
from company_agent.api.service import Service
from company_agent.tests.test_agent import Fixture
from examples.official_document_workflow.application import background_upload as upload
from examples.shared.runtime import concurrency
from examples.official_document_workflow.infrastructure.storage.processing_cache import upload_outcome


class BackgroundUploadTests(unittest.IsolatedAsyncioTestCase, Fixture):
    def setUp(self):
        super().setUp()
        import sys
        functions = types.ModuleType('open_webui.models.functions')
        functions.Functions = types.SimpleNamespace(get_function_by_id=AsyncMock())
        chats = types.ModuleType('open_webui.models.chats')
        chats.Chats = types.SimpleNamespace(get_chat_by_id_and_user_id=AsyncMock())
        self.enterContext(patch.dict(sys.modules, {functions.__name__: functions, chats.__name__: chats}))
        self.queue = WorkQueue(self.private, self.base / 'uploads')
        self.svc = Service(
            self.queue, AsyncMock(return_value={'id': 'alice', 'role': 'user'}),
            Mock(side_effect=AssertionError('document must not create a company billing client')), None,
        )
        self.file = types.SimpleNamespace(id='source-id', filename='letter.pdf', hash='source-sha', meta={})
        self.function = types.SimpleNamespace(content='approved-code', is_active=True)
        self.saved = []
        self.started, self.release = asyncio.Event(), asyncio.Event()
        self.block_store = False
        outer = self

        class Valves(BaseModel):
            model: str = 'approved-model'
            document_analysis_cache_enabled: bool = False

        class Pipe:
            def __init__(self):
                self.valves = Valves()

            async def owned_sources(self, files, owner):
                if owner != 'alice':
                    raise ValueError('not owned')
                return 'pdf', (outer.file,)

            async def store_output(self, **kwargs):
                outer.started.set()
                if outer.block_store:
                    await outer.release.wait()
                item = types.SimpleNamespace(id='output-id', filename=kwargs['display_name'])
                outer.saved.append(item)
                return item

            async def delete_output(self, item):
                outer.saved.remove(item)

            async def emit_status(self, *args):
                pass

            @concurrency.document_request
            async def pipe(self, body, __user__=None, __files__=None, __event_emitter__=None, __metadata__=None):
                if __metadata__ is not None:
                    return await upload.submit(self, __user__, __metadata__, __event_emitter__)
                await __event_emitter__({'type': 'status', 'data': {'description': '讀取公文中'}})
                await self.store_output(user_id=__user__['id'], display_name='letter.docx', kind='document_docx')
                upload_outcome.get()['status'] = 'done'
                return '完成公文輸出'
        Pipe.Valves = Valves
        self.pipe = Pipe()
        self.factory = lambda: self.pipe
        self.modules = patch.object(
            upload, 'pipe_module',
            side_effect=lambda _: nullcontext(types.SimpleNamespace(Pipe=lambda: self.factory())),
        )
        self.modules.start()
        self.addCleanup(self.modules.stop)
        from open_webui.models.functions import Functions
        self.function_patch = patch.object(
            Functions, 'get_function_by_id',
            AsyncMock(side_effect=lambda _: self.function),
        )
        self.function_patch.start()
        self.addCleanup(self.function_patch.stop)

    async def test_trusted_chat_reply_uses_saved_continuation_without_enqueuing_work(self):
        from open_webui.models.chats import Chats
        chat = {'history': {'currentId': 'answer', 'messages': {
            'question': {'id': 'question', 'role': 'user', 'content': '全部', 'files': []},
            'answer': {'id': 'answer', 'role': 'assistant', 'parentId': 'question'},
        }}}
        self.pipe.clarifications = types.SimpleNamespace(
            scope=Mock(return_value='trusted-scope'), complete=AsyncMock(return_value='continued CSV'),
        )
        with patch.object(Chats, 'get_chat_by_id_and_user_id', AsyncMock(return_value=types.SimpleNamespace(chat=chat))), \
                patch.object(upload, 'verified_user', AsyncMock(return_value={'id': 'alice'})), \
                patch.object(upload, 'configured_service', side_effect=AssertionError('must not enqueue')):
            result = await upload.submit(self.pipe, {'id': 'alice'}, {'chat_id': 'chat', 'message_id': 'answer'})
        self.assertEqual(result, 'continued CSV')
        self.pipe.clarifications.complete.assert_awaited_once_with(
            body={'messages': [{'role': 'user', 'content': '全部'}]}, user_id='alice', scope='trusted-scope', request=None,
        )

    def enqueue(self, request='request'):
        return self.queue.submit_document(
            'alice', 'chat', request, [upload.source_identity(self.file)],
            self.pipe.valves.model_dump(), upload.workflow_revision(self.function.content),
        )

    async def test_core_update_rejects_queued_work_even_with_unchanged_adapter(self):
        key = self.enqueue()
        with patch('examples.official_document_workflow.services.version.implementation_revision', return_value='changed-core'):
            await self.svc._run_claimed(self.queue.get('alice', key))
        self.assertEqual(self.queue.get('alice', key)['status'], 'error')
        self.assertFalse(self.started.is_set())

    async def test_submission_enqueues_before_document_slot_and_disconnect_preserves_owned_job(self):
        from open_webui.models.chats import Chats
        chat = {
            'history': {
                'messages': {
                    'u': {'id': 'u', 'role': 'user', 'content': '', 'files': [{'id': 'source-id'}]},
                    'a': {'id': 'a', 'role': 'assistant', 'parentId': 'u'},
                },
            },
        }
        with patch.object(upload, 'verified_user', AsyncMock(return_value={'id': 'alice'})), \
                patch.object(upload, 'configured_service', return_value=self.svc), \
                patch.object(Chats, 'get_chat_by_id_and_user_id', AsyncMock(return_value=types.SimpleNamespace(chat=chat))), \
                patch.object(concurrency, 'DOCUMENTS', concurrency.OwnerSlots(1)):
            async with concurrency.DOCUMENTS.slot('busy'):
                accepted = asyncio.Event()

                async def emit(event):
                    accepted.set()

                task = asyncio.create_task(self.pipe.pipe(
                        {'messages': [{'content': 'forged'}]}, __user__={'id': 'alice'},
                        __files__=[{'id': 'forged-file'}],
                        __metadata__={'chat_id': 'chat', 'message_id': 'a'},
                        __event_emitter__=emit,
                    ))
                try:
                    await asyncio.wait_for(accepted.wait(), 2)
                    self.assertFalse(task.done())
                    self.assertEqual(len(self.queue.list('alice')), 1)
                finally:
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
        job = self.queue.list('alice')[0]
        self.assertEqual(job['status'], 'queued')
        self.assertEqual(self.queue.get('alice', job['id'])['payload']['sources'], [upload.source_identity(self.file)])
        self.assertFalse(self.started.is_set())
        self.svc.model_factory.assert_not_called()

    async def test_chat_observer_returns_document_result_and_downloads_without_replaying(self):
        from company_agent.api.pipe import wait_for_result

        key = self.enqueue()
        events = AsyncMock()
        task = asyncio.create_task(wait_for_result(self.svc, 'alice', key, events))
        try:
            async with asyncio.timeout(3):
                while not events.await_count:
                    await asyncio.sleep(.01)
            self.assertFalse(task.done())
            await self.svc._run_claimed(self.queue.claim())
            result = await asyncio.wait_for(task, 3)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assertIn('完成公文輸出', result)
        self.assertIn('/api/v1/files/output-id/content?attachment=true', result)
        self.assertTrue(events.await_args.args[0]['data']['done'])
        self.assertEqual(len(self.saved), 1)
        self.assertEqual(len(self.queue.list('alice')), 1)
        self.svc.model_factory.assert_not_called()

    async def test_duplicate_and_regeneration_attach_to_one_job_and_setting_changes_reject(self):
        key = self.enqueue()
        self.assertEqual(self.enqueue(), key)
        self.assertEqual(self.enqueue('regenerated'), key)
        self.assertEqual(self.queue.get('alice', key)['request_aliases'], ['regenerated'])
        self.pipe.valves.model = 'changed'
        with self.assertRaises(ValueError):
            self.enqueue()
        self.assertEqual(len(self.queue.list('alice')), 1)

    async def test_available_document_lane_cannot_jump_older_company_work_for_same_owner(self):
        first = self.queue.submit('alice', 'subject-alice', 'chat', 'first', 'company work', [])
        second = self.enqueue('second')
        other = self.queue.submit_document('bob', 'chat', 'other', [{'id': 'input'}], {}, 'hash')
        # Company capacity is occupied by another owner. Bob can use the
        # document lane, while Alice's later PDF waits for her earlier work.
        self.assertEqual(self.queue.claim(kinds=['document'])['id'], other)
        self.assertIsNone(self.queue.claim(kinds=['document']))
        self.assertEqual(self.queue.claim(kinds=['company'])['id'], first)
        self.queue.update(first, status='done')
        self.assertEqual(self.queue.claim(kinds=['document'])['id'], second)

    async def test_background_completion_survives_queue_reconstruction_and_owner_isolation(self):
        key = self.enqueue()
        await self.svc._run_claimed(self.queue.claim())
        restored = WorkQueue(self.private, self.base/'uploads')
        result = restored.public('alice', key)
        self.assertEqual(result['kind'], 'document')
        self.assertEqual(result['status'], 'done')
        self.assertEqual(result['answer'], '完成公文輸出')
        self.assertEqual(result['files'][0]['url'], '/api/v1/files/output-id/content?attachment=true')
        self.assertFalse(result['resumable'])
        self.assertIsNone(upload_outcome.get())
        with self.assertRaises(ValueError):
            restored.public('bob', key)
        self.svc.model_factory.assert_not_called()

    async def test_background_document_binds_the_same_app_content_cache_as_eip(self):
        from examples.official_document_workflow.runtime import current_resources
        self.pipe.valves.document_analysis_cache_enabled = True
        key = self.enqueue('shared-content')
        await self.svc._run_claimed(self.queue.claim())
        result = self.queue.public('alice', key)
        self.assertEqual(result['status'], 'done', result)
        self.assertIs(self.pipe.content_cache, current_resources().shared_content)
        self.assertIs(self.pipe.evidence_cache, current_resources().shared_content)
        self.svc.model_factory.assert_not_called()

    async def test_native_pdf_pipeline_records_pdf_word_csv_and_typed_completion(self):
        from tests.test_official_upload_groups import UploadGroupTests, DEADLINE_EVIDENCE, payload
        fixture = UploadGroupTests()
        fixture.addCleanup = self.addCleanup
        fixture.setUp()
        fixture.add()
        fixture.add(DEADLINE_EVIDENCE)
        fixture.pipe.analyze_text = AsyncMock(side_effect=fixture.responses([['S1', 'S2']], payload()))
        self.factory = lambda: fixture.pipe
        self.pipe = fixture.pipe
        sources = [upload.source_identity(item.source_file) for item in fixture.prepared]
        key = self.queue.submit_document(
            'alice', 'chat', 'native-pdf', sources,
            fixture.pipe.valves.model_dump(), upload.workflow_revision(self.function.content),
        )
        await self.svc._run_claimed(self.queue.claim())
        result = self.queue.public('alice', key)
        self.assertEqual(result['status'], 'done', result)
        self.assertIn('整理為 1 組', result['answer'])
        self.assertEqual(
            [file['kind'] for file in result['files']],
            ['document_pdf', 'document_docx', 'document_pdf', 'document_docx', 'calendar_csv'],
        )
        self.assertEqual(len(fixture.rows()), 1)
        self.assertTrue(all(values['user_id'] == 'alice' for values in fixture.saved))
        self.assertIsNone(upload_outcome.get())
        self.svc.model_factory.assert_not_called()

    async def test_changed_code_and_revoked_file_stop_before_any_processing(self):
        for mode in ['code', 'file']:
            with self.subTest(mode=mode):
                self.function.content = 'approved-code'
                self.file.hash = 'source-sha'
                key = self.enqueue(mode)
                if mode == 'code':
                    self.function.content = 'changed-code'
                else:
                    self.file.hash = 'changed-sha'
                await self.svc._run_claimed(self.queue.claim())
                result = self.svc.result('alice', key)
                self.assertEqual(result['status'], 'error')
                self.assertFalse(result['files'])
                self.assertIn('已', result['progress'])
        self.assertFalse(self.started.is_set())

    async def test_cancel_acknowledges_immediately_and_preserves_completed_storage_write(self):
        key = self.enqueue()
        row = self.queue.claim()
        self.block_store = True
        task = asyncio.create_task(self.svc._run_claimed(row))
        self.svc.executions[('alice', key)] = task
        self.svc.execution_kinds[('alice', key)] = 'document'
        await asyncio.wait_for(self.started.wait(), 3)
        try:
            async with asyncio.timeout(2):
                await self.svc.cancel('alice', key, wait=False)
            self.assertFalse(task.done())
            self.assertIn('正在取消', self.queue.public('alice', key)['progress'])
            self.release.set()
            await asyncio.wait_for(task, 3)
            result = self.queue.public('alice', key)
            self.assertEqual(result['status'], 'cancelled')
            self.assertEqual(result['files'][0]['id'], 'output-id')
        finally:
            self.release.set()
            await asyncio.gather(task, return_exceptions=True)
        self.assertFalse(concurrency.DOCUMENTS.active)

    async def test_interrupted_document_keeps_files_without_automatic_replay(self):
        key = self.enqueue()
        self.queue.claim()
        self.queue.update(key, document_result=json.dumps({'files': [{'id': 'kept', 'name': 'kept.docx'}]}))
        self.queue.recover_running()
        result = self.queue.public('alice', key)
        self.assertEqual(result['status'], 'interrupted')
        self.assertEqual(result['files'][0]['id'], 'kept')
        self.assertIsNone(self.queue.claim())
        self.assertFalse(self.started.is_set())

    async def test_company_and_document_lanes_admit_independently_and_serialize_same_owner(self):
        documents = [
            self.queue.submit_document(owner, 'chat', 'd', [{'id': 'input'}], {}, 'hash')
            for owner in ['d1', 'd2', 'd3']
        ]
        company = [
            self.queue.submit(owner, 'sub-'+owner, 'chat', 'c', 'company work', [])
            for owner in ['c1', 'c2', 'd1']
        ]
        started = []
        release = asyncio.Event()
        service = Service(self.queue, None, None, None, max_concurrent=2)

        async def execute(row):
            started.append((row['owner'], row['kind']))
            await release.wait()
            self.queue.update(row['id'], status='done')

        async def document(svc, row):
            await execute(row)
        with patch.object(concurrency, 'DOCUMENTS', concurrency.OwnerSlots(2)), \
                patch.object(service, 'execute', side_effect=execute), \
                patch.object(upload, 'execute', side_effect=document):
            task = asyncio.create_task(service.loop())
            try:
                async with asyncio.timeout(5):
                    while len(started) < 4:
                        await asyncio.sleep(.01)
                self.assertCountEqual(
                    started, [
                        ('d1', 'document'), ('d2', 'document'),
                        ('c1', 'company'), ('c2', 'company'),
                    ],
                )
                self.assertEqual(self.queue.get('d1', company[2])['status'], 'queued')
                self.assertEqual(self.queue.get('d3', documents[2])['status'], 'queued')
                release.set()
                async with asyncio.timeout(5):
                    while service.executions or len(started) < 6:
                        await asyncio.sleep(.01)
            finally:
                release.set()
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        self.assertEqual(len(started), 6)
