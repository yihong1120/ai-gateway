"""Reopen real bytes and compare exact cells/text with immutable tool evidence."""
from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path

from langchain_core.tools import ToolException

import company_agent.runtime.assignments as _assignments_imports
import company_agent.services.delivery.native_cells as _native_cells_imports
import company_agent.services.delivery.preservation as _preservation_imports
import company_agent.services.files.nas as _nas_imports
import company_agent.services.office.documents as _documents_imports
from company_agent.services.delivery.schemas import DeliveryPlan
from company_agent.services.delivery.schemas import ExpectedFile
from company_agent.services.files.errors import RecoverableToolError
from company_agent.services.files.operations import worker
from company_agent.services.office.documents import spreadsheet_books


def statistics_for_plan(store, owner, job):
    plan = DeliveryPlan.model_validate(store.delivery_plan(owner, job) or {})
    evidence = store.evidence(owner, job)
    resolved = {}
    for requirement in plan.statistics:
        parameters = requirement.model_dump(exclude={'key'})
        matches = [e for e in evidence if e['parameters'] == parameters]
        if not matches:
            raise ToolException('尚未取得必要統計工具結果：' + requirement.key)
        # A source change within a job must not silently select an older receipt.
        if len({e['source']['sha256'] for e in matches}) != 1:
            raise ToolException('統計來源在本工作中有多個版本，請另開工作確認來源。')
        resolved[requirement.key] = matches[-1]
    return plan, resolved


def require_statistics_before_output(store, owner, job, name):
    plan = DeliveryPlan.model_validate(store.delivery_plan(owner, job) or {})
    if any(f.name == name for f in plan.files):
        return list(statistics_for_plan(store, owner, job)[1].values())
    return []


def verify_evidence_sources(ctx, evidence):

    access = _nas_imports.file_access(ctx.settings, ctx.store, ctx.owner)
    for source in {(e['source']['root'], e['source']['path'], e['source']['sha256']) for e in evidence}:
        _, sha = access.read(source[0], source[1])
        if sha != source[2]:
            raise ValueError('統計來源已變更，未依舊憑據產檔或交付，請重新確認來源。')


def prepare_output(ctx, name, operation=None, parameters=None):

    _assignments_imports.check_output(ctx, name)
    plan = ctx.store.delivery_plan(ctx.owner, ctx.job)
    specification = DeliveryPlan.model_validate(plan or {})
    expected = next((f for f in specification.files if f.name == name), None)
    if expected is not None and expected.base_source is not None:
        if operation != 'edit_spreadsheet':
            raise RecoverableToolError('native_edit_required', '此成果綁定原簿保留，必須用edit_spreadsheet，不能重建工作簿。')
        if {k: (parameters or {}).get(k) for k in ('root', 'path', 'sha256')} != expected.base_source.model_dump():
            raise RecoverableToolError('preservation_source_mismatch', '修改參數必須使用已固定的原簿來源版本，不可換來源或SHA。')
        rules = {(r.sheet, r.cell): r for r in expected.cells if r.statistic is None}
        for change in (parameters or {}).get('changes', []):
            rule = rules.get((change.get('sheet'), change.get('cell')))
            if rule is None:
                continue
            actual = change.get('value')
            if not _native_cells_imports.same_value(actual, rule.expected) or change.get('formula', False) != rule.formula:
                raise RecoverableToolError(
                    'planned_cell_value_mismatch', '修改值或型別不符已固定儲存格規格，尚未產檔；數值12與文字「12」不同。',
                    sheet=rule.sheet, cell=rule.cell, expected_value=rule.expected, expected_type=type(
                        rule.expected,
                    ).__name__,
                )
    if ctx.settings.require_delivery_plan:
        if plan is None:
            raise RecoverableToolError(
                'delivery_plan_required',
                '產檔前須先 plan_delivery 登記使用者明確要求的全部檔案、指定欄位與必要統計；不要先建立猜測草稿。',
            )
        if name not in {f['name'] for f in plan['files']}:
            raise RecoverableToolError('file_not_planned', '此檔名不在已固定交付清單內；請沿用要求的檔名。規格變更須另開工作。')
    try:
        evidence = require_statistics_before_output(ctx.store, ctx.owner, ctx.job, name)
    except ToolException as error:
        raise RecoverableToolError('missing_statistics', str(error)) from error
    verify_evidence_sources(ctx, [*evidence, *ctx.store.evidence(ctx.owner, ctx.job, ctx.required_evidence)])
    _preservation_imports.verify_preservation_sources(ctx, specification)


def resolved_value(rule, evidence):
    if rule.statistic is None:
        return rule.expected
    ref = rule.statistic
    return evidence[ref.key]['result'][ref.metric]


def display_number(value):
    return str(int(value)) if isinstance(value, float) and value.is_integer() else str(value)


def statistic_text_matches(template, value, text):
    """Compare the complete numeric token, not an integer prefix like 30 in 30.5."""
    if type(value) not in (int, float):
        return template.replace('{value}', display_number(value)) in text
    prefix, suffix = template.split('{value}')
    number = r'[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:[eE][+-]?\d+)?'
    pattern = re.escape(prefix) + '(' + number + r')(?!\d|[.,]\d|[eE][+-]?\d)' + re.escape(suffix)
    return any(Decimal(m[1].replace(',', '')) == Decimal(str(value)) for m in re.finditer(pattern, text))


def validate_delivery(store, owner, job):
    plan, evidence = statistics_for_plan(store, owner, job)
    files = store.deliverable_files(owner, job)
    names = [f['name'].casefold() for f in files]
    if len(names) != len(set(names)):
        raise ToolException('存在同名有效成果，請明確取代舊版或另開工作，不可默默漏交。')
    by_name = {f['name']: f for f in files}
    missing = [f.name for f in plan.files if f.name not in by_name]
    if missing:
        raise ToolException('缺少必要交付檔案：' + '、'.join(missing))
    receipts = []
    rules = {f.name: f for f in plan.files}
    for file in files:
        name, sha, data = store.read_artifact(owner, file['id'], job)
        expected = rules.get(name)
        if expected is not None and expected.base_source is not None:
            _preservation_imports.verify_native_receipt(store, owner, job, file, expected.base_source)
        checks = worker(
            'validate_delivery_file', data=data, name=name,
            specification=expected.model_dump() if expected else None, evidence=evidence,
        )
        receipt = {
            'status': 'passed', 'checks': checks,
            'evidence_ids': [e['evidence_id'] for e in evidence.values()],
            'scope': '明確規格與可解析內容；不代表版面、語意完整性或人工核准。',
        }
        store.save_validation(owner, job, file['id'], sha, receipt)
        receipts.append({'artifact_id': file['id'], 'sha256': sha, **receipt})
    return receipts


def validate_file(data, name, specification, evidence):

    expected = ExpectedFile.model_validate(specification) if specification else None
    suffix = Path(name).suffix.lower()
    checks = []
    if suffix == '.xlsx':
        # The normal parser's package and resource limits apply as well.
        with spreadsheet_books(data) as (book, _):
            if expected and expected.sheet_names and book.sheetnames != expected.sheet_names:
                raise ToolException('工作表名稱或順序不符交付規格：' + name)
            for rule in expected.cells if expected else []:
                if rule.sheet not in book.sheetnames:
                    raise ToolException('缺少必要工作表：' + rule.sheet)
                cell = book[rule.sheet][rule.cell]
                value = resolved_value(rule, evidence)
                actual = cell.value
                # Boolean is not a numeric value; formulas/caches cannot prove a statistic.
                equal = actual == value and (type(actual) is bool) == (type(value) is bool)
                if not equal or (cell.data_type == 'f') != rule.formula:
                    raise ToolException(f'儲存格驗收失敗：{name} {rule.sheet}!{rule.cell}')
                checks.append({'sheet': rule.sheet, 'cell': rule.cell, 'expected': value})
            if expected and expected.texts:
                raise ToolException('XLSX 文字規格請使用精確儲存格檢查。')
    elif suffix in {'.docx', '.pptx', '.txt', '.md', '.csv', '.pdf'}:

        blocks = _documents_imports.extract(data, suffix)
        if expected and (expected.cells or expected.sheet_names):
            raise ToolException('非 XLSX 不接受工作表／儲存格規格。')
        for rule in expected.texts if expected else []:
            text = rule.text
            if rule.statistic is not None:
                ref = rule.statistic
                value = evidence[ref.key]['result'][ref.metric]
                matched = any(statistic_text_matches(text, value, block.get('text', '')) for block in blocks)
                text = text.replace('{value}', display_number(value))
            else:
                text = text.replace('{{value}}', '{value}')
                matched = any(text in block.get('text', '') for block in blocks)
            if not matched:
                raise ToolException('必要文字或統計數值驗收失敗：' + name + '：' + text)
            checks.append({'text': text})
    elif expected and (expected.cells or expected.texts or expected.sheet_names):
        raise ToolException('此格式尚未提供指定的內容驗收：' + name)
    return checks
