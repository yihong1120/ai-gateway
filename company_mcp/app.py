"""Independent ASGI edge; WebUI, databases and source keys are not loaded."""
from __future__ import annotations

from contextlib import asynccontextmanager
from functools import partial

import httpx
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

import examples.shared.config as _config_imports
from .config import Configuration
from .config import METADATA_PATH
from .config import PATH
from .remote import BackendClient
from .remote import RemoteResources
from .remote import RemoteVerifier
from .server import Boundary
from .server import build
from .settings import Settings
from .tools import TOOLS


async def health(request):
    return JSONResponse({'status': 'ok', 'service': 'company-mcp'}, headers={'Cache-Control': 'no-store'})


async def ready(request):
    try:
        await request.app.state.backend.ready()
    except Exception:
        return JSONResponse({'status': 'not_ready'}, status_code=503, headers={'Cache-Control': 'no-store'})
    return JSONResponse({'status': 'ready'}, headers={'Cache-Control': 'no-store'})


class ServiceLifespan:
    def __init__(self, configuration, settings, transport):
        self.configuration, self.settings, self.transport = configuration, settings, transport

    @asynccontextmanager
    async def __call__(self, app):
        async with httpx.AsyncClient(transport=self.transport, trust_env=False, follow_redirects=False) as client:
            backend = BackendClient(self.configuration, self.settings, client)
            app.state.backend = backend
            managed = build(self.configuration, RemoteVerifier(backend), partial(RemoteResources, backend), tools=TOOLS)
            boundary = Boundary(managed, self.configuration)
            routes = [Route(PATH, boundary), Route(METADATA_PATH, boundary)]
            app.router.routes.extend(routes)
            try:
                async with managed.router.lifespan_context(managed):
                    yield
            finally:
                for route in routes:
                    app.router.routes.remove(route)
                del app.state.backend


def create_app(configuration=None, settings=None, *, transport=None):
    if configuration is None or settings is None:
        _config_imports.load_service('company_mcp')
        configuration = configuration or Configuration.from_environment(backend=False)
    if configuration is None:
        raise RuntimeError('Company MCP is disabled; enable the dedicated service configuration.')
    settings = settings or Settings.from_environment()
    if configuration.introspection_client or configuration.introspection_secret:
        raise ValueError('The independent MCP edge must not receive backend IdP client secrets.')

    return Starlette(
        routes=[Route('/healthz', health), Route('/readyz', ready)],
        lifespan=ServiceLifespan(configuration, settings, transport),
    )
