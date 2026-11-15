"""BFF config operations."""

from __future__ import annotations

import logging


from fastapi import HTTPException

logger = logging.getLogger('uvicorn.error')

_DROP_REQUEST_HEADERS = {
    'x-company-ai-network-proxy',
    'x-company-ai-bridge',
    'x-company-ai-subject',
    'x-company-ai-admin',
    'x-company-ai-client-ip',
    'authorization',
    'connection',
    'content-length',
    'cookie',
    'host',
    'proxy-authorization',
    'te',
    'trailer',
    'transfer-encoding',
    'upgrade',
    'x-csrf-token',
    'x-forwarded-host',
    'x-forwarded-proto',
}

_PASS_RESPONSE_HEADERS = {
    'accept-ranges',
    'content-disposition',
    'content-language',
    'content-range',
    'content-type',
    'etag',
    'last-modified',
    'retry-after',
}

_VIOLATION_MEDIA_FIELDS = frozenset({'image_url', 'thumbnail_url'})

_VIOLATION_MEDIA_ENDPOINTS = frozenset(
    {'/get_violation_image', '/get_violation_thumbnail'},
)


def resolve_upstream(path: str) -> tuple[str, str]:
    """Resolve an allow-listed service name without accepting a caller URL.

    Args:
        path: Service-prefixed BFF path.

    Returns:
        Upstream base URL and remaining path suffix.

    Raises:
        HTTPException: If the service name is not allow-listed.
    """
    route, separator, suffix = path.strip('/').partition('/')
    base = get_bff_options().upstreams.get(route)
    if not base:
        raise HTTPException(status_code=404, detail='bff_route_not_allowed')
    return base.rstrip('/'), suffix if separator else ''


from examples.identity.settings.bff import get_bff_options
