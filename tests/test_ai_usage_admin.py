"""Exercise admin APIs against a real SQL directory and mocked LiteLLM I/O."""
from __future__ import annotations

import math
import os
import sqlite3
from dataclasses import replace
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import AsyncMock
from unittest.mock import patch
from zoneinfo import ZoneInfo

import httpx
from fastapi.testclient import TestClient
from jwt.exceptions import InvalidTokenError

from examples.ai_usage.app import app
from examples.ai_usage.deps import get_litellm_http_client
from examples.identity.providers.oidc import OidcTokenVerifier


class AdminUsageApiTests(TestCase):
    """Assert SQL scoping, current permissions, complete totals, and safe output."""

    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / 'directory.db'
        with sqlite3.connect(self.db_path) as db:
            db.executescript('''
                CREATE TABLE tenants (id TEXT PRIMARY KEY, status TEXT);
                CREATE TABLE group_info (id INTEGER PRIMARY KEY, name TEXT);
                CREATE TABLE users (
                    id INTEGER PRIMARY KEY, username TEXT, role TEXT,
                    tenant_id TEXT, group_id INTEGER, status TEXT
                );
                CREATE TABLE user_identities (
                    user_id INTEGER, provider TEXT, provider_user_id TEXT
                );
                CREATE TABLE user_profiles (
                    user_id INTEGER, family_name TEXT, middle_name TEXT,
                    given_name TEXT
                );
                INSERT INTO tenants VALUES ('a', 'active'), ('b', 'active'),
                    ('disabled', 'disabled');
                INSERT INTO group_info VALUES (10, '甲組'), (20, '乙組'),
                    (30, '其他租戶');
                INSERT INTO users VALUES
                    (1, 'changdar', 'admin', 'a', NULL, 'active'),
                    (2, 'group-admin', 'admin', 'a', 10, 'active'),
                    (3, 'alice', 'user', 'a', 10, 'active'),
                    (4, 'bob', 'user', 'a', 20, 'active'),
                    (5, 'unassigned', 'admin', 'a', NULL, 'active'),
                    (6, 'other-tenant', 'user', 'b', 30, 'active'),
                    (7, 'unlinked', 'user', 'a', 10, 'active'),
                    (8, 'suspended', 'user', 'a', 10, 'suspended'),
                    (9, 'zero-usage', 'user', 'a', 10, 'active'),
                    (10, 'shared-group-other-tenant', 'user', 'b', 10, 'active'),
                    (11, 'disabled-tenant', 'admin', 'disabled', 10, 'active');
                INSERT INTO user_profiles VALUES (3, '王', NULL, '小明');
                INSERT INTO user_identities
                    SELECT id, 'keycloak', 'subject-' || id FROM users
                    WHERE id != 7;
                INSERT INTO user_identities VALUES (7, 'other-provider', 'other-7');
            ''')
        env = {
            'PYTHON_DOTENV_DISABLED': '1',
            'AI_USAGE_ADMIN_DATABASE_URL': f'sqlite+aiosqlite:///{self.db_path}',
            'AI_USAGE_OIDC_IDENTITY_PROVIDER': 'keycloak',
            'AI_USAGE_LITELLM_BASE_URL': 'http://litellm.test',
            'LITELLM_USAGE_READER_KEY': 'test-reader-key',
            'AI_USAGE_OIDC_ISSUER_URL': 'https://issuer.test/realms/test',
            'AI_USAGE_OIDC_JWKS_URL': 'https://issuer.test/certs',
            'AI_USAGE_OIDC_AUDIENCE': 'test-api',
            'AI_USAGE_TIMEZONE': 'Asia/Taipei',
            'AI_USAGE_DAILY_BUDGET_USD': '4.00',
            'AI_USAGE_LOG_PAGE_SIZE': '1000',
            'AI_USAGE_MAX_LOG_PAGES': '100',
            'AI_USAGE_MAX_RANGE_DAYS': '366',
            'AI_USAGE_OFFICIAL_DOCUMENT_KEY_HASH': 'd' * 64,
        }
        env_patch = patch.dict(os.environ, env)
        env_patch.start()
        self.addCleanup(env_patch.stop)

        async def decode(token: str) -> dict:
            if token == 'invalid':
                raise InvalidTokenError('invalid')
            return {
                'sub': token,
                'iss': 'https://issuer.test/realms/test',
                # Tampered/stale permission claims must never control scope.
                'role': 'super_admin',
                'group_id': 20,
            }

        verifier_patch = patch.object(
            OidcTokenVerifier, 'decode_access_token', AsyncMock(side_effect=decode),
        )
        verifier_patch.start()
        self.addCleanup(verifier_patch.stop)
        self.upstream_requests: list[httpx.Request] = []
        self.upstream_failure = False
        self.force_incomplete = False
        self.today = datetime.now(ZoneInfo('Asia/Taipei')).date().isoformat()
        self.logs = [
            self.log(3, '1', 3), self.log(4, '2', 4),
            self.log(2, '0.5', 2), self.log(8, '0.25', 8),
            self.log(6, '999', 6), self.log(10, '888', 10),
            self.log(999, '777', 9), {'end_user': None},
        ]

        async def upstream(request: httpx.Request) -> httpx.Response:
            self.upstream_requests.append(request)
            if self.upstream_failure:
                return httpx.Response(500, text='secret upstream error')
            owner = request.url.params.get('end_user')
            rows = [row for row in self.logs if row.get('end_user') == owner] if owner else self.logs
            page_size = int(request.url.params['page_size'])
            page = int(request.url.params['page'])
            total_pages = 9999 if self.force_incomplete else math.ceil(len(rows) / page_size)
            return httpx.Response(
                200, json={
                    'data': rows[(page - 1) * page_size:page * page_size],
                    'total_pages': total_pages,
                },
            )

        async def get_http_client():
            async with httpx.AsyncClient(
                base_url='http://litellm.test', transport=httpx.MockTransport(upstream),
            ) as client:
                yield client

        app.dependency_overrides[get_litellm_http_client] = get_http_client
        self.addCleanup(app.dependency_overrides.clear)
        self.client = TestClient(app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def log(self, user_id: int, spend: str, hour: int) -> dict:
        """Create an accounting row including fields that must never escape."""
        return {
            'end_user': f'subject-{user_id}',
            'startTime': f'{self.today}T{hour:02}:00:00+08:00',
            'model_group': 'cloud-model',
            'prompt_tokens': 100,
            'completion_tokens': 20,
            'total_tokens': 120,
            'spend': spend,
            'request': {'messages': 'secret prompt'},
            'api_key': 'secret-key',
        }

    def get(self, path: str, actor: int = 2, **params) -> httpx.Response:
        """Call a management endpoint with a verified-subject test token."""
        return self.client.get(
            f'/v1/admin/usage/{path}', params=params,
            headers={'Authorization': f'Bearer subject-{actor}'},
        )

    def test_authentication_and_disabled_or_unlinked_accounts(self) -> None:
        for headers in ({}, {'Authorization': 'Bearer invalid'}):
            response = self.client.get('/v1/admin/usage/summary', headers=headers)
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.headers['cache-control'], 'no-store')
        for actor in (7, 8, 11):
            self.assertEqual(self.get('capabilities', actor).status_code, 403)
        self.assertEqual(self.upstream_requests, [])

    def test_capabilities_use_local_permissions(self) -> None:
        self.assertEqual(self.get('capabilities', 1).json()['scope'], 'all_groups')
        admin = self.get('capabilities').json()
        self.assertEqual(admin['scope'], 'own_group')
        self.assertEqual(admin['managed_group_id'], 10)
        self.assertFalse(admin['can_filter_groups'])
        for actor in (3, 5):
            self.assertFalse(self.get('capabilities', actor).json()['can_view_admin_usage'])
            for endpoint in ('groups', 'summary', 'users', 'records'):
                self.assertEqual(self.get(endpoint, actor).status_code, 403)
        self.assertEqual(self.upstream_requests, [])

    def test_admin_scope_is_enforced_before_upstream(self) -> None:
        self.assertEqual(self.get('summary', group_id=20).status_code, 403)
        self.assertEqual(self.get('records', user_id=4).status_code, 404)
        self.assertEqual(self.get('users', user_id=10).status_code, 404)
        self.assertEqual(self.get('summary', 1, user_id=6).status_code, 404)
        self.assertEqual(self.get('summary', 1, group_id=30).status_code, 404)
        self.assertEqual(self.upstream_requests, [])

    def test_group_choices_are_tenant_scoped(self) -> None:
        self.assertEqual(
            self.get('groups').json()['items'], [
                {'group_id': 10, 'group_name': '甲組'},
            ],
        )
        self.assertEqual(
            [group['group_id'] for group in self.get('groups', 1).json()['items']],
            [10, 20],
        )

    def test_summary_counts_full_scope_and_does_not_expose_secrets(self) -> None:
        response = self.get('summary')
        self.assertEqual(response.status_code, 200, response.text)
        summary = response.json()
        self.assertEqual(summary['selection']['group_id'], 10)
        self.assertEqual(summary['user_count'], 5)
        self.assertEqual(summary['linked_user_count'], 4)
        self.assertEqual(summary['unlinked_user_count'], 1)
        self.assertEqual(summary['totals']['spend_usd'], 1.75)
        self.assertEqual(summary['totals']['requests'], 3)
        self.assertEqual(summary['period']['date'], self.today)
        self.assertEqual(summary['period']['timezone'], 'Asia/Taipei')
        self.assertEqual(self.get('summary', 1).json()['totals']['spend_usd'], 3.75)
        for endpoint in ('summary', 'users', 'records'):
            text = self.get(endpoint).text
            for forbidden in ('secret prompt', 'secret-key', 'subject-', 'end_user', 'api_key'):
                self.assertNotIn(forbidden, text)
        self.assertEqual(response.headers['cache-control'], 'no-store')
        self.assertTrue(response.headers['x-request-id'])

    def test_users_paginate_and_distinguish_zero_from_unlinked(self) -> None:
        first = self.get('users', page_size=2).json()
        self.assertEqual(first['total'], 5)
        self.assertTrue(first['has_more'])
        self.assertEqual([item['user_id'] for item in first['items']], [2, 3])
        self.assertEqual(first['items'][1]['display_name'], '王小明')
        unlinked = self.get('users', user_id=7).json()['items'][0]
        self.assertFalse(unlinked['ai_identity_linked'])
        self.assertIsNone(unlinked['budget'])
        zero = self.get('users', user_id=9).json()['items'][0]
        self.assertTrue(zero['ai_identity_linked'])
        self.assertEqual(zero['totals']['requests'], 0)
        self.assertEqual(zero['budget']['remaining_usd'], 4)
        self.assertEqual(self.get('users', page=999).json()['items'], [])

    def test_records_paginate_and_filter_by_local_user_id(self) -> None:
        first = self.get('records', page_size=1).json()
        self.assertEqual(first['total'], 3)
        self.assertTrue(first['has_more'])
        self.assertEqual(first['items'][0]['user_id'], 8)
        second = self.get('records', page=2, page_size=1).json()
        self.assertEqual(second['items'][0]['user_id'], 3)
        selected = self.get('records', user_id=3).json()
        self.assertEqual(selected['total'], 1)
        self.assertEqual(selected['items'][0]['group_name'], '甲組')
        self.assertNotIn('end_user', self.upstream_requests[-1].url.params)
        self.assertEqual(self.get('summary').json()['totals']['requests'], 3)

    def test_upstream_pagination_is_complete_before_display_pagination(self) -> None:
        settings = app.state.settings
        app.state.settings = replace(settings, log_page_size=2)
        try:
            summary = self.get('summary').json()
            self.assertEqual(summary['totals']['spend_usd'], 1.75)
            self.assertEqual(len(self.upstream_requests), 4)
            self.assertTrue(all('end_user' not in request.url.params for request in self.upstream_requests))
            self.assertEqual(
                self.upstream_requests[0].url.params['start_date'],
                str(
                    datetime.fromisoformat(self.today).date()
                    - timedelta(days=1),
                ) + ' 16:00:00',
            )
            self.assertEqual(self.upstream_requests[-1].url.params['end_date'], self.today + ' 16:00:00')
        finally:
            app.state.settings = settings

    def test_directory_failure_is_safe_and_personal_api_still_works(self) -> None:
        with sqlite3.connect(self.db_path) as db:
            db.execute('DROP TABLE tenants')
        response = self.get('summary')
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['detail']['code'], 'ai_usage_directory_unavailable')
        self.assertNotIn('SELECT', response.text)
        self.assertEqual(self.upstream_requests, [])
        response = self.client.get(
            '/v1/me/usage/daily', headers={
                'Authorization': 'Bearer subject-3',
            },
        )
        self.assertEqual(response.status_code, 200, response.text)

    def test_current_group_and_permission_changes_take_effect(self) -> None:
        with sqlite3.connect(self.db_path) as db:
            db.execute('UPDATE users SET group_id = 20 WHERE id = 3')
        self.assertEqual(self.get('records', user_id=3).status_code, 404)
        self.assertEqual(self.get('summary').json()['totals']['spend_usd'], 0.75)
        self.assertEqual(self.get('summary', 1, group_id=20).json()['totals']['spend_usd'], 3)
        with sqlite3.connect(self.db_path) as db:
            db.execute("UPDATE users SET role = 'user' WHERE id = 2")
        self.assertEqual(self.get('summary').status_code, 403)

    def test_incomplete_and_failed_upstream_never_return_false_totals(self) -> None:
        self.force_incomplete = True
        response = self.get('summary')
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['detail']['code'], 'ai_usage_data_incomplete')
        self.force_incomplete = False
        self.upstream_failure = True
        response = self.get('users')
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['detail']['code'], 'ai_usage_upstream_unavailable')
        self.assertNotIn('secret upstream error', response.text)

    def test_parameter_validation(self) -> None:
        for params in ({'page': 0}, {'page_size': 101}, {'user_id': 0}, {'group_id': -1}):
            self.assertEqual(self.get('records', **params).status_code, 422)
        self.assertEqual(self.upstream_requests, [])

    def test_document_and_company_usage_preserve_personal_budget(self) -> None:
        document = {
            **self.log(3, '2', 10), 'end_user': None, 'api_key': 'd' * 64,
            'metadata': {
                'spend_logs_metadata': {
                    'ai_usage_source': 'official_document', 'ai_usage_subject': 'subject-3',
                },
            },
        }
        company = {
            **self.log(3, '0.1', 11),
            'metadata': {'spend_logs_metadata': {'ai_usage_source': 'company_agent'}},
        }
        self.logs.extend([document, company])
        summary = self.get('summary', user_id=3).json()
        self.assertEqual(summary['totals']['spend_usd'], 3.1)
        self.assertEqual(summary['user_totals']['spend_usd'], 1.1)
        self.assertEqual(summary['service_totals']['spend_usd'], 2)
        self.assertEqual(
            {item['source'] for item in summary['sources']},
            {'company_agent', 'official_document', 'unknown'},
        )
        user = self.get('users', user_id=3, source='official_document').json()['items'][0]
        self.assertEqual(user['totals']['spend_usd'], 2)
        self.assertEqual(user['budget']['used_usd'], 1.1)
        self.assertEqual(user['budget']['remaining_usd'], 2.9)
        record = self.get('records', user_id=3, source='official_document').json()['items'][0]
        self.assertEqual(record['billing_scope'], 'service')
        self.assertEqual(record['source'], 'official_document')
        self.assertEqual(record['user_id'], 3)
        self.assertEqual(self.get('summary', user_id=3, source='company_agent').json()['totals']['spend_usd'], 0.1)

    def test_document_ownership_requires_pinned_key_and_cannot_cross_group(self) -> None:
        for subject, key in [('subject-3', 'untrusted-key'), ('subject-4', 'd' * 64), (None, 'd' * 64)]:
            self.logs.append({
                **self.log(3, '999', 10), 'end_user': None, 'api_key': key,
                'metadata': {
                    'spend_logs_metadata': {
                        'ai_usage_source': 'official_document', 'ai_usage_subject': subject,
                    },
                },
            })
        self.assertEqual(self.get('summary').json()['totals']['spend_usd'], 1.75)
        self.assertEqual(self.get('records', source='official_document').json()['total'], 0)
        self.assertEqual(self.get('summary', source='invalid').status_code, 422)

    def test_date_range_totals_details_and_sources_include_history(self) -> None:
        yesterday = (datetime.fromisoformat(self.today).date() - timedelta(days=1)).isoformat()
        self.logs.extend([
            {**self.log(3, '2', 5), 'startTime': yesterday + 'T05:00:00+08:00'},
            {
                **self.log(3, '0.75', 6), 'startTime': yesterday + 'T06:00:00+08:00',
                'end_user': None, 'api_key': 'd' * 64,
                'metadata': {
                    'spend_logs_metadata': {
                        'ai_usage_source': 'official_document', 'ai_usage_subject': 'subject-3',
                    },
                },
            },
        ])
        query = {'start_date': yesterday, 'end_date': self.today, 'user_id': 3}
        summary = self.get('summary', **query).json()
        self.assertEqual(summary['totals']['spend_usd'], 3.75)
        self.assertEqual(summary['user_totals']['spend_usd'], 3)
        self.assertEqual(summary['service_totals']['spend_usd'], 0.75)
        self.assertEqual(summary['period']['start_date'], yesterday)
        self.assertEqual(summary['period']['end_date'], self.today)
        self.assertEqual(summary['period']['date'], yesterday)
        self.assertFalse(summary['period']['is_today'])
        page = self.get('records', page_size=1, **query).json()
        self.assertEqual(page['total'], 3)
        self.assertTrue(page['has_more'])
        self.assertEqual(page['period'], summary['period'])
        self.assertEqual(self.get('records', source='official_document', **query).json()['total'], 1)
        self.assertIsNone(self.get('users', **query).json()['items'][0]['budget'])
        historical = self.get('summary', start_date=yesterday, end_date=yesterday, user_id=3).json()
        self.assertEqual(historical['totals']['spend_usd'], 2.75)
        self.assertFalse(historical['period']['is_today'])
        self.assertIsNone(
            self.get(
                'users', start_date=yesterday, end_date=yesterday,
                user_id=3,
            ).json()['items'][0]['budget'],
        )

    def test_range_filters_midnight_and_last_microsecond_in_taipei(self) -> None:
        end = datetime.fromisoformat(self.today).date() - timedelta(days=1)
        start = end - timedelta(days=1)
        self.logs = [
            {**self.log(3, '100', 1), 'startTime': f'{start - timedelta(days=1)}T15:59:59.999999Z'},
            {**self.log(3, '1', 1), 'startTime': f'{start - timedelta(days=1)}T16:00:00Z'},
            {**self.log(3, '2', 1), 'startTime': f'{end}T15:59:59.999999Z'},
            {**self.log(3, '1000', 1), 'startTime': f'{end}T16:00:00Z'},
        ]
        response = self.get('summary', start_date=str(start), end_date=str(end), user_id=3)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['totals']['spend_usd'], 3)
        self.assertEqual(response.json()['totals']['requests'], 2)
        params = self.upstream_requests[-1].url.params
        self.assertEqual(params['start_date'], str(start - timedelta(days=1)) + ' 16:00:00')
        self.assertEqual(params['end_date'], str(end) + ' 16:00:00')
        self.assertEqual(response.json()['period']['reset_at'], self.today + 'T00:00:00+08:00')

    def test_invalid_ranges_are_rejected_without_upstream_queries(self) -> None:
        today = datetime.fromisoformat(self.today).date()
        tomorrow = str(today + timedelta(days=1))
        cases = [
            ({'start_date': self.today}, 'ai_usage_date_range_required'),
            ({'end_date': self.today}, 'ai_usage_date_range_required'),
            ({'start_date': self.today, 'end_date': str(today - timedelta(days=1))}, 'ai_usage_invalid_date_range'),
            ({'start_date': self.today, 'end_date': tomorrow}, 'ai_usage_future_date_not_allowed'),
            ({'start_date': str(today - timedelta(days=366)), 'end_date': self.today}, 'ai_usage_date_range_too_long'),
        ]
        for query, code in cases:
            response = self.get('summary', **query)
            self.assertEqual(response.status_code, 422, response.text)
            self.assertEqual(response.json()['detail']['code'], code)
        self.assertEqual(self.get('summary', start_date='not-a-date', end_date=self.today).status_code, 422)
        self.assertEqual(self.upstream_requests, [])

    def test_range_capabilities_limit_and_today_compatibility(self) -> None:
        capabilities = self.get('capabilities').json()
        self.assertEqual(capabilities['period_mode'], 'today')
        self.assertTrue(capabilities['supports_date_range'])
        self.assertEqual(capabilities['default_period'], 'today')
        self.assertEqual(capabilities['max_range_days'], 366)
        self.assertEqual(
            self.get('summary').json(),
            self.get('summary', start_date=self.today, end_date=self.today).json(),
        )
        self.assertTrue(self.get('summary').json()['period']['is_today'])
        user = self.get('users', start_date=self.today, end_date=self.today, user_id=3).json()['items'][0]
        self.assertEqual(user['budget']['used_usd'], 1)
        original = app.state.settings
        app.state.settings = replace(original, max_range_days=3)
        try:
            day = datetime.fromisoformat(self.today).date()
            self.assertEqual(
                self.get(
                    'summary', start_date=str(
                        day - timedelta(days=2),
                    ), end_date=self.today,
                ).status_code, 200,
            )
            self.assertEqual(
                self.get(
                    'summary', start_date=str(
                        day - timedelta(days=3),
                    ), end_date=self.today,
                ).status_code, 422,
            )
        finally:
            app.state.settings = original

    def test_historical_queries_keep_role_and_group_restrictions(self) -> None:
        query = {'start_date': self.today, 'end_date': self.today}
        self.assertEqual(self.get('summary', actor=3, **query).status_code, 403)
        self.assertEqual(self.get('summary', group_id=20, **query).status_code, 403)
        self.assertEqual(self.get('records', user_id=4, **query).status_code, 404)
        self.assertEqual(self.upstream_requests, [])

    def test_missing_scoped_timestamp_is_not_reported_as_zero_history(self) -> None:
        self.logs = [{'end_user': 'subject-3', 'spend': '1'}]
        response = self.get('summary')
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['detail']['code'], 'ai_usage_upstream_unavailable')

    def ledger_fixture(self) -> None:
        """A pinned gateway with attributed, legacy, and foreign-tenant usage."""
        app.state.settings = replace(app.state.settings, gateway_tenant_id='a')
        self.logs = [
            self.log(3, '1', 3), self.log(4, '2', 4),
            {**self.log(3, '20', 5), 'end_user': None, 'api_key': 'd' * 64},
            self.log(999, '0.5', 6),
            self.log(6, '999', 7), self.log(10, '888', 8),
        ]

    def test_complete_ledger_counts_legacy_cost_without_inventing_users(self) -> None:
        self.ledger_fixture()
        response = self.get('summary', 1)
        self.assertEqual(response.status_code, 200, response.text)
        summary = response.json()
        self.assertEqual(summary['totals']['spend_usd'], 23.5)
        self.assertEqual(summary['totals']['requests'], 4)
        self.assertEqual(summary['attributed_totals']['spend_usd'], 3)
        self.assertEqual(summary['unattributed_totals']['spend_usd'], 20.5)
        self.assertEqual(
            summary['coverage'], {
                'scope': 'gateway_ledger', 'includes_unattributed': True,
                'attributed_record_count': 2, 'unattributed_record_count': 2,
            },
        )
        self.assertEqual(summary['user_totals']['spend_usd'], 3.5)
        self.assertEqual(summary['service_totals']['spend_usd'], 20)
        documents = self.get('summary', 1, source='official_document').json()
        self.assertEqual(documents['totals']['spend_usd'], 20)
        self.assertEqual(documents['sources'][0]['source'], 'official_document')
        users = self.get('users', 1).json()
        self.assertEqual(sum(user['totals']['spend_usd'] for user in users['items']), 3)
        self.assertEqual(users['unattributed_totals']['spend_usd'], 20.5)
        self.assertTrue(all(user['user_id'] is not None for user in users['items']))

    def test_complete_ledger_does_not_broaden_selected_group_or_user(self) -> None:
        self.ledger_fixture()
        for actor, params in [(2, {}), (1, {'group_id': 10}), (1, {'user_id': 3})]:
            summary = self.get('summary', actor, **params).json()
            self.assertEqual(summary['totals']['spend_usd'], 1)
            self.assertFalse(summary['coverage']['includes_unattributed'])
            self.assertEqual(summary['unattributed_totals']['requests'], 0)
            self.assertEqual(self.get('unattributed-records', actor, **params).status_code, 403)
        user = self.get('users', 1, user_id=3).json()['items'][0]
        self.assertEqual(user['budget']['used_usd'], 1)
        self.assertEqual(user['budget']['remaining_usd'], 3)

    def test_unattributed_details_are_separate_safe_and_paginated(self) -> None:
        self.ledger_fixture()
        attributed = self.get('records', 1).json()
        self.assertEqual(attributed['total'], 2)
        self.assertEqual({row['user_id'] for row in attributed['items']}, {3, 4})
        first = self.get('unattributed-records', 1, page_size=1).json()
        self.assertEqual(first['total'], 2)
        self.assertTrue(first['has_more'])
        self.assertEqual(first['items'][0]['attribution_reason'], 'identity_not_linked')
        second = self.get('unattributed-records', 1, page=2, page_size=1).json()
        self.assertFalse(second['has_more'])
        self.assertEqual(second['items'][0]['attribution_reason'], 'missing_identity')
        self.assertEqual(second['items'][0]['source'], 'official_document')
        for page in (first, second):
            row = page['items'][0]
            self.assertEqual(len(row['accounting_id']), 24)
            for field in ('user_id', 'username', 'group_id', 'api_key', 'end_user', 'metadata'):
                self.assertNotIn(field, row)
            self.assertNotIn('subject-999', str(page))
            self.assertNotIn('secret prompt', str(page))
        only_documents = self.get('unattributed-records', 1, source='official_document').json()
        self.assertEqual(only_documents['total'], 1)

    def test_gateway_binding_is_required_and_does_not_cross_tenants(self) -> None:
        self.ledger_fixture()
        self.assertTrue(self.get('capabilities', 1).json()['can_view_complete_ledger'])
        self.assertFalse(self.get('capabilities', 2).json()['can_view_complete_ledger'])
        for tenant in ('', 'b'):
            app.state.settings = replace(app.state.settings, gateway_tenant_id=tenant)
            self.assertFalse(self.get('capabilities', 1).json()['can_view_complete_ledger'])
            self.assertEqual(self.get('summary', 1).json()['totals']['spend_usd'], 3)
            self.assertEqual(self.get('unattributed-records', 1).status_code, 403)

    def test_unattributed_categories_partition_totals_and_filter_before_pagination(self) -> None:
        self.ledger_fixture()
        base = {**self.log(3, '0.25', 9), 'end_user': None}
        self.logs.extend([
            {**base, 'call_type': '/health/readiness', 'status': 'failure'},
            {**base, 'status': 'failure'},
            {**base, 'metadata': {'spend_logs_metadata': {'ai_usage_classification': 'system_operation'}}},
        ])
        summary = self.get('summary', 1).json()
        categories = {item['category']: item['totals'] for item in summary['unattributed_categories']}
        self.assertEqual(
            set(categories), {
                'document_service', 'identity_not_linked',
                'system_operation', 'failed_request', 'missing_identity',
            },
        )
        self.assertEqual(
            sum(item['requests'] for item in categories.values()),
            summary['unattributed_totals']['requests'],
        )
        self.assertEqual(
            sum(item['spend_usd'] for item in categories.values()),
            summary['unattributed_totals']['spend_usd'],
        )
        for category, totals in categories.items():
            page = self.get('unattributed-records', 1, category=category, page_size=1).json()
            self.assertEqual(page['category'], category)
            self.assertEqual(page['total'], totals['requests'])
            self.assertEqual(page['unattributed_totals'], totals)
            self.assertEqual(page['items'][0]['category'], category)
            self.assertEqual(page['coverage'], summary['coverage'])
        empty = self.get('unattributed-records', 1, category='failed_request', source='official_document').json()
        self.assertEqual(empty['total'], 0)
        self.assertEqual(empty['unattributed_totals']['spend_usd'], 0)
        self.assertEqual(self.get('unattributed-records', 1, category='guessed').status_code, 422)
        self.assertEqual(self.get('unattributed-records', 2, category='document_service').status_code, 403)

    def test_missing_directory_config_does_not_break_personal_usage(self) -> None:
        factory = app.state.admin_session_factory
        app.state.admin_session_factory = None
        try:
            response = self.get('summary')
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json()['detail']['code'], 'ai_usage_admin_not_configured')
            response = self.client.get(
                '/v1/me/usage/daily', headers={
                    'Authorization': 'Bearer subject-3',
                },
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()['budget']['used_usd'], 1)
        finally:
            app.state.admin_session_factory = factory
