"""Immutable BFF transport and routing settings; no request-time environment reads."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field
from types import MappingProxyType

from pydantic import Field
from pydantic_settings import BaseSettings

from examples.shared.runtime.modules import ModuleReference

_identity_runtime = ModuleReference('examples.identity.runtime')


class BffSettings(BaseSettings):
    model_config = {'frozen': True, 'populate_by_name': True}
    ai_access: str = Field(
        default='http://127.0.0.1:8013', validation_alias='BFF_AI_ACCESS_API_URL',
    )
    chat: str = Field(
        default='http://127.0.0.1:8001', validation_alias='BFF_CHAT_API_URL',
    )
    management: str = Field(
        default='http://127.0.0.1:8005', validation_alias='BFF_MANAGEMENT_API_URL',
    )
    detection: str = Field(
        default='http://127.0.0.1:8000', validation_alias='BFF_DETECTION_API_URL',
    )
    fcm: str = Field(
        default='http://127.0.0.1:8003', validation_alias='BFF_FCM_API_URL',
    )
    files: str = Field(
        default='http://127.0.0.1:8004', validation_alias='BFF_FILES_API_URL',
    )
    streaming: str = Field(
        default='http://127.0.0.1:8800', validation_alias='BFF_STREAMING_API_URL',
    )
    violations: str = Field(
        default='http://127.0.0.1:8002', validation_alias='BFF_VIOLATIONS_API_URL',
    )
    ai_usage: str = Field(
        default='http://127.0.0.1:8012', validation_alias='BFF_AI_USAGE_API_URL',
    )
    refresh_before_seconds: int = Field(
        default=30, ge=0, validation_alias='BFF_REFRESH_BEFORE_SECONDS',
    )
    timeout_seconds: float = Field(
        default=20.0,
        gt=0,
        allow_inf_nan=False,
        validation_alias='BFF_UPSTREAM_TIMEOUT_SECONDS',
    )
    cookie_name: str = Field(
        default='__Host-vn_session',
        min_length=1,
        validation_alias='BFF_SESSION_COOKIE_NAME',
    )
    cookie_secure: bool = Field(
        default=True, validation_alias='BFF_SESSION_COOKIE_SECURE',
    )
    network_proxy_key: str = Field(
        default='', repr=False, validation_alias='BFF_COMPANY_AI_NETWORK_PROXY_KEY',
    )


@dataclass(frozen=True)
class BffOptions:
    upstreams: Mapping[str, str]
    refresh_before_seconds: int
    timeout_seconds: float
    cookie_name: str
    cookie_secure: bool
    network_proxy_key: str = field(repr=False)


def load_bff_options(settings):
    return BffOptions(
        MappingProxyType(
            {
                'ai_access': settings.ai_access,
                'chat': settings.chat,
                'db_management': settings.management,
                'detection': settings.detection,
                'fcm': settings.fcm,
                'files': settings.files,
                'file_manage': settings.files,
                'streaming': settings.streaming,
                'streaming_web': settings.streaming,
                'violations': settings.violations,
                'ai_usage': settings.ai_usage,
            },
        ),
        settings.refresh_before_seconds,
        settings.timeout_seconds,
        settings.cookie_name,
        settings.cookie_secure,
        settings.network_proxy_key,
    )


def get_bff_options():
    get_identity = _identity_runtime.attribute('get_identity')

    return get_identity().bff
