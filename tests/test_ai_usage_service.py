"""Focused unit tests for the standalone AI usage service layer."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from unittest import IsolatedAsyncioTestCase
from unittest import TestCase
from zoneinfo import ZoneInfo

import httpx

from examples.ai_usage.config import AiUsageSettings
from examples.ai_usage.services.litellm_usage import build_daily_usage_response
from examples.ai_usage.services.litellm_usage import LiteLLMUsageClient


def _settings() -> AiUsageSettings:
    """Build non-secret deterministic settings for service tests."""
    timezone = ZoneInfo('Asia/Taipei')
    return AiUsageSettings(
        litellm_base_url='http://127.0.0.1:4000',
        litellm_reader_key='test-reader-key',
        daily_budget_usd=Decimal('4.00'),
        timezone_name='Asia/Taipei',
        timezone=timezone,
        oidc_issuer_url='https://issuer.example/realms/visionnaire',
        oidc_jwks_url='https://issuer.example/certs',
        oidc_audiences=('visionnaire-api',),
        oidc_algorithms=('RS256',),
        oidc_jwks_cache_seconds=300,
        oidc_jwks_timeout_seconds=5,
        request_timeout_seconds=15,
        log_page_size=1000,
        max_log_pages=100,
        max_record_limit=100,
        local_model_names=frozenset({'gpt-oss-20b'}),
    )


class DailyUsageAggregationTests(TestCase):
    """Verify that display data never contains raw LiteLLM request fields."""

    def test_aggregates_cloud_and_local_records(self) -> None:
        settings = _settings()
        now = datetime(2026, 8, 29, 12, 0, tzinfo=settings.timezone)
        # ``current_window`` does not perform I/O, so no HTTP client is used
        # by this synchronous aggregation test.
        client = LiteLLMUsageClient(settings, None)  # type: ignore[arg-type]
        window = client.current_window(now)
        response = build_daily_usage_response(
            settings=settings,
            window=window,
            rows=[
                {
                    'startTime': '2026-08-29T02:00:00Z',
                    'end_user': 'oidc-stable-subject',
                    'model_group': 'Kimi-K2.6',
                    'custom_llm_provider': 'azure',
                    'prompt_tokens': 100,
                    'completion_tokens': 20,
                    'cache_read_input_tokens': 5,
                    'total_tokens': 120,
                    'spend': '0.0012345',
                    'request': {'messages': 'must never be returned'},
                },
                {
                    'startTime': '2026-08-29T03:00:00Z',
                    'end_user': 'oidc-stable-subject',
                    'model_group': 'gpt-oss-20b',
                    'custom_llm_provider': 'ollama',
                    'prompt_tokens': 30,
                    'completion_tokens': 10,
                    'total_tokens': 40,
                    'spend': 0,
                },
            ],
            record_limit=50,
        )

        self.assertEqual(response.totals.requests, 2)
        self.assertEqual(response.totals.cloud_requests, 1)
        self.assertEqual(response.totals.local_requests, 1)
        self.assertEqual(response.totals.total_tokens, 160)
        self.assertEqual(response.budget.used_usd, 0.001234)
        self.assertEqual(response.budget.remaining_usd, 3.998766)
        self.assertEqual(response.models[0].model, 'Kimi-K2.6')
        self.assertFalse(response.models[0].is_local)
        self.assertNotIn('must never be returned', response.model_dump_json())
        self.assertNotIn('"request":', response.model_dump_json())


class LiteLLMUsageClientTests(IsolatedAsyncioTestCase):
    """Verify the only upstream query uses the authenticated OIDC subject."""

    async def test_fetches_only_the_requested_end_user(self) -> None:
        settings = _settings()
        observed: dict[str, str] = {}

        async def handler(request: httpx.Request) -> httpx.Response:
            observed['path'] = request.url.path
            observed['end_user'] = request.url.params['end_user']
            observed['authorization'] = request.headers['authorization']
            return httpx.Response(
                200,
                json={
                    'data': [
                        {
                            'end_user': 'oidc-stable-subject',
                            'model_group': 'Kimi-K2.6',
                        },
                        {
                            'end_user': 'a-different-user',
                            'model_group': 'must-not-leak',
                        },
                    ],
                    'total_pages': 1,
                },
                request=request,
            )

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(
            base_url=settings.litellm_base_url,
            transport=transport,
        ) as http_client:
            client = LiteLLMUsageClient(settings, http_client)
            _, rows = await client.fetch_current_day('oidc-stable-subject')

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['model_group'], 'Kimi-K2.6')
        self.assertEqual(observed['path'], '/spend/logs/v2')
        self.assertEqual(observed['end_user'], 'oidc-stable-subject')
        self.assertEqual(observed['authorization'], 'Bearer test-reader-key')
