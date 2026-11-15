"""Source-backed admission and native-output proof for preservation plans."""
from __future__ import annotations

import hashlib
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from langchain_core.messages import AIMessage
from langchain_core.messages import HumanMessage
from langchain_core.tools import ToolException
from pydantic import ValidationError

from company_agent.runtime.context import Context
from company_agent.runtime.execution import DocumentAgent
from company_agent.services.delivery.explicit import check_explicit_cells
from company_agent.services.delivery.preservation import native_targets
from company_agent.services.delivery.preservation import verify_preservation_sources
from company_agent.services.delivery.schemas import DeliveryPlan
from company_agent.services.delivery.schemas import TextExpectation
from company_agent.services.delivery.tools import plan
from company_agent.services.delivery.validation import prepare_output
from company_agent.services.delivery.validation import validate_delivery
from company_agent.services.files.errors import RecoverableToolError
from company_agent.services.files.operations import ToolBox
from company_agent.services.files.tools import file_tool
from company_agent.services.office.documents import create_spreadsheet
from company_agent.services.office.schemas import EditSpreadsheet
from company_agent.services.office.schemas import ReadSpreadsheet
from company_agent.tests.test_agent import call
from company_agent.tests.test_agent import Fixture
from company_agent.tests.test_enterprise_completion import finish
from company_agent.tests.test_multi_agent import RawRoleModel


REQUEST = '讀 source.xlsx，用edit_spreadsheet另存revised.xlsx，保留原簿，只把「工程」B2由10改12。'


class NativeTargetTests(unittest.TestCase):
    def test_unbound_statistical_placeholder_cannot_be_locked(self):
        with self.assertRaisesRegex(ValidationError, 'statistic'):
            TextExpectation(text='合計：{value}')
        self.assertEqual(
            TextExpectation(
                text='合計：{value}', statistic={
                    'key': 'actual', 'metric': 'sum',
                },
            ).statistic.metric, 'sum',
        )

    def test_literal_placeholder_requires_user_request_and_explicit_escaping(self):
        spec = DeliveryPlan(files=[{'name': 'template.docx', 'texts': [{'text': '合計：{{value}}'}]}])
        with self.assertRaisesRegex(ToolException, '字面預留符號'):
            check_explicit_cells('交付template.docx，寫真實合計數值。', spec)
        check_explicit_cells('交付template.docx，保留字面符號「{value}」。', spec)

    def test_explicit_row_aligned_statistic_range_rejects_missing_or_wrong_metric(self):
        instruction = '交付checked.xlsx；「結果」表A1=合計、A2=筆數、A3=最小、A4=最大、A5=平均，B1:B5 依序填對應的真實工具數值。'
        metrics = [('合計', 'sum'), ('筆數', 'count'), ('最小', 'min'), ('最大', 'max'), ('平均', 'mean')]
        cells = [
            {'sheet': '結果', 'cell': 'A' + str(i), 'expected': label}
            for i, (label, _) in enumerate(metrics, 1)
        ]
        cells += [
            {'sheet': '結果', 'cell': 'B' + str(i), 'statistic': {'key': 'stats', 'metric': metric}}
            for i, (_, metric) in enumerate(metrics, 1)
        ]
        spec = DeliveryPlan(
            statistics=[{
                'key': 'stats', 'root': 'project', 'path': 'source.xlsx',
                'sheet': '工程', 'column': 1, 'start_row': 2, 'end_row': 10,
            }],
            files=[{'name': 'checked.xlsx', 'sheet_names': ['結果'], 'cells': cells}],
        )
        check_explicit_cells(instruction, spec)
        spec.files[0].cells[-3].statistic.metric = 'mean'
        with self.assertRaisesRegex(ToolException, 'B3.*min'):
            check_explicit_cells(instruction, spec)
        spec.files[0].cells[-3].statistic.metric = 'min'
        spec.files[0].cells.pop()
        with self.assertRaisesRegex(ToolException, 'B5.*mean'):
            check_explicit_cells(instruction, spec)

    def test_native_target_does_not_capture_another_new_report(self):
        spec = DeliveryPlan(files=[{'name': 'revised.xlsx'}, {'name': 'report.xlsx'}])
        text = '用edit_spreadsheet另存revised.xlsx，另用create_spreadsheet_report輸出report.xlsx。'
        self.assertEqual(native_targets(text, spec.files), {'revised.xlsx'})


class PreservationTests(unittest.IsolatedAsyncioTestCase, Fixture):
    def setUp(self):
        super().setUp()
        self.config.enable_spreadsheet_reports = True

    def source(self, name='source.xlsx'):
        data = create_spreadsheet([
            {'name': '工程', 'rows': [['項目', '數量'], ['甲', 10]]},
            {'name': '第二表', 'rows': [['原值']]},
        ])
        self.file(name, data)
        result = ToolBox(self.config, self.store, 'alice', self.job).read_spreadsheet(
            ReadSpreadsheet(root='project', path=name),
        )
        return data, {k: result[k] for k in ('root', 'path', 'sha256')}

    def runtime(self, sources, instruction=REQUEST):
        return SimpleNamespace(
            context=Context('alice', self.job, self.config, self.store),
            state={'messages': [HumanMessage(content=instruction)], 'sources': sources}, tool_call_id='plan', stream_writer=lambda _: None,
        )

    async def bind(self, runtime, **file):
        await plan(runtime, files=[{'name': 'revised.xlsx', **file}])
        return self.store.delivery_plan('alice', self.job)

    async def test_hallucinated_sheet_is_rejected_before_lock_then_source_names_are_derived(self):
        data, source = self.source()
        runtime = self.runtime([source])
        with self.assertRaisesRegex(RecoverableToolError, 'preservation_plan_mismatch'):
            await self.bind(runtime, sheet_names=['工程', 'invented_alias', '第二表'])
        self.assertIsNone(self.store.delivery_plan('alice', self.job))
        result = await self.bind(runtime)
        self.assertEqual(result['files'][0]['sheet_names'], ['工程', '第二表'])
        self.assertEqual(result['files'][0]['base_source'], source)
        self.assertEqual((self.root/'source.xlsx').read_bytes(), data)

    async def test_source_and_artifact_may_share_a_basename_without_overwriting_source(self):
        data, source = self.source('revised.xlsx')
        instruction = '讀revised.xlsx，保留原簿，用edit_spreadsheet另存revised.xlsx作為下載成果。'
        result = await self.bind(self.runtime([source], instruction=instruction))
        self.assertEqual(result['files'][0]['base_source'], source)
        self.assertEqual((self.root / 'revised.xlsx').read_bytes(), data)

    async def test_missing_or_forged_read_reference_cannot_bind(self):
        _, source = self.source()
        with self.assertRaisesRegex(RecoverableToolError, 'preservation_source_required'):
            await self.bind(self.runtime([]))
        with self.assertRaisesRegex(RecoverableToolError, 'preservation_source_unread'):
            await self.bind(self.runtime([source]), base_source={**source, 'sha256': '0'*64})
        self.assertIsNone(self.store.delivery_plan('alice', self.job))

    async def test_injected_native_plan_must_have_verified_source_before_model(self):
        _, source = self.source()
        model = RawRoleModel()
        with self.assertRaisesRegex(ValueError, 'base_source'):
            await DocumentAgent(self.config, model).run('alice', REQUEST, delivery_plan={'files': [{'name': 'revised.xlsx'}]})
        with self.assertRaisesRegex(RecoverableToolError, 'preservation_plan_mismatch'):
            await DocumentAgent(self.config, model).run(
                'alice', REQUEST, delivery_plan={
                    'files': [
                        {
                            'name': 'revised.xlsx', 'base_source': source, 'sheet_names': ['made_up'],
                        }, ],
                },
            )
        self.assertFalse(model.invocations)

    async def test_multiple_sources_require_explicit_exact_reference(self):
        _, a = self.source()
        _, b = self.source('reference.xlsx')
        runtime = self.runtime([a, b])
        with self.assertRaisesRegex(RecoverableToolError, 'preservation_source_required'):
            await self.bind(runtime)
        result = await self.bind(runtime, base_source=a)
        self.assertEqual(result['files'][0]['base_source'], a)

    async def test_source_change_and_acl_revocation_do_not_create_plan(self):
        data, source = self.source()
        runtime = self.runtime([source])
        (self.root/'source.xlsx').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, '來源已變更'):
            await self.bind(runtime)
        (self.root/'source.xlsx').write_bytes(data)
        self.config.roots[0].readers = []
        with self.assertRaises(ValueError):
            await self.bind(runtime)
        self.assertIsNone(self.store.delivery_plan('alice', self.job))

    async def test_new_report_can_choose_its_own_sheet_without_source_preservation(self):
        _, source = self.source()
        runtime = self.runtime([source], instruction='讀source.xlsx，交付revised.xlsx作全新統計報表。')
        with patch('company_agent.services.delivery.preservation.worker') as parser:
            result = await self.bind(runtime, sheet_names=['統計報表'])
        parser.assert_not_called()
        self.assertIsNone(result['files'][0]['base_source'])
        self.assertEqual(result['files'][0]['sheet_names'], ['統計報表'])

    async def test_bound_output_requires_native_edit_and_the_exact_source(self):
        _, source = self.source()
        await self.bind(self.runtime([source]))
        ctx = Context('alice', self.job, self.config, self.store)
        with self.assertRaisesRegex(RecoverableToolError, 'native_edit_required'):
            prepare_output(ctx, 'revised.xlsx', 'create_spreadsheet', {})
        with self.assertRaisesRegex(RecoverableToolError, 'preservation_source_mismatch'):
            prepare_output(ctx, 'revised.xlsx', 'edit_spreadsheet', {**source, 'sha256': '0'*64})
        prepare_output(ctx, 'revised.xlsx', 'edit_spreadsheet', source)
        (self.root/'source.xlsx').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, '原簿來源已變更'):
            prepare_output(ctx, 'revised.xlsx', 'edit_spreadsheet', source)

    async def test_rebuilt_file_cannot_claim_native_preservation(self):
        data, source = self.source()
        await self.bind(self.runtime([source]))
        self.store.artifact('alice', self.job, 'revised.xlsx', data)
        with self.assertRaisesRegex(RecoverableToolError, 'native_edit_receipt_required'):
            validate_delivery(self.store, 'alice', self.job)

    async def test_true_native_receipt_passes_and_final_source_is_rechecked(self):
        _, source = self.source()
        await self.bind(self.runtime([source]))
        runtime = self.runtime([source])
        runtime.tool_call_id = 'native'
        await file_tool(
            runtime, tool_name='edit_spreadsheet', input_schema=EditSpreadsheet,
            name='revised.xlsx', **source, changes=[{'sheet': '工程', 'cell': 'B2', 'expected': 10, 'value': 12}],
        )
        self.assertEqual(validate_delivery(self.store, 'alice', self.job)[0]['status'], 'passed')
        specification = DeliveryPlan.model_validate(self.store.delivery_plan('alice', self.job))
        (self.root/'source.xlsx').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, '原簿來源已變更'):
            verify_preservation_sources(runtime.context, specification)

    async def test_real_graph_recovers_from_unbound_placeholder_schema_error(self):
        self.config.structured_completion = True
        self.config.require_delivery_plan = True
        self.config.orchestration_mode = 'hybrid'
        self.config.office_execution_mode = 'specialist'
        self.config.max_steps = 12
        ids = []

        def document_done(messages):
            ids.append(json.loads(messages[-1].content)['id'])
            return finish('complete_assignment', ids=ids)

        model = RawRoleModel(
            coordinator=[
                call(
                    'plan_delivery', {
                        'files': [{
                            'name': 'summary.docx',
                            'texts': [{'text': '合計：{value}'}],
                        }],
                    }, 'unbound',
                ),
                call(
                    'plan_delivery', {
                        'files': [{
                            'name': 'summary.docx',
                            'texts': [{'text': '完成'}],
                        }],
                    }, 'corrected',
                ),
                call(
                    'delegate_document', {
                        'task': '製作summary.docx，文字完成',
                        'work_key': 'document', 'outputs': ['summary.docx'],
                    }, 'doc',
                ),
                lambda _: finish(ids=ids),
            ], document=[
                call(
                    'create_word', {
                        'name': 'summary.docx', 'title': '工作說明',
                        'paragraphs': [{'text': '完成', 'heading': False}],
                    }, 'word',
                ),
                document_done,
            ],
        )
        result = await DocumentAgent(self.config, model).run('alice', '交付summary.docx，包含文字「完成」。')
        self.assertEqual(result['status'], 'done', result)
        self.assertEqual(
            self.store.delivery_plan('alice', result['job_id'])['files'][0]['texts'],
            [{'text': '完成', 'statistic': None}],
        )

    async def test_real_graph_repairs_bad_plan_before_any_child_and_reviews_true_native_output(self):
        original, source = self.source()
        self.config.structured_completion = True
        self.config.require_delivery_plan = True
        self.config.require_artifact_review = True
        self.config.office_execution_mode = 'specialist'
        self.config.max_steps = 25
        self.config.orchestration_mode = 'hybrid'
        ids = []
        bad = {
            'files': [
                {
                    'name': 'revised.xlsx', 'sheet_names': [
                        '工程', 'wrong_alias', '第二表',
                    ],
                }, {'name': 'summary.docx'},
            ],
        }
        good = {'files': [{'name': 'revised.xlsx'}, {'name': 'summary.docx', 'texts': [{'text': '已修改'}]}]}

        def done(messages):
            ids.append(json.loads(messages[-1].content)['id'])
            return finish('complete_assignment', ids=[ids[-1]])
        model = RawRoleModel(
            coordinator=[
                call('read_spreadsheet', {'root': 'project', 'path': 'source.xlsx'}, 'read'),
                call('plan_delivery', bad, 'bad'), call('plan_delivery', good, 'good'),
                call(
                    'delegate_data', {
                        'task': '原簿B2由10改12',
                        'work_key': 'native', 'outputs': ['revised.xlsx'],
                    }, 'data',
                ),
                call('delegate_document', {'task': '寫修改說明', 'work_key': 'doc', 'outputs': ['summary.docx']}, 'doc'),
                call('delegate_reviewer', {'task': '核對兩份成果', 'work_key': 'review', 'outputs': []}, 'review'),
                lambda _:finish(ids=ids),
            ], data=[
                call(
                    'edit_spreadsheet', {
                        'name': 'revised.xlsx', **source,
                        'changes': [{'sheet': '工程', 'cell': 'B2', 'expected': 10, 'value': '12'}],
                    }, 'bad_type',
                ),
                call(
                    'edit_spreadsheet', {
                        'name': 'revised.xlsx', **source,
                        'changes': [{'sheet': '工程', 'cell': 'B2', 'expected': 10, 'value': 12}],
                    }, 'edit',
                ), done,
            ],
            document=[
                call(
                    'create_word', {
                        'name': 'summary.docx', 'title': '修改說明',
                        'paragraphs': [{'text': '已修改', 'heading': False}],
                    }, 'word',
                ), done,
            ],
            reviewer=[
                lambda _:AIMessage(
                    content='', tool_calls=[
                        {
                            'name': 'inspect_artifact', 'id': str(
                                i,
                            ), 'args': {'artifact_id': v},
                        } for i, v in enumerate(ids)
                    ],
                ),
                lambda _:finish('complete_assignment', ids=ids),
            ],
        )
        result = await DocumentAgent(self.config, model).run('alice', REQUEST+'另交 summary.docx，文字已修改，獨立審核。')
        self.assertEqual(result['status'], 'done', result)
        self.assertEqual(len(result['tool_feedback']), 2)
        self.assertIn('preservation_plan_mismatch', result['tool_feedback'][0]['message'])
        self.assertIn('planned_cell_value_mismatch', result['tool_feedback'][1]['message'])
        self.assertEqual(len(result['files']), 2)
        self.assertFalse(result['artifact_review']['unreviewed_ids'])
        self.assertEqual(
            hashlib.sha256(
                (self.root/'source.xlsx').read_bytes(),
            ).hexdigest(), hashlib.sha256(original).hexdigest(),
        )
        stored = self.store.delivery_plan('alice', result['job_id'])
        self.assertEqual(stored['files'][0]['sheet_names'], ['工程', '第二表'])
