"""One immutable configuration and explicit resources for each identity application."""
from __future__ import annotations

import base64
import hashlib
from contextlib import contextmanager
from contextvars import ContextVar

import httpx
from cryptography.fernet import Fernet

from examples.identity.accounts.mail import Mailer
from examples.identity.providers.keycloak import KeycloakClient
from examples.identity.providers.native_transactions import NativeTransactions
from examples.identity.providers.verifiers import ProviderVerifier
from examples.identity.sessions.local import LocalSessions
from examples.shared.runtime.http_clients import application_http_clients
from examples.shared.runtime.http_clients import HttpClientPool
from examples.shared.runtime.modules import ModuleReference

_identity_recovery = ModuleReference('examples.identity.accounts.recovery')
_identity_cache = ModuleReference('examples.identity.cache')
_identity_database = ModuleReference('examples.identity.database')
_identity_accounts = ModuleReference('examples.identity.providers.accounts')
_identity_native_exchange = ModuleReference('examples.identity.providers.native_exchange')
_identity_oidc = ModuleReference('examples.identity.providers.oidc')
_identity_jwt = ModuleReference('examples.identity.sessions.jwt')
_identity_tokens = ModuleReference('examples.identity.sessions.tokens')
_identity_bff = ModuleReference('examples.identity.settings.bff')

_current = ContextVar('identity_application', default=None)


class IdentityResources:
    def __init__(self, settings):
        Database = _identity_database.attribute('Database')
        PyJWTBearer = _identity_jwt.attribute('PyJWTBearer')
        OidcTokenVerifier = _identity_oidc.attribute('OidcTokenVerifier')
        load_bff_options = _identity_bff.attribute('load_bff_options')

        self.settings = settings
        secret = settings.sessions.encryption_key.strip() or settings.authjwt_secret_key
        self.session_cipher = Fernet(
            base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest()),
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
            settings.authjwt_secret_key, settings.ALGORITHM, token_use='refresh',
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
                    'timeout': settings.resources.playback_timeout_seconds,
                },
            },
        )
        ProviderAccounts = _identity_accounts.attribute('ProviderAccounts')
        NativeSocialService = _identity_native_exchange.attribute('NativeSocialService')

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
        AccountRecovery = _identity_recovery.attribute('AccountRecovery')
        LocalTokens = _identity_tokens.attribute('LocalTokens')
        rate_limiter_service = _identity_cache.attribute('rate_limiter_service')
        self.mailer = Mailer(settings.mail, self.http_clients)
        self.local_sessions = LocalSessions(rate_limiter_service.project_prefix)
        self.recovery = AccountRecovery(settings.mail, settings.app_public_url, self.mailer, self.local_sessions)
        self.local_tokens = LocalTokens(self.jwt_access, self.jwt_refresh, self.local_sessions, rate_limiter_service)


def get_identity():
    resources = _current.get()
    if resources is None:
        raise RuntimeError(
            'Identity application resources are not bound; use the application lifespan/request scope.',
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
