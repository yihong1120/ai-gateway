"""Check literal output-cell assignments, without interpreting general prose."""
from __future__ import annotations

import re

from langchain_core.tools import ToolException


def explicit_outputs(instruction):
    """Read names in output clauses; a new source directive ends that scope."""
    names = set()
    extension = r'(?:docx|pptx|xlsx|txt|md|csv|dxf)'
    tokens = re.compile(
        r'(?P<verb>另交|補交|另製|製作|輸出|交付|產出|另存|建立|參考|來源|讀取|讀回|讀|依據|根據|比對|比較)'
        + r'|`(?P<quoted>[^`/\\\n]+?\.' + extension + r')`'
        + r'|「(?P<corner>[^」/\\\n]+?\.' + extension + r')」'
        + r'|『(?P<double_corner>[^』/\\\n]+?\.' + extension + r')』'
        + r'|(?P<quoted_text>「[^」\n]*」|『[^』\n]*』|`[^`\n]*`)'
        + r'|(?P<name>[A-Za-z0-9_][A-Za-z0-9_.-]*?\.' + extension + r')(?![A-Za-z0-9_.-])',
        re.IGNORECASE,
    )
    for clause in re.split(r'[。\n]', instruction):
        output = False
        for match in tokens.finditer(clause):
            if match['verb']:
                output = match['verb'] in {'另交', '補交', '另製', '製作', '輸出', '交付', '產出', '另存', '建立'}
                if re.search(r'(?:不要|不必|無需|禁止|勿|不)$', clause[:match.start()]):
                    output = False
            elif output and not match['quoted_text']:
                names.add(match['name'] or match['quoted'] or match['corner'] or match['double_corner'])
    return names


def check_explicit_cells(instruction, plan):
    """Only a named output clause with an exact 'cell=value' grammar is binding.

    This narrows model-authored contracts; it never creates files, chooses source
    scopes or infers permissions. Ambiguous/unrecognised prose stays with review.
    """
    missing = explicit_outputs(instruction) - {f.name for f in plan.files}
    if missing:
        raise ToolException(
            '交付規格漏列使用者明確要求的檔名：' + '、'.join(sorted(missing))
            + '。請在產檔前登記完整清單，不能只登記目前角色的一份。',
        )
    if '{value}' not in instruction and any(r.statistic is None and '{{value}}' in r.text for f in plan.files for r in f.texts):
        raise ToolException('使用者要求真實數值時，須以statistic的key／metric引用工具憑據；不可改登記字面預留符號來迴避統計驗收。')
    for file in plan.files:
        if not file.name.lower().endswith('.xlsx'):
            continue
        clause = re.search(
            r'(?:交付|產出|另存|建立)[^。\n]{0,100}?' + re.escape(file.name)
            + r'(?P<spec>[^。\n]{0,1200})', instruction,
        )
        if not clause:
            continue
        specification = clause['spec']
        sheet = re.search(r'「([^」]{1,31})」(?:工作)?表', specification)
        if sheet is None:
            continue
        assignments = re.findall(
            r'(?<![A-Z0-9])([A-Z]{1,2}[1-9][0-9]{0,3})\s*[=＝]\s*([^/／、，,；;。\s）)]+)',
            specification,
        )
        rules = {(r.sheet, r.cell): r for r in file.cells}
        assigned = {literal for _, literal in assignments}
        # A bounded, explicit row-aligned range requirement is also a cell contract.
        # Never infer metrics from an unrelated narrative or a source range.
        metric_labels = {
            '合計': 'sum', '總和': 'sum', '筆數': 'count',
            '最小': 'min', '最大': 'max', '平均': 'mean',
        }
        ranges = re.finditer(
            r'(?<![A-Z0-9])([A-Z]{1,2})([1-9][0-9]{0,3}):\1([1-9][0-9]{0,3})'
            r'\s*依序填(?:入)?對應的(?:真實)?(?:工具|統計|計算)數值', specification,
        )
        for aligned in ranges:
            start, end = int(aligned[2]), int(aligned[3])
            if end < start or end - start > 99:
                continue
            for row in range(start, end + 1):
                labels = [
                    (cell, literal, metric_labels[literal]) for cell, literal in assignments
                    if literal in metric_labels and re.fullmatch(r'[A-Z]{1,2}' + str(row), cell)
                ]
                if len(labels) != 1:
                    continue  # Ambiguous row labels remain a reviewer responsibility.
                label_cell, label, metric = labels[0]
                value_cell = aligned[1] + str(row)
                if value_cell == label_cell:
                    continue
                label_rule = rules.get((sheet[1], label_cell))
                value_rule = rules.get((sheet[1], value_cell))
                if label_rule is None or label_rule.statistic is not None or label_rule.expected != label:
                    raise ToolException(f'{file.name} {label_cell}須為指定統計標籤「{label}」。')
                if value_rule is None or value_rule.statistic is None or value_rule.statistic.metric != metric:
                    raise ToolException(f'{file.name} {value_cell}須依同列「{label}」引用真實統計{metric}；請補齊或修正後重送計畫。')
        for cell, literal in assignments:
            rule = rules.get((sheet[1], cell))
            if rule is None:
                raise ToolException(f'交付規格漏列使用者明確指定的 {file.name} {sheet[1]}!{cell}={literal}；請在產檔前補正。')
            quoted = len(literal) >= 2 and (literal[0], literal[-1]) in {
                ('`', '`'), ('「', '」'), ('『', '』'), ('"', '"'), ("'", "'"),
            }
            metric = {
                '合計值': 'sum', '總和值': 'sum', '筆數值': 'count',
                '平均值': 'mean', '最小值': 'min', '最大值': 'max',
            }.get(literal)
            text = literal[1:-1] if quoted else literal if literal + '值' in assigned else None
            if metric is not None:
                if rule.statistic is None or rule.statistic.metric != metric:
                    raise ToolException(f'{file.name} {cell}={literal} 必須引用真實統計 {metric}，不能當文字或對應到標籤格。')
            elif text is not None:
                # Quoted text and paired label/value columns are unambiguous.
                # Terms such as 空白 or an unpaired 總額 are not coerced to text.
                if rule.statistic is not None or rule.expected != text:
                    raise ToolException(f'{file.name} {cell} 必須為指定文字「{text}」，不能綁定統計數值。')
