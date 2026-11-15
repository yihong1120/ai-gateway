from __future__ import annotations

import hashlib
import io
import json
import unittest
import zipfile
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import patch

from langchain_core.messages import AIMessage
from langchain_core.messages import HumanMessage
from openpyxl import load_workbook

import company_agent.evals.smoke_vision as _smoke_vision_imports
import company_agent.services.eip.calendar as _calendar_imports
import company_agent.services.eip.client as _client_imports
import company_agent.services.eip.processing as _processing_imports
import company_agent.services.office.vision as _vision_imports
import examples.official_document_workflow.services.analysis_reuse as _analysis_reuse_imports
from company_agent.evals.fixtures import workbook
from company_agent.runtime.context import Context
from company_agent.runtime.models import billed_analysis
from company_agent.runtime.models import UserBilledModel
from company_agent.services.eip.client import remember
from company_agent.services.eip.schemas import EIPProcess
from company_agent.services.files.operations import ToolBox
from company_agent.services.office.schemas import EditSpreadsheet
from company_agent.services.office.schemas import ReadSpreadsheet
from company_agent.services.office.schemas import UnderstandImage
from company_agent.services.office.spreadsheet_edit import edit_spreadsheet
from company_agent.services.office.vision import understand
from company_agent.tests.test_agent import Fixture
from examples.official_document_workflow.infrastructure.storage.state import Journal
from examples.official_document_workflow.infrastructure.storage.state import Link
from examples.official_document_workflow.infrastructure.storage.state import State
from examples.official_document_workflow.runtime import current_resources
from tests.eip_calendar_fixture import new_calendar_matches
from tests.eip_postgres import EIPPostgresTestCase


def change(**kwargs):
    return {'sheet': '工程', 'cell': 'B2', 'expected': 10, 'value': 12, **kwargs}


class SpreadsheetEdits(unittest.TestCase):
    def test_vision_payload_requires_full_schema_and_accepts_single_fence(self):

        raw = '{"summary":"看見方形","observations":["紅色"],"uncertainties":[]}'
        self.assertEqual(
            _vision_imports.vision_payload(raw), _vision_imports.vision_payload('```json\n' + raw + '\n```'),
        )
        for text in [
            '[1]',
            '{}',
            '文字' + raw,
            raw + '多餘文字',
            '{"summary":"","observations":[],"uncertainties":[]}',
        ]:
            with self.assertRaises(ValueError):
                _vision_imports.vision_payload(text)

        self.assertTrue(_smoke_vision_imports.synthetic_pass('左側紅色方形，右側藍色圓形'))
        self.assertTrue(_smoke_vision_imports.synthetic_pass('左侧红色四边形，右侧蓝色圆形'))
        self.assertFalse(_smoke_vision_imports.synthetic_pass('左側綠色三角形，右側黃色圓形'))

    def test_multi_sheet_styles_formula_and_preservation(self):
        original = workbook()
        result = edit_spreadsheet(
            original,
            [
                change(style_from='B3'),
                change(
                    sheet='第二表', cell='A1', expected='原值', value='=not a formula',
                ),
                change(cell='B4', expected='=SUM(B2:B3)', value='=B2+B3', formula=True),
            ],
        )
        b = load_workbook(io.BytesIO(result))
        self.assertEqual(b['工程']['B2'].value, 12)
        self.assertEqual(b['工程']['B2'].number_format, '0%')
        self.assertEqual(b['工程']['B4'].value, '=B2+B3')
        self.assertEqual(b['第二表']['A1'].data_type, 's')
        self.assertEqual(str(b['工程'].merged_cells), 'D1:E1')
        self.assertEqual(b['工程'].freeze_panes, 'B2')
        self.assertEqual(len(b['工程']._charts), 1)
        self.assertIsNone(
            load_workbook(io.BytesIO(result), data_only=True)['工程']['B4'].value,
        )
        with (
            zipfile.ZipFile(io.BytesIO(original)) as a,
            zipfile.ZipFile(io.BytesIO(result)) as b,
        ):
            for n in a.namelist():
                if n not in {
                    'xl/workbook.xml',
                    'xl/worksheets/sheet1.xml',
                    'xl/worksheets/sheet2.xml',
                }:
                    self.assertEqual(a.read(n), b.read(n), n)

    def test_atomic_reject_wrong_original_duplicate_external_and_merge(self):
        for changes in [
            [change(expected=9)],
            [change(), change()],
            [change(value='=WEBSERVICE(A1)', formula=True)],
            [change(value='=B2+1', formula=True)],
            [change(value='=B4+1', formula=True)],
            [change(cell='E1', expected=None, value='bad')],
            [change(cell='B9999', expected=None, value=1)],
            [change(expected=True)],
        ]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                edit_spreadsheet(workbook(), changes)

    def test_shared_formula_and_protection_rejected(self):
        for fragment in ['<sheetProtection sheet="1"/>', '<extLst/>']:
            out = io.BytesIO()
            with (
                zipfile.ZipFile(io.BytesIO(workbook())) as z,
                zipfile.ZipFile(out, 'w') as target,
            ):
                for n in z.namelist():
                    target.writestr(
                        n,
                        (
                            z.read(n).replace(
                                b'</worksheet>', fragment.encode() + b'</worksheet>',
                            )
                            if n == 'xl/worksheets/sheet1.xml'
                            else z.read(n)
                        ),
                    )
            with self.assertRaises(ValueError):
                edit_spreadsheet(out.getvalue(), [change()])


class SpreadsheetTool(Fixture):
    def test_actual_worker_sha_acl_and_private_output(self):
        raw = workbook()
        path = self.file('source.xlsx', raw)
        self.config.enable_spreadsheet_reports = True
        box = ToolBox(self.config, self.store, 'alice', self.job)
        args = EditSpreadsheet(
            root='project',
            path='source.xlsx',
            sha256=hashlib.sha256(raw).hexdigest(),
            name='changed.xlsx',
            changes=[change()],
        )
        with self.assertRaises(ValueError):
            box.edit_spreadsheet(args)
        box.read_spreadsheet(ReadSpreadsheet(root='project', path='source.xlsx'))
        result = box.edit_spreadsheet(args)
        self.assertEqual(path.read_bytes(), raw)
        _name, _sha, data = self.store.read_artifact('alice', result['id'], self.job)
        self.assertEqual(load_workbook(io.BytesIO(data))['工程']['B2'].value, 12)
        with self.assertRaises(ValueError):
            self.store.read_artifact('bob', result['id'])
        path.write_bytes(raw + b'changed')
        with self.assertRaises(ValueError):
            box.edit_spreadsheet(args)


class SemanticTests(unittest.IsolatedAsyncioTestCase, Fixture):
    async def test_vision_cache_one_billed_request_and_no_url_fetch(self):
        self.file('image.png', b'fake-test-image')
        self.config.enable_vision = True
        ctx = Context('alice', self.job, self.config, self.store)
        args = UnderstandImage(
            root='project', path='image.png', question='描述可見物件',
        )
        response = json.dumps(
            {
                'summary': '紅色方形',
                'observations': ['一個方形'],
                'uncertainties': ['無法量測尺寸'],
            },
        )
        with (
            patch(
                'company_agent.services.office.sandbox.office_worker',
                return_value=({'page': 1, 'pages': 1}, b'png'),
            ),
            patch(
                'company_agent.services.office.vision.billed_analysis',
                new=AsyncMock(return_value=response),
            ) as call,
        ):
            a = await understand(ctx, args)
            b = await understand(ctx, args)
            self.assertEqual(a, b)
            self.assertEqual(call.await_count, 1)
            messages = call.call_args.args[1]
            self.assertTrue(
                messages[1]
                .content[1]['image_url']['url']
                .startswith('data:image/png;base64,'),
            )
        self.config.enable_vision = False
        with self.assertRaises(ValueError):
            await understand(ctx, args)

    async def test_empty_vision_fails_once_and_binding_acl_rechecked(self):
        self.file('image.png', b'test')
        self.config.enable_vision = True
        ctx = Context('alice', self.job, self.config, self.store)
        args = UnderstandImage(root='project', path='image.png', question='看圖')
        with (
            patch(
                'company_agent.services.office.sandbox.office_worker',
                return_value=({}, b'png'),
            ),
            patch(
                'company_agent.services.office.vision.billed_analysis',
                new=AsyncMock(return_value='{}'),
            ) as call,
        ):
            with self.assertRaises(ValueError):
                await understand(ctx, args)
            with self.assertRaises(ValueError):
                await understand(ctx, args)
            self.assertEqual(call.await_count, 1)

    async def test_billed_helper_uses_verified_subject_and_accounts_usage(self):
        call = AsyncMock(
            return_value=AIMessage(
                content='answer',
                usage_metadata={
                    'input_tokens': 10,
                    'output_tokens': 2,
                    'total_tokens': 12,
                },
            ),
        )
        client = SimpleNamespace(
            model_name='test-model',
            bind=lambda **kw: (
                self.assertEqual(
                    kw,
                    {
                        'user': 'oidc-alice', 'response_format': {'type': 'json_object'},
                        'extra_body': {'metadata': {'spend_logs_metadata': {'ai_usage_source': 'company_agent'}}},
                    },
                )
                or SimpleNamespace(ainvoke=call)
            ),
        )
        ctx = Context(
            'alice',
            self.job,
            self.config,
            self.store,
            UserBilledModel('alice', 'oidc-alice', client),
        )
        self.assertEqual(
            await billed_analysis(ctx, [HumanMessage(content='hello')]), 'answer',
        )
        self.assertEqual(self.store.attempts(self.job), 1)
        with self.assertRaises(ValueError):
            await billed_analysis(
                Context('bob', self.job, self.config, self.store, ctx.model), [],
            )

    async def test_truncated_analysis_keeps_billed_usage_and_does_not_retry(self):
        invoke = AsyncMock(
            return_value=AIMessage(
                content='incomplete', response_metadata={'finish_reason': 'length'},
                usage_metadata={'input_tokens': 10, 'output_tokens': 2, 'total_tokens': 12},
            ),
        )
        client = SimpleNamespace(model_name='test-model', bind=lambda **kw: SimpleNamespace(ainvoke=invoke))
        ctx = Context('alice', self.job, self.config, self.store, UserBilledModel('alice', 'oidc-alice', client))
        with self.assertRaisesRegex(ValueError, '截斷'):
            await billed_analysis(ctx, [HumanMessage(content='test')])
        invoke.assert_awaited_once()
        with self.store.db() as db:
            receipt = db.execute('SELECT result FROM tool_receipts WHERE job=%s', (self.job,)).fetchone()[0]
        self.assertEqual(json.loads(receipt)['usage']['output_tokens'], 2)
        self.assertEqual(self.store.attempts(self.job), 1)


class EIPProcessingTests(
    EIPPostgresTestCase, unittest.IsolatedAsyncioTestCase, Fixture,
):
    async def test_calendar_preview_confirmation_dedup_and_unknown_stop(self):

        state = State()
        _ = Journal()
        art = self.store.artifact('alice', self.job, 'calendar.csv', b'csv')
        with self.store.db() as db:
            db.execute(
                'INSERT INTO eip_calendar_sources VALUES(%s,%s,%s,%s)',
                ('alice', self.job, art['id'], 'rev'),
            )
        adapter = SimpleNamespace(
            user_id='EIP-user', commit=AsyncMock(return_value=None),
            calendar_import_matches=AsyncMock(side_effect=new_calendar_matches),
        )
        link = Link(adapter, '本人帳號')
        read = AsyncMock(return_value=[])

        @asynccontextmanager
        async def session(*args):
            yield SimpleNamespace(read=read, link=link)

        db = SimpleNamespace(account=lambda _: {'generation': 'rev'})
        events = [
            {
                'key': 'event1',
                'row': {'Subject': '測試', 'Start Date': '2026/09/08'},
                'line': 'transport',
            },
        ]
        with (
            patch.object(current_resources(), 'state', state),
            patch(
                'company_agent.services.eip.calendar.current_binding',
                new=AsyncMock(return_value=db),
            ),
            patch(
                'examples.official_document_workflow.infrastructure.eip.calendar_csv.parse_csv',
                return_value=([{}], b'transport'),
            ),
            patch(
                'examples.official_document_workflow.infrastructure.eip.calendar_sync.verified_events',
                return_value=events,
            ),
            patch(
                'examples.official_document_workflow.infrastructure.eip.sessions.manual_session',
                session,
            ),
        ):
            p = await _calendar_imports.preview(self.store, 'alice', art['id'])
            adapter.commit.assert_not_called()
            with self.assertRaises(ValueError):
                await _calendar_imports.confirm(self.store, 'bob', p['token'])
            self.assertEqual(
                await _calendar_imports.confirm(self.store, 'alice', p['token']),
                {'created': 1, 'skipped': 0},
            )
            self.assertEqual(
                await _calendar_imports.confirm(self.store, 'alice', p['token']),
                {'created': 0, 'skipped': 1},
            )
            self.assertEqual(adapter.commit.await_count, 1)
            state.previews[p['token']]['generation'] = 'wrong'
            with self.assertRaises(ValueError):
                await _calendar_imports.confirm(self.store, 'alice', p['token'])
            p = await _calendar_imports.preview(self.store, 'alice', art['id'])
            state.previews[p['token']]['events'] = [
                {'key': 'ambiguous', 'row': {}, 'line': 'next'},
            ]
            adapter.commit.side_effect = ValueError('network timeout')
            with self.assertRaises(ValueError):
                await _calendar_imports.confirm(self.store, 'alice', p['token'])
            with self.assertRaises(ValueError):
                await _calendar_imports.confirm(self.store, 'alice', p['token'])
            self.assertEqual(adapter.commit.await_count, 2)
            state.previews[p['token']]['expires'] = 0
            with self.assertRaises(ValueError):
                await _calendar_imports.confirm(self.store, 'alice', p['token'])

    async def test_billed_pipe_replaces_both_semantic_requests_and_blocks_exemption(
        self,
    ):

        self.config.model_name = 'fake'
        module = SimpleNamespace(INSTRUCTION_PLANNER_PROMPT='planner')
        pipe = SimpleNamespace(
            valves=SimpleNamespace(
                cloud_ocr_text_max_input_characters=10000, cloud_ocr_text_model='old',
                document_analysis_cache_enabled=True,
            ),
            _cloud_json_object=json.loads,
            _cloud_ocr_text_prompt=lambda _: 'analysis',
        )
        ctx = Context(
            'alice', self.job, self.config, self.store,
            UserBilledModel(
                'alice', 'sub', SimpleNamespace(
                    model_name='fake', openai_api_base='http://127.0.0.1:4000/v1',
                    reasoning_effort=None, max_tokens=4096, temperature=0, model_kwargs={}, extra_body=None,
                ),
            ),
        )
        with (
            patch(
                'examples.official_document_workflow.application.integration.module',
                return_value=module,
            ),
            patch(
                'examples.official_document_workflow.application.integration.configured_pipe',
                new=AsyncMock(return_value=pipe),
            ),
            patch('company_agent.services.eip.processing.binding', new=AsyncMock()),
            patch(
                'company_agent.services.eip.processing.billed_analysis',
                new=AsyncMock(return_value='{"ok":true}'),
            ) as model,
        ):
            bound = await _processing_imports.billed_pipe(ctx)
            self.assertIs(bound.content_cache, current_resources().shared_content)
            self.assertIsNone(bound.evidence_cache)
            self.assertEqual(bound.valves.cloud_ocr_text_model, 'fake')
            self.assertEqual(bound.valves.instruction_plan_model, 'fake')
            with self.assertRaises(ValueError):
                bound._official_document_litellm_connection()
            self.assertEqual(
                await bound._cloud_instruction_plan_payload('產生文件'), {'ok': True},
            )
            self.assertEqual(
                await bound.analyze_text(
                    SimpleNamespace(text='內容', kind='paddle_layout_ocr'),
                    prompt='analysis',
                ),
                {'ok': True},
            )
            await bound.analyze_text(SimpleNamespace(text='內容', kind='paddle_layout_ocr'), prompt='analysis')
            self.assertEqual(model.await_count, 3)
            schema = {'type': 'object', 'properties': {'ok': {'type': 'boolean'}}, 'required': ['ok']}
            original = SimpleNamespace(text='共同公文事實', kind='native_pdf_text')
            await _analysis_reuse_imports.analyze_content(bound, original, stage='document-group', prompt='analysis', schema=schema)
            self.assertEqual(model.await_count, 4)
            self.assertIs(model.await_args.args[0], ctx)
            bob = Context('bob', self.job, self.config, self.store, UserBilledModel('bob', 'bob-sub', ctx.model.client))
            other = await _processing_imports.billed_pipe(bob)
            self.assertEqual(await _analysis_reuse_imports.analyze_content(other, original, stage='document-group', prompt='analysis', schema=schema), {'ok': True})
            self.assertEqual(model.await_count, 4)  # No second user's model reservation or fake charge.
            self.assertEqual(other._usage_owner, 'bob')
            resources = current_resources()
            with patch.object(resources, 'settings', replace(resources.settings, shared_content_cache_enabled=False)):
                disabled = await _processing_imports.billed_pipe(ctx)
                self.assertIsNone(disabled.content_cache)
                self.assertIsNone(disabled.analysis_results)

    async def test_grouping_private_outputs_receipt_and_no_calendar_write(self):

        self.config.enable_eip_processing = True
        self.config.eip_binding_revision = 'rev'
        self.config.model_name = 'fake'
        ctx = Context(
            'alice',
            self.job,
            self.config,
            self.store,
            UserBilledModel('alice', 'sub', None),
        )
        handles = [
            remember(
                ctx,
                {
                    'kind': 'attachment',
                    'doc': {'id': doc, 'folder': 'sent'},
                    'attachment': {'name': name, 'url': name},
                },
            )
            for doc, name in [
                ('one', 'main.doc'),
                ('one', 'attach.pdf'),
                ('two', 'other.docx'),
            ]
        ]

        async def read(op, *args):
            if op == 'attachments':
                return [
                    {'name': name, 'url': name}
                    for name in (
                        ['main.doc', 'attach.pdf']
                        if args[0] == 'one'
                        else ['other.docx']
                    )
                ]
            return io.BytesIO(b'%PDF-fake')

        @asynccontextmanager
        async def session(*args):
            yield SimpleNamespace(read=read)

        output = {
            'status': 'done',
            'files': [{'id': 'out', 'name': 'test.pdf'}],
            'csv_ids': [],
            'notices': ['沒有期限，不新增行程'],
            'has_deadlines': False,
        }
        with (
            patch(
                'company_agent.services.eip.processing.binding',
                new=AsyncMock(return_value=object()),
            ),
            patch(
                'examples.official_document_workflow.infrastructure.eip.sessions.manual_session',
                session,
            ),
            patch(
                'examples.official_document_workflow.application.integration.source_file',
                new=AsyncMock(
                    side_effect=[SimpleNamespace(id=str(i)) for i in range(3)],
                ),
            ),
            patch(
                'company_agent.services.eip.processing.billed_pipe',
                new=AsyncMock(return_value=(None, None)),
            ),
            patch(
                'examples.official_document_workflow.application.multi_agent.process_with_team',
                new=AsyncMock(return_value=output),
            ) as team,
            patch(
                'company_agent.services.eip.processing.owned_bytes',
                new=AsyncMock(return_value=('test.pdf', b'%PDF-export', 'plain')),
            ),
        ):
            result = await _processing_imports.process(ctx, EIPProcess(handles=handles))
            self.assertEqual(team.call_args.args[3], [['0', '1'], ['2']])
            self.assertFalse(result['has_deadlines'])
            self.assertEqual(await _processing_imports.process(ctx, EIPProcess(handles=handles)), result)
            self.assertEqual(team.await_count, 1)
            self.assertEqual(
                self.store.read_artifact('alice', result['files'][0]['id'])[2],
                b'%PDF-export',
            )
            with self.store.db() as db:
                self.assertEqual(
                    db.execute('SELECT count(*) FROM eip_calendar_sources').fetchone()[
                        0
                    ],
                    0,
                )

    async def test_source_owner_kind_and_generation(self):

        self.config.eip_binding_revision = 'rev'
        ctx = Context('alice', self.job, self.config, self.store)
        handle = remember(ctx, {'kind': 'attachment', 'name': 'x'})
        with self.assertRaises(ValueError):
            _client_imports.source(ctx, handle, 'document')
        with self.assertRaises(ValueError):
            _client_imports.source(
                Context('bob', self.job, self.config, self.store), handle, 'attachment',
            )
        self.config.eip_binding_revision = 'new'
        with self.assertRaises(ValueError):
            _client_imports.source(ctx, handle, 'attachment')

    async def test_calendar_only_pipeline_csv_and_owner(self):

        art = self.store.artifact('alice', self.job, 'calendar.csv', b'csv')
        with self.assertRaises(ValueError):
            _calendar_imports.calendar_source(self.store, 'alice', art['id'])
        with self.store.db() as db:
            db.execute(
                'INSERT INTO eip_calendar_sources VALUES(%s,%s,%s,%s)',
                ('alice', self.job, art['id'], 'rev'),
            )
        self.assertEqual(_calendar_imports.calendar_source(self.store, 'alice', art['id'])[0], 'rev')
        with self.assertRaises(ValueError):
            _calendar_imports.calendar_source(self.store, 'bob', art['id'])
