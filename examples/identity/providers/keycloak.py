"""Application-owned KeycloakClient dependencies and operations."""

from __future__ import annotations
from examples.identity.settings.keycloak import KeycloakSettings
from collections.abc import Mapping
from typing import Literal
from urllib.parse import quote
import httpx
from fastapi import HTTPException

NativeSocialProvider = Literal['google', 'apple']


class KeycloakClient:
    def __init__(self, settings: KeycloakSettings, base_url: str | None, http_clients):
        self.settings, self.base_url, self.http_clients = (
            settings,
            base_url,
            http_clients,
        )

    async def service_access_token(self) -> str:
        """Acquire a short-lived Admin API token without storing it in Redis."""
        if self.base_url is None:
            raise HTTPException(
                status_code=503, detail='keycloak_identity_service_unavailable'
            )
        settings = self.settings
        token_url = f'{self.base_url}/realms/{quote(settings.realm, safe="")}/protocol/openid-connect/token'
        try:
            client = await self.http_clients.get('keycloak-admin')
            response = await client.post(
                token_url,
                data={
                    'grant_type': 'client_credentials',
                    'client_id': settings.user_linker_client_id,
                    'client_secret': settings.user_linker_client_secret,
                },
            )
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=503, detail='keycloak_identity_service_unavailable'
            ) from exc
        if response.status_code != 200:
            raise HTTPException(
                status_code=503, detail='keycloak_identity_service_unavailable'
            )
        try:
            token = response.json().get('access_token')
        except ValueError as exc:
            raise HTTPException(
                status_code=503, detail='keycloak_identity_service_unavailable'
            ) from exc
        if not isinstance(token, str) or not token:
            raise HTTPException(
                status_code=503, detail='keycloak_identity_service_unavailable'
            )
        return token

    async def admin_request(
        self, method: str, path: str, *, json_body: Mapping[str, object] | None = None
    ) -> httpx.Response:
        """Perform one authenticated Keycloak Admin API request."""
        settings = self.settings
        token = await self.service_access_token()
        url = f'{self.base_url}/admin/realms/{quote(settings.realm, safe="")}{path}'
        try:
            client = await self.http_clients.get('keycloak-admin')
            return await client.request(
                method,
                url,
                headers={'Authorization': f'Bearer {token}'},
                json=json_body,
            )
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=503, detail='keycloak_identity_service_unavailable'
            ) from exc

    async def link_federated_identity(
        self,
        *,
        keycloak_subject: str,
        provider: NativeSocialProvider,
        provider_subject: str,
    ) -> Literal['linked', 'already_linked']:
        """Attach an immutable provider subject using Keycloak's Admin API.

        The backend selects the target solely from the verified current Keycloak
        token.  It never accepts a user ID or an email in the client payload.
        """
        user_path = f'/users/{quote(keycloak_subject, safe="")}/federated-identity'
        existing = await self.admin_request('GET', user_path)
        if existing.status_code != 200:
            raise HTTPException(
                status_code=503, detail='keycloak_identity_service_unavailable'
            )
        try:
            identities = existing.json()
        except ValueError as exc:
            raise HTTPException(
                status_code=503, detail='keycloak_identity_service_unavailable'
            ) from exc
        if not isinstance(identities, list):
            raise HTTPException(
                status_code=503, detail='keycloak_identity_service_unavailable'
            )
        for identity in identities:
            if (
                not isinstance(identity, Mapping)
                or identity.get('identityProvider') != provider
            ):
                continue
            if identity.get('userId') == provider_subject:
                return 'already_linked'
            raise HTTPException(status_code=409, detail='provider_already_linked')
        response = await self.admin_request(
            'POST',
            f'{user_path}/{provider}',
            json_body={
                'identityProvider': provider,
                'userId': provider_subject,
                'userName': provider_subject,
            },
        )
        if response.status_code == 204:
            return 'linked'
        if response.status_code == 409:
            latest = await self.admin_request('GET', user_path)
            if latest.status_code == 200:
                try:
                    identities = latest.json()
                except ValueError:
                    identities = []
                if isinstance(identities, list) and any(
                    (
                        isinstance(identity, Mapping)
                        and identity.get('identityProvider') == provider
                        and (identity.get('userId') == provider_subject)
                        for identity in identities
                    )
                ):
                    return 'already_linked'
            raise HTTPException(
                status_code=409, detail='provider_identity_already_linked'
            )
        raise HTTPException(
            status_code=503, detail='keycloak_identity_service_unavailable'
        )
