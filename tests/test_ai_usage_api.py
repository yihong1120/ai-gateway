"""Tests for the private AI usage API boundary."""
from __future__ import annotations

import os
from unittest import TestCase
from unittest.mock import patch

from fastapi.testclient import TestClient


_TEST_ENV = {
    'AI_USAGE_LITELLM_BASE_URL': 'http://127.0.0.1:4000',
    'LITELLM_USAGE_READER_KEY': 'test-reader-key',
    'AI_USAGE_OIDC_ISSUER_URL': 'https://issuer.example/realms/visionnaire',
    'AI_USAGE_OIDC_JWKS_URL': (
        'https://issuer.example/realms/visionnaire/'
        'protocol/openid-connect/certs'
    ),
    'AI_USAGE_OIDC_AUDIENCE': 'visionnaire-api',
}


class AiUsageApiTests(TestCase):
    """Ensure private routes reject unauthenticated access and are not cached."""

    def test_daily_usage_requires_a_bearer_token(self) -> None:
        with patch.dict(os.environ, _TEST_ENV, clear=False):
            from examples.ai_usage.app import app

            with TestClient(app) as client:
                response = client.get(
                    '/v1/me/usage/daily?end_user=another-person',
                )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(
            response.json()['detail']
            ['code'], 'missing_access_token',
        )
        self.assertEqual(response.headers['cache-control'], 'no-store')
        self.assertTrue(response.headers['x-request-id'])
