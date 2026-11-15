"""Validated resource options, captured with the application settings."""

from __future__ import annotations
from urllib.parse import quote
from pydantic import Field, field_validator
from urllib.parse import urlsplit
from pydantic_settings import BaseSettings


class ResourcesSettings(BaseSettings):
    model_config = {'frozen': True, 'populate_by_name': True}
    redis_host: str = Field(
        default='127.0.0.1', min_length=1, validation_alias='REDIS_HOST'
    )
    redis_port: int = Field(default=6379, ge=1, le=65535, validation_alias='REDIS_PORT')
    redis_password: str = Field(
        default='', repr=False, validation_alias='REDIS_PASSWORD'
    )
    auto_create_schema: bool = Field(
        default=False, validation_alias='AUTO_CREATE_SCHEMA'
    )
    playback_api_url: str = Field(
        default='http://127.0.0.1:8800', validation_alias='PLAYBACK_STREAMING_API_URL'
    )

    @field_validator('playback_api_url')
    @classmethod
    def validate_playback_url(cls, value):
        parts = urlsplit(value)
        if (parts.scheme not in {'http', 'https'} or not parts.hostname
                or parts.username or parts.password or parts.query or parts.fragment
                or any(character.isspace() for character in value)):
            raise ValueError('PLAYBACK_STREAMING_API_URL must be an HTTP(S) service URL.')
        try:
            if parts.port is not None and not 1 <= parts.port <= 65535:
                raise ValueError('Invalid playback service port.')
        except ValueError as error:
            raise ValueError('Invalid playback service port.') from error
        return value.rstrip('/')

    playback_timeout_seconds: float = Field(
        default=20.0,
        gt=0,
        allow_inf_nan=False,
        validation_alias='PLAYBACK_UPSTREAM_TIMEOUT_SECONDS',
    )

    @property
    def redis_url(self):
        host = self.redis_host
        if ':' in host and not host.startswith('['):
            host = '[' + host + ']'
        return (
            f'redis://:{quote(self.redis_password, safe="")}@{host}:{self.redis_port}/0'
        )
