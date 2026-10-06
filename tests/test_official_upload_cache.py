"""Real encrypted persistence and chat validation; only model HTTP is mocked."""
from __future__ import annotations

import asyncio
import copy
import json
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock
from unittest.mock import patch

import httpx

from . import test_official_upload_groups as fixtures
from .eip_postgres import EIPPostgresTestCase
from .test_eip_group_processing import payload
from .test_official_document_processor_pipe import PIPE_MODULE as M
from official_document_workflow.infrastructure.storage.automation_store import AutomationStore
from official_document_workflow.infrastructure.storage.processing_cache import active_requests


class UploadCacheTests(EIPPostgresTestCase, unittest.IsolatedAsyncioTestCase):
    add = fixtures.UploadGroupTests.add
    grouping = fixtures.UploadGroupTests.grouping
    responses = fixtures.UploadGroupTests.responses
    unnamed_attachments = staticmethod(fixtures.UploadGroupTests.unnamed_attachments)

    def setUp(self):
        super().setUp()
        fixtures.UploadGroupTests.setUp(self)
        self.pipe.valves.document_analysis_cache_enabled = True
        self.db = AutomationStore(Path(self.tmp.name) / 'key')
        p = patch('official_document_workflow.infrastructure.storage.automation_store.store', return_value=self.db)
        p.start()
        self.addCleanup(p.stop)
        self.client = AsyncMock()
        self.client.__aenter__.return_value = self.client
        p = patch.object(M.httpx, 'AsyncClient', return_value=self.client)
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(self.pipe, '_official_document_litellm_connection', return_value=('http://local/v1', 'secret'))
        p.start()
        self.addCleanup(p.stop)
        self.add()
        self.answer = self.responses([['S1']], payload(('S1',)))[0]
        self.client.post.side_effect = lambda *a, **kw: httpx.Response(
            200, json={'status': 'completed', 'output_text': json.dumps(self.answer)},
        )

    async def run_pipe(self, owner='owner'):
        return await self.pipe.pipe(
            {'messages': [{'role': 'user', 'content': 'test'}]},
            __user__={'id': owner}, __files__=[{'id': p.source_file.id} for p in self.prepared],
        )

    async def test_reupload_new_id_reuses_result_but_exports_current_source_ids(self):
        first = await self.run_pipe()
        self.assertIn('整理為 1 組', first)
        self.assertEqual(self.client.post.await_count, 1)
        self.prepared[0].source_file.id = 'new-upload-id'
        self.saved.clear()
        second = await self.run_pipe()
        self.assertIn('整理為 1 組', second)
        self.assertEqual(self.client.post.await_count, 1)
        self.assertTrue(
            all(
                f['extra_metadata']['eip_group_source_ids'] == ['new-upload-id']
                for f in self.saved if f['kind'] != 'calendar_csv'
            ),
        )
        self.assertEqual(self.pipe._get_owned_source_files.await_count, 2)
        self.assertIsNone(active_requests.get())
        with self.db.db() as db:
            rows = db.execute('SELECT owner,payload FROM processing_chunks').fetchall()
        self.assertTrue(rows)
        self.assertTrue(all(r['owner'] == 'owner' and b'1153022027' not in bytes(r['payload']) for r in rows))

    async def test_other_owner_uses_separate_result(self):
        await self.run_pipe('owner')
        await self.run_pipe('second-user')
        self.assertEqual(self.client.post.await_count, 2)
        await self.run_pipe('owner')
        self.assertEqual(self.client.post.await_count, 2)

    async def test_concurrent_other_owners_keep_cache_context_and_exports_separate(self):
        both = asyncio.Event()
        owners = []

        async def slow(*args, **kwargs):
            owners.append(active_requests.get().backend.owner)
            if len(owners) == 2:
                both.set()
            await both.wait()
            return httpx.Response(200, json={'status': 'completed', 'output_text': json.dumps(self.answer)})
        self.client.post.side_effect = slow
        async with asyncio.timeout(5):
            results = await asyncio.gather(self.run_pipe('alice'), self.run_pipe('bob'))
        self.assertTrue(all('整理為 1 組' in result for result in results))
        self.assertCountEqual(owners, ['alice', 'bob'])
        self.assertIsNone(active_requests.get())
        with self.db.db() as db:
            rows = db.execute('SELECT owner,payload FROM processing_chunks').fetchall()
        self.assertEqual({row['owner'] for row in rows}, {'alice', 'bob'})
        for row in rows:
            self.assertTrue(self.db.decode(row['owner'], row['payload']))

    async def test_same_batch_concurrently_calls_model_only_once(self):
        async def slow(*args, **kwargs):
            await asyncio.sleep(.05)
            return httpx.Response(200, json={'status': 'completed', 'output_text': json.dumps(self.answer)})
        self.client.post.side_effect = slow
        results = await asyncio.gather(self.run_pipe(), self.run_pipe())
        self.assertTrue(all('整理為 1 組' in result for result in results))
        self.assertEqual(self.client.post.await_count, 1)

    async def test_changed_model_settings_prompt_or_source_bypass_saved_response(self):
        await self.run_pipe()
        self.pipe.valves.cloud_ocr_text_reasoning_effort = 'medium'
        await self.run_pipe()
        self.pipe.valves.cloud_ocr_text_model = 'gemini-3.5-flash-lite'
        await self.run_pipe()
        with patch('official_document_workflow.application.upload_batch.GROUPING_PROMPT', '新版分組規則'):
            await self.run_pipe()
        path = self.prepared[0].pdf_path
        old = self.source_by_path[path]
        self.source_by_path[path] = M.CloudSemanticSource(old.text + '\n修訂文字', old.kind, old.structural_lines)
        await self.run_pipe()
        self.assertEqual(self.client.post.await_count, 5)

    async def test_changed_file_bytes_and_preparation_date_invalidate_result(self):
        self.pipe.valves.calendar_ai_preparation_reminders = False
        await self.run_pipe()
        path = self.prepared[0].pdf_path
        path.write_bytes(path.read_bytes() + b'\n% revised PDF metadata\n')
        await self.run_pipe()
        self.pipe.valves.calendar_ai_preparation_reminders = True
        with patch('official_document_workflow.application.upload_batch.datetime') as clock:
            clock.now.side_effect = [datetime(2026, 9, 23), datetime(2026, 9, 24)]
            await self.run_pipe()
            await self.run_pipe()
        self.assertEqual(self.client.post.await_count, 4)

    async def test_calendar_pending_keeps_verified_document_result_for_reruns(self):
        self.answer['analysis']['groups'][0]['analysis']['events'][0]['action'] = None
        first = await self.run_pipe()
        second = await self.run_pipe()
        self.assertIn('缺少具體工作', first)
        self.assertIn('缺少具體工作', second)
        self.assertNotIn('日期主旨待確認', second)
        self.assertFalse(any(f['kind'] == 'calendar_csv' for f in self.saved))
        self.assertEqual(self.client.post.await_count, 1)

    async def test_file_permission_is_checked_before_reading_any_cached_result(self):
        await self.run_pipe()
        self.pipe._get_owned_source_files.side_effect = M.ProcessingError('無法存取附件')
        with patch('official_document_workflow.infrastructure.storage.automation_store.store') as store:
            answer = await self.run_pipe()
        self.assertIn('無法存取附件', answer)
        store.assert_not_called()
        self.assertEqual(self.client.post.await_count, 1)

    async def test_office_cache_uses_original_bytes_not_conversion_timestamps(self):
        original = Path(self.tmp.name) / 'source.docx'
        original.write_bytes(b'original-office-upload')
        self.prepared[0] = replace(self.prepared[0], kind='office', original_path=original)
        await self.run_pipe()
        pdf = self.prepared[0].pdf_path
        pdf.write_bytes(pdf.read_bytes() + b'\n% regenerated conversion metadata\n')
        await self.run_pipe()
        self.assertEqual(self.client.post.await_count, 1)
        original.write_bytes(b'changed-office-upload')
        await self.run_pipe()
        self.assertEqual(self.client.post.await_count, 2)

    async def test_invalid_grouping_is_not_persisted(self):
        good = copy.deepcopy(self.answer)
        self.answer['grouping']['groups'][0]['main_pages'] = ['S9:P1']
        await self.run_pipe()
        self.answer = good
        answer = await self.run_pipe()
        self.assertIn('整理為 1 組', answer)
        self.assertEqual(self.client.post.await_count, 2)

    async def test_cancellation_releases_lock_and_does_not_cache_partial_work(self):
        started = asyncio.Event()

        async def wait(*args, **kwargs):
            started.set()
            await asyncio.Event().wait()
        self.client.post.side_effect = wait
        task = asyncio.create_task(self.run_pipe())
        await asyncio.wait_for(started.wait(), timeout=3)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.client.post.side_effect = lambda *a, **kw: httpx.Response(
            200, json={
                'status': 'completed', 'output_text': json.dumps(self.answer),
            },
        )
        result = await asyncio.wait_for(self.run_pipe(), timeout=3)
        self.assertIn('整理為 1 組', result)
        self.assertEqual(self.client.post.await_count, 2)

    async def test_invalid_evidence_and_cloud_error_are_not_persisted(self):
        original = copy.deepcopy(self.answer)
        self.answer['analysis']['groups'][0]['analysis']['document']['evidence']['subject'] = '無原文依據'
        await self.run_pipe()
        self.answer = original
        self.client.post.side_effect = [
            httpx.Response(429), httpx.Response(
                200, json={
                    'status': 'completed', 'output_text': json.dumps(self.answer),
                },
            ),
        ]
        await self.run_pipe()
        answer = await self.run_pipe()
        self.assertIn('整理為 1 組', answer)
        self.assertEqual(self.client.post.await_count, 3)
        await self.run_pipe()
        self.assertEqual(self.client.post.await_count, 3)

    async def test_cached_response_is_still_validated_against_current_source(self):
        await self.run_pipe()
        # A validation rule change must not make stored model output trusted.
        with patch.object(self.pipe, '_cloud_document_from_payload', side_effect=ValueError('來源證據不符')):
            answer = await self.run_pipe()
        self.assertIn('來源證據不符', answer)
        self.assertIn('日期主旨待確認', answer)
        self.assertEqual(self.client.post.await_count, 1)

    async def test_disabled_cache_forces_analysis_without_overwriting_saved_result(self):
        await self.run_pipe()
        self.pipe.valves.document_analysis_cache_enabled = False
        await self.run_pipe()
        await self.run_pipe()
        self.assertEqual(self.client.post.await_count, 3)
        self.pipe.valves.document_analysis_cache_enabled = True
        await self.run_pipe()
        self.assertEqual(self.client.post.await_count, 3)

    async def test_background_tasks_do_not_open_cache_or_send_model_requests(self):
        with patch('official_document_workflow.infrastructure.storage.automation_store.store') as store:
            answer = await self.pipe.pipe({}, __user__={'id': 'owner'}, __task__='title_generation')
        self.assertEqual(json.loads(answer), {'title': ''})
        store.assert_not_called()
        self.client.post.assert_not_awaited()
