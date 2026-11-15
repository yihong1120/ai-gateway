"""Dev app uses production modules with independent state and no identity writes."""
from contextlib import asynccontextmanager
from starlette.responses import JSONResponse
from starlette.routing import Match


async def dev_role(user, user_data, *, token_claims=None):
    from fastapi import HTTPException
    # Match Open WebUI's role hook contract. Userinfo wins; only fall back to
    # claims already verified by the OAuth manager when the claim is absent.
    roles = user_data.get('dev_roles')
    if roles is None and token_claims is not None:
        roles = token_claims.get('dev_roles')
    if (not isinstance(roles, list) or not all(isinstance(role, str) for role in roles)
            or not set(roles).intersection({'dev-user', 'dev-admin'})):
        raise HTTPException(403, 'Keycloak DEV membership is required.')
    return 'admin' if 'dev-admin' in roles else 'user'


def create_app():
    from examples.deployment.dev.state import ensure_keys
    ensure_keys()
    from examples.openwebui.app import create_app as factory
    from company_agent.api.identity import router
    app = factory()
    # Enforce dev membership even for the first/single user bootstrap shortcut.
    app.state.oauth_manager.get_user_role = dev_role
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(app):
        async with original_lifespan(app):
            from open_webui.models.config import Config
            from company_agent.ops.deploy_webui import updated_banners
            banners = updated_banners(await Config.get('ui.banners') or [])
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
