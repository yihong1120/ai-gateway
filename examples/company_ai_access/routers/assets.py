"""Installer downloads and browser setup assets."""
from __future__ import annotations

import os
from html import escape
from pathlib import Path
from urllib.parse import parse_qsl
from urllib.parse import urlencode
from urllib.parse import urlsplit
from urllib.parse import urlunsplit

from fastapi import APIRouter
from fastapi import HTTPException
from fastapi import Request
from fastapi.responses import FileResponse
from fastapi.responses import Response

from examples.company_ai_access.errors import error


def assets_router(*, browser_api_prefix='/api/company-ai', login_path='/auth'):
    router = APIRouter()

    @router.get('/company-ai/network-access', include_in_schema=False)
    async def network_access():
        # Guarded by the standalone or host bridge network middleware.
        # No login, ranges, identities or credentials are disclosed.
        return {'allowed': True}

    @router.get('/company-ai/portal/{asset:path}', include_in_schema=False)
    async def flutter(asset: str):

        configured = os.getenv('AI_ACCESS_FLUTTER_DIR', '')
        if not configured:
            raise error(503, 'ai_access_flutter_not_installed')
        root = Path(configured).resolve()
        target = (root / asset).resolve()
        if not target.is_relative_to(root):
            raise HTTPException(404)
        if not target.is_file() and (not asset or not Path(asset).suffix):
            target = root / 'index.html'
        if not target.is_file():
            raise HTTPException(404)
        return FileResponse(
            target,
            headers={
                'Cache-Control': 'no-store',
                'Referrer-Policy': 'no-referrer',
                'X-Content-Type-Options': 'nosniff',
            },
        )

    @router.get('/company-ai/downloads/{filename}', include_in_schema=False)
    async def download(filename: str):
        if filename not in {
            'Setup-CompanyAI.ps1',
            'Get-CompanyAIKey.ps1',
            'Start-CompanyCodex.ps1',
            'changdar-ai-setup.exe',
        }:
            raise HTTPException(404)
        path = Path(__file__).resolve().parents[1] / 'windows' / filename
        if not path.is_file():
            raise HTTPException(404)
        return FileResponse(
            path,
            media_type='application/octet-stream',
            headers={
                'Content-Disposition': f'attachment; filename="{filename}"',
                'Cache-Control': 'no-store',
                'X-Content-Type-Options': 'nosniff',
            },
        )

    @router.get('/company-ai/setup', include_in_schema=False)
    async def setup(request: Request):
        page = (Path(__file__).resolve().parents[1] / 'ui' / 'setup.html').read_text()
        page = page.replace('<main>', '<main data-api-prefix="' + browser_api_prefix + '">')
        login = urlsplit(login_path)
        if login.path == '/bff/auth/oidc/login':
            params = dict(parse_qsl(login.query))
            params['return_to'] = request.url.path + ('?' + request.url.query if request.url.query else '')
            login_path_for_request = urlunsplit(login._replace(query=urlencode(params)))
        else:
            login_path_for_request = login_path
        page = page.replace('href="/auth"', 'href="' + escape(login_path_for_request, quote=True) + '"')
        page = page.replace('登入 AI 開發網站', '登入公司網站')
        return Response(
            page,
            media_type='text/html',
            headers={
                'Content-Security-Policy': "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
                'Referrer-Policy': 'no-referrer',
                'Cache-Control': 'no-store',
            },
        )

    @router.get('/company-ai/setup.js', include_in_schema=False)
    async def script():
        return FileResponse(
            Path(__file__).resolve().parents[1] / 'ui' / 'setup.js',
            media_type='application/javascript',
        )

    return router
