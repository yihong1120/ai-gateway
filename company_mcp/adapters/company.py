"""Bounded MCP façade over existing NAS/EIP services and private workflows."""
from __future__ import annotations

import hashlib
import json
from functools import partial

import company_agent.services.eip.drafts as _drafts_imports
import company_agent.services.eip.reading as _reading_imports
from company_agent.config import Settings
from company_agent.runtime.context import Context
from company_agent.services.eip import client as eip
from company_agent.services.files.nas import file_access
from company_agent.services.files.nas import NASAccess
from company_agent.services.files.nas import NASVault
from company_agent.services.files.operations import ToolBox
from company_agent.services.files.schemas import Read
from company_agent.services.files.schemas import Search
from company_mcp.authorization import AccessPolicy
from company_mcp.storage import Repository
from company_mcp.storage import ResourceError
from company_mcp.tools import TOOLS
from examples.identity.adapters import webui as webui_identity
from examples.shared.runtime.io import run_sync


class Resources:
    def __init__(self, configuration, store, workflow, *, queue=None, identity=None):
        self.configuration, self.store, self.workflow, self.queue = configuration, store, workflow, queue
        self.identity = identity
        self.repository = Repository(store)
        self.vault = NASVault(store)

    async def authorize(self, person):
        await run_sync(self.repository.grant, person)

    async def audit(self, person, name):
        await run_sync(self.store.audit, person.owner, None, 'mcp:' + name, 'done')

    async def confirm_analysis(self, person, identifier):
        """Internal browser adapter port; never registered as an MCP tool."""
        operation = await run_sync(self.repository.operation, person.owner, identifier, client=person.client)
        if operation['web_job']:
            return operation['web_job']
        ctx = await self.context(person, operation['workspace'], need_eip=True)
        for handle in operation['payload']['handles']:
            await run_sync(eip.source, ctx, handle, 'attachment')
        return await run_sync(self.repository.submit_analysis, person, identifier, self.queue)

    async def eip_account(self, owner):
        return await run_sync(self.workflow().store.account, owner)

    async def context(self, person, workspace, *, need_eip=False, confirmed_operation=False):
        policy = AccessPolicy(
            self.configuration, self.repository, self.identity or webui_identity.verified_user,
            self.eip_account,
        )
        authorize = partial(
            policy.authorize, person, workspace, need_eip=need_eip,
            confirmed_operation=confirmed_operation,
        )

        snapshot = await authorize()
        settings = Settings(
            state_dir=str(self.store.directory), nas_binding_revisions=snapshot.get('nas_revisions', []),
            eip_binding_revision=snapshot.get('eip_generation'), enable_eip_read=need_eip,
            enable_eip_processing=need_eip, enable_ocr=True,
        )
        return Context(person.owner, workspace, settings, self.store, authorize_resources=authorize)

    async def validate_result(self, person, name, value, output=None):
        if not hasattr(value, 'workspace'):
            return
        ctx = await self.context(
            person, value.workspace, need_eip=name.startswith('eip_') or name in {
                'calendar_plan_prepare', 'document_analysis_prepare',
            },
        )
        if name.startswith('nas_'):
            revisions = ctx.settings.nas_binding_revisions
            if name == 'nas_roots' and output is not None:
                revisions = [
                    revision for revision in revisions if any(
                        root['name'].startswith('nas_' + revision[:8] + '_') for root in output['roots']
                    )
                ]
            if hasattr(value, 'root'):
                revisions = [revision for revision in revisions if value.root.startswith('nas_' + revision[:8] + '_')]
                if len(revisions) != 1:
                    raise ResourceError('binding_changed', 'NAS 帳號版本不符，請建立新工作。')
            for revision in revisions:
                data = await run_sync(self.vault.credentials, person.owner, revision)
                data.clear()

    async def execute(self, person, name, value):
        await run_sync(self.repository.grant, person)
        await run_sync(self.repository.rate_limit, person.owner)
        if name == 'connections_list':
            bindings = await run_sync(self.vault.bindings, person.owner)
            account = await run_sync(self.workflow().store.account, person.owner)
            return {
                'resource_mode': 'all_owned_bindings',
                'nas': [{key: row[key] for key in ('binding_id', 'label', 'usable', 'read_only')} for row in bindings],
                'eip': {
                    'saved': bool(account), 'automatic_enabled': bool(account and account['enabled']),
                    'transport_approved': self.configuration.eip_transport_acknowledged,
                },
                'connections_url': self.configuration.origin + '/connections',
            }
        if name == 'workspace_prepare':
            with self.store.db() as db:
                revisions = [
                    row[0] for row in db.execute(
                        'SELECT revision FROM nas_bindings WHERE owner=%s AND usable ORDER BY binding_id',
                        (person.owner,),
                    )
                ]
            account = await run_sync(self.workflow().store.account, person.owner)
            identifier = await run_sync(
                self.repository.create_workspace, person,
                {'nas_revisions': revisions, 'eip_generation': account['generation'] if account else None},
                self.configuration.workspace_seconds, value.request_id,
            )
            # An idempotency hit must still be active; no implicit new snapshot.
            snapshot = await run_sync(self.repository.workspace, person, identifier)
            return {
                'workspace': identifier, 'nas_accounts': len(snapshot['nas_revisions']),
                'eip_saved': bool(snapshot['eip_generation']), 'resource_mode': 'all_owned_bindings',
            }
        if name == 'operation_status':
            operation = await run_sync(self.repository.operation, person.owner, value.operation, client=person.client)
            await self.context(
                person, operation['workspace'], need_eip=True,
                confirmed_operation=bool(operation['web_job']),
            )
            if not operation['web_job']:
                return {
                    'status': 'awaiting_personal_confirmation', 'operation': value.operation,
                    'confirmation_url': self.configuration.origin + '/agents/mcp?operation=' + value.operation,
                }
            result = await run_sync(self.queue.public, person.owner, operation['web_job'])
            return {'operation': value.operation, 'job': result, 'workspace_url': self.configuration.origin + '/agents'}
        need_eip = name.startswith('eip_') or name in {'calendar_plan_prepare', 'document_analysis_prepare'}
        ctx = await self.context(person, value.workspace, need_eip=need_eip)
        if name == 'nas_roots':
            roots, failures = [], []
            for revision in ctx.settings.nas_binding_revisions:
                try:
                    remote = NASAccess(self.vault, person.owner, revision, ctx.settings.max_file_bytes)
                    current = await run_sync(remote.visible_roots)
                    data = await run_sync(self.vault.credentials, person.owner, revision)
                    try:
                        label = data.get('label') or data['account']
                    finally:
                        data.clear()
                    roots.extend({**root, 'connection': label} for root in current)
                except (ValueError, ConnectionError, PermissionError):
                    failures.append({'binding_revision': revision, 'status': 'unavailable'})
            return {'roots': roots, 'failures': failures, 'complete': not failures}
        if name == 'nas_list':
            return await run_sync(
                file_access(ctx.settings, self.store, person.owner).listing,
                value.root, value.path, value.offset, value.limit,
            )
        if name in {'nas_search', 'nas_read'}:
            box = ToolBox(ctx.settings, self.store, person.owner, ctx.job)
            if name == 'nas_search':
                if value.mode != 'name':
                    raise ResourceError('invalid_arguments', 'MCP NAS 搜尋目前只支援檔名模式。')
                return await run_sync(box.search_files, Search(root=value.root, query=value.query, limit=value.limit))
            return await run_sync(box.read_file, Read(root=value.root, path=value.path, start=value.start, limit=value.limit))
        if name in {'eip_query', 'eip_attachments', 'eip_calendar_query', 'eip_calendar_detail'}:
            schema = TOOLS[name][0]
            arguments = value.model_dump(exclude={'workspace'})
            # Query receipts are owner/workspace-scoped and never use a token.
            call = hashlib.sha256(json.dumps([name, arguments], sort_keys=True).encode()).hexdigest()
            return await eip.query(ctx, name, schema.model_validate(value.model_dump()), 'mcp:' + call)
        if name == 'eip_document_read':
            return await eip.view_document(self.store, person.owner, ctx.job, value.handle)
        if name == 'eip_read_attachment':
            return await _reading_imports.read_attachment(ctx, value)
        if name == 'calendar_plan_prepare':
            result = await _drafts_imports.prepare(ctx, value)
            result['confirmation_url'] = self.configuration.origin + '/agents/mcp?calendar=' + result['files'][0]['id']
            return result
        if name == 'document_analysis_prepare':
            for handle in value.handles:
                await run_sync(eip.source, ctx, handle, 'attachment')
            payload = value.model_dump(exclude={'workspace', 'request_id'})
            identifier = await run_sync(self.repository.prepare, person, ctx.job, value.request_id, payload)
            return {
                'operation': identifier, 'status': 'awaiting_personal_confirmation', 'model_called': False,
                'confirmation_url': self.configuration.origin + '/agents/mcp?operation=' + identifier,
            }
        if name == 'artifact_get':
            # Reading is owner AND workspace-scoped, including stale-binding
            # validation at browser import time; bytes are never made public.
            with self.store.db() as db:
                row = db.execute(
                    'SELECT a.job FROM artifacts a WHERE a.owner=%s AND a.id=%s AND '
                    '(a.job=%s OR EXISTS(SELECT 1 FROM mcp_operations o WHERE o.owner=a.owner '
                    'AND o.client=%s AND o.workspace=%s AND o.web_job=a.job))',
                    (person.owner, value.artifact, ctx.job, person.client, ctx.job),
                ).fetchone()
            if not row:
                raise ResourceError('artifact_unavailable', '成果不屬於本人本次資源工作。')
            artifact = await run_sync(self.store.read_artifact, person.owner, value.artifact, row[0])
            return {
                'name': artifact[0], 'sha256': artifact[1],
                'download_url': self.configuration.origin + '/agents/artifacts/' + value.artifact,
            }
        raise ResourceError('unknown_tool', '公司 MCP 不提供此工具。')
