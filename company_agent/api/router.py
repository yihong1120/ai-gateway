"""Authenticated same-origin task/download/approval endpoints."""
from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import uuid
from contextlib import asynccontextmanager
from importlib.resources import files
from urllib.parse import quote

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from fastapi.responses import Response
from starlette.background import BackgroundTask
from starlette.responses import StreamingResponse

from company_agent.api.http import active
from company_agent.api.http import PrivateRoute
from company_agent.api.identity import router as identity_router
from company_agent.api.service import configured_service
from company_agent.api.service import service
from company_agent.runtime.context import verified_user
from company_agent.runtime.models import close_model

router = APIRouter(route_class=PrivateRoute)
router.include_router(identity_router)


async def data(request):
    expected = os.getenv('EIP_WEBUI_ORIGIN', str(request.base_url).rstrip('/'))
    if request.headers.get('origin') != expected or request.headers.get('x-company-action') != '1':
        raise HTTPException(403, '請從本站工作頁操作。')
    payload = bytearray()
    async for chunk in request.stream():
        payload.extend(chunk)
        if len(payload) > 16000:
            raise HTTPException(413, '請求過大。')
    value = json.loads(payload)
    if not isinstance(value, dict):
        raise ValueError()
    return value


@router.get('/agents')
@router.get('/connections')
@router.get('/connections/', include_in_schema=False)
@router.get('/connections/app.js')
@router.get('/connections/app.css')
@router.get('/agents/app.js')
@router.get('/agents/app.css')
@router.get('/agents/eip-document.js')
@router.get('/agents/eip-document.css')
async def ui_resource(request: Request, user=Depends(active)):
    filename, media_type = {
        '/agents': ('web_dashboard.html', 'text/html'),
        '/connections': ('connections.html', 'text/html'),
        '/connections/': ('connections.html', 'text/html'),
        '/connections/app.js': ('connections.js', 'application/javascript'),
        '/agents/app.js': ('web_dashboard.js', 'application/javascript'),
        '/connections/app.css': ('web_dashboard.css', 'text/css'),
        '/agents/app.css': ('web_dashboard.css', 'text/css'),
        '/agents/eip-document.js': ('eip_document.js', 'application/javascript'),
        '/agents/eip-document.css': ('eip_document.css', 'text/css'),
    }[request.url.path]
    response = Response(
        content=files('company_agent.ui').joinpath(filename).read_bytes(), media_type=media_type,
    )
    if media_type == 'text/html':
        response.headers['Content-Security-Policy'] = (
            "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
        )
    return response


@router.get('/agents/eip/{job}/{handle}')
async def eip_document_page(user=Depends(active)):
    response = Response(
        content=files('company_agent.ui').joinpath('eip_document.html').read_bytes(),
        media_type='text/html',
    )
    response.headers['Content-Security-Policy'] = (
        "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
        "frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
    )
    return response


@router.get('/agents/eip/{job}/{handle}/data')
async def eip_document_data(job: str, handle: str, user=Depends(active)):
    from company_agent.services.eip.client import view_document

    return await view_document(service().queue.store, user.id, job, handle)


@router.get('/agents/eip/{job}/{handle}/attachments/{index}')
async def eip_document_attachment(job: str, handle: str, index: int, user=Depends(active)):
    from company_agent.services.eip.client import view_attachment

    name, payload = await view_attachment(service().queue.store, user.id, job, handle, index)
    media_type = mimetypes.guess_type(name)[0] or 'application/octet-stream'
    disposition = 'inline' if media_type == 'application/pdf' else 'attachment'
    return StreamingResponse(
        iter(lambda: payload.read(64 * 1024), b''),
        media_type=media_type,
        headers={'Content-Disposition': disposition + "; filename*=UTF-8''" + quote(name, safe='')},
        background=BackgroundTask(payload.close),
    )


@router.get('/agents/jobs')
async def jobs(user=Depends(active)):
    return {'jobs': await asyncio.to_thread(service().queue.list, user.id)}


@router.post('/agents/calendar/preview')
async def calendar_preview(request: Request, user=Depends(active)):
    value = await data(request)
    if set(value) != {'artifact_id'} or not isinstance(value['artifact_id'], str):
        raise ValueError('請選擇行事曆 CSV。')
    from company_agent.services.eip.calendar import preview

    return await preview(service().queue.store, user.id, value['artifact_id'])


@router.post('/agents/calendar/confirm')
async def calendar_confirm(request: Request, user=Depends(active)):
    value = await data(request)
    if (
        set(value) != {'token', 'confirm'}
        or value['confirm'] is not True
        or not isinstance(value['token'], str)
    ):
        raise ValueError('需要本人明確確認行事曆。')
    from company_agent.services.eip.calendar import confirm

    return await confirm(service().queue.store, user.id, value['token'])


@router.get('/agents/nas')
async def nas_status(user=Depends(active)):
    from company_agent.services.files.nas import NASVault, endpoint

    configured = bool(os.getenv('COMPANY_AGENT_NAS_URL'))
    if configured:
        endpoint()
    return {**NASVault(configured_service().queue.store).status(user.id), 'configured': configured}


@router.post('/agents/nas/bind')
async def nas_bind(request: Request, user=Depends(active)):
    value = await data(request)
    if set(value) != {'account', 'password', 'consent'} or value['consent'] is not True:
        raise ValueError('請確認個人帳號、加密保存及所需文件雲端分析的同意事項。')
    from company_agent.services.files.nas import NASVault

    vault = NASVault(configured_service().queue.store)
    result = await asyncio.to_thread(vault.bind, user.id, value['account'], value['password'])
    vault.store.audit(user.id, None, 'nas_bind', 'done')
    return result


@router.post('/agents/nas/unlink')
async def nas_unlink(request: Request, user=Depends(active)):
    if await data(request) != {}:
        raise ValueError('解除綁定不接受其他身分或參數。')
    from company_agent.services.files.nas import NASVault

    vault = NASVault(configured_service().queue.store)
    await asyncio.to_thread(vault.unlink, user.id)
    vault.store.audit(user.id, None, 'nas_unlink', 'done')
    return vault.status(user.id)


@router.post('/agents/nas/test')
async def nas_test(request: Request, user=Depends(active)):
    if await data(request) != {}:
        raise ValueError('測試連線不接受身分或連線參數。')
    from company_agent.services.files.nas import NASVault

    vault = NASVault(configured_service().queue.store)
    status = vault.status(user.id)

    return {
        **status,
        'shares': await asyncio.to_thread(read_nas_shares, vault, user.id, status['revision']),
    }


@router.get('/agents/jobs/{key}')
async def job(key: str, user=Depends(active)):
    return await asyncio.to_thread(service().result, user.id, key)


@router.get('/agents/artifacts/{key}')
async def artifact(key: str, user=Depends(active)):
    name, sha, payload = service().queue.store.read_artifact(user.id, key)
    return Response(
        payload,
        media_type='application/octet-stream',
        headers={
            'Content-Disposition': "attachment; filename*=UTF-8''" + quote(name, safe=''),
            'ETag': '"' + sha + '"',
        },
    )


@router.post('/agents/jobs/{key}/cancel')
async def cancel_job(key: str, request: Request, user=Depends(active)):
    if await data(request) != {}:
        raise ValueError('取消工作不接受其他參數。')
    svc = configured_service()
    await svc.cancel(user.id, key, wait=False)
    return await asyncio.to_thread(svc.queue.public, user.id, key)


@router.post('/agents/jobs/{key}/resume')
async def resume(key: str, request: Request, user=Depends(active)):
    value = await data(request)
    if set(value) - {'decision'}:
        raise ValueError('不接受额外身分或執行參數。')
    svc = service()
    subject = await billed_subject(user)
    svc.queue.resume(user.id, subject, key, value.get('decision'))
    return svc.queue.public(user.id, key)


@router.post('/agents/jobs/{key}/followup')
async def followup(key: str, request: Request, user=Depends(active)):
    value = await data(request)
    if set(value) != {'instruction', 'request_id'}:
        raise ValueError('接續修改欄位不符。')
    request_id = str(uuid.UUID(str(value['request_id'])))
    svc = service()
    old = svc.queue.get(user.id, key)
    if old['kind'] != 'company':
        raise ValueError('請回公文聊天另送新訊息；未重播既有公文工作。')
    subject = await billed_subject(user)
    new = svc.queue.submit(
        user.id, subject, old['chat'], request_id, value['instruction'], [], [key],
    )
    return svc.queue.public(user.id, new)


async def billed_subject(user):
    model = service().model_factory(await verified_user(user.id))
    try:
        return model.subject
    finally:
        await close_model(model)


class ServiceLifespan:
    def __init__(self, original):
        self.original = original

    @asynccontextmanager
    async def run(self, app):
        async with self.original(app):
            svc = service()
            svc.task = asyncio.create_task(svc.loop())
            try:
                yield
            finally:
                svc.task.cancel()
                await asyncio.wait({svc.task})
                if not svc.task.cancelled():
                    svc.task.result()


def install(app):
    app.router.lifespan_context = ServiceLifespan(app.router.lifespan_context).run
    app.router.routes[0:0] = router.routes


def read_nas_shares(vault, owner, revision):
    with vault.session(owner, revision) as client:
        return client.shares()


@router.get('/agents/capacity')
async def capacity(user=Depends(active)):
    """Content-free operational counters; administrators only."""
    if user.role != 'admin':
        raise HTTPException(403, '需要管理員權限。')
    from official_document_workflow.infrastructure import concurrency as limits
    from official_document_workflow.infrastructure.storage.state import STATE
    current = configured_service()

    def queued():
        with current.queue.store.db() as db:
            return dict(db.execute("SELECT kind,count(*) FROM web_jobs WHERE status='queued' GROUP BY kind").fetchall())
    waiting = await current._io(queued)
    return {
        'company_agent': {
            'limit': current.max_concurrent, 'active': sum(kind == 'company' for kind in current.execution_kinds.values()),
            'waiting': waiting.get('company', 0),
            'max_pending': current.queue.max_pending,
            'max_pending_per_user': current.queue.max_per_user,
        },
        'documents': {
            'limit': limits.DOCUMENTS.limit, 'active': len(limits.DOCUMENTS.active),
            'waiting': waiting.get('document', 0),
            'executing': sum(kind == 'document' for kind in current.execution_kinds.values()),
            'resource_waiting': len(limits.DOCUMENTS.waiting),
        },
        'eip': {
            'limit': STATE.processing.limit, 'active': len(STATE.processing.active),
            'waiting': len(STATE.processing.waiting),
            'manual_tasks': sum(not task.done() for task in STATE.tasks),
            'automatic_accounts': len(STATE.automatic_tasks),
        },
        'models': {'limit': limits.MODEL_LIMIT, 'active': limits.MODEL_LIMIT - limits.MODELS._value},
        'ocr': {'limit': limits.OCR_LIMIT, 'active': limits.OCR_LIMIT - limits.OCR._value},
    }
