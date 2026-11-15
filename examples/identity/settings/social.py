from __future__ import annotations

import os

from pydantic import Field
from pydantic_settings import BaseSettings

from .common import _join_env_values
from .common import DEFAULT_APPLE_BUNDLE_ID
from .common import DEFAULT_APPLE_SERVICE_ID
from .common import DEFAULT_GOOGLE_CLIENT_IDS


class SocialSettings(BaseSettings):
    model_config = {'frozen': True, 'populate_by_name': True}

    google_client_ids: str = Field(
        validation_alias='GOOGLE_CLIENT_IDS',
        default_factory=lambda: (
            os.getenv('GOOGLE_CLIENT_IDS')
            or _join_env_values(
                'GOOGLE_WEB_CLIENT_ID',
                'GOOGLE_IOS_CLIENT_ID',
                'GOOGLE_ANDROID_CLIENT_ID',
            )
            or DEFAULT_GOOGLE_CLIENT_IDS
        ),
    )

    apple_client_ids: str = Field(
        validation_alias='APPLE_CLIENT_IDS',
        default_factory=lambda: (
            os.getenv('APPLE_CLIENT_IDS')
            or _join_env_values('APPLE_BUNDLE_ID', 'APPLE_SERVICE_ID')
            or f'{DEFAULT_APPLE_BUNDLE_ID},{DEFAULT_APPLE_SERVICE_ID}'
        ),
    )

    apple_team_id: str = Field(
        validation_alias='APPLE_TEAM_ID',
        default_factory=lambda: os.getenv('APPLE_TEAM_ID', '5DU8R27949'),
    )

    apple_key_id: str = Field(
        validation_alias='APPLE_KEY_ID',
        default_factory=lambda: os.getenv('APPLE_KEY_ID', 'NGC4QBS7ZY'),
    )

    apple_private_key: str = Field(
        validation_alias='APPLE_PRIVATE_KEY',
        default_factory=lambda: os.getenv('APPLE_PRIVATE_KEY', ''),
    )

    apple_private_key_path: str = Field(
        validation_alias='APPLE_PRIVATE_KEY_PATH',
        default_factory=lambda: os.getenv(
            'APPLE_PRIVATE_KEY_PATH',
            'config/secrets/apple/AuthKey_NGC4QBS7ZY.p8',
        ),
    )

    apple_service_id: str = Field(
        validation_alias='APPLE_SERVICE_ID',
        default_factory=lambda: os.getenv(
            'APPLE_SERVICE_ID',
            DEFAULT_APPLE_SERVICE_ID,
        ),
    )

    apple_bundle_id: str = Field(
        validation_alias='APPLE_BUNDLE_ID',
        default_factory=lambda: os.getenv(
            'APPLE_BUNDLE_ID',
            DEFAULT_APPLE_BUNDLE_ID,
        ),
    )

    apple_redirect_uri: str = Field(
        validation_alias='APPLE_REDIRECT_URI',
        default_factory=lambda: os.getenv(
            'APPLE_REDIRECT_URI',
            (
                'https://changdar-server.mooo.com/hazard/api/db_management/auth/apple/callback'
            ),
        ),
    )
