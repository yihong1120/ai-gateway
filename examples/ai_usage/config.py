"""Configuration for the private AI usage API.

Personal queries need public Keycloak settings and a restricted LiteLLM reader
key. Optional administrator queries also need read access to the local account
directory. No Keycloak client secret or LiteLLM master key is required.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from examples.shared.config import load_service


class ConfigurationError(ValueError):
    """Raised when an AI usage service setting is absent or unsafe."""


_ASYMMETRIC_OIDC_ALGORITHMS = frozenset(
    {
        'ES256',
        'ES384',
        'ES512',
        'PS256',
        'PS384',
        'PS512',
        'RS256',
        'RS384',
        'RS512',
    },
)


def _value(name: str, *fallback_names: str) -> str:
    """Return the first non-empty environment value among trusted names."""
    for candidate in (name, *fallback_names):
        value = os.getenv(candidate, '').strip()
        if value:
            return value
    return ''


def _require(name: str, *fallback_names: str) -> str:
    """Return a required environment value or a safe configuration error."""
    value = _value(name, *fallback_names)
    if not value:
        raise ConfigurationError(f'{name} is required')
    return value


def _http_url(name: str, value: str) -> str:
    """Require an absolute HTTP(S) URL and normalise its trailing slash."""
    try:
        parsed = urlsplit(value)
    except ValueError:
        raise ConfigurationError(f'{name} must be an absolute HTTP(S) URL') from None
    if parsed.scheme not in {'http', 'https'} or not parsed.netloc:
        raise ConfigurationError(f'{name} must be an absolute HTTP(S) URL')
    return value.rstrip('/')


def _positive_int(
    name: str,
    value: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    """Parse a bounded positive integer setting."""
    try:
        result = int(value)
    except ValueError as exc:
        raise ConfigurationError(f'{name} must be an integer') from exc
    if not minimum <= result <= maximum:
        raise ConfigurationError(
            f'{name} must be between {minimum} and {maximum}',
        )
    return result


def _positive_float(
    name: str,
    value: str,
    *,
    minimum: float,
    maximum: float,
) -> float:
    """Parse a bounded positive floating-point setting."""
    try:
        result = float(value)
    except ValueError as exc:
        raise ConfigurationError(f'{name} must be a number') from exc
    if not minimum <= result <= maximum:
        raise ConfigurationError(
            f'{name} must be between {minimum} and {maximum}',
        )
    return result


def _csv(value: str) -> tuple[str, ...]:
    """Split a comma-separated setting and discard blank values."""
    return tuple(part.strip() for part in value.split(',') if part.strip())


@dataclass(frozen=True, slots=True, kw_only=True)
class UsageQuerySettings:
    """A validated read-only usage snapshot without login or directory settings."""

    litellm_base_url: str
    litellm_reader_key: str = field(repr=False)
    daily_budget_usd: Decimal = Decimal('4.00')
    timezone_name: str = 'Asia/Taipei'
    timezone: ZoneInfo | None = None
    request_timeout_seconds: float = 15
    log_page_size: int = 1000
    max_log_pages: int = 100
    max_record_limit: int = 100
    local_model_names: frozenset[str] = frozenset({'qwen3.5-35b-a3b', 'gpt-oss-20b'})
    official_document_key_hash: str = ''
    max_range_days: int = 366

    def __post_init__(self):
        object.__setattr__(self, 'litellm_base_url', _http_url('AI_USAGE_LITELLM_BASE_URL', self.litellm_base_url))
        try:
            budget = Decimal(str(self.daily_budget_usd))
        except InvalidOperation:
            raise ConfigurationError('AI_USAGE_DAILY_BUDGET_USD must be a decimal amount') from None
        if not budget.is_finite() or not 0 < budget <= Decimal(100000):
            raise ConfigurationError('AI_USAGE_DAILY_BUDGET_USD must be above 0 and reasonable')
        object.__setattr__(self, 'daily_budget_usd', budget)
        try:
            zone = ZoneInfo(self.timezone_name)
        except (ZoneInfoNotFoundError, ValueError):
            raise ConfigurationError('AI_USAGE_TIMEZONE must be a valid IANA timezone') from None
        object.__setattr__(self, 'timezone', zone)
        object.__setattr__(self, 'request_timeout_seconds', _positive_float(
            'AI_USAGE_REQUEST_TIMEOUT_SECONDS', self.request_timeout_seconds, minimum=1, maximum=120))
        for name, maximum in (('log_page_size', 1000), ('max_log_pages', 1000),
                              ('max_record_limit', 500), ('max_range_days', 366)):
            object.__setattr__(self, name, _positive_int('AI_USAGE_' + name.upper(), getattr(self, name),
                                                      minimum=1, maximum=maximum))
        object.__setattr__(self, 'local_model_names', frozenset(self.local_model_names))
        fingerprint = self.official_document_key_hash.lower()
        if fingerprint and (len(fingerprint) != 64 or any(char not in '0123456789abcdef' for char in fingerprint)):
            raise ConfigurationError('AI_USAGE_OFFICIAL_DOCUMENT_KEY_HASH must be a SHA256 hex digest')
        object.__setattr__(self, 'official_document_key_hash', fingerprint)


@dataclass(frozen=True, slots=True)
class AiUsageSettings(UsageQuerySettings):
    """Usage query settings plus the API's identity and directory contract."""

    oidc_issuer_url: str
    oidc_jwks_url: str
    oidc_audiences: tuple[str, ...]
    oidc_algorithms: tuple[str, ...]
    oidc_jwks_cache_seconds: int
    oidc_jwks_timeout_seconds: float
    admin_database_url: str = field(default='', repr=False)
    oidc_identity_provider: str = 'keycloak'
    gateway_tenant_id: str = ''

    @classmethod
    def from_environment(cls) -> AiUsageSettings:
        """Build a validated service snapshot; explicit process env wins."""
        load_service('ai_usage')

        audiences = _csv(
            _require('AI_USAGE_OIDC_AUDIENCE', 'OIDC_AUDIENCE'),
        )
        if not audiences:
            raise ConfigurationError('AI_USAGE_OIDC_AUDIENCE is required')

        algorithms = _csv(
            _value('AI_USAGE_OIDC_ALGORITHMS', 'OIDC_JWT_ALGORITHMS')
            or 'RS256',
        )
        if not algorithms or not set(algorithms) <= _ASYMMETRIC_OIDC_ALGORITHMS:
            raise ConfigurationError(
                'AI_USAGE_OIDC_ALGORITHMS must contain asymmetric algorithms',
            )

        local_models = frozenset(
            _csv(
                _value('AI_USAGE_LOCAL_MODEL_NAMES')
                or 'qwen3.5-35b-a3b,gpt-oss-20b',
            ),
        )
        gateway_tenant_id = _value('AI_USAGE_GATEWAY_TENANT_ID')
        if gateway_tenant_id:
            try:
                gateway_tenant_id = str(UUID(gateway_tenant_id))
            except ValueError as exc:
                raise ConfigurationError('AI_USAGE_GATEWAY_TENANT_ID must be a tenant UUID') from exc

        return cls(
            litellm_base_url=_require('AI_USAGE_LITELLM_BASE_URL', 'LITELLM_USAGE_API_URL'),
            litellm_reader_key=_require('LITELLM_USAGE_READER_KEY'),
            daily_budget_usd=_value('AI_USAGE_DAILY_BUDGET_USD') or '4.00',
            timezone_name=_value('AI_USAGE_TIMEZONE', 'TZ') or 'Asia/Taipei',
            oidc_issuer_url=_http_url(
                'AI_USAGE_OIDC_ISSUER_URL',
                _require('AI_USAGE_OIDC_ISSUER_URL', 'OIDC_ISSUER_URL'),
            ),
            oidc_jwks_url=_http_url(
                'AI_USAGE_OIDC_JWKS_URL',
                _require('AI_USAGE_OIDC_JWKS_URL', 'OIDC_JWKS_URL'),
            ),
            oidc_audiences=audiences,
            oidc_algorithms=algorithms,
            oidc_jwks_cache_seconds=_positive_int(
                'AI_USAGE_OIDC_JWKS_CACHE_SECONDS',
                _value(
                    'AI_USAGE_OIDC_JWKS_CACHE_SECONDS',
                    'OIDC_JWKS_CACHE_SECONDS',
                )
                or '300',
                minimum=60,
                maximum=86400,
            ),
            oidc_jwks_timeout_seconds=_positive_float(
                'AI_USAGE_OIDC_JWKS_TIMEOUT_SECONDS',
                _value(
                    'AI_USAGE_OIDC_JWKS_TIMEOUT_SECONDS',
                    'OIDC_JWKS_TIMEOUT_SECONDS',
                )
                or '5',
                minimum=1,
                maximum=60,
            ),
            request_timeout_seconds=_value('AI_USAGE_REQUEST_TIMEOUT_SECONDS') or '15',
            log_page_size=_value('AI_USAGE_LOG_PAGE_SIZE') or '1000',
            max_log_pages=_value('AI_USAGE_MAX_LOG_PAGES') or '100',
            max_record_limit=_value('AI_USAGE_MAX_RECORD_LIMIT') or '100',
            local_model_names=local_models,
            admin_database_url=_value('AI_USAGE_ADMIN_DATABASE_URL'),
            oidc_identity_provider=(
                _value('AI_USAGE_OIDC_IDENTITY_PROVIDER') or 'keycloak'
            ),
            official_document_key_hash=_value('AI_USAGE_OFFICIAL_DOCUMENT_KEY_HASH'),
            gateway_tenant_id=gateway_tenant_id,
            max_range_days=_value('AI_USAGE_MAX_RANGE_DAYS') or '366',
        )
