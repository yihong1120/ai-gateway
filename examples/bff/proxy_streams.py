"""BFF streams operations."""

from __future__ import annotations

import asyncio
from anyio import CancelScope

import json

from collections.abc import AsyncIterator

import httpx

from fastapi import HTTPException

from fastapi import Request

from fastapi.responses import StreamingResponse

from redis.asyncio import Redis

from examples.identity.deployment_context import DeploymentBinding

from examples.shared.runtime.http_clients import HttpClientPool

from examples.identity.settings.bff import get_bff_options
from examples.bff.proxy_config import logger
from examples.bff.proxy_headers import _proxy_request_headers, _proxy_response_headers
from examples.bff.proxy_tokens import _get_proxy_access_token_or_503


def _is_sse_request(request: Request, suffix: str) -> bool:
    """Return whether the proxied request should remain streaming.

    Args:
        request: Browser request whose accepted media types are inspected.
        suffix: Path after the allow-listed upstream service name.

    Returns:
        ``True`` when the upstream response must not be buffered.
    """
    accept = request.headers.get('accept', '').lower()
    return 'text/event-stream' in accept or suffix.startswith(
        'metadata/stream-id/',
    )


def _log_sse_error_events(
    buffer: bytearray,
    chunk: bytes,
    *,
    request_path: str,
    upstream_status: int,
) -> None:
    """Log structured error events without changing streamed SSE bytes.

    Args:
        buffer: Partial SSE event bytes retained across chunks.
        chunk: Newly received SSE bytes.
        request_path: Original BFF request path for observability.
        upstream_status: Status returned by the upstream service.
    """
    buffer.extend(chunk)
    while b'\n\n' in buffer:
        raw_event, _, remaining = buffer.partition(b'\n\n')
        buffer[:] = remaining
        event_type = 'message'
        data_lines: list[str] = []
        for line in bytes(raw_event).decode('utf-8', errors='replace').splitlines():
            field, separator, value = line.partition(':')
            if not separator:
                continue
            if field == 'event':
                event_type = value.lstrip()
            elif field == 'data':
                data_lines.append(value.lstrip())
        if event_type not in {'error', 'redis_error'}:
            continue
        try:
            payload = json.loads('\n'.join(data_lines))
        except json.JSONDecodeError:
            payload = {}
        if payload.get('source') == 'redis':
            logger.warning(
                'BFF SSE Redis error path=%s upstream_status=%s code=%s',
                request_path,
                upstream_status,
                payload.get('code', 'unknown'),
            )
        else:
            logger.warning(
                'BFF SSE upstream error event path=%s upstream_status=%s',
                request_path,
                upstream_status,
            )


async def _proxy_streaming_request(
    request: Request,
    redis: Redis,
    session_id: str,
    url: str,
    access_token: str,
    deployment: DeploymentBinding | None = None,
) -> StreamingResponse:
    """Proxy one long-lived upstream stream without buffering it.

    Args:
        request: Original browser streaming request.
        redis: Redis connection holding BFF token state.
        session_id: Opaque browser session identifier.
        url: Fully resolved allow-listed upstream URL.
        access_token: Current server-side access token.

    Returns:
        Non-buffering response that forwards upstream stream bytes.
    """
    client, upstream, close_client = await _open_proxy_stream(
        request,
        url,
        access_token,
        deployment,
    )
    if upstream.status_code == 401:
        (
            client,
            upstream,
            close_client,
        ) = await _refresh_unauthorized_proxy_stream(
            request,
            redis,
            session_id,
            url,
            client,
            upstream,
            close_client,
            deployment,
        )
    _log_proxy_stream_open(request.url.path, upstream.status_code)

    headers = _proxy_response_headers(upstream, streaming=True)

    return StreamingResponse(
        _proxy_stream_body(
            request.url.path,
            client,
            upstream,
            close_client=close_client,
        ),
        status_code=upstream.status_code,
        headers=headers,
        media_type=headers.get('content-type'),
    )


async def _open_proxy_stream(
    request: Request,
    url: str,
    access_token: str,
    deployment: DeploymentBinding | None = None,
) -> tuple[httpx.AsyncClient, httpx.Response, bool]:
    """Open an upstream SSE response without buffering its body.

    Args:
        request: Original browser streaming request.
        url: Fully resolved allow-listed upstream URL.
        access_token: Server-side access token for the upstream request.

    Returns:
        Open HTTP client, streaming upstream response, and client ownership.

    Raises:
        HTTPException: If the upstream streaming connection cannot be opened.
    """
    client, close_client = await _proxy_sse_http_client(request)
    try:
        upstream_request = client.build_request(
            request.method,
            url,
            params=request.query_params,
            headers=_proxy_request_headers(
                request,
                access_token,
                deployment,
                upstream_url=url,
            ),
        )
        response = await client.send(upstream_request, stream=True)
    except BaseException as exc:
        if close_client:
            with CancelScope(shield=True):
                await client.aclose()
        if not isinstance(exc, httpx.TransportError):
            raise
        logger.warning(
            'BFF SSE upstream connection failed path=%s error_type=%s',
            request.url.path,
            type(exc).__name__,
            exc_info=True,
        )
        raise HTTPException(
            status_code=502,
            detail='bff_upstream_unavailable',
        ) from exc
    return client, response, close_client


async def _proxy_sse_http_client(
    request: Request,
) -> tuple[httpx.AsyncClient, bool]:
    """Return the lifespan SSE client or a disposable non-app fallback."""
    app = getattr(request, 'app', None)
    state = getattr(app, 'state', None)
    pool = getattr(state, 'http_clients', None)
    timeout = httpx.Timeout(get_bff_options().timeout_seconds, read=None)
    if isinstance(pool, HttpClientPool):
        return (
            await pool.get(
                'bff-upstream-sse',
            ),
            False,
        )
    return (
        httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
        ),
        True,
    )


async def _refresh_unauthorized_proxy_stream(
    request: Request,
    redis: Redis,
    session_id: str,
    url: str,
    client: httpx.AsyncClient,
    upstream: httpx.Response,
    close_client: bool,
    deployment: DeploymentBinding | None = None,
) -> tuple[httpx.AsyncClient, httpx.Response, bool]:
    """Refresh a BFF session once after an unauthorised SSE response.

    Args:
        request: Original browser streaming request.
        redis: Redis connection holding BFF token state.
        session_id: Opaque browser session identifier.
        url: Fully resolved allow-listed upstream URL.
        client: Client used by the rejected upstream response.
        upstream: Rejected upstream response to close before retrying.
        close_client: Whether the rejected client is disposable.

    Returns:
        Replacement client and upstream streaming response.
    """
    logger.info(
        'BFF SSE upstream returned 401; refreshing session path=%s',
        request.url.path,
    )
    await upstream.aclose()
    if close_client:
        await client.aclose()
    access_token, _ = await _get_proxy_access_token_or_503(
        redis,
        session_id,
        request.url.path,
        force_refresh=True,
        deployment=deployment,
    )
    return await _open_proxy_stream(
        request,
        url,
        access_token,
        deployment,
    )


def _log_proxy_stream_open(request_path: str, upstream_status: int) -> None:
    """Log the status returned while opening an SSE upstream.

    Args:
        request_path: Original BFF request path.
        upstream_status: Status returned by the upstream service.
    """
    if upstream_status >= 400:
        logger.warning(
            'BFF SSE upstream response path=%s upstream_status=%s',
            request_path,
            upstream_status,
        )
        return
    logger.info(
        'BFF SSE upstream opened path=%s upstream_status=%s',
        request_path,
        upstream_status,
    )


async def _proxy_stream_body(
    request_path: str,
    client: httpx.AsyncClient,
    upstream: httpx.Response,
    *,
    close_client: bool,
) -> AsyncIterator[bytes]:
    """Yield SSE bytes and close its response and any disposable client.

    Args:
        request_path: Original BFF request path for observability.
        client: Open HTTP client used for the upstream stream.
        upstream: Open upstream response whose bytes are yielded.
        close_client: Whether this request, rather than app lifespan, owns the
            HTTP client.

    Yields:
        Unmodified upstream SSE byte chunks.
    """
    error_buffer = bytearray()
    bytes_sent = 0
    close_reason = 'upstream_completed'
    try:
        async for chunk in upstream.aiter_bytes():
            bytes_sent += len(chunk)
            _log_sse_error_events(
                error_buffer,
                chunk,
                request_path=request_path,
                upstream_status=upstream.status_code,
            )
            yield chunk
    except asyncio.CancelledError:
        close_reason = 'downstream_disconnected'
        raise
    except httpx.TransportError as exc:
        close_reason = type(exc).__name__
    except Exception as exc:
        close_reason = type(exc).__name__
        logger.exception(
            'BFF SSE proxy failed path=%s upstream_status=%s reason=%s',
            request_path,
            upstream.status_code,
            close_reason,
        )
        raise
    finally:
        logger.info(
            'BFF SSE upstream closed path=%s upstream_status=%s reason=%s bytes_sent=%s',
            request_path,
            upstream.status_code,
            close_reason,
            bytes_sent,
        )
        with CancelScope(shield=True):
            try:
                await upstream.aclose()
            finally:
                if close_client:
                    await client.aclose()
