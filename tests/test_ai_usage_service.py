"""Focused unit tests for the standalone AI usage service layer."""
from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from unittest import IsolatedAsyncioTestCase, TestCase
from zoneinfo import ZoneInfo

import httpx

from examples.ai_usage.config import (
    AiUsageSettings,
    ConfigurationError,
    UsageQuerySettings,
)
from examples.ai_usage.services.litellm_usage import (
    IncompleteUsageData,
    LiteLLMUsageClient,
    build_daily_usage_response,
)


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

    def test_range_window_handles_leap_day_and_configured_local_today(self) -> None:
        client = LiteLLMUsageClient(_settings(), None)  # type: ignore[arg-type]
        window = client.range_window(
            date(2024, 2, 28), date(2024, 3, 1),
            now=datetime.fromisoformat('2024-03-01T17:00:00+00:00'),
        )
        self.assertEqual(window.started_at.isoformat(), '2024-02-28T00:00:00+08:00')
        self.assertEqual(window.reset_at.isoformat(), '2024-03-02T00:00:00+08:00')
        self.assertFalse(window.is_today)
        self.assertEqual(window.now.date(), date(2024, 3, 2))

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

    async def test_capped_ranges_split_without_duplicate_boundary_records(self) -> None:
        settings = _settings()
        calls = []
        timestamps = (
            '2024-03-01T00:00:00+08:00', '2024-03-01T23:59:59.999999+08:00',
            '2024-03-02T00:00:00+08:00', '2024-03-02T23:59:59.999999+08:00',
        )
        rows = [{'end_user': 'owner', 'startTime': value} for value in timestamps]

        async def handler(request):
            calls.append(request)
            start = datetime.strptime(
                request.url.params['start_date'],
                '%Y-%m-%d %H:%M:%S',
            ).replace(tzinfo=UTC)
            end = datetime.strptime(request.url.params['end_date'], '%Y-%m-%d %H:%M:%S').replace(tzinfo=UTC)
            selected = [row for row in rows if start <= datetime.fromisoformat(row['startTime']) <= end]
            return httpx.Response(200, json={'data': selected[:3], 'total_pages': 1, 'total_is_capped': len(selected) > 3})

        async with httpx.AsyncClient(base_url=settings.litellm_base_url, transport=httpx.MockTransport(handler)) as http:
            client = LiteLLMUsageClient(settings, http)
            window = client.range_window(
                date(2024, 3, 1),
                date(2024, 3, 2),
                now=datetime(2024, 3, 3, tzinfo=settings.timezone),
            )
            returned_window, result = await client.fetch_scoped_window(frozenset({'owner'}), window)
            self.assertEqual(returned_window, window)
            self.assertEqual(result, rows)
            self.assertEqual(len(calls), 3)
            calls.clear()
            client = LiteLLMUsageClient(replace(settings, max_log_pages=2), http)
            with self.assertRaisesRegex(IncompleteUsageData, 'usage_log_page_cap_reached'):
                await client.fetch_scoped_window(frozenset({'owner'}), window)
            self.assertEqual(len(calls), 2)

    async def test_rejects_upstream_capped_totals(self) -> None:
        settings = _settings()

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json={
                    'data': [], 'total_pages': 1, 'total_is_capped': True,
                },
            )

        async with httpx.AsyncClient(
            base_url=settings.litellm_base_url,
            transport=httpx.MockTransport(handler),
        ) as http_client:
            client = LiteLLMUsageClient(settings, http_client)
            with self.assertRaises(IncompleteUsageData):
                await client.fetch_scoped_day(frozenset({'alice', 'bob'}))

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
                            'startTime': datetime.now(UTC).isoformat(),
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


class UsageQuerySettingsTests(TestCase):
    def test_snapshot_has_no_oidc_requirement_and_validates_query_policy(self):
        settings = UsageQuerySettings(litellm_base_url='http://127.0.0.1:4000/', litellm_reader_key='reader')
        self.assertEqual(settings.litellm_base_url, 'http://127.0.0.1:4000')
        self.assertEqual(settings.timezone.key, 'Asia/Taipei')
        self.assertNotIn('reader', repr(settings))
        self.assertFalse(hasattr(settings, 'oidc_issuer_url'))
        for policy in (
            {'daily_budget_usd': Decimal('NaN')}, {'daily_budget_usd': Decimal('Infinity')},
            {'daily_budget_usd': Decimal(0)}, {'daily_budget_usd': Decimal(100001)},
            {'max_log_pages': 0}, {'log_page_size': 1001}, {'max_range_days': 367},
            {'litellm_base_url': 'not-a-url'}, {'litellm_base_url': 'http://[bad'},
            {'timezone_name': 'Not/AZone'},
        ):
            with self.subTest(policy=policy), self.assertRaises(ConfigurationError):
                replace(settings, **policy)


class DailyUsageQueryTests(IsolatedAsyncioTestCase):
    async def test_personal_day_keeps_last_fractional_second_and_excludes_next_midnight(self):
        settings = UsageQuerySettings(litellm_base_url='http://usage.test', litellm_reader_key='reader')
        timestamps = (
            '2026-11-10T23:59:59.999999+08:00',
            '2026-11-11T00:00:00+08:00',
            '2026-11-11T23:59:59.999999+08:00',
            '2026-11-12T00:00:00+08:00',
        )
        seen = []

        def handler(request):
            seen.append(request)
            start = datetime.strptime(request.url.params['start_date'], '%Y-%m-%d %H:%M:%S').replace(tzinfo=UTC)
            end = datetime.strptime(request.url.params['end_date'], '%Y-%m-%d %H:%M:%S').replace(tzinfo=UTC)
            # Model the upstream's inclusive bounds, including midnight.
            rows = [{'startTime': stamp, 'end_user': 'owner', 'spend': '0.1'} for stamp in timestamps
                    if start <= datetime.fromisoformat(stamp) <= end]
            return httpx.Response(200, json={'data': rows, 'total_pages': 1})

        async with httpx.AsyncClient(base_url=settings.litellm_base_url,
                                     transport=httpx.MockTransport(handler)) as http:
            usage = await LiteLLMUsageClient(settings, http).daily_usage(
                'owner', record_limit=10, now=datetime(2026, 11, 11, 12, tzinfo=settings.timezone),
            )
        self.assertEqual(usage.totals.requests, 2)
        self.assertEqual(usage.budget.used_usd, 0.2)
        self.assertEqual(seen[0].url.params['end_date'], '2026-11-11 16:00:00')
        self.assertEqual(usage.records[0].started_at.microsecond, 999999)

    async def test_daily_operation_filters_owner_and_keeps_complete_totals_without_records(self):
        settings = UsageQuerySettings(litellm_base_url='http://127.0.0.1:14002', litellm_reader_key='private-reader',
                                      daily_budget_usd=Decimal(1))
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, json={'data': [
                {'end_user': 'owner', 'model': 'cloud', 'spend': '0.1', 'total_tokens': 7,
                 'startTime': datetime.now(UTC).isoformat()},
                {'end_user': 'another-user', 'model': 'must-not-appear', 'spend': '100'},
            ], 'total_pages': 1})

        async with httpx.AsyncClient(base_url=settings.litellm_base_url,
                                     transport=httpx.MockTransport(handler)) as http:
            usage = await LiteLLMUsageClient(settings, http).daily_usage('owner')
        self.assertEqual(usage.totals.requests, 1)
        self.assertEqual(usage.budget.used_usd, 0.1)
        self.assertEqual(usage.budget.remaining_usd, 0.9)
        self.assertEqual(usage.records, [])
        self.assertTrue(usage.records_truncated)  # Only detail was omitted, not totals.
        self.assertEqual([model.model for model in usage.models], ['cloud'])
        self.assertEqual(seen[0].url.host, '127.0.0.1')
        self.assertEqual(seen[0].url.port, 14002)
        self.assertEqual(seen[0].url.params['end_user'], 'owner')
        self.assertEqual(seen[0].headers['authorization'], 'Bearer private-reader')

    async def test_daily_operation_rejects_partial_query_instead_of_returning_a_budget(self):
        settings = UsageQuerySettings(litellm_base_url='http://unused', litellm_reader_key='reader', max_log_pages=1)
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json={'data': [], 'total_pages': 2}))
        async with httpx.AsyncClient(base_url=settings.litellm_base_url, transport=transport) as http:
            with self.assertRaises(IncompleteUsageData):
                await LiteLLMUsageClient(settings, http).daily_usage('owner')

    async def test_nonfinite_upstream_values_cannot_escape_into_totals(self):
        settings = UsageQuerySettings(litellm_base_url='http://unused', litellm_reader_key='reader')
        now = datetime(2026, 11, 11, tzinfo=UTC)
        async with httpx.AsyncClient() as http:
            window = LiteLLMUsageClient(settings, http).current_window(now)
        usage = build_daily_usage_response(settings=settings, window=window, record_limit=0, rows=[
            {'spend': 'NaN', 'total_tokens': float('inf')},
            {'spend': 'Infinity', 'prompt_tokens': float('nan')},
            {'spend': '-Infinity', 'completion_tokens': float('-inf')},
            {'spend': '0.25', 'total_tokens': 5},
        ])
        self.assertEqual(usage.totals.spend_usd, 0.25)
        self.assertEqual(usage.totals.total_tokens, 5)
        self.assertEqual(usage.budget.remaining_usd, 3.75)
