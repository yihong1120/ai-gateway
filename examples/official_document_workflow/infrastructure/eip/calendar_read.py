"""Netask cm_list.htm contract: occurrence rows, not workflow dates.

Verified against deployed UI 2026-09-07. Parse new Date(...) without eval.
The server applies visibility ACLs; cm_list's default is personal + shared.
"""
from __future__ import annotations

import re
from datetime import date
from typing import TypedDict


class CalendarOccurrence(TypedDict):
    id: str
    subject: str
    date: str
    weekday: str
    all_day: bool
    start_time: str | None
    end_time: str | None


def decode_escape(match):
    return chr(int(match[1] or match[2], 16))


def occurrence_order(item):
    return item['date'], item['start_time'] or '', item['subject']


def unescape(value):
    # Javascript unescape, not percent-decoded UTF-8; combine UTF-16 pairs.
    value = re.sub(
        r'%u([0-9a-fA-F]{4})|%([0-9a-fA-F]{2})',
        decode_escape, value,
    )
    return value.encode('utf-16-le', 'surrogatepass').decode('utf-16-le')


def occurrences(rows: list[list[object]], start: str, end: str) -> list[CalendarOccurrence]:
    a, b = date.fromisoformat(start), date.fromisoformat(end)
    if a.isoformat() != start or b.isoformat() != end or not 0 <= (b-a).days <= 92:
        raise ValueError()
    if len(rows) > 500:
        raise ValueError('行事曆超過 500 筆，請縮小日期範圍；未截斷結果。')
    result: list[CalendarOccurrence] = []
    for row in rows:
        if len(row) not in (20, 21):
            raise ValueError()
        m = re.fullmatch(r'new Date\(\s*(\d{4}),\s*(\d{1,2}),\s*(\d{1,2}),\s*0,\s*0,\s*0\s*\)', str(row[7]))
        if not m or str(row[8]) not in ('0', '1'):
            raise ValueError()
        day = date(int(m[1]), int(m[2])+1, int(m[3]))
        subject = unescape(row[6])
        if not subject.strip() or len(subject) > 2000:
            raise ValueError()
        all_day = str(row[8]) == '1'
        times: list[str | None] = []
        for value in row[9:11]:
            if all_day:
                times.append(None)
            else:
                if not re.fullmatch(r'\d{1,4}', str(value)) or not 0 <= int(str(value)) <= 1440:
                    raise ValueError()
                minutes = int(str(value))
                times.append(f'{minutes//60:02d}:{minutes % 60:02d}')
        if a <= day <= b:
            result.append({
                'id': str(row[2]), 'subject': subject, 'date': day.isoformat(), 'weekday': '星期'+'一二三四五六日'[day.weekday()], 'all_day': all_day,
                'start_time': times[0], 'end_time': times[1],
            })
    return sorted(result, key=occurrence_order)


def detail(rows, event_id):
    """Decode the existing Detail/cm/viewdtl contract, without executing its HTML."""
    if len(rows) < 2 or len(rows[0]) < 43 or rows[0][0] != event_id or len(rows[1]) != 1:
        raise ValueError('EIP 行程詳細資料格式不符。')
    count = int(rows[1][0])
    if not 0 <= count <= 50 or len(rows) < count + 2:
        raise ValueError('EIP 行程附件清單不完整或超過 50 份。')
    attachments = []
    for row in rows[2:count + 2]:
        if len(row) != 7 or not row[2] or not row[3]:
            raise ValueError('EIP 行程附件格式不符。')
        attachments.append({'name': str(row[2]), 'url': str(row[3])})
    return {
        'subject': unescape(str(rows[0][8])),
        'description': unescape(str(rows[0][14])),
        'attachments': attachments,
    }
