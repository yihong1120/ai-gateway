"""Authentication and runtime dependencies for the AI usage API."""
from __future__ import annotations

from typing import cast

import httpx
from fastapi import HTTPException
from fastapi import Request
from fastapi import Security
from fastapi import status
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.security import HTTPBearer
from jwt.exceptions import InvalidTokenError

from examples.ai_usage.config import AiUsageSettings
from examples.auth.oidc import OidcTokenVerifier


_bearer = HTTPBearer(auto_error=False)


def get_settings(request: Request) -> AiUsageSettings:
    """Return settings created by the application lifespan."""
    return cast(AiUsageSettings, request.app.state.settings)


def get_litellm_http_client(request: Request) -> httpx.AsyncClient:
    """Return the lifespan-owned LiteLLM client."""
    return cast(httpx.AsyncClient, request.app.state.litellm_http_client)


async def get_current_subject(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer),
) -> str:
    """Verify Keycloak access token and return its immutable OIDC ``sub``."""
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
    return subject.strip()


def _authentication_error(code: str) -> HTTPException:
    """Build a generic bearer-token failure without exposing verifier detail."""
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={'code': code},
        headers={'WWW-Authenticate': 'Bearer'},
    )
