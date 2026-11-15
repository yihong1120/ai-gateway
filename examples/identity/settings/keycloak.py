from __future__ import annotations

import os
from urllib.parse import urlsplit

from pydantic import Field
from pydantic_settings import BaseSettings


class KeycloakSettings(BaseSettings):
    model_config = {'frozen': True, 'populate_by_name': True}

    admin_base_url: str = Field(
        validation_alias='KEYCLOAK_ADMIN_BASE_URL',
        default_factory=lambda: (
            os.getenv(
                'KEYCLOAK_ADMIN_BASE_URL',
                '',
            )
            .strip()
            .rstrip('/')
        ),
    )

    realm: str = Field(
        validation_alias='KEYCLOAK_REALM',
        default_factory=lambda: os.getenv('KEYCLOAK_REALM', 'visionnaire').strip(),
    )

    user_linker_client_id: str = Field(
        validation_alias='KEYCLOAK_USER_LINKER_CLIENT_ID',
        default_factory=lambda: os.getenv(
            'KEYCLOAK_USER_LINKER_CLIENT_ID',
            'visionnaire-user-linker',
        ).strip(),
    )

    user_linker_client_secret: str = Field(
        validation_alias='KEYCLOAK_USER_LINKER_CLIENT_SECRET',
        default_factory=lambda: os.getenv(
            'KEYCLOAK_USER_LINKER_CLIENT_SECRET',
            '',
        ).strip(),
    )

    def resolve_base_url(self, issuer_url: str) -> str:
        """Return trusted Keycloak base used by backend-only Admin calls."""
        candidate = self.admin_base_url
        if not candidate:
            marker = '/realms/'
            if marker not in issuer_url:
                raise RuntimeError(
                    'OIDC_ISSUER_URL must contain /realms/ for Keycloak native social exchange',
                )
            candidate = issuer_url.split(marker, maxsplit=1)[0]
        parsed = urlsplit(candidate)
        if (
            parsed.scheme not in {'http', 'https'}
            or not parsed.netloc
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise RuntimeError('KEYCLOAK_ADMIN_BASE_URL is invalid')
        return candidate.rstrip('/')
