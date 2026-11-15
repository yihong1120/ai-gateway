"""Generated upload CSVs continue directly into EIP without reprocessing PDFs."""
from __future__ import annotations

import importlib
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import Mock
from unittest.mock import patch

import httpx
from fastapi import FastAPI

from examples.official_document_workflow.application import integration
from examples.official_document_workflow.domain.calendar.csv_schema import CALENDAR_CSV_FORMAT
from examples.official_document_workflow.domain.calendar.csv_schema import CALENDAR_CSV_FORMAT_KEY
from examples.official_document_workflow.infrastructure.eip.calendar_csv import export_eip_csv
from examples.official_document_workflow.infrastructure.eip.netask import Netask
from examples.official_document_workflow.infrastructure.storage.state import Journal
from examples.official_document_workflow.infrastructure.storage.state import Link
from examples.official_document_workflow.services.exports import DocumentExports
from examples.official_document_workflow.services.ocr import DocumentOCR
from examples.official_document_workflow.services.transport import ModelTransport
from tests.eip_postgres import EIPPostgresTestCase
from tests.test_eip_connector import row
from tests.test_eip_connector import verified
from tests.test_eip_pipeline_integration import CalendarRemote
from tests.test_eip_pipeline_integration import OwnedFiles


class UploadCalendarReuseTests(EIPPostgresTestCase, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.files = OwnedFiles(Path(temporary.name))
        self.remote = CalendarRemote()
        self.adapter = Netask()
        self.adapter.user_id = '17'
        self.adapter.stage = AsyncMock(side_effect=self.remote.stage)
        self.adapter.rpc = AsyncMock(side_effect=self.remote.rpc)
        self.addAsyncCleanup(self.adapter.close)
        self._workflow.state.links['alice'] = Link(self.adapter, 'synthetic-account')

        auth = ModuleType('open_webui.utils.auth')
        auth.get_verified_user = auth.get_current_user = verified
        with patch.dict(sys.modules, {'open_webui.utils.auth': auth}):
            routes = importlib.import_module('examples.official_document_workflow.api.routes')
        self.app = FastAPI()
        self.app.include_router(routes.router)
        self.app.dependency_overrides[routes.get_verified_user] = verified
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url='https://webui.test',
            headers={'test-owner': 'alice', 'origin': 'https://webui.test', 'x-eip-action': '1'},
        )
        self.addAsyncCleanup(self.client.aclose)
        self.enterContext(patch.dict('os.environ', {'EIP_WEBUI_ORIGIN': 'https://webui.test'}))
        self.enterContext(patch.object(integration, 'Files', self.files))
        self.enterContext(patch.object(integration, 'Storage', SimpleNamespace(get_file=lambda path: path)))
        self.enterContext(patch.object(integration, 'prior_csv_ids', AsyncMock(return_value=[])))

        # Each public expensive boundary is guarded, in addition to blocking a
        # second document pipeline. Calendar readback and the journal stay real.
        self.unused = [
            self.enterContext(
                patch.object(
                    integration, 'process', AsyncMock(
                        side_effect=AssertionError('CSV must not run document processing.'),
                    ),
                ),
            ),
            self.enterContext(
                patch.object(
                    integration, 'configured_pipe', AsyncMock(
                        side_effect=AssertionError('CSV must not load a document processor.'),
                    ),
                ),
            ),
            self.enterContext(
                patch.object(
                    ModelTransport, 'analyze_text', AsyncMock(
                        side_effect=AssertionError('CSV must not analyze the original PDF.'),
                    ),
                ),
            ),
            self.enterContext(
                patch.object(
                    DocumentOCR, '_paddleocr_vl_single_request',
                    Mock(side_effect=AssertionError('CSV must not run OCR.')),
                ),
            ),
            self.enterContext(
                patch.object(
                    DocumentExports, 'render_word', Mock(
                        side_effect=AssertionError('CSV must not render Word.'),
                    ),
                ),
            ),
        ]
        self.source = await self.files.store(
            user_id='alice', payload=b'%PDF-1.7\nsynthetic original upload', display_name='公文.pdf',
            content_type='application/pdf', source_file_id='', kind='upload',
        )
        self.csv = await self.generated_csv(self.source.id)

    async def generated_csv(self, source_id, *, subject='提送評估報告[AI產生]'):
        rows = [{**row(), 'Subject': subject, 'Custom Field1': '1153022027'}]
        return await self.files.store(
            user_id='alice', payload=export_eip_csv(rows), display_name='公文行事曆.csv',
            content_type='text/csv', source_file_id=source_id, kind='calendar_csv',
            extra_metadata={CALENDAR_CSV_FORMAT_KEY: CALENDAR_CSV_FORMAT, 'eip_group_source_ids': [source_id]},
        )

    async def preview(self, item=None):
        response = await self.client.post('/api/v1/eip/preview', json={'file_id': (item or self.csv).id})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    async def confirm(self, preview):
        response = await self.client.post('/api/v1/eip/confirm', json={'token': preview['token'], 'confirm': True})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def assert_no_document_processing(self):
        for boundary in self.unused:
            boundary.assert_not_called()

    async def test_generated_upload_csv_preview_is_read_only_then_confirm_imports_without_reprocessing(self):
        preview = await self.preview()
        self.assertEqual([event['status'] for event in preview['events']], ['new'])
        self.assertEqual(self.remote.writes, 0)
        with self._workflow.journal.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM writes').fetchone()[0], 0)
        self.assertEqual(await self.confirm(preview), {'created': 1, 'skipped': 0})
        self.assertEqual(self.remote.writes, 1)
        with self._workflow.journal.connect() as db:
            self.assertEqual(db.execute('SELECT count(DISTINCT event_id) FROM event_receipts').fetchone()[0], 1)
        self.assert_no_document_processing()

    async def test_restarted_csv_preview_and_repeat_confirmation_reuse_durable_journal(self):
        preview = await self.preview()
        self.assertEqual(await self.confirm(preview), {'created': 1, 'skipped': 0})
        self._workflow._journal = Journal()
        self.assertEqual(await self.confirm(preview), {'created': 0, 'skipped': 1})
        restarted = await self.preview()
        self.assertEqual([event['status'] for event in restarted['events']], ['done'])
        self.assertEqual(await self.confirm(restarted), {'created': 0, 'skipped': 1})
        self.assertEqual(self.remote.writes, 1)
        self.assert_no_document_processing()

    async def test_same_source_bytes_with_new_upload_id_and_csv_title_do_not_create_another_event(self):
        self.assertEqual(await self.confirm(await self.preview()), {'created': 1, 'skipped': 0})
        duplicate = await self.files.store(
            user_id='alice', payload=Path(self.source.path).read_bytes(), display_name='再次上傳.pdf',
            content_type='application/pdf', source_file_id='', kind='upload',
        )
        self.assertNotEqual(duplicate.id, self.source.id)
        regenerated = await self.generated_csv(duplicate.id, subject='重新整理的顯示名稱[AI產生]')
        self._workflow._journal = Journal()
        preview = await self.preview(regenerated)
        self.assertEqual([event['status'] for event in preview['events']], ['done'])
        self.assertEqual(await self.confirm(preview), {'created': 0, 'skipped': 1})
        self.assertEqual(self.remote.writes, 1)
        self.assert_no_document_processing()

    async def test_ambiguous_eip_write_stays_blocked_after_restart_and_never_reprocesses_source(self):
        async def lose_update_reply(method, **kwargs):
            response = await self.remote.rpc(method, **kwargs)
            if method == 'Update' and 'import_data' in kwargs:
                raise TimeoutError('synthetic response loss after remote update')
            return response
        self.adapter.rpc.side_effect = lose_update_reply
        preview = await self.preview()
        with self.assertRaisesRegex(TimeoutError, 'synthetic response loss'):
            await self.confirm(preview)
        self.assertEqual(self.remote.writes, 1)
        self._workflow._journal = Journal()
        restarted = await self.preview()
        self.assertEqual([event['status'] for event in restarted['events']], ['unknown'])
        with self.assertRaisesRegex(ValueError, '結果不明'):
            await self.confirm(restarted)
        self.assertEqual(self.remote.writes, 1)
        self.assert_no_document_processing()

    async def test_another_owner_cannot_resume_someone_elses_generated_csv(self):
        with self.assertRaisesRegex(ValueError, '自己擁有'):
            await self.client.post('/api/v1/eip/preview', json={'file_id': self.csv.id}, headers={'test-owner': 'bob'})
        self.adapter.stage.assert_not_awaited()
        self.assertEqual(self.remote.writes, 0)
        self.assert_no_document_processing()
