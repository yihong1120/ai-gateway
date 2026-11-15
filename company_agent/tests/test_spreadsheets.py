from __future__ import annotations

import hashlib
import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from subprocess import CalledProcessError
from unittest.mock import patch

from langchain_core.tools import ToolException
from openpyxl import load_workbook
from openpyxl import Workbook
from pydantic import ValidationError

from company_agent.config import Root
from company_agent.config import Settings
from company_agent.runtime.storage import Store
from company_agent.services.files.errors import RecoverableToolError
from company_agent.services.files.operations import ToolBox
from company_agent.services.files.operations import worker
from company_agent.services.files.tools import FILE_TOOL_SPECS
from company_agent.services.office.documents import analyze_spreadsheet
from company_agent.services.office.documents import create_spreadsheet
from company_agent.services.office.documents import read_spreadsheet
from company_agent.services.office.documents import spreadsheet_package
from company_agent.services.office.schemas import AnalyzeSpreadsheet
from company_agent.services.office.schemas import ReadSpreadsheet
from company_agent.services.office.schemas import Spreadsheet
from company_agent.tests.postgres_fixture import PostgreSQLTestCase


def replace_part(data, name, body):
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as original, zipfile.ZipFile(out, 'w') as changed:
        for info in original.infolist():
            changed.writestr(info, body if info.filename == name else original.read(info))
        if name not in original.namelist():
            changed.writestr(name, body)
    return out.getvalue()


def formula_workbook():
    book = Workbook()
    book.active.title = '資料'
    for value in (10, 20, '=SUM(A1:A2)', '=A1*2', '30', True, None):
        book.active.append([value])
    out = io.BytesIO()
    book.save(out)
    data = out.getvalue()
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        body = z.read('xl/worksheets/sheet1.xml').replace(
            b'<f>SUM(A1:A2)</f><v></v>',
            b'<f>SUM(A1:A2)</f><v>999</v>',
        )
    return replace_part(data, 'xl/worksheets/sheet1.xml', body)


class SpreadsheetTests(unittest.TestCase):
    def test_real_editable_artifact_and_formula_injection_are_literal(self):
        values = [
            '=HYPERLINK("https://example.invalid","click")', '+1+1',
            '-1+1', '@SUM(A1)', '保留中文', 4, 2.5, True, None,
        ]
        data = worker('spreadsheet', sheets=[{'name': '資料', 'rows': [values]}])
        book = load_workbook(io.BytesIO(data))
        self.assertEqual([book.active.cell(1, i).value for i in range(1, 10)], values)
        self.assertTrue(all(book.active.cell(1, i).data_type == 's' for i in range(1, 6)))
        book.active['A2'] = '可以修改'
        out = io.BytesIO()
        book.save(out)
        self.assertEqual(load_workbook(io.BytesIO(out.getvalue())).active['A2'].value, '可以修改')
        book.close()

    def test_formula_cache_is_unverified_and_never_used_in_analysis(self):
        data = formula_workbook()
        with patch('socket.create_connection', side_effect=AssertionError('No network allowed')):
            result = read_spreadsheet(data)
            stats = analyze_spreadsheet(data, '資料', 1, 1, 7)
        formula = result['rows'][2]['cells'][0]
        self.assertEqual(formula['formula'], '=SUM(A1:A2)')
        self.assertIsNone(formula['value'])
        self.assertEqual((formula['cached_value'], formula['cached_status']), (999, 'unverified'))
        self.assertEqual(result['rows'][3]['cells'][0]['cached_status'], 'missing')
        self.assertEqual((stats['count'], stats['sum'], stats['mean']), (2, 30, 15))
        self.assertEqual(stats['excluded'], {'formula': 2, 'blank': 1, 'other': 2})

    def test_pagination_sheet_names_empty_and_dimension_spoof(self):
        data = create_spreadsheet([
            {'name': "工'程", 'rows': [[1, 2], [3, 4], [5, 6]]},
            {'name': '空表', 'rows': []},
        ])
        result = read_spreadsheet(data, row_limit=1, column_limit=1)
        self.assertEqual(result['next_row'], 2)
        self.assertTrue(result['columns_truncated'])
        self.assertEqual(result['rows'][0]['cells'][0]['location'], "'工''程'!A1")
        self.assertEqual(len(read_spreadsheet(data, start_row=2)['rows']), 2)
        self.assertEqual(read_spreadsheet(data, start_row=99)['rows'], [])
        self.assertEqual(read_spreadsheet(data, sheet='空表')['sheets'][1]['rows'], 1)
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            body = z.read('xl/worksheets/sheet1.xml').replace(
                b'<sheetData>',
                b'<dimension ref="A1:XFD1048576"/><sheetData>',
            )
        spoof = read_spreadsheet(replace_part(data, 'xl/worksheets/sheet1.xml', body))
        self.assertEqual(spoof['sheets'][0]['rows'], 3)
        with self.assertRaises(ToolException):
            read_spreadsheet(data, sheet='missing')

    def test_contract_limits(self):
        for sheets in (
            [{'name': 'bad/name', 'rows': []}],
            [{'name': 'A', 'rows': []}, {'name': 'a', 'rows': []}],
            [{'name': 'A', 'rows': [[1]*101]}],
            [{'name': 'A', 'rows': [[1]]*1001}],
            [{'name': 'A', 'rows': [[float('nan')]]}],
            [{'name': 'A', 'rows': [[{'formula': '=1'}]]}],
            [{'name': 'A', 'rows': [[1]*100]*501}],
        ):
            with self.subTest(sheets=str(sheets)[:50]), self.assertRaises(ValidationError):
                Spreadsheet(name='a.xlsx', sheets=sheets)
        with self.assertRaises(ValidationError):
            AnalyzeSpreadsheet(root='a', path='b.xlsx', sheet='a', column=1, start_row=3, end_row=2)
        self.assertTrue({'read_spreadsheet', 'analyze_spreadsheet', 'create_spreadsheet'} <= FILE_TOOL_SPECS.keys())

    def test_dangerous_packages_and_resource_limits(self):
        data = create_spreadsheet([{'name': 'a', 'rows': [[1]]}])
        malicious = [
            ('xl/vbaProject.bin', b'macro'),
            ('xl/externalLinks/externalLink1.xml', b'<x/>'),
            ('xl/connections.xml', b'<x/>'),
            ('../escape.xml', b'<x/>'),
            ('xl/_rels/extra.rels', b'<Relationships><Relationship TargetMode="External" Target="https://example.invalid"/></Relationships>'),
            ('xl/_rels/workbook.xml.rels', b'<Relationships><Relationship Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="unbounded.xml"/></Relationships>'),
            ('[Content_Types].xml', b'<Types><Override ContentType="application/vnd.ms-excel.sheet.macroEnabled.main+xml"/></Types>'),
            ('xl/worksheets/sheet1.xml', b'<!DOCTYPE a [<!ENTITY x SYSTEM "file:///etc/passwd">]><a>&x;</a>'),
            ('xl/worksheets/sheet1.xml', b'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="10001"><c r="A10001"/></row></sheetData></worksheet>'),
            ('xl/worksheets/sheet1.xml', b'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1"><c r="CW1"/></row></sheetData></worksheet>'),
        ]
        for name, body in malicious:
            expected = RecoverableToolError if b'row r="10001"' in body else (ValueError, CalledProcessError)
            with self.subTest(part=name), self.assertRaises(expected):
                worker('read_spreadsheet', data=replace_part(data, name, body))
        out = io.BytesIO()
        with zipfile.ZipFile(out, 'w', compression=zipfile.ZIP_DEFLATED) as z:
            z.writestr('bomb.bin', b'0'*2_000_000)
        with self.assertRaises((ValueError, CalledProcessError)):
            spreadsheet_package(out.getvalue())

    def test_analysis_empty_range_and_numeric_overflow(self):
        data = create_spreadsheet([{'name': 'a', 'rows': [['no numbers'], [True], [None]]}])
        stats = analyze_spreadsheet(data, 'a', 1, 1, 3)
        self.assertEqual((stats['count'], stats['sum'], stats['mean'], stats['min']), (0, 0, None, None))
        data = create_spreadsheet([{'name': 'a', 'rows': [[1e308], [1e308]]}])
        with self.assertRaises(OverflowError):
            analyze_spreadsheet(data, 'a', 1, 1, 2)


class SpreadsheetToolTests(PostgreSQLTestCase):
    def test_authorization_source_hash_private_artifact_and_name_safety(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root, state = base/'docs', base/'state'
            root.mkdir()
            state.mkdir(mode=0o700)
            settings = Settings(
                state_dir=str(state), roots=[
                    Root(
                        name='project', path=str(root), readers=['alice'], model_access=True,
                    ),
                ],
            )
            store = Store(state)
            job = store.job('alice')
            box = ToolBox(settings, store, 'alice', job)
            data = formula_workbook()
            (root/'source.xlsx').write_bytes(data)
            source = box.read_spreadsheet(ReadSpreadsheet(root='project', path='source.xlsx'))
            self.assertEqual(source['sha256'], hashlib.sha256(data).hexdigest())
            stats = box.analyze_spreadsheet(
                AnalyzeSpreadsheet(
                    root='project', path='source.xlsx', sheet='資料', column=1, end_row=7,
                ),
            )
            self.assertEqual(stats['sum'], 30)
            output = box.create_spreadsheet(
                Spreadsheet(
                    name='draft.xlsx', sheets=[
                        {'name': 'summary', 'rows': [['total', stats['sum']]]},
                    ],
                ),
            )
            self.assertEqual(output['sources'][0]['sha256'], source['sha256'])
            self.assertFalse((root/'draft.xlsx').exists())
            name, _, artifact = store.read_artifact('alice', output['id'], job)
            self.assertEqual(name, 'draft.xlsx')
            self.assertEqual(read_spreadsheet(artifact)['rows'][0]['cells'][1]['value'], 30)
            for bad in ('../draft.xlsx', 'draft.xlsm'):
                with self.assertRaises(ValueError):  # schema now rejects before ToolBox
                    box.create_spreadsheet(Spreadsheet(name=bad, sheets=[{'name': 'a', 'rows': []}]))
            for owner, path in (('bob', 'source.xlsx'), ('alice', '../source.xlsx'), ('alice', 'source.xlsm')):
                with self.subTest(owner=owner, path=path), self.assertRaises(ValueError):
                    ToolBox(settings, store, owner, job).read_spreadsheet(ReadSpreadsheet(root='project', path=path))


if __name__ == '__main__':
    unittest.main()
