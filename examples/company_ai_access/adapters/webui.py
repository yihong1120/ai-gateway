"""Opt-in installation into the existing Open WebUI host."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse
import httpx

from examples.company_ai_access.routers.assets import assets_router
from .bridge import BridgeClient, BridgeSettings
from examples.company_ai_access.errors import error
from examples.company_ai_access.services.inference import stream_response


def install_webui(app):
    from examples.shared.runtime.installation import install_once
    with install_once(app, 'company_ai_installed') as installing:
        if not installing:
            return
        from open_webui.utils.auth import get_optional_verified_user_from_request
        from examples.company_ai_access.routers.dependencies import limited_json, validate_inference_query

        router = APIRouter()

        @router.api_route('/api/company-ai/{suffix:path}', methods=['GET', 'POST'])
        @router.api_route('/company-ai/v1/{suffix:path}', methods=['GET', 'POST'])
        async def bridge(request: Request, suffix: str):
            client = getattr(app.state, 'company_ai_bridge', None)
            if client is None:
                raise error(503, 'ai_access_not_configured')
            headers = {
                'X-Company-AI-Client-IP': request.client.host if request.client else '',
            }
            native = request.url.path in {
                '/api/company-ai/device/start',
                '/api/company-ai/device/poll',
                '/api/company-ai/device/ack',
            }
            inference = request.url.path.startswith('/company-ai/v1/')
            if not native and not inference:
                # Use the host's normal session, including revocation checks. The
                # bridge never accepts a caller-supplied subject/admin assertion.
                if request.headers.get('authorization', '').startswith('Bearer sk-') or request.cookies.get(
                    'token', ''
                ).startswith('sk-'):
                    raise error(401, 'ai_access_browser_login_required')
                user = await get_optional_verified_user_from_request(request)
                if user is None:
                    raise error(401, 'ai_access_login_required')
                subject = ((user.oauth or {}).get('oidc') or {}).get('sub')
                if user.role not in {'user', 'admin'} or not isinstance(subject, str) or not subject:
                    raise error(403, 'ai_access_oidc_required')
                headers['X-Company-AI-Subject'] = subject
                headers['X-Company-AI-Admin'] = '1' if user.role == 'admin' else '0'
            for name in (
                'origin',
                'sec-fetch-site',
                'x-company-ai-action',
                'content-type',
                'anthropic-version',
                'anthropic-beta',
            ):
                if name in request.headers:
                    headers[name] = request.headers[name]
            if inference:
                for name in ('authorization', 'x-api-key'):
                    if name in request.headers:
                        headers[name] = request.headers[name]
            body = None
            if request.method == 'POST':
                body = await limited_json(
                    request,
                    8 * 1024 * 1024 if inference else 16000,
                    allow_empty=not inference,
                )
            if inference:
                # Claude Code's beta SDK appends this fixed hint. Validate it at
                # both trust boundaries, then omit it from the upstream URL.
                validate_inference_query(request)
            elif request.scope.get('query_string', b''):
                # Control/device flow queries still cannot carry credentials.
                raise error(400, 'ai_access_query_not_allowed')
            upstream = await client.send(request.method, request.url.path, headers, body)

            return stream_response(
                upstream, {'Retry-After': upstream.headers['retry-after']} if 'retry-after' in upstream.headers else {}
            )

        routes = [*router.routes, *assets_router().routes]
        app.router.routes[0:0] = routes
        original = app.router.lifespan_context

        @asynccontextmanager
        async def lifespan(app):
            async with original(app):
                settings = BridgeSettings.from_environment()
                app.state.company_ai_bridge = None
                if settings is None:
                    yield
                else:
                    async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as transport:
                        app.state.company_ai_bridge = BridgeClient(settings, transport)
                        try:
                            yield
                        finally:
                            app.state.company_ai_bridge = None

        app.router.lifespan_context = lifespan

        @app.middleware('http')
        async def private(request: Request, call_next):
            if request.url.path.startswith(('/api/company-ai/', '/company-ai/')):
                client = getattr(app.state, 'company_ai_bridge', None)
                if client is None:
                    response = JSONResponse({'detail': {'code': 'ai_access_not_configured'}}, status_code=503)
                else:
                    try:
                        await client.check_network(request.client.host if request.client else '')
                    except HTTPException as failure:
                        response = JSONResponse({'detail': failure.detail}, status_code=failure.status_code)
                    else:
                        response = await call_next(request)
                response.headers.update(
                    {
                        'Cache-Control': 'no-store',
                        'Referrer-Policy': 'no-referrer',
                        'X-Content-Type-Options': 'nosniff',
                    }
                )
            else:
                response = await call_next(request)
            return response
