"""Run only browser-confirmed analysis through the existing leased job queue."""
from __future__ import annotations

import asyncio
import time
from dataclasses import replace

from company_agent.runtime.models import close_model
from company_agent.services.eip.processing import process
from company_agent.services.eip.schemas import EIPProcess
from examples.shared.runtime.io import run_sync

from company_mcp.authentication import Principal
from company_mcp.storage import ResourceError


def prepare_job(store, owner, job, source_job, handles):
    with store.db() as db:
        db.execute('INSERT INTO jobs(id,owner,status,result,created) VALUES(%s,%s,%s,%s,%s)',
                   (job, owner, 'running', '{}', time.time()))
        db.execute(
            'INSERT INTO eip_sources(owner,job,handle,revision,payload) '
            'SELECT owner,%s,handle,revision,payload FROM eip_sources '
            'WHERE owner=%s AND job=%s AND handle=ANY(%s)',
            (job, owner, source_job, handles),
        )


async def execute(service, row):
    # Configuration/service live only on the current WebUI application; nothing
    # in a queued model instruction can change them or grant a different owner.
    from company_mcp.config import Configuration
    from .company import Resources
    from examples.official_document_workflow.runtime import current_resources

    configuration = Configuration.from_environment()
    if configuration is None:
        raise ResourceError('mcp_disabled', '公司 MCP 已停止，未執行分析。')
    payload = row['payload']
    if row['kind'] != 'mcp_analysis' or payload.get('operation') != 'mcp_analysis':
        raise ResourceError('operation_unavailable', '工作類型與本人確認的操作不符。')
    person = Principal(row['owner'], row['subject'], payload['mcp_client'])
    resources = Resources(configuration, service.queue.store, current_resources, queue=service.queue)
    user = await service.identity(row['owner'])
    if ((user.get('oauth') or {}).get('oidc', {}).get('sub') != person.subject
            or user.get('role') not in {'user', 'admin'} or person.client not in configuration.clients):
        raise ResourceError('authentication_required', '本人公司身分已變更，未執行分析。')
    approved = await run_sync(resources.repository.operation, person.owner, row['request_id'], client=person.client)
    if approved['web_job'] != row['id'] or approved['workspace'] != payload['workspace']:
        raise ResourceError('operation_unavailable', '缺少本人確認的公文操作。')
    # A browser-confirmed durable job may wait longer than the interactive
    # workspace TTL. Its original source versions, owner and grant stay checked.
    ctx = await resources.context(person, payload['workspace'], need_eip=True, confirmed_operation=True)
    await run_sync(prepare_job, service.queue.store, person.owner, row['id'], ctx.job, payload['handles'])
    ctx = replace(ctx, job=row['id'])
    await run_sync(service.queue.update, row['id'], engine_job=ctx.job, progress='正在處理本人已確認的公文')
    selected = service.settings_factory(row)
    # Reuse configured routing/profiles and personal billing, never a privileged
    # official-document exemption key. Source scope stays on the MCP workspace.
    ctx = replace(ctx, settings=selected.model_copy(update={
        'eip_binding_revision': ctx.settings.eip_binding_revision,
        'enable_eip_read': True, 'enable_eip_processing': True,
    }))
    model = service.model_factory(user, selected.model_name)
    try:
        if model.subject != person.subject:
            raise ResourceError('authentication_required', '計費身分與本人不符。')
        ctx = replace(ctx, model=model)
        async with asyncio.timeout(selected.execution_timeout_seconds):
            result = await process(ctx, EIPProcess.model_validate({
                key: payload[key] for key in ('handles', 'instruction', 'model_profile')
            }))
        await ctx.authorize_resources()
        await resources.context(person, payload['workspace'], need_eip=True, confirmed_operation=True)
        await run_sync(service.queue.store.finish, person.owner, ctx.job, result['status'], result)
        state = result['status'] if result['status'] in {'done', 'partial', 'error', 'needs_input'} else 'partial'
        await run_sync(service.queue.update, row['id'], engine_job=ctx.job,
                       status=state, progress='公文處理已結束；請核對成果，行事曆仍待本人預覽確認')
    finally:
        await close_model(model)
