"""Tests for the single-path official-document processor.

The Function is loaded with small Open WebUI stubs. These tests do not contact
LiteLLM, PaddleOCR, storage, or a database.
"""
from __future__ import annotations

import asyncio
import csv
import importlib.util
import inspect
import io
import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch
from urllib.parse import unquote_plus


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PIPE_PATH = REPOSITORY_ROOT / 'examples/openwebui/functions/official_document_processor_pipe.py'
CALENDAR_TEMPLATE_PATH = REPOSITORY_ROOT / 'example_files/行事曆格式.csv'


def _load_pipe_module() -> types.ModuleType:
    package = types.ModuleType('open_webui')
    package.__path__ = []  # type: ignore[attr-defined]
    models = types.ModuleType('open_webui.models')
    models.__path__ = []  # type: ignore[attr-defined]
    files = types.ModuleType('open_webui.models.files')
    storage = types.ModuleType('open_webui.storage')
    storage.__path__ = []  # type: ignore[attr-defined]
    provider = types.ModuleType('open_webui.storage.provider')

    class FileForm:
        def __init__(self, **values: object) -> None:
            self.__dict__.update(values)

    class FileModel:
        pass

    class Files:
        pass

    class Storage:
        pass

    files.FileForm = FileForm
    files.FileModel = FileModel
    files.Files = Files
    provider.Storage = Storage
    modules = {
        'open_webui': package,
        'open_webui.models': models,
        'open_webui.models.files': files,
        'open_webui.storage': storage,
        'open_webui.storage.provider': provider,
    }
    original_modules = {name: sys.modules.get(name) for name in modules}
    sys.modules.update(modules)
    try:
        spec = importlib.util.spec_from_file_location(
            'official_document_processor_pipe_under_test', PIPE_PATH,
        )
        if spec is None or spec.loader is None:
            raise RuntimeError(f"Could not load {PIPE_PATH}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        # Services may already be imported by another test entry point. Bind
        # their host adapters to the same explicit fakes in either import order.
        from examples.official_document_workflow.services import sources, exports, processor
        for boundary in (sources, exports, processor):
            if hasattr(boundary, 'Files'):
                boundary.Files = Files
            if hasattr(boundary, 'Storage'):
                boundary.Storage = Storage
        exports.FileForm = FileForm
        return module
    finally:
        for name, value in original_modules.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


PIPE_MODULE = _load_pipe_module()
from examples.official_document_workflow.application.pending import PendingClarificationStore


class Pipe(PIPE_MODULE.Pipe):
    def __init__(self, *, pending=None):
        super().__init__(pending=pending if pending is not None else PendingClarificationStore())
PIPE_MODULE.Pipe = Pipe
Deadline = PIPE_MODULE.Deadline
DocumentDate = PIPE_MODULE.DocumentDate
DocumentTime = PIPE_MODULE.DocumentTime
OfficialDocument = PIPE_MODULE.OfficialDocument
CloudDocumentExtraction = PIPE_MODULE.CloudDocumentExtraction
CloudSemanticSource = PIPE_MODULE.CloudSemanticSource
PreparedDocument = PIPE_MODULE.PreparedDocument
ProcessingError = PIPE_MODULE.ProcessingError
CloudExtractionError = PIPE_MODULE.CloudExtractionError

REFERENCE = '南新工單一字第1153022027號'
SUBJECT = '檢送「代辦國道3號古坑交流道平面側車道工程出流管制計畫書」施工督導查核紀錄表1份，請查照。'
DEADLINE_EVIDENCE = '於115年9月10日前提送評估報告予本段續辦。'
SOURCE = f'''<page index="1">
交通部公路局南區公路新建工程分局第一工務段 函
受文者：長達營造工程有限公司
發文日期：中華民國115年8月13日
發文字號：{REFERENCE}
主旨：{SUBJECT}
說明：
一、請晉國工程顧問有限公司重新通盤檢討穿越水路或截流溝，並{DEADLINE_EVIDENCE}
承辦人：鍾羽雯
承辦單位：交通部公路局南區公路新建工程分局第一工務段
</page>'''


def _date(year: str = '115', month: str = '08', day: str = '13') -> DocumentDate:
    return Pipe._document_date(year, month, day)


def _document() -> OfficialDocument:
    return OfficialDocument(
        issued=_date(), reference_number=REFERENCE, subject=SUBJECT,
        recipient='長達營造工程有限公司',
        extraction_source='雲端模型 DeepSeek-V4-Flash 根據本機 OCR/PDF 文字判讀',
        handler_name='鍾羽雯',
        handling_unit='交通部公路局南區公路新建工程分局第一工務段',
        explanation='一、請晉國工程顧問有限公司重新通盤檢討穿越水路或截流溝，並於115年9月10日前提送評估報告予本段續辦。',
    )


def _deadline(*, timed: bool = False) -> Deadline:
    time = DocumentTime(display='15:30:00', twenty_four_hour='15:30') if timed else None
    return Deadline(
        date=_date('115', '09', '10'), time=time, action='提送評估報告',
        sentence=DEADLINE_EVIDENCE, responsible_party='晉國工程顧問有限公司',
        category='01-代辦國道3號古坑交流道平面側車道工程',
    )


def _valid_payload() -> dict[str, object]:
    explanation = '一、請晉國工程顧問有限公司重新通盤檢討穿越水路或截流溝，並於115年9月10日前提送評估報告予本段續辦。'
    filename_subject = '檢送「代辦國道3號古坑交流道平面側車道工程出流管制計畫書」施工督導查核紀錄表1份'
    evidence = {
        'issued_date': '發文日期：中華民國115年8月13日',
        'reference_number': REFERENCE,
        'subject': SUBJECT,
        'filename_subject': filename_subject,
        'recipient': '受文者：長達營造工程有限公司',
        'handler_name': '承辦人：鍾羽雯',
        'handling_unit': '承辦單位：交通部公路局南區公路新建工程分局第一工務段',
        'contact_phone': None, 'contact_email': None, 'sender_address': None,
        'primary_recipients': None,
        'calendar_subject': None,
    }
    return {
        'document': {
            'issued_date': '2026-08-13', 'reference_number': REFERENCE,
            'subject': SUBJECT, 'filename_subject': filename_subject, 'calendar_subject': None,
            'recipient': '長達營造工程有限公司', 'handler_name': '鍾羽雯',
            'handling_unit': '交通部公路局南區公路新建工程分局第一工務段',
            'contact_phone': None, 'contact_email': None, 'sender_address': None,
            'primary_recipients': None,
            'calendar_description': {
                'mode': 'verbatim', 'value': explanation, 'evidence': [explanation],
            },
            'evidence': evidence,
        },
        'events': [{
            'start_date': '2026-09-10', 'start_time': None,
            'end_date': None, 'end_time': None, 'all_day_event': '真', 'work_hour': None,
            'action': '提送評估報告',
            'category': {'value': '01-代辦國道3號古坑交流道平面側車道工程', 'evidence': SUBJECT},
            'private': {'value': None, 'evidence': None},
            'priority': {'value': None, 'evidence': None},
            'completed': {'value': None, 'evidence': None},
            'responsible_party': '晉國工程顧問有限公司', 'evidence': DEADLINE_EVIDENCE,
        }],
    }


def _layout_response(text: str = '第一行\n第二行') -> dict[str, object]:
    return {
        'result': {
            'layoutParsingResults': [{
                'prunedResult': {
                    'width': 1000, 'height': 1400,
                    'parsing_res_list': [{'block_content': text, 'block_bbox': [80, 100, 920, 220], 'block_label': 'paragraph_text'}],
                },
            }],
        },
    }


class CalendarCsvTests(unittest.TestCase):
    def setUp(self) -> None:
        self.pipe = Pipe()

    def test_schema_and_translation_row_match_supplied_template(self) -> None:
        with CALENDAR_TEMPLATE_PATH.open('r', encoding='utf-8-sig', newline='') as handle:
            rows = list(csv.reader(handle))
        self.assertEqual(rows[0], PIPE_MODULE.CALENDAR_HEADERS)
        self.assertEqual(
            dict(
                zip(
                    PIPE_MODULE.CALENDAR_HEADERS,
                    rows[1], strict=True,
                ),
            ), PIPE_MODULE.CALENDAR_TRANSLATION_ROW,
        )

    def test_verified_event_becomes_complete_template_row(self) -> None:
        document = _document()
        rows = self.pipe._calendar_rows(
            owner_name=self.pipe._calendar_owner_name(document),
            document=document,
            deadlines=[_deadline()],
        )
        parsed = list(csv.DictReader(io.StringIO(self.pipe.calendar_csv(rows).decode('utf-8-sig'))))
        self.assertEqual(len(parsed), 1)
        row = parsed[0]
        self.assertEqual(row['Name'], '鍾羽雯')
        self.assertEqual(row['Start Date'], '2026/09/10')
        self.assertEqual(row['End Date'], '2026/09/10')
        self.assertEqual(row['All day event'], '真')
        self.assertEqual(row['Private'], '假')
        self.assertEqual(row['Priority'], '普通')
        self.assertEqual(row['Completed'], '0')
        self.assertEqual(row['Categories'], '01-代辦國道3號古坑交流道平面側車道工程')
        self.assertEqual(row['Custom Field1'], REFERENCE)
        self.assertIn('重新通盤檢討', row['Description'])

    def test_full_explanation_survives_csv_and_overrides_event_summary(self) -> None:
        explanation = '一、請依附件辦理。\n二、聯絡人：王先生，電話 02-12345678。\n（一）其餘事項詳附件。'
        document = OfficialDocument(**{**_document().__dict__, 'explanation': explanation})
        event = Deadline(**{**_deadline().__dict__, 'calendar_description': '事件摘要'})
        rows = self.pipe._calendar_rows(owner_name='測試', document=document, deadlines=[event])
        parsed = list(csv.DictReader(io.StringIO(self.pipe.calendar_csv(rows).decode('utf-8-sig'))))
        self.assertEqual(parsed[-1]['Description'], explanation)

    def test_generated_description_is_csv_only(self) -> None:
        base_document = _document()
        generated_document = OfficialDocument(
            **{
                **base_document.__dict__,
                'explanation': '',
                'generated_description': '本案召開施工協調會議，請相關人員依通知出席並更新提報資料。',
            },
        )
        row = self.pipe._calendar_rows(
            owner_name=self.pipe._calendar_owner_name(generated_document),
            document=generated_document,
            deadlines=[_deadline()],
        )[0]
        self.assertEqual(row['Description'], generated_document.generated_description)
        self.assertEqual(
            self.pipe.pdf_filename(generated_document),
            self.pipe.pdf_filename(base_document),
        )
        self.assertEqual(
            self.pipe._docx_filename(generated_document),
            self.pipe._docx_filename(base_document),
        )

    def test_timed_event_calculates_duration_only_from_valid_endpoints(self) -> None:
        event = _deadline(timed=True)
        event = Deadline(
            **{
                **event.__dict__, 'end_date': event.date,
                'end_time': DocumentTime(display='17:00:00', twenty_four_hour='17:00'),
            },
        )
        row = self.pipe._calendar_rows(
            owner_name='鍾羽雯',
            document=_document(),
            deadlines=[event],
        )[0]
        self.assertEqual(row['All day event'], '假')
        self.assertEqual(row['Work Hour'], '1.5')

    def test_formula_cells_are_escaped(self) -> None:
        encoded = self.pipe.calendar_csv([{
            'Name': '=unsafe', 'Subject': '+unsafe',
            'Start Date': '2026/09/10', 'End Date': '2026/09/10', 'All day event': '真',
        }]).decode('utf-8-sig')
        row = list(csv.DictReader(io.StringIO(unquote_plus(encoded))))[0]
        self.assertEqual(row['Name'], "'=unsafe")
        self.assertEqual(row['Subject'], "'+unsafe")

    def test_generated_download_is_direct_eip_format_even_with_legacy_valves(self):
        from examples.official_document_workflow.infrastructure.eip.calendar_csv import parse_csv
        from examples.official_document_workflow.infrastructure.eip.netask import csv_minutes

        self.pipe.valves = Pipe.Valves(include_template_translation_row=True)
        payload = self.pipe.calendar_csv([{
            'Subject': '現勘 2K+500，50%，字面 %2B[AI產生]',
            'Start Date': '2026/10/7', 'End Date': '2026/10/7',
            'Start Time': '14:00:00', 'Start Time(24hr)': '14:00',
            'End Time': '15:00:00', 'End Time(24hr)': '15:00',
            'All day event': '假', 'Private': '真', 'Priority': '普通',
            'Description': '工程里程 2K+500\n字面 %2B，進度 50%',
        }])
        self.assertTrue(payload.startswith(b'\xef\xbb\xbf'))
        direct = list(csv.DictReader(io.StringIO(unquote_plus(payload.decode('utf-8-sig')))))
        self.assertEqual(len(direct), 1)
        self.assertEqual(set(direct[0]), set(PIPE_MODULE.CALENDAR_HEADERS))
        self.assertEqual(direct[0]['Start Date'], '2026/10/07')
        self.assertEqual(direct[0]['Start Time'], '下午02:00:00')
        self.assertEqual(csv_minutes(direct[0]['End Time']), '900')
        self.assertIn('2K+500', direct[0]['Subject'])
        rows, _ = parse_csv(payload, csv_format=PIPE_MODULE.CALENDAR_CSV_FORMAT)
        self.assertEqual(rows[0]['Subject'], direct[0]['Subject'])
        self.assertEqual(rows[0]['Description'], direct[0]['Description'])


class CloudValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.pipe = Pipe()
        self.source = CloudSemanticSource(text=SOURCE, kind='native_pdf_text')

    def test_source_evidenced_payload_is_accepted(self) -> None:
        result = self.pipe._parse_cloud_ocr_text_payload(_valid_payload(), 'DeepSeek-V4-Flash', self.source)
        self.assertEqual(result.document.reference_number, REFERENCE)
        self.assertEqual(result.document.handler_name, '鍾羽雯')
        self.assertIn('重新通盤檢討', result.document.explanation)
        self.assertEqual(result.document.generated_description, '')
        self.assertEqual(len(result.deadlines), 1)
        self.assertEqual(result.deadlines[0].date.gregorian, '2026/09/10')

    def test_notice_subject_preserves_quotes_and_requires_literal_evidence(self) -> None:
        subject = '「甲工程」路口號誌、路燈遷移及防汛道路封閉事宜會勘'
        source = CloudSemanticSource(
            text=SOURCE + '\n會勘事由：' + subject,
            kind='paddle_layout_ocr',
        )
        payload = _valid_payload()
        payload['document']['subject'] = subject
        payload['document']['filename_subject'] = '「甲工程」路口號誌'
        payload['document']['evidence']['subject'] = subject
        payload['document']['evidence']['filename_subject'] = '「甲工程」路口號誌'
        result = self.pipe._parse_cloud_ocr_text_payload(payload, 'DeepSeek-V4-Flash', source)
        self.assertEqual(result.document.subject, subject)
        self.assertIn('「甲工程」', self.pipe.pdf_filename(result.document))
        # Deleting punctuation is still rejected, not hidden by fuzzy matching.
        payload['document']['subject'] = subject.replace('「', '').replace('」', '')
        payload['document']['evidence']['subject'] = payload['document']['subject']
        with self.assertRaisesRegex(CloudExtractionError, 'subject lacks source evidence'):
            self.pipe._parse_cloud_ocr_text_payload(payload, 'DeepSeek-V4-Flash', source)

    def test_subject_evidence_ignores_pdf_visual_wrap_after_punctuation(self) -> None:
        subject = '工程督導缺失改善成果，經確認已改善完成，請同意辦理。'
        source = '主旨：工程督導缺失改善成果，\n經確認已改善完成，請同意辦理。'
        payload = {'subject': subject}
        evidence = {'subject': subject}
        self.assertEqual(
            self.pipe._cloud_document_field(
                payload,
                evidence,
                'subject',
                source,
                maximum_characters=1200,
                required=True,
            ),
            subject,
        )
        payload['subject'] = subject.replace('，', '')
        evidence['subject'] = payload['subject']
        with self.assertRaisesRegex(
            CloudExtractionError,
            'subject lacks source evidence',
        ):
            self.pipe._cloud_document_field(
                payload,
                evidence,
                'subject',
                source,
                maximum_characters=1200,
                required=True,
            )

    def test_prompt_requires_generated_description_for_notice_without_formal_explanation(self) -> None:
        prompt = self.pipe._cloud_ocr_text_prompt('paddle_layout_ocr')
        self.assertIn('開會通知、表單、簡表', prompt)
        self.assertIn('必須選 mode=generated', prompt)

    def test_verbatim_calendar_description_accepts_ordered_ocr_blocks(self) -> None:
        payload = _valid_payload()
        first = '一、請晉國工程顧問有限公司重新通盤檢討穿越水路或截流溝。'
        second = '二、並於115年9月10日前提送評估報告予本段續辦。'
        document = payload['document']  # type: ignore[index]
        document['calendar_description'] = {  # type: ignore[index]
            'mode': 'verbatim',
            'value': f"{first}\n{second}",
            'evidence': [first, second],
        }
        source = CloudSemanticSource(
            text=f"{SOURCE}\n<ocr-block>{first}</ocr-block>\n<ocr-block>{second}</ocr-block>",
            kind='paddle_layout_ocr',
        )
        result = self.pipe._parse_cloud_ocr_text_payload(payload, 'DeepSeek-V4-Flash', source)
        self.assertEqual(result.document.explanation, f"{first}\n{second}")
        self.assertEqual(result.document.generated_description, '')

    def test_cross_page_description_keeps_complete_value_with_split_evidence(self) -> None:
        first, second = '二、為利通行安', '全，同意封閉道路。'
        source = f'<page index="1"><ocr-block>{first}</ocr-block>第1頁，共2頁</page>\n<page index="2"><ocr-block>{second}</ocr-block></page>'
        payload = {'calendar_description': {'mode': 'verbatim', 'value': first + second, 'evidence': [first, second]}}
        self.assertEqual(self.pipe._cloud_calendar_description_from_payload(payload, source), (first + second, ''))
        payload['calendar_description']['evidence'] = [first + second]
        with self.assertRaisesRegex(CloudExtractionError, 'lacks source evidence'):
            self.pipe._cloud_calendar_description_from_payload(payload, source)
        self.assertIn('跨頁句子必須拆成多個 evidence 元素', self.pipe._cloud_ocr_text_prompt('paddle_layout_ocr'))

    def test_long_formal_explanation_is_not_limited_to_short_evidence(self) -> None:
        explanation = '一、' + '請依附件辦理，並完整保留各項說明。' * 100
        result = self.pipe._cloud_calendar_description_from_payload(
            {'calendar_description': {'mode': 'verbatim', 'value': explanation, 'evidence': [explanation]}},
            '說明：\n' + explanation,
        )
        self.assertEqual(result, (explanation, ''))

    def test_generated_calendar_description_requires_literal_evidence(self) -> None:
        payload = _valid_payload()
        document = payload['document']  # type: ignore[index]
        document['calendar_description'] = {  # type: ignore[index]
            'mode': 'generated',
            'value': '本案要求完成檢討並提送評估報告。',
            'evidence': [DEADLINE_EVIDENCE],
        }
        result = self.pipe._parse_cloud_ocr_text_payload(payload, 'DeepSeek-V4-Flash', self.source)
        self.assertEqual(result.document.explanation, '')
        self.assertEqual(result.document.generated_description, '本案要求完成檢討並提送評估報告。')

    def test_missing_or_incomplete_calendar_description_is_terminal(self) -> None:
        payload = _valid_payload()
        document = payload['document']  # type: ignore[index]
        document.pop('calendar_description')  # type: ignore[index]
        with self.assertRaises(CloudExtractionError):
            self.pipe._parse_cloud_ocr_text_payload(payload, 'DeepSeek-V4-Flash', self.source)

    def test_calendar_description_cannot_duplicate_subject(self) -> None:
        payload = _valid_payload()
        document = payload['document']  # type: ignore[index]
        document['calendar_description'] = {  # type: ignore[index]
            'mode': 'verbatim', 'value': SUBJECT, 'evidence': [SUBJECT],
        }
        with self.assertRaises(CloudExtractionError):
            self.pipe._parse_cloud_ocr_text_payload(payload, 'DeepSeek-V4-Flash', self.source)

        payload = _valid_payload()
        document = payload['document']  # type: ignore[index]
        document['calendar_description'] = {  # type: ignore[index]
            'mode': 'verbatim',
            'value': '模型截斷的說明',
            'evidence': ['模型截斷的說明'],
        }
        with self.assertRaises(CloudExtractionError):
            self.pipe._parse_cloud_ocr_text_payload(payload, 'DeepSeek-V4-Flash', self.source)

    def test_unverified_required_document_value_is_terminal(self) -> None:
        payload = _valid_payload()
        payload['document']['subject'] = '模型杜撰的主旨'  # type: ignore[index]
        with self.assertRaises(CloudExtractionError):
            self.pipe._parse_cloud_ocr_text_payload(payload, 'DeepSeek-V4-Flash', self.source)

    def test_missing_events_array_is_terminal_schema_error(self) -> None:
        payload = _valid_payload()
        payload.pop('events')
        with self.assertRaises(CloudExtractionError):
            self.pipe._parse_cloud_ocr_text_payload(payload, 'DeepSeek-V4-Flash', self.source)

    def test_unverified_optional_event_fields_are_blank_not_inferred(self) -> None:
        payload = _valid_payload()
        event = payload['events'][0]  # type: ignore[index]
        event['category'] = {'value': '任意分類', 'evidence': '不存在'}
        event['private'] = {'value': '真', 'evidence': '不存在'}
        result = self.pipe._parse_cloud_ocr_text_payload(payload, 'DeepSeek-V4-Flash', self.source)
        self.assertEqual(result.deadlines[0].category, '')
        self.assertEqual(result.deadlines[0].private, '')

    def test_responses_envelope_text_is_not_an_analysis_fallback(self) -> None:
        response = {
            'output': [
                {
                    'type': 'reasoning',
                    'content': [{'type': 'output_text', 'text': 'Synthetic non-answer text'}],
                },
                {
                    'type': 'message',
                    'content': [
                        {'type': 'output_text', 'text': '{"document":{},"events":[]}'},
                    ],
                },
            ],
        }
        self.assertEqual(
            Pipe._responses_output_text(response),
            '{"document":{},"events":[]}',
        )


class CanonicalSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.pipe = Pipe()

    def test_native_pdf_uses_raw_text_only(self) -> None:
        with patch.object(self.pipe, '_run_text', return_value='第一頁\f第二頁') as run_text:
            source = self.pipe.semantic_source(Path('native.pdf'), (), None)
        self.assertEqual(source.kind, 'native_pdf_text')
        self.assertIn('<page index="1">', source.text)
        self.assertIn('<page index="2">', source.text)
        run_text.assert_called_once_with(['pdftotext', '-raw', 'native.pdf', '-'], timeout_seconds=30)

    def test_native_blank_pages_keep_physical_numbers_without_trailing_phantom_page(self):
        from examples.official_document_workflow.application.upload_batch import source_pages
        structural = set()
        text = self.pipe._page_tagged_cloud_text('first\f\fthird\f', structural_lines=structural)
        source = PIPE_MODULE.CloudSemanticSource(text, 'native_pdf_text', frozenset(structural))
        self.assertEqual(list(source_pages({'S1': source})), ['S1:P1', 'S1:P2', 'S1:P3'])
        self.assertNotIn('<page index="4">', text)

    def test_ocr_blank_page_still_has_a_physical_page_marker(self):
        import copy
        from examples.official_document_workflow.application.upload_batch import source_pages
        response = _layout_response()
        blank = copy.deepcopy(response['result']['layoutParsingResults'][0])
        blank['prunedResult']['parsing_res_list'] = []
        response['result']['layoutParsingResults'].append(blank)
        structural = set()
        text = self.pipe._paddleocr_vl_cloud_text(response, structural_lines=structural)
        source = PIPE_MODULE.CloudSemanticSource(text, 'paddle_layout_ocr', frozenset(structural))
        self.assertEqual(list(source_pages({'S1': source})), ['S1:P1', 'S1:P2'])

    def test_scan_or_mixed_pdf_requires_paddle_layout_text(self) -> None:
        with self.assertRaises(ProcessingError):
            self.pipe.semantic_source(Path('scan.pdf'), (0,), None)
        source = self.pipe.semantic_source(Path('scan.pdf'), (0,), _layout_response())
        self.assertEqual(source.kind, 'paddle_layout_ocr')
        self.assertIn('第一行第二行', source.text)

    def test_layout_block_reflow_only_folds_visual_lines_inside_that_block(self) -> None:
        source = self.pipe.semantic_source(
            Path('scan.pdf'),
            (0,),
            _layout_response('第一行\n第二行\n\n第三段'),
        )
        self.assertIn('第一行第二行\n\n第三段', source.text)

    def test_canonical_context_is_an_instance_method_and_selects_one_source(self) -> None:
        with patch.object(self.pipe, '_pages_without_native_pdf_text', return_value=()):
            scan_pages, layout = asyncio.run(self.pipe.ocr_context(Path('native.pdf')))
        self.assertEqual(scan_pages, ())
        self.assertIsNone(layout)

        response = _layout_response()
        with patch.object(self.pipe, '_pages_without_native_pdf_text', return_value=(0,)), patch.object(
            self.pipe, '_paddleocr_vl_response', return_value=response,
        ) as paddle:
            scan_pages, layout = asyncio.run(self.pipe.ocr_context(Path('scan.pdf')))
        self.assertEqual(scan_pages, (0,))
        self.assertIs(layout, response)
        paddle.assert_called_once_with(Path('scan.pdf'))

    def test_oversized_text_is_rejected_instead_of_truncated_or_retried(self) -> None:
        self.pipe.valves.cloud_ocr_text_max_input_characters = 10
        with self.assertRaises(ProcessingError):
            self.pipe._bounded_cloud_semantic_text('x' * 11)

    def test_removed_fallback_valves_and_methods_are_not_available(self) -> None:
        for name in ('enable_direct_cloud_pdf', 'cloud_primary_input_mode', 'cloud_direct_pdf_model', 'enable_cloud_review', 'allow_tesseract_fallback', 'prefer_positioned_ocr_docx_for_pdf'):
            self.assertFalse(hasattr(self.pipe.valves, name), name)
        for name in ('_cloud_pdf_extraction_if_enabled', '_cloud_calendar_extraction_if_enabled', '_extract_document', '_extract_deadlines', '_ocr_pdf_tesseract', '_layout_docx_bytes', '_pdf_to_docx_bytes'):
            self.assertFalse(hasattr(self.pipe, name), name)

    def test_shared_word_layout_retains_pages_without_text(self):
        response = {
            'result': {
                'layoutParsingResults': [{
                    'prunedResult': {
                        'width': 1000, 'height': 1400, 'spotting_res': {'rec_texts': [], 'rec_polys': []},
                    },
                }],
            },
        }
        with self.assertRaises(ProcessingError):
            self.pipe.paddleocr_vl_spotting_pages(response)
        pages = self.pipe.paddleocr_vl_spotting_pages(response, allow_empty=True)
        self.assertEqual(len(pages), 1)
        self.assertEqual(pages[0].blocks, ())
        self.assertEqual((pages[0].width, pages[0].height), (1000, 1400))

    def test_editable_docx_has_one_required_line_coordinate_signature(self) -> None:
        self.assertNotIn('line_coordinates', inspect.signature(self.pipe.render_word).parameters)


class CloudRequestTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_model_outputs_must_decide_tentative_schedule_without_changing_legacy_schemas(self):
        from examples.official_document_workflow.application.upload_batch import UploadAnalysis
        from examples.official_document_workflow.domain.calendar.contracts import GROUP_ANALYSIS_SCHEMA
        from examples.official_document_workflow.domain.calendar.contracts import SINGLE_ANALYSIS_SCHEMA

        for schema, event_type in (
            (GROUP_ANALYSIS_SCHEMA, 'CalendarEvent'),
            (SINGLE_ANALYSIS_SCHEMA, 'SingleEvent'),
            (UploadAnalysis.model_json_schema(), 'CalendarEvent'),
        ):
            with self.subTest(event_type=event_type):
                before = json.dumps(schema, sort_keys=True)
                pipe = Pipe()
                pipe.valves.cloud_ocr_text_model = 'gemini-3.8-flash'
                client = AsyncMock()
                client.__aenter__.return_value = client
                client.post.return_value = PIPE_MODULE.httpx.Response(
                    200, json={'output_text': '{"events":[]}'},
                )
                with patch.object(pipe, '_official_document_litellm_connection', return_value=('http://localhost/v1', 'test-key')), \
                        patch.object(PIPE_MODULE.httpx, 'AsyncClient', return_value=client):
                    await pipe.analyze_text(CloudSemanticSource('原文', 'native_pdf_text'), schema=schema)
                sent = client.post.await_args.kwargs['json']['text']['format']['schema']
                event_schema = sent['$defs'][event_type]
                self.assertIn('tentative_schedule', event_schema['required'])
                self.assertIn({'type': 'null'}, event_schema['properties']['tentative_schedule']['anyOf'])
                self.assertEqual(json.dumps(schema, sort_keys=True), before)

    async def test_gemini_models_send_configured_thinking_and_parse_only_answer(self):
        from examples.official_document_workflow.application.upload_batch import AttachmentGroups
        schema = AttachmentGroups.model_json_schema()
        for model in ('gemini-3.5-flash-lite', 'gemini-3.8-flash', 'gemini-4-argon'):
            with self.subTest(model=model):
                pipe = Pipe()
                pipe.valves = Pipe.Valves(cloud_ocr_text_model=model, cloud_ocr_text_reasoning_effort='medium')
                client = AsyncMock()
                client.__aenter__.return_value = client
                client.post.return_value = PIPE_MODULE.httpx.Response(
                    200, json={
                        'status': 'completed', 'output': [
                            {'type': 'reasoning', 'content': [{'type': 'output_text', 'text': 'Synthetic non-answer'}]},
                            {'type': 'message', 'content': [{'type': 'output_text', 'text': '{"groups":[["S1"]]}'}]},
                        ],
                    },
                )
                with patch.object(pipe, '_official_document_litellm_connection', return_value=('http://localhost/v1', 'test-key')), \
                        patch.object(PIPE_MODULE.httpx, 'AsyncClient', return_value=client):
                    result = await pipe.analyze_text(CloudSemanticSource('原文', 'native_pdf_text'), schema=schema)
                self.assertEqual(result, {'groups': [['S1']]})
                self.assertEqual(client.post.await_args.kwargs['json']['reasoning'], {'effort': 'medium'})
                self.assertEqual(
                    client.post.await_args.kwargs['json']['text']['format'], {
                        'type': 'json_schema', 'name': 'document_result', 'schema': schema, 'strict': True,
                    },
                )
                self.assertFalse(client.post.await_args.kwargs['json']['store'])
                client.post.assert_awaited_once()

    async def test_batch_output_budget_is_explicit_and_truncation_is_not_resubmitted(self):
        for status in ('completed', 'incomplete'):
            pipe = Pipe()
            client = AsyncMock()
            client.__aenter__.return_value = client
            client.post.return_value = PIPE_MODULE.httpx.Response(
                200, json={
                    'status': status, 'incomplete_details': {'reason': 'max_output_tokens'},
                    'output_text': '{"groups":[]}',
                },
            )
            with self.subTest(status=status), \
                    patch.object(pipe, '_official_document_litellm_connection', return_value=('http://localhost/v1', 'test-key')), \
                    patch.object(PIPE_MODULE.httpx, 'AsyncClient', return_value=client):
                if status == 'incomplete':
                    with self.assertRaisesRegex(ProcessingError, '未完整'):
                        await pipe.analyze_text(CloudSemanticSource('原文', 'native_pdf_text'), max_output_tokens=3000)
                else:
                    result = await pipe.analyze_text(CloudSemanticSource('原文', 'native_pdf_text'), max_output_tokens=9000)
                    self.assertEqual(result, {'groups': []})
                    self.assertEqual(client.post.await_args.kwargs['json']['max_output_tokens'], 9000)
                client.post.assert_awaited_once()

    async def test_other_providers_keep_their_existing_json_mode(self):
        pipe = Pipe()
        pipe.valves.cloud_ocr_text_model = 'DeepSeek-V4-Flash'
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.post.return_value = PIPE_MODULE.httpx.Response(200, json={'output_text': '{"events":[]}'})
        with patch.object(pipe, '_official_document_litellm_connection', return_value=('http://localhost/v1', 'test-key')), \
                patch.object(PIPE_MODULE.httpx, 'AsyncClient', return_value=client):
            await pipe.analyze_text(CloudSemanticSource('原文', 'native_pdf_text'), schema={'type': 'object'})
        request = client.post.await_args.kwargs['json']
        self.assertEqual(request['text']['format'], {'type': 'json_object'})
        self.assertNotIn('reasoning', request)

    async def test_temporary_transport_and_http_errors_are_distinct_from_bad_requests(self):
        pipe = Pipe()
        source = CloudSemanticSource(text=SOURCE, kind='native_pdf_text')
        cases = [(status, True) for status in (408, 429, 500, 502, 503, 504)] + \
            [(401, False), (400, False), (200, False)]
        for status, temporary in cases + [(None, True)]:
            with self.subTest(status=status):
                client = AsyncMock()
                client.__aenter__.return_value = client
                if status is None:
                    client.post.side_effect = PIPE_MODULE.httpx.ReadTimeout('private transport details')
                else:
                    client.post.return_value = PIPE_MODULE.httpx.Response(status, json={'bad': 'response'})
                with patch.object(pipe, '_official_document_litellm_connection', return_value=('http://localhost/v1', 'test-key')), \
                        patch.object(PIPE_MODULE.httpx, 'AsyncClient', return_value=client):
                    with self.assertRaises(ProcessingError) as caught:
                        await pipe.analyze_text(source)
                self.assertEqual(isinstance(caught.exception, PIPE_MODULE.CloudTemporaryFailure), temporary)
                self.assertNotIn('private transport details', str(caught.exception))
                self.assertNotIn('等待下輪續跑', str(caught.exception))
                client.post.assert_awaited_once()

    async def test_request_sends_only_text_and_never_a_pdf_attachment(self) -> None:
        pipe = Pipe()
        captured: dict[str, object] = {}

        class Response:
            status_code = 200

            @staticmethod
            def json() -> dict[str, object]:
                return {'output_text': json.dumps(_valid_payload(), ensure_ascii=False)}

        class Client:
            async def __aenter__(self) -> Client: return self
            async def __aexit__(self, *_: object) -> None: return None

            async def post(self, _url: str, *, json: dict[str, object], **_kwargs: object) -> Response:
                captured.update(json)
                return Response()

        source = CloudSemanticSource(text=SOURCE, kind='native_pdf_text')
        with patch.object(pipe, '_official_document_litellm_connection', return_value=('http://127.0.0.1:4000/v1', 'key')), patch.object(PIPE_MODULE.httpx, 'AsyncClient', side_effect=lambda **_kwargs: Client()):
            result = await pipe._cloud_ocr_text_document_extraction(source)
        messages = captured['input']
        self.assertEqual([item['role'] for item in messages], ['developer', 'user'])
        self.assertNotIn(SOURCE, messages[0]['content'][0]['text'])
        self.assertIn(SOURCE, messages[1]['content'][0]['text'])
        content = [part for message in messages for part in message['content']]
        self.assertEqual([part['type'] for part in content], ['input_text', 'input_text'])
        self.assertTrue(all('file_data' not in part for part in content))
        self.assertEqual(captured['store'], False)
        self.assertEqual(result.document.subject, SUBJECT)

    async def test_cloud_failure_is_terminal_before_export(self) -> None:
        pipe = Pipe()
        source_file = types.SimpleNamespace(id='source', filename='source.pdf')
        prepared = PreparedDocument(kind='pdf', source_files=(source_file,), pdf_path=Path('source.pdf'))
        pipe.ocr_context = AsyncMock(return_value=((), None))
        pipe.semantic_source = MagicMock(
            return_value=CloudSemanticSource(text=SOURCE, kind='native_pdf_text'),
        )
        pipe._cloud_ocr_text_document_extraction = AsyncMock(side_effect=ProcessingError('cloud failure'))
        pipe.paddleocr_vl_spotting_response = MagicMock(return_value={})
        pipe._create_or_rename_pdf = AsyncMock(side_effect=AssertionError('must not export after cloud failure'))
        with self.assertRaisesRegex(ProcessingError, 'cloud failure'):
            await pipe._process_prepared_document(prepared, 'user', force_derived_pdf=True)
        pipe._create_or_rename_pdf.assert_not_awaited()

    async def test_required_spotting_ocr_failure_is_terminal_before_export(self) -> None:
        pipe = Pipe()
        source_file = types.SimpleNamespace(id='source', filename='source.pdf')
        prepared = PreparedDocument(kind='pdf', source_files=(source_file,), pdf_path=Path('source.pdf'))
        pipe.ocr_context = AsyncMock(return_value=((0,), {}))
        pipe.semantic_source = MagicMock(
            return_value=CloudSemanticSource(text=SOURCE, kind='paddle_layout_ocr'),
        )
        pipe._cloud_ocr_text_document_extraction = AsyncMock(
            return_value=CloudDocumentExtraction('DeepSeek-V4-Flash', _document(), (_deadline(),)),
        )
        pipe.paddleocr_vl_spotting_response = MagicMock(side_effect=ProcessingError('spotting failure'))
        pipe._create_or_rename_pdf = AsyncMock(side_effect=AssertionError('must not export after OCR failure'))
        with self.assertRaisesRegex(ProcessingError, 'spotting failure'):
            await pipe._process_prepared_document(prepared, 'user', force_derived_pdf=True)
        pipe._create_or_rename_pdf.assert_not_awaited()

    async def test_user_can_skip_docx_without_starting_positioned_ocr(self) -> None:
        pipe = Pipe()
        source_file = types.SimpleNamespace(id='source', filename='source.pdf')
        prepared = PreparedDocument(kind='pdf', source_files=(source_file,), pdf_path=Path('source.pdf'))
        pipe.ocr_context = AsyncMock(return_value=((), None))
        pipe.semantic_source = MagicMock(
            return_value=CloudSemanticSource(text=SOURCE, kind='native_pdf_text'),
        )
        pipe._cloud_ocr_text_document_extraction = AsyncMock(
            return_value=CloudDocumentExtraction('DeepSeek-V4-Flash', _document(), (_deadline(),)),
        )
        pipe.paddleocr_vl_spotting_response = MagicMock(
            side_effect=AssertionError('positioned OCR must be skipped'),
        )

        selection = PIPE_MODULE.OutputSelection(
            renamed_pdf=False,
            searchable_ocr_pdf=True,  # persisted legacy plan cannot restore OCR export
            editable_docx=False,
            calendar_csv=True,
        )
        result = await pipe._process_prepared_document(
            prepared,
            'user',
            force_derived_pdf=True,
            output_selection=selection,
        )

        pipe.paddleocr_vl_spotting_response.assert_not_called()
        self.assertIsNone(result.docx_file)
        self.assertIsNone(result.output_pdf)
        self.assertIsNone(result.searchable_pdf_file)


class ControlledInstructionRequestTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _plan_payload() -> dict[str, object]:
        return {
            'version': 1,
            'scope': 'current_uploads',
            'outputs': {
                'renamed_pdf': True,
                'searchable_ocr_pdf': True,
                'editable_docx': True,
                'calendar_csv': True,
            },
            'calendar_operations': [],
            'eip_calendar': {'request': 'none'},
            'needs_clarification': False,
            'clarification': None,
        }

    async def test_planner_request_sends_only_latest_instruction_as_user_content(self) -> None:
        pipe = Pipe()
        captured: dict[str, object] = {}

        class Response:
            status_code = 200

            @staticmethod
            def json() -> dict[str, object]:
                return {'output_text': json.dumps(self._plan_payload(), ensure_ascii=False)}

        class Client:
            async def __aenter__(self) -> Client:
                return self

            async def __aexit__(self, *_: object) -> None:
                return None

            async def post(self, _url: str, *, json: dict[str, object], **_kwargs: object) -> Response:
                captured.update(json)
                return Response()

        instruction = '在每一個已驗證期限後七天新增全天追蹤提醒'
        with patch.object(
            pipe,
            '_official_document_litellm_connection',
            return_value=('http://127.0.0.1:4000/v1', 'key'),
        ), patch.object(
            PIPE_MODULE.httpx,
            'AsyncClient',
            side_effect=lambda **_kwargs: Client(),
        ):
            payload = await pipe._cloud_instruction_plan_payload(instruction)

        self.assertEqual(payload['scope'], 'current_uploads')
        input_items = captured['input']  # type: ignore[index]
        self.assertEqual([item['role'] for item in input_items], ['developer', 'user'])
        developer_content = input_items[0]['content'][0]['text']  # type: ignore[index]
        user_content = input_items[1]['content'][0]['text']  # type: ignore[index]
        self.assertIn('受控工作規劃器', developer_content)
        self.assertIn(instruction, user_content)
        self.assertNotIn('file_id', json.dumps(captured, ensure_ascii=False))
        self.assertEqual(captured['store'], False)

    async def test_planner_reads_only_last_user_turn_from_openwebui_body(self) -> None:
        pipe = Pipe()
        pipe._cloud_instruction_plan_payload = AsyncMock(return_value=self._plan_payload())
        body = {
            'messages': [
                {'role': 'user', 'content': '舊附件：不要輸出 DOCX'},
                {'role': 'assistant', 'content': '已完成'},
                {'role': 'user', 'content': '這次新附件全部輸出'},
            ],
        }

        plan = await pipe.plan_current_user_request(body)

        pipe._cloud_instruction_plan_payload.assert_awaited_once_with('這次新附件全部輸出')
        self.assertTrue(plan.outputs.editable_docx)

    async def test_invalid_upload_is_rejected_before_planner_service_is_called(self) -> None:
        pipe = Pipe()
        pipe.owned_sources = AsyncMock(side_effect=ProcessingError('沒有附件'))
        pipe.plan_current_user_request = AsyncMock(return_value=self._plan_payload())

        response = await pipe.pipe(
            {'messages': [{'role': 'user', 'content': '處理這份公文'}]},
            __user__={'id': 'user'},
            __files__=[],
        )

        self.assertIn('沒有附件', response)
        pipe.plan_current_user_request.assert_not_awaited()


class AuxiliaryTaskTests(unittest.IsolatedAsyncioTestCase):
    async def test_tasks_with_inherited_attachments_never_enter_document_processing(self):
        pipe = Pipe()
        pipe.owned_sources = AsyncMock(side_effect=AssertionError('task read files'))
        pipe.prepare_owned_source_documents = AsyncMock(side_effect=AssertionError('task prepared files'))
        emitter = AsyncMock()
        expected = {
            'title_generation': {'title': ''},
            'tags_generation': {'tags': []},
            'follow_up_generation': {'follow_ups': []},
            'query_generation': {'queries': []},
            'emoji_generation': {},
            'image_prompt_generation': {},
            'autocomplete_generation': {},
            'function_calling': {},
            'moa_response_generation': {},
            'context_compaction': {},
            'memory_review': {},
            'future_auxiliary_task': {},
        }
        with patch.object(pipe.clarifications.pending, 'discard') as discard:
            for task, response in expected.items():
                with self.subTest(task=task):
                    result = await pipe.pipe(
                        {'messages': [{'role': 'user', 'content': '處理附件'}]},
                        __user__={'id': 'owner'}, __files__=[{'id': 'file-1'}],
                        __chat_id__='chat-1', __task__=task, __event_emitter__=emitter,
                    )
                    self.assertEqual(json.loads(result), response)
            discard.assert_not_called()
        pipe.owned_sources.assert_not_awaited()
        pipe.prepare_owned_source_documents.assert_not_awaited()
        emitter.assert_not_awaited()


class FixedUploadExportTests(unittest.IsolatedAsyncioTestCase):
    async def run_upload(self, message, *, dates=True):
        pipe = Pipe()
        source = types.SimpleNamespace(
            source_file=types.SimpleNamespace(filename='source-1.pdf'), cleanup=MagicMock(),
        )
        result = types.SimpleNamespace(
            document=types.SimpleNamespace(handler_name='承辦人'),
            deadlines=(object(),) if dates else (),
            output_pdf=types.SimpleNamespace(id='pdf-1'), output_pdf_name='改名-1.pdf',
            docx_file=types.SimpleNamespace(id='word-1'), docx_name='改名-1.docx',
        )
        pipe.plan_current_user_request = AsyncMock(side_effect=AssertionError('chat must not plan'))
        pipe.owned_sources = AsyncMock(return_value=('image', ()))
        pipe.prepare_owned_source_documents = AsyncMock(return_value=(source,))
        pipe._process_prepared_document = AsyncMock(return_value=result)
        pipe._calendar_rows = MagicMock(return_value=[{'Subject': '已驗證事件'}])
        pipe._store_batch_calendar_csv = AsyncMock(return_value=('行事曆.csv', types.SimpleNamespace(id='csv-1')))
        output = await pipe.pipe(
            {'messages': [{'role': 'user', 'content': message}]},
            __user__={'id': 'user'}, __files__=[{'id': 'file-1'}],
        )
        pipe.plan_current_user_request.assert_not_awaited()
        self.assertEqual(
            pipe._process_prepared_document.await_args.kwargs['output_selection'], PIPE_MODULE.OutputSelection(),
        )
        source.cleanup.assert_called_once()
        self.assertIn('pdf-1', output)
        self.assertIn('word-1', output)
        self.assertNotIn('/eip', output)
        return pipe, output

    async def test_uploaded_file_exports_without_interpreting_chat_text(self):
        for message in ('滿難的', '', '如何產生uuid', '不要Word，直接匯入EIP'):
            with self.subTest(message=message):
                pipe, output = await self.run_upload(message)
                self.assertIn('csv-1', output)
                pipe._store_batch_calendar_csv.assert_awaited_once()
                self.assertEqual(len(pipe._store_batch_calendar_csv.await_args.kwargs['calendar_rows']), 1)

    async def test_no_events_keeps_pdf_and_word_without_inventing_csv(self):
        pipe, output = await self.run_upload('滿難的', dates=False)
        pipe._store_batch_calendar_csv.assert_not_awaited()
        self.assertIn('未產生 CSV', output)


class PendingCalendarClarificationPipeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.pipe = Pipe()
        self.scope = PIPE_MODULE.PendingClarificationScope(
            user_id='user',
            chat_id='chat',
        )
        self.pipe.clarifications.pending.discard(self.scope)

    def tearDown(self) -> None:
        self.pipe.clarifications.pending.discard(self.scope)

    @staticmethod
    def _plan() -> object:
        return PIPE_MODULE.JobPlan.model_validate(
            {
                'version': 1,
                'scope': 'current_uploads',
                'outputs': {
                    'renamed_pdf': True,
                    'searchable_ocr_pdf': True,
                    'editable_docx': True,
                    'calendar_csv': True,
                },
                'calendar_operations': [
                    {
                        'operation': 'add_relative_reminder',
                        'anchor': 'verified_document_deadline',
                        'target': 'single_verified_deadline',
                        'offset_days': 7,
                        'title': '追蹤進度',
                        'all_day': True,
                    },
                ],
                'eip_calendar': {'request': 'none'},
                'needs_clarification': False,
                'clarification': None,
            },
        )

    def _pending_payload(self) -> tuple[object, types.SimpleNamespace]:
        second_deadline = Deadline(
            **{
                **_deadline().__dict__,
                'date': _date('115', '09', '11'),
                'action': '回覆第二項資料',
            },
        )
        rows = tuple(
            self.pipe._calendar_rows(
                owner_name='鍾羽雯',
                document=_document(),
                deadlines=[_deadline(), second_deadline],
            ),
        )
        source_file = types.SimpleNamespace(
            id='source-file',
            hash='stable-hash',
            filename='source.pdf',
        )
        payload = PIPE_MODULE.PendingCalendarReminder(
            calendar_rows=rows,
            job_plan=self._plan(),
            selected_deadline_indices=(),
            unresolved_operation_index=0,
            source_file_ids=('source-file',),
            csv_base_name='行事曆_test.csv',
            batch_mode=False,
        )
        return payload, source_file

    async def test_same_scoped_follow_up_creates_replacement_csv_without_reupload(self) -> None:
        payload, source_file = self._pending_payload()
        fingerprint = self.pipe._attachment_fingerprint((source_file,))
        self.pipe.clarifications.pending.put(
            self.scope,
            attachment_fingerprint=fingerprint,
            payload=payload,
            ttl_seconds=60,
        )
        self.pipe._resolve_pending_reminder_choice = AsyncMock(
            return_value=PIPE_MODULE.PendingReminderResolution(
                decision='all_verified_deadlines',
                selected_deadline_index=None,
                clarification=None,
            ),
        )
        self.pipe.exports.delivery.writer = AsyncMock(side_effect=lambda spec: types.SimpleNamespace(id=spec.id))
        self.pipe.exports.store_output = AsyncMock(wraps=self.pipe.exports.store_output)
        with patch.object(
            PIPE_MODULE.Files,
            'get_file_by_id_and_user_id',
            new=AsyncMock(return_value=source_file),
            create=True,
        ):
            response = await self.pipe.clarifications.complete(
                body={'messages': [{'role': 'user', 'content': '全部都加'}]},
                user_id='user',
                scope=self.scope,
                request=None,
            )

        self.assertIsNotNone(response)
        self.assertIn('更新後行事曆 CSV', response or '')
        self.assertIn('/api/v1/files/' + self.pipe.exports.store_output.await_args.kwargs['output_id'] + '/content', response or '')
        self.pipe._resolve_pending_reminder_choice.assert_awaited_once_with(
            '全部都加', ('2026/09/10', '2026/09/11'),
        )
        stored_bytes = self.pipe.exports.store_output.await_args.kwargs['payload']
        self.assertEqual(len(list(csv.DictReader(io.StringIO(stored_bytes.decode('utf-8-sig'))))), 4)
        self.assertIsNone(self.pipe.clarifications.pending.get(self.scope))

    async def test_cancelled_choice_restores_original_scope_and_expiry(self):
        import asyncio
        payload, source_file = self._pending_payload()
        store = self.pipe.clarifications.pending
        store.put(self.scope, attachment_fingerprint=self.pipe._attachment_fingerprint((source_file,)), payload=payload, ttl_seconds=60)
        original_expiry = store.get(self.scope).expires_at
        entered = asyncio.Event()

        async def wait(*args):
            entered.set()
            await asyncio.Event().wait()

        self.pipe._resolve_pending_reminder_choice = wait
        with patch.object(PIPE_MODULE.Files, 'get_file_by_id_and_user_id', AsyncMock(return_value=source_file), create=True):
            task = asyncio.create_task(self.pipe.clarifications.complete(
                body={'messages': [{'role': 'user', 'content': '全部'}]}, user_id='user', scope=self.scope, request=None,
            ))
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        restored = store.get(self.scope)
        self.assertIsNotNone(restored)
        self.assertEqual(restored.expires_at, original_expiry)

    async def test_cancel_during_database_claim_drains_and_restores_it(self):
        from threading import Event
        payload, source_file = self._pending_payload()
        store = self.pipe.clarifications.pending
        store.put(self.scope, attachment_fingerprint=self.pipe._attachment_fingerprint((source_file,)), payload=payload, ttl_seconds=60)
        entered, release = Event(), Event()
        original = store.claim
        def claim(*args, **kwargs):
            entered.set()
            if not release.wait(3):
                raise AssertionError('claim timed out')
            return original(*args, **kwargs)
        with patch.object(store, 'claim', side_effect=claim), patch.object(
            PIPE_MODULE.Files, 'get_file_by_id_and_user_id', AsyncMock(return_value=source_file), create=True,
        ):
            task = asyncio.create_task(self.pipe.clarifications.complete(
                body={'messages': [{'role': 'user', 'content': '全部'}]}, user_id='user', scope=self.scope, request=None,
            ))
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            task.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertIsNotNone(store.get(self.scope))

    async def test_reply_without_upload_reaches_only_the_saved_continuation(self):
        payload, source_file = self._pending_payload()
        self.pipe.clarifications.pending.put(
            self.scope, attachment_fingerprint=self.pipe._attachment_fingerprint((source_file,)),
            payload=payload, ttl_seconds=60,
        )
        self.pipe._resolve_pending_reminder_choice = AsyncMock(return_value=PIPE_MODULE.PendingReminderResolution(decision='all_verified_deadlines'))
        self.pipe.exports.delivery.writer = AsyncMock(side_effect=lambda spec: types.SimpleNamespace(id=spec.id))
        self.pipe.exports.store_output = AsyncMock(wraps=self.pipe.exports.store_output)
        self.pipe.owned_sources = AsyncMock(side_effect=AssertionError('must not repeat file processing'))
        with patch.object(PIPE_MODULE.Files, 'get_file_by_id_and_user_id', AsyncMock(return_value=source_file), create=True):
            output = await self.pipe.pipe(
                {'messages': [{'role': 'user', 'content': '全部'}]}, __user__={'id': 'user'}, __chat_id__='chat', __files__=[],
            )
        self.assertIn(self.pipe.exports.store_output.await_args.kwargs['output_id'], output)
        self.pipe.owned_sources.assert_not_awaited()

    def test_pending_question_lists_server_created_deadline_choices(self) -> None:
        payload, _source_file = self._pending_payload()
        question = self.pipe._pending_question(payload, '請指定一項期限')
        self.assertIn('0：2026/09/10', question)
        self.assertIn('1：2026/09/11', question)
        self.assertIn('不需重新上傳附件', question)

    def test_pending_payload_measurement_is_utf8_bounded_material(self) -> None:
        payload, _source_file = self._pending_payload()
        size = self.pipe._pending_payload_size_bytes(payload)
        self.assertGreater(size, 0)
        self.assertGreaterEqual(size, len('鍾羽雯'.encode()))

    def test_oversized_pending_payload_is_not_retained(self) -> None:
        payload, source_file = self._pending_payload()
        self.pipe.valves.pending_clarification_max_payload_bytes = 1
        results = [
            types.SimpleNamespace(
                prepared_source=types.SimpleNamespace(source_files=(source_file,)),
            ),
        ]
        with patch.object(self.pipe.clarifications.pending, 'put') as put:
            retained = self.pipe._retain_pending_calendar_state(
                scope=self.scope,
                results=results,  # type: ignore[arg-type]
                payload=payload,
            )
        self.assertFalse(retained)
        put.assert_not_called()

    async def test_pending_resolver_request_sends_only_latest_answer_and_deadline_dates(self) -> None:
        captured: dict[str, object] = {}

        class Response:
            status_code = 200

            @staticmethod
            def json() -> dict[str, object]:
                return {
                    'output_text': json.dumps(
                        {
                            'version': 1,
                            'decision': 'all_verified_deadlines',
                            'selected_deadline_index': None,
                            'clarification': None,
                        },
                        ensure_ascii=False,
                    ),
                }

        class Client:
            async def __aenter__(self) -> Client:
                return self

            async def __aexit__(self, *_: object) -> None:
                return None

            async def post(self, _url: str, *, json: dict[str, object], **_kwargs: object) -> Response:
                captured.update(json)
                return Response()

        with patch.object(
            self.pipe,
            '_official_document_litellm_connection',
            return_value=('http://127.0.0.1:4000/v1', 'key'),
        ), patch.object(
            PIPE_MODULE.httpx,
            'AsyncClient',
            side_effect=lambda **_kwargs: Client(),
        ):
            payload = await self.pipe._cloud_pending_reminder_choice_payload(
                '全部都加',
                ('2026/09/10', '2026/09/11'),
            )

        self.assertEqual(payload['decision'], 'all_verified_deadlines')
        serialized = json.dumps(captured, ensure_ascii=False)
        self.assertIn('2026/09/10', serialized)
        self.assertIn('全部都加', serialized)
        self.assertNotIn(DEADLINE_EVIDENCE, serialized)
        self.assertNotIn('source-file', serialized)
        self.assertEqual(captured['store'], False)


class InputAndNameTests(unittest.TestCase):
    def setUp(self) -> None:
        self.pipe = Pipe()

    def test_retired_ocr_pdf_valve_is_ignored_and_not_exposed(self) -> None:
        values = {'create_searchable_ocr_pdf': True, 'max_pdf_size_mb': 40}
        valves = self.pipe.Valves(**values)
        self.assertEqual(valves.max_pdf_size_mb, 40)
        self.assertNotIn('create_searchable_ocr_pdf', valves.model_dump())
        self.assertNotIn('create_searchable_ocr_pdf', valves.model_json_schema()['properties'])
        self.assertTrue(values['create_searchable_ocr_pdf'])

    def test_image_page_order_is_filename_natural_order(self) -> None:
        files = [types.SimpleNamespace(filename=name) for name in ('page-10.jpg', 'page-2.jpg', 'page-1.jpg')]
        ordered = sorted(files, key=self.pipe._natural_image_filename_key)
        self.assertEqual([item.filename for item in ordered], ['page-1.jpg', 'page-2.jpg', 'page-10.jpg'])

    def test_canonical_names_never_add_a_review_prefix(self) -> None:
        document = _document()
        self.assertTrue(self.pipe.pdf_filename(document).startswith('1150813_'))
        self.assertTrue(self.pipe._docx_filename(document).startswith('1150813_'))
        self.assertTrue(self.pipe._csv_filename(document).startswith('1150813_'))
        self.assertNotIn('待覆核', self.pipe.pdf_filename(document))

    def test_long_subject_names_use_a_visible_generic_truncation_boundary(self) -> None:
        long_subject = (
            '本所於115年8月14日辦理「台9線351K+740~353K+250賓朗外環段道路新闢工程」'
            '交通、安衛、環保稽複（核）共計2項缺失，請承商速於115年8月28日前改善完成，'
            '並將成果改善函復本所，請查照。'
        )
        document = OfficialDocument(
            **{**_document().__dict__, 'issued': _date('115', '08', '18'), 'reference_number': '棪谷監字第115081804號', 'subject': long_subject},
        )
        pdf_name = self.pipe.pdf_filename(document)
        docx_name = self.pipe._docx_filename(document)
        for name in (pdf_name, docx_name):
            self.assertLessEqual(len(name.encode('utf-8')), 200)
            self.assertIn('棪谷監字第115081804號', name)
            self.assertIn('…', name)
            self.assertNotIn('請承商速於', name)
        self.assertTrue(pdf_name.endswith('.pdf'))
        self.assertTrue(docx_name.endswith('.docx'))
        self.assertEqual(pdf_name[:-4], docx_name[:-5])

    def test_filename_is_independent_of_model_selected_subject_fragment(self) -> None:
        names = set()
        for fragment in ('代辦國道3號古坑交流道平面側車道工程出流管制計畫書', '施工督導查核紀錄表', ''):
            document = OfficialDocument(**{**_document().__dict__, 'filename_subject': fragment})
            names.add(self.pipe.pdf_filename(document))
            names.add(self.pipe._docx_filename(document)[:-5] + '.pdf')
            self.assertEqual(document.subject, SUBJECT)
        self.assertEqual(len(names), 1)

    def test_attachment_names_reserve_space_for_word_extension(self) -> None:
        document = OfficialDocument(**{**_document().__dict__, 'subject': SUBJECT * 4})
        filename = self.pipe.pdf_filename(document, attachment_label='附件12(施工督導查核紀錄表及照片)')
        self.assertTrue(filename.startswith('1150813_'))
        self.assertIn('_附件12(', filename)
        self.assertNotIn('/', filename)
        self.assertLessEqual(len((filename[:-4] + '.docx').encode('utf-8')), 200)

    def test_formal_attachment_title_is_not_limited_to_a_short_label(self) -> None:
        title = '經濟部水利署第五河川分署「國道3號古坑交流道平面側車道工程」申請開挖河防建造物查核表'
        document = _document()
        filename = self.pipe.pdf_filename(document, attachment_label='附件2', attachment_title=title)
        self.assertIn('_附件2-' + title, filename)
        self.assertNotIn('檢送', filename)
        self.assertLessEqual(len((filename[:-4] + '.docx').encode()), 200)
        self.assertEqual(document.subject, SUBJECT)


if __name__ == '__main__':
    unittest.main()
