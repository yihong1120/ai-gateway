"""Structured calendar drafts. Only the authenticated browser can commit."""
from __future__ import annotations

import csv
import hashlib
import io
import json
from datetime import date

import company_agent.services.eip.client as _client_imports
from examples.official_document_workflow.domain.calendar.csv_schema import (
    CALENDAR_HEADERS,
)
from examples.official_document_workflow.infrastructure.eip.calendar_csv import (
    parse_csv,
)


async def prepare(ctx, args):

    await _client_imports.binding(ctx)
    canonical = sorted(
        (e.model_dump() for e in args.events), key=lambda e: json.dumps(e, sort_keys=True),
    )
    digest = hashlib.sha256(
        json.dumps(canonical, sort_keys=True, ensure_ascii=False).encode(),
    ).hexdigest()
    key = 'calendar_prepare:' + digest
    cached = ctx.store.begin_tool(
        ctx.owner,
        ctx.job,
        key,
        'eip_calendar_prepare',
        {'events': canonical},
        limit_names={'eip_calendar_prepare'},
        limit=4,
    )
    if cached is not None:
        return cached
    headers = CALENDAR_HEADERS
    stream = io.StringIO(newline='')
    writer = csv.DictWriter(stream, fieldnames=headers, quoting=csv.QUOTE_ALL)
    writer.writeheader()
    for event in args.events:
        row = dict.fromkeys(headers, '')
        row.update(
            {
                'Subject': event.subject,
                'Start Date': event.start_date.replace('-', '/'),
                'End Date': event.end_date.replace('-', '/'),
                'Start Time(24hr)': event.start_time,
                'End Time(24hr)': event.end_time,
                'All day event': '真' if event.all_day else '假',
                'Private': '真',
                'Description': event.description,
                'Priority': '普通',
            },
        )
        writer.writerow(row)
    payload = stream.getvalue().encode('utf-8-sig')
    parse_csv(payload)
    await _client_imports.binding(ctx)
    artifact = ctx.store.artifact(
        ctx.owner, ctx.job, '行事曆待確認_' + digest[:10] + '.csv', payload,
    )
    with ctx.store.db() as db:
        db.execute(
            'INSERT INTO eip_calendar_sources(owner,job,artifact,revision) VALUES(%s,%s,%s,%s)',
            (ctx.owner, ctx.job, artifact['id'], ctx.settings.eip_binding_revision),
        )
    result = {
        'status': 'prepared_not_written',
        'files': [artifact],
        'events': [
            {
                **e.model_dump(),
                'start_weekday': '星期'
                + '一二三四五六日'[date.fromisoformat(e.start_date).weekday()],
                'end_weekday': '星期' + '一二三四五六日'[date.fromisoformat(e.end_date).weekday()],
            }
            for e in args.events
        ],
        'timezone': 'Asia/Taipei',
        'workspace': '/agents',
        'notice': '僅建立待確認 CSV，尚未新增行程。請在工作台點預覽行事曆匯入並本人確認；僅私人、不分享不寄信。不等於已設定系統通知。',
    }
    ctx.store.finish_tool(ctx.owner, ctx.job, key, result)
    return result
