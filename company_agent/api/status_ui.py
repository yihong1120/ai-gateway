"""Serve repository-owned chat enhancements through WebUI's custom asset hooks."""
from importlib.resources import files

from fastapi import APIRouter, Request
from fastapi.responses import Response

router = APIRouter()
ASSETS = {
    '/static/loader.js': ('status_history.js', 'application/javascript'),
    '/static/custom.css': ('status_history.css', 'text/css'),
}


@router.get('/static/loader.js', include_in_schema=False)
@router.get('/static/custom.css', include_in_schema=False)
async def status_asset(request: Request):
    from open_webui.config import STATIC_DIR

    name, media_type = ASSETS[request.url.path]
    original = STATIC_DIR / request.url.path.rsplit('/', 1)[-1]
    upstream = original.read_bytes() if original.is_file() else b''
    extension = files('company_agent.ui').joinpath(name).read_bytes()
    separator = b'\n;\n' if media_type == 'application/javascript' else b'\n'
    return Response(upstream + separator + extension, media_type=media_type, headers={
        'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
    })


def install(app):
    from examples.shared.runtime.installation import install_once
    with install_once(app, 'company_status_ui_installed') as installing:
        if installing:
            app.router.routes[0:0] = router.routes
