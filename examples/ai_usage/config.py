"""Configuration for the private AI usage API.

Only public Keycloak resource-server settings and a restricted LiteLLM reader
key are required.  The service never needs a Keycloak client secret, the
LiteLLM master key, provider credentials, or access to Visionnaire's database.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from dataclasses import field
from decimal import Decimal
from decimal import InvalidOperation
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo
from zoneinfo import ZoneInfoNotFoundError

from dotenv import load_dotenv


class ConfigurationError(ValueError):
    """Raised when an AI usage service setting is absent or unsafe."""


_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_ENV_FILE = _REPOSITORY_ROOT / '.env.ai_usage'
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


def _load_env_file() -> None:
    """Load the dedicated service environment file without overwriting env."""
    configured = os.getenv('AI_USAGE_ENV_FILE', '').strip()
    env_file = Path(configured).expanduser(
    ) if configured else _DEFAULT_ENV_FILE
    if not env_file.is_absolute():
        env_file = _REPOSITORY_ROOT / env_file
    if env_file.is_file():
        load_dotenv(env_file, override=False)


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
    parsed = urlsplit(value)
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


@dataclass(frozen=True, slots=True)
class AiUsageSettings:
    """Validated runtime settings for the AI usage API."""

    litellm_base_url: str
    litellm_reader_key: str = field(repr=False)
    daily_budget_usd: Decimal
    timezone_name: str
    timezone: ZoneInfo
    oidc_issuer_url: str
    oidc_jwks_url: str
    oidc_audiences: tuple[str, ...]
    oidc_algorithms: tuple[str, ...]
    oidc_jwks_cache_seconds: int
    oidc_jwks_timeout_seconds: float
    request_timeout_seconds: float
    log_page_size: int
    max_log_pages: int
    max_record_limit: int
    local_model_names: frozenset[str]

    @classmethod
    def from_environment(cls) -> AiUsageSettings:
        """Build validated settings from ``.env.ai_usage`` and process env."""
        _load_env_file()

        timezone_name = _value('AI_USAGE_TIMEZONE', 'TZ') or 'Asia/Taipei'
        try:
            timezone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ConfigurationError(
                'AI_USAGE_TIMEZONE must be a valid IANA timezone',
            ) from exc

        try:
            daily_budget = Decimal(
                _value('AI_USAGE_DAILY_BUDGET_USD') or '4.00',
            )
        except InvalidOperation as exc:
            raise ConfigurationError(
                'AI_USAGE_DAILY_BUDGET_USD must be a decimal amount',
            ) from exc
        if daily_budget <= 0 or daily_budget > Decimal('100000'):
            raise ConfigurationError(
                'AI_USAGE_DAILY_BUDGET_USD must be above 0 and reasonable',
            )

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

        return cls(
            litellm_base_url=_http_url(
                'AI_USAGE_LITELLM_BASE_URL',
                _require(
                    'AI_USAGE_LITELLM_BASE_URL',
                    'LITELLM_USAGE_API_URL',
                ),
            ),
            litellm_reader_key=_require('LITELLM_USAGE_READER_KEY'),
            daily_budget_usd=daily_budget,
            timezone_name=timezone_name,
            timezone=timezone,
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
            request_timeout_seconds=_positive_float(
                'AI_USAGE_REQUEST_TIMEOUT_SECONDS',
                _value('AI_USAGE_REQUEST_TIMEOUT_SECONDS') or '15',
                minimum=1,
                maximum=120,
            ),
            log_page_size=_positive_int(
                'AI_USAGE_LOG_PAGE_SIZE',
                _value('AI_USAGE_LOG_PAGE_SIZE') or '1000',
                minimum=1,
                maximum=1000,
            ),
            max_log_pages=_positive_int(
                'AI_USAGE_MAX_LOG_PAGES',
                _value('AI_USAGE_MAX_LOG_PAGES') or '100',
                minimum=1,
                maximum=1000,
            ),
            max_record_limit=_positive_int(
                'AI_USAGE_MAX_RECORD_LIMIT',
                _value('AI_USAGE_MAX_RECORD_LIMIT') or '100',
                minimum=1,
                maximum=500,
            ),
            local_model_names=local_models,
        )
