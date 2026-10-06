"""Shared validation for both explicitly confirmed and opt-in scheduled writes."""
from __future__ import annotations

import hashlib
import re

from .netask import csv_minutes
from .netask import import_line
from official_document_workflow.infrastructure.eip import calendar_csv

ASCII_UPPER = str.maketrans('abcdefghijklmnopqrstuvwxyz', 'ABCDEFGHIJKLMNOPQRSTUVWXYZ')
TRANSPORT_FIELDS = (
    ('Categories', 21), ('Priority', 20),
    *((f"Custom Field{i}", 22+i) for i in range(1, 6)),
)


def import_aliases(journal, user_id, line):
    """Canonical and historical newline/plus encodings of one import."""
    fields = line.split(',')
    for index in (0, 13, 18, 19, 21):
        fields[index] = fields[index].replace('%2B', '+')
    versions = dict.fromkeys((line, ','.join(fields)))
    return [
        journal.key(user_id, {'import': version + suffix})
        for version in versions for suffix in ('', '\n')
    ]


def verified_events(rows, staged, user_id, journal):
    if len(staged) != len(rows):
        raise ValueError('EIP 解析筆數與 CSV 不符，已停止匯入。')
    candidates = []
    interval_aliases = set()
    for row, remote in zip(rows, staged):
        if len(remote) != 29 or [str(remote[i]) for i in (0, 1, 3)] != [row[k] for k in ('Subject', 'Start Date', 'End Date')]:
            raise ValueError('EIP 解析主旨或日期與 CSV 不符，已停止匯入。')
        expected_description = row['Description'].replace('\r\n', '\n')
        actual_description = str(remote[19]).replace('%0D%0A', '\n').replace('\r\n', '\n')
        # Deployed CM_Import uppercases ASCII letters in Description, including
        # OCR LaTeX commands and case-sensitive filenames. Read-only probes
        # confirmed this also happens after percent-encoding the upload. Accept
        # ONLY that exact deterministic transform, then write the original CSV
        # text. Never persist the uppercased preview or relax detail readback.
        preview_upper = expected_description.translate(ASCII_UPPER)
        if remote[5] != row['All day event'] or actual_description not in (expected_description, preview_upper):
            raise ValueError('EIP 解析全天設定或說明與 CSV 不符，已停止匯入。')
        if actual_description != expected_description:
            remote = list(remote)
            remote[19] = expected_description.replace('\n', '\r\n')
        if row['All day event'] == '假':
            for prefix, idx in (('Start', 2), ('End', 4)):
                expected = csv_minutes(calendar_csv.canonical_time(row, prefix), allow_end_of_day=idx == 4)
                if csv_minutes(str(remote[idx]), allow_end_of_day=idx == 4) != expected:
                    raise ValueError('EIP 解析時間與 CSV 不符，已停止匯入。')
            if row['Start Date'] == row['End Date'] and int(csv_minutes(remote[4], allow_end_of_day=True)) < int(csv_minutes(remote[2])):
                raise ValueError('行程結束時間早於開始時間，請先修正 CSV。')
        for key, idx in TRANSPORT_FIELDS:
            if str(remote[idx]) != row[key]:
                raise ValueError('EIP 解析分類或自訂欄位與 CSV 不符，已停止匯入。')
        line = import_line(remote, user_id)
        original = row.get('_source_interval')
        if original:
            # A new daily transport must not evade an old full-interval receipt,
            # including unknown writes and the legacy newline/plus encodings.
            prior = list(remote)
            for key_name, idx in (('Start Date', 1), ('End Date', 3), ('Description', 19)):
                prior[idx] = original[key_name]
            prior[19] = prior[19].replace('\r\n', '\n').replace('\n', '\r\n')
            prior[2] = calendar_csv.canonical_time(original, 'Start')
            prior[4] = calendar_csv.canonical_time(original, 'End')
            old_line = import_line(prior, user_id)
            interval_aliases.update(import_aliases(journal, user_id, old_line))
        candidates.append({'aliases': import_aliases(journal, user_id, line), 'line': line, 'row': row})
    states = journal.statuses([*interval_aliases, *(key for event in candidates for key in event['aliases'])])
    if interval_aliases.intersection(states):
        raise ValueError('既有跨日行程已有匯入紀錄，須核對原行程後才能分段；未重送或新增重複行程。')
    events, seen = [], set()
    for event in candidates:
        aliases = event['aliases']
        key = next((alias for alias in aliases if alias in states), aliases[0])
        # Preserve the original newline receipt preference. All aliases are
        # checked again atomically by reserve_event before any external write.
        if aliases[1] in states:
            key = aliases[1]
        if key not in seen:
            events.append({'key': key, **event})
            seen.add(key)
    return events


async def write_events(adapter, events, journal, allowed=object):
    created = skipped = 0
    if not allowed():
        raise ValueError('自動處理已暫停或綁定變更，未繼續寫入。')
    if any(e.get('row', {}).get('_eip_origin') and not e.get('source_batch') for e in events):
        raise ValueError('尚未完成原始來源重跑核對，未匯入。')
    for batch in {e['source_batch'] for e in events if e.get('source_batch')}:
        journal.reserve_source_batch(*batch)
    for event in events:
        if not allowed():
            raise ValueError('自動處理已暫停或綁定變更，未繼續寫入。')
        state = journal.reserve_event(event['key'], event.get('aliases', ()))
        if state == 'done':
            skipped += 1
            continue
        if state != 'reserved':
            raise ValueError('先前匯入結果不明，已停止重送；請到 EIP 核對。')
        receipt = await adapter.commit(event['line'])
        journal.done(event['key'], aliases=event.get('aliases', ()), receipt=receipt)
        # Kept only in the in-process event object for the immediately
        # following attachment stage; do not change the public import result.
        event['receipt'] = receipt
        created += 1
    return {'created': created, 'skipped': skipped}


async def write_project_assignments(adapter, events, journal, allowed=object):
    """Assign a live-matched EIP project to freshly created events only.

    Existing imports are never retrofitted by the hourly worker.  Each edit is
    separately journaled before the network request, so an unknown outcome is
    stopped for manual review instead of being replayed.
    """
    assigned = private = skipped = 0
    decisions = []
    for event in events:
        decision = event.get('project_match') or {'status': 'no_source_project'}
        status = decision.get('status')
        receipt = event.get('receipt')
        if status != 'matched':
            private += 1
            decisions.append({'status': status, 'document_project': decision.get('document_project')})
            continue
        if not receipt:
            # The calendar import had been completed in a prior run.  No
            # background task may change its project classification later.
            skipped += 1
            decisions.append({
                'status': 'existing_event_not_changed',
                'document_project': decision.get('document_project'),
            })
            continue
        if not allowed():
            raise ValueError('自動處理已暫停或綁定變更，未繼續設定行事曆專案。')
        event_id = receipt.get('event_id')
        project = decision.get('project') or {}
        project_id = project.get('id')
        members = project.get('members')
        if not members or any(not re.fullmatch(r'Ru\d+', m) for m in members):
            raise ValueError('EIP 專案成員資料不完整，未設定專案。')
        sharing = sorted({*members, f'Ru{adapter.user_id}'})
        key = journal.key(
            adapter.user_id, {
                'calendar_project': {
                    'event_id': event_id, 'project_id': project_id,
                },
            },
        )
        state = journal.reserve_event(key)
        if state == 'done':
            skipped += 1
            event['project_assignment_receipt'] = {'event_id': event_id, 'project_id': project_id, 'sharing': sharing}
            decisions.append({'status': 'assigned', 'project': project, 'reused_receipt': True})
            continue
        if state != 'reserved':
            raise ValueError('行事曆專案設定結果不明，請到 EIP 核對；未重送。')
        receipt = await adapter.assign_calendar_project(event_id, event['line'], project_id, sharing=sharing)
        journal.done(key, receipt=receipt)
        event['project_assignment_receipt'] = receipt
        assigned += 1
        decisions.append({'status': 'assigned', 'project': project})
    return {
        'assigned_events': assigned, 'private_events': private,
        'skipped_events': skipped, 'events': decisions,
    }


async def write_attachments(adapter, events, files, journal, allowed=object):
    """Attach renamed source PDFs only after calendar create/detail verification.

    A per-event + content-hash journal means an uncertain upload/update is
    never replayed.  Previously completed imports are deliberately not
    backfilled by the hourly worker.
    """

    if not files:
        return {'attached_events': 0, 'skipped_events': 0}
    digests = [(name, hashlib.sha256(payload).hexdigest()) for name, payload in files]
    attached = skipped = 0
    for event in events:
        receipt = event.get('receipt')
        if not receipt:
            # An existing event was skipped by the import journal; do not
            # retrofit it during a scheduler run without a separate explicit
            # operator action and a fresh event-detail verification.
            skipped += 1
            continue
        if not allowed():
            raise ValueError('自動處理已暫停或綁定變更，未繼續夾帶附件。')
        event_id = receipt.get('event_id')
        key = journal.key(
            adapter.user_id, {
                'calendar_attachment': {
                    'event_id': event_id, 'files': digests,
                },
            },
        )
        state = journal.reserve_event(key)
        if state == 'done':
            skipped += 1
            continue
        if state != 'reserved':
            raise ValueError('行事曆附件上傳結果不明，請到 EIP 核對；未重送。')
        project_receipt = event.get('project_assignment_receipt') or {}
        if project_receipt.get('project_id'):
            attachment_receipt = await adapter.attach_calendar_files(
                event_id, event['line'], files, project_id=project_receipt['project_id'], sharing=project_receipt.get(
                    'sharing',
                ),
            )
        else:
            attachment_receipt = await adapter.attach_calendar_files(event_id, event['line'], files)
        journal.done(key, receipt={'event_id': event_id, 'detail_verified': True})
        event['attachment_receipt'] = attachment_receipt
        attached += 1
    return {'attached_events': attached, 'skipped_events': skipped}
