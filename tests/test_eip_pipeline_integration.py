"""Isolated workflow tests, not live EIP or model-quality acceptance.

Real workflow, PDF validation/export, DOCX renderer, CSV file ACL/parser, source
deduplication, import/readback and persistent checkpoints. Only model/OCR,
Open WebUI storage and remote EIP responses are replaced at their boundaries.
"""
from __future__ import annotations

import asyncio
import copy
import csv
import hashlib
import io
import tempfile
import types
import unittest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock
from unittest.mock import Mock
from unittest.mock import patch

import pymupdf
from docx import Document

from .test_eip_connector import remote
from .test_eip_group_processing import payload
from .test_official_document_processor_pipe import DEADLINE_EVIDENCE
from .test_official_document_processor_pipe import PIPE_MODULE as M
from .test_official_document_processor_pipe import SOURCE
from official_document_workflow.application import automatic_task
from official_document_workflow.application import automation
from official_document_workflow.application import integration
from official_document_workflow.infrastructure.eip.netask import Netask
from official_document_workflow.infrastructure.storage.automation_store import (
    AutomationStore,
)
from official_document_workflow.infrastructure.storage.state import Journal
from official_document_workflow.infrastructure.storage.state import State
from tests.eip_postgres import EIPPostgresTestCase

PROJECT = '代辦國道3號古坑交流道平面側車道工程'
EVENT_ID = '2026-09-11 10:00:00.001;1001'


def span(source, line):
    return [{'source_id': source, 'start_line': line, 'end_line': line}]


def candidate():
    data = payload()
    data.update(
        calendar_contract_version=2,
        events=[
            {
                'status': 'current',
                'title': '提送評估報告',
                'action': '提送評估報告',
                'project_name': PROJECT,
                'start_date': '2026-09-10',
                'start_time': None,
                'end_date': None,
                'end_time': None,
                'location': None,
                'responsible_party': '晉國工程顧問有限公司',
                'evidence_spans': {
                    'title': span('S1', 8),
                    'action': span('S1', 8),
                    'project_name': span('S1', 6),
                    'start_date': span('S2', 1),
                    'responsible_party': span('S1', 8),
                    'relevance': span('S1', 8),
                },
                'description_spans': [*span('S1', 8), *span('S2', 1)],
            },
        ],
    )
    return data


class OwnedFiles:
    def __init__(self, root):
        self.root, self.items = root, {}

    async def store(
        self,
        *,
        user_id,
        payload,
        display_name,
        content_type,
        source_file_id,
        kind,
        extra_metadata=None,
    ):
        if hasattr(payload, 'read'):
            payload = payload.read()
        file_id = str(uuid.uuid4())
        path = self.root / file_id
        path.write_bytes(payload)
        item = types.SimpleNamespace(
            id=file_id,
            user_id=user_id,
            filename=display_name,
            path=str(path),
            meta={
                'content_type': content_type,
                'kind': kind,
                'file_hash': hashlib.sha256(payload).hexdigest(),
                **(extra_metadata or {}),
            },
        )
        self.items[file_id] = item
        return item

    async def get_file_by_id_and_user_id(self, file_id, owner):
        item = self.items.get(file_id)
        return item if item and item.user_id == owner else None

    async def get_files_by_user_id(self, owner):
        return [item for item in self.items.values() if item.user_id == owner]


class CalendarRemote:
    """In-memory remote state behind the real Netask commit/readback methods."""

    def __init__(self):
        self.fields = None
        self.writes = 0
        self.bad_detail = False
        self.bad_preview = False
        self.project_id = ''
        self.sharing = 'Ru17'

    async def rpc(self, method, **kwargs):
        if method == 'Update':
            if 'import_data' in kwargs:
                self.writes += 1
                self.fields = kwargs['import_data'].split(',')
            else:
                self.project_id = kwargs['proj_id']
                self.sharing = kwargs['pub']
            return [[0, '']]
        fields = self.fields
        if method == 'List':
            row = [''] * 21
            for index, value in {
                0: '17',
                2: EVENT_ID,
                6: fields[0],
                8: fields[5],
                9: fields[2],
                10: fields[4],
            }.items():
                row[index] = value
            return [[0, ''], row]
        if method == 'Detail':
            row = [''] * 43
            for index, value in {
                0: EVENT_ID,
                2: '17',
                4: self.project_id,
                8: fields[0],
                9: 'new Date(2026, 8, 10, 0, 0, 0)',
                10: fields[5],
                11: fields[2],
                12: fields[4],
                13: fields[20],
                14: fields[19],
                15: 'Ru999' if self.bad_detail else self.sharing,
                17: '0',
                22: '0',
                23: '0',
            }.items():
                row[index] = value
            return [[0, ''], row, [0]]
        raise AssertionError('Unexpected remote method: ' + method)

    async def stage(self, transport):
        rows = list(csv.DictReader(io.StringIO(transport.decode('utf-8-sig'))))
        result = [remote(row) for row in rows]
        if self.bad_preview:
            result[0][1] = '2026/09/11'
        return result


class PipelineIntegrationTests(EIPPostgresTestCase, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.files = OwnedFiles(root)
        self.db = AutomationStore(root / 'key')
        self.journal = Journal()
        self.db.bind('alice', '17', 'fake-user', 'fake-password')
        self.generation = self.db.account('alice')['generation']
        self.pipe = M.Pipe()
        self.pipe._store_derived_bytes = AsyncMock(side_effect=self.files.store)
        self.pipe._canonical_ocr_context = AsyncMock(return_value=((), None))
        self.pipe._cloud_semantic_source_text = Mock(
            side_effect=[
                M.CloudSemanticSource(SOURCE, 'native_pdf_text'),
                M.CloudSemanticSource(DEADLINE_EVIDENCE, 'native_pdf_text'),
            ],
        )
        self.analysis = candidate()
        self.review = {
            'checks': [
                {
                    'index': 0,
                    'decision': 'keep',
                    'target': None,
                    'reason': '原文指定執行單位、成果與提送期限。',
                    'evidence_spans': [*span('S1', 8), *span('S2', 1)],
                },
            ],
            'issues': [],
        }
        self.pipe._cloud_ocr_text_request = AsyncMock(side_effect=self.model)
        self.pipe._paddleocr_vl_spotting_response = Mock(
            return_value={
                'result': {
                    'layoutParsingResults': [
                        {
                            'prunedResult': {
                                'width': 595,
                                'height': 842,
                                'spotting_res': {
                                    'rec_texts': ['可編輯文件測試'],
                                    'rec_polys': [
                                        [[50, 50], [300, 50], [300, 80], [50, 80]],
                                    ],
                                },
                            },
                        },
                    ],
                },
            },
        )
        pdf = pymupdf.open()
        pdf.new_page().insert_text((50, 65), 'Fixture document')
        self.original = pdf.tobytes()
        pdf.close()
        self.remote = CalendarRemote()
        self.adapter = Netask()
        self.adapter.user_id = '17'
        self.addAsyncCleanup(self.adapter.close)
        self.adapter.login = AsyncMock()
        self.adapter.automatic_documents = AsyncMock(
            return_value=[
                {
                    'id': 'fixture-10',
                    'folder': 'inbox',
                    'subject': '隔離驗收文件',
                    'date': '2026-01-01T10:00:00+08:00',
                    'unread': True,
                },
            ],
        )
        self.adapter.attachments = AsyncMock(
            return_value=[
                {'name': '1-main.pdf', 'url': '/fo/main'},
                {'name': '2-attachment.pdf', 'url': '/fo/attachment'},
            ],
        )
        self.adapter.download = AsyncMock(side_effect=lambda *_: io.BytesIO(self.original))
        self.adapter.stage = AsyncMock(side_effect=self.remote.stage)
        self.adapter.rpc = AsyncMock(side_effect=self.remote.rpc)
        self.adapter.calendar_projects = AsyncMock(
            return_value=[
                {'id': '77', 'name': '01-' + PROJECT, 'members': ['Ru17', 'Ru18']},
            ],
        )
        storage = types.SimpleNamespace(get_file=lambda path: path)
        patches = [
            patch.object(automatic_task, 'STATE', State()),
            patch.object(automation, 'store', return_value=self.db),
            patch.object(integration, 'store', return_value=self.db),
            patch.object(integration, 'prior_csv_ids', AsyncMock(return_value=[])),
            patch.object(
                integration, 'configured_pipe', AsyncMock(return_value=self.pipe),
            ),
            patch.object(integration, 'pipe_module', return_value=M),
            patch.object(M, 'Files', self.files),
            patch.object(M, 'Storage', storage),
            patch.object(integration, 'Files', self.files),
            patch.object(integration, 'Storage', storage),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    async def model(self, source, *, prompt, schema):
        from official_document_workflow.application.prompts import (
            REVIEW_PROMPT,
        )
        from official_document_workflow.domain.calendar.contracts import (
            CALENDAR_REVIEW_SCHEMA,
            GROUP_ANALYSIS_SCHEMA,
        )

        self.assertEqual(
            schema,
            CALENDAR_REVIEW_SCHEMA if prompt == REVIEW_PROMPT else GROUP_ANALYSIS_SCHEMA,
        )
        return copy.deepcopy(self.review if prompt == REVIEW_PROMPT else self.analysis)

    async def run_once(self, verify=None, factory=None):
        # Run a complete scheduler iteration, including its account boundary.
        worker = automation.run_account

        async def run(*args):
            return await worker(
                *args,
                factory=factory or (lambda: self.adapter),
                verify=verify or AsyncMock(return_value=True),
            )

        with (
            patch.object(automation, 'store', return_value=self.db),
            patch.object(
                self.db,
                'claim_due',
                return_value=[{'owner': 'alice', 'generation': self.generation}],
            ),
            patch.object(automation, 'run_account', side_effect=run),
            patch.object(
                automation.asyncio, 'sleep', side_effect=asyncio.CancelledError,
            ),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await automation.scheduler(self.journal)
        return self.db.tasks('alice')[0]

    async def test_real_workflow_exports_csv_import_readback_and_restart_dedup(self):
        task = await self.run_once()
        self.assertEqual(task['phase'], 'done', task['payload'])
        self.assertEqual(self.remote.writes, 1)
        self.assertEqual(self.pipe._cloud_ocr_text_request.await_count, 2)
        output = task['payload']['output']
        self.assertEqual(len(output['files']), 5)
        for entry in output['files']:
            content = Path(self.files.items[entry['id']].path).read_bytes()
            if entry['kind'] == 'document_pdf':
                self.assertEqual(content, self.original)
            elif entry['kind'] == 'document_docx':
                self.assertIn(
                    '可編輯文件測試', Document(io.BytesIO(content)).element.xml,
                )
        rows, _ = await integration.read_csv('alice', output['csv_ids'][0])
        self.assertEqual(
            rows[0]['Subject'],
            '提送評估報告[AI產生](代辦國道3號古坑交流道平面側車道工程)',
        )
        self.assertEqual(rows[0]['Start Date'], '2026/09/10')
        self.assertEqual(rows[0]['Private'], '真')
        self.assertIn('晉國工程顧問有限公司', rows[0]['Description'])
        self.assertNotIn('【來源 S', rows[0]['Description'])
        with self.assertRaises(ValueError):
            await integration.read_csv('bob', output['csv_ids'][0])
        self.db = AutomationStore(Path(self.tmp.name) / 'key')
        self.journal = Journal()
        self.db.recover()
        await self.run_once()
        self.assertEqual(self.remote.writes, 1)
        self.assertEqual(self.pipe._cloud_ocr_text_request.await_count, 2)
        self.assertEqual(self.adapter.download.await_count, 2)

    async def test_project_and_real_file_preflight_then_upload_boundary(self):
        self.db.set_calendar_attach_renamed_pdf('alice', True)
        with (
            patch(
                'official_document_workflow.infrastructure.eip.calendar_projects.assign_project',
                AsyncMock(
                    return_value={
                        'event_id': EVENT_ID,
                        'project_id': '77',
                        'sharing': ['Ru17', 'Ru18'],
                        'detail_verified': True,
                    },
                ),
            ) as assign,
            patch(
                'official_document_workflow.infrastructure.eip.calendar_attachments.attach_event',
                AsyncMock(return_value={'attachment_verified': True}),
            ) as attach,
        ):
            task = await self.run_once()
            self.assertEqual(task['phase'], 'done', task['payload'])
            self.assertEqual(assign.call_args.args[3], '77')
            self.assertEqual(attach.call_args.args[3][0][1], self.original)
            self.assertEqual(len(attach.call_args.args[3]), 2)
            self.assertEqual(
                attach.call_args.kwargs,
                {'project_id': '77', 'sharing': ['Ru17', 'Ru18']},
            )
            await self.run_once()
            assign.assert_awaited_once()
            attach.assert_awaited_once()

    async def test_preview_date_corruption_keeps_exports_without_write(self):
        self.remote.bad_preview = True
        task = await self.run_once()
        self.assertEqual(task['phase'], 'needs_review')
        self.assertEqual(len(task['payload']['output']['files']), 5)
        self.assertEqual(self.remote.writes, 0)

    async def test_success_response_wrong_detail_never_replays_after_restart(self):
        self.remote.bad_detail = True
        task = await self.run_once()
        self.assertEqual(task['phase'], 'needs_review')
        self.assertEqual(self.remote.writes, 1)
        self.db.recover()
        await self.run_once()
        self.assertEqual(self.remote.writes, 1)

    async def test_review_rejection_preserves_files_but_no_csv_or_write(self):
        self.review['checks'][0].update(decision='reject', reason='排程責任需要確認')
        task = await self.run_once()
        self.assertEqual(task['phase'], 'needs_review')
        self.assertEqual(len(task['payload']['output']['files']), 4)
        self.assertEqual(task['payload']['output']['csv_ids'], [])
        self.assertEqual(self.remote.writes, 0)
        self.assertEqual(self.pipe._cloud_ocr_text_request.await_count, 2)

    async def test_no_event_exports_documents_without_calendar(self):
        self.analysis['events'] = []
        task = await self.run_once()
        self.assertEqual(task['phase'], 'done', task['payload'])
        self.assertEqual(len(task['payload']['output']['files']), 4)
        self.assertEqual(self.remote.writes, 0)
        # Empty extraction receives one bounded completeness audit before the
        # workflow concludes that no calendar event exists.
        self.assertEqual(self.pipe._cloud_ocr_text_request.await_count, 2)

    async def test_attachment_filename_contract_checked_before_remote_upload(self):
        for name in ('字' * 100 + '.pdf', 'folder/file.pdf', 'bad\\name.pdf'):
            with self.subTest(name=name):
                source = await self.files.store(
                    user_id='alice',
                    payload=self.original,
                    display_name=name,
                    content_type='application/pdf',
                    source_file_id='',
                    kind='document_pdf',
                )
                with self.assertRaisesRegex(ValueError, '檔名不符合'):
                    await integration.read_calendar_attachments(
                        'alice', [{'id': source.id, 'name': name}],
                    )
        self.assertEqual(self.remote.writes, 0)
