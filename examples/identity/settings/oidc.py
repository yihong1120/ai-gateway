from __future__ import annotations

import os

from pydantic import Field
from pydantic import model_validator
from pydantic_settings import BaseSettings

from .common import _env_bool


class OidcSettings(BaseSettings):
    model_config = {'frozen': True, 'populate_by_name': True}

    enabled: bool = Field(
        validation_alias='OIDC_ENABLED',
        default_factory=lambda: _env_bool('OIDC_ENABLED', False),
    )

    issuer_url: str = Field(
        validation_alias='OIDC_ISSUER_URL',
        default_factory=lambda: os.getenv('OIDC_ISSUER_URL', '').strip().rstrip('/'),
    )

    jwks_url: str = Field(
        validation_alias='OIDC_JWKS_URL',
        default_factory=lambda: os.getenv('OIDC_JWKS_URL', '').strip(),
    )

    audience: str = Field(
        validation_alias='OIDC_AUDIENCE',
        default_factory=lambda: os.getenv('OIDC_AUDIENCE', '').strip(),
    )

    identity_provider: str = Field(
        validation_alias='OIDC_IDENTITY_PROVIDER',
        default_factory=lambda: os.getenv(
            'OIDC_IDENTITY_PROVIDER',
            'keycloak',
        ).strip(),
    )

    jwt_algorithms: str = Field(
        validation_alias='OIDC_JWT_ALGORITHMS',
        default_factory=lambda: os.getenv(
            'OIDC_JWT_ALGORITHMS',
            'RS256',
        ).strip(),
    )

    jwks_cache_seconds: int = Field(
        validation_alias='OIDC_JWKS_CACHE_SECONDS',
        default_factory=lambda: max(
            60,
            int(os.getenv('OIDC_JWKS_CACHE_SECONDS', '300')),
        ),
    )

    jwks_timeout_seconds: float = Field(
        validation_alias='OIDC_JWKS_TIMEOUT_SECONDS',
        default_factory=lambda: max(
            1.0,
            float(os.getenv('OIDC_JWKS_TIMEOUT_SECONDS', '5')),
        ),
    )

    web_client_id: str = Field(
        validation_alias='OIDC_WEB_CLIENT_ID',
        default_factory=lambda: os.getenv('OIDC_WEB_CLIENT_ID', '').strip(),
    )

    web_client_secret: str = Field(
        validation_alias='OIDC_WEB_CLIENT_SECRET',
        default_factory=lambda: os.getenv(
            'OIDC_WEB_CLIENT_SECRET',
            '',
        ).strip(),
    )

    web_authorization_endpoint: str = Field(
        validation_alias='OIDC_WEB_AUTHORIZATION_ENDPOINT',
        default_factory=lambda: os.getenv(
            'OIDC_WEB_AUTHORIZATION_ENDPOINT',
            '',
        ).strip(),
    )

    web_token_endpoint: str = Field(
        validation_alias='OIDC_WEB_TOKEN_ENDPOINT',
        default_factory=lambda: os.getenv(
            'OIDC_WEB_TOKEN_ENDPOINT',
            '',
        ).strip(),
    )

    web_redirect_uri: str = Field(
        validation_alias='OIDC_WEB_REDIRECT_URI',
        default_factory=lambda: os.getenv(
            'OIDC_WEB_REDIRECT_URI',
            '',
        ).strip(),
    )

    web_end_session_endpoint: str = Field(
        validation_alias='OIDC_WEB_END_SESSION_ENDPOINT',
        default_factory=lambda: os.getenv(
            'OIDC_WEB_END_SESSION_ENDPOINT',
            '',
        ).strip(),
    )

    web_post_logout_redirect_uri: str = Field(
        validation_alias='OIDC_WEB_POST_LOGOUT_REDIRECT_URI',
        default_factory=lambda: os.getenv(
            'OIDC_WEB_POST_LOGOUT_REDIRECT_URI',
            '',
        ).strip(),
    )

    account_url: str = Field(
        validation_alias='OIDC_ACCOUNT_URL',
        default_factory=lambda: os.getenv('OIDC_ACCOUNT_URL', '').strip(),
    )

    passwords_managed_externally: bool = Field(
        validation_alias='OIDC_PASSWORDS_MANAGED_EXTERNALLY',
        default_factory=lambda: _env_bool(
            'OIDC_PASSWORDS_MANAGED_EXTERNALLY',
            False,
        ),
    )

    state_ttl_seconds: int = Field(
        validation_alias='OIDC_STATE_TTL_SECONDS',
        default_factory=lambda: max(
            60,
            min(600, int(os.getenv('OIDC_STATE_TTL_SECONDS', '300'))),
        ),
    )

    @property
    def audiences(self) -> tuple[str, ...]:
        """Return the configured API audiences as non-empty values."""
        return tuple(
            value.strip() for value in self.audience.split(',') if value.strip()
        )

    @property
    def algorithms(self) -> tuple[str, ...]:
        """Return the explicit asymmetric JWT algorithms accepted for OIDC."""
        return tuple(
            value.strip() for value in self.jwt_algorithms.split(',') if value.strip()
        )

    @property
    def web_client_configured(self) -> bool:
        """Return whether this deployment can start BFF OIDC sign-in."""
        return self.enabled and all(
            (
                self.web_client_id,
                self.web_client_secret,
                self.web_authorization_endpoint,
                self.web_token_endpoint,
                self.web_redirect_uri,
            ),
        )

    @property
    def web_logout_configured(self) -> bool:
        """Return whether the BFF can end the Keycloak browser session."""
        return self.web_client_configured and all(
            (
                self.web_end_session_endpoint,
                self.web_post_logout_redirect_uri,
            ),
        )

    @model_validator(mode='after')
    def validate_oidc(self):
        if self.passwords_managed_externally and not self.enabled:
            raise RuntimeError(
                'OIDC_ENABLED is required when Keycloak manages passwords',
            )
        if self.enabled:
            missing = [
                name
                for name, value in (
                    ('OIDC_ISSUER_URL', self.issuer_url),
                    ('OIDC_JWKS_URL', self.jwks_url),
                    ('OIDC_AUDIENCE', self.audience),
                    ('OIDC_IDENTITY_PROVIDER', self.identity_provider),
                )
                if not value
            ]
            if missing:
                raise RuntimeError(
                    'OIDC is enabled but required settings are missing: '
                    + ', '.join(missing),
                )
            if len(self.identity_provider) > 20:
                raise RuntimeError(
                    'OIDC_IDENTITY_PROVIDER must be at most 20 characters',
                )
            if self.passwords_managed_externally and not self.account_url:
                raise RuntimeError(
                    'OIDC_ACCOUNT_URL is required when Keycloak manages passwords',
                )
        return self
