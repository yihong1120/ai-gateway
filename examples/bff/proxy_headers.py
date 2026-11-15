"""BFF headers operations."""

from __future__ import annotations

from urllib.parse import urlsplit

import httpx

from fastapi import HTTPException

from fastapi import Request

from examples.identity.deployment_context import DeploymentBinding

from examples.identity.settings.bff import get_bff_options
from examples.bff.proxy_config import _DROP_REQUEST_HEADERS, _PASS_RESPONSE_HEADERS


def _proxy_request_headers(
    request: Request,
    access_token: str,
    deployment: DeploymentBinding | None = None,
    *,
    upstream_url: str | None = None,
) -> dict[str, str]:
    """Build upstream headers without forwarding browser credentials.

    Args:
        request: Original browser request.
        access_token: Server-side access token for the upstream request.

    Returns:
        Sanitised upstream headers with BFF authentication markers.
    """
    headers = {name: value for name, value in request.headers.items() if name.lower() not in _DROP_REQUEST_HEADERS}
    headers['Authorization'] = f'Bearer {access_token}'
    headers['X-BFF-Request'] = '1'
    ai_base = get_bff_options().upstreams['ai_access'].rstrip('/')
    if upstream_url and (upstream_url == ai_base or upstream_url.startswith(ai_base + '/')):
        proof = get_bff_options().network_proxy_key
        if len(proof) < 32:
            raise HTTPException(503, detail='company_ai_network_proxy_not_configured')
        headers['X-Company-AI-Network-Proxy'] = proof
        headers['X-Company-AI-Client-IP'] = request.client.host if request.client else ''
    if deployment is not None:
        # The internal services resolve their deployment from the request's
        # scheme and Host.  ``url`` points to a loopback upstream, so retain
        # the verified public deployment authority instead of forwarding an
        # untrusted browser-supplied Host/origin.
        public_url = urlsplit(deployment.api_base_url)
        headers['Host'] = public_url.netloc
        headers['X-Forwarded-Host'] = public_url.netloc
        headers['X-Forwarded-Proto'] = public_url.scheme
    return headers


def _proxy_response_headers(
    upstream: httpx.Response,
    *,
    streaming: bool = False,
    content_rewritten: bool = False,
) -> dict[str, str]:
    """Select safe upstream response headers for a browser response.

    Args:
        upstream: Response received from the allow-listed service.
        streaming: Whether the response is an SSE stream.

    Returns:
        Safe response headers with BFF no-store policy applied.
    """
    headers = {name: value for name, value in upstream.headers.items() if name.lower() in _PASS_RESPONSE_HEADERS}
    headers['Cache-Control'] = 'no-store'
    if content_rewritten:
        # The BFF serialised a different JSON body, so the upstream validator
        # would no longer describe the bytes being returned to the browser.
        headers.pop('etag', None)
    if streaming:
        headers['X-Accel-Buffering'] = 'no'
    return headers
