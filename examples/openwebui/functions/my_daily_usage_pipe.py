"""
title: 我的今日額度
author: Visionnaire
version: 1.1.0
required_open_webui_version: 0.11.1
description: Shows only the authenticated user's current-day LiteLLM usage and remaining daily budget.
"""
from __future__ import annotations

from typing import Any

from examples.ai_usage.presentation import render_daily_usage
from examples.ai_usage.services.litellm_usage import (
    IncompleteUsageData,
    LiteLLMUsageError,
)
from examples.identity.providers.subjects import linked_subject


class Pipe:
    def __init__(self) -> None:
        self.name = '我的今日額度（不使用模型）'

    async def pipe(self, body: dict, __user__: dict | None = None, __request__: Any = None) -> str:
        subject = linked_subject((__user__ or {}).get('oauth'))
        if subject is None:
            return '無法確認你的 Keycloak 身分，因此無法安全讀取個人額度。請登出後以 Keycloak 重新登入。'
        reader = getattr(__request__.app.state, 'daily_usage', None) if __request__ is not None else None
        if reader is None:
            return '個人額度查詢尚未完成伺服器端設定；請聯絡管理員。'
        try:
            usage = await reader.daily_usage(subject.strip())
        except IncompleteUsageData:
            return '今日用量紀錄不完整，暫時無法確認已使用與剩餘額度；請聯絡管理員。'
        except LiteLLMUsageError:
            return '目前無法讀取個人額度，請稍後再試；若持續發生請聯絡管理員。'
        return render_daily_usage(usage)
