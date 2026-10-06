"""Offline fixed-path OCR page preparation and NEW spreadsheet reports only."""
from __future__ import annotations

import json
import math
import re
import subprocess
import tempfile
from pathlib import Path


def formula_valid(value, rows, columns):
    from openpyxl.formula import Tokenizer
    from openpyxl.utils.cell import range_boundaries

    if not isinstance(value, str) or not value.startswith('=') or len(value) > 300:
        raise ValueError('formula')
    if any(x in value for x in ('!', '[', ']', '|', '\\', '"', "'", ';', '{', '}')):
        raise ValueError('external formula')
    for token in Tokenizer(value).items:
        if token.type == 'FUNC':
            if token.subtype == 'OPEN' and token.value not in {
                'SUM(',
                'AVERAGE(',
                'MIN(',
                'MAX(',
                'COUNT(',
                'ROUND(',
                'ABS(',
            }:
                raise ValueError('function')
        elif token.type == 'OPERAND':
            if token.subtype == 'NUMBER':
                if not math.isfinite(float(token.value)) or abs(float(token.value)) > 1e15:
                    raise ValueError('number')
            elif token.subtype == 'RANGE':
                if not re.fullmatch(
                    r'\$?[A-Z]{1,2}\$?[1-9][0-9]{0,3}(?::\$?[A-Z]{1,2}\$?[1-9][0-9]{0,3})?',
                    token.value,
                ):
                    raise ValueError('reference')
                a, b, c, d = range_boundaries(token.value)
                if not (1 <= a <= c <= columns and 1 <= b <= d <= rows):
                    raise ValueError('range')
            else:
                raise ValueError('operand')
        elif token.type == 'OPERATOR-INFIX':
            if token.value not in {'+', '-', '*', '/'}:
                raise ValueError('operator')
        elif token.type == 'OPERATOR-PREFIX':
            if token.value not in {'+', '-'}:
                raise ValueError('prefix')
        elif token.type == 'OPERATOR-POSTFIX':
            if token.value != '%':
                raise ValueError('postfix')
        elif token.type not in {'PAREN', 'SEP', 'WHITE-SPACE'}:
            raise ValueError('token')


def report(req):
    from openpyxl import Workbook, load_workbook
    from openpyxl.chart import BarChart, LineChart, Reference

    book = Workbook()
    book.remove(book.active)
    for spec in req['sheets']:
        ws = book.create_sheet(spec['name'])
        for row in spec['rows']:
            ws.append(row)
            # No implicit formula injection; formulas have a separate typed field.
            for cell in ws[ws.max_row]:
                if isinstance(cell.value, str):
                    cell.data_type = 's'
    for spec in req['formulas']:
        ws = book[spec['sheet']]
        formula_valid(spec['formula'], ws.max_row, ws.max_column)
        cell = ws[spec['cell']]
        if cell.value is not None:
            raise ValueError('formula overwrites value')
        cell.value = spec['formula']
    for spec in req['charts']:
        ws = book[spec['sheet']]
        if not (
            spec['end_row'] <= ws.max_row
            and max(spec['value_column'], spec['category_column']) <= ws.max_column
        ):
            raise ValueError('chart range')
        chart = BarChart() if spec['kind'] == 'bar' else LineChart()
        chart.title = spec['title']
        chart.add_data(
            Reference(
                ws,
                min_col=spec['value_column'],
                max_col=spec['value_column'],
                min_row=spec['start_row'],
                max_row=spec['end_row'],
            ),
        )
        chart.set_categories(
            Reference(
                ws,
                min_col=spec['category_column'],
                max_col=spec['category_column'],
                min_row=spec['start_row'],
                max_row=spec['end_row'],
            ),
        )
        ws.add_chart(chart, spec['anchor'])
    with tempfile.TemporaryDirectory(dir='/tmp') as directory:
        root = Path(directory)
        profile = root / 'profile'
        profile.mkdir()
        (profile / 'user').mkdir()
        (profile / 'user/registrymodifications.xcu').write_text(
            '<oor:items xmlns:oor="http://openoffice.org/2001/registry"><item oor:path="/org.openoffice.Office.Common/Security/Scripting"><prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop></item></oor:items>',
        )
        source = root / 'report.xlsx'
        book.save(source)
        run = subprocess.run(
            [
                'libreoffice',
                '-env:UserInstallation=' + profile.as_uri(),
                '--headless',
                '--nologo',
                '--nodefault',
                '--nofirststartwizard',
                '--norestore',
                '--convert-to',
                'xlsx:Calc MS Excel 2007 XML',
                '--outdir',
                '/out',
                str(source),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=45,
        )
        if run.returncode:
            raise ValueError('calc')
    target = Path('/out/report.xlsx')
    if not target.is_file() or target.stat().st_size > 20000000:
        raise ValueError('output')
    checked = load_workbook(target, data_only=True)
    formulas = load_workbook(target, data_only=False)
    for sheet in checked:
        for row in sheet:
            if any(c.data_type == 'e' for c in row):
                raise ValueError('spreadsheet calculation error')
    values = []
    for spec in req['formulas']:
        value = checked[spec['sheet']][spec['cell']].value
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(value)
        ):
            raise ValueError('no numeric result')
        if formulas[spec['sheet']][spec['cell']].data_type != 'f':
            raise ValueError('missing formula')
        values.append(
            {
                'sheet': spec['sheet'],
                'cell': spec['cell'],
                'formula': spec['formula'],
                'value': value,
            },
        )
    if sum(len(s._charts) for s in formulas) != len(req['charts']):
        raise ValueError('missing chart')
    # Verify literal inputs survived conversion; this is never a source roundtrip.
    for spec in req['sheets']:
        for r, row in enumerate(spec['rows'], 1):
            for c, value in enumerate(row, 1):
                if value is not None and checked[spec['name']].cell(r, c).value != value:
                    raise ValueError('changed input')
    return {
        'formula_results': values,
        'chart_count': len(req['charts']),
        'engine': 'LibreOffice Calc',
        'notice': '新建計算報表；只支援列明的同表公式，不是任意 Excel 重算或原簿無損修改。圖表外觀須校對。',
    }


def page(req):
    import pymupdf

    source = Path('/input/source' + req['suffix'])
    with pymupdf.open(source) as doc:
        if (
            doc.needs_pass
            or not 1 <= doc.page_count <= 100
            or not 1 <= req['page'] <= doc.page_count
        ):
            raise ValueError('page')
        p = doc[req['page'] - 1]
        if p.rect.width * p.rect.height * 4 > 20000000:
            raise ValueError('pixels')
        scale = (
            min(2, req['max_edge'] / max(p.rect.width, p.rect.height)) if req.get('max_edge') else 2
        )
        pix = p.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
        pix.save('/out/page.png')
        return {
            'pages': doc.page_count,
            'page': req['page'],
            'width': pix.width,
            'height': pix.height,
        }


if __name__ == '__main__':
    req = json.loads(Path('/input/request.json').read_text())
    result = {'ocr_page': page, 'report': report}[req['operation']](req)
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
