"""Read visible owned/shared EIP events before writes; never edit a match."""
from __future__ import annotations

import re
from datetime import date, timedelta
from difflib import SequenceMatcher

from examples.official_document_workflow.domain.calendar import identity
from examples.official_document_workflow.domain.calendar.deduplication import (
    original_schedule_days,
)
from examples.official_document_workflow.domain.calendar.match_review import (
    REVISION_DAYS,
)

from .calendar_read import unescape
from .calendar_verification import validate_wire

ASCII_UPPER = str.maketrans('abcdefghijklmnopqrstuvwxyz', 'ABCDEFGHIJKLMNOPQRSTUVWXYZ')
MAX_DETAILS = 100


def event_day(value):
    match = re.fullmatch(r'new Date\(\s*(\d{4}),\s*(\d+),\s*(\d+),\s*0,\s*0,\s*0\s*\)', str(value))
    if not match:
        raise ValueError('EIP 行程日期格式不完整，未新增。')
    return date(int(match[1]), int(match[2])+1, int(match[3])).isoformat()


def related_title(left, right):
    # Retrieval only. The model must still prove the same concrete event;
    # similar wording or a shared project never authorizes receipt adoption.
    a, b = identity.title(left), identity.title(right)
    if a == b:
        return True
    work_a, work_b = str(a[1]), str(b[1])
    grams = lambda value: {value[i:i+2] for i in range(len(value)-1)}
    overlap = grams(work_a) & grams(work_b)
    return len(overlap) >= 4 and SequenceMatcher(None, work_a, work_b, autojunk=False).ratio() >= .45


def incoming_snapshot(fields):
    return {
        'title': unescape(fields[0]), 'description': unescape(fields[19]),
        'start_date': fields[1], 'end_date': fields[3],
        'start_time': fields[2], 'end_time': fields[4], 'all_day': fields[5],
        'custom_fields': list(fields[23:28]), 'priority': fields[20],
    }


def existing_snapshot(detail):
    row = detail[0]
    match = re.fullmatch(r'new Date\((\d{4}),\s*(\d+),\s*(\d+),\s*0,\s*0,\s*0\)', str(row[9]))
    if not match:
        raise ValueError('EIP 既有行程日期格式無法核對，未新增。')
    day = date(int(match[1]), int(match[2])+1, int(match[3])).isoformat()
    return {
        'title': unescape(str(row[8])), 'description': unescape(str(row[14])),
        'start_date': day, 'end_date': day,
        'start_time': str(row[11]), 'end_time': str(row[12]), 'all_day': str(row[10]),
        'custom_fields': [str(v) for v in row[37:42]], 'priority': str(row[13]),
    }


def same_slot(row, fields):
    return str(row[8]) == fields[5] and (
        fields[5] == '1' or [str(row[9]), str(row[10])] == [fields[2], fields[4]]
    )


def distinct_all_day_segments(detail, fields):
    """A proven sibling day is not a revision or duplicate of this segment."""
    from .calendar_csv import ALL_DAY_SEGMENT_NOTICE

    row = detail[0]
    if fields[5] != '1' or str(row[10]) != '1':
        return False
    incoming = unescape(fields[19]).replace('\r\n', '\n')
    existing = unescape(str(row[14])).replace('\r\n', '\n')
    new, old = ALL_DAY_SEGMENT_NOTICE.search(incoming), ALL_DAY_SEGMENT_NOTICE.search(existing)
    if not new or not old or (new['start'], new['end']) != (old['start'], old['end']):
        return False
    actual_day = event_day(row[9]).replace('-', '/')
    if (new['segment'] != fields[1].replace('-', '/') or old['segment'] != actual_day
            or new['segment'] == old['segment']):
        return False
    if identity.title(unescape(fields[0])) != identity.title(unescape(str(row[8]))):
        return False
    if tuple(identity.text(v) for v in row[37:42]) != tuple(identity.text(v) for v in fields[23:28]):
        return False
    source, prior_source = incoming[:new.start()], existing[:old.start()]
    return prior_source in (source, source.translate(ASCII_UPPER))


def compare(detail, fields, user_id, event_id, *, expected_owner=None, occurrence_day=None):
    if not detail or len(detail[0]) < 43:
        raise ValueError('EIP 既有行程詳細資料不完整，未新增；請稍後重新核對。')
    row = detail[0]
    if row[0] != event_id or row[2] != (expected_owner or user_id):
        raise ValueError('EIP 既有行程擁有人不符，未認領或新增行程。')
    expected_ref = identity.text(fields[23])
    expected_title = identity.title(unescape(fields[0]))
    actual_title = identity.title(unescape(str(row[8])))
    same_title = expected_title == actual_title
    match = re.fullmatch(r'new Date\((\d{4}),\s*(\d+),\s*(\d+),\s*0,\s*0,\s*0\)', str(row[9]))
    if not match:
        raise ValueError('EIP 既有行程日期格式無法核對，未新增。')
    day = date(int(match[1]), int(match[2])+1, int(match[3])).isoformat()
    if occurrence_day is not None:
        day = occurrence_day
    interval = day == fields[1] == fields[3] and str(row[10]) == fields[5] and (
        fields[5] == '1' or [str(row[11]), str(row[12])] == [fields[2], fields[4]]
    )
    expected_description = unescape(fields[19])
    actual_description = identity.description(unescape(str(row[14])))
    descriptions = {
        identity.description(expected_description),
        identity.description(expected_description.translate(ASCII_UPPER)),
    }
    same_details = actual_description in descriptions and any((expected_ref, expected_description.strip()))
    custom = tuple(identity.text(v) for v in row[37:42]) == tuple(identity.text(v) for v in fields[23:28])
    if same_title and interval and same_details and custom and str(row[13]) == fields[20]:
        return 'same'
    return 'conflict'


async def inspect_imports(client, lines):
    """Return new/existing/conflict in input order without modifying EIP.

    The server's visible listing supplies the ACL and expected detail owner.
    Shared matches are read-only receipts, never project/attachment edit targets.
    Adjacent-date title candidates cover revised notices within 15 days.
    Listing/detail limits are explicit;
    inability to prove absence is never treated as permission to create.
    """
    if not 1 <= len(lines) <= 100:
        raise ValueError('既有行程核對數量超出範圍。')
    parsed = [validate_wire(line, client.user_id) for line in lines]
    seen = set()
    for fields in parsed:
        key = (
            tuple(fields[1:6]), identity.title(unescape(fields[0])),
            identity.description(unescape(fields[19])), tuple(fields[23:28]),
        )
        if key in seen:
            raise ValueError('本批 CSV 含相同來源與時段的重複行程，請先核對；本批未新增。')
        seen.add(key)
    listings = {}
    for day in dict.fromkeys(fields[1] for fields in parsed):
        day_value = date.fromisoformat(day)
        start, end = (day_value-timedelta(days=REVISION_DAYS)).isoformat(), (day_value+timedelta(days=REVISION_DAYS)).isoformat()
        old_days = {old for fields in parsed if fields[1] == day for old in original_schedule_days(incoming_snapshot(fields))
                    if not start <= old <= end}
        if len(old_days) > 2:
            raise ValueError('同日來源包含過多原訂日期，請分批核對；未新增行程。')
        candidates = []
        seen_ids = set()
        for lower, upper in [(start, end), *((old, old) for old in sorted(old_days))]:
            listing = await client.rpc('List', ap_name='cm', start_date=lower, end_date=upper, __tag='event_list', strict_json=True)
            if not listing or len(listing) > 501 or any(len(row) not in (20, 21) for row in listing[1:]):
                raise ValueError('EIP 既有行程清單無法完整核對，未新增；請人工確認。')
            query_ids = set()
            for row in listing[1:]:
                listed_day = event_day(row[7])
                if not lower <= listed_day <= upper:
                    raise ValueError('EIP 行程超出查詢區間，未新增。')
                if not str(row[0]).isdecimal() or not row[2]:
                    raise ValueError('EIP 既有行程擁有人／識別碼不完整，未新增。')
                key = (str(row[2]), listed_day)
                if key in query_ids:
                    raise ValueError('EIP 行程清單重複／分頁不完整，未新增。')
                query_ids.add(key)
                if key not in seen_ids:
                    seen_ids.add(key)
                    candidates.append(row)
        listings[day] = candidates
    details, results = {}, []
    for fields in parsed:
        matches, conflicts, candidates = [], [], []
        old_days = original_schedule_days(incoming_snapshot(fields))
        for row in listings[fields[1]]:
            listed_day = event_day(row[7])
            if listed_day != fields[1] and listed_day not in old_days and not related_title(unescape(str(row[6])), unescape(fields[0])):
                continue
            event_id = str(row[2])
            if event_id not in details:
                if len(details) >= MAX_DETAILS:
                    raise ValueError('EIP 同日待核對行程過多，未新增；請分批或人工確認。')
                details[event_id] = await client.calendar_detail(event_id)
            detail = details[event_id]
            if not detail or len(detail[0]) < 43:
                raise ValueError('EIP 既有行程詳細資料不完整，未新增。')
            if detail[0][0] != event_id or str(detail[0][2]) != str(row[0]):
                raise ValueError('EIP 既有行程擁有人與可見清單不符，未新增。')
            recurring = str(detail[0][22]) not in ('', '0')
            if recurring and listed_day != fields[1]:
                continue  # Other occurrences of a recurring series are separate instances.
            if event_day(detail[0][9]) != listed_day and not recurring:
                raise ValueError('EIP 清單與詳細資料日期不符，請重新核對；未新增。')
            if not recurring and distinct_all_day_segments(detail, fields):
                continue
            decision = compare(detail, fields, client.user_id, event_id, expected_owner=str(row[0]), occurrence_day=listed_day if recurring else None)
            if decision in {'same', 'conflict'}:
                snapshot = existing_snapshot(detail)
                if recurring:
                    snapshot['start_date'] = snapshot['end_date'] = listed_day
                candidates.append({
                    'event_id': event_id, 'comparison': decision,
                    'event': snapshot,
                    'shared': str(row[0]) != client.user_id,
                    'owner_id': str(row[0]),
                })
            if decision == 'same':
                matches.append(event_id)
            elif decision == 'conflict':
                conflicts.append(event_id)
        if len(matches) > 1 or conflicts:
            results.append({
                'status': 'conflict',
                'reason': 'EIP 已有多筆相似行程，或原行程與本次時間、內容不同；請核對原行程，未另建新行程。',
                'review_context': {'incoming': incoming_snapshot(fields), 'candidates': candidates},
            })
        elif matches:
            results.append({'status': 'existing', 'receipt': {'event_id': matches[0], 'detail_verified': True}})
        else:
            results.append({'status': 'new'})
    return results
