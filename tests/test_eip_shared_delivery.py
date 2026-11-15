"""Real shared persistence, private EIP workflows; no external OCR/model/write."""
from __future__ import annotations

import asyncio
import io
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import Mock
from unittest.mock import patch

from . import test_official_upload_groups as uploads
from .eip_postgres import EIPPostgresTestCase
from .test_eip_group_processing import payload
from .test_official_document_processor_pipe import PIPE_MODULE as M
from .test_official_document_processor_pipe import SOURCE
from examples.official_document_workflow.application import integration
from examples.official_document_workflow.domain.calendar.relative_dates import (
    ReceiptContext,
)
from examples.official_document_workflow.domain.workflow import JobPlan
from examples.shared.runtime.concurrency import model_request
from examples.shared.runtime.concurrency import ModelNotStarted
from examples.shared.runtime.modules import ModuleReference

_docx = ModuleReference('docx')
_pymupdf = ModuleReference('pymupdf')


def content_payload():
    value = payload(('S1',))
    value['document'].pop('calendar_subject', None)
    value['document']['evidence'].pop('calendar_subject', None)
    for name in (
        'title', 'action', 'project_name', 'location', 'responsible_party', 'relevance',
        'start_date', 'start_time', 'end_date', 'end_time', 'anchor_date',
    ):
        value['events'][0]['evidence_spans'].setdefault(name, [])
    return value


class SharedEIPDeliveryTests(EIPPostgresTestCase, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        pymupdf = _pymupdf.load()
        Document = _docx.attribute('Document')

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        with pymupdf.open() as pdf:
            pdf.new_page().insert_text((30, 30), 'Shared official document')
            source = pdf.tobytes()
        word = io.BytesIO()
        Document().save(word)
        self.render = Mock(return_value=word.getvalue())
        self.spotting = Mock(return_value={})
        self.pipes, self.sources, self.contexts = [], [], []
        for owner, day in (('alice', '2026-09-01'), ('bob', '2026-09-02')):
            path = self.directory / f'{owner}.pdf'
            path.write_bytes(source)
            file = SimpleNamespace(id=str(uuid.uuid4()), filename=owner + '-original.pdf')
            prepared = M.PreparedDocument(kind='pdf', source_files=(file,), pdf_path=path)
            pipe = M.Pipe()

            async def owned(requests, actual_owner, *, expected_owner=owner, file=file):
                if actual_owner != expected_owner or requests != [{'id': file.id, 'type': 'file'}]:
                    raise ValueError('本人來源不存在或未授權')
                return 'pdf', (file,)

            pipe.owned_sources = AsyncMock(side_effect=owned)
            pipe.prepare_owned_source_documents = AsyncMock(return_value=(prepared,))
            pipe.plan_current_user_request = AsyncMock(return_value=JobPlan())
            pipe.ocr_context = AsyncMock(return_value=((), None))
            pipe.semantic_source = Mock(return_value=M.CloudSemanticSource(SOURCE, 'native_pdf_text'))
            pipe.analyze_text = AsyncMock(side_effect=lambda *a, **kw: content_payload())
            pipe.paddleocr_vl_spotting_response = self.spotting
            pipe.render_word = self.render

            async def save(**kwargs):
                return SimpleNamespace(id=str(uuid.uuid4()), filename=kwargs['display_name'])

            pipe.store_output = AsyncMock(side_effect=save)
            self.pipes.append(pipe)
            self.sources.append(file)
            self.contexts.append(ReceiptContext(owner + '-flow', 'reference', day))
        review = patch(
            'examples.official_document_workflow.application.multi_agent.review_calendar',
            AsyncMock(return_value={'approved': True, 'checks': [], 'issues': []}),
        )
        review.start()
        self.addCleanup(review.stop)

    async def run_for(self, index):
        owner = ('alice', 'bob')[index]
        source = self.sources[index]
        return await integration.process(
            owner, [source.id], owner + ' private instruction', None,
            groups=[[source.id]], date_context=self.contexts[index],
        )

    async def test_concurrent_owners_share_analysis_and_word_but_keep_personal_work_and_downloads_private(self):
        with patch.object(integration, 'configured_pipe', AsyncMock(side_effect=self.pipes)):
            results = await asyncio.gather(self.run_for(0), self.run_for(1))
        self.assertEqual(self.render.call_count, 1)
        self.assertEqual(self.spotting.call_count, 0)
        output_ids = []
        for index, result in enumerate(results):
            self.assertEqual(result['status'], 'done', result['errors'])
            self.assertTrue(result['calendar_ready'])
            pipe = self.pipes[index]
            self.assertEqual(
                pipe.plan_current_user_request.await_args.args[0]['messages'][0]['content'],
                ('alice', 'bob')[index] + ' private instruction',
            )
            calls = [call.kwargs for call in pipe.store_output.await_args_list]
            self.assertEqual({call['user_id'] for call in calls}, {('alice', 'bob')[index]})
            self.assertEqual({call['source_file_id'] for call in calls}, {self.sources[index].id})
            self.assertEqual({call['kind'] for call in calls}, {'document_pdf', 'document_docx', 'calendar_csv'})
            for call in calls:
                self.assertEqual(call['extra_metadata']['eip_group_source_ids'], [self.sources[index].id])
            output_ids.append({file['id'] for file in result['files']})
        self.assertFalse(output_ids[0] & output_ids[1])
        self.assertEqual(sum(pipe.analyze_text.await_count for pipe in self.pipes), 1)
        producer = next(pipe for pipe in self.pipes if pipe.analyze_text.await_count)
        request = json.loads(producer.analyze_text.await_args.args[0].text)
        self.assertNotIn('eip_receipt_context', request)
        self.assertNotIn('alice-original', str(request))
        self.assertNotIn('bob-original', str(request))

    async def test_warm_cache_does_not_bypass_owner_authorization(self):
        with patch.object(integration, 'configured_pipe', AsyncMock(return_value=self.pipes[0])):
            await self.run_for(0)
        self.pipes[1].owned_sources.side_effect = ValueError('本人來源已移除')
        with patch.object(integration, 'configured_pipe', AsyncMock(return_value=self.pipes[1])):
            with self.assertRaisesRegex(ValueError, '本人來源已移除'):
                await self.run_for(1)
        self.assertEqual(self.render.call_count, 1)
        self.assertEqual(self.spotting.call_count, 0)
        self.pipes[1].prepare_owned_source_documents.assert_not_awaited()
        self.pipes[1].analyze_text.assert_not_awaited()
        self.pipes[1].store_output.assert_not_awaited()

    async def test_pdf_upload_then_eip_reuses_native_word_with_private_outputs(self):
        fixture = uploads.UploadGroupTests()
        fixture.addCleanup = self.addCleanup
        fixture.setUp()
        prepared = fixture.add()
        fixture.pipe.valves.document_analysis_cache_enabled = True
        fixture.pipe.analyze_text = AsyncMock(side_effect=fixture.responses([['S1']], payload(('S1',))))
        fixture.pipe.render_word = self.render
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(workflow=self._workflow)))
        answer = await fixture.pipe.pipe(
            {'messages': []}, __user__={'id': 'alice'}, __files__=[{'id': prepared.source_file.id}],
            __request__=request,
        )
        self.assertIn('/eip?csv=', answer)
        self.assertEqual(self.render.call_count, 1)
        fixture.pipe.paddleocr_vl_spotting_response.assert_not_called()
        self.assertEqual({item['kind'] for item in fixture.saved}, {'document_pdf', 'document_docx', 'calendar_csv'})
        self.assertTrue(all(item['user_id'] == 'alice' for item in fixture.saved))

        bob = self.pipes[1]
        bob_prepared = bob.prepare_owned_source_documents.return_value[0]
        bob_prepared.pdf_path.write_bytes(prepared.pdf_path.read_bytes())
        with patch.object(integration, 'configured_pipe', AsyncMock(return_value=bob)):
            result = await self.run_for(1)
        self.assertEqual(result['status'], 'done', result['errors'])
        self.assertTrue(result['calendar_ready'])
        self.assertEqual(self.render.call_count, 1)
        self.spotting.assert_not_called()
        bob.analyze_text.assert_awaited_once()
        self.assertTrue(all(call.kwargs['user_id'] == 'bob' for call in bob.store_output.await_args_list))
        uploaded_word = next(item['payload'] for item in fixture.saved if item['kind'] == 'document_docx')
        eip_word = next(
            call.kwargs['payload'] for call in bob.store_output.await_args_list
            if call.kwargs['kind'] == 'document_docx'
        )
        self.assertEqual(uploaded_word, eip_word)

    async def test_rejected_owner_before_provider_does_not_permanently_block_shared_content(self):
        cache = self._workflow.shared_content
        key = cache.key('evidence-preflight-test')
        sent = []

        class Worker:
            def __init__(self, owner):
                self._usage_owner = owner

            @model_request
            async def scan(self):
                sent.append(self._usage_owner)
                return {'verified': True}

        async def identity(owner):
            return {
                'id': owner, 'role': 'pending' if owner == 'alice' else 'user',
                'oauth': {'oidc': {'sub': owner + '-subject'}},
            }

        with patch('examples.identity.adapters.webui.verified_user', AsyncMock(side_effect=identity)):
            with self.assertRaises(ModelNotStarted):
                await cache.aget_or_compute(key, Worker('alice').scan, replay_safe=False)
            self.assertEqual(sent, [])
            with self._workflow.store.db() as db:
                row = db.execute('SELECT state FROM shared_content_cache WHERE key=%s', (key,)).fetchone()
                self.assertEqual(row['state'], 'failed')
                db.execute("UPDATE shared_content_cache SET expires_at=CURRENT_TIMESTAMP-INTERVAL '1 second' WHERE key=%s", (key,))
            self.assertEqual(await cache.aget_or_compute(key, Worker('bob').scan, replay_safe=False), {'verified': True})
        self.assertEqual(sent, ['bob'])
