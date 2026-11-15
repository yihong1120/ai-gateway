"""Create the Open WebUI application with the local API routers installed."""

from __future__ import annotations

import os

from fastapi import FastAPI

from examples.shared.config import load_service

load_service('webui')
os.environ['PYTHON_DOTENV_DISABLED'] = '1'

from open_webui.main import app as webui_app

from company_agent.api.router import install as install_company_agent
from company_agent.api.status_ui import install as install_status_ui
from company_mcp.adapters.webui import install as install_company_resources
from examples.ai_usage.adapters.webui import install_webui_usage
from examples.company_ai_access.adapters.webui import (
    install_webui as install_company_ai_access,
)
from examples.official_document_workflow.api.lifespan import WorkflowLifespan
from examples.official_document_workflow.api.routes import router
from examples.official_document_workflow.runtime import WorkflowMiddleware
from examples.shared.runtime.database import DatabasePoolMiddleware
from examples.shared.runtime.installation import install_once


def create_app() -> FastAPI:
    """Return the Open WebUI host with the local services installed once."""
    with install_once(webui_app, 'webui_integrations_installed') as installing:
        if installing:
            webui_app.add_middleware(DatabasePoolMiddleware, owner=webui_app)
            webui_app.add_middleware(WorkflowMiddleware, owner=webui_app)
            webui_app.router.lifespan_context = WorkflowLifespan(
                webui_app.router.lifespan_context,
            )
            webui_app.router.routes[0:0] = router.routes
            install_webui_usage(webui_app)
            install_company_agent(webui_app)
            install_company_ai_access(webui_app)
            install_status_ui(webui_app)
            install_company_resources(webui_app)
    return webui_app
