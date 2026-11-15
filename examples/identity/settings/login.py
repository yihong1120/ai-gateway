from __future__ import annotations

import os

from pydantic import Field
from pydantic_settings import BaseSettings


class LoginSettings(BaseSettings):
    model_config = {'frozen': True, 'populate_by_name': True}

    failure_window_seconds: int = Field(
        validation_alias='LOGIN_FAILURE_WINDOW_SECONDS',
        default_factory=lambda: int(
            os.getenv('LOGIN_FAILURE_WINDOW_SECONDS', '1800'),
        ),
    )

    cooldown_threshold: int = Field(
        validation_alias='LOGIN_COOLDOWN_THRESHOLD',
        default_factory=lambda: int(
            os.getenv('LOGIN_COOLDOWN_THRESHOLD', '5'),
        ),
    )

    cooldown_seconds: int = Field(
        validation_alias='LOGIN_COOLDOWN_SECONDS',
        default_factory=lambda: int(
            os.getenv('LOGIN_COOLDOWN_SECONDS', '300'),
        ),
    )

    lock_threshold: int = Field(
        validation_alias='LOGIN_LOCK_THRESHOLD',
        default_factory=lambda: int(
            os.getenv('LOGIN_LOCK_THRESHOLD', '10'),
        ),
    )

    lock_seconds: int = Field(
        validation_alias='LOGIN_LOCK_SECONDS',
        default_factory=lambda: int(
            os.getenv('LOGIN_LOCK_SECONDS', '1800'),
        ),
    )
