"""Compose WebUI identity, shared services, browser routes and MCP lifetime."""
from contextlib import asynccontextmanager

import httpx
from starlette.routing import Route
from examples.shared.runtime.installation import install_once

from company_mcp.authentication import AuthorizationUnavailable, Verifier
from company_mcp.config import BACKEND_PATH, Configuration
from .company import Resources
from .analysis import execute
from .backend import check_store, endpoint


async def webui_owner(subject):
    # Resolve exactly one immutable OIDC sub. Never match email/name or take
    # the host's first match if a damaged database contains duplicate mappings.
    from open_webui.internal.db import get_async_db_context
    from open_webui.models.users import User
    from sqlalchemy import select

    async with get_async_db_context() as db:
        rows = (await db.execute(select(User.id).where(
            User.oauth['oidc']['sub'].as_string() == subject,
        ).limit(2))).scalars().all()
    owner = rows[0] if len(rows) == 1 else None
    if owner is None:
        return None
    from examples.identity.adapters.webui import verified_user
    from fastapi import HTTPException
    try:
        user = await verified_user(owner)
    except ValueError:
        return None
    except HTTPException as failure:
        if failure.status_code in {401, 403}:
            return None
        raise AuthorizationUnavailable('Company authorization is unavailable.') from None
    if (user.get('id') != owner or user.get('role') not in {'user', 'admin'}
            or (user.get('oauth') or {}).get('oidc', {}).get('sub') != subject):
        return None
    return owner


class BackendResources:
    def __init__(self, configuration, current):
        self.configuration, self.current = configuration, current

    def __call__(self):
        from examples.official_document_workflow.runtime import current_resources
        return Resources(self.configuration, self.current.queue.store, current_resources,
                         queue=self.current.queue, identity=self.current.identity)

    async def ready(self):
        if self.current.task is None or self.current.task.done():
            raise RuntimeError('Resource worker is unavailable.')
        await check_store(self.current.queue.store)


class BackendIntegration:
    def __init__(self, configuration):
        self.configuration = configuration

    @asynccontextmanager
    async def __call__(self, host, current):
        remove_handler = current.register_job_handler('mcp_analysis', execute)
        try:
            if self.configuration is None:
                yield
                return
            async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
                verifier = Verifier(self.configuration, client, resolve_owner=webui_owner)
                resources = BackendResources(self.configuration, current)
                host.state.company_mcp = (self.configuration, resources)
                route = Route(BACKEND_PATH, endpoint(self.configuration, verifier, resources,
                                                     ready=resources.ready), methods=['POST'])
                host.router.routes.insert(0, route)
                try:
                    yield
                finally:
                    host.router.routes.remove(route)
                    del host.state.company_mcp
        finally:
            remove_handler()


def install(app):
    with install_once(app, 'company_mcp_installed') as installing:
        if installing:
            _install(app)


def _install(app):
    from company_mcp.browser import router

    configuration = Configuration.from_environment()
    app.router.routes[0:0] = router.routes
    app.state.job_integrations = (*getattr(app.state, 'job_integrations', ()), BackendIntegration(configuration))
