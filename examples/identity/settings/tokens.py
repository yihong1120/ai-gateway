from __future__ import annotations
import os
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings


class TokensSettings(BaseSettings):
    model_config = {'frozen': True, 'populate_by_name': True}

    authjwt_secret_key: str = Field(
        validation_alias='AUTHJWT_SECRET_KEY',
        default_factory=lambda: os.getenv('JWT_SECRET_KEY', ''),
    )

    fcm_token_encryption_key: str = Field(
        validation_alias='FCM_TOKEN_ENCRYPTION_KEY',
        default_factory=lambda: os.getenv(
            'FCM_TOKEN_ENCRYPTION_KEY',
            '',
        ),
    )

    ALGORITHM: str = 'HS256'

    @model_validator(mode='after')
    def require_secret(self):
        if not self.authjwt_secret_key:
            raise RuntimeError('JWT_SECRET_KEY is required')
        return self
