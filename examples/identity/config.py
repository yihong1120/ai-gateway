"""Application settings facade with one validated instance of each domain."""
from __future__ import annotations

from typing import ClassVar

from pydantic import Field
from pydantic import model_validator
from pydantic_settings import BaseSettings

from .settings.bff import BffSettings
from .settings.captcha import CaptchaSettings
from .settings.database import DatabaseSettings
from .settings.deployment import DeploymentSettings
from .settings.keycloak import KeycloakSettings
from .settings.login import LoginSettings
from .settings.mail import MailSettings
from .settings.migration import MigrationSettings
from .settings.native import NativeSettings
from .settings.oidc import OidcSettings
from .settings.resources import ResourcesSettings
from .settings.sessions import SessionsSettings
from .settings.social import SocialSettings
from .settings.tokens import TokensSettings
from .settings.web import WebSettings


class Settings(BaseSettings):
    model_config = {'frozen': True}

    tokens: TokensSettings = Field(default_factory=TokensSettings)
    oidc: OidcSettings = Field(default_factory=OidcSettings)
    native: NativeSettings = Field(default_factory=NativeSettings)
    migration: MigrationSettings = Field(default_factory=MigrationSettings)
    keycloak: KeycloakSettings = Field(default_factory=KeycloakSettings)
    captcha: CaptchaSettings = Field(default_factory=CaptchaSettings)
    sessions: SessionsSettings = Field(default_factory=SessionsSettings)
    mail: MailSettings = Field(default_factory=MailSettings)
    web: WebSettings = Field(default_factory=WebSettings)
    login: LoginSettings = Field(default_factory=LoginSettings)
    social: SocialSettings = Field(default_factory=SocialSettings)
    deployment: DeploymentSettings = Field(default_factory=DeploymentSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    resources: ResourcesSettings = Field(default_factory=ResourcesSettings)
    bff: BffSettings = Field(default_factory=BffSettings)

    _legacy_fields: ClassVar[dict[str, tuple[str, str]]] = {
        'authjwt_secret_key': ('tokens', 'authjwt_secret_key'),
        'oidc_enabled': ('oidc', 'enabled'),
        'oidc_issuer_url': ('oidc', 'issuer_url'),
        'oidc_jwks_url': ('oidc', 'jwks_url'),
        'oidc_audience': ('oidc', 'audience'),
        'oidc_identity_provider': ('oidc', 'identity_provider'),
        'oidc_jwt_algorithms': ('oidc', 'jwt_algorithms'),
        'oidc_jwks_cache_seconds': ('oidc', 'jwks_cache_seconds'),
        'oidc_jwks_timeout_seconds': ('oidc', 'jwks_timeout_seconds'),
        'oidc_web_client_id': ('oidc', 'web_client_id'),
        'oidc_web_client_secret': ('oidc', 'web_client_secret'),
        'oidc_web_authorization_endpoint': ('oidc', 'web_authorization_endpoint'),
        'oidc_web_token_endpoint': ('oidc', 'web_token_endpoint'),
        'oidc_web_redirect_uri': ('oidc', 'web_redirect_uri'),
        'oidc_web_end_session_endpoint': ('oidc', 'web_end_session_endpoint'),
        'oidc_web_post_logout_redirect_uri': ('oidc', 'web_post_logout_redirect_uri'),
        'oidc_account_url': ('oidc', 'account_url'),
        'oidc_passwords_managed_externally': ('oidc', 'passwords_managed_externally'),
        'native_social_exchange_enabled': ('native', 'exchange_enabled'),
        'native_social_exchange_shared_secret': ('native', 'exchange_shared_secret'),
        'native_social_allowed_clients_json': ('native', 'allowed_clients_json'),
        'native_social_exchange_ttl_seconds': ('native', 'exchange_ttl_seconds'),
        'native_social_link_ttl_seconds': ('native', 'link_ttl_seconds'),
        'native_social_link_max_auth_age_seconds': (
            'native',
            'link_max_auth_age_seconds',
        ),
        'legacy_password_migration_enabled': ('migration', 'enabled'),
        'legacy_password_migration_ttl_seconds': ('migration', 'ttl_seconds'),
        'keycloak_admin_base_url': ('keycloak', 'admin_base_url'),
        'keycloak_realm': ('keycloak', 'realm'),
        'keycloak_user_linker_client_id': ('keycloak', 'user_linker_client_id'),
        'keycloak_user_linker_client_secret': ('keycloak', 'user_linker_client_secret'),
        'oidc_state_ttl_seconds': ('oidc', 'state_ttl_seconds'),
        'hcaptcha_enabled': ('captcha', 'enabled'),
        'hcaptcha_secret_key': ('captcha', 'secret_key'),
        'hcaptcha_site_key': ('captcha', 'site_key'),
        'hcaptcha_bypass_key': ('captcha', 'bypass_key'),
        'web_refresh_cookie_name': ('sessions', 'cookie_name'),
        'web_refresh_cookie_path': ('sessions', 'cookie_path'),
        'web_refresh_cookie_domain': ('sessions', 'cookie_domain'),
        'web_refresh_cookie_secure': ('sessions', 'cookie_secure'),
        'web_refresh_cookie_samesite': ('sessions', 'cookie_samesite'),
        'web_refresh_cookie_max_age_seconds': ('sessions', 'cookie_max_age_seconds'),
        'fcm_token_encryption_key': ('tokens', 'fcm_token_encryption_key'),
        'brevo_api_key': ('mail', 'brevo_api_key'),
        'mail_from': ('mail', 'mail_from'),
        'mail_from_name': ('mail', 'mail_from_name'),
        'app_public_url': ('web', 'app_public_url'),
        'password_reset_token_ttl_seconds': (
            'mail',
            'password_reset_token_ttl_seconds',
        ),
        'password_reset_email_rate_limit_seconds': (
            'mail',
            'password_reset_email_rate_limit_seconds',
        ),
        'password_reset_ip_rate_limit_window_seconds': (
            'mail',
            'password_reset_ip_rate_limit_window_seconds',
        ),
        'password_reset_ip_rate_limit_max': (
            'mail',
            'password_reset_ip_rate_limit_max',
        ),
        'email_verification_token_ttl_seconds': (
            'mail',
            'email_verification_token_ttl_seconds',
        ),
        'email_verification_resend_rate_limit_seconds': (
            'mail',
            'email_verification_resend_rate_limit_seconds',
        ),
        'email_verification_daily_limit': ('mail', 'email_verification_daily_limit'),
        'email_verification_daily_limit_window_seconds': (
            'mail',
            'email_verification_daily_limit_window_seconds',
        ),
        'brevo_email_verification_template_id': (
            'mail',
            'brevo_email_verification_template_id',
        ),
        'login_failure_window_seconds': ('login', 'failure_window_seconds'),
        'login_cooldown_threshold': ('login', 'cooldown_threshold'),
        'login_cooldown_seconds': ('login', 'cooldown_seconds'),
        'login_lock_threshold': ('login', 'lock_threshold'),
        'login_lock_seconds': ('login', 'lock_seconds'),
        'google_client_ids': ('social', 'google_client_ids'),
        'apple_client_ids': ('social', 'apple_client_ids'),
        'apple_team_id': ('social', 'apple_team_id'),
        'apple_key_id': ('social', 'apple_key_id'),
        'apple_private_key': ('social', 'apple_private_key'),
        'apple_private_key_path': ('social', 'apple_private_key_path'),
        'apple_service_id': ('social', 'apple_service_id'),
        'apple_bundle_id': ('social', 'apple_bundle_id'),
        'apple_redirect_uri': ('social', 'apple_redirect_uri'),
        'cors_allowed_origins': ('web', 'cors_allowed_origins'),
        'deployment_api_base_path': ('deployment', 'deployment_api_base_path'),
        'local_development_auth_enabled': (
            'deployment',
            'local_development_auth_enabled',
        ),
        'local_development_deployment_id': (
            'deployment',
            'local_development_deployment_id',
        ),
        'deployment_registry_ed25519_private_key': (
            'deployment',
            'deployment_registry_ed25519_private_key',
        ),
        'deployment_registry_key_id': ('deployment', 'deployment_registry_key_id'),
        'deployment_registry_ttl_seconds': (
            'deployment',
            'deployment_registry_ttl_seconds',
        ),
        'deployment_enrollment_code_pepper': (
            'deployment',
            'deployment_enrollment_code_pepper',
        ),
        'deployment_enrollment_rate_limit_max': (
            'deployment',
            'deployment_enrollment_rate_limit_max',
        ),
        'deployment_enrollment_rate_limit_window_seconds': (
            'deployment',
            'deployment_enrollment_rate_limit_window_seconds',
        ),
        'sqlalchemy_database_uri': ('database', 'database_uri'),
        'sqlalchemy_track_modifications': ('database', 'track_modifications'),
        'ALGORITHM': ('tokens', 'ALGORITHM'),
    }

    def __init__(self, **values):
        grouped = dict(values)
        for name, (group, field) in self._legacy_fields.items():
            if name in grouped:
                current = grouped.get(group, {})
                if isinstance(current, BaseSettings):
                    current = current.model_dump()
                if field in current:
                    raise ValueError('Conflicting settings field: ' + name)
                grouped[group] = {**current, field: grouped.pop(name)}
        super().__init__(**grouped)

    def __getattr__(self, name):
        if name in self._legacy_fields:
            group, field = self._legacy_fields[name]
            return getattr(getattr(self, group), field)
        return super().__getattr__(name)

    @model_validator(mode='after')
    def validate_integrations(self):
        if self.native.exchange_enabled:
            if not self.oidc.enabled:
                raise RuntimeError(
                    'OIDC_ENABLED is required for native social exchange',
                )
            if not self.keycloak.user_linker_client_secret or not self.keycloak.realm:
                raise RuntimeError(
                    'Native social exchange requires KEYCLOAK_USER_LINKER_CLIENT_SECRET and KEYCLOAK_REALM',
                )
            _ = self.resolved_keycloak_admin_base_url
        if self.migration.enabled:
            if not self.oidc.passwords_managed_externally:
                raise RuntimeError(
                    'OIDC_PASSWORDS_MANAGED_EXTERNALLY is required for legacy password migration',
                )
            if len(self.native.exchange_shared_secret) < 32:
                raise RuntimeError(
                    'NATIVE_SOCIAL_EXCHANGE_SHARED_SECRET must be at least 32 characters when legacy password migration is enabled',
                )
        return self

    @property
    def resolved_keycloak_admin_base_url(self):
        return self.keycloak.resolve_base_url(self.oidc.issuer_url)

    @property
    def native_social_allowed_clients(self):
        return self.native.allowed_clients

    @property
    def oidc_audiences(self):
        return self.oidc.audiences

    @property
    def oidc_algorithms(self):
        return self.oidc.algorithms

    @property
    def oidc_web_client_configured(self):
        return self.oidc.web_client_configured

    @property
    def oidc_web_logout_configured(self):
        return self.oidc.web_logout_configured
