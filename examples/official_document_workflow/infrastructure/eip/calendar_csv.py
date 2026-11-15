"""CSV validation and EIP transport formatting without WebUI or Pipe loading."""
from __future__ import annotations

import csv
import io
import os
import re
import sys
from datetime import datetime
from datetime import timedelta
from urllib.parse import unquote_plus

from .netask import csv_minutes
from examples.official_document_workflow.domain.calendar.csv_schema import CALENDAR_CSV_FORMAT
from examples.official_document_workflow.domain.calendar.identity import csv_key
from examples.official_document_workflow.domain.calendar.csv_schema import CALENDAR_HEADERS
from examples.official_document_workflow.domain.calendar.csv_schema import CALENDAR_TRANSLATION_ROW
from examples.official_document_workflow.domain.calendar.csv_schema import protect_eip_csv
from examples.official_document_workflow.domain.calendar.presentation import format_ai_title
from examples.official_document_workflow.domain.calendar.presentation import split_ai_title
from examples.official_document_workflow.domain.calendar.scheduling import EIP_REVIEW_NOTICE

csv.field_size_limit(sys.maxsize)

EIP_SUBJECT_UTF8_BYTE_LIMIT = 200
SUBJECT_NOTICE = '行事曆顯示標題因 EIP 字元限制縮短；完整主旨：'
MIDNIGHT_IMPORT_NOTICE = '此 CSV 已將 00:00 開始的時段轉為可直接上傳 EIP 的全天提醒；原定起訖完整保留於說明，匯入後請至 EIP 核對修改。'
MIDNIGHT_FALLBACK_HEADING = 'EIP 直接匯入相容提醒：本列以全天提醒匯入，原定時段請於匯入後在 EIP 修改。'
MIDNIGHT_FALLBACK = re.compile(
    r'\A' + re.escape(MIDNIGHT_FALLBACK_HEADING)
    + r'\n原定時段：(?P<date>\d{4}/\d{2}/\d{2}) 00:00–(?P<end>(?:[01]\d|2[0-4]):[0-5]\d)\n\n',
)
SEGMENT_NOTICE = re.compile(
    r'\nEIP 跨日分段：完整區間 (?P<start>\d{4}/\d{2}/\d{2} \d{2}:\d{2})'
    r'–(?P<end>\d{4}/\d{2}/\d{2} \d{2}:\d{2})；'
    r'本段 \d{4}/\d{2}/\d{2} \d{2}:\d{2}–\d{2}:\d{2}，非新增獨立任務。\Z',
)
ALL_DAY_SEGMENT_NOTICE = re.compile(
    r'\nEIP 全天跨日分段：完整區間 (?P<start>\d{4}/\d{2}/\d{2})'
    r'–(?P<end>\d{4}/\d{2}/\d{2})；本段 (?P<segment>\d{4}/\d{2}/\d{2})，非新增獨立任務。\Z',
)


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
    # Preserve this text for legacy import receipts and source replay checks.
    return f"原文未載明結束時間；系統預留 {minutes} 分鐘，非公文載明的結束時間，可依實際安排調整。"


def reserve_missing_end(row, minutes):
    """Reuse the scheduled import reservation for legacy/no-suggestion rows."""
    if (
        row['All day event'] != '假'
        or any(row[k] for k in ('End Time', 'End Time(24hr)'))
        or row['End Date'] not in ('', row['Start Date'])
        or not (start_time := canonical_time(row, 'Start'))
    ):
        return row
    start = datetime.strptime(row['Start Date'], '%Y/%m/%d')
    end = start + timedelta(minutes=int(csv_minutes(start_time)) + minutes)
    return {
        **row,
        'End Date': end.strftime('%Y/%m/%d'),
        'End Time': end.strftime('%H:%M:00'),
        'End Time(24hr)': end.strftime('%H:%M'),
        'Description': row['Description'].rstrip() + '\n' + duration_notice(minutes),
    }


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
    """Split multi-day intervals into EIP daily rows, retaining provenance."""
    result = []
    for row in rows:
        if row['All day event'] == '真':
            start = datetime.strptime(row['Start Date'], '%Y/%m/%d')
            end = datetime.strptime(row['End Date'], '%Y/%m/%d')
            if end < start:
                raise ValueError('行程結束日期早於開始日期，請先修正 CSV。')
            if start == end:
                result.append(row)
                continue
            count = (end - start).days + 1
            if len(result) + count > 100:
                raise ValueError('跨日分段後超過 EIP 單次 100 筆上限，請分批處理。')
            for offset in range(count):
                day = (start + timedelta(days=offset)).strftime('%Y/%m/%d')
                result.append({
                    **row, 'Start Date': day, 'End Date': day, '_source_interval': row,
                    'Description': row['Description'] + (
                        f"\nEIP 全天跨日分段：完整區間 {start:%Y/%m/%d}–{end:%Y/%m/%d}；"
                        f"本段 {day}，非新增獨立任務。"
                    ),
                })
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
            part['Description'] = row['Description'] + (
                f"\nEIP 跨日分段：完整區間 {start:%Y/%m/%d %H:%M}–{end:%Y/%m/%d %H:%M}；"
                f"本段 {cursor:%Y/%m/%d %H:%M}–{end_clock}，非新增獨立任務。"
            )
            result.append(part)
            cursor = stop
    if len(result) > 100:
        raise ValueError('跨日分段後超過 EIP 單次 100 筆上限，請分批處理。')
    return result


def restore_source_intervals(rows):
    """Recover receipt aliases only from complete, exactly reproducible segments.

    The download has the EIP template's 21 columns. Its generated final notice
    preserves the source interval without adding a private column. Rebuild all
    expected parts and compare every exported field before using that interval
    for legacy receipt checks; the notice alone cannot establish an alias.
    """
    groups = {}
    for row in rows:
        match = SEGMENT_NOTICE.search(row['Description'])
        all_day_match = ALL_DAY_SEGMENT_NOTICE.search(row['Description'])
        if row['All day event'] == '真' and all_day_match is not None:
            start = datetime.strptime(all_day_match['start'], '%Y/%m/%d')
            end = datetime.strptime(all_day_match['end'], '%Y/%m/%d')
            if start >= end:
                raise ValueError('跨日分段的完整區間無效，未匯入。')
            source = {**row, 'Start Date': start.strftime('%Y/%m/%d'),
                      'End Date': end.strftime('%Y/%m/%d'),
                      'Description': row['Description'][:all_day_match.start()]}
            key = tuple(source[k] for k in CALENDAR_HEADERS)
            groups.setdefault(key, (source, []))[1].append(row)
            continue
        if row['All day event'] != '假' or match is None:
            continue
        start = datetime.strptime(match['start'], '%Y/%m/%d %H:%M')
        end = datetime.strptime(match['end'], '%Y/%m/%d %H:%M')
        if start.date() >= end.date() or end <= start:
            raise ValueError('跨日分段的完整區間無效，未匯入。')
        source = {
            **row,
            'Start Date': start.strftime('%Y/%m/%d'),
            'End Date': end.strftime('%Y/%m/%d'),
            'Start Time': start.strftime('%H:%M:00'),
            'Start Time(24hr)': start.strftime('%H:%M'),
            'End Time': end.strftime('%H:%M:00'),
            'End Time(24hr)': end.strftime('%H:%M'),
            'Description': row['Description'][:match.start()],
        }
        key = tuple(source[k] for k in CALENDAR_HEADERS)
        groups.setdefault(key, (source, []))[1].append(row)
    for source, parts in groups.values():
        expected = import_segments([source])
        if sorted(map(segment_fields, parts)) != sorted(map(segment_fields, expected)):
            raise ValueError('跨日分段不完整或內容與完整區間不符，未匯入；請使用完整 CSV。')
        for part in parts:
            part['_source_interval'] = source
    return rows


def segment_fields(row):
    """Compare EIP cells while accepting equivalent clock display formats."""
    return tuple(
        canonical_time(row, k.removesuffix(' Time')) if k in ('Start Time', 'End Time')
        else row[k] for k in CALENDAR_HEADERS
    )


def decode_csv(payload: bytes, csv_format='plain') -> bytes:
    """Decode only a server-recorded format; legacy literal %2B stays literal."""
    if csv_format == 'plain':
        return payload
    if csv_format != CALENDAR_CSV_FORMAT:
        raise ValueError('行事曆 CSV 格式版本不支援，請重新產生。')
    return unquote_plus(payload.decode('utf-8-sig'), errors='strict').encode('utf-8-sig')


def direct_import_notice(payload):
    """Explain temporary all-day reminders without requiring another importer."""
    reader = csv.DictReader(io.StringIO(decode_csv(payload, CALENDAR_CSV_FORMAT).decode('utf-8-sig')))
    if any(row['Description'].startswith(MIDNIGHT_FALLBACK_HEADING) for row in reader):
        return MIDNIGHT_IMPORT_NOTICE
    return ''


def native_midnight_reminder(row):
    """Represent an unsupported zero-minute native clock as a labelled reminder."""
    if row['All day event'] != '假' or canonical_time(row, 'Start') != '上午12:00:00':
        return row
    if row['Start Date'] != row['End Date']:
        raise ValueError('零時相容提醒必須先依日期分段。')
    end = int(csv_minutes(canonical_time(row, 'End'), allow_end_of_day=True))
    hour, minute = divmod(end, 60)
    return {
        **row,
        'All day event': '真',
        'Start Time': '', 'Start Time(24hr)': '',
        'End Time': '', 'End Time(24hr)': '',
        'Description': (
            f'{MIDNIGHT_FALLBACK_HEADING}\n原定時段：{row["Start Date"]} 00:00–{hour:02d}:{minute:02d}\n\n'
            + row['Description']
        ),
    }


def restore_midnight_reminders(rows):
    """Retain exact original intervals for the existing versioned AI importer."""
    restored = []
    for row in rows:
        match = MIDNIGHT_FALLBACK.match(row['Description'])
        if match is None:
            restored.append(row)
            continue
        if (
            row['All day event'] != '真'
            or row['Start Date'] != match['date'] or row['End Date'] != match['date']
            or any(row[k] for k in ('Start Time', 'Start Time(24hr)', 'End Time', 'End Time(24hr)'))
        ):
            raise ValueError('零時相容提醒的日期、全天設定或欄位已變更，請核對原始 CSV。')
        restored.append({
            **row,
            'All day event': '假',
            'Start Time': '', 'Start Time(24hr)': '00:00',
            'End Time': '', 'End Time(24hr)': match['end'],
            'Description': row['Description'][match.end():],
        })
    return restored


def native_time(row, prefix):
    """The website's csv2mins adds 720 to PM hours, including 12 PM."""
    value = canonical_time(row, prefix)
    return value.replace('下午12:', '下午00:', 1) if value.startswith('下午12:') else value


def export_eip_csv(rows):
    """Produce the one download that Netask can import directly, without labels."""
    normalized = []
    for source in rows:
        row = {key: str(source.get(key, '')) for key in CALENDAR_HEADERS}
        for prefix in ('Start', 'End'):
            if row[f'{prefix} Date']:
                row[f'{prefix} Date'] = datetime.strptime(
                    row[f'{prefix} Date'], '%Y/%m/%d',
                ).strftime('%Y/%m/%d')
        minutes = automatic_duration_minutes()
        reserved = reserve_missing_end(row, minutes)
        if reserved is not row:
            reserved['Work Hour'] = f'{minutes / 60:g}'
            reserved['Description'] += '\n' + EIP_REVIEW_NOTICE
        row = reserved
        row = fit_calendar_subject(row)
        # Reserved transport headings in source prose cannot change an all-day
        # source event into a timed event when the versioned file is re-read.
        if row['Description'].startswith(MIDNIGHT_FALLBACK_HEADING):
            row['Description'] = '原文內容：\n' + row['Description']
        normalized.append(row)
    if len(normalized) > 100:
        raise ValueError('行程數量超過 CSV 上限，請分批處理。')
    unique = {}
    for row in normalized:
        unique.setdefault(csv_key(row), row)
    normalized = list(unique.values())
    # The real EIP importer accepts an overnight row but stores only its first
    # day. Split downloads as well as adapter transports into valid daily rows.
    normalized = [native_midnight_reminder(row) for row in import_segments(normalized)]
    output = io.StringIO(newline='')
    writer = csv.DictWriter(output, fieldnames=CALENDAR_HEADERS, quoting=csv.QUOTE_ALL)
    writer.writeheader()
    for row in normalized:
        converted = {key: row[key] for key in CALENDAR_HEADERS}
        for prefix in ('Start', 'End'):
            converted[f'{prefix} Time'] = native_time(row, prefix)
        writer.writerow(converted)
    payload = protect_eip_csv(output.getvalue().encode('utf-8-sig'))
    # Validate the actual download before storage, including complete segments.
    parse_csv(payload, csv_format=CALENDAR_CSV_FORMAT)
    return payload


def parse_csv(payload: bytes, *, default_duration_minutes=None, csv_format='plain'):
    if default_duration_minutes is not None and default_duration_minutes not in (
        60,
        120,
    ):
        raise ValueError('預留時間僅允許 60 或 120 分鐘。')
    reader = csv.DictReader(io.StringIO(decode_csv(payload, csv_format).decode('utf-8-sig')))
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
        canonical_time(row, 'Start')
        canonical_time(row, 'End')
        # Add only the configured, explicitly labelled reservation.
        if default_duration_minutes is not None:
            row = reserve_missing_end(row, default_duration_minutes)
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
        # Netask pads month/day in CM_Import. Compare and journal the same
        # canonical dates even when a downloaded CSV used 2026/10/7.
        row['Start Date'] = start.strftime('%Y/%m/%d')
        row['End Date'] = end.strftime('%Y/%m/%d')
        converted = fit_calendar_subject(row)
        rows[index] = converted
    if csv_format == CALENDAR_CSV_FORMAT:
        rows = restore_midnight_reminders(rows)
    rows = import_segments(restore_source_intervals(rows))
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
