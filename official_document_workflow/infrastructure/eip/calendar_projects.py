"""Safe dynamic EIP project selection for imported official-document events.

The model provides only a source-verified engineering name in the calendar
description.  This module reads the account's *current* EIP project choices,
does a deliberately narrow exact comparison, then changes only ``proj_id`` on
the freshly imported event and reads it back. EIP expands project sharing, so
the caller must opt in and supply the freshly read project members explicitly.
It never accepts an LLM project ID, fuzzy guesses, task assignment or replay.
"""
from __future__ import annotations

import re
import unicodedata

from .calendar_read import unescape
from .calendar_repair import fingerprint
from .calendar_repair import repair_parameters
from .calendar_verification import validate_wire
from .calendar_verification import verify_detail
from .netask import ProjectRecord

_PROJECT_LINE = re.compile(r'^工程名稱\s*[:：]\s*([^\r\n]+?)\s*$', re.MULTILINE)
_PREFIX = re.compile(r'^\d{1,3}\s*[-－]\s*')


def project_name_from_description(description: str | None):
    """Read the one source-verified project heading from generated CSV text."""
    if description is None or len(description) > 20_000:
        return None
    names = [
        item.strip() for item in _PROJECT_LINE.findall(description)
        if item.strip() and not any(ord(char) < 32 for char in item)
    ]
    if len(names) != 1:
        return None
    return names[0]


def normalize_project_name(name: str | None) -> str:
    """Normalize display-only differences, never substantive project wording."""
    if name is None:
        return ''
    normalized = unicodedata.normalize('NFKC', name).replace('\u200b', '').replace('\ufeff', '')
    normalized = _PREFIX.sub('', normalized.strip())
    return re.sub(r'\s+', '', normalized)


def match_project(document_project, projects, index=None):
    """Return a unique current EIP project or an explainable private fallback."""
    target = normalize_project_name(document_project)
    if not target:
        return {'status': 'no_source_project', 'document_project': document_project or None}
    if index is None:
        index = {}
        for item in projects:
            index.setdefault(normalize_project_name(item.get('name')), []).append(item)
    matches = index.get(target, [])
    if len(matches) == 1:
        return {
            'status': 'matched', 'document_project': document_project,
            'project': dict(matches[0]),
        }
    if len(matches) > 1:
        return {
            'status': 'ambiguous', 'document_project': document_project,
            'candidates': [item['name'] for item in matches],
        }
    return {'status': 'unmatched', 'document_project': document_project}


def prepare_project_matches(events: list[dict], projects: list[ProjectRecord]):
    """Attach the dynamic matching decision to every staged import event."""
    index = {}
    for project in projects:
        index.setdefault(normalize_project_name(project.get('name')), []).append(project)
    for event in events:
        row = event.get('row') or {}
        decision = match_project(project_name_from_description(row.get('Description')), projects, index)
        event['project_match'] = decision
    return events


def project_assignment_parameters(detail, line, user_id, event_id, project_id, *, sharing=None):
    """Build an edit that preserves every imported field except project ID."""
    if not project_id.isdecimal() or int(project_id) <= 0:
        raise ValueError('EIP 專案識別碼格式不符，未更新行程。')
    verify_detail(detail, validate_wire(line, user_id), user_id, event_id)
    params = repair_parameters(detail, line, user_id, event_id, fingerprint(detail))
    params['proj_id'] = project_id
    params['task_id'] = ''  # Keep the source-verified custom subject; never invent a task.
    if sharing is not None:
        if not sharing or any(not re.fullmatch(r'Ru\d+', member) for member in sharing):
            raise ValueError('EIP 專案成員格式不符，未更新行程。')
        params['pub'] = ','.join(sharing)
    return params


def verify_project_assignment_detail(before, after, line, user_id, event_id, project_id, *, sharing=None):
    """Require the expected project and prove no other user-visible data moved."""
    fields = validate_wire(line, user_id)
    verify_detail(before, fields, user_id, event_id)
    if len(after) != 2 or after[1] not in ([0], ['0']) or len(after[0]) < 43:
        raise ValueError('EIP 專案更新後詳細資料格式不符，請人工核對；未重送。')
    old, new = before[0], after[0]
    if (
        new[0] != event_id or new[2] != user_id or str(new[4]) != project_id
        or any(new[index] for index in (6, 7))
    ):
        raise ValueError('EIP 專案更新回讀不符，請人工核對；未重送。')
    # All calendar content, ownership/privacy, reminder, report and custom
    # fields must remain unchanged.  This is stricter than simply checking the
    # selected project ID because this worker must not alter an existing event.
    verify_detail(after, fields, user_id, event_id, project_id=project_id, sharing=sharing)
    protected = (
        0, 2, 6, 7, 8, 9, 10, 13, 14, 16, 17,
        22, 23, 37, 38, 39, 40, 41,
    )
    for index in protected:
        if unescape(str(old[index])).replace('\r\n', '\n') != unescape(str(new[index])).replace('\r\n', '\n'):
            raise ValueError('EIP 專案更新意外變更行程內容，請人工核對；未重送。')
    if any(new[i] not in ('', '0') for i in (31, 33)) or new[32] not in ('', '0000'):
        raise ValueError('EIP 專案更新改變提醒設定，請人工核對；未重送。')
    return {
        'event_id': event_id, 'project_id': project_id, 'detail_verified': True,
        'sharing': sharing,
    }


async def assign_project(adapter, event_id, line, project_id, *, sharing=None):
    """Perform one verified project-only update after a durable reservation."""
    before = await adapter.calendar_detail(event_id)
    params = project_assignment_parameters(before, line, adapter.user_id, event_id, project_id, sharing=sharing)
    await adapter.rpc('Update', **params)
    after = await adapter.calendar_detail(event_id)
    return verify_project_assignment_detail(before, after, line, adapter.user_id, event_id, project_id, sharing=sharing)
