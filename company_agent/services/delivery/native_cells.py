"""Literal native-edit requirements from a narrow, explicit user grammar."""
from __future__ import annotations

import math
import re

from company_agent.services.delivery.schemas import CellExpectation
from company_agent.services.files.errors import RecoverableToolError


_NUMBER = r'[+-]?(?:\d{1,30}(?:\.\d{1,30})?)(?:[eE][+-]?\d{1,3})?'
_EDIT = re.compile(
    r'「(?P<sheet>[^」\n]{1,31})」\s*(?P<cell>[A-Z]{1,2}[1-9][0-9]{0,3})\s*'
    r'(?:(?:由|從)\s*' + _NUMBER + r'\s*)?改(?:成|為)?\s*'
    r'(?P<value>「[^」\n]{0,5000}」|『[^』\n]{0,5000}』|"[^"\n]{0,5000}"|'
    + _NUMBER + r'(?![A-Za-z0-9_.]))',
)


def literal_changes(instruction):
    """Quoted replacements are text; unquoted finite numeric literals are numbers."""
    result = {}
    for match in _EDIT.finditer(instruction):
        if re.search(r'(?:不要|不必|禁止|勿|無需)(?:把|將)?\s*$', instruction[max(0, match.start()-15):match.start()]):
            continue
        token = match['value']
        if token[0] in '「『"':
            value = token[1:-1]
        else:
            value = float(token) if any(c in token for c in '.eE') else int(token)
            if isinstance(value, float) and not math.isfinite(value):
                raise RecoverableToolError('native_numeric_literal_limit', '明確數值超出有限數值範圍，須先確認要求。')
        key = (match['sheet'], match['cell'])
        if key in result and not same_value(result[key], value):
            raise RecoverableToolError(
                'conflicting_native_cell_request',
                '同一儲存格有互相衝突的明確修改值，須先確認要求。', sheet=key[0], cell=key[1],
            )
        result[key] = value
    return result


def same_value(actual, expected):
    return actual == expected and (type(actual) is bool) == (type(expected) is bool)


def bind_literal_cells(instruction, file, actual_sheets):
    existing = {(r.sheet, r.cell): r for r in file.cells}
    for (sheet, cell), value in literal_changes(instruction).items():
        if sheet not in actual_sheets:
            raise RecoverableToolError(
                'requested_native_sheet_missing',
                '明確修改要求的工作表不在來源內，不能猜別名或改寫使用者要求。', sheet=sheet, available_sheets=actual_sheets,
            )
        rule = existing.get((sheet, cell))
        if rule is not None:
            if rule.statistic is not None or rule.formula or not same_value(rule.expected, value):
                raise RecoverableToolError(
                    'native_cell_contract_mismatch', '原簿計畫須符合使用者明確修改值與型別；未鎖定，請修正後重送。',
                    sheet=sheet, cell=cell, expected_value=value, expected_type=type(value).__name__,
                )
        else:
            if len(file.cells) >= 100:
                raise RecoverableToolError('native_cell_limit', '明確修改格超出100格驗收上限，請拆分工作。')
            file.cells.append(CellExpectation(sheet=sheet, cell=cell, expected=value))
