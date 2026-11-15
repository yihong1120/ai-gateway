from __future__ import annotations
import os
from pydantic import Field
from pydantic_settings import BaseSettings


class WebSettings(BaseSettings):
    model_config = {'frozen': True, 'populate_by_name': True}

    app_public_url: str = Field(
        validation_alias='APP_PUBLIC_URL',
        default_factory=lambda: os.getenv(
            'APP_PUBLIC_URL',
            'https://changdar-server.mooo.com',
        ),
    )

    cors_allowed_origins: str = Field(
        validation_alias='CORS_ALLOWED_ORIGINS',
        default_factory=lambda: os.getenv(
            'CORS_ALLOWED_ORIGINS',
            (
                'https://changdar-server.mooo.com,'
                'http://localhost:3000,http://127.0.0.1:3000,'
                'http://localhost:5000,http://127.0.0.1:5000,'
                'http://localhost:8080,http://127.0.0.1:8080'
            ),
        ),
    )
