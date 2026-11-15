"""Secondary checks on generated Excel formulas; never edits benchmark grades.

Run after the paid campaign so offline LibreOffice work cannot affect latency.
Only synthetic generated reports are reconstructed in the existing sandbox.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import re
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.utils.cell import coordinate_to_tuple

from company_agent.services.office.sandbox import office_worker
from company_agent.services.office.worker import formula_valid
from examples.shared.runtime.modules import ModuleReference

_openpyxl = ModuleReference('openpyxl')
_openpyxl_chart = ModuleReference('openpyxl.chart')


VERSION = 'company-data-artifact-audit-v1'


def same_number(actual, expected):
    return (
        isinstance(actual, (int, float)) and not isinstance(actual, bool)
        and math.isclose(actual, expected, rel_tol=1e-9, abs_tol=1e-10)
    )


def chart_reference(value, sheet, cells):
    if not value or '!' not in value:
        return False
    name, span = value.rsplit('!', 1)
    return name.strip("'").replace("''", "'") == sheet and span.replace('$', '').upper() == cells


def static_chart_checks(book):
    checks = {
        'sum_function': False, 'average_function': False, 'bar_chart': False,
        'chart_title': False, 'chart_category_range': False, 'chart_value_range': False,
    }
    if '報表' not in book.sheetnames:
        return checks
    sheet = book['報表']
    checks['sum_function'] = bool(re.search(r'\bSUM\s*\(', str(sheet['B7'].value), re.I))
    checks['average_function'] = bool(re.search(r'\bAVERAGE\s*\(', str(sheet['B8'].value), re.I))
    if len(sheet._charts) != 1:
        return checks
    chart = sheet._charts[0]
    checks['bar_chart'] = 'BarChart' in type(chart).__name__
    title = ''.join(
        node.text or '' for node in chart.title.to_tree().iter(
            '{http://schemas.openxmlformats.org/drawingml/2006/main}t',
        )
    ) if chart.title else ''
    checks['chart_title'] = title.strip() == '各工區數量'
    if len(chart.series) != 1:
        return checks
    series = chart.series[0]
    category = getattr(series.cat, 'strRef', None) or getattr(series.cat, 'numRef', None)
    values = getattr(series.val, 'numRef', None)
    checks['chart_category_range'] = chart_reference(getattr(category, 'f', None), '報表', 'A2:A5')
    checks['chart_value_range'] = chart_reference(getattr(values, 'f', None), '報表', 'B2:B5')
    return checks


def request_from_artifact(book, sheet_name, changes):
    sheets, formulas = [], []
    for sheet in book:
        rows = [[None if cell.data_type == 'f' else cell.value for cell in row] for row in sheet]
        if sheet.title == sheet_name:
            for address, value in changes.items():
                row, column = coordinate_to_tuple(address)
                rows[row - 1][column - 1] = value
        for row in sheet:
            for cell in row:
                if cell.data_type == 'f':
                    formula_valid(cell.value, sheet.max_row, sheet.max_column)
                    formulas.append({'sheet': sheet.title, 'cell': cell.coordinate, 'formula': cell.value})
        sheets.append({'name': sheet.title, 'rows': rows})
    return {'sheets': sheets, 'formulas': formulas, 'charts': []}


def challenges(case, source):
    sheet = source['工程']
    if case == 'D11':
        a, b, c = (sheet[f'B{i}'].value for i in (2, 3, 4))
        return [(
            '換算', {'B3': b + 7}, {
                'D2': a, 'D3': (b + 7) * 1000,
                'D4': c * 10000, 'D5': a + (b + 7) * 1000 + c * 10000,
            },
        )]
    if case == 'D12':
        a, b, x, y = (sheet[cell].value for cell in ('B2', 'B3', 'C2', 'C3'))
        return [
            ('加權', {'B2': a + 300}, {'B5': ((a + 300) * x + b * y) / (a + 300 + b)}),
            ('加權', {'C2': x + 0.17}, {'B5': (a * (x + 0.17) + b * y) / (a + b)}),
        ]
    if case == 'D13':
        numbers = [sheet[f'B{i}'].value for i in range(2, 6)]
        return [('報表', {'B2': numbers[0] + 17}, {'B7': sum(numbers) + 17, 'B8': (sum(numbers) + 17) / 4})]
    raise ValueError('Unknown secondary audit case')


def audit(case, artifact, source_bytes):
    book = load_workbook(io.BytesIO(artifact), data_only=False)
    source = load_workbook(io.BytesIO(source_bytes), data_only=False)
    checks = static_chart_checks(book) if case == 'D13' else {}
    probes = []
    for sheet, changes, expected in challenges(case, source):
        if sheet not in book.sheetnames:
            checks['expected_sheet'] = False
            continue
        req = request_from_artifact(book, sheet, changes)
        calculation, data = office_worker('report', **req)
        values = load_workbook(io.BytesIO(data), data_only=True)
        passed = all(same_number(values[sheet][cell].value, value) for cell, value in expected.items())
        probes.append({
            'sheet': sheet, 'changed_inputs': changes, 'expected': expected,
            'actual': {cell: values[sheet][cell].value for cell in expected},
            'passed': passed, 'engine': calculation['engine'],
        })
    checks['formula_outputs_follow_changed_inputs'] = bool(probes) and all(probe['passed'] for probe in probes)
    return {'checks': checks, 'probes': probes, 'passed': all(checks.values())}


def self_test():
    Workbook = _openpyxl.attribute('Workbook')
    BarChart, Reference = _openpyxl_chart.attributes('BarChart', 'Reference')
    source = Workbook()
    source.active.title = '工程'
    for row in [['工區', '數量'], ['甲', 10], ['乙', 20], ['丙', 30], ['丁', 40]]:
        source.active.append(row)
    original = io.BytesIO()
    source.save(original)
    book = Workbook()
    sheet = book.active
    sheet.title = '報表'
    for row in source.active.values:
        sheet.append(row)
    sheet['A7'], sheet['B7'] = '合計', '=SUM(B2:B5)'
    sheet['A8'], sheet['B8'] = '平均', '=AVERAGE(B2:B5)'
    chart = BarChart()
    chart.title = '各工區數量'
    chart.add_data(Reference(sheet, min_col=2, min_row=2, max_row=5))
    chart.set_categories(Reference(sheet, min_col=1, min_row=2, max_row=5))
    sheet.add_chart(chart, 'D2')
    correct = io.BytesIO()
    book.save(correct)
    assert audit('D13', correct.getvalue(), original.getvalue())['passed']
    # This has the correct initial numeric answers and named functions but no dependency.
    sheet['B7'], sheet['B8'] = '=SUM(100)', '=AVERAGE(25)'
    wrong = io.BytesIO()
    book.save(wrong)
    result = audit('D13', wrong.getvalue(), original.getvalue())
    assert not result['passed'] and not result['checks']['formula_outputs_follow_changed_inputs']
    assert result['checks']['sum_function'] and result['checks']['average_function']
    chart.title = '錯誤標題'
    chart.set_categories(Reference(sheet, min_col=1, min_row=1, max_row=4))
    checks = static_chart_checks(book)
    assert not checks['chart_title'] and not checks['chart_category_range']
    print(json.dumps({'self_test': 'passed', 'real_calc_negative_control': True}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--self-test', action='store_true')
    args = parser.parse_args()
    if args.self_test:
        self_test()
    if not args.report:
        return
    if not args.output:
        parser.error('--output is required with --report')
    report = json.loads(args.report.read_text())
    if not report.get('completed_at') or report.get('unrun'):
        parser.error('The entire paid campaign must finish before secondary audit.')
    results = []
    for row in report['runs']:
        if row['case'] not in {'D11', 'D12', 'D13'}:
            continue
        item = {'case': row['case'], 'arm': row['arm'], 'repeat': row['repeat'], 'primary_passed': row['passed']}
        folder = args.report.parent / row['folder']
        artifact = folder / 'downloads/checked.xlsx'
        if not artifact.exists():
            item.update(status='artifact_missing', passed=None)
        else:
            before = artifact.read_bytes()
            item['artifact_sha256'] = hashlib.sha256(before).hexdigest()
            assert item['artifact_sha256'] == row['artifact_sha256']['checked.xlsx']
            try:
                item.update(audit(row['case'], before, (folder / 'sources/source.xlsx').read_bytes()), status='audited')
            except Exception as error:
                item.update(status='audit_inconclusive', error_type=type(error).__name__, passed=None)
            assert artifact.read_bytes() == before
        results.append(item)
    value = {
        'version': VERSION, 'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'scope': 'secondary audit; primary grades unchanged; synthetic generated reports only',
        'primary_source_sha256': report['source_sha256'], 'runs': results,
        'counts': {
            status: sum(row['status'] == status for row in results)
            for status in ('audited', 'artifact_missing', 'audit_inconclusive')
        },
        'strict_passed': sum(row['passed'] is True for row in results),
        'strict_failed': sum(row['passed'] is False for row in results),
    }
    args.output.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({key: value[key] for key in ('version', 'counts', 'strict_passed', 'strict_failed')}))


if __name__ == '__main__':
    main()
