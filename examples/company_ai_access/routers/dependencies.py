"""HTTP identity, request bounds and access-policy dependencies."""
from __future__ import annotations

import asyncio
import json
import secrets
from ipaddress import ip_address

import jwt
from fastapi import Request

from examples.company_ai_access.errors import error
from examples.company_ai_access.schemas import Principal


def service(request):
    value = getattr(request.app.state, 'company_ai_access', None)
    if value is None:
        raise error(503, 'ai_access_not_configured')
    return value


def mutation(request: Request):
    state = service(request)
    allowed = (state.settings.origin, *state.settings.control_origins)
    if request.headers.get('origin') not in allowed or request.headers.get('x-company-ai-action') != '1':
        raise error(403, 'ai_access_same_origin_required')
    if request.headers.get('sec-fetch-site') not in (None, 'same-origin'):
        raise error(403, 'ai_access_same_origin_required')


def client_ip(request):
    # The hosting ASGI server must trust forwarded headers ONLY from its
    # explicitly configured reverse proxy. Never read an arbitrary X-Real-IP.
    address = request.client.host if request.client else ''
    settings = service(request).settings
    bridge = settings.bridge_key
    if (
        address in {'127.0.0.1', '::1'}
        and bridge
        and secrets.compare_digest(request.headers.get('x-company-ai-bridge', ''), bridge)
    ):
        return request.headers.get('x-company-ai-client-ip', '')
    # The website BFF preserves its normal OIDC principal. Its separate proof
    # authenticates ONLY provenance, never a subject or administrator assertion.
    network_proxy = settings.network_proxy_key
    if (
        address in {'127.0.0.1', '::1'}
        and network_proxy
        and secrets.compare_digest(request.headers.get('x-company-ai-network-proxy', ''), network_proxy)
    ):
        return request.headers.get('x-company-ai-client-ip', '')
    if request.headers.get('x-company-ai-network-proxy'):
        return ''  # A bad provenance proof must not fall back to loopback.
    if (
        network_proxy
        and address in {'127.0.0.1', '::1'}
        and request.url.path.startswith('/api/company-ai/')
        and request.headers.get('authorization')
    ):
        return ''  # OIDC control forwarding must authenticate its source.
    return address


def require_inference_network(request: Request):
    state = service(request)
    try:
        address = ip_address(client_ip(request))
        allowed = any(address in network for network in state.settings.inference_networks)
        allowed = allowed and not any(address in network for network in state.settings.inference_denied_networks)
    except ValueError:
        allowed = False
    if not allowed:
        raise error(403, 'ai_access_network_denied')


async def limited_json(request, maximum=8 * 1024 * 1024, *, allow_empty=False):
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > maximum:
            raise error(413, 'ai_access_request_too_large')
    if not body and allow_empty:
        return {}
    try:
        value = json.loads(body)
    except (ValueError, UnicodeError):
        raise error(400, 'ai_access_invalid_json')
    if not isinstance(value, dict):
        raise error(400, 'ai_access_invalid_json')
    return value


def validate_inference_query(request):
    """Accept only the SDK's exact beta routing hint, never query credentials."""
    query = request.scope.get('query_string', b'')
    if not query:
        return
    if (
        request.method == 'POST'
        and request.url.path in {'/company-ai/v1/messages', '/company-ai/v1/messages/count_tokens'}
        and query == b'beta=true'
    ):
        return
    raise error(400, 'ai_access_query_not_allowed')


def browser_principal(settings):
    bridge = settings.bridge_key
    if not settings.oidc_audience and len(bridge) < 32:
        raise ValueError('Company AI API requires a fixed OIDC audience or a private loopback bridge.')
    # Bridge-only deployments never accept external OIDC bearer tokens. Avoid
    # initializing a JWKS client for an intentionally absent issuer in that mode.
    verifier = (
        jwt.PyJWKClient(settings.issuer + '/protocol/openid-connect/certs', timeout=5)
        if settings.oidc_audience
        else None
    )

    async def principal(request: Request):
        if (
            request.client
            and request.client.host in {'127.0.0.1', '::1'}
            and bridge
            and secrets.compare_digest(request.headers.get('x-company-ai-bridge', ''), bridge)
        ):
            subject = request.headers.get('x-company-ai-subject', '')
            if not subject or len(subject) > 128:
                raise error(401, 'ai_access_login_required')
            return Principal(subject, request.headers.get('x-company-ai-admin') == '1')
        if not settings.oidc_audience:
            raise error(401, 'ai_access_login_required')
        authorization = request.headers.get('authorization', '')
        if not authorization.startswith('Bearer '):
            raise error(401, 'ai_access_login_required')
        raw = authorization[7:]
        try:
            key = await asyncio.to_thread(verifier.get_signing_key_from_jwt, raw)
            claims = jwt.decode(
                raw,
                key.key,
                algorithms=['RS256'],
                audience=settings.oidc_audience,
                issuer=settings.issuer,
                options={'require': ['iss', 'aud', 'iat', 'exp', 'sub']},
            )
            if not isinstance(claims['sub'], str) or not claims['sub']:
                raise ValueError()
        except (jwt.PyJWTError, ValueError):
            raise error(401, 'ai_access_login_required')
        # Administrative access comes from the configured client's CURRENT
        # directory roles, never arbitrary or stale JWT realm-role claims.
        return Principal(claims['sub'], await service(request).directory.is_admin(claims['sub']))

    return principal
