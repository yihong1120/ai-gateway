"""Authenticated same-origin task/download/approval endpoints."""
from __future__ import annotations

import asyncio
import mimetypes
import os
import uuid
from importlib.resources import files
from urllib.parse import quote

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from fastapi.responses import Response
from starlette.background import BackgroundTask
from starlette.responses import StreamingResponse

import company_agent.services.eip.calendar as _calendar_imports
import company_agent.services.eip.client as _client_imports
import examples.official_document_workflow.runtime as _runtime_imports
import examples.shared.runtime.installation as _installation_imports
from company_agent.api.http import active
from company_agent.api.http import PrivateRoute
from company_agent.api.identity import router as identity_router
from company_agent.api.lifespan import ServiceLifespan
from company_agent.api.service import configured_service
from company_agent.api.service import service
from company_agent.runtime.models import close_model
from company_agent.services.files.nas import endpoint
from company_agent.services.files.nas import NASVault
from examples.identity.adapters.webui import verified_user
from examples.shared.runtime import concurrency as limits
from examples.shared.runtime.browser import data
from examples.shared.runtime.jobs import JobServiceMiddleware

router = APIRouter(route_class=PrivateRoute)
router.include_router(identity_router)


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

    return await _client_imports.view_document(service().queue.store, user.id, job, handle)


@router.get('/agents/eip/{job}/{handle}/attachments/{index}')
async def eip_document_attachment(job: str, handle: str, index: int, user=Depends(active)):

    name, payload = await _client_imports.view_attachment(service().queue.store, user.id, job, handle, index)
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
    return {'jobs': await service()._io(service().list, user.id)}


@router.post('/agents/calendar/preview')
async def calendar_preview(request: Request, user=Depends(active)):
    value = await data(request)
    if set(value) != {'artifact_id'} or not isinstance(value['artifact_id'], str):
        raise ValueError('請選擇行事曆 CSV。')

    return await _calendar_imports.preview(service().queue.store, user.id, value['artifact_id'])


@router.post('/agents/calendar/confirm')
async def calendar_confirm(request: Request, user=Depends(active)):
    value = await data(request)
    if (
        set(value) != {'token', 'confirm'}
        or value['confirm'] is not True
        or not isinstance(value['token'], str)
    ):
        raise ValueError('需要本人明確確認行事曆。')

    return await _calendar_imports.confirm(service().queue.store, user.id, value['token'])


@router.get('/agents/nas')
async def nas_status(user=Depends(active)):

    configured = bool(os.getenv('COMPANY_AGENT_NAS_URL'))
    if configured:
        endpoint()
    vault = NASVault(configured_service().queue.store)
    return {**vault.status(user.id), 'bindings': await asyncio.to_thread(vault.bindings, user.id), 'configured': configured}


@router.post('/agents/nas/bind')
async def nas_bind(request: Request, user=Depends(active)):
    value = await data(request)
    if not {'account', 'password', 'consent'} <= set(value) or set(value) - {'account', 'password', 'consent', 'binding_id', 'label'} or value['consent'] is not True:
        raise ValueError('請確認個人帳號、加密保存及所需文件雲端分析的同意事項。')

    vault = NASVault(configured_service().queue.store)
    result = await asyncio.to_thread(
        vault.bind, user.id, value['account'], value['password'],
        binding_id=value.get('binding_id'), label=value.get('label'),
    )
    vault.store.audit(user.id, None, 'nas_bind', 'done')
    return result


@router.post('/agents/nas/unlink')
async def nas_unlink(request: Request, user=Depends(active)):
    value = await data(request)
    if set(value) - {'binding_id'}:
        raise ValueError('解除綁定僅接受本人連線識別碼。')

    vault = NASVault(configured_service().queue.store)
    await asyncio.to_thread(vault.unlink, user.id, value.get('binding_id'))
    vault.store.audit(user.id, None, 'nas_unlink', 'done')
    return {**vault.status(user.id), 'bindings': await asyncio.to_thread(vault.bindings, user.id)}


@router.post('/agents/nas/test')
async def nas_test(request: Request, user=Depends(active)):
    value = await data(request)
    if set(value) - {'binding_id'}:
        raise ValueError('測試連線僅接受本人連線識別碼。')

    vault = NASVault(configured_service().queue.store)
    if not value.get('binding_id') and len(vault.bindings(user.id)) > 1:
        raise ValueError('請選擇要測試的 NAS 連線。')
    status = vault.status(user.id, value.get('binding_id'))

    return {
        **status,
        'shares': await asyncio.to_thread(read_nas_shares, vault, user.id, status['revision']),
    }


@router.get('/agents/jobs/{key}')
async def job(key: str, user=Depends(active)):
    return await asyncio.to_thread(service().result, user.id, key)


@router.get('/agents/artifacts/{key}')
async def artifact(key: str, user=Depends(active)):
    svc = service()
    name, sha, payload = await svc._io(svc.queue.store.read_artifact, user.id, key)
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
    await svc._io(svc.queue.resume, user.id, subject, key, value.get('decision'))
    return await svc._io(svc.result, user.id, key)


@router.post('/agents/jobs/{key}/followup')
async def followup(key: str, request: Request, user=Depends(active)):
    value = await data(request)
    if set(value) != {'instruction', 'request_id'}:
        raise ValueError('接續修改欄位不符。')
    request_id = str(uuid.UUID(str(value['request_id'])))
    svc = service()
    old = await svc._io(svc.queue.get, user.id, key)
    if old['kind'] != 'company':
        raise ValueError('請回公文聊天另送新訊息；未重播既有公文工作。')
    subject = await billed_subject(user)
    new = await svc._io(
        svc.queue.submit,
        user.id, subject, old['chat'], request_id, value['instruction'], [], [key],
    )
    return await svc._io(svc.result, user.id, new)


async def billed_subject(user):
    model = service().model_factory(await verified_user(user.id))
    try:
        return model.subject
    finally:
        await close_model(model)


def install(app):
    with _installation_imports.install_once(app, 'company_agent_installed') as installing:
        if installing:
            app.add_middleware(JobServiceMiddleware, owner=app)
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
    state = _runtime_imports.current_resources().state
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
            'mutation_locks': current.queue.store.lock_metrics(),
        },
        'documents': {
            'limit': limits.DOCUMENTS.limit, 'active': len(limits.DOCUMENTS.active),
            'waiting': waiting.get('document', 0),
            'executing': sum(kind == 'document' for kind in current.execution_kinds.values()),
            'resource_waiting': len(limits.DOCUMENTS.waiting),
        },
        'eip': {
            'limit': state.processing.limit, 'active': len(state.processing.active),
            'waiting': len(state.processing.waiting),
            'manual_tasks': sum(not task.done() for task in state.tasks),
            'automatic_accounts': len(state.automatic_tasks),
        },
        'models': {'limit': limits.MODEL_LIMIT, 'active': limits.MODEL_LIMIT - limits.MODELS._value},
        'ocr': {'limit': limits.OCR_LIMIT, 'active': limits.OCR_LIMIT - limits.OCR._value},
    }
