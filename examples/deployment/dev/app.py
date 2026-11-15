"""Dev app uses production modules with independent state and no identity writes."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import HTTPException
from starlette.responses import JSONResponse
from starlette.routing import Match

import company_agent.ops.deploy_webui as _deploy_webui_imports
import examples.deployment.dev.state as _state_imports
from examples.shared.runtime.modules import ModuleReference

_agent_identity = ModuleReference('company_agent.api.identity')
_webui_app = ModuleReference('examples.openwebui.app')
_webui_config = ModuleReference('open_webui.models.config')


async def dev_role(user, user_data, *, token_claims=None):
    # Match Open WebUI's role hook contract. Userinfo wins; only fall back to
    # claims already verified by the OAuth manager when the claim is absent.
    roles = user_data.get('dev_roles')
    if roles is None and token_claims is not None:
        roles = token_claims.get('dev_roles')
    if (
        not isinstance(roles, list) or not all(isinstance(role, str) for role in roles)
        or not set(roles).intersection({'dev-user', 'dev-admin'})
    ):
        raise HTTPException(403, 'Keycloak DEV membership is required.')
    return 'admin' if 'dev-admin' in roles else 'user'


def create_app():
    _state_imports.ensure_keys()
    factory = _webui_app.attribute('create_app')
    router = _agent_identity.attribute('router')
    app = factory()
    # Enforce dev membership even for the first/single user bootstrap shortcut.
    app.state.oauth_manager.get_user_role = dev_role
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(app):
        async with original_lifespan(app):
            Config = _webui_config.attribute('Config')
            banners = _deploy_webui_imports.updated_banners(await Config.get('ui.banners') or [])
            for banner in banners:
                if banner['id'] == 'company-account-connections':
                    banner['content'] = '**[帳號與連線：EIP／NAS 綁定](/connections)**　DEV 共用登入身分，不提供共用 Keycloak 帳號管理。'
            await Config.upsert({'ui.banners': banners})
            yield

    app.router.lifespan_context = lifespan

    @app.middleware('http')
    async def protect_identity(request, call_next):
        if any(route.matches(request.scope)[0] != Match.NONE for route in router.routes):
            response = JSONResponse({'detail': 'DEV does not manage shared Keycloak accounts.'}, status_code=403)
        else:
            response = await call_next(request)
        response.headers['X-AI-Environment'] = 'dev'
        return response

    return app
