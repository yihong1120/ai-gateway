"""Standalone private API for Visionnaire users' LiteLLM usage dashboard."""
from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from uuid import uuid4

import httpx
import uvicorn
from fastapi import FastAPI
from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.responses import Response

from examples.ai_usage.config import AiUsageSettings
from examples.ai_usage.routers.usage import router as usage_router
from examples.auth.oidc import OidcTokenVerifier


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    """Create the one shared internal LiteLLM client for this process."""
    settings = AiUsageSettings.from_environment()
    app.state.settings = settings
    app.state.oidc_verifier = OidcTokenVerifier(
        issuer=settings.oidc_issuer_url,
        jwks_url=settings.oidc_jwks_url,
        audiences=settings.oidc_audiences,
        algorithms=settings.oidc_algorithms,
        jwks_cache_seconds=settings.oidc_jwks_cache_seconds,
        jwks_timeout_seconds=settings.oidc_jwks_timeout_seconds,
    )
    timeout = httpx.Timeout(
        settings.request_timeout_seconds,
        connect=min(5.0, settings.request_timeout_seconds),
    )
    async with httpx.AsyncClient(
        base_url=settings.litellm_base_url,
        timeout=timeout,
        follow_redirects=False,
    ) as client:
        app.state.litellm_http_client = client
        yield


app = FastAPI(
    title='Visionnaire AI Usage API',
    version='1.0.0',
    description=(
        'Private current-day LiteLLM usage for the authenticated Keycloak user.'
    ),
    lifespan=lifespan,
)


@app.middleware('http')
async def request_id_and_safe_error_response(
    request: Request,
    call_next,
) -> Response:
    """Attach trace IDs while avoiding request data and secret leakage."""
    request_id = str(uuid4())
    request.state.request_id = request_id
    try:
        response = await call_next(request)
    except Exception:  # pragma: no cover - last-resort guard
        response = JSONResponse(
            status_code=500,
            content={
                'detail': {
                    'code': 'internal_server_error',
                    'request_id': request_id,
                },
            },
        )
    response.headers['X-Request-ID'] = request_id
    if request.url.path.startswith('/v1/me/'):
        response.headers['Cache-Control'] = 'no-store'
    return response


app.include_router(usage_router)


def main() -> None:
    """Run the AI usage API locally."""
    uvicorn.run(app, host='127.0.0.1', port=8012, workers=4)


if __name__ == '__main__':
    main()


"""
uvicorn examples.ai_usage.app:app \
    --host 127.0.0.1 --port 8012 --workers 4
"""
