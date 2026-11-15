"""Authentication and runtime dependencies for the AI usage API."""
from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any
from typing import cast

import httpx
from fastapi import HTTPException
from fastapi import Request
from fastapi import Security
from fastapi import status
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.security import HTTPBearer
from jwt.exceptions import InvalidTokenError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from examples.ai_usage.config import AiUsageSettings
from examples.identity.providers.oidc import OidcTokenVerifier


_bearer = HTTPBearer(auto_error=False)


def get_settings(request: Request) -> AiUsageSettings:
    """Return settings created by the application lifespan."""
    return cast(AiUsageSettings, request.app.state.settings)


def get_litellm_http_client(request: Request) -> httpx.AsyncClient:
    """Return the lifespan-owned LiteLLM client."""
    return cast(httpx.AsyncClient, request.app.state.litellm_http_client)


async def get_current_claims(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer),
) -> dict[str, Any]:
    """Verify a Keycloak access token before any directory lookup."""
    if credentials is None or not credentials.credentials:
        raise _authentication_error('missing_access_token')

    verifier = cast(OidcTokenVerifier, request.app.state.oidc_verifier)
    try:
        claims = await verifier.decode_access_token(credentials.credentials)
    except InvalidTokenError as exc:
        raise _authentication_error('invalid_access_token') from exc

    subject = claims.get('sub')
    if not isinstance(subject, str) or not subject.strip():
        raise _authentication_error('invalid_access_token')
    return claims


async def get_current_subject(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer),
) -> str:
    """Keep the personal API's immutable accounting identity unchanged."""
    claims = await get_current_claims(request, credentials)
    return str(claims['sub']).strip()


async def get_admin_db(request: Request) -> AsyncGenerator[AsyncSession]:
    """Open a request-local directory session only for administrator routes."""
    factory = getattr(request.app.state, 'admin_session_factory', None)
    if factory is None:
        raise HTTPException(
            status_code=503,
            detail={'code': 'ai_usage_admin_not_configured'},
        )
    try:
        async with factory() as session:
            yield session
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=503,
            detail={'code': 'ai_usage_directory_unavailable'},
        ) from exc


def _authentication_error(code: str) -> HTTPException:
    """Build a generic bearer-token failure without exposing verifier detail."""
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={'code': code},
        headers={'WWW-Authenticate': 'Bearer'},
    )
