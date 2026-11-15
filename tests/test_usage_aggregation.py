"""Exact totals and bounded response construction across summary/detail views."""
from __future__ import annotations

import unittest
from datetime import datetime
from datetime import timedelta
from datetime import UTC
from decimal import Decimal
from unittest.mock import patch
from zoneinfo import ZoneInfo

from examples.ai_usage.config import AiUsageSettings
from examples.ai_usage.schemas.admin_usage import UsageSelection
from examples.ai_usage.schemas.admin_usage import UsageUser
from examples.ai_usage.services.admin_usage import build_admin_usage_data
from examples.ai_usage.services.directory import DirectoryUser
from examples.ai_usage.services.litellm_usage import build_daily_usage_response
from examples.ai_usage.services.litellm_usage import DailyLogWindow
from examples.ai_usage.services.litellm_usage import UsageFact


def dataset(count=1000):
    now = datetime(2026, 11, 8, tzinfo=UTC)
    settings = AiUsageSettings(
        litellm_base_url='http://unused', litellm_reader_key='reader',
        daily_budget_usd=Decimal(4), timezone_name='UTC', timezone=ZoneInfo('UTC'),
        oidc_issuer_url='https://issuer', oidc_jwks_url='https://issuer/keys',
        oidc_audiences=('api',), oidc_algorithms=('RS256',), oidc_jwks_cache_seconds=300,
        oidc_jwks_timeout_seconds=5, request_timeout_seconds=10, log_page_size=1000,
        max_log_pages=100, max_record_limit=100, local_model_names=frozenset({'local'}),
    )

    users = [
        DirectoryUser(
            UsageUser(
                user_id=1,
                username='user',
                display_name='User',
                group_id=1,
                group_name='Group',
            ),
            'active',
            'subject',
        ),
    ]
    rows = [
        {
            'end_user': 'subject',
            'request_id': str(i),
            'model': 'model',
            'provider': 'provider',
            'startTime': (now + timedelta(seconds=i)).isoformat(),
            'prompt_tokens': 3,
            'completion_tokens': 2,
            'total_tokens': 5,
            'spend': '0.000001',
        }
        for i in range(count)
    ]
    return dict(
        settings=settings,
        window=DailyLogWindow(now, now, now + timedelta(days=1)),
        selection=UsageSelection(scope='all_groups', group_id=None, user_id=None),
        users=users,
        rows=rows,
    )


class UsageAggregationTests(unittest.TestCase):
    def test_summary_constructs_no_detail_models_and_page_constructs_only_its_items(
        self,
    ):
        values = dataset()
        with patch.object(
            UsageFact,
            'record',
            side_effect=AssertionError('summary built a detail model'),
        ):
            data = build_admin_usage_data(**values)
        self.assertEqual(data.summary.totals.requests, 1000)
        self.assertEqual(data.summary.totals.spend_usd, 0.001)
        self.assertEqual(data.users[0].budget.used_usd, 0.001)
        self.assertEqual(data.record_count, 1000)
        original = UsageFact.record
        calls = []

        def record(fact):
            calls.append(fact)
            return original(fact)

        with patch.object(UsageFact, 'record', record):
            page = data.records_page(10, 5)
        self.assertEqual(len(calls), 5)
        self.assertEqual(len(page), 5)
        self.assertGreater(page[0].started_at, page[-1].started_at)

    def test_zero_detail_daily_view_keeps_full_totals_and_count(self):
        values = dataset()
        with patch.object(
            UsageFact, 'record', side_effect=AssertionError('unrequested detail'),
        ):
            response = build_daily_usage_response(
                settings=values['settings'],
                window=values['window'],
                rows=values['rows'],
                record_limit=0,
            )
        self.assertEqual(response.record_count, 1000)
        self.assertTrue(response.records_truncated)
        self.assertEqual(response.totals.requests, 1000)
        self.assertEqual(response.records, [])

    def test_personal_and_admin_views_agree_across_cloud_and_local_models(self):
        values = dataset()
        for index, row in enumerate(values['rows']):
            row.update(model='local' if index % 2 else 'cloud')
        personal = build_daily_usage_response(
            settings=values['settings'], window=values['window'],
            rows=values['rows'], record_limit=5,
        )
        admin = build_admin_usage_data(**values)
        self.assertEqual(personal.totals, admin.summary.totals)
        self.assertEqual(personal.totals, admin.users[0].totals)
        self.assertEqual(personal.totals.cloud_requests, 500)
        self.assertEqual(personal.totals.local_requests, 500)
        self.assertEqual(personal.totals.total_tokens, 5000)
        self.assertEqual(sum(model.requests for model in personal.models), 1000)
        self.assertEqual(
            sum(Decimal(str(model.spend_usd)) for model in personal.models),
            Decimal('0.001'),
        )
        self.assertEqual(personal.budget.used_usd, admin.users[0].budget.used_usd)
