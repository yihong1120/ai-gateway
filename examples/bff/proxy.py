"""BFF proxy operations."""

from __future__ import annotations

from fastapi import Request

from fastapi.responses import Response

from redis.asyncio import Redis

from examples.identity.deployment_context import DeploymentBinding

from examples.bff.proxy_config import resolve_upstream
from examples.bff.proxy_headers import _proxy_response_headers
from examples.bff.proxy_http import _send_proxy_request
from examples.bff.proxy_responses import rewrite_response
from examples.bff.proxy_streams import _is_sse_request, _proxy_streaming_request
from examples.bff.proxy_tokens import _get_proxy_access_token_or_503


async def proxy_request(
    request: Request,
    redis: Redis,
    session_id: str,
    path: str,
    *,
    deployment: DeploymentBinding | None = None,
) -> Response:
    """Forward an authenticated request to an allow-listed upstream service.

    Args:
        request: Original browser request.
        redis: Redis connection holding BFF token state.
        session_id: Opaque browser session identifier.
        path: Service-prefixed allow-listed upstream path.

    Returns:
        Non-cacheable upstream HTTP or streaming response.

    Raises:
        HTTPException: If authentication, routing, or upstream communication
            fails.
    """
    base, suffix = resolve_upstream(path)
    url = f'{base}/{suffix}' if suffix else base

    access_token, _ = await _get_proxy_access_token_or_503(
        redis,
        session_id,
        request.url.path,
        deployment=deployment,
    )
    if request.method == 'GET' and _is_sse_request(request, suffix):
        return await _proxy_streaming_request(
            request,
            redis,
            session_id,
            url,
            access_token,
            deployment,
        )

    upstream = await _send_proxy_request(
        request,
        url,
        access_token,
        deployment,
    )
    if upstream.status_code == 401:
        access_token, _ = await _get_proxy_access_token_or_503(
            redis,
            session_id,
            request.url.path,
            force_refresh=True,
            deployment=deployment,
        )
        upstream = await _send_proxy_request(
            request,
            url,
            access_token,
            deployment,
        )

    response_content, content_rewritten = rewrite_response(
        upstream, request, deployment, path.strip('/').partition('/')[0], suffix,
    )

    return Response(
        content=response_content,
        status_code=upstream.status_code,
        headers=_proxy_response_headers(
            upstream,
            content_rewritten=content_rewritten,
        ),
    )


from examples.bff.proxy_tokens import get_proxy_access_token

__all__ = ['proxy_request', 'resolve_upstream', 'get_proxy_access_token']
