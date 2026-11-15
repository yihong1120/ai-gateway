from __future__ import annotations
import os
from pydantic import Field
from pydantic_settings import BaseSettings
from .common import _env_bool


class SessionsSettings(BaseSettings):
    model_config = {'frozen': True, 'populate_by_name': True}

    cookie_name: str = Field(
        validation_alias='WEB_REFRESH_COOKIE_NAME',
        default_factory=lambda: os.getenv(
            'WEB_REFRESH_COOKIE_NAME',
            'refresh_session',
        ),
    )

    cookie_path: str = Field(
        validation_alias='WEB_REFRESH_COOKIE_PATH',
        default_factory=lambda: os.getenv(
            'WEB_REFRESH_COOKIE_PATH',
            '/hazard/api/db_management',
        ),
    )

    cookie_domain: str = Field(
        validation_alias='WEB_REFRESH_COOKIE_DOMAIN',
        default_factory=lambda: os.getenv(
            'WEB_REFRESH_COOKIE_DOMAIN',
            '',
        ),
    )

    cookie_secure: bool = Field(
        validation_alias='WEB_REFRESH_COOKIE_SECURE',
        default_factory=lambda: _env_bool(
            'WEB_REFRESH_COOKIE_SECURE',
            True,
        ),
    )

    cookie_samesite: str = Field(
        validation_alias='WEB_REFRESH_COOKIE_SAMESITE',
        default_factory=lambda: os.getenv(
            'WEB_REFRESH_COOKIE_SAMESITE',
            'lax',
        ).lower(),
    )

    cookie_max_age_seconds: int = Field(
        validation_alias='WEB_REFRESH_COOKIE_MAX_AGE_SECONDS',
        default_factory=lambda: int(
            os.getenv('WEB_REFRESH_COOKIE_MAX_AGE_SECONDS', str(30 * 24 * 3600)),
        ),
    )

    encryption_key: str = Field(
        default='', repr=False, validation_alias='BFF_TOKEN_ENCRYPTION_KEY'
    )
    ttl_seconds: int = Field(
        default=30 * 24 * 3600, gt=0, validation_alias='BFF_SESSION_TTL_SECONDS'
    )
    media_ttl_seconds: int = Field(
        default=600, ge=300, le=900, validation_alias='MEDIA_SESSION_TTL_SECONDS'
    )
