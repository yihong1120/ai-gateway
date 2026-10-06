from __future__ import annotations

import hashlib
import io
import json
import os
import unittest
from contextlib import asynccontextmanager
from subprocess import CalledProcessError
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import patch

from openpyxl import load_workbook

from company_agent.runtime.context import Context
from company_agent.runtime.execution import DocumentAgent
from company_agent.services.eip.client import query
from company_agent.services.eip.schemas import EIPAttachments
from company_agent.services.eip.schemas import EIPQuery
from company_agent.services.files.operations import ToolBox
from company_agent.services.files.operations import worker
from company_agent.services.files.schemas import ReadOCR
from company_agent.services.office.sandbox import office_worker
from company_agent.services.office.schemas import SpreadsheetReport
from company_agent.tests.test_agent import call
from company_agent.tests.test_agent import Fixture
from company_agent.tests.test_enterprise_completion import finish
from company_agent.tests.test_multi_agent import RawRoleModel


def report_spec():
    return {
        'name': '測試.xlsx', 'sheets': [{'name': '工程', 'rows': [['項目', '數量'], ['A', 10], ['B', 20], ['總計', None]]}],
        'formulas': [{'sheet': '工程', 'cell': 'B4', 'formula': '=SUM(B2:B3)'}],
        'charts': [{'sheet': '工程', 'kind': 'bar', 'title': '數量', 'start_row': 2, 'end_row': 3, 'category_column': 1, 'value_column': 2}],
    }


class ExpansionContracts(unittest.TestCase):
    def test_arithmetic_contract(self):
        for f in ['=B2+B3', '=B2*2', '=B3/2', '=B3-B2', '=ROUND(B2/3,2)', '=10%']:
            spec = report_spec()
            spec['formulas'][0]['formula'] = f
            SpreadsheetReport.model_validate(spec)

    def test_owned_message_inline_image_and_external_refusal(self):
        from company_agent.api.pipe import current_turn
        message = {
            'id': 'u', 'role': 'user', 'content': [
                {'type': 'text', 'text': '讀圖中文字'}, {
                    'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,aGVsbG8='},
                },
            ],
        }
        chat = {'history': {'messages': {'u': message, 'a': {'parentId': 'u'}}}}
        self.assertEqual(current_turn(chat, 'u', 'a')[1], [{'inline_image': 'data:image/png;base64,aGVsbG8='}])
        message['content'][1]['image_url']['url'] = 'http://127.0.0.1/private'
        with self.assertRaises(ValueError):
            current_turn(chat, 'u', 'a')

    def test_formula_external_code_and_overwrite_rejected(self):
        for f in ['=WEBSERVICE(A1)', '=HYPERLINK(A1)', '=SUM(Sheet1!A1)', '=SUM([1]A1)', '=exec(A1)', '=A9999', '=SUM(A:A)', '=A1^9999999', '=INDIRECT(A1)']:
            spec = report_spec()
            spec['formulas'][0]['formula'] = f
            with self.subTest(formula=f), self.assertRaises(ValueError):
                SpreadsheetReport.model_validate(spec)
        spec = report_spec()
        spec['formulas'][0]['cell'] = 'B2'
        with self.assertRaises(ValueError):
            SpreadsheetReport.model_validate(spec)

    def test_report_ranges_duplicate_and_bad_query_dates(self):
        spec = report_spec()
        spec['formulas'] *= 2
        with self.assertRaises(ValueError):
            SpreadsheetReport.model_validate(spec)
        spec = report_spec()
        spec['charts'][0]['end_row'] = 200
        with self.assertRaises(ValueError):
            SpreadsheetReport.model_validate(spec)
        for args in [
            {'folder': 'all', 'start': '2026-09-01', 'end': '2026-09-06'},
            {'folder': 'inbox', 'start': '2026-09-06', 'end': '2026-09-01'},
            {'folder': 'inbox', 'start': '2020-01-01', 'end': '2026-09-01'},
        ]:
            with self.assertRaises(ValueError):
                EIPQuery(**args)

    def test_ocr_endpoint_rejects_remote_or_credentials(self):
        from company_agent.services.office.ocr import endpoint
        for url in ['https://example.com', 'http://127.0.0.1:8113/path', 'http://user:pw@127.0.0.1:8113', 'http://localhost:8113']:
            with patch.dict(os.environ, {'COMPANY_AGENT_OCR_URL': url}), self.assertRaises(ValueError):
                endpoint()


@unittest.skipUnless(os.getenv('COMPANY_AGENT_OFFICE_TEST') == '1', 'Needs offline Office image')
class OfficeContainerTests(unittest.TestCase):
    def test_real_calc_formulas_charts_and_literal_formula_text(self):
        spec = report_spec()
        spec['sheets'][0]['rows'][1][0] = '=WEBSERVICE("https://example.invalid")'
        for kind in ['bar', 'line']:
            spec['charts'][0]['kind'] = kind
            arg = SpreadsheetReport.model_validate(spec)
            result, data = office_worker('report', **arg.model_dump(exclude={'name'}))
            self.assertEqual(result['formula_results'][0]['value'], 30)
            book = load_workbook(io.BytesIO(data))
            self.assertEqual(book['工程']['A2'].data_type, 's')
            self.assertEqual(len(book['工程']._charts), 1)
            # Existing reviewer/parser must accept the real Calc output.
            page = worker('read_spreadsheet', data=data, sheet='工程')
            self.assertEqual(page['rows'][3]['cells'][1]['cached_value'], 30)

    def test_divide_zero_and_circular_reference_rejected(self):
        for formula in ['=1/0', '=B4+1']:
            spec = report_spec()
            spec['formulas'][0]['formula'] = formula
            with self.assertRaises((ValueError, CalledProcessError)):
                office_worker('report', **SpreadsheetReport.model_validate(spec).model_dump(exclude={'name'}))

    def test_pdf_page_bounds_and_image_preparation(self):
        import pymupdf
        doc = pymupdf.open()
        p = doc.new_page(width=300, height=100)
        p.insert_text((20, 50), 'Page one')
        doc.new_page(width=300, height=100)
        raw = doc.tobytes()
        sha = hashlib.sha256(raw).hexdigest()
        meta, png = office_worker('ocr_page', data=raw, page=1)
        self.assertEqual(meta['pages'], 2)
        self.assertTrue(png.startswith(b'\x89PNG'))
        meta2, _ = office_worker('ocr_page', data=png, suffix='.png', page=1)
        self.assertEqual(meta2['pages'], 1)
        self.assertEqual(hashlib.sha256(raw).hexdigest(), sha)
        with self.assertRaises((ValueError, CalledProcessError)):
            office_worker('ocr_page', data=raw, page=3)

    @unittest.skipUnless(os.getenv('COMPANY_AGENT_OCR_TEST') == '1', 'Needs live local PaddleOCR')
    def test_local_ocr_real_scanned_page(self):
        import pymupdf
        from company_agent.services.office.ocr import ocr_page
        doc = pymupdf.open()
        p = doc.new_page(width=400, height=150)
        p.insert_text((20, 60), 'Company OCR test 12345', fontsize=20)
        result = ocr_page(p.get_pixmap().tobytes('png'), '.png', 1)
        self.assertIn('12345', ''.join(b['text'] for b in result['blocks']))


class OCRToolTests(Fixture):
    def test_permission_disabled_cache_and_pagination(self):
        self.file('scan.pdf', b'%PDF-test')
        args = ReadOCR(root='project', path='scan.pdf', limit=1)
        box = ToolBox(self.config, self.store, 'alice', self.job)
        with self.assertRaises(ValueError):
            box.read_ocr(args)
        self.config.enable_ocr = True
        result = {'pages': 1, 'page': 1, 'blocks': [{'text': 'one'}, {'text': 'two'}]}
        with patch('company_agent.services.office.ocr.ocr_page', return_value=result) as parse:
            first = box.read_ocr(args)
            self.assertEqual(first['next_start'], 1)
            second = box.read_ocr(ReadOCR(root='project', path='scan.pdf', start=1))
            self.assertEqual(second['blocks'][0]['text'], 'two')
            self.assertEqual(parse.call_count, 1)
            with self.assertRaises(ValueError):
                ToolBox(self.config, self.store, 'bob', self.job).read_ocr(args)
        self.assertTrue(box.sources)

    def test_failed_ocr_is_not_automatically_replayed(self):
        self.config.enable_ocr = True
        self.file('scan.pdf', b'%PDF-test')
        box = ToolBox(self.config, self.store, 'alice', self.job)
        args = ReadOCR(root='project', path='scan.pdf')
        with patch('company_agent.services.office.ocr.ocr_page', side_effect=ValueError('timeout')) as parse:
            for _ in range(2):
                with self.assertRaises(ValueError):
                    box.read_ocr(args)
            self.assertEqual(parse.call_count, 1)


class EIPToolTests(unittest.IsolatedAsyncioTestCase, Fixture):
    def setUp(self):
        super().setUp()
        self.config.enable_eip_read = True
        self.config.eip_binding_revision = 'test-revision'

    async def test_inline_image_signature_dedup_and_remote_refusal(self):
        import base64
        from company_agent.api.service import upload_bytes
        raw = b'\x89PNG\r\n\x1a\nexample'
        item = {'inline_image': 'data:image/png;base64,'+base64.b64encode(raw).decode()}
        self.assertEqual(await upload_bytes('alice', [item, item]), [('image_1.png', raw)])
        for item in [{'inline_image': 'http://127.0.0.1/private'}, {'inline_image': 'data:image/png;base64,aGVsbG8='}, {'inline_image': 'data:image/png;base64,!'}]:
            with self.assertRaises(ValueError):
                await upload_bytes('alice', [item])

    async def test_followup_cannot_carry_rebound_eip_context(self):
        from company_agent.api.service import WorkQueue
        revision = ['original']
        queue = WorkQueue(self.private, self.base/'image-inputs', lambda _: revision[0])
        first = queue.submit('alice', 'subject', 'chat', 'first', '查 EIP', [])
        queue.update(first, status='done')
        revision[0] = 'changed'
        with self.assertRaisesRegex(ValueError, 'EIP'):
            queue.submit('alice', 'subject', 'chat', 'next', '繼續', [], [first])

    async def test_query_owner_scoped_handles_and_binding_recheck(self):
        ctx = Context('alice', self.job, self.config, self.store)
        read = AsyncMock(
            side_effect=[
                [{
                    'id': '123', 'subject': '測試', 'date': '2026-09-06',
                    'folder': 'sent',
                }], [{'name': 'main.docx', 'url': 'private-url'}],
            ],
        )

        @asynccontextmanager
        async def session(owner, *args):
            self.assertEqual(owner, 'alice')
            yield SimpleNamespace(read=read)
        with patch('company_agent.services.eip.client.binding', new=AsyncMock(return_value=object())), patch('official_document_workflow.infrastructure.eip.sessions.manual_session', session):
            found = await query(ctx, 'eip_query', EIPQuery(folder='sent', start='2026-09-01', end='2026-09-06'), 'q')
            document = found['documents'][0]
            handle = document['handle']
            # An opaque UUID can legitimately contain the digits "123".
            # Assert the response contract instead of a random substring ban.
            self.assertRegex(handle, r'^[0-9a-f]{32}$')
            self.assertEqual(
                document, {
                    'handle': handle, 'subject': '測試', 'date': '2026-09-06', 'folder': 'sent',
                    'view_url': f'/agents/eip/{self.job}/{handle}',
                    'view_link': f'[測試](/agents/eip/{self.job}/{handle})',
                },
            )
            files = await query(ctx, 'eip_attachments', EIPAttachments(handle=handle), 'a')
            self.assertEqual(files['attachments'][0]['name'], 'main.docx')
            self.assertEqual(len(files['attachments'][0]['handle']), 32)
            self.assertNotIn('private-url', json.dumps(files))
            self.assertEqual((await query(ctx, 'eip_query', EIPQuery(folder='sent', start='2026-09-01', end='2026-09-06'), 'q')), found)
            other = Context('alice', self.store.job('alice'), self.config, self.store)
            with self.assertRaises(ValueError):
                await query(other, 'eip_attachments', EIPAttachments(handle=handle), 'wrong')
        with patch('company_agent.services.eip.client.binding', new=AsyncMock(side_effect=ValueError('binding changed'))), self.assertRaises(ValueError):
            await query(ctx, 'eip_query', EIPQuery(folder='sent', start='2026-09-01', end='2026-09-06'), 'q')
        self.assertEqual(read.call_count, 2)
        read.assert_any_await('attachments', '123', 'sent')

    async def test_eip_role_typed_graph(self):
        self.config.structured_completion = True
        model = RawRoleModel(
            coordinator=[call('delegate_eip', {'task': '查本人送出文件'}, 'd'), finish()],
            eip=[
                call(
                    'eip_query', {
                        'folder': 'sent', 'start': '2026-09-01',
                        'end': '2026-09-06',
                    }, 'q',
                ), finish('complete_assignment'),
            ],
        )
        with patch('company_agent.services.eip.client.query', new=AsyncMock(return_value={'documents': []})), patch('company_agent.services.eip.client.binding', new=AsyncMock()):
            result = await DocumentAgent(self.config, model).run('alice', '查我九月一日至六日送出的公文')
        self.assertEqual(result['status'], 'done', result)
        self.assertIn('eip', result['agent_visits'])
        self.assertTrue(result['notices'])

    async def test_binding_checks_real_owner_and_revision(self):
        from company_agent.services.eip.client import binding
        db = SimpleNamespace(account=lambda owner: {'generation': 'different'})
        with patch('company_agent.runtime.context.verified_user', new=AsyncMock()) as verified, patch('official_document_workflow.application.automation.store', return_value=db):
            with self.assertRaises(ValueError):
                await binding(Context('alice', self.job, self.config, self.store))
            verified.assert_awaited_once_with('alice')

    @unittest.skipUnless(os.getenv('COMPANY_AGENT_OFFICE_TEST') == '1', 'Needs Office image')
    async def test_real_report_graph_review_and_typed_completion(self):
        self.config.enable_spreadsheet_reports = True
        self.config.structured_completion = True
        self.config.require_artifact_review = True
        ids = []

        def created(messages):
            result = json.loads(messages[-1].content)
            ids.append(result['id'])
            self.assertEqual(result['calculation']['formula_results'][0]['value'], 30)
            return finish('complete_assignment', ids=ids)
        model = RawRoleModel(
            coordinator=[
                call('delegate_data', {'task': '新建數量報表與圖表'}, 'd'),
                call('delegate_reviewer', {'task': '讀回核對'}, 'r'), lambda _:finish(ids=ids),
            ],
            data=[call('create_spreadsheet_report', report_spec(), 'make'), created],
            reviewer=[
                lambda _:call(
                    'inspect_artifact', {'artifact_id': ids[0]},
                    'read',
                ), lambda _:finish('complete_assignment', ids=ids),
            ],
        )
        result = await DocumentAgent(self.config, model).run('alice', '請依明確數量製作 Excel 報表和圖表')
        self.assertEqual(result['status'], 'done', result)
        self.assertFalse(result['artifact_review']['unreviewed_ids'])
