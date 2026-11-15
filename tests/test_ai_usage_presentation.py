"""The WebUI view consumes exact personal API totals and actual model costs."""
from __future__ import annotations

import unittest
from datetime import datetime
from datetime import UTC
from decimal import Decimal

import httpx

from examples.ai_usage.config import UsageQuerySettings
from examples.ai_usage.presentation import render_daily_usage
from examples.ai_usage.services.litellm_usage import build_daily_usage_response
from examples.ai_usage.services.litellm_usage import LiteLLMUsageClient


class UsagePresentationTests(unittest.IsolatedAsyncioTestCase):
    async def test_complete_totals_survive_omitted_details_and_local_costs_remain_visible(self):
        settings = UsageQuerySettings(
            litellm_base_url='http://unused', litellm_reader_key='secret-reader',
            local_model_names=frozenset({'local'}),
        )
        async with httpx.AsyncClient() as http:
            window = LiteLLMUsageClient(settings, http).current_window(datetime(2026, 11, 11, tzinfo=UTC))
        usage = build_daily_usage_response(
            settings=settings, window=window, record_limit=0, rows=[
                {'model': 'cloud', 'spend': '0.1', 'total_tokens': 10, 'end_user': 'private-subject'},
                {'model': 'cloud', 'spend': '0.2', 'total_tokens': 20, 'end_user': 'private-subject'},
                {
                    'model': 'local', 'spend': '0.3', 'total_tokens': 40, 'end_user': 'private-subject',
                    'messages': 'must-not-display', 'api_key': 'secret-reader',
                },
            ],
        )
        self.assertTrue(usage.records_truncated)
        page = render_daily_usage(usage)
        self.assertTrue(page.startswith('## 我的今日 AI 額度'))
        self.assertIn('US$0.600000 / US$4.00', page)
        self.assertIn('剩餘額度：**US$3.400000**', page)
        self.assertIn('雲端模型：2 次、30 tokens、US$0.300000', page)
        self.assertIn('本地模型：1 次、40 tokens、US$0.300000', page)
        self.assertIn('下次重設：2026-11-12 00:00（Asia/Taipei）', page)
        self.assertIn('### 模型明細', page)
        self.assertIn('不會顯示聊天內容、OIDC subject 或其他使用者資料', page)
        for private in ('private-subject', 'must-not-display', 'secret-reader', '10,000', '資料不完整'):
            self.assertNotIn(private, page)

    async def test_empty_day_still_shows_budget_and_reset(self):
        settings = UsageQuerySettings(
            litellm_base_url='http://unused', litellm_reader_key='reader',
            daily_budget_usd=Decimal(1),
        )
        async with httpx.AsyncClient() as http:
            window = LiteLLMUsageClient(settings, http).current_window(datetime(2026, 11, 11, tzinfo=UTC))
        usage = build_daily_usage_response(settings=settings, window=window, record_limit=0, rows=[])
        page = render_daily_usage(usage)
        self.assertIn('剩餘額度：**US$1.000000**', page)
        self.assertIn('今天尚無可歸屬於你的模型請求', page)
