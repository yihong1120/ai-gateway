"""Create the Open WebUI application with the local API routers installed."""
from __future__ import annotations

from fastapi import FastAPI
from open_webui.main import app as webui_app

from .lifespan import WorkflowLifespan
from .routes import router
from company_agent.api.router import install as install_company_agent


def create_app() -> FastAPI:
    """Return the shared Open WebUI application with both local APIs installed."""
    webui_app.router.lifespan_context = WorkflowLifespan(
        webui_app.router.lifespan_context,
    )
    webui_app.router.routes[0:0] = router.routes
    install_company_agent(webui_app)
    return webui_app
