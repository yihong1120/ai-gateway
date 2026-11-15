from __future__ import annotations

import os

from pydantic import Field
from pydantic_settings import BaseSettings

from .common import _env_bool
from examples.deployment.registry.schemas import MAX_REGISTRY_TTL_SECONDS


class DeploymentSettings(BaseSettings):
    model_config = {'frozen': True, 'populate_by_name': True}

    deployment_api_base_path: str = Field(
        validation_alias='DEPLOYMENT_API_BASE_PATH',
        default_factory=lambda: os.getenv(
            'DEPLOYMENT_API_BASE_PATH',
            '/hazard/api',
        ),
    )

    local_development_auth_enabled: bool = Field(
        validation_alias='LOCAL_DEVELOPMENT_AUTH_ENABLED',
        default_factory=lambda: _env_bool(
            'LOCAL_DEVELOPMENT_AUTH_ENABLED',
            False,
        ),
    )

    local_development_deployment_id: str = Field(
        validation_alias='LOCAL_DEVELOPMENT_DEPLOYMENT_ID',
        default_factory=lambda: os.getenv(
            'LOCAL_DEVELOPMENT_DEPLOYMENT_ID',
            '',
        ),
    )

    deployment_registry_ed25519_private_key: str = Field(
        validation_alias='DEPLOYMENT_REGISTRY_ED25519_PRIVATE_KEY',
        default_factory=lambda: os.getenv(
            'DEPLOYMENT_REGISTRY_ED25519_PRIVATE_KEY',
            '',
        ),
    )

    deployment_registry_key_id: str = Field(
        validation_alias='DEPLOYMENT_REGISTRY_KEY_ID',
        default_factory=lambda: os.getenv(
            'DEPLOYMENT_REGISTRY_KEY_ID',
            'registry-ed25519-2026-01',
        ),
    )

    deployment_registry_ttl_seconds: int = Field(
        validation_alias='DEPLOYMENT_REGISTRY_TTL_SECONDS',
        default_factory=lambda: int(
            os.getenv(
                'DEPLOYMENT_REGISTRY_TTL_SECONDS',
                str(MAX_REGISTRY_TTL_SECONDS),
            ),
        ),
    )

    deployment_enrollment_code_pepper: str = Field(
        validation_alias='DEPLOYMENT_ENROLLMENT_CODE_PEPPER',
        default_factory=lambda: os.getenv(
            'DEPLOYMENT_ENROLLMENT_CODE_PEPPER',
            '',
        ),
    )

    deployment_enrollment_rate_limit_max: int = Field(
        validation_alias='DEPLOYMENT_ENROLLMENT_RATE_LIMIT_MAX',
        default_factory=lambda: int(
            os.getenv('DEPLOYMENT_ENROLLMENT_RATE_LIMIT_MAX', '5'),
        ),
    )

    deployment_enrollment_rate_limit_window_seconds: int = Field(
        validation_alias='DEPLOYMENT_ENROLLMENT_RATE_LIMIT_WINDOW_SECONDS',
        default_factory=lambda: int(
            os.getenv('DEPLOYMENT_ENROLLMENT_RATE_LIMIT_WINDOW_SECONDS', '300'),
        ),
    )
