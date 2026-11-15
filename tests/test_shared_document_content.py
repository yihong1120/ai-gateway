"""Real PG coordination for reusable content; no model, GPU or file service."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from docx import Document
import pymupdf
from PIL import Image, ImageDraw

from .eip_postgres import EIPPostgresTestCase
from .test_eip_large_sources import answer_for
from .test_official_document_processor_pipe import PIPE_MODULE as M
from examples.official_document_workflow.application.long_source import group_text
from examples.official_document_workflow.application.multi_agent import GroupExporter
from examples.official_document_workflow.application.outcomes import DocumentResult, CalendarResult, GroupResult
from examples.official_document_workflow.application.upload_batch import UploadBatch, DocumentGroup, group_pdf
from examples.official_document_workflow.domain.documents import OutputUncertain
from examples.official_document_workflow.domain.validation import ContentValidationError
from examples.official_document_workflow.domain.workflow import JobPlan
from examples.official_document_workflow.infrastructure.storage.automation_store import AutomationStore
from examples.official_document_workflow.infrastructure.storage.shared_content import SharedContentCache


class SharedDocumentContentTests(EIPPostgresTestCase, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.db = AutomationStore(self.root / 'key')
        self.shared = SharedContentCache(self.db, 'eip', 'consumer-tests-v1')
        self.source = self.root / 'source.pdf'
        self.source.write_bytes(b'%PDF-synthetic-owned-content')
        self.response = {'errorCode': 0, 'result': {
            'layoutParsingResults': [{'prunedResult': {'page_index': 0}}],
        }}
        document = Document()
        document.add_paragraph('Synthetic shared document')
        stream = io.BytesIO()
        document.save(stream)
        self.word = stream.getvalue()

    def pipe(self, owner):
        pipe = M.Pipe()
        pipe.content_cache, pipe.evidence_cache = self.shared, self.shared
        pipe._usage_owner = owner
        pipe.valves.eip_large_sources = True
        pipe.transport._official_document_litellm_connection = MagicMock(
            return_value=('http://127.0.0.1:4000/v1', 'synthetic-service-key'),
        )
        return pipe

    async def recognize(self, pipe, *, layout=True, prompt=None, path=None):
        return await asyncio.to_thread(
            pipe.ocr._paddleocr_vl_checkpointed_request, path or self.source,
            pages=1, use_layout_detection=layout, prompt_label=prompt,
        )

    async def test_two_owners_share_one_ocr_result_without_private_checkpoint_reads(self):
        alice, bob = self.pipe('alice'), self.pipe('bob')
        request = MagicMock(side_effect=lambda *args, **kwargs: copy.deepcopy(self.response))
        for pipe in (alice, bob):
            pipe.ocr._paddleocr_vl_single_request = request
            pipe._eip_processing_cache = MagicMock()
        bob_source = self.root / 'bob-private-upload.pdf'
        bob_source.write_bytes(self.source.read_bytes())
        first, second = await asyncio.gather(self.recognize(alice), self.recognize(bob, path=bob_source))
        self.assertEqual(first, second)
        request.assert_called_once()
        alice._eip_processing_cache.get.assert_not_called()
        bob._eip_processing_cache.get.assert_not_called()

    async def test_ocr_mode_parameters_and_contract_revision_invalidate_content(self):
        pipe = self.pipe('alice')
        request = pipe.ocr._paddleocr_vl_single_request = MagicMock(return_value=self.response)
        await self.recognize(pipe)
        await self.recognize(pipe)
        await self.recognize(pipe, layout=False, prompt='spotting')
        pipe.valves.paddleocr_vl_max_new_tokens += 256
        await self.recognize(pipe)
        with patch('examples.official_document_workflow.services.ocr.OCR_CONTENT_REVISION', 'new-contract'):
            await self.recognize(pipe)
        pipe.valves.paddleocr_vl_base_url = 'http://127.0.0.1:8114'
        await self.recognize(pipe)
        self.assertEqual(request.call_count, 5)

    async def test_invalid_shared_ocr_page_order_is_rebuilt_before_returning(self):
        pipe = self.pipe('alice')
        digest = hashlib.sha256(self.source.read_bytes()).hexdigest()
        key = self.shared.key('ocr-chunk', *pipe.ocr.content_identity(), digest, 1, True, None)
        invalid = copy.deepcopy(self.response)
        invalid['result']['layoutParsingResults'][0]['prunedResult']['page_index'] = 9
        await asyncio.to_thread(self.shared.get_or_compute, key, lambda: invalid)
        pipe.ocr._paddleocr_vl_single_request = MagicMock(return_value=self.response)
        self.assertEqual(await self.recognize(pipe), self.response)
        self.assertEqual(await self.recognize(pipe), self.response)
        pipe.ocr._paddleocr_vl_single_request.assert_called_once()

    async def test_native_semantic_text_is_read_once_across_owners_and_preserves_line_provenance(self):
        with pymupdf.open() as document:
            document.new_page().insert_text((72, 72), 'First original native line')
            document.new_page().insert_text((72, 72), 'Second original native line')
            self.source.write_bytes(document.tobytes())
        bob_source = self.root / 'bob-copy.pdf'
        bob_source.write_bytes(self.source.read_bytes())
        alice, bob = self.pipe('alice'), self.pipe('bob')
        extract = MagicMock(wraps=alice.ocr._run_text)
        alice.ocr._run_text, bob.ocr._run_text = extract, extract
        first = await asyncio.to_thread(alice.ocr.semantic_source, self.source, (), None)
        second = await asyncio.to_thread(bob.ocr.semantic_source, bob_source, (), None)
        self.assertEqual(first, second)
        self.assertEqual(first.kind, 'native_pdf_text')
        self.assertIn('First original native line', first.text)
        self.assertIn('Second original native line', first.text)
        self.assertTrue(first.structural_lines)
        self.assertTrue(all(first.text.splitlines()[number - 1].startswith('<') for number in first.structural_lines))
        extract.assert_called_once()

    async def test_native_text_limits_apply_after_cache_hits_without_poisoning_shared_content(self):
        alice, bob = self.pipe('alice'), self.pipe('bob')
        alice.valves.eip_large_sources = False
        alice.valves.cloud_ocr_text_max_input_characters = 1000
        extract = MagicMock(return_value='Original raw source ' + 'x' * 1500 + '\f')
        alice.ocr._run_text, bob.ocr._run_text = extract, extract
        with self.assertRaises(M.ProcessingError):
            await asyncio.to_thread(alice.ocr.semantic_source, self.source, (), None)
        result = await asyncio.to_thread(bob.ocr.semantic_source, self.source, (), None)
        self.assertIn('Original raw source', result.text)
        with self.assertRaises(M.ProcessingError):
            await asyncio.to_thread(alice.ocr.semantic_source, self.source, (), None)
        extract.assert_called_once()

    async def test_invalid_native_text_cache_is_rebuilt_with_valid_structural_lines(self):
        pipe = self.pipe('alice')
        pipe.ocr._run_text = MagicMock(return_value='Original raw source\f')
        with patch.object(self.shared, 'key', wraps=self.shared.key) as keys:
            first = await asyncio.to_thread(pipe.ocr.semantic_source, self.source, (), None)
        key = self.shared.key(*keys.call_args.args)
        with self.db.db() as db:
            db.execute('DELETE FROM shared_content_cache WHERE scope=%s AND key=%s', (self.shared.scope, key))
        await asyncio.to_thread(
            self.shared.get_or_compute, key,
            lambda: {'text': 'invalid cached source', 'structural_lines': [False, 9999]},
        )
        self.assertEqual(await asyncio.to_thread(pipe.ocr.semantic_source, self.source, (), None), first)
        self.assertEqual(pipe.ocr._run_text.call_count, 2)

    async def test_failed_ocr_producer_does_not_launch_another_owners_request(self):
        alice, bob = self.pipe('alice'), self.pipe('bob')
        request = MagicMock(side_effect=M.ProcessingError('synthetic OCR timeout'))
        alice.ocr._paddleocr_vl_single_request = request
        bob.ocr._paddleocr_vl_single_request = request
        with self.assertRaisesRegex(M.ProcessingError, 'synthetic OCR timeout'):
            await self.recognize(alice)
        with self.assertRaises(M.ProcessingError):
            await self.recognize(bob)
        request.assert_called_once()

    def exporter(self, pipe, owner, writer):
        source = SimpleNamespace(id=owner + '-source', filename=owner + '.pdf')
        prepared = M.PreparedDocument(kind='pdf', source_files=(source,), pdf_path=self.source)
        result = DocumentResult(1, None, None, (prepared,))
        pipe.store_output = AsyncMock(side_effect=writer)
        return GroupExporter(pipe, owner, [source.id], result, JobPlan(), None)

    async def test_two_owners_share_spotting_and_word_bytes_but_create_their_own_files(self):
        alice, bob = self.pipe('alice'), self.pipe('bob')
        spotting, render = MagicMock(return_value={}), MagicMock(return_value=self.word)
        attempts = []

        async def writer(**values):
            attempts.append(values)
            return SimpleNamespace(id=f'{values["user_id"]}-{values["kind"]}', filename=values['display_name'])

        for pipe in (alice, bob):
            pipe.paddleocr_vl_spotting_response = spotting
            pipe.render_word = render
        results = await asyncio.gather(
            self.exporter(alice, 'alice', writer).run(), self.exporter(bob, 'bob', writer).run(),
        )
        spotting.assert_called_once()
        render.assert_called_once()
        for owner, result in zip(('alice', 'bob'), results, strict=True):
            self.assertFalse(result.errors)
            self.assertEqual(len(result.files), 2)
            self.assertTrue(all(file['id'].startswith(owner) for file in result.files))
            owned = [item for item in attempts if item['user_id'] == owner]
            self.assertTrue(all(item['source_file_id'] == owner + '-source' for item in owned))
            self.assertEqual(next(item['payload'] for item in owned if item['kind'] == 'document_docx'), self.word)

    async def test_identical_selected_pages_share_word_content_and_different_pages_do_not(self):
        with pymupdf.open() as document:
            for text in ('FIRST ORIGINAL PAGE', 'SECOND ORIGINAL PAGE'):
                document.new_page().insert_text((72, 72), text)
            self.source.write_bytes(document.tobytes())
        original = self.source.read_bytes()
        render, spotting = MagicMock(return_value=self.word), MagicMock(return_value={})
        pdfs = []

        async def writer(**values):
            if values['kind'] == 'document_pdf':
                pdfs.append(values['payload'])
            return SimpleNamespace(id=f'{values["user_id"]}-{values["kind"]}', filename=values['display_name'])

        for owner, pages in (('alice', (1,)), ('bob', (1,)), ('charlie', (2,))):
            pipe = self.pipe(owner)
            pipe.paddleocr_vl_spotting_response, pipe.render_word = spotting, render
            exporter = self.exporter(pipe, owner, writer)
            result = await exporter.export(exporter.document.members[0], 1, page_numbers=pages)
            self.assertFalse(result.errors)
            self.assertEqual(len(result.files), 2)
        self.assertEqual(pdfs[0], pdfs[1])
        self.assertNotEqual(pdfs[0], pdfs[2])
        self.assertEqual(render.call_count, 2)
        self.assertEqual(spotting.call_count, 2)
        self.assertEqual(self.source.read_bytes(), original)

    async def test_upload_and_eip_share_full_pdf_word_bytes_without_mixing_native_and_spotting(self):
        with pymupdf.open() as document:
            document.new_page().insert_text((72, 72), 'Owned original page')
            self.source.write_bytes(document.tobytes())
        for spotting_required in (False, True):
            with self.subTest(spotting=spotting_required):
                alice, bob = self.pipe('alice'), self.pipe('bob')
                layout = (M.OcrLayoutPage(595, 842, (M.OcrLayoutBlock('Original page', (10, 10, 100, 30), 'native_line'),)),)
                render = MagicMock(return_value=self.word)
                spotting = MagicMock(return_value={})
                for pipe in (alice, bob):
                    pipe.render_word, pipe.paddleocr_vl_spotting_response = render, spotting
                    pipe.native_pdf_layout_pages = MagicMock(return_value=layout)
                    pipe.paddleocr_vl_spotting_pages = MagicMock(return_value=layout)

                async def writer(**values):
                    return SimpleNamespace(id=f'{values["user_id"]}-{values["kind"]}', filename=values['display_name'])

                pipe_exporter = self.exporter(alice, 'alice', writer)
                prepared = pipe_exporter.document.members[0]
                origins = {'S1:P1': [{'source_file_id': prepared.source_file.id, 'page_number': 1}]}
                batch = UploadBatch(alice, 'alice', (prepared,), origins, self.root)
                batch.members = {'S1': prepared}
                batch.spotting_required = {'S1': spotting_required}
                batch.groups = [DocumentGroup(
                    reference_number='SYNTHETIC', main_pages=['S1:P1'], attachments=[],
                    related_reference_numbers=[], unread_source_ids=[],
                )]
                batch.results = [GroupResult(pipe_exporter.document, CalendarResult((), (), False), ())]
                await batch.export_documents()
                self.assertFalse(batch.export_failed)
                self.assertEqual(render.call_args.kwargs['layout_pages'], layout)
                self.assertEqual(await bob.exports.word_payload(self.source, spotting_required=spotting_required), self.word)
                render.assert_called_once()
                self.assertEqual(spotting.call_count, int(spotting_required))

    async def test_native_header_with_scanned_body_reuses_ocr_and_keeps_body_editable(self):
        image_stream = io.BytesIO()
        with Image.new('RGB', (450, 450), 'white') as image:
            ImageDraw.Draw(image).text((10, 10), 'Scanned body editable', fill='black')
            image.save(image_stream, format='PNG')
        with pymupdf.open() as document:
            page = document.new_page()
            page.insert_text((50, 50), 'Native header')
            page.insert_image(pymupdf.Rect(50, 140, 500, 590), stream=image_stream.getvalue())
            self.source.write_bytes(document.tobytes())
        response = {'errorCode': 0, 'result': {'layoutParsingResults': [{'prunedResult': {
            'page_index': 0, 'width': 595, 'height': 842,
            'parsing_res_list': [{'block_content': 'Native header\nScanned body editable', 'block_bbox': [50, 30, 500, 200]}],
            'spotting_res': {
                'rec_texts': ['Native header', 'Scanned body editable'],
                'rec_polys': [
                    [[50, 30], [300, 30], [300, 55], [50, 55]],
                    [[50, 140], [400, 140], [400, 170], [50, 170]],
                ],
            },
        }}]}}
        alice, bob = self.pipe('alice'), self.pipe('bob')
        request = MagicMock(return_value=response)
        render = MagicMock(wraps=alice.exports.render_word)
        saved = []

        async def writer(**values):
            saved.append(values)
            return SimpleNamespace(id=f'{values["user_id"]}-{values["kind"]}', filename=values['display_name'])

        sources = []
        for pipe in (alice, bob):
            pipe.ocr._paddleocr_vl_single_request = request
            pipe.render_word = render
            pipe.native_pdf_layout_pages = MagicMock(side_effect=AssertionError('mixed PDF must use OCR geometry'))
            pages, ocr = await pipe.ocr.ocr_context(self.source)
            self.assertEqual(pages, (0,))
            source = await asyncio.to_thread(pipe.ocr.semantic_source, self.source, pages, ocr)
            self.assertEqual(source.kind, 'paddle_layout_ocr')
            self.assertIn('Scanned body editable', source.text)
            sources.append(source)

        exporter = self.exporter(alice, 'alice', writer)
        prepared = exporter.document.members[0]
        origins = {'S1:P1': [{'source_file_id': prepared.source_file.id, 'page_number': 1}]}
        batch = UploadBatch(alice, 'alice', (prepared,), origins, self.root)
        batch.members, batch.spotting_required = {'S1': prepared}, {'S1': True}
        batch.groups = [DocumentGroup(
            reference_number='SYNTHETIC', main_pages=['S1:P1'], attachments=[],
            related_reference_numbers=[], unread_source_ids=[],
        )]
        batch.results = [GroupResult(exporter.document, CalendarResult((), (), False), ())]
        await batch.export_documents()
        self.assertFalse(batch.export_failed)
        eip_exporter = self.exporter(bob, 'bob', writer)
        eip_exporter.document = DocumentResult.from_analysis({
            'group_index': 1, 'members': eip_exporter.document.members, 'sources': {'S1': sources[1]},
        })
        result = await eip_exporter.run()
        self.assertFalse(result.errors)
        self.assertEqual(request.call_count, 2)
        self.assertEqual([call.kwargs['use_layout_detection'] for call in request.call_args_list], [True, False])
        render.assert_called_once()
        self.assertTrue(all(block.label == 'ocr_line' for page in render.call_args.kwargs['layout_pages'] for block in page.blocks))
        words = [item['payload'] for item in saved if item['kind'] == 'document_docx']
        self.assertEqual(len(words), 2)
        self.assertEqual(words[0], words[1])
        with zipfile.ZipFile(io.BytesIO(words[0])) as archive:
            self.assertIn(b'Scanned body editable', archive.read('word/document.xml'))

    async def test_composed_pdf_bytes_are_stable_for_same_pages_and_distinct_for_different_pages(self):
        with pymupdf.open() as document:
            for text in ('Original first page', 'Original second page'):
                document.new_page().insert_text((72, 72), text)
            self.source.write_bytes(document.tobytes())
        prepared = M.PreparedDocument(kind='pdf', source_files=(SimpleNamespace(id='source', filename='source.pdf'),), pdf_path=self.source)
        members = {'S1': prepared}
        first = group_pdf(members, ['S1:P1'], self.root / 'first.pdf')
        second = group_pdf(members, ['S1:P1'], self.root / 'second.pdf')
        other = group_pdf(members, ['S1:P2'], self.root / 'other.pdf')
        self.assertEqual(first.read_bytes(), second.read_bytes())
        self.assertNotEqual(first.read_bytes(), other.read_bytes())

    async def test_unknown_private_write_does_not_poison_other_owners_content_or_downloads(self):
        alice, bob = self.pipe('alice'), self.pipe('bob')
        spotting, render = MagicMock(return_value={}), MagicMock(return_value=self.word)
        attempts = []

        async def writer(**values):
            attempts.append((values['user_id'], values['kind']))
            if values['user_id'] == 'alice' and values['kind'] == 'document_docx':
                raise OutputUncertain('synthetic private write needs review')
            return SimpleNamespace(id=f'{values["user_id"]}-{values["kind"]}', filename=values['display_name'])

        for pipe in (alice, bob):
            pipe.paddleocr_vl_spotting_response = spotting
            pipe.render_word = render
        first = await self.exporter(alice, 'alice', writer).run()
        second = await self.exporter(bob, 'bob', writer).run()
        self.assertEqual([file['kind'] for file in first.files], ['document_pdf'])
        self.assertEqual(len(first.errors), 1)
        self.assertFalse(second.errors)
        self.assertEqual(len(second.files), 2)
        self.assertEqual(attempts.count(('alice', 'document_docx')), 1)
        render.assert_called_once()
        spotting.assert_called_once()

    async def test_cancelled_owner_does_not_cancel_shared_render_or_write_a_private_word(self):
        alice, bob = self.pipe('alice'), self.pipe('bob')
        entered, release = Event(), Event()
        attempts = []

        def render(*args):
            entered.set()
            if not release.wait(5):
                raise AssertionError('render test timed out')
            return self.word

        async def writer(**values):
            attempts.append((values['user_id'], values['kind']))
            return SimpleNamespace(id=f'{values["user_id"]}-{values["kind"]}', filename=values['display_name'])

        shared_render, spotting = MagicMock(side_effect=render), MagicMock(return_value={})
        for pipe in (alice, bob):
            pipe.paddleocr_vl_spotting_response = spotting
            pipe.render_word = shared_render
        first = asyncio.create_task(self.exporter(alice, 'alice', writer).run())
        second = None
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 3))
            second = asyncio.create_task(self.exporter(bob, 'bob', writer).run())
            first.cancel()
            await asyncio.sleep(0)
            self.assertFalse(first.done())
        finally:
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await first
            if second is not None:
                result = await second
        self.assertFalse(result.errors)
        self.assertNotIn(('alice', 'document_docx'), attempts)
        self.assertIn(('bob', 'document_docx'), attempts)
        shared_render.assert_called_once()
        spotting.assert_called_once()

    async def test_word_render_revision_and_ocr_settings_invalidate_before_spotting(self):
        pipe = self.pipe('alice')
        pipe.paddleocr_vl_spotting_response = MagicMock(return_value={})
        pipe.render_word = MagicMock(return_value=self.word)
        await pipe.exports.word_payload(self.source)
        await pipe.exports.word_payload(self.source)
        pipe.valves.paddleocr_vl_max_new_tokens += 256
        await pipe.exports.word_payload(self.source)
        with patch('examples.official_document_workflow.services.exports.WORD_RENDER_REVISION', 'new-renderer'):
            await pipe.exports.word_payload(self.source)
        self.assertEqual(pipe.render_word.call_count, 3)
        self.assertEqual(pipe.paddleocr_vl_spotting_response.call_count, 3)

    async def test_invalid_cached_word_bytes_are_rebuilt_before_spotting_is_skipped(self):
        pipe = self.pipe('alice')
        pipe.paddleocr_vl_spotting_response = MagicMock(return_value={})
        pipe.render_word = MagicMock(return_value=self.word)
        key = self.shared.key('word-payload', 'invalid-content-test')
        await asyncio.to_thread(self.shared.get_or_compute, key, lambda: b'not-a-docx')
        with patch.object(self.shared, 'key', return_value=key):
            self.assertEqual(await pipe.exports.word_payload(self.source), self.word)
            self.assertEqual(await pipe.exports.word_payload(self.source), self.word)
        pipe.render_word.assert_called_once()
        pipe.paddleocr_vl_spotting_response.assert_called_once()

    def long_input(self):
        members = [SimpleNamespace(source_file=SimpleNamespace(filename='main.pdf'))]
        sources = {'S1': M.CloudSemanticSource('任務原文\n' + ('無日期的圖形座標 ' * 50 + '\n') * 12, 'native_pdf_text')}
        return members, sources

    async def test_evidence_content_is_shared_but_source_names_remain_each_owners(self):
        alice, bob = self.pipe('alice'), self.pipe('bob')
        scan = AsyncMock(side_effect=lambda source, **kwargs: answer_for(source, lambda item: item['line'] == 1))
        for pipe in (alice, bob):
            pipe.valves.cloud_ocr_text_max_input_characters = 3000
            pipe.analyze_text = scan
            pipe._eip_processing_cache = MagicMock()
        members, sources = self.long_input()
        first, first_reading = await group_text(alice, members, sources)
        calls = scan.await_count
        renamed = [SimpleNamespace(source_file=SimpleNamespace(filename='bob-private-name.pdf'))]
        second, second_reading = await group_text(bob, renamed, sources)
        self.assertGreater(calls, 1)
        self.assertEqual(scan.await_count, calls)
        self.assertEqual(first_reading, second_reading)
        self.assertEqual(json.loads(first)['sources'][0]['name'], 'main.pdf')
        self.assertEqual(json.loads(second)['sources'][0]['name'], 'bob-private-name.pdf')
        alice._eip_processing_cache.get.assert_not_called()
        bob._eip_processing_cache.get.assert_not_called()

    async def test_evidence_semantic_settings_invalidate_without_sharing_group_analysis(self):
        pipe = self.pipe('alice')
        pipe.valves.cloud_ocr_text_max_input_characters = 3000
        pipe.analyze_text = AsyncMock(side_effect=lambda source, **kwargs: answer_for(source, lambda item: item['line'] == 1))
        members, sources = self.long_input()
        await group_text(pipe, members, sources)
        calls = pipe.analyze_text.await_count
        pipe.valves.cloud_ocr_text_reasoning_effort = 'high'
        await group_text(pipe, members, sources)
        self.assertEqual(pipe.analyze_text.await_count, calls * 2)
        pipe.valves.cloud_ocr_text_max_output_tokens += 256
        await group_text(pipe, members, sources)
        self.assertEqual(pipe.analyze_text.await_count, calls * 3)
        with patch('examples.official_document_workflow.application.long_source.SCAN_PROMPT', 'new exact evidence policy'):
            await group_text(pipe, members, sources)
        self.assertEqual(pipe.analyze_text.await_count, calls * 4)

    async def test_evidence_opt_out_does_not_reuse_another_owners_model_content(self):
        alice, bob = self.pipe('alice'), self.pipe('bob')
        scan = AsyncMock(side_effect=lambda source, **kwargs: answer_for(source, lambda item: item['line'] == 1))
        for pipe in (alice, bob):
            pipe.valves.cloud_ocr_text_max_input_characters = 3000
            pipe.analyze_text = scan
        members, sources = self.long_input()
        await group_text(alice, members, sources)
        calls = scan.await_count
        bob.evidence_cache = None
        await group_text(bob, members, sources)
        self.assertIs(bob.content_cache, self.shared)
        self.assertEqual(scan.await_count, calls * 2)

    async def test_invalid_shared_evidence_acknowledgement_is_rebuilt(self):
        pipe = self.pipe('alice')
        pipe.valves.cloud_ocr_text_max_input_characters = 3000
        pipe.analyze_text = AsyncMock(side_effect=lambda source, **kwargs: answer_for(source, lambda item: item['line'] == 1))
        members, sources = self.long_input()
        with patch.object(self.shared, 'key', wraps=self.shared.key) as keys:
            first = await group_text(pipe, members, sources)
        calls = pipe.analyze_text.await_count
        key = self.shared.key(*keys.call_args_list[0].args)
        with self.db.db() as db:
            db.execute('DELETE FROM shared_content_cache WHERE scope=%s AND key=%s', (self.shared.scope, key))
        await asyncio.to_thread(self.shared.get_or_compute, key, lambda: {'uncertain': False})
        self.assertEqual(await group_text(pipe, members, sources), first)
        self.assertEqual(pipe.analyze_text.await_count, calls + 1)

    async def test_invalid_evidence_is_not_published_or_used_as_success(self):
        pipe = self.pipe('alice')
        pipe.valves.cloud_ocr_text_max_input_characters = 3000
        pipe.analyze_text = AsyncMock(return_value={'read_count': True, 'keep_ranges': [[True, 1]], 'uncertain': False})
        members, sources = self.long_input()
        with self.assertRaises(ContentValidationError):
            await group_text(pipe, members, sources)
        pipe.analyze_text.assert_awaited_once()
        with self.db.db() as db:
            self.assertEqual(db.execute("SELECT count(*) AS n FROM shared_content_cache WHERE state='ready'").fetchone()['n'], 0)

    async def test_paid_evidence_publish_failure_returns_content_once_and_blocks_replay(self):
        alice, bob = self.pipe('alice'), self.pipe('bob')
        scan = AsyncMock(side_effect=lambda source, **kwargs: answer_for(source, lambda item: True))
        for pipe in (alice, bob):
            pipe.valves.cloud_ocr_text_max_input_characters = 3000
            pipe.analyze_text = scan
        members = [SimpleNamespace(source_file=SimpleNamespace(filename='owned.pdf'))]
        sources = {'S1': M.CloudSemanticSource('Original source fragment\n' + '\n' * 4000, 'native_pdf_text')}
        publish = self.shared._apublish

        async def fail_ready(db, key, payload, *, state='ready'):
            if state == 'ready':
                raise OSError('synthetic lost ready commit')
            await publish(db, key, payload, state=state)

        with patch.object(self.shared, '_apublish', side_effect=fail_ready):
            text, reading = await group_text(alice, members, sources)
        self.assertIn('Original source fragment', text)
        self.assertEqual(reading['windows'], 1)
        with self.assertRaises(M.ProcessingError):
            await group_text(bob, members, sources)
        scan.assert_awaited_once()

    async def test_paid_429_can_retry_after_cooldown_without_replaying_early(self):
        alice, bob = self.pipe('alice'), self.pipe('bob')
        rejected = True

        async def scan(source, **kwargs):
            nonlocal rejected
            if rejected:
                rejected = False
                raise M.CloudRateLimited('synthetic HTTP 429')
            return answer_for(source, lambda item: True)

        request = AsyncMock(side_effect=scan)
        for pipe in (alice, bob):
            pipe.valves.cloud_ocr_text_max_input_characters = 3000
            pipe.analyze_text = request
        members = [SimpleNamespace(source_file=SimpleNamespace(filename='owned.pdf'))]
        sources = {'S1': M.CloudSemanticSource('Original source fragment\n' + '\n' * 4000, 'native_pdf_text')}
        with self.assertRaises(M.CloudRateLimited):
            await group_text(alice, members, sources)
        with self.assertRaises(M.ProcessingError):
            await group_text(bob, members, sources)
        request.assert_awaited_once()
        with self.db.db() as db:
            db.execute("UPDATE shared_content_cache SET expires_at=CURRENT_TIMESTAMP-INTERVAL '1 second' WHERE state='failed'")
        text, reading = await group_text(bob, members, sources)
        self.assertIn('Original source fragment', text)
        self.assertEqual(reading['windows'], 1)
        self.assertEqual(request.await_count, 2)

    async def test_ambiguous_paid_timeout_stays_unknown_and_is_not_replayed(self):
        alice, bob = self.pipe('alice'), self.pipe('bob')
        request = AsyncMock(side_effect=M.CloudTemporaryFailure('synthetic request timeout'))
        for pipe in (alice, bob):
            pipe.valves.cloud_ocr_text_max_input_characters = 3000
            pipe.analyze_text = request
        members = [SimpleNamespace(source_file=SimpleNamespace(filename='owned.pdf'))]
        sources = {'S1': M.CloudSemanticSource('Original source fragment\n' + '\n' * 4000, 'native_pdf_text')}
        with self.assertRaises(M.CloudTemporaryFailure):
            await group_text(alice, members, sources)
        with self.db.db() as db:
            db.execute("UPDATE shared_content_cache SET expires_at=CURRENT_TIMESTAMP-INTERVAL '1 second' WHERE state='unknown'")
        with self.assertRaises(M.ProcessingError):
            await group_text(bob, members, sources)
        request.assert_awaited_once()

    async def test_cache_flag_false_bypasses_shared_content_for_both_owners(self):
        alice, bob = self.pipe('alice'), self.pipe('bob')
        render, spotting = MagicMock(return_value=self.word), MagicMock(return_value={})
        request = MagicMock(return_value=self.response)
        scan = AsyncMock(side_effect=lambda source, **kwargs: answer_for(source, lambda item: item['line'] == 1))
        members, sources = self.long_input()
        windows = []
        for pipe in (alice, bob):
            pipe.valves.document_analysis_cache_enabled = False
            pipe.valves.cloud_ocr_text_max_input_characters = 3000
            pipe.ocr._paddleocr_vl_single_request = request
            pipe.analyze_text = scan
            pipe.render_word, pipe.paddleocr_vl_spotting_response = render, spotting
            await self.recognize(pipe)
            await pipe.exports.word_payload(self.source)
            _, reading = await group_text(pipe, members, sources)
            windows.append(reading['windows'])
        self.assertEqual(render.call_count, 2)
        self.assertEqual(spotting.call_count, 2)
        self.assertEqual(request.call_count, 2)
        self.assertEqual(scan.await_count, sum(windows))

