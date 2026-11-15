from __future__ import annotations

import hashlib
import io
import json
import unittest
from subprocess import CalledProcessError
from unittest.mock import patch

from company_agent.evals.scenario_suite import prepare
from company_agent.evals.scenario_suite import SCENARIOS
from company_agent.runtime.execution import DocumentAgent
from company_agent.services.files.errors import RecoverableToolError
from company_agent.services.files.operations import ToolBox
from company_agent.services.files.operations import worker
from company_agent.services.files.schemas import Read
from company_agent.services.office.documents import create_spreadsheet
from company_agent.services.office.schemas import ReadSpreadsheet
from company_agent.tests.test_agent import call
from company_agent.tests.test_agent import Fixture
from company_agent.tests.test_enterprise_completion import finish
from company_agent.tests.test_enterprise_completion import RawRoleModel
from examples.shared.runtime.modules import ModuleReference

_openpyxl = ModuleReference('openpyxl')


class FormatDispatchTests(Fixture):
    def test_scenario_names_existing_sheet_and_missing_sheet_is_not_substituted(self):
        directory = self.root/'scenario'
        prepare(directory, 'data_to_document')
        box = ToolBox(self.config, self.store, 'alice', self.job)
        value = box.read_spreadsheet(ReadSpreadsheet(root='project', path='scenario/source.xlsx', sheet='工程'))
        self.assertIn('工作表「'+value['sheet']+'」', SCENARIOS['data_to_document'])
        with self.assertRaises(RecoverableToolError):
            box.read_spreadsheet(ReadSpreadsheet(root='project', path='scenario/source.xlsx', sheet='工程表'))

    def test_xlsx_direct_reader_pagination_scope_and_hash(self):
        raw = create_spreadsheet([
            {'name': '工程', 'rows': [[10], [20], ['未提供']]},
            {'name': '其他', 'rows': [[99]]},
        ])
        source = self.file('SOURCE.XLSX', raw)
        box = ToolBox(self.config, self.store, 'alice', self.job)
        with patch('company_agent.services.files.operations.worker', wraps=worker) as invoked:
            page = box.read_file(Read(root='project', path='SOURCE.XLSX', limit=2))
            self.assertEqual([c.args[0] for c in invoked.call_args_list], ['read_spreadsheet'])
        self.assertEqual(page['reader'], 'read_spreadsheet')
        self.assertEqual(page['scope'], 'first_worksheet_rows')
        self.assertEqual(page['sheet'], '工程')
        self.assertEqual([s['name'] for s in page['sheets']], ['工程', '其他'])
        self.assertEqual([r['cells'][0]['value'] for r in page['rows']], [10, 20])
        self.assertEqual(page['next_start'], 2)
        second = box.read_file(Read(root='project', path='SOURCE.XLSX', start=2, limit=2))
        self.assertEqual(second['rows'][0]['cells'][0]['value'], '未提供')
        self.assertIsNone(second['next_start'])
        self.assertIn('不代表整本已讀完', second['notice'])
        self.assertEqual(page['sha256'], hashlib.sha256(raw).hexdigest())
        self.assertEqual(source.read_bytes(), raw)

    def test_formula_cache_remains_unverified_and_columns_explicit(self):
        Workbook = _openpyxl.attribute('Workbook')
        book = Workbook()
        book.active['A1'] = '=1+2'
        book.active['U1'] = 99
        stream = io.BytesIO()
        book.save(stream)
        book.close()
        self.file('formula.xlsx', stream.getvalue())
        result = ToolBox(self.config, self.store, 'alice', self.job).read_file(
            Read(root='project', path='formula.xlsx'),
        )
        cell = result['rows'][0]['cells'][0]
        self.assertEqual(cell['formula'], '=1+2')
        self.assertIsNone(cell['value'])
        self.assertEqual(cell['cached_status'], 'missing')
        self.assertTrue(result['columns_truncated'])

    def test_permission_invalid_file_and_range_do_not_fallback(self):
        self.file('broken.xlsx', b'not an Office file')
        box = ToolBox(self.config, self.store, 'alice', self.job)
        with patch('company_agent.services.files.operations.worker', wraps=worker) as invoked:
            with self.assertRaises((ValueError, CalledProcessError)):
                box.read_file(Read(root='project', path='broken.xlsx'))
            self.assertEqual(invoked.call_count, 1)
        with patch('company_agent.services.files.operations.worker') as invoked:
            for args in (
                Read(root='project', path='../secret.xlsx'),
                Read(root='project', path='broken.xlsx', start=10_000),
            ):
                with self.assertRaises((ValueError, CalledProcessError)):
                    box.read_file(args)
            with self.assertRaises((ValueError, CalledProcessError)):
                ToolBox(self.config, self.store, 'bob', self.job).read_file(Read(root='project', path='broken.xlsx'))
            invoked.assert_not_called()
        self.assertFalse(box.sources)


class ReviewerFormatDispatchTests(unittest.IsolatedAsyncioTestCase, Fixture):
    async def test_reviewer_general_reader_really_verifies_xlsx_and_artifact(self):
        self.file('source.xlsx', create_spreadsheet([{'name': '工程', 'rows': [[10], [20], ['未提供']]}]))
        self.config.structured_completion = True
        self.config.require_artifact_review = True
        ids = []

        def created(messages):
            ids.append(json.loads(messages[-1].content)['id'])
            return finish('complete_assignment', ids=ids)

        def verify_source(messages):
            value = json.loads(messages[-1].content)
            self.assertEqual([r['cells'][0]['value'] for r in value['rows']], [10, 20, '未提供'])
            return call('inspect_artifact', {'artifact_id': ids[0]}, 'inspect')
        model = RawRoleModel(
            coordinator=[
                call('delegate_document', {'task': '製作統計草稿'}, 'document'),
                call('delegate_reviewer', {'task': '實際讀來源 XLSX 及草稿核對'}, 'review'), lambda _:finish(ids=ids),
            ],
            document=[
                call(
                    'create_word', {
                        'name': 'summary.docx', 'title': '統計', 'paragraphs': [
                            {'text': '合計30；2筆數值；排除未提供文字。', 'heading': False},
                        ],
                    }, 'create',
                ), created,
            ],
            reviewer=[
                call('read_file', {'root': 'project', 'path': 'source.xlsx'}, 'source'), verify_source,
                lambda _:finish('complete_assignment', ids=ids),
            ],
        )
        result = await DocumentAgent(self.config, model).run('alice', '製作及核對統計草稿')
        self.assertEqual(result['status'], 'done', result)
        self.assertNotIn('errors', result)
        self.assertFalse(result['artifact_review']['unreviewed_ids'])
