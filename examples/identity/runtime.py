"""One immutable configuration and explicit resources for each identity application."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import base64
import hashlib
from cryptography.fernet import Fernet

import httpx

from examples.shared.runtime.http_clients import HttpClientPool, application_http_clients

_current = ContextVar('identity_application', default=None)


class IdentityResources:
    def __init__(self, settings):
        from examples.identity.database import Database
        from examples.identity.sessions.jwt import PyJWTBearer
        from examples.identity.providers.oidc import OidcTokenVerifier
        from examples.identity.settings.bff import load_bff_options

        self.settings = settings
        secret = settings.sessions.encryption_key.strip() or settings.authjwt_secret_key
        self.session_cipher = Fernet(
            base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
        )
        self.session_ttl_seconds = settings.sessions.ttl_seconds
        self.media_ttl_seconds = settings.sessions.media_ttl_seconds
        self.redis_url = settings.resources.redis_url
        self.auto_create_schema = settings.resources.auto_create_schema
        self.database = Database(settings)
        self.oidc_verifier = OidcTokenVerifier.from_settings(settings)
        self.jwt_access = PyJWTBearer(
            settings.authjwt_secret_key,
            settings.ALGORITHM,
            oidc_verifier=self.oidc_verifier,
            oidc_identity_provider=settings.oidc_identity_provider,
            passwords_managed_externally=settings.oidc_passwords_managed_externally,
        )
        self.jwt_refresh = PyJWTBearer(
            settings.authjwt_secret_key, settings.ALGORITHM, token_use='refresh'
        )
        self.bff = load_bff_options(settings.bff)
        self.http_clients = HttpClientPool(
            {
                'bff-oidc': {
                    'timeout': httpx.Timeout(10.0, connect=5.0),
                    'follow_redirects': False,
                },
                'brevo-email': {'timeout': 10.0, 'follow_redirects': False},
                'keycloak-admin': {'timeout': 5.0, 'follow_redirects': False},
                'hcaptcha': {'timeout': 10.0, 'follow_redirects': False},
                'apple-token-exchange': {'timeout': 10.0, 'follow_redirects': False},
                'bff-upstream': {
                    'timeout': self.bff.timeout_seconds,
                    'follow_redirects': False,
                },
                'bff-upstream-sse': {
                    'timeout': httpx.Timeout(self.bff.timeout_seconds, read=None),
                    'follow_redirects': False,
                },
                'streaming-playback': {
                    'timeout': settings.resources.playback_timeout_seconds
                },
            }
        )

        from examples.identity.providers.verifiers import ProviderVerifier
        from examples.identity.providers.accounts import ProviderAccounts
        from examples.identity.providers.keycloak import KeycloakClient
        from examples.identity.providers.native_transactions import NativeTransactions
        from examples.identity.providers.native_exchange import NativeSocialService

        self.providers = ProviderVerifier(settings.social, self.http_clients)
        self.provider_accounts = ProviderAccounts(self.providers)
        self.keycloak = KeycloakClient(
            settings.keycloak,
            settings.resolved_keycloak_admin_base_url
            if settings.native.exchange_enabled
            else None,
            self.http_clients,
        )
        self.native_social = NativeSocialService(
            settings.native,
            settings.oidc,
            self.providers,
            NativeTransactions(settings.native),
            self.keycloak,
        )

        from examples.identity.accounts.mail import Mailer
        from examples.identity.accounts.recovery import AccountRecovery
        from examples.identity.sessions.local import LocalSessions
        from examples.identity.sessions.tokens import LocalTokens
        from examples.identity.cache import rate_limiter_service
        self.mailer = Mailer(settings.mail,self.http_clients)
        self.local_sessions = LocalSessions(rate_limiter_service.project_prefix)
        self.recovery = AccountRecovery(settings.mail,settings.app_public_url,self.mailer,self.local_sessions)
        self.local_tokens = LocalTokens(self.jwt_access,self.jwt_refresh,self.local_sessions,rate_limiter_service)


def get_identity():
    resources = _current.get()
    if resources is None:
        raise RuntimeError(
            'Identity application resources are not bound; use the application lifespan/request scope.'
        )
    return resources


def get_settings():
    return get_identity().settings


@contextmanager
def identity_scope(resources):
    token = _current.set(resources)
    try:
        with application_http_clients(resources.http_clients):
            yield
    finally:
        _current.reset(token)


class IdentityScopeMiddleware:
    def __init__(self, app, resources):
        self.app, self.resources = app, resources

    async def __call__(self, scope, receive, send):
        with identity_scope(self.resources):
            await self.app(scope, receive, send)
