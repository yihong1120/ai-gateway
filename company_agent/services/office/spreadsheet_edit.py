"""Surgical XLSX cell edits, never openpyxl-save a user's workbook.

Untouched ZIP members keep exact bytes. No row/column shifts, external refresh,
macros or formula execution. All derived caches are invalidated, not fabricated.
"""
from __future__ import annotations

import io
import math
import zipfile

from lxml import etree
from openpyxl.utils.cell import coordinate_to_tuple
from openpyxl.utils.cell import range_boundaries

from company_agent.services.office.documents import R
from company_agent.services.office.documents import spreadsheet_books
from company_agent.services.office.documents import spreadsheet_package
from company_agent.services.office.documents import spreadsheet_value
from company_agent.services.office.documents import X
from company_agent.services.office.schemas import CellChange
from company_agent.services.office.worker import formula_valid

NS = '{' + X + '}'
PARSER = etree.XMLParser(resolve_entities=False, no_network=True)


class FormulaDependencies:
    def __init__(self, book, specs):
        self.book = book
        self.planned = {(s.sheet, s.cell): (s.value if s.formula else None) for s in specs}
        self.active, self.done = set(), set()
        self.visits = 0

    def visit(self, sheet, cell):
        from openpyxl.formula import Tokenizer
        from openpyxl.utils.cell import get_column_letter

        book, planned, active, done = self.book, self.planned, self.active, self.done
        key = (sheet, cell)
        if key in active:
            raise ValueError('新公式造成循環參照，未修改。')
        if key in done:
            return
        self.visits += 1
        if self.visits > 5000 or len(active) > 100:
            raise ValueError('公式相依範圍過大，未修改。')
        value = planned.get(key, book[sheet][cell].value)
        if key not in planned and book[sheet][cell].data_type != 'f':
            value = None
        if isinstance(value, str):
            formula_valid(value, book[sheet].max_row, book[sheet].max_column)
            active.add(key)
            for token in Tokenizer(value).items:
                if token.type == 'OPERAND' and token.subtype == 'RANGE':
                    a, b, c, d = range_boundaries(token.value)
                    if (c - a + 1) * (d - b + 1) > 5000:
                        raise ValueError('公式相依範圍過大，未修改。')
                    for r in range(b, d + 1):
                        for col in range(a, c + 1):
                            self.visit(sheet, get_column_letter(col) + str(r))
            active.remove(key)
        done.add(key)


def check_formula_dependencies(book, specs):
    dependencies = FormulaDependencies(book, specs)
    for s in specs:
        if s.formula:
            if s.sheet not in book.sheetnames:
                raise ValueError('找不到工作表。')
            dependencies.visit(s.sheet, s.cell)


class XMLParts:
    def __init__(self, archive):
        self.archive, self.trees = archive, {}

    def read(self, name):
        if name not in self.trees:
            self.trees[name] = etree.fromstring(self.archive.read(name), PARSER)
        return self.trees[name]


def edit_spreadsheet(data, changes):
    specs = [CellChange.model_validate(c) for c in changes]
    if not 1 <= len(specs) <= 100:
        raise ValueError('修改範圍超過上限。')
    with spreadsheet_books(data) as (book, _cache):
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            parts = XMLParts(z)
            trees, tree = parts.trees, parts.read

            workbook = tree('xl/workbook.xml')
            protection = workbook.find(NS + 'workbookProtection')
            if protection is not None and any(
                v not in ('0', 'false', '') for v in protection.attrib.values()
            ):
                raise ValueError('不修改受保護工作簿。')
            rels = tree('xl/_rels/workbook.xml.rels')
            targets = {r.get('Id'): r.get('Target') for r in rels}
            sheetparts = {
                s.get('name'): targets[s.get('{' + R + '}id')]
                for s in workbook.iter(NS + 'sheet')
            }
            if len(sheetparts) != len(list(workbook.iter(NS + 'sheet'))):
                raise ValueError('工作表名稱重複。')
            sheetparts = {
                k: ('xl/' + v if not v.startswith('/') else v.lstrip('/'))
                for k, v in sheetparts.items()
            }
            # Shared/array/data-table/spill formula semantics require a different editor.
            for part in sheetparts.values():
                root = tree(part)
                coords = [c.get('r') for c in root.iter(NS + 'c')]
                rows = [r.get('r') for r in root.iter(NS + 'row')]
                if len(set(coords)) != len(coords) or len(set(rows)) != len(rows):
                    raise ValueError('工作表含重複儲存格或列。')
                if root.find(NS + 'sheetProtection') is not None:
                    raise ValueError('不修改含受保護工作表的來源。')
                if any(f.attrib for f in root.iter(NS + 'f')):
                    raise ValueError('不修改含共用／陣列或特殊公式的來源。')
                if root.find(NS + 'extLst') is not None:
                    raise ValueError('來源含擴充工作表功能，需專用編輯器。')
            seen = set()
            modified = set()
            check_formula_dependencies(book, specs)
            for spec in specs:
                key = (spec.sheet, spec.cell)
                if key in seen or spec.sheet not in sheetparts:
                    raise ValueError('重複儲存格或找不到工作表。')
                seen.add(key)
                ws = book[spec.sheet]
                r, c = coordinate_to_tuple(spec.cell)
                if r > ws.max_row or c > ws.max_column:
                    raise ValueError('僅修改既有資料範圍內儲存格。')
                old = spreadsheet_value(ws[spec.cell].value)
                if type(old) is not type(spec.expected) or old != spec.expected:
                    # JSON numbers may represent the same finite integer as float.
                    if not (
                        type(old) in (int, float)
                        and type(spec.expected) in (int, float)
                        and old == spec.expected
                    ):
                        raise ValueError('儲存格原值不符；未產出修改版。')
                root = tree(sheetparts[spec.sheet])
                body = root.find(NS + 'sheetData')
                for merge in root.iter(NS + 'mergeCell'):
                    a, b, d, e = range_boundaries(merge.get('ref'))
                    if a <= c <= d and b <= r <= e and (c, r) != (a, b):
                        raise ValueError('不可修改合併儲存格非左上角位置。')
                row = next((x for x in body if x.get('r') == str(r)), None)
                if row is None:
                    raise ValueError('目標列不存在；不新增或移動列。')
                cell = next((x for x in row if x.get('r') == spec.cell), None)
                if cell is None:
                    cell = etree.Element(NS + 'c', r=spec.cell)
                    pos = next(
                        (
                            i
                            for i, x in enumerate(row)
                            if coordinate_to_tuple(x.get('r'))[1] > c
                        ),
                        len(row),
                    )
                    row.insert(pos, cell)
                if set(cell.attrib) - {'r', 's', 't'} or any(
                    x.tag not in {NS + 'v', NS + 'is', NS + 'f'} for x in cell
                ):
                    raise ValueError('目標儲存格含擴充資料，未修改。')
                if spec.style_from:
                    style = next(
                        (x for x in root.iter(NS + 'c') if x.get('r') == spec.style_from), None,
                    )
                    if style is None:
                        raise ValueError('找不到樣式來源儲存格。')
                    if style.get('s') is None:
                        cell.attrib.pop('s', None)
                    else:
                        cell.set('s', style.get('s'))
                for child in list(cell):
                    cell.remove(child)
                cell.attrib.pop('t', None)
                value = spec.value
                if spec.formula:
                    formula_valid(value, ws.max_row, ws.max_column)
                    etree.SubElement(cell, NS + 'f').text = value[1:]
                elif isinstance(value, str):
                    if len(value) > 16000 or any(
                        ord(ch) < 32 and ch not in '\n\r\t' for ch in value
                    ):
                        raise ValueError('文字含無效控制字元或過長。')
                    cell.set('t', 'inlineStr')
                    t = etree.SubElement(etree.SubElement(cell, NS + 'is'), NS + 't')
                    t.set('{http://www.w3.org/XML/1998/namespace}space', 'preserve')
                    t.text = value
                elif value is not None:
                    if isinstance(value, bool):
                        cell.set('t', 'b')
                        text = '1' if value else '0'
                    else:
                        if not math.isfinite(value):
                            raise ValueError('不接受非有限數值。')
                        text = str(value)
                    etree.SubElement(cell, NS + 'v').text = text
                modified.add(sheetparts[spec.sheet])
            # Any cell may feed any existing formula or chart. Do not leave stale
            # calculated numbers masquerading as current analysis results.
            for part in sheetparts.values():
                for cell in tree(part).iter(NS + 'c'):
                    if cell.find(NS + 'f') is not None:
                        cell.attrib.pop('t', None)
                        for v in cell.findall(NS + 'v'):
                            cell.remove(v)
                        modified.add(part)
            for part in z.namelist():
                if part.startswith('xl/charts/') and part.endswith('.xml'):
                    root = tree(part)
                    for node in list(root.iter()):
                        if etree.QName(node).localname in {'numCache', 'strCache'}:
                            node.getparent().remove(node)
                            modified.add(part)
            calc = workbook.find(NS + 'calcPr')
            if calc is None:
                calc = etree.SubElement(workbook, NS + 'calcPr')
            calc.set('fullCalcOnLoad', '1')
            calc.set('forceFullCalc', '1')
            calc.set('calcMode', 'auto')
            modified.add('xl/workbook.xml')
            # Calc chain indexes can be obsolete after replacing a formula cell.
            removed = {'xl/calcChain.xml'} & set(z.namelist())
            if removed:
                for node in list(rels):
                    if node.get('Type', '').endswith('/calcChain'):
                        rels.remove(node)
                types = tree('[Content_Types].xml')
                for node in list(types):
                    if node.get('PartName') == '/xl/calcChain.xml':
                        types.remove(node)
                modified.update({'[Content_Types].xml', 'xl/_rels/workbook.xml.rels'})
            target = io.BytesIO()
            with zipfile.ZipFile(target, 'w') as out:
                for info in z.infolist():
                    if info.filename in removed:
                        continue
                    out.writestr(
                        info,
                        (
                            etree.tostring(
                                trees[info.filename], xml_declaration=True, encoding='UTF-8',
                            )
                            if info.filename in modified
                            else z.read(info)
                        ),
                    )
            result = target.getvalue()
        spreadsheet_package(result)
        return result
