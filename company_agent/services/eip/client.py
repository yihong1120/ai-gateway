"""EIP queries and drafts. Authentication and confirmed writes stay server-side."""
from __future__ import annotations
from examples.shared.runtime.io import run_sync

import json
import re
import uuid


def source(ctx, handle, kind):
    with ctx.store.db() as connection:
        row = connection.execute(
            'SELECT payload FROM eip_sources WHERE owner=%s AND job=%s AND handle=%s AND revision=%s',
            (ctx.owner, ctx.job, handle, ctx.settings.eip_binding_revision),
        ).fetchone()
    if not row:
        raise ValueError('EIP 來源不屬於本工作／目前帳號。')
    value = json.loads(row[0])
    if value.get('kind', 'document') != kind:
        raise ValueError('EIP 來源 handle 類型不符。')
    return value


def view_source(store, owner, job, handle, revision):
    if not re.fullmatch(r'[a-f0-9]{32}', handle):
        raise ValueError('EIP 文件連結無效。')
    with store.db() as connection:
        row = connection.execute(
            'SELECT payload FROM eip_sources WHERE owner=%s AND job=%s AND handle=%s AND revision=%s',
            (owner, job, handle, revision),
        ).fetchone()
    if not row:
        raise ValueError('EIP 文件連結已失效或不屬於目前帳號。')
    value = json.loads(row[0])
    if value.get('kind', 'document') != 'document':
        raise ValueError('EIP 文件連結格式不符。')
    return value


def remember(ctx, payload):
    handle = uuid.uuid4().hex
    with ctx.store.db() as connection:
        connection.execute(
            'INSERT INTO eip_sources VALUES(%s,%s,%s,%s,%s)',
            (
                ctx.owner,
                ctx.job,
                handle,
                ctx.settings.eip_binding_revision,
                json.dumps(payload, ensure_ascii=False),
            ),
        )
    return handle


async def view_document(store, owner, job, handle):
    from examples.official_document_workflow.infrastructure.storage.automation_store import store as workflow_store
    from examples.official_document_workflow.infrastructure.eip.sessions import manual_session
    from examples.official_document_workflow.runtime import current_resources
    state = current_resources().state

    account_store = await run_sync(workflow_store,)
    account = await run_sync(account_store.account,owner)
    if not account:
        raise ValueError('EIP 尚未綁定，請至帳號與連線重新連結。')
    document = await run_sync(view_source,store, owner, job, handle, account['generation'])
    async with manual_session(owner, state, account_store) as current:
        detail = await current.read(
            'document_detail', document['id'], document['folder'],
        )
    folder_names = {
        'inbox': '等待我處理的文件',
        'sign': '簽核紀錄',
        'notify': '通知紀錄',
        'sent': '我送出的簽核',
    }
    return {
        'subject': document['subject'],
        'date': document['date'],
        'folder_name': folder_names[document['folder']],
        'due_date': document.get('due_date', ''),
        'flow_number': document.get('flow_number', ''),
        'status': document.get('status', ''),
        'fields': detail['fields'],
        'attachments': [
            {
                'name': attachment['name'],
                'url': f"/agents/eip/{job}/{handle}/attachments/{index}",
            }
            for index, attachment in enumerate(detail['attachments'])
        ],
    }


async def view_attachment(store, owner, job, handle, index):
    from examples.official_document_workflow.infrastructure.storage.automation_store import store as workflow_store
    from examples.official_document_workflow.infrastructure.eip.sessions import manual_session
    from examples.official_document_workflow.runtime import current_resources
    state = current_resources().state

    account_store = await run_sync(workflow_store,)
    account = await run_sync(account_store.account,owner)
    if not account:
        raise ValueError('EIP 尚未綁定，請至帳號與連線重新連結。')
    document = await run_sync(view_source,store, owner, job, handle, account['generation'])
    async with manual_session(owner, state, account_store) as current:
        attachments = await current.read(
            'attachments', document['id'], document['folder'],
        )
        if not 0 <= index < len(attachments):
            raise ValueError('EIP 附件不存在或文件內容已更新。')
        attachment = attachments[index]
        payload = await current.read(
            'download', attachment['url'], attachment['name'],
        )
    return attachment['name'], payload


async def binding(ctx):
    if getattr(ctx, 'authorize_resources', None) is not None:
        await ctx.authorize_resources()
    if not ctx.settings.enable_eip_read:
        raise ValueError('此入口未啟用 EIP 查詢。')
    from examples.identity.adapters.webui import verified_user
    from examples.official_document_workflow.infrastructure.storage.automation_store import store as workflow_store

    await verified_user(ctx.owner)
    db = await run_sync(workflow_store,)
    account = await run_sync(db.account,ctx.owner)
    if (
        not account
        or not ctx.settings.eip_binding_revision
        or account['generation'] != ctx.settings.eip_binding_revision
    ):
        raise ValueError('EIP 尚未綁定或綁定已變更；請至帳號與連線確認後另開新工作。')
    return db


async def query(ctx, name, args, call_id):
    from examples.official_document_workflow.infrastructure.eip import calendar_read
    from examples.official_document_workflow.infrastructure.eip.sessions import manual_session
    from examples.official_document_workflow.runtime import current_resources
    state = current_resources().state

    db = await binding(ctx)
    event = await run_sync(source,ctx, args.handle, 'calendar_event') if name == 'eip_calendar_detail' else None
    if event is not None:
        call_id = 'eip_calendar_detail:' + args.handle
    cached = await run_sync(ctx.store.begin_tool,
        ctx.owner,
        ctx.job,
        call_id,
        name,
        args.model_dump(),
        limit_names={'eip_query', 'eip_attachments', 'eip_calendar_query', 'eip_calendar_detail'},
        limit=24,
    )
    if cached is not None:
        return cached
    async with manual_session(ctx.owner, state, db) as current:
        if name == 'eip_calendar_query':
            rows = await current.read('calendar', args.start, args.end)
            events = [
                {
                    **{k: v for k, v in row.items() if k != 'id'},
                    'handle': await run_sync(remember,ctx, {**row, 'kind': 'calendar_event'}),
                }
                for row in rows
            ]
            output = {
                'source': 'eip_calendar',
                'events': events,
                'count': len(rows),
                'scope': {
                    'start': args.start,
                    'end': args.end,
                    'visibility': 'personal_and_shared',
                    'timezone': 'Asia/Taipei',
                },
                'notice': '行事曆清單，尚未讀取詳細內容或附件。需要單筆詳細內容時，用該筆 handle 呼叫 eip_calendar_detail；不可據清單宣稱沒有內文或附件。日期為該次行程顯示日；重複／跨日事項可能分日列出。未新增或修改行程。',
            }
        elif name == 'eip_calendar_detail':
            detail = calendar_read.detail(await current.read('calendar_detail', event['id']), event['id'])
            output = {
                'source': 'eip_calendar_detail',
                'event': {
                    **{k: v for k, v in event.items() if k not in {'kind', 'id'}},
                    'subject': detail['subject'], 'description': detail['description'],
                },
                'attachments': [
                    {
                        'name': attachment['name'],
                        'handle': await run_sync(remember,ctx, {'kind': 'attachment', 'doc': event, 'attachment': attachment}),
                    }
                    for attachment in detail['attachments']
                ],
                'notice': '已讀取本筆行程的事項內容與附件清單，尚未讀取附件正文。需要附件內容才使用 eip_read_attachment。行事曆時間與原文若有差異須分別說明；通知內容不代表會後紀錄。未新增或修改行程。',
            }
        elif name == 'eip_query':
            rows = await current.read('documents', args.folder, args.start, args.end, args.page)
            if len(rows) > 20:
                raise ValueError('EIP 清單超過單頁上限。')
            result = []
            for row in rows:
                handle = await run_sync(remember,ctx, row)
                view_url = f"/agents/eip/{ctx.job}/{handle}"
                result.append(
                    {
                        'handle': handle,
                        'subject': row['subject'],
                        'date': row['date'],
                        'folder': row['folder'],
                        'view_url': view_url,
                        'view_link': document_link(row['subject'], view_url),
                    },
                )
            output = {
                'documents': result,
                'page': args.page,
                'has_more': len(rows) == 20,
                'scope': {'folder': args.folder, 'start': args.start, 'end': args.end},
                'notice': '只列本帳號此文件匣及日期範圍；文件匣日期不是公文期限。未讀附件，不代表已分析公文。',
            }
        else:
            doc = await run_sync(source,ctx, args.handle, 'document')
            rows = await current.read('attachments', doc['id'], doc['folder'])
            if len(rows) > 50:
                raise ValueError('附件清單超過 50 份上限。')
            output = {
                'attachments': [
                    {
                        'name': r['name'],
                        'handle': await run_sync(remember,
                            ctx, {'kind': 'attachment', 'doc': doc, 'attachment': r},
                        ),
                    }
                    for r in rows
                ],
                'workspace': '/agents',
                'notice': '只列附件，尚未分析。查內容用 eip_read_attachment；明確要求產製 PDF/Word/CSV 才用 eip_process_attachments。可能更新 EIP 閱讀紀錄。',
            }
    await binding(ctx)
    if len(json.dumps(output, ensure_ascii=False)) > 18000:
        raise ValueError('EIP 結果過大，未截斷宣稱完整。')
    await run_sync(ctx.store.finish_tool,ctx.owner, ctx.job, call_id, output)
    return output


async def attachment_list(current, document):
    if document.get('kind') == 'calendar_event':
        from examples.official_document_workflow.infrastructure.eip import calendar_read

        rows = await current.read('calendar_detail', document['id'])
        return calendar_read.detail(rows, document['id'])['attachments']
    return await current.read('attachments', document['id'], document['folder'])


def document_link(subject, view_url):
    label = subject.replace('\\', '\\\\').replace('[', '\\[').replace(']', '\\]')
    return f"[{label}]({view_url})"
