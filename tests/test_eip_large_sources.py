"""Complete large-source processing, isolation, failure and resume tests."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import patch

import httpx
import pymupdf
from cryptography.fernet import InvalidToken

from .test_official_document_processor_pipe import PIPE_MODULE as M
from examples.official_document_workflow.application.long_source import group_text
from examples.official_document_workflow.application.long_source import source_lines
from examples.official_document_workflow.application.long_source import windows
from examples.official_document_workflow.domain.calendar.calendar_semantics import resolve_spans
from examples.official_document_workflow.infrastructure.eip import calendar_csv
from examples.official_document_workflow.infrastructure.eip.netask import Netask
from examples.official_document_workflow.infrastructure.office.office_input import validate
from examples.official_document_workflow.infrastructure.storage.automation_store import AutomationStore
from examples.official_document_workflow.infrastructure.storage.processing_cache import ProcessingCache
from tests.eip_postgres import EIPPostgresTestCase


def answer_for(source, keep):
    items = json.loads(source.text)['fragments']
    indices = [i for i, item in enumerate(items, 1) if keep(item)]
    ranges = []
    for i in indices:
        if ranges and ranges[-1][1]+1 == i:
            ranges[-1][1] = i
        else:
            ranges.append([i, i])
    return {
        'read_count': len(items), 'first_id': items[0]['id'], 'last_id': items[-1]['id'],
        'keep_ranges': ranges, 'uncertain': False,
    }


class CompactSourceTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_system_layout_wrappers_are_removed_and_evidence_coordinates_survive(self):
        text = '<page index="1">\n<ocr-block>\n第一段原文\n</ocr-block>\n\n<ocr-block>\n 第二段原文 \n</ocr-block>\n</page>\n<ocr-block>'
        source = M.CloudSemanticSource(text, 'paddle_layout_ocr', frozenset({1, 2, 4, 6, 8, 9}))
        expected = [(1, '<page index="1">'), (3, '第一段原文'), (7, ' 第二段原文 '), (9, '</page>'), (10, '<ocr-block>')]
        self.assertEqual(source_lines(source), expected)
        pipe = M.Pipe()
        result, reading = await group_text(pipe,  [SimpleNamespace(source_file=SimpleNamespace(filename='a.pdf'))], {'S1': source})
        self.assertEqual(json.loads(result)['sources'][0]['lines'], [list(v) for v in expected])
        self.assertEqual(reading['mode'], 'complete')
        fragments = [item for chunk in windows({'S1': source}, 2000) for item in chunk]
        self.assertEqual([(v['line'], v['text']) for v in fragments], expected)
        parts = resolve_spans([{'source_id': 'S1', 'start_line': 3, 'end_line': 7}], {'S1': source})
        self.assertIn('第一段原文', parts[0][3])
        self.assertIn('第二段原文', parts[0][3])
        self.assertEqual(source.text, text)


class LargeSourceTests(EIPPostgresTestCase, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.pipe = M.Pipe()
        self.pipe.valves.eip_large_sources = True
        self.pipe.valves.cloud_ocr_text_max_input_characters = 3000
        self.pipe.emit_status = AsyncMock()
        self.db = AutomationStore(self.root/'key')
        self.pipe._eip_processing_cache = ProcessingCache(self.db, 'alice')
        self.members = [
            SimpleNamespace(source_file=SimpleNamespace(filename='main.pdf')),
            SimpleNamespace(source_file=SimpleNamespace(filename='supplement.pdf')),
        ]
        self.sources = {
            'S1': M.CloudSemanticSource('主旨：會勘通知\n會勘日期詳附件。', 'native_pdf_text'),
            'S2': M.CloudSemanticSource(('無日期的圖形座標 ' * 50+'\n')*200+'本次會勘：115年9月8日上午10時整', 'native_pdf_text'),
        }

    async def test_full_reading_including_final_attachment_date_and_original_line_ids(self):
        calls = []

        async def scan(source, **kwargs):
            calls.append(json.loads(source.text)['fragments'])
            self.assertLessEqual(len(source.text), 3000)
            return answer_for(source, lambda i: i['source_id'] == 'S1' or '本次會勘' in i['text'])
        self.pipe.analyze_text = AsyncMock(side_effect=scan)
        text, reading = await group_text(self.pipe,  self.members, self.sources)
        data = json.loads(text)
        self.assertGreater(reading['windows'], 10)
        self.assertIn('115年9月8日', text)
        self.assertEqual(data['sources'][1]['lines'][-1][0], 201)
        actual = ''.join(i['text'] for chunk in calls for i in chunk if i['source_id'] == 'S2')
        self.assertEqual(actual, ''.join(self.sources['S2'].text.splitlines()))
        before = len(calls)
        again = await group_text(self.pipe,  self.members, self.sources)
        self.assertEqual(again, (text, reading))
        self.assertEqual(len(calls), before)

    async def test_failed_middle_window_resumes_completed_only(self):
        count = 0

        async def scan(source, **kwargs):
            nonlocal count
            count += 1
            if count == 2:
                raise ValueError('temporary failure')
            return answer_for(source, lambda i: i['source_id'] == 'S1' or '本次會勘' in i['text'])
        self.pipe.analyze_text = AsyncMock(side_effect=scan)
        with self.assertRaisesRegex(ValueError, 'temporary'):
            await group_text(self.pipe,  self.members, self.sources)
        self.assertEqual(count, 2)
        _, reading = await group_text(self.pipe,  self.members, self.sources)
        self.assertEqual(count, reading['windows']+1)

    async def test_incomplete_or_fabricated_ranges_never_accepted(self):
        from examples.official_document_workflow.domain.validation import ContentValidationError
        for update in (
            {'read_count': 0}, {'first_id': 'other'}, {'uncertain': True},
            {'keep_ranges': [[0, 1]]}, {'keep_ranges': [[1, 1], [1, 2]]},
        ):
            async def scan(source, **kwargs): return {**answer_for(source, lambda i: True), **update}
            self.pipe.analyze_text = AsyncMock(side_effect=scan)
            with self.assertRaises(ContentValidationError) as caught:
                await group_text(self.pipe,  self.members, self.sources)
            self.assertEqual(caught.exception.code, 'source_reading_unverified')
            self.assertFalse(caught.exception.can_auto_repair)

    async def test_date_context_and_short_main_survive_empty_model_selection(self):
        self.pipe.analyze_text = AsyncMock(side_effect=lambda s, **kw: answer_for(s, lambda i: False))
        text, _ = await group_text(self.pipe,  self.members, self.sources)
        self.assertIn('115年9月8日', text)
        self.assertIn('主旨：會勘通知', text)

    async def test_cloud_429_is_explicit_and_not_replayed(self):
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.post.return_value = httpx.Response(429, json={'error': 'private provider body'})
        with patch.object(self.pipe, '_official_document_litellm_connection', return_value=('http://127.0.0.1:4000/v1', 'test-key')), patch.object(M.httpx, 'AsyncClient', return_value=client):
            with self.assertRaisesRegex(M.ProcessingError, 'HTTP 429') as error:
                await self.pipe.analyze_text(M.CloudSemanticSource('test', 'native_pdf_text'))
        self.assertNotIn('private provider', str(error.exception))
        client.post.assert_awaited_once()

    async def test_truncated_json_gets_one_larger_response_not_partial_parse(self):
        client = AsyncMock()
        client.__aenter__.return_value = client
        incomplete = {
            'status': 'incomplete', 'incomplete_details': {'reason': 'max_output_tokens'},
            'output_text': '{"events":[',
        }
        complete = {'status': 'completed', 'output_text': '{"events":[]}'}
        budgets = []

        async def respond(*args, **kwargs):
            budgets.append(kwargs['json']['max_output_tokens'])
            return httpx.Response(200, json=incomplete if len(budgets) == 1 else complete)
        client.post.side_effect = respond
        with patch.object(self.pipe, '_official_document_litellm_connection', return_value=('http://127.0.0.1:4000/v1', 'test-key')), patch.object(M.httpx, 'AsyncClient', return_value=client):
            result = await self.pipe.analyze_text(M.CloudSemanticSource('test', 'native_pdf_text'))
        self.assertEqual(result, {'events': []})
        self.assertEqual(
            budgets, [
                self.pipe.valves.cloud_ocr_text_max_output_tokens,
                self.pipe.valves.cloud_ocr_text_max_output_tokens*2,
            ],
        )

    async def test_persistent_incomplete_output_does_not_loop_or_parse(self):
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.post.return_value = httpx.Response(
            200, json={
                'status': 'incomplete',
                'incomplete_details': {'reason': 'max_output_tokens'}, 'output_text': '{"events":[]}',
            }, )
        with patch.object(self.pipe, '_official_document_litellm_connection', return_value=('http://127.0.0.1:4000/v1', 'test-key')), patch.object(M.httpx, 'AsyncClient', return_value=client):
            with self.assertRaisesRegex(M.ProcessingError, '未完整產生'):
                await self.pipe.analyze_text(M.CloudSemanticSource('test', 'native_pdf_text'))
        self.assertEqual(client.post.await_count, 2)

    async def test_other_incomplete_reason_is_not_retried(self):
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.post.return_value = httpx.Response(
            200, json={
                'status': 'incomplete',
                'incomplete_details': {'reason': 'content_filter'}, 'output_text': '{"events":[]}',
            }, )
        with patch.object(self.pipe, '_official_document_litellm_connection', return_value=('http://127.0.0.1:4000/v1', 'test-key')), patch.object(M.httpx, 'AsyncClient', return_value=client):
            with self.assertRaisesRegex(M.ProcessingError, '未完整產生'):
                await self.pipe.analyze_text(M.CloudSemanticSource('test', 'native_pdf_text'))
        client.post.assert_awaited_once()

    async def test_truncation_at_configured_ceiling_is_not_retried(self):
        self.pipe.valves.cloud_ocr_text_max_output_tokens = 6000
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.post.return_value = httpx.Response(
            200, json={
                'status': 'incomplete',
                'incomplete_details': {'reason': 'max_output_tokens'}, 'output_text': '{}',
            },
        )
        with patch.object(self.pipe, '_official_document_litellm_connection', return_value=('http://127.0.0.1:4000/v1', 'test-key')), patch.object(M.httpx, 'AsyncClient', return_value=client):
            with self.assertRaisesRegex(M.ProcessingError, '未完整產生'):
                await self.pipe.analyze_text(M.CloudSemanticSource('test', 'native_pdf_text'))
        client.post.assert_awaited_once()

    def test_large_csv_description_is_preserved_not_truncated(self):
        from examples.official_document_workflow.application import integration
        from .test_eip_connector import row, csv_bytes
        item = row()
        item['Description'] = '完整內容與標點，保持換行。\n'*80000
        with patch.object(integration, 'module', new=M):
            rows, transport = calendar_csv.parse_csv(csv_bytes([item]))
        self.assertEqual(rows[0]['Description'], item['Description'])
        self.assertGreater(len(transport), 1024*1024)

    async def test_dense_relevant_content_not_summarized_or_discarded_to_force_success(self):
        from examples.official_document_workflow.domain.validation import ContentValidationError
        self.pipe.analyze_text = AsyncMock(side_effect=lambda s, **kw: answer_for(s, lambda i: True))
        with self.assertRaises(ContentValidationError) as caught:
            await group_text(self.pipe,  self.members, self.sources)
        self.assertEqual(caught.exception.code, 'source_evidence_capacity_exceeded')
        self.assertFalse(caught.exception.can_auto_repair)

    async def test_mandatory_evidence_over_budget_stops_before_paid_window_calls(self):
        from examples.official_document_workflow.domain.validation import ContentValidationError
        original = ('115年10月28日期限：保留本件完整原文證據。\n' * 300)
        sources = {'S1': M.CloudSemanticSource(original, 'native_pdf_text')}
        self.pipe.analyze_text = AsyncMock()
        with self.assertRaises(ContentValidationError) as caught:
            await group_text(self.pipe,  self.members[:1], sources)
        self.assertEqual(caught.exception.code, 'source_evidence_capacity_exceeded')
        self.pipe.analyze_text.assert_not_awaited()
        self.assertEqual(sources['S1'].text, original)

    async def test_layout_wrappers_do_not_count_as_mandatory_evidence(self):
        text = '<ocr-block>\n115年10月28日期限\n</ocr-block>\n' * 100
        structural = frozenset(n for n, line in enumerate(text.splitlines(), 1) if '<' in line)
        sources = {'S1': M.CloudSemanticSource(text, 'paddle_layout_ocr', structural)}
        self.pipe.analyze_text = AsyncMock(side_effect=lambda s, **kw: answer_for(s, lambda i: True))
        result, _ = await group_text(self.pipe,  self.members[:1], sources)
        self.assertIn('115年10月28日期限', result)
        self.assertNotIn('<ocr-block>', result)

    def test_long_single_line_and_escaped_characters_are_losslessly_partitioned(self):
        original = '\\"漢字'*40000
        source = {'S1': M.CloudSemanticSource(original, 'native_pdf_text')}
        chunks = list(windows(source, 3000))
        self.assertGreater(len(chunks), 1)
        self.assertEqual(''.join(i['text'] for c in chunks for i in c), original)
        for chunk in chunks:
            self.assertLessEqual(len(json.dumps({'fragments': chunk}, ensure_ascii=False, separators=(',', ':'))), 3000)

    def test_encrypted_cache_is_owner_isolated_and_integrity_checked(self):
        cache = self.pipe._eip_processing_cache
        cache.put('key', {'secret': 'ONLY-ALICE-PRIVATE-SOURCE'})
        with self.db.db() as db:
            encrypted = db.execute(
                'SELECT payload FROM processing_chunks WHERE owner=%s AND key=%s',
                ('alice', 'key'),
            ).fetchone()['payload']
        self.assertNotIn(b'ONLY-ALICE', encrypted)
        self.assertEqual(cache.get('key'), {'secret': 'ONLY-ALICE-PRIVATE-SOURCE'})
        self.assertIsNone(ProcessingCache(self.db, 'bob').get('key'))
        with self.db.db() as db:
            db.execute('UPDATE processing_chunks SET payload=%s WHERE owner=%s', (b'bad', 'alice'))
        with self.assertRaises(InvalidToken):
            self.db.decode('alice', b'bad')
        # Rebuildable checkpoints reject corrupt content without failing the document.
        self.assertIsNone(cache.get('key'))

    def test_source_bytes_pages_text_and_description_no_legacy_total_caps(self):
        path = self.root/'large.pdf'
        with pymupdf.open() as pdf:
            for i in range(163):
                pdf.new_page().insert_text((30, 30), f'PAGE {i+1}')
            pdf.save(path)
        with path.open('ab') as stream:
            stream.truncate(32*1024*1024)
        self.pipe._validate_pdf_path(path)
        self.assertEqual(self.pipe._validate_pdf_page_count(path), 163)
        text = '完整原文'*50000
        self.assertEqual(self.pipe._bounded_cloud_semantic_text(text), text)
        sources = {'S1': M.CloudSemanticSource(text, 'native_pdf_text')}
        self.assertEqual(resolve_spans([{'source_id': 'S1', 'start_line': 1, 'end_line': 1}], sources)[0][3], text)
        with path.open('rb') as stream:
            self.assertEqual(validate(stream, 'large.pdf'), '.pdf')

    def test_ocr_chunks_resume_with_stable_keys_and_global_page_numbers(self):
        path = self.root/'scan.pdf'
        with pymupdf.open() as pdf:
            for i in range(38):
                pdf.new_page().insert_text((30, 30), f'PAGE {i+1}')
            pdf.save(path)
        calls = 0

        def response(path, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise M.ProcessingError('temporary OCR failure')
            with pymupdf.open(path) as pdf:
                return {
                    'errorCode': 0, 'result': {
                        'layoutParsingResults': [
                            {'prunedResult': {'page_index': i, 'parsing_res_list': []}} for i in range(len(pdf))
                        ],
                    },
                }
        with patch.object(self.pipe, '_paddleocr_vl_single_request', side_effect=response):
            with self.assertRaises(M.ProcessingError):
                self.pipe._paddleocr_vl_response(path)
            result = self.pipe._paddleocr_vl_response(path)
            self.assertEqual(calls, 14)  # 13 successful chunks + one failed request
            self.assertEqual(
                [
                    p['prunedResult']['page_index']
                    for p in result['result']['layoutParsingResults']
                ], list(range(38)),
            )
            self.pipe._paddleocr_vl_response(path)
            self.assertEqual(calls, 14)

    async def test_large_download_spools_to_disk_without_30_mib_cap(self):
        class Body(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'%PDF-'
                for _ in range(32):
                    yield b'x'*(1024*1024)
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, stream=Body())))
        adapter = Netask(client)
        try:
            stream = await adapter.download('/fo/large.pdf')
            try:
                self.assertTrue(stream._rolled)
                self.assertEqual(stream.read(5), b'%PDF-')
                self.assertEqual(stream.seek(0, 2), 32*1024*1024+5)
            finally:
                stream.close()
        finally:
            await adapter.close()

    async def test_download_still_rejects_login_page_and_cross_origin(self):
        adapter = Netask(
            httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda req: httpx.Response(
                        200,
                        content=b'<html><form><input name="_token"></form></html>',
                    ),
                ),
            ),
        )
        try:
            with self.assertRaises(ConnectionError):
                await adapter.download('/fo/test.pdf')
            with self.assertRaises(ValueError):
                await adapter.download('http://example.com/fo/test.pdf')
        finally:
            await adapter.close()
