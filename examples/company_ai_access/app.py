"""Standalone API for the company Flutter site's BFF allowlisted upstream."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from examples.company_ai_access.routers.assets import assets_router
from examples.company_ai_access.routers.control import create_router
from examples.company_ai_access.routers.inference import inference_router
from examples.company_ai_access.routers.dependencies import browser_principal, require_inference_network
from examples.company_ai_access.config import Settings
from examples.company_ai_access.runtime import runtime
from examples.company_ai_access.routers.middleware import BodyLimit


def create_app():
    settings = Settings.from_env()
    principal = browser_principal(settings)

    @asynccontextmanager
    async def lifespan(app):
        async with runtime(app, settings):
            yield

    app = FastAPI(title='Company AI Access', version='1.0.0', lifespan=lifespan)
    app.add_middleware(BodyLimit)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Never echo the submitted verifier or any credential-bearing input.
        return JSONResponse(
            status_code=422,
            content={
                'detail': {
                    'code': 'ai_access_invalid_request',
                    'errors': [{'loc': list(item['loc']), 'type': item['type']} for item in exc.errors()],
                }
            },
        )

    @app.get('/health', include_in_schema=False)
    async def health():
        return {
            'status': getattr(app.state, 'company_ai_access', None) is not None,
            'environment': settings.environment,
        }

    app.include_router(create_router(principal))
    app.include_router(
        assets_router(
            browser_api_prefix='/bff/ai_access/api/company-ai' if settings.oidc_audience else '/api/company-ai',
            login_path='/bff/auth/oidc/login?return_to=/company-ai/setup' if settings.oidc_audience else '/auth',
        )
    )
    app.include_router(inference_router())

    @app.middleware('http')
    async def private(request: Request, call_next):
        if request.url.path.startswith(('/api/company-ai', '/company-ai')):
            try:
                require_inference_network(request)
            except HTTPException as exc:
                response = JSONResponse(status_code=exc.status_code, content={'detail': exc.detail})
            else:
                response = await call_next(request)
        else:
            response = await call_next(request)
        response.headers.update(
            {
                'Cache-Control': 'no-store',
                'Referrer-Policy': 'no-referrer',
                'X-Content-Type-Options': 'nosniff',
            }
        )
        return response

    return app
