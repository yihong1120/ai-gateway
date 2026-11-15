"""Local-only chunking tests: no OCR, LLM or network."""
from __future__ import annotations

import copy
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import pymupdf
from PIL import Image

from .test_official_document_processor_pipe import PIPE_MODULE as m
from examples.official_document_workflow.infrastructure.storage.processing_cache import ProcessingCache
from examples.official_document_workflow.services.ocr import validated_ocr_result


class NativePageClassificationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / 'source.pdf'

    def source(self, *, displayed_image=False, unused_image=False):
        with pymupdf.open() as document:
            page = document.new_page()
            page.insert_text((72, 72), 'Native header text')
            if displayed_image or unused_image:
                stream = io.BytesIO()
                with Image.new('RGB', (100, 100), 'white') as image:
                    image.save(stream, format='PNG')
                before = set(page.get_contents())
                page.insert_image(pymupdf.Rect(72, 100, 300, 400), stream=stream.getvalue())
                if unused_image:
                    for xref in set(page.get_contents()) - before:
                        document.update_stream(xref, b'')
            self.path.write_bytes(document.tobytes())

    def test_native_header_with_displayed_raster_body_requires_whole_page_ocr(self):
        self.source(displayed_image=True)
        with pymupdf.open(self.path) as document:
            self.assertTrue(document[0].get_text('words'))
            self.assertTrue(document[0].get_image_info())
        self.assertEqual(m.Pipe._pages_without_native_pdf_text(self.path), (0,))

    def test_pure_native_words_keep_native_geometry(self):
        self.source()
        self.assertEqual(m.Pipe._pages_without_native_pdf_text(self.path), ())

    def test_unused_image_resource_does_not_force_ocr(self):
        self.source(unused_image=True)
        with pymupdf.open(self.path) as document:
            self.assertTrue(document[0].get_images())
            self.assertFalse(document[0].get_image_info())
            self.assertTrue(document[0].get_text('words'))
        self.assertEqual(m.Pipe._pages_without_native_pdf_text(self.path), ())


class LongPdfTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)/'source.pdf'
        with pymupdf.open() as pdf:
            for i in range(25):
                pdf.new_page().insert_text((20, 20), f'PAGE-{i+1}')
            pdf.save(self.path)
        self.pipe = m.Pipe()

    def tearDown(self): self.tmp.cleanup()

    def response(self, path, **kw):
        with pymupdf.open(path) as pdf:
            return {'errorCode': 0, 'result': {'layoutParsingResults': [{'prunedResult': {'page_index': i, 'width': 595, 'height': 842, 'parsing_res_list': [{'block_content': p.get_text().strip(), 'block_bbox': [0, 0, 100, 30]}]}} for i, p in enumerate(pdf)]}}

    def test_all_25_pages_in_order_with_global_indices_and_no_source_changes(self):
        before = self.path.read_bytes()
        with patch.object(self.pipe, '_paddleocr_vl_single_request', side_effect=self.response) as request:
            result = self.pipe._paddleocr_vl_response(self.path)
        pages = result['result']['layoutParsingResults']
        self.assertEqual(request.call_count, 9)
        self.assertEqual([p['prunedResult']['page_index'] for p in pages], list(range(25)))
        text = self.pipe._paddleocr_vl_cloud_text(result)
        self.assertIn('<page index="25">', text)
        self.assertIn('PAGE-25', text)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertTrue(all(not call.args[0].exists() for call in request.call_args_list))

    def test_middle_chunk_missing_page_fails_without_retry_or_truncation(self):
        calls = []

        def response(path, **kw):
            calls.append(path)
            r = self.response(path, **kw)
            if len(calls) == 2:
                r['result']['layoutParsingResults'].pop()
            return r
        with patch.object(self.pipe, '_paddleocr_vl_single_request', side_effect=response):
            with self.assertRaisesRegex(m.ProcessingError, '第 4–6 頁'):
                self.pipe._paddleocr_vl_response(self.path)
        self.assertEqual(len(calls), 2)

    def test_spotting_uses_same_bounded_full_page_route(self):
        with patch.object(self.pipe, '_paddleocr_vl_single_request', side_effect=self.response) as request:
            result = self.pipe.paddleocr_vl_spotting_response(self.path)
        self.assertEqual(len(result['result']['layoutParsingResults']), 25)
        self.assertTrue(
            all(
                c.kwargs == {
                    'use_layout_detection': False,
                    'prompt_label': 'spotting',
                } for c in request.call_args_list
            ),
        )

    def test_pdf_cap_is_separate_from_request_cap(self):
        self.pipe.valves.max_ocr_pages = 2
        self.assertEqual(self.pipe._validate_pdf_page_count(self.path), 25)
        with patch.object(self.pipe, '_paddleocr_vl_single_request', side_effect=self.response) as request:
            self.pipe._paddleocr_vl_response(self.path)
        self.assertEqual(request.call_count, 13)
        self.pipe.valves.max_pdf_pages = 24
        with self.assertRaises(m.ProcessingError):
            self.pipe._validate_pdf_page_count(self.path)

    def test_small_response_missing_page_is_not_accepted(self):
        with patch.object(self.pipe, '_validate_pdf_page_count', return_value=2), patch.object(self.pipe, '_paddleocr_vl_single_request', return_value={'errorCode': 0, 'result': {'layoutParsingResults': [{}]}}):
            with self.assertRaises(m.ProcessingError):
                self.pipe._paddleocr_vl_response(self.path)

    def test_chunk_merge_does_not_change_cached_local_page_numbers(self):
        cached = {}
        cache = MagicMock()
        cache.key.side_effect = ProcessingCache.key
        cache.get.side_effect = cached.get
        cache.put.side_effect = cached.__setitem__
        self.pipe._eip_processing_cache = cache
        with patch.object(self.pipe, '_paddleocr_vl_single_request', side_effect=self.response) as request:
            first = self.pipe._paddleocr_vl_response(self.path)
            second = self.pipe._paddleocr_vl_response(self.path)
        self.assertEqual(request.call_count, 9)
        self.assertEqual(first, second)
        for response in cached.values():
            items = response['result']['layoutParsingResults']
            self.assertEqual([p['prunedResult']['page_index'] for p in items], list(range(len(items))))


class OcrCacheValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'source.pdf'
        self.path.write_bytes(b'%PDF-synthetic')
        self.pipe = m.Pipe()
        self.cache = MagicMock()
        self.pipe._eip_processing_cache = self.cache
        self.valid = {
            'errorCode': 0,
            'result': {'layoutParsingResults': [{'prunedResult': {'page_index': i}} for i in range(2)]},
        }
        count = patch.object(self.pipe, '_validate_pdf_page_count', return_value=2)
        count.start()
        self.addCleanup(count.stop)

    def invalid_responses(self):
        responses = [None, [], {'errorCode': 1, 'result': self.valid['result']}, {'errorCode': 0, 'result': []}]
        for value in ([], [{}], [{}, {}], [{'prunedResult': []}, {'prunedResult': {}}]):
            responses.append({'errorCode': 0, 'result': {'layoutParsingResults': value}})
        for index in (1, False, 0.0, '0'):
            response = copy.deepcopy(self.valid)
            response['result']['layoutParsingResults'][0]['prunedResult']['page_index'] = index
            responses.append(response)
        response = copy.deepcopy(self.valid)
        response['result']['dataInfo'] = {'pages': 'invalid'}
        responses.append(response)
        return responses

    def test_bad_cache_is_rebuilt_and_replaced_only_with_validated_response(self):
        for invalid in self.invalid_responses():
            with self.subTest(invalid=invalid):
                self.cache.reset_mock()
                self.cache.get.return_value = invalid
                with patch.object(self.pipe, '_paddleocr_vl_single_request', return_value=self.valid) as request:
                    self.assertEqual(self.pipe._paddleocr_vl_response(self.path), self.valid)
                request.assert_called_once()
                self.cache.put.assert_called_once_with(self.cache.key.return_value, self.valid)

    def test_invalid_fresh_response_is_never_cached_or_retried(self):
        for invalid in self.invalid_responses():
            with self.subTest(invalid=invalid):
                self.cache.reset_mock()
                self.cache.get.return_value = None
                with (
                    patch.object(self.pipe, '_paddleocr_vl_single_request', return_value=invalid) as request,
                    self.assertRaises(m.ProcessingError),
                ):
                    self.pipe._paddleocr_vl_response(self.path)
                request.assert_called_once()
                self.cache.put.assert_not_called()

    def test_invalid_cache_does_not_hide_a_failed_rebuild(self):
        self.cache.get.return_value = self.invalid_responses()[-2]
        with (
            patch.object(self.pipe, '_paddleocr_vl_single_request', side_effect=m.ProcessingError('OCR unavailable')) as request,
            self.assertRaisesRegex(m.ProcessingError, 'OCR unavailable'),
        ):
            self.pipe._paddleocr_vl_response(self.path)
        request.assert_called_once()
        self.cache.put.assert_not_called()

    def test_valid_cache_does_not_start_an_ocr_request(self):
        self.cache.get.return_value = self.valid
        with patch.object(self.pipe, '_paddleocr_vl_single_request', side_effect=AssertionError('cache hit')):
            self.assertEqual(self.pipe._paddleocr_vl_response(self.path), self.valid)
        self.cache.put.assert_not_called()

    def test_optional_page_indices_are_preserved_without_mutating_input(self):
        response = copy.deepcopy(self.valid)
        del response['result']['layoutParsingResults'][0]['prunedResult']['page_index']
        response['result']['layoutParsingResults'][1]['prunedResult']['page_index'] = None
        before = copy.deepcopy(response)
        self.assertIs(validated_ocr_result(response, 2), response['result'])
        self.assertEqual(response, before)
