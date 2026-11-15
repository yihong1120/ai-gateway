"""Authenticated model routes; transport owns upstream HTTP and streaming."""
from __future__ import annotations

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Request

import examples.company_ai_access.protocols.responses as _responses_imports
from examples.company_ai_access.errors import error
from examples.company_ai_access.protocols.messages import envelope as message_envelope
from examples.company_ai_access.routers.dependencies import client_ip
from examples.company_ai_access.routers.dependencies import limited_json
from examples.company_ai_access.routers.dependencies import require_inference_network
from examples.company_ai_access.routers.dependencies import service
from examples.company_ai_access.routers.dependencies import validate_inference_query


def inference_router():
    router = APIRouter(prefix='/company-ai', dependencies=[Depends(require_inference_network)])

    async def authenticated(request):
        state = service(request)
        await state.throttle('inference', client_ip(request), 300, 60)
        validate_inference_query(request)
        authorization = request.headers.get('authorization', '')
        raw = authorization[7:] if authorization.startswith('Bearer ') else request.headers.get('x-api-key', '')
        if not raw or len(raw) > 256:
            raise error(401, 'ai_access_invalid_key')
        return raw, await state.authorize(raw)

    @router.get('/v1/models')
    async def models(request: Request):
        await authenticated(request)
        state = service(request)
        return {
            'object': 'list',
            'has_more': False,
            'mcp': state.settings.mcp_policy,
            'data': [
                {
                    'id': name,
                    'object': 'model',
                    'display_name': name,
                    'created_at': '2026-01-01T00:00:00Z',
                }
                for name in state.settings.models
            ],
        }

    @router.get('/v1/codex/install-policy')
    async def codex_install_policy(request: Request):
        # LAN/VPN gate applies, but no device key is needed to install the CLI
        # before approval. Never disclose identity, credentials or a catalog.
        validate_inference_query(request)
        settings = service(request).settings
        return {
            'allowed': True,
            'codex_enabled': bool(settings.codex_models),
            'codex_release': settings.codex_release or None,
        }

    @router.get('/v1/codex/config')
    async def codex_config(request: Request):
        _, device = await authenticated(request)
        settings = service(request).settings
        if not settings.codex_models:
            raise error(503, 'ai_access_codex_not_enabled')
        if not device.get('codex_enabled', False):
            raise error(403, 'ai_access_codex_enrollment_required')
        return {
            'models': list(settings.codex_models),
            'default_model': settings.codex_models[0],
            'release': settings.codex_release,
            'catalog': settings.codex_catalog,
            'base_url': settings.origin + '/company-ai/v1',
            'mcp': settings.mcp_policy,
        }

    @router.post('/v1/responses')
    async def responses(request: Request):

        raw, device = await authenticated(request)
        state = service(request)
        if not state.settings.codex_models:
            raise error(503, 'ai_access_codex_not_enabled')
        if not device.get('codex_enabled', False):
            raise error(403, 'ai_access_codex_enrollment_required')
        value = await limited_json(request)
        if value.get('model') not in state.settings.codex_models:
            raise error(403, 'ai_access_model_not_allowed')
        body = _responses_imports.envelope(value)
        body['user'] = device['subject']
        return await request.app.state.company_ai_inference.forward(request, raw, device, body, '/v1/responses')

    @router.post('/v1/messages')
    @router.post('/v1/messages/count_tokens')
    async def messages(request: Request):
        raw, device = await authenticated(request)
        state = service(request)
        value = await limited_json(request)
        if value.get('model') not in state.settings.models:
            raise error(403, 'ai_access_model_not_allowed')
        body = message_envelope(value)
        path = '/v1/messages/count_tokens' if request.url.path.endswith('/count_tokens') else '/v1/messages'
        return await request.app.state.company_ai_inference.forward(request, raw, device, body, path)

    return router
