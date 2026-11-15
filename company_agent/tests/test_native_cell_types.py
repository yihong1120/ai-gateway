"""Regression for accepted native-edit numeric strings seen in the paid cohort."""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from langchain_core.messages import HumanMessage
from langchain_core.tools import ToolException

from company_agent.runtime.context import Context
from company_agent.services.delivery.native_cells import literal_changes
from company_agent.services.delivery.tools import plan
from company_agent.services.delivery.validation import prepare_output
from company_agent.services.delivery.validation import validate_delivery
from company_agent.services.files.errors import RecoverableToolError
from company_agent.services.files.operations import ToolBox
from company_agent.services.files.tools import file_tool
from company_agent.services.office.documents import create_spreadsheet
from company_agent.services.office.schemas import EditSpreadsheet
from company_agent.services.office.schemas import ReadSpreadsheet
from company_agent.tests.test_agent import Fixture


REQUEST = '讀source.xlsx，用edit_spreadsheet另存revised.xlsx，只把「工程」B2由10改12、「第二表」A1改為「已核對」，保留原簿。'


class LiteralCellTests(unittest.TestCase):
    def test_numeric_and_quoted_text_have_distinct_contracts(self):
        self.assertEqual(literal_changes('「工程」B2由10改12、「工程」C3從0改-1.25'), {('工程', 'B2'): 12, ('工程', 'C3'): -1.25})
        self.assertEqual(literal_changes('「工程」B2改為「12」'), {('工程', 'B2'): '12'})
        self.assertFalse(literal_changes('不要把「工程」B2改12'))
        with self.assertRaisesRegex(RecoverableToolError, 'conflicting_native_cell_request'):
            literal_changes('「工程」B2改12，另把「工程」B2改13')


class NativeCellTypeTests(unittest.IsolatedAsyncioTestCase, Fixture):
    def setUp(self):
        super().setUp()
        self.config.enable_spreadsheet_reports = True
        self.original = create_spreadsheet([
            {'name': '工程', 'rows': [['項目', '數量'], ['甲', 10]]},
            {'name': '第二表', 'rows': [['原值']]},
        ])
        self.file('source.xlsx', self.original)
        read = ToolBox(self.config, self.store, 'alice', self.job).read_spreadsheet(
            ReadSpreadsheet(root='project', path='source.xlsx'),
        )
        self.source = {k: read[k] for k in ('root', 'path', 'sha256')}
        self.runtime = SimpleNamespace(
            context=Context('alice', self.job, self.config, self.store),
            state={'messages': [HumanMessage(content=REQUEST)], 'sources': [self.source]}, tool_call_id='plan', stream_writer=lambda _: None,
        )

    async def bind(self, **spec):
        await plan(self.runtime, files=[{'name': 'revised.xlsx', **spec}])
        return self.store.delivery_plan('alice', self.job)

    async def edit(self, value):
        self.runtime.tool_call_id = 'edit-'+type(value).__name__
        return await file_tool(
            self.runtime, tool_name='edit_spreadsheet', input_schema=EditSpreadsheet,
            name='revised.xlsx', **self.source, changes=[
                {'sheet': '工程', 'cell': 'B2', 'expected': 10, 'value': value},
                {'sheet': '第二表', 'cell': 'A1', 'expected': '原值', 'value': '已核對'},
            ],
        )

    async def test_server_adds_exact_numeric_and_text_requirements(self):
        spec = await self.bind()
        rules = {(c['sheet'], c['cell']): c['expected'] for c in spec['files'][0]['cells']}
        self.assertEqual(rules, {('工程', 'B2'): 12, ('第二表', 'A1'): '已核對'})
        self.assertIs(type(rules[('工程', 'B2')]), int)

    async def test_wrong_type_in_plan_rejected_before_lock(self):
        with self.assertRaisesRegex(RecoverableToolError, 'native_cell_contract_mismatch'):
            await self.bind(cells=[{'sheet': '工程', 'cell': 'B2', 'expected': '12'}])
        self.assertIsNone(self.store.delivery_plan('alice', self.job))

    async def test_numeric_string_cannot_create_artifact_then_correct_retry_passes(self):
        await self.bind()
        with self.assertRaisesRegex(RecoverableToolError, 'planned_cell_value_mismatch'):
            await self.edit('12')
        self.assertFalse(self.store.files('alice', self.job))
        await self.edit(12)
        self.assertEqual(validate_delivery(self.store, 'alice', self.job)[0]['status'], 'passed')
        self.assertEqual((self.root/'source.xlsx').read_bytes(), self.original)

    async def test_quoted_numeric_text_remains_supported(self):
        self.runtime.state['messages'] = [HumanMessage(content=REQUEST.replace('B2由10改12', 'B2改為「12」'))]
        await self.bind()
        await self.edit('12')
        self.assertEqual(validate_delivery(self.store, 'alice', self.job)[0]['status'], 'passed')

    async def test_existing_formula_contract_uses_the_value_and_formula_flag(self):
        await self.bind(cells=[{'sheet': '工程', 'cell': 'B4', 'expected': '=SUM(B2:B3)', 'formula': True}])
        prepare_output(
            self.runtime.context, 'revised.xlsx', 'edit_spreadsheet', {
                **self.source, 'changes': [{'sheet': '工程', 'cell': 'B4', 'value': '=SUM(B2:B3)', 'formula': True}],
            },
        )
        with self.assertRaisesRegex(RecoverableToolError, 'planned_cell_value_mismatch'):
            prepare_output(
                self.runtime.context, 'revised.xlsx', 'edit_spreadsheet', {
                    **self.source, 'changes': [{'sheet': '工程', 'cell': 'B4', 'value': '=SUM(B2:B3)', 'formula': False}],
                },
            )

    async def test_final_validation_rejects_wrong_type_even_with_native_receipt(self):
        await self.bind()
        bad = create_spreadsheet([
            {'name': '工程', 'rows': [['項目', '數量'], ['甲', '12']]},
            {'name': '第二表', 'rows': [['已核對']]},
        ])
        artifact = self.store.artifact('alice', self.job, 'revised.xlsx', bad)
        self.store.begin_tool('alice', self.job, 'injected', 'edit_spreadsheet', {})
        self.store.finish_tool('alice', self.job, 'injected', {**artifact, 'source': self.source})
        with self.assertRaisesRegex(ToolException, '儲存格驗收失敗'):
            validate_delivery(self.store, 'alice', self.job)
