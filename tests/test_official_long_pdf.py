"""Local-only chunking tests: no OCR, LLM or network."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pymupdf

from .test_official_document_processor_pipe import PIPE_MODULE as m


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
            result = self.pipe._paddleocr_vl_spotting_response(self.path)
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
