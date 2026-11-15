"""BFF http operations."""
from __future__ import annotations

import httpx
from anyio import CancelScope
from fastapi import HTTPException
from fastapi import Request

from examples.bff.proxy_headers import _proxy_request_headers
from examples.identity.deployment_context import DeploymentBinding
from examples.identity.settings.bff import get_bff_options
from examples.shared.runtime.http_clients import HttpClientPool


async def _send_proxy_request(
    request: Request,
    url: str,
    access_token: str,
    deployment: DeploymentBinding | None = None,
) -> httpx.Response:
    """Send one non-streaming request to an allow-listed upstream service.

    Args:
        request: Original browser request.
        url: Fully resolved allow-listed upstream URL.
        access_token: Server-side access token for the upstream request.

    Returns:
        Complete upstream HTTP response.

    Raises:
        HTTPException: If the upstream connection cannot be established.
    """
    client, close_client = await _proxy_http_client(request)
    try:
        return await client.request(
            request.method,
            url,
            params=request.query_params,
            content=await request.body(),
            headers=_proxy_request_headers(
                request,
                access_token,
                deployment,
                upstream_url=url,
            ),
        )
    except httpx.TransportError as exc:
        raise HTTPException(
            status_code=502,
            detail='bff_upstream_unavailable',
        ) from exc
    finally:
        if close_client:
            with CancelScope(shield=True):
                await client.aclose()


async def _proxy_http_client(
    request: Request,
) -> tuple[httpx.AsyncClient, bool]:
    """Return the application pool client or a disposable test fallback."""
    app = getattr(request, 'app', None)
    state = getattr(app, 'state', None)
    pool = getattr(state, 'http_clients', None)
    if isinstance(pool, HttpClientPool):
        return (
            await pool.get(
                'bff-upstream',
            ),
            False,
        )
    return (
        httpx.AsyncClient(
            timeout=get_bff_options().timeout_seconds,
            follow_redirects=False,
        ),
        True,
    )
