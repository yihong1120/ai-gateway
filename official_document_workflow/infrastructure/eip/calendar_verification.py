"""Validate serialized writes, then read the persisted owned event and detail."""
from __future__ import annotations

import re
from datetime import date

from . import calendar_read


def validate_wire(line, user_id):
    fields = line.split(',')
    if (
        not user_id.isdecimal() or len(fields) != 32 or any(c in line for c in '\r\n\x00')
        or fields[14] != '1' or fields[-3:] != [f'Ru{user_id}', '', f'Ru{user_id}']
    ):
        raise ValueError('私人行事曆寫入序列不符，未送出。')
    return fields


def verify_detail(detail, fields, user_id, event_id, *, project_id='', sharing=None):
    if not detail or len(detail[0]) < 43:
        raise ValueError('已送出行程，但詳細資料格式不符，請人工核對；未重送。')
    row = detail[0]
    match = re.fullmatch(r'new Date\((\d{4}),\s*(\d+),\s*(\d+),\s*0,\s*0,\s*0\)', str(row[9]))
    day = date(int(match[1]), int(match[2])+1, int(match[3])).isoformat() if match else ''
    same = (
        row[0] == event_id and row[2] == user_id and row[4] == project_id and row[13] == fields[20]
        and (row[15] in ('', f'Ru{user_id}') if sharing is None else set(row[15].split(',')) == set(sharing)) and day == fields[1]
        and calendar_read.unescape(row[8]) == calendar_read.unescape(fields[0])
        and calendar_read.unescape(row[14]).replace('\r\n', '\n') == calendar_read.unescape(fields[19]).replace('\r\n', '\n')
        and row[10] == fields[5]
    )
    if fields[5] == '0':
        same = same and row[11] == fields[2] and row[12] == fields[4]
    if not same:
        raise ValueError('已送出行程，但詳細資料／時間／私人設定核對不符，請人工核對；未重送。')
    return {'event_id': event_id, 'detail_verified': True}


async def verify_import(client, line):
    fields = validate_wire(line, client.user_id)
    subject = calendar_read.unescape(fields[0])
    listing = await client.rpc('List', ap_name='cm', start_date=fields[1], end_date=fields[1], __tag='event_list', strict_json=True)
    if len(listing) > 501:
        raise ValueError('匯入後清單過大，請人工核對；未重送。')
    candidates = []
    for row in listing[1:]:
        if len(row) not in (20, 21):
            raise ValueError('匯入後清單格式不符，請人工核對；未重送。')
        if (
            row[0] == client.user_id and calendar_read.unescape(row[6]) == subject and row[8] == fields[5]
            and (fields[5] == '1' or row[9:11] == fields[2:5:2])
        ):
            candidates.append(row[2])
    if len(candidates) != 1:
        raise ValueError('匯入後無法確認唯一行程，請人工核對；未重送。')
    event_id = candidates[0]
    return verify_detail(await client.calendar_detail(event_id), fields, client.user_id, event_id)
