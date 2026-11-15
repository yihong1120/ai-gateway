"""Keep a proven manual schedule; back up and remove only managed AI copies.

This is a workflow operation, not a model tool or a general calendar delete API.
Every deletion is owned, backed up, reserved once, and verified without replay.
"""
from __future__ import annotations

import base64
import hashlib
import re
from datetime import datetime

from examples.official_document_workflow.domain.calendar import deduplication as rules
from examples.official_document_workflow.infrastructure.storage.calendar_managed import (
    digest,
    normalized_detail,
)
from examples.shared.runtime.io import run_sync

from .calendar_read import detail as read_detail
from .calendar_reconciliation import event_day, existing_snapshot, incoming_snapshot


def ordinary(detail, *, removable=False):
    read_detail(detail, str(detail[0][0]))
    row = detail[0]
    return (not any(row[i] for i in (6, 7)) and all(str(row[i]) in ('', '0') for i in (22, 23, 33))
            and (not removable or str(row[17]) in ('', '0')) and len(detail) == int(detail[1][0])+2)


def covers_audience(before, retained, identity):
    original = {f'Ru{identity}', *(p for p in str(before[0][15]).split(',') if p)}
    # Current-account visibility was independently verified by EIP. Other
    # recipients must be explicitly covered; never log into their accounts.
    human = {f'Ru{identity}', f'Ru{retained[0][2]}', *(p for p in str(retained[0][15]).split(',') if p)}
    return all(re.fullmatch(r'Ru\d+', principal) for principal in original) and original <= human


async def visible(client, day):
    rows = await client.rpc('List', ap_name='cm', start_date=day, end_date=day, __tag='event_list', strict_json=True)
    if not rows or len(rows) > 501:
        raise ValueError('清理後行程清單不完整，請核對；未重送。')
    result = set()
    for row in rows[1:]:
        if len(row) not in (20, 21) or event_day(row[7]) != day or not str(row[0]).isdecimal() or not row[2] or str(row[2]) in result:
            raise ValueError('清理後行程清單無法核對，請核對；未重送。')
        result.add(str(row[2]))
    return result


def read_bytes(stream):
    try:
        stream.seek(0)
        value = stream.read(50_000_001)
    finally:
        stream.close()
    if not value or len(value) > 50_000_000:
        raise ValueError('行程附件備份超出範圍，未清理。')
    return value


async def reconcile(client, history, journal, *, allowed=object, authorize=None, keys=None):
    if (history.store.dsn, history.store.schema) != (journal.dsn, journal.schema):
        raise ValueError('行程來源與寫入收條不在同一資料庫，未清理。')
    async def guard():
        if not await run_sync(allowed):
            raise ValueError('綁定變更或自動處理暫停，未清理行程。')
        if authorize is not None:
            await authorize()

    stats = {'checked': 0, 'removed': 0, 'needs_review': 0}
    async with journal.calendar_lock():
        await guard()
        records = await run_sync(history.due, client.user_id, keys)
        for record in records:
            await guard()
            identity, event_id = client.user_id, record['event_id']
            op_key = journal.key(identity, {'calendar_manual_supersession_v1': event_id})
            # Creation provenance never comes from an adoption receipt or title.
            if await run_sync(journal.status, op_key) is not None:
                await run_sync(history.state, identity, event_id, 'review')
                stats['needs_review'] += 1
                continue
            states = await run_sync(journal.statuses, [record['source_key'], *record['value'].get('aliases', ())])
            if states.get(record['source_key']) != 'done' or 'unknown' in states.values():
                await run_sync(history.state, identity, event_id, 'review')
                stats['needs_review'] += 1
                continue
            stats['checked'] += 1
            incoming = incoming_snapshot(record['value']['line'].split(','))
            matches = await client.calendar_import_matches([record['value']['line']])
            if len(matches) != 1:
                raise ValueError('人工行程核對回應不完整，未清理。')
            candidates = matches[0].get('review_context', {}).get('candidates', [])
            manual, unproven = [], False
            for candidate in candidates:
                if not rules.same_event_schedule(incoming, candidate['event']) or await run_sync(history.managed, candidate['event_id']):
                    continue
                owner = candidate.get('owner_id')
                if not owner or await run_sync(history.unresolved_creation, owner):
                    unproven = True
                    continue
                if rules.manual_preferred(incoming, candidate['event']):
                    manual.append(candidate)
                else:
                    # Native/manual uploads can keep the AI title. Managed AI
                    # writes persist intent BEFORE I/O; later external records
                    # are authoritative only without managed/unknown provenance.
                    try:
                        older = datetime.fromisoformat(event_id.split(';')[0])
                        newer = datetime.fromisoformat(candidate['event_id'].split(';')[0])
                    except ValueError:
                        continue
                    if newer > older:
                        manual.append(candidate)
                    else:
                        unproven = True
            if not manual:
                await run_sync(history.state, identity, event_id, 'review' if unproven else 'ready')
                continue
            if any(not rules.compatible_facts(a['event'], b['event']) or not rules.clock_compatible(a['event'], b['event'])
                   for n, a in enumerate(manual) for b in manual[n+1:]):
                await run_sync(history.state, identity, event_id, 'review')
                stats['needs_review'] += 1
                continue
            chosen = min(manual, key=lambda c: (not c.get('shared', False), c['event_id']))
            keep_id = chosen['event_id']
            await guard()
            before = await client.calendar_detail(event_id)
            retained = await client.calendar_detail(keep_id)
            if (str(before[0][2]) != identity or str(retained[0][0]) != keep_id
                    or digest(before) != digest(record['value']['detail'])
                    or not ordinary(before, removable=True) or not ordinary(retained)
                    or not covers_audience(before, retained, identity)
                    or str(retained[0][2]) != chosen['owner_id']
                    or not rules.same_event_schedule(existing_snapshot(before), existing_snapshot(retained))
                    or existing_snapshot(retained) != chosen['event']):
                await run_sync(history.state, identity, event_id, 'review')
                stats['needs_review'] += 1
                continue
            # Preserve all attachment bytes before any deletion reservation.
            files, total = [], 0
            for item in read_detail(before, event_id)['attachments']:
                await guard()
                stream = await client.download(item['url'], item['name'])
                payload = await run_sync(read_bytes, stream)
                total += len(payload)
                if total > 100_000_000:
                    raise ValueError('行程附件備份總量過大，未清理。')
                files.append({'name': item['name'], 'sha256': hashlib.sha256(payload).hexdigest(),
                              'data_base64': base64.b64encode(payload).decode()})
            backup = {'contract': 'manual-supersession-v1', 'removed': normalized_detail(before),
                      'retained': normalized_detail(retained), 'files': files}
            await run_sync(history.backup, identity, event_id, backup)
            # Recheck after potentially slow file downloads. Human edits win.
            await guard()
            if (digest(await client.calendar_detail(event_id)) != digest(before)
                    or digest(await client.calendar_detail(keep_id)) != digest(retained)):
                await run_sync(history.state, identity, event_id, 'review')
                stats['needs_review'] += 1
                continue
            states = await run_sync(journal.statuses, [record['source_key'], *record['value'].get('aliases', ())])
            if states.get(record['source_key']) != 'done' or 'unknown' in states.values():
                await run_sync(history.state, identity, event_id, 'review')
                continue
            await guard()
            state = await run_sync(journal.reserve_event, op_key)
            if state != 'reserved':
                raise ValueError('行程清理結果不明，請人工核對；未重送。')
            await run_sync(history.state, identity, event_id, 'deleting')
            # No login refresh, fallback account, or retry across this boundary.
            await guard()
            await client.rpc('Delete', ap_name='cm', event_id=event_id, __tag='delitem', strict_json=True)
            ids = await visible(client, incoming['start_date'])
            if event_id in ids or keep_id not in ids or digest(await client.calendar_detail(keep_id)) != digest(retained):
                raise ValueError('行程刪除結果不明，請人工核對；未重送。')
            await run_sync(journal.done, op_key)
            await run_sync(history.state, identity, event_id, 'deleted')
            stats['removed'] += 1
        stats['needs_review'] = await run_sync(history.attention, client.user_id)
    return stats
