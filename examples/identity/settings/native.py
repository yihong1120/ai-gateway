from __future__ import annotations

import json
import os
from collections.abc import Mapping
from urllib.parse import urlsplit

from pydantic import Field
from pydantic import model_validator
from pydantic_settings import BaseSettings

from .common import _env_bool
from .common import _OIDC_CLIENT_ID_RE
from .common import DEFAULT_NATIVE_SOCIAL_ALLOWED_CLIENTS_JSON


class NativeSettings(BaseSettings):
    model_config = {'frozen': True, 'populate_by_name': True}

    exchange_enabled: bool = Field(
        validation_alias='NATIVE_SOCIAL_EXCHANGE_ENABLED',
        default_factory=lambda: _env_bool(
            'NATIVE_SOCIAL_EXCHANGE_ENABLED',
            False,
        ),
    )

    exchange_shared_secret: str = Field(
        validation_alias='NATIVE_SOCIAL_EXCHANGE_SHARED_SECRET',
        default_factory=lambda: os.getenv(
            'NATIVE_SOCIAL_EXCHANGE_SHARED_SECRET',
            '',
        ).strip(),
    )

    allowed_clients_json: str = Field(
        validation_alias='NATIVE_SOCIAL_ALLOWED_CLIENTS_JSON',
        default_factory=lambda: os.getenv(
            'NATIVE_SOCIAL_ALLOWED_CLIENTS_JSON',
            DEFAULT_NATIVE_SOCIAL_ALLOWED_CLIENTS_JSON,
        ).strip(),
    )

    exchange_ttl_seconds: int = Field(
        validation_alias='NATIVE_SOCIAL_EXCHANGE_TTL_SECONDS',
        default_factory=lambda: max(
            30,
            min(300, int(os.getenv('NATIVE_SOCIAL_EXCHANGE_TTL_SECONDS', '90'))),
        ),
    )

    link_ttl_seconds: int = Field(
        validation_alias='NATIVE_SOCIAL_LINK_TTL_SECONDS',
        default_factory=lambda: max(
            30,
            min(300, int(os.getenv('NATIVE_SOCIAL_LINK_TTL_SECONDS', '120'))),
        ),
    )

    link_max_auth_age_seconds: int = Field(
        validation_alias='NATIVE_SOCIAL_LINK_MAX_AUTH_AGE_SECONDS',
        default_factory=lambda: max(
            60,
            min(
                900,
                int(os.getenv('NATIVE_SOCIAL_LINK_MAX_AUTH_AGE_SECONDS', '300')),
            ),
        ),
    )

    @property
    def allowed_clients(self) -> dict[str, tuple[str, ...]]:
        """Return the strict Keycloak client/redirect allow-list.

        This is not an OAuth dynamic-registration mechanism.  Every native
        callback is a deployment-owned, exact URI and is subsequently checked
        again by Keycloak before it can issue an authorisation code.
        """
        try:
            raw = json.loads(self.allowed_clients_json)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                'NATIVE_SOCIAL_ALLOWED_CLIENTS_JSON must be valid JSON',
            ) from exc
        if not isinstance(raw, Mapping) or not raw:
            raise RuntimeError(
                'NATIVE_SOCIAL_ALLOWED_CLIENTS_JSON must be a non-empty map',
            )
        allowed: dict[str, tuple[str, ...]] = {}
        for client_id, redirect_uris in raw.items():
            if (
                not isinstance(client_id, str)
                or not _OIDC_CLIENT_ID_RE.fullmatch(client_id)
                or not isinstance(redirect_uris, list)
                or not redirect_uris
            ):
                raise RuntimeError(
                    'NATIVE_SOCIAL_ALLOWED_CLIENTS_JSON contains an invalid client entry',
                )
            validated_uris: list[str] = []
            for redirect_uri in redirect_uris:
                if not isinstance(redirect_uri, str) or not redirect_uri:
                    raise RuntimeError(
                        'NATIVE_SOCIAL_ALLOWED_CLIENTS_JSON contains an invalid redirect URI',
                    )
                parsed = urlsplit(redirect_uri)
                # Native custom schemes and HTTPS browser callbacks are both
                # valid; opaque / relative URLs, credentials, fragments, and
                # wildcard redirects are not.
                if (
                    not parsed.scheme
                    or parsed.scheme.lower() in {'data', 'file', 'javascript'}
                    or not (
                        parsed.netloc or redirect_uri.startswith(f'{parsed.scheme}:/')
                    )
                    or parsed.username
                    or parsed.password
                    or parsed.fragment
                    or '*' in redirect_uri
                ):
                    raise RuntimeError(
                        'NATIVE_SOCIAL_ALLOWED_CLIENTS_JSON contains an unsafe redirect URI',
                    )
                if parsed.scheme.lower() == 'https' and not parsed.netloc:
                    raise RuntimeError(
                        'NATIVE_SOCIAL_ALLOWED_CLIENTS_JSON contains an unsafe HTTPS redirect URI',
                    )
                if redirect_uri not in validated_uris:
                    validated_uris.append(redirect_uri)
            allowed[client_id] = tuple(validated_uris)
        return allowed

    @model_validator(mode='after')
    def validate_exchange(self):
        if self.exchange_enabled:
            if not self.exchange_shared_secret:
                raise RuntimeError(
                    'Native social exchange is enabled but required settings are missing: NATIVE_SOCIAL_EXCHANGE_SHARED_SECRET',
                )
            if len(self.exchange_shared_secret) < 32:
                raise RuntimeError(
                    'NATIVE_SOCIAL_EXCHANGE_SHARED_SECRET must be at least 32 characters',
                )
            _ = self.allowed_clients
        return self
