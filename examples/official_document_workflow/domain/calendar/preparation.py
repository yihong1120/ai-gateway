"""Turn content-based preparation suggestions into dated CSV rows after review."""
from __future__ import annotations


from datetime import date
from datetime import timedelta

from pydantic import TypeAdapter

from .calendar_semantics import compact
from .calendar_semantics import resolve_spans
from .contracts import PreparationReminder
from .values import CalendarValues
from .presentation import format_ai_title


def _anchor_key(row):
    return tuple(
        row.get(field, '') for field in (
            'Custom Field1', 'Subject', 'Start Date', 'Start Time(24hr)', 'End Date', 'End Time(24hr)',
        )
    )


def preparation_rows(calendar,  analysis, *, today: date, seen: set):
    """Use only exported anchors; suggestions never authorize an external write.

    Local projection matches each accepted event to an actual reviewed CSV row,
    including consolidated/filtered events. No source is re-read by a model.
    """
    anchors = {_anchor_key(row): row for row in analysis['rows']}
    additions, notices = [], []
    document = analysis['document']
    earliest = max(today, date.fromisoformat(document.issued.gregorian.replace('/', '-')))
    for event in analysis.get('accepted_events', []):
        suggestions = event.get('preparation_reminders', [])
        if not suggestions or event.get('status') != 'current' or not anchors:
            continue
        _, projected = calendar.events(
             {'events': [event], 'primary_source_id': analysis['primary']},
            analysis['sources'], document,
        )
        if not projected or (base := anchors.get(_anchor_key(projected[0]))) is None:
            continue
        anchor = date.fromisoformat(base['Start Date'].replace('/', '-'))
        for raw in suggestions:
            try:
                suggestion = TypeAdapter(PreparationReminder).validate_python(raw)
                resolve_spans(suggestion['evidence_spans'], analysis['sources'])
                action = suggestion['action'].strip()
                if not action or not suggestion['reason'].strip():
                    raise ValueError('缺少準備事項或理由')
                if suggestion['end_time'] <= suggestion['start_time']:
                    raise ValueError('準備提醒的同日結束時間須晚於開始時間')
                title, title_notice = format_ai_title('準備提醒：' + action, event.get('project_name'))
            except ValueError:
                notices.append('有 AI 準備建議的內容、時段或依據未通過檢查，未加入提醒；原定行程保留。')
                continue
            try:
                scheduled = anchor - timedelta(days=suggestion['days_before'])
            except OverflowError:
                notices.append('有 AI 準備提醒超出可表示的日期範圍，未加入；原定行程保留。')
                continue
            if scheduled < earliest:
                notices.append('有 AI 準備提醒日期已過或早於發文日，未補排；原定行程保留。')
                continue
            key = (
                anchor, base['Start Time(24hr)'], event.get('project_name') or base['Custom Field1'],
                scheduled, compact(action),
            )
            if key in seen:
                continue
            seen.add(key)
            scheduled_date = CalendarValues._cloud_date_from_event(scheduled.isoformat())
            start_time = CalendarValues._cloud_time_from_value(suggestion['start_time'])
            end_time = CalendarValues._cloud_time_from_value(suggestion['end_time'])
            duration = CalendarValues._calendar_duration_hours(scheduled_date, start_time, scheduled_date, end_time)
            row = dict(base)
            row.update({
                'Name': 'AI填寫',
                'Subject': title,
                'Start Date': scheduled_date.gregorian,
                'End Date': scheduled_date.gregorian,
                'Start Time': start_time.display,
                'Start Time(24hr)': start_time.twenty_four_hour,
                'End Time': end_time.display,
                'End Time(24hr)': end_time.twenty_four_hour,
                'Work Hour': duration,
                'All day event': '假',
                'Private': '真',
                'Custom Field3': base['Custom Field3'] or '未載明',
                'Custom Field4': '待指派（AI建議）',
                'Custom Field5': base['Custom Field5'] or '未載明',
                'Report Status': '待辦',
                'Completed': '0',
                'Description': '\n\n'.join(
                    filter(
                        None, [
                            'AI 建議準備提醒（非公文明訂期限）',
                            '準備事項：' + action,
                            f"基準行程：{base['Subject']}\n原定日期：{base['Start Date']}"
                            f"\n建議提前 {suggestion['days_before']} 天：{scheduled:%Y/%m/%d}",
                            f"AI 建議時段（台北時間）：{start_time.twenty_four_hour}–{end_time.twenty_four_hour}"
                            f"，預估工時 {duration} 小時；可依實際工作安排調整，非公文明訂時間。",
                            '安排理由：' + suggestion['reason'].strip(),
                            title_notice,
                            '公文內容：\n' + base['Description'],
                        ],
                    ),
                ),
            })
            additions.append(row)
    return additions, list(dict.fromkeys(notices))
