"""Bind explicit native-workbook preservation to server-read source facts."""
from __future__ import annotations

import json
import re
from pathlib import Path

import company_agent.services.delivery.native_cells as _native_cells_imports
import company_agent.services.files.nas as _nas_imports
from company_agent.services.delivery.schemas import OriginalWorkbook
from company_agent.services.files.errors import RecoverableToolError
from company_agent.services.files.operations import worker


def native_targets(instruction, files):
    targets = set()
    for file in files:
        if Path(file.name).suffix.lower() != '.xlsx':
            continue
        # Bind the nearest output verb after the explicit native-edit tool.
        between = r'(?:(?!(?:另存|產出|輸出|create_spreadsheet))[^。\n]){0,120}'
        pattern = r'edit_spreadsheet' + between + \
            r'(?:另存|產出|輸出)(?:為|成)?\s*[`「『\"]?' + re.escape(file.name) + r'(?![A-Za-z0-9_.-])'
        if re.search(pattern, instruction):
            targets.add(file.name)
    xlsx = [f.name for f in files if Path(f.name).suffix.lower() == '.xlsx']
    if len(xlsx) == 1 and re.search(r'(?:保留|修改|沿用)原(?:簿|工作簿)', instruction):
        targets.add(xlsx[0])
    return targets


def bind_preservation(ctx, instruction, plan, sources):

    targets = native_targets(instruction, plan.files)
    if not targets:
        if any(f.base_source is not None for f in plan.files):
            raise RecoverableToolError('preservation_not_requested', '新建報表不綁定原簿；只有明確原簿保留修改才使用base_source。')
        return plan
    access = _nas_imports.file_access(ctx.settings, ctx.store, ctx.owner)
    by_source = {
        (s['root'], s['path']): s for s in sources
        if all(k in s for k in ('root', 'path', 'sha256')) and Path(s['path']).suffix.lower() == '.xlsx'
    }
    for file in plan.files:
        if file.base_source is not None and file.name not in targets:
            raise RecoverableToolError(
                'preservation_not_requested',
                '新建報表不綁定原簿；只有明確原簿保留修改才使用base_source。', file=file.name,
            )
        if file.name not in targets:
            continue
        reference = file.base_source
        if reference is None:
            if len(by_source) != 1:
                raise RecoverableToolError(
                    'preservation_source_required',
                    '先實際讀取原簿；有多個已讀來源時，以base_source明確指定原簿root/path/sha256。', file=file.name,
                )
            reference = OriginalWorkbook.model_validate(
                {k: next(iter(by_source.values()))[k] for k in ('root', 'path', 'sha256')},
            )
        prior = by_source.get((reference.root, reference.path))
        if prior is None or prior['sha256'] != reference.sha256:
            raise RecoverableToolError('preservation_source_unread', '原簿綁定必須符合本工作真正已讀來源版本，不能猜測路徑或SHA。', file=file.name)
        data, sha = access.read(reference.root, reference.path)
        if sha != reference.sha256:
            raise ValueError('原簿來源已變更，請重新確認並另開工作。')
        actual = [s['name'] for s in worker('read_spreadsheet', data=data, row_limit=1)['sheets']]
        if (file.sheet_names and file.sheet_names != actual) or any(cell.sheet not in actual for cell in file.cells):
            raise RecoverableToolError(
                'preservation_plan_mismatch', '原簿保留規格含不存在、缺少或順序不符的工作表，尚未鎖定計畫。',
                file=file.name, actual_sheet_names=actual, planned_sheet_names=file.sheet_names,
                next_action='修正後重送plan_delivery；sheet_names留空時由伺服器依實讀來源補齊，不新增別名。',
            )
        if len(targets) == 1:
            _native_cells_imports.bind_literal_cells(instruction, file, actual)
        file.base_source = reference
        file.sheet_names = actual
    return plan


def verify_preservation_sources(ctx, plan):

    access = _nas_imports.file_access(ctx.settings, ctx.store, ctx.owner)
    for source in {(f.base_source.root, f.base_source.path, f.base_source.sha256) for f in plan.files if f.base_source is not None}:
        _, sha = access.read(source[0], source[1])
        if sha != source[2]:
            raise ValueError('原簿來源已變更，未依舊版本交付；請重新確認並另開工作。')


def verify_native_receipt(store, owner, job, file, source):
    with store.db() as db:
        rows = db.execute(
            "SELECT result FROM tool_receipts WHERE owner=%s AND job=%s AND name='edit_spreadsheet' AND result IS NOT NULL",
            (owner, job),
        ).fetchall()
    for (payload,) in rows:
        result = json.loads(payload)
        if result.get('id') == file['id'] and result.get('sha256') == file['sha256'] and result.get('source') == source.model_dump():
            return
    raise RecoverableToolError(
        'native_edit_receipt_required',
        '原簿保留成果必須有相同來源版本的edit_spreadsheet實際執行憑據，不能以重建工作簿代替。', file=file['name'],
    )
