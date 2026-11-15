"""Project AI time suggestions without changing source-verified event facts."""
from __future__ import annotations

from datetime import datetime
from datetime import timedelta

from pydantic import TypeAdapter

from .contracts import TentativeSchedule


TENTATIVE_NOTICE = 'AI 暫排（非公文明訂時間）'
EIP_REVIEW_NOTICE = '匯入後請至 EIP 行事曆核對並修改實際起訖時間。'


def apply_tentative_schedule(row, suggestion):
    """Fill missing clocks/end date only; a missing source date stays pending."""
    if not suggestion or all(row.get(k) for k in (
        'Start Time(24hr)', 'End Date', 'End Time(24hr)',
    )):
        return row
    proposed = TypeAdapter(TentativeSchedule).validate_python(suggestion)
    start_day = datetime.strptime(row['Start Date'], '%Y/%m/%d')
    end_day = datetime.strptime(
        row['End Date'], '%Y/%m/%d',
    ) if row['End Date'] else datetime.strptime(proposed['end_date'], '%Y-%m-%d')
    start_clock = row['Start Time(24hr)'] or proposed['start_time']
    end_clock = row['End Time(24hr)'] or proposed['end_time']
    start = datetime.combine(start_day, datetime.strptime(start_clock, '%H:%M').time())
    end = datetime.combine(end_day, datetime.strptime(end_clock, '%H:%M').time())
    if end <= start or (not row['End Date'] and end_day > start_day + timedelta(days=1)):
        raise ValueError('AI 暫排起訖無效；須保留已知日期，未載結束日最多暫排至次日。')
    reason = proposed['reason'].strip()
    if not reason:
        raise ValueError('AI 暫排缺少估算理由。')
    missing = [label for field, label in (
        ('Start Time(24hr)', '開始時間'), ('End Date', '結束日期'), ('End Time(24hr)', '結束時間'),
    ) if not row[field]]
    return {
        **row,
        'Start Time': start.strftime('%H:%M:00'),
        'Start Time(24hr)': start_clock,
        'End Date': end.strftime('%Y/%m/%d'),
        'End Time': end.strftime('%H:%M:00'),
        'End Time(24hr)': end_clock,
        'Work Hour': f'{(end - start).total_seconds() / 3600:g}',
        'All day event': '假',
        'Description': '\n'.join([
            TENTATIVE_NOTICE,
            '待確認欄位：' + '、'.join(missing),
            f'暫排時段（台北時間）：{start:%Y/%m/%d %H:%M}–{end:%Y/%m/%d %H:%M}',
            '估算理由：' + reason,
            EIP_REVIEW_NOTICE,
        ]) + '\n\n' + row['Description'],
    }
