from __future__ import annotations
import os
from pydantic import Field
from pydantic_settings import BaseSettings
from .common import _env_bool


class MigrationSettings(BaseSettings):
    model_config = {'frozen': True, 'populate_by_name': True}

    enabled: bool = Field(
        validation_alias='LEGACY_PASSWORD_MIGRATION_ENABLED',
        default_factory=lambda: _env_bool(
            'LEGACY_PASSWORD_MIGRATION_ENABLED',
            False,
        ),
    )

    ttl_seconds: int = Field(
        validation_alias='LEGACY_PASSWORD_MIGRATION_TTL_SECONDS',
        default_factory=lambda: max(
            15,
            min(
                120,
                int(os.getenv('LEGACY_PASSWORD_MIGRATION_TTL_SECONDS', '30')),
            ),
        ),
    )
