from __future__ import annotations

import os

from pydantic import Field
from pydantic_settings import BaseSettings


class DatabaseSettings(BaseSettings):
    model_config = {'frozen': True, 'populate_by_name': True}

    database_uri: str = Field(
        validation_alias='SQLALCHEMY_DATABASE_URI',
        default_factory=lambda: os.getenv(
            'DATABASE_URL',
            'postgresql+asyncpg://user:password@localhost/dbname',
        ),
    )

    track_modifications: bool = False

    pool_size: int = Field(default=2, ge=1, validation_alias='DB_POOL_SIZE')
    max_overflow: int = Field(default=1, ge=0, validation_alias='DB_MAX_OVERFLOW')
    pool_timeout_seconds: float = Field(
        default=10.0,
        gt=0,
        allow_inf_nan=False,
        validation_alias='DB_POOL_TIMEOUT_SECONDS',
    )
    pool_recycle_seconds: int = Field(
        default=1800, ge=1, validation_alias='DB_POOL_RECYCLE_SECONDS',
    )
