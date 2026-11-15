"""Optional, application-owned external tool connections without domain code."""
from __future__ import annotations

from contextlib import asynccontextmanager


@asynccontextmanager
async def external_tools_connection(app, servers):
    if not servers:
        yield None
        return
    manager = getattr(app.state, 'oauth_manager', None)
    if manager is None:
        raise ValueError('外部工具需要宿主 OIDC 登入服務。')

    # Disabled deployments can load the application without MCP SDK modules.
    from company_agent.services.external_tools.client import ExternalTools
    from company_agent.services.external_tools.identity import WebUIUserTokens

    yield ExternalTools(servers, WebUIUserTokens(manager))
