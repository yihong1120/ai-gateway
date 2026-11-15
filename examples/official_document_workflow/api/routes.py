"""FastAPI routes for the EIP document workflow."""
from __future__ import annotations

import asyncio
import os
import secrets
import time
import uuid
from contextlib import AsyncExitStack
from dataclasses import asdict
from functools import partial
from pathlib import Path
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Header
from fastapi import HTTPException
from fastapi import Query
from fastapi import Request
from fastapi import Response
from fastapi.responses import FileResponse
from fastapi.responses import RedirectResponse
from fastapi.routing import APIRoute
from open_webui.utils.auth import get_current_user
from open_webui.utils.auth import get_verified_user

from .schemas import AttachmentRequest
from .schemas import ConfirmRequest
from .schemas import DocumentsRequest
from .schemas import LoginRequest
from .schemas import PreviewRequest
from .schemas import ProcessRequest
from examples.official_document_workflow.application import integration
from examples.official_document_workflow.application.manual_job import ManualJob
from examples.official_document_workflow.infrastructure.eip.calendar_identity import (
    prepare_source_replay,
)
from examples.official_document_workflow.infrastructure.eip.calendar_sync import check_existing_events
from examples.official_document_workflow.infrastructure.eip.calendar_sync import verified_events
from examples.official_document_workflow.infrastructure.eip.calendar_sync import write_events
from examples.official_document_workflow.infrastructure.eip.netask import Netask
from examples.official_document_workflow.infrastructure.eip.sessions import manual_session
from examples.official_document_workflow.infrastructure.eip.task_times import (
    document_recorded_at,
)
from examples.official_document_workflow.infrastructure.storage.state import Link
from examples.official_document_workflow.runtime import current_resources
from examples.shared.runtime.io import run_sync

FRONTEND = Path(__file__).resolve().parents[1] / 'frontend'


class BrowserPageRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request: Request):
            try:
                return await original(request)
            except HTTPException as exc:
                if exc.status_code != 401 or request.url.path not in {'/eip', '/eip/'}:
                    raise
                destination = '/eip'
                try:
                    destination += '?csv=' + str(uuid.UUID(request.query_params['csv']))
                except (KeyError, ValueError):
                    pass
                return RedirectResponse(
                    '/auth?redirect=' + quote(destination, safe=''), status_code=303,
                )

        return handler


router = APIRouter(route_class=BrowserPageRoute)


def require_same_origin(
    request: Request,
    origin: Annotated[str | None, Header()] = None,
    action: Annotated[str | None, Header(alias='X-EIP-Action')] = None,
) -> None:
    expected = os.getenv('EIP_WEBUI_ORIGIN', str(request.base_url).rstrip('/'))
    if origin != expected or action != '1':
        raise ValueError('請從本站 EIP 連結頁操作。')


actions = APIRouter(
    prefix='/api/v1/eip',
    dependencies=[Depends(require_same_origin)],
)


@router.get('/eip')
@router.get('/eip/', include_in_schema=False)
async def dashboard(user=Depends(get_current_user)):
    if user.role not in {'user', 'admin'}:
        raise ValueError('你的 Open WebUI 帳號尚未獲准使用 EIP 工作台，請聯絡管理員。')
    response = FileResponse(FRONTEND / 'index.html', media_type='text/html')
    response.headers['Cache-Control'] = 'no-store'
    response.headers['Content-Security-Policy'] = (
        "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
    )
    return response


@router.get('/eip/assets/{filename}', include_in_schema=False)
async def frontend_asset(filename: str):
    if filename not in {'app.css', 'app.js'}:
        raise ValueError('找不到前端資源。')
    return FileResponse(FRONTEND / 'assets' / filename, headers={'Cache-Control': 'no-store'})


@router.get('/api/v1/eip/status')
async def status(
    response: Response,
    user=Depends(get_verified_user),
    automatic_page: int = Query(1, ge=1, le=1000000),
    automatic_page_size: int = Query(5, ge=1, le=20),
):
    resources = current_resources()
    state = resources.state
    response.headers['Cache-Control'] = 'no-store'
    await state.sweep()
    link = state.links.get(user.id)
    storage = await run_sync(lambda: resources.store)
    config = await run_sync(storage.account, user.id)
    automatic = (
        None
        if not config
        else {
            k: config[k]
            for k in (
                'enabled',
                'identity',
                'next_run',
                'last_run',
                'message',
                'scan_start',
                'tracking_reminders_enabled',
            )
        }
    )
    results = []
    tasks, pagination = await run_sync(storage.task_page, user.id, automatic_page, automatic_page_size)
    for task in tasks:
        p = task['payload']
        results.append(
            {
                'id': task['id'],
                'phase': task['phase'],
                'subject': p.get('document', {}).get('subject', ''),
                'eip_recorded_at': document_recorded_at(p.get('document', {})),
                'first_detected_at': task.get('first_detected_at'),
                'last_processed_at': task.get('last_processed_at'),
                'notice': p.get('notice', '執行中／等待下一輪'),
                'errors': (
                    p.get('output', {}).get('errors', [])
                    if task['phase'] in ('needs_review', 'done')
                    else []
                ),
                'calendar_pending': p.get('output', {}).get('calendar_pending', []),
                'validation_issues': (
                    ([p['input_issue']] if p.get('input_issue', {}).get('reason') else [])
                    + p.get('output', {}).get('validation_issues', [])
                ),
                'date_estimates': p.get('output', {}).get('date_estimates', []),
                'tracking_reminders': p.get('output', {}).get('tracking_reminders', []),
                'files': p.get('output', {}).get('files', []),
                'calendar_sync': p.get('import'),
                'calendar_reconciliation': p.get('calendar_reconciliation', []),
            },
        )
    return {
        'automatic': automatic,
        'automatic_results': results,
        'automatic_pagination': pagination,
        'session': {'saved_credentials': bool(config)},
        'linked': bool(link),
        'account': link.account if link else None,
        'expires': link.expires if link else None,
        'jobs': [
            {'id': k, 'status': j['status'], 'progress': j.get('progress', '')}
            for k, j in state.jobs.items()
            if j['owner'] == user.id
        ],
    }


@router.get('/api/v1/eip/health')
async def workflow_health(user=Depends(get_verified_user)):
    if user.role != 'admin':
        raise HTTPException(403, '需要管理員權限。')
    resources = current_resources()
    return {'workers': {name: asdict(health) for name, health in resources.health.items()}}


@actions.post('/link')
async def login(data: LoginRequest, user=Depends(get_verified_user)):
    resources = current_resources()
    state = resources.state
    storage = await run_sync(lambda: resources.store)
    await state.sweep()
    state.allow_login(user.id)
    account, password = data.account, data.password
    if len(state.links) >= 64 and user.id not in state.links:
        raise ValueError('連線數已滿，請稍後再試。')
    adapter = Netask()
    async with AsyncExitStack() as cleanup, state.session_lock(user.id):
        cleanup.push_async_callback(adapter.close)
        await adapter.login(account, password)
        persist = data.enable_auto if data.save_credentials is None else data.save_credentials
        if persist:
            await run_sync(storage.bind, user.id, adapter.user_id, account, password, enable_auto=data.enable_auto)
        else:
            await run_sync(storage.pause, user.id, forget=True)
        config = await run_sync(storage.account, user.id)
        await state.unlink(user.id)
        state.links[user.id] = Link(
            adapter,
            account,
            binding_generation=config['generation'] if config else None,
        )
        cleanup.pop_all()
    return {'linked': True, 'expires': state.links[user.id].expires}


@actions.post('/unlink')
async def unlink(user=Depends(get_verified_user)):
    resources = current_resources()
    state = resources.state
    storage = await run_sync(lambda: resources.store)
    async with state.session_lock(user.id):
        await run_sync(storage.pause, user.id, forget=True)
        await state.unlink(user.id)
    return {'linked': False}


@actions.post('/automatic/pause')
async def pause_automatic(user=Depends(get_verified_user)):
    resources = current_resources()
    state = resources.state
    storage = await run_sync(lambda: resources.store)
    async with state.session_lock(user.id):
        await run_sync(storage.pause, user.id)
    return {'paused': True}


@actions.post('/session/restore')
async def restore_session(user=Depends(get_verified_user)):
    resources = current_resources()
    state = resources.state
    storage = await run_sync(lambda: resources.store)
    async with manual_session(
        user.id, state, storage, factory=Netask, force=True,
    ) as current:
        return {'linked': True, 'expires': current.link.expires}


@actions.post('/documents')
async def documents(data: DocumentsRequest, user=Depends(get_verified_user)):
    resources = current_resources()
    state = resources.state
    storage = await run_sync(lambda: resources.store)
    async with manual_session(
        user.id, state, storage, factory=Netask,
    ) as current:
        rows = await current.read(
            'documents',
            data.folder,
            data.start.isoformat(),
            data.end.isoformat(),
            data.page,
        )
        link = current.link
        if len(link.documents) + len(rows) > 1000:
            link.documents.clear()
            link.attachments.clear()
        result = []
        for row in rows:
            key = secrets.token_urlsafe(24)
            link.documents[key] = row
            result.append(
                {'handle': key, 'subject': row['subject'], 'date': row['date']},
            )
    return {'documents': result, 'page': data.page, 'has_more': len(rows) == 20}


@actions.post('/attachments')
async def attachments(data: AttachmentRequest, user=Depends(get_verified_user)):
    resources = current_resources()
    state = resources.state
    storage = await run_sync(lambda: resources.store)
    async with manual_session(
        user.id, state, storage, factory=Netask,
    ) as current:
        link = current.link
        doc = link.documents.get(data.handle)
        if not doc or state.link(user.id) is not link:
            raise ValueError('請重新查詢目前帳號的文件。')
        rows = await current.read('attachments', doc['id'], doc['folder'])
        if len(link.attachments) + len(rows) > 500:
            link.attachments.clear()
        result = []
        for row in rows:
            key = secrets.token_urlsafe(24)
            link.attachments[key] = {**row, 'document': doc}
            result.append({'handle': key, 'name': row['name']})
    return {'attachments': result}


@actions.post('/process')
async def start_job(data: ProcessRequest, user=Depends(get_verified_user)):
    resources = current_resources()
    state = resources.state
    storage = await run_sync(lambda: resources.store)
    async with manual_session(
        user.id, state, storage, factory=Netask,
    ) as current:
        link = current.link
        if any(handle not in link.attachments for handle in data.handles):
            raise ValueError('附件已過期，請重新查詢。')
        generation = link.generation
        selected = [dict(link.attachments[handle]) for handle in data.handles]
    await state.sweep()
    if len(state.jobs) >= 64 or any(
        j['owner'] == user.id and j['status'] in {'queued', 'running'}
        for j in state.jobs.values()
    ):
        raise ValueError('已有公文工作執行中，請等待完成。')
    key = secrets.token_urlsafe(24)
    job = {
        'owner': user.id,
        'expires': time.time() + 86400,
        'status': 'queued',
        'progress': '等待公文處理資源',
        'result': '',
    }
    state.jobs[key] = job
    worker = ManualJob(user.id, generation, selected, data.instruction, job, state, storage)
    task = asyncio.create_task(worker.run())
    state.tasks.add(task)
    task.add_done_callback(worker.finished)
    return {'job_id': key}


@router.get('/api/v1/eip/jobs/{job_id}')
async def get_job(job_id: str, user=Depends(get_verified_user)):
    resources = current_resources()
    state = resources.state
    job = state.owned(state.jobs, job_id, user.id)
    return {k: v for k, v in job.items() if k not in {'owner', 'expires'}}


@actions.post('/preview')
async def preview(data: PreviewRequest, user=Depends(get_verified_user)):
    resources = current_resources()
    state = resources.state
    journal = await run_sync(lambda: resources.journal)
    storage = await run_sync(lambda: resources.store)
    rows, payload = await integration.read_csv(user.id, data.file_id)
    await state.sweep()
    if len(state.previews) >= 64:
        raise ValueError('待確認預覽過多，請稍後再試。')
    async with manual_session(
        user.id, state, storage, factory=Netask,
    ) as current:
        staged = await current.read('stage', payload)
        link = current.link
        events = await run_sync(verified_events, rows, staged, link.adapter.user_id, journal)
        await prepare_source_replay(
            events,
            link.adapter.user_id,
            journal,
            partial(current.read, 'stage'),
            integration.read_csv,
        )
        await check_existing_events(
            events, journal, partial(current.read, 'calendar_import_matches'),
            reviewer=partial(integration.review_calendar_matches, owner=user.id),
        )
        token = secrets.token_urlsafe(32)
        state.previews[token] = {
            'owner': user.id,
            'expires': time.time() + 600,
            'generation': link.generation,
            'events': events,
        }
    statuses = await run_sync(journal.statuses, [e['key'] for e in events])
    return {
        'token': token,
        'account': link.account,
        'expires': state.previews[token]['expires'],
        'visibility': '新增行程僅限目前 EIP 帳號本人（不分享、不寄信）；既有行程維持原設定',
        'limitations': 'EIP 現有 CSV 匯入介面不讀取 Work Hour、Report Status、Completed；這些值仍保留在下載 CSV，但不會由本次匯入寫入。自訂欄位含逗號或換行時會停止匯入。',
        'events': [
            {
                'row': {k: v for k, v in e['row'].items() if not k.startswith('_')},
                'status': statuses.get(e['key']) or e.get('existing_match', {}).get('status', 'new'),
                'reason': e.get('existing_match', {}).get('reason', ''),
            }
            for e in events
        ],
    }


@actions.post('/confirm')
async def confirm(data: ConfirmRequest, user=Depends(get_verified_user)):
    resources = current_resources()
    state = resources.state
    journal = await run_sync(lambda: resources.journal)
    storage = await run_sync(lambda: resources.store)
    preview = state.owned(state.previews, data.token, user.id)
    async with manual_session(
        user.id, state, storage, factory=Netask,
    ) as current:
        link = current.link
        if state.link(user.id) is not link or preview['generation'] != link.generation:
            raise ValueError('EIP 登入已變更，請重新預覽。')
        return await write_events(
            link.adapter, preview['events'], journal,
            reviewer=partial(integration.review_calendar_matches, owner=user.id),
        )


router.include_router(actions)
