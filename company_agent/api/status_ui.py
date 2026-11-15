"""Serve repository-owned chat enhancements through WebUI's custom asset hooks."""
from __future__ import annotations

from importlib.resources import files

from fastapi import APIRouter
from fastapi import Request
from fastapi.responses import Response

import examples.shared.runtime.installation as _installation_imports
from examples.shared.runtime.modules import ModuleReference

_webui_config = ModuleReference('open_webui.config')

router = APIRouter()
ASSETS = {
    '/static/loader.js': ('status_history.js', 'application/javascript'),
    '/static/custom.css': ('status_history.css', 'text/css'),
}


@router.get('/static/loader.js', include_in_schema=False)
@router.get('/static/custom.css', include_in_schema=False)
async def status_asset(request: Request):
    STATIC_DIR = _webui_config.attribute('STATIC_DIR')

    name, media_type = ASSETS[request.url.path]
    original = STATIC_DIR / request.url.path.rsplit('/', 1)[-1]
    upstream = original.read_bytes() if original.is_file() else b''
    extension = files('company_agent.ui').joinpath(name).read_bytes()
    separator = b'\n;\n' if media_type == 'application/javascript' else b'\n'
    return Response(
        upstream + separator + extension, media_type=media_type, headers={
            'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
        },
    )


def install(app):
    with _installation_imports.install_once(app, 'company_status_ui_installed') as installing:
        if installing:
            app.router.routes[0:0] = router.routes
