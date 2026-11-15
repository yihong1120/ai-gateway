"""Bind the WebUI usage reader to one application and its HTTP lifespan."""
from __future__ import annotations

import os
from contextlib import AsyncExitStack, asynccontextmanager
from decimal import Decimal

import httpx

from examples.ai_usage.config import UsageQuerySettings
from examples.ai_usage.services.litellm_usage import LiteLLMUsageClient
from examples.shared.runtime.installation import install_once


def settings_from_environment() -> UsageQuerySettings | None:
    """Keep the WebUI's reader credential and deployment settings isolated."""
    reader_key = os.getenv('LITELLM_USAGE_READER_KEY', '')
    if not reader_key:
        return None
    local_models = os.getenv('AI_USAGE_LOCAL_MODEL_NAMES')
    options = {'local_model_names': frozenset(name.strip() for name in local_models.split(',') if name.strip())} \
        if local_models is not None else {}
    return UsageQuerySettings(
        litellm_base_url=os.getenv('LITELLM_USAGE_BASE_URL', 'http://127.0.0.1:4000').rstrip('/'),
        litellm_reader_key=reader_key,
        daily_budget_usd=Decimal(os.getenv('LITELLM_DAILY_BUDGET_USD', '4.00')),
        max_log_pages=10,
        **options,
    )


def install_webui_usage(app) -> None:
    """Install once; requests use the captured settings and shared transport."""
    with install_once(app, 'daily_usage_installed') as installing:
        if not installing:
            return
        settings = settings_from_environment()
        original = app.router.lifespan_context
        app.state.daily_usage = None

        @asynccontextmanager
        async def lifespan(current):
            async with AsyncExitStack() as cleanup:
                if settings is not None:
                    transport = await cleanup.enter_async_context(httpx.AsyncClient(
                        base_url=settings.litellm_base_url,
                        timeout=httpx.Timeout(settings.request_timeout_seconds, connect=5.0),
                        follow_redirects=False,
                        trust_env=False,
                    ))
                    current.state.daily_usage = LiteLLMUsageClient(settings, transport)
                try:
                    await cleanup.enter_async_context(original(current))
                    yield
                finally:
                    current.state.daily_usage = None

        app.router.lifespan_context = lifespan
