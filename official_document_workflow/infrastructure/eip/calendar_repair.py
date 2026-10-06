"""Shared verified edit builder for calendar project and attachment updates."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date

from . import calendar_read
from . import calendar_verification
from official_document_workflow.infrastructure.eip import netask


def fingerprint(detail):
    return hashlib.sha256(json.dumps(detail, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def repair_parameters(detail, line, user_id, event_id, expected_fingerprint, *, project_id='', sharing=None):
    fields = calendar_verification.validate_wire(line, user_id)
    if fingerprint(detail) != expected_fingerprint:
        raise ValueError('行程已變更，停止修復；請重新核對。')
    if len(detail) != 2 or detail[1] not in ([0], ['0']) or len(detail[0]) < 43:
        raise ValueError('僅可修復無附件與回報紀錄的私人匯入行程。')
    row = detail[0]
    if (
        row[0] != event_id or row[2] != user_id or row[4] != project_id or any(row[i] for i in (6, 7))
        or (row[15].strip() not in ('', f'Ru{user_id}') if sharing is None else set(row[15].split(',')) != set(sharing)) or row[22:24] != ['0', '0']
        or row[33] not in ('', '0') or row[31] not in ('', '0') or row[32] not in ('', '0000')
        or calendar_read.unescape(row[8]) != calendar_read.unescape(fields[0])
    ):
        raise ValueError('行程不屬於可修復的本人私人匯入資料，未變更。')
    year, month, day = fields[1].split('-')
    if fields[1] != fields[3]:
        raise ValueError('此修復入口僅支援同日行程。')
    match = re.fullmatch(r'new Date\((\d{4}),\s*(\d+),\s*(\d+),\s*0,\s*0,\s*0\)', str(row[9]))
    source_day = date(int(match[1]), int(match[2])+1, int(match[3])).isoformat() if match else ''
    if source_day != fields[1] or row[10] != fields[5] or (fields[5] == '0' and row[11] != fields[2]):
        raise ValueError('修復不得變更原行程開始日期、時間或全天設定。')
    params = {
        'ap_name': 'cm', '__tag': 'edit_event', 'event_id': event_id, 'type': '0',
        'proj_id': project_id, 'task_id': '', 'subject': fields[0],
        'all_day': fields[5], 'dateY': year, 'dateM': str(int(month)), 'dateD': str(int(day)),
        'start_time': fields[2], 'end_time': fields[4], 'importance': row[13],
        'content': fields[19], 'pub': ','.join(sharing) if sharing is not None else f'Ru{user_id}',
        'repeat_op1': '0', 'repeat_op2': '0', 'repeat_enddate': fields[1],
        'status': netask.js_escape(calendar_read.unescape(row[16])), 'complete': row[17] or '0',
        'cword': '', 'isMailtoShared': '', 'mfrom': user_id, 'mto': '',
        'public': '0', 'remind_now': '0', 'remind_alert': '0', 'remind_who': '0000', 'update_history': '0',
    }
    # Official new_event.js omits these fields for all-day updates. Sending
    # empty strings instead can trigger a server-side error on legacy EIP.
    if fields[5] == '1':
        params.pop('start_time')
        params.pop('end_time')
    if project_id:
        params['original_project_id'] = project_id
    # EIP persists custom-field input literally, unlike subject/content.
    # Preserve its existing representation: raw Chinese from CSV must not
    # expand into %uXXXX strings (long unit names then cause HTTP 500).
    # Already escaped values from an earlier editor remain unchanged too.
    # The HTTP form transport encodes these values; no second JS escape here.
    for i, value in enumerate(row[37:42]):
        params[f'user_def_value[{i}]'] = value
    return params
