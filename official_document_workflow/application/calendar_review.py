"""One independent semantic check after literal validation, before CSV export."""
from __future__ import annotations

import json
from operator import itemgetter

from .prompts import REVIEW_PROMPT
from official_document_workflow.domain.calendar.calendar_semantics import resolve_spans
from official_document_workflow.domain.calendar.calendar_semantics import verify_event_date
from official_document_workflow.domain.calendar.contracts import CALENDAR_REVIEW_SCHEMA


async def review_calendar(pipe, module, request, events, sources):
    data = {'sources': request['sources'], 'events': [e for e in events if e.get('status') == 'current']}
    result = await pipe._cloud_ocr_text_request(
        module.CloudSemanticSource(
            text=json.dumps(
                data, ensure_ascii=False,
                separators=(',', ':'),
            ), kind='paddle_layout_ocr',
        ),
        prompt=REVIEW_PROMPT, schema=CALENDAR_REVIEW_SCHEMA,
    )
    count = len(data['events'])
    checks = [None] * count
    global_issues = result.get('issues')
    raw_checks = result.get('checks')
    if len(raw_checks) != count or any(not issue.strip() for issue in global_issues):
        raise ValueError('行事曆語意核對回應格式無效，未自動匯入。')
    for check in raw_checks:
        index = check['index']
        if (
            index is True or index is False or not 0 <= index < count or checks[index] is not None
            or check['decision'] not in {'keep', 'merge', 'reject'} or not check['reason'].strip()
            or not 1 <= len(check['evidence_spans']) <= 16
        ):
            raise ValueError('行事曆語意核對回應格式無效，未自動匯入。')
        checks[index] = check.copy()
        blocking = checks[index].get('blocking_fields', [])
        if any(
            field not in {
                'action', 'start_date', 'start_time', 'end_date', 'end_time',
                'event_relation', 'effect', 'duplicate', 'responsible_party', 'location',
            } for field in blocking
        ):
            raise ValueError('行事曆語意核對回應格式無效，未自動匯入。')
        optional_nulls = {
            field for field in ('responsible_party', 'location')
            if data['events'][index].get(field) is None
        }
        if checks[index]['decision'] == 'reject' and blocking and set(blocking) <= optional_nulls:
            checks[index].update(decision='keep', target=None)
    resolved = {}
    for check in checks:
        spans = check.get('evidence_spans')
        # Citations support a judgment, not an ordered document reconstruction.
        # Validate every range against the source without imposing list order.
        resolved[check['index']] = resolve_spans(spans, sources)
        target = check.get('target')
        if check['decision'] == 'merge':
            if (
                target is True or target is False or not 0 <= target < count or target == check['index']
                or checks[target]['decision'] != 'keep'
            ):
                raise ValueError('行事曆合併對應無效，未自動匯入。')
        elif target is not None:
            raise ValueError('非合併行程不得指定合併對象。')
    for check in checks:
        event = data['events'][check['index']]
        if check['decision'] == 'keep' and event.get('start_date'):
            parts = resolved[check['index']]
            if not verify_event_date(
                event['start_date'], '\n'.join(p[3] for p in parts),
                [sources[sid].text for sid in {p[0] for p in parts}],
            ):
                check.update(decision='reject', reason='核對者所引原文未支持本項排程日期。')
    for check in checks:
        if check['decision'] == 'merge' and checks[check['target']]['decision'] != 'keep':
            check.update(decision='reject', target=None, reason='合併目標未通過排程日期來源核對。')
    issues = [*global_issues, *[f"行程 {c['index']+1}：{c['reason']}" for c in checks if c['decision'] == 'reject']]
    return {
        'approved': not global_issues and (not checks or any(c['decision'] == 'keep' for c in checks)),
        'issues': issues, 'global_issues': global_issues, 'checks': checks,
    }


def consolidate_events(events, checks):
    """Apply classified merges without regenerating facts or discarding clauses."""
    current = []
    for event in events:
        if event.get('status') != 'current':
            continue
        candidate = event.copy()
        if 'description_spans' in event:
            candidate['description_spans'] = [dict(span) for span in event['description_spans']]
        current.append(candidate)
    slots = ('project_name', 'start_date', 'start_time', 'end_date', 'end_time')
    for check in checks:
        if check['decision'] != 'merge':
            continue
        source, target = current[check['index']], current[check['target']]
        if any(source.get(field) != target.get(field) for field in slots):
            raise ValueError('合併行程的工程或起訖不一致，請核對；未自動匯入。')
        spans = source['description_spans']+target['description_spans']
        merged = []
        for span in sorted(spans, key=itemgetter('source_id', 'start_line', 'end_line')):
            if (
                merged and merged[-1]['source_id'] == span['source_id']
                and span['start_line'] <= merged[-1]['end_line']+1
            ):
                merged[-1]['end_line'] = max(merged[-1]['end_line'], span['end_line'])
            else:
                merged.append(dict(span))
        target['description_spans'] = merged
        if source.get('preparation_reminders'):
            target['preparation_reminders'] = [
                *target.get('preparation_reminders', []), *source['preparation_reminders'],
            ]
    return [current[c['index']] for c in checks if c['decision'] == 'keep']
