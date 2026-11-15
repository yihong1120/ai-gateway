"""One independent semantic check after literal validation, before CSV export."""
from __future__ import annotations

from examples.official_document_workflow.domain import documents as module

import json
from copy import deepcopy
from operator import itemgetter
from examples.official_document_workflow.services.analysis_reuse import analyze_content, neutral_event

from .prompts import REVIEW_PROMPT
from examples.official_document_workflow.domain.calendar.calendar_semantics import resolve_spans
from examples.official_document_workflow.domain.calendar.calendar_semantics import verify_event_date
from examples.official_document_workflow.domain.calendar.contracts import CALENDAR_REVIEW_SCHEMA
from examples.official_document_workflow.domain.calendar.relative_dates import RelativeDateProof
from examples.official_document_workflow.domain.calendar.tracking_reminders import TrackingReminderProof


async def review_calendar(pipe,  request, events, sources):
    current = [e for e in events if e.get('status') == 'current']
    data = {'sources': request['sources'],
            'events': [{key: value for key, value in e.items() if not key.startswith('_')} for e in current]}
    calculations = [
        {'index': n, **event['_relative_date_proof'].audit()}
        for n, event in enumerate(current) if isinstance(event.get('_relative_date_proof'), RelativeDateProof)
    ]
    tracking = [
        {'index': n, **event['_tracking_reminder_proof'].audit()}
        for n, event in enumerate(current) if isinstance(event.get('_tracking_reminder_proof'), TrackingReminderProof)
    ]
    prompt = (REVIEW_PROMPT + '\n行事曆只收錄本帳號確實需要安排、參與或追蹤的具體事項。'
              '公文的發文或收文日期本身不是行程。單純通知、知悉、備查、轉知、背景，'
              '以及無關或已完成事項均須 reject；其他單位的時程也須有本帳號需要追蹤的具體依據。'
              '若是否需要本人處理或追蹤的來源不明確，須 reject 並保留核對，不能因公文存在就保留。')
    if calculations:
        data['date_calculations'] = calculations
        prompt += ('\ndate_calculations 是系統根據原文明示相對期間及收文／發文日期試算的追蹤提醒。'
                   '核對原文工作、起算條件、天數與計日方式，不要求計算後日期逐字出現在原文。'
                   '發文日暫代送達日及假日規則未確認均已標為推估，不可聲稱送達已確認或法定期限已核定。'
                   '推估日期不會讓未核定提案、取消、或無關的工作成立；仍須 reject。')
    if tracking:
        data['tracking_reminders'] = tracking
        prompt += ('\ntracking_reminders 是 AI 建議的追蹤提醒，並非公文明定期限。'
                   '對清單中的 index，start_date 僅供追蹤，程式已依核對過的收文／發文日期及 AI 建議間隔計算。'
                   '不要求這個日期或間隔逐字出現在原文，不能僅以原文未載明追蹤日期或期限為由 reject。'
                   '請依完整語境核對本人或本公司是否有具體工作、會議、稽查或待辦，且確有安排或追蹤必要；'
                   '不以「儘速」等單一字詞作判斷條件。一般通知、備查、知悉、轉知、純背景、沒有具體追蹤必要的籠統依規定辦理，'
                   '以及提案未核定、工作有歧義、已完成、取消或無關的項目均須 reject。')
    model_data = data
    if getattr(pipe, 'analysis_results', None) is not None:
        model_data = deepcopy(data)
        model_data['events'] = [neutral_event(event) for event in current]
        private_dates = {'anchor_date', 'anchor_source', 'deadline', 'reminder_date', 'receipt_date_recorded'}
        for name in ('date_calculations', 'tracking_reminders'):
            if name in model_data:
                model_data[name] = [{k: v for k, v in item.items() if k not in private_dates} for item in model_data[name]]
        prompt += ('\n共用核對只判斷公文原文的工作與期間規格；個別收文日期與試算日期不提供給模型。'
                   '具有 date_calculations 或 tracking_reminders 的項目由程式另外核對並計算個人日期，'
                   '不要僅因該項 start_date=null 而拒絕已成立且有原文期間依據的工作。')
    result = await analyze_content(
        pipe,
        module.CloudSemanticSource(
            text=json.dumps(
                model_data, ensure_ascii=False,
                separators=(',', ':'),
            ), kind='paddle_layout_ocr',
        ),
        stage='calendar-source-review', prompt=prompt, schema=CALENDAR_REVIEW_SCHEMA,
        revisions=request.get('document_revisions', ()),
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
            proof = current[check['index']].get('_relative_date_proof') or current[check['index']].get('_tracking_reminder_proof')
            evidence = '\n'.join(p[3] for p in parts)
            verified = (
                proof.supports(event['start_date'], evidence) if isinstance(proof, (RelativeDateProof, TrackingReminderProof))
                else verify_event_date(event['start_date'], evidence,
                                       [sources[sid].text for sid in {p[0] for p in parts}])
            )
            if verified and isinstance(proof, TrackingReminderProof):
                action_refs = (current[check['index']].get('evidence_spans') or {}).get('action') or []
                verified = any(
                    sid == span['source_id'] and start <= span['end_line'] and end >= span['start_line']
                    for sid, start, end, _ in parts for span in action_refs
                )
            if not verified:
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
        if bool(source.get('_tracking_reminder_proof')) != bool(target.get('_tracking_reminder_proof')):
            raise ValueError('追蹤提醒與原文明定行程不可合併，請核對。')
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
