from __future__ import annotations
import os
from pydantic import Field
from pydantic_settings import BaseSettings
from .common import _env_bool


class CaptchaSettings(BaseSettings):
    model_config = {'frozen': True, 'populate_by_name': True}

    enabled: bool = Field(
        validation_alias='HCAPTCHA_ENABLED',
        default_factory=lambda: _env_bool('HCAPTCHA_ENABLED', True),
    )

    secret_key: str = Field(
        validation_alias='HCAPTCHA_SECRET_KEY',
        default_factory=lambda: os.getenv('HCAPTCHA_SECRET_KEY', ''),
    )

    site_key: str = Field(
        validation_alias='HCAPTCHA_SITE_KEY',
        default_factory=lambda: os.getenv('HCAPTCHA_SITE_KEY', ''),
    )

    bypass_key: str = Field(
        validation_alias='HCAPTCHA_BYPASS_KEY',
        default_factory=lambda: os.getenv('HCAPTCHA_BYPASS_KEY', ''),
    )
