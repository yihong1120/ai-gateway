"""Authenticated, self-service LiteLLM usage endpoints."""
from __future__ import annotations

from typing import Annotated

import httpx
from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from fastapi import status

from examples.ai_usage.config import AiUsageSettings
from examples.ai_usage.deps import get_current_subject
from examples.ai_usage.deps import get_litellm_http_client
from examples.ai_usage.deps import get_settings
from examples.ai_usage.schemas.usage import DailyUsageResponse
from examples.ai_usage.services.litellm_usage import IncompleteUsageData
from examples.ai_usage.services.litellm_usage import LiteLLMUsageClient
from examples.ai_usage.services.litellm_usage import LiteLLMUsageUnavailable

router = APIRouter(prefix='/v1/me/usage', tags=['ai-usage'])


@router.get('/daily', response_model=DailyUsageResponse)
async def get_my_daily_usage(
    record_limit: Annotated[int, Query(ge=1)] = 50,
    subject: str = Depends(get_current_subject),
    settings: AiUsageSettings = Depends(get_settings),
    http_client: httpx.AsyncClient = Depends(get_litellm_http_client),
) -> DailyUsageResponse:
    """Return only the caller's current Taiwan-local daily AI usage.

    ``record_limit`` controls display volume only.  The budget and model
    aggregates are calculated from every retrievable current-day record; a
    client cannot select another person, date, model, or LiteLLM query field.
    """
    if record_limit > settings.max_record_limit:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                'code': 'record_limit_too_large',
                'maximum': settings.max_record_limit,
            },
        )

    usage_client = LiteLLMUsageClient(settings, http_client)
    try:
        return await usage_client.daily_usage(subject, record_limit=record_limit)
    except IncompleteUsageData as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={'code': 'ai_usage_data_incomplete'},
        ) from exc
    except LiteLLMUsageUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={'code': 'ai_usage_upstream_unavailable'},
        ) from exc
