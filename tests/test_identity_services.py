"""Identity service boundaries preserve nonce, one-use proof and link contracts."""
from __future__ import annotations

import base64
import json
import os
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import Mock
from unittest.mock import patch

import httpx
from fastapi import HTTPException
from fastapi import Request
from pydantic import ValidationError

from examples.identity.config import Settings
from examples.identity.runtime import IdentityResources
from examples.identity.schemas.auth import ProviderClaims


def configuration(**values):
    defaults = dict(
        authjwt_secret_key='test-secret',
        oidc_enabled=True,
        oidc_issuer_url='https://identity.test/realms/company',
        oidc_jwks_url='https://identity.test/keys',
        oidc_audience='api',
        native_social_exchange_enabled=True,
        native_social_exchange_shared_secret='s' * 32,
        keycloak_user_linker_client_secret='linker-secret',
        native_social_allowed_clients_json='{"mobile":["company:/callback"]}',
    )
    return Settings(**{**defaults, **values})


class IdentityServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(
            patch.dict(os.environ, {'PYTHON_DOTENV_DISABLED': '1'}, clear=True),
        )
        self.resources = IdentityResources(configuration())
        self.addAsyncCleanup(self.resources.http_clients.close)

    async def test_provider_nonce_and_verified_email_are_still_required(self):
        verifier = self.resources.providers
        claims = ProviderClaims(
            sub='google-subject',
            email='a@example.test',
            email_verified=True,
            nonce='bound',
        )
        with patch.object(verifier, '_verify_jwt_with_jwks', return_value=claims):
            self.assertEqual(
                await verifier.verify_google_id_token('token', expected_nonce='bound'),
                claims,
            )
            with self.assertRaises(HTTPException) as mismatch:
                await verifier.verify_google_id_token(
                    'token', expected_nonce='different',
                )
            self.assertEqual(mismatch.exception.status_code, 401)
        with patch.object(
            verifier,
            '_verify_jwt_with_jwks',
            return_value=claims.model_copy(update={'email_verified': False}),
        ):
            with self.assertRaises(HTTPException):
                await verifier.verify_google_id_token('token')
            self.assertEqual(
                (
                    await verifier.verify_google_id_token(
                        'token', require_verified_email=False,
                    )
                ).sub,
                claims.sub,
            )

    async def test_apple_exchange_uses_its_owned_pool_without_a_request_scope(self):
        client = await self.resources.http_clients.get('apple-token-exchange')
        with (
            patch.object(
                client,
                'post',
                new=AsyncMock(
                    return_value=httpx.Response(200, json={'id_token': 'verified'}),
                ),
            ) as post,
            patch.object(
                self.resources.providers,
                '_build_apple_client_secret',
                return_value='assertion',
            ),
        ):
            result = (
                await self.resources.providers._exchange_apple_authorization_code_once(
                    'one-use-code', 'client',
                )
            )
        self.assertEqual(result.id_token, 'verified')
        self.assertEqual(post.await_args.kwargs['data']['client_secret'], 'assertion')
        self.assertFalse(client.is_closed)

    async def test_keycloak_link_reuses_owned_client_and_never_replays_a_conflict_post(
        self,
    ):
        client = await self.resources.http_clients.get('keycloak-admin')
        responses = [
            httpx.Response(200, json=[]),
            httpx.Response(409),
            httpx.Response(
                200, json=[{'identityProvider': 'google', 'userId': 'provider-sub'}],
            ),
        ]
        with (
            patch.object(
                client,
                'post',
                new=AsyncMock(
                    return_value=httpx.Response(200, json={'access_token': 'admin'}),
                ),
            ),
            patch.object(
                client, 'request', new=AsyncMock(side_effect=responses),
            ) as request,
        ):
            result = await self.resources.keycloak.link_federated_identity(
                keycloak_subject='server/subject',
                provider='google',
                provider_subject='provider-sub',
            )
        self.assertEqual(result, 'already_linked')
        self.assertEqual(
            [call.args[0] for call in request.await_args_list], ['GET', 'POST', 'GET'],
        )
        self.assertIn('server%2Fsubject', request.await_args_list[0].args[1])
        self.assertFalse(client.is_closed)

    async def test_native_redemption_checks_hmac_pkce_and_consumes_only_once(self):
        service = self.resources.native_social
        transactions = service.transactions
        identifier = transactions.identifier()
        payload = {
            'transaction_id': identifier,
            'client_id': 'mobile',
            'redirect_uri': 'company:/callback',
            'code_challenge': 'bound-pkce',
        }
        encoded = base64.urlsafe_b64encode(b'provider-subject').rstrip(b'=').decode()
        stored = json.dumps(
            {
                **payload,
                'kind': 'login-redeem',
                'provider': 'google',
                'provider_subject_b64': encoded,
            },
        ).encode()
        redis = SimpleNamespace(getdel=AsyncMock(side_effect=[stored, None]))
        body = json.dumps(payload).encode()
        stamp = str(int(time.time()))

        def request(signature):
            async def receive():
                return {'type': 'http.request', 'body': body, 'more_body': False}

            return Request(
                {
                    'type': 'http',
                    'method': 'POST',
                    'path': '/',
                    'client': ('127.0.0.1', 1),
                    'headers': [
                        (b'x-visionnaire-timestamp', stamp.encode()),
                        (b'x-visionnaire-signature', signature.encode()),
                    ],
                },
                receive,
            )

        with self.assertRaises(HTTPException):
            await service.redeem_keycloak_native_social_exchange(
                request('wrong'), redis,
            )
        redis.getdel.assert_not_awaited()
        signature = service._hmac_signature(stamp, body)
        result = await service.redeem_keycloak_native_social_exchange(
            request(signature), redis,
        )
        self.assertEqual(
            result, {'provider': 'google', 'provider_subject_b64': encoded},
        )
        with self.assertRaises(HTTPException):
            await service.redeem_keycloak_native_social_exchange(
                request(signature), redis,
            )
        self.assertEqual(redis.getdel.await_count, 2)
        self.assertNotIn(identifier, redis.getdel.await_args.args[0])

    async def test_jwks_cache_and_services_are_isolated_between_apps(self):
        other = IdentityResources(configuration())
        self.addAsyncCleanup(other.http_clients.close)
        with patch(
            'examples.identity.providers.verifiers.jwt.PyJWKClient',
            side_effect=[Mock(), Mock()],
        ) as factory:
            first = self.resources.providers._jwks_client('https://provider.test/keys')
            self.assertIs(
                self.resources.providers._jwks_client('https://provider.test/keys'),
                first,
            )
            self.assertIsNot(
                other.providers._jwks_client('https://provider.test/keys'), first,
            )
        self.assertEqual(factory.call_count, 2)
        self.assertIs(self.resources.native_social.verifier, self.resources.providers)
        self.assertIs(self.resources.native_social.keycloak, self.resources.keycloak)


class GroupedConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(
            patch.dict(os.environ, {'PYTHON_DOTENV_DISABLED': '1'}, clear=True),
        )

    def test_environment_legacy_keywords_and_grouped_keywords_agree(self):
        with patch.dict(
            os.environ,
            {'JWT_SECRET_KEY': 'one', 'OIDC_ENABLED': '0', 'DB_POOL_SIZE': '3'},
        ):
            settings = Settings(
                oidc_audience='overridden', web={'app_public_url': 'https://app.test'},
            )
        self.assertEqual(settings.oidc.audience, 'overridden')
        self.assertEqual(settings.oidc_audience, settings.oidc.audience)
        self.assertEqual(settings.authjwt_secret_key, 'one')
        self.assertEqual(settings.app_public_url, 'https://app.test')
        with self.assertRaises(ValidationError):
            settings.oidc.audience = 'changed'
        with self.assertRaises(ValueError):
            Settings(
                authjwt_secret_key='one', oidc={'audience': 'a'}, oidc_audience='b',
            )

    def test_domain_and_integration_validation_run_at_construction(self):
        with self.assertRaisesRegex(RuntimeError, 'JWT_SECRET_KEY'):
            Settings()
        with self.assertRaisesRegex(RuntimeError, 'required settings'):
            Settings(authjwt_secret_key='one', oidc_enabled=True)
        with self.assertRaisesRegex(RuntimeError, 'OIDC_ENABLED'):
            Settings(authjwt_secret_key='one', oidc_passwords_managed_externally=True)
        with self.assertRaisesRegex(RuntimeError, 'at least 32'):
            configuration(native_social_exchange_shared_secret='short')
