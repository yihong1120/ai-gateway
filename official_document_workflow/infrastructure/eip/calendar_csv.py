"""CSV validation and EIP transport formatting without WebUI or Pipe loading."""
from __future__ import annotations

import csv
import io
import os
import sys
from datetime import datetime
from datetime import timedelta

from .netask import csv_minutes
from official_document_workflow.domain.calendar.csv_schema import CALENDAR_HEADERS
from official_document_workflow.domain.calendar.csv_schema import CALENDAR_TRANSLATION_ROW
from official_document_workflow.domain.calendar.presentation import format_ai_title
from official_document_workflow.domain.calendar.presentation import split_ai_title

csv.field_size_limit(sys.maxsize)

EIP_SUBJECT_UTF8_BYTE_LIMIT = 200
SUBJECT_NOTICE = '行事曆顯示標題因 EIP 字元限制縮短；完整主旨：'


def fit_calendar_subject(row):
    """Fit the observed 200-byte EIP boundary while retaining the full subject."""
    original = row['Subject']
    if len(original.encode('utf-8')) <= EIP_SUBJECT_UTF8_BYTE_LIMIT:
        return dict(row)
    # Relative reminders can add a prefix after the initial event was sized.
    # Keep attribution/project intact rather than truncating the final suffix.
    if parsed := split_ai_title(original):
        fitted, notice = format_ai_title(parsed[0], parsed[1])
        return {
            **row,
            'Subject': fitted,
            'Description': notice + '\n\n' + row['Description'],
        }
    prefix = ''
    for character in original:
        if (
            len((prefix + character + '…').encode('utf-8'))
            > EIP_SUBJECT_UTF8_BYTE_LIMIT
        ):
            break
        prefix += character
    return {
        **row,
        'Subject': prefix + '…',
        'Description': SUBJECT_NOTICE + '\n' + original + '\n\n' + row['Description'],
    }


def automatic_duration_minutes():
    minutes = int(os.getenv('EIP_CALENDAR_DEFAULT_DURATION_MINUTES', '60'))
    if minutes not in (60, 120):
        raise ValueError('自動行事曆預留時間設定不正確；僅允許 60 或 120 分鐘。')
    return minutes


def duration_notice(minutes):
    return f"原文未載明結束時間；系統預留 {minutes} 分鐘，非公文載明的結束時間，可依實際安排調整。"


def canonical_time(row, prefix):
    """Accept the producer's 24h display and Netask's AM/PM display, losslessly."""
    raw24, raw12 = row[f"{prefix} Time(24hr)"], row[f"{prefix} Time"]
    minutes = None
    if raw24:
        if prefix == 'End' and raw24 == '24:00':
            minutes = 1440
        else:
            moment = datetime.strptime(raw24, '%H:%M')
            minutes = moment.hour * 60 + moment.minute
    if raw12:
        compact = raw12.replace(' ', '')
        if compact.startswith(('上午', '下午')):
            display_minutes = int(
                csv_minutes(compact, allow_end_of_day=prefix == 'End'),
            )
        else:
            moment = datetime.strptime(compact, '%H:%M:%S')
            if moment.second:
                raise ValueError()
            display_minutes = moment.hour * 60 + moment.minute
        if minutes is not None and minutes != display_minutes:
            raise ValueError('CSV 的 12／24 小時制時間不一致，請先修正。')
        minutes = display_minutes
    if minutes is None:
        return ''
    if minutes == 1440:
        return '上午24:00:00'
    hour, minute = divmod(minutes, 60)
    return ('上午' if hour < 12 else '下午') + f"{hour % 12 or 12:02d}:{minute:02d}:00"


def import_segments(rows):
    """Split timed intervals for EIP and retain original receipt metadata."""
    result = []
    for row in rows:
        if row['All day event'] == '真':
            result.append(row)
            continue
        start_day = datetime.strptime(row['Start Date'], '%Y/%m/%d')
        end_day = datetime.strptime(row['End Date'], '%Y/%m/%d')
        start = start_day + timedelta(
            minutes=int(csv_minutes(canonical_time(row, 'Start'))),
        )
        end = end_day + timedelta(
            minutes=int(csv_minutes(canonical_time(row, 'End'), allow_end_of_day=True)),
        )
        if end < start:
            raise ValueError('行程結束時間早於開始時間，請先修正 CSV。')
        if start_day == end_day:
            result.append(row)
            continue
        count = (end.date() - start.date()).days + (end.time() != datetime.min.time())
        if len(result) + count > 100:
            raise ValueError('跨日分段後超過 EIP 單次 100 筆上限，請分批處理。')
        cursor = start
        while cursor < end:
            midnight = cursor.replace(hour=0, minute=0) + timedelta(days=1)
            stop = min(midnight, end)
            end_clock = '24:00' if stop == midnight else stop.strftime('%H:%M')
            part = {
                **row,
                'Start Date': cursor.strftime('%Y/%m/%d'),
                'End Date': cursor.strftime('%Y/%m/%d'),
                'Start Time': '',
                'Start Time(24hr)': cursor.strftime('%H:%M'),
                'End Time': '',
                'End Time(24hr)': end_clock,
                '_source_interval': row,
            }
            part['Description'] = row['Description'].rstrip() + (
                f"\nEIP 跨日分段：完整區間 {start:%Y/%m/%d %H:%M}–{end:%Y/%m/%d %H:%M}；"
                f"本段 {cursor:%Y/%m/%d %H:%M}–{end_clock}，非新增獨立任務。"
            )
            result.append(part)
            cursor = stop
    if len(result) > 100:
        raise ValueError('跨日分段後超過 EIP 單次 100 筆上限，請分批處理。')
    return result


def parse_csv(payload: bytes, *, default_duration_minutes=None):
    if default_duration_minutes is not None and default_duration_minutes not in (
        60,
        120,
    ):
        raise ValueError('預留時間僅允許 60 或 120 分鐘。')
    reader = csv.DictReader(io.StringIO(payload.decode('utf-8-sig')))
    if reader.fieldnames != CALENDAR_HEADERS:
        raise ValueError()
    rows = list(reader)
    if rows and rows[0] == CALENDAR_TRANSLATION_ROW:
        rows.pop(0)
    if not 1 <= len(rows) <= 100:
        raise ValueError()
    for index, row in enumerate(rows):
        if None in row or any(v is None for v in row.values()) or not row['Subject']:
            raise ValueError()
        start = datetime.strptime(row['Start Date'], '%Y/%m/%d')
        start_time = canonical_time(row, 'Start')
        canonical_time(row, 'End')
        # Add only the configured, explicitly labelled reservation.
        if (
            default_duration_minutes is not None
            and row['All day event'] == '假'
            and not any(row[k] for k in ('End Time', 'End Time(24hr)'))
            and row['End Date'] in ('', row['Start Date'])
            and start_time
        ):
            end_moment = start + timedelta(
                minutes=int(csv_minutes(start_time)) + default_duration_minutes,
            )
            row['End Date'] = end_moment.strftime('%Y/%m/%d')
            row['End Time'] = end_moment.strftime('%H:%M:00')
            row['End Time(24hr)'] = end_moment.strftime('%H:%M')
            row['Description'] = (
                row['Description'].rstrip()
                + '\n'
                + duration_notice(default_duration_minutes)
            )
        end = datetime.strptime(row['End Date'], '%Y/%m/%d')
        if end < start or row['All day event'] not in {'真', '假'}:
            raise ValueError()
        if row['All day event'] == '假' and (
            not (row['Start Time'] or row['Start Time(24hr)'])
            or not (row['End Time'] or row['End Time(24hr)'])
        ):
            raise ValueError()
        if any('\x00' in v for v in row.values()):
            raise ValueError()
        converted = fit_calendar_subject(row)
        rows[index] = converted
    rows = import_segments(rows)
    stream = io.StringIO(newline='')
    writer = csv.DictWriter(stream, fieldnames=CALENDAR_HEADERS, quoting=csv.QUOTE_ALL)
    writer.writeheader()
    # EIP ignores 24-hour columns, so serialize equivalent 12-hour fields.
    for row in rows:
        converted = {key: row[key] for key in CALENDAR_HEADERS}
        for prefix in ('Start', 'End'):
            converted[f"{prefix} Time"] = canonical_time(row, prefix)
        writer.writerow(converted)
    return rows, stream.getvalue().encode('utf-8-sig')
