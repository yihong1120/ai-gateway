"""Loopback LiteLLM administration and read-only Keycloak membership checks."""
from __future__ import annotations

import asyncio
import time
from urllib.parse import quote

from fastapi import HTTPException
import httpx

from examples.company_ai_access.storage.registry import digest


def unavailable():
    return HTTPException(503, detail={'code': 'ai_access_dependency_unavailable'})


class LiteLLM:
    def __init__(self, settings, client):
        self.settings, self.client = settings, client

    async def call(self, path, payload):
        try:
            response = await self.client.post(self.settings.litellm_url + path, json=payload,
                headers={'Authorization': 'Bearer ' + self.settings.master_key}, timeout=30)
            if not response.is_success:
                raise unavailable()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise unavailable() from exc

    async def ensure_key(self, device, raw):
        # The caller persists a random key before this call. A retry after a
        # process crash reconciles the same key instead of leaking orphan keys.
        try:
            response = await self.client.get(self.settings.litellm_url + '/key/info',
                params={'key': digest(raw)}, headers={'Authorization': 'Bearer ' + self.settings.master_key}, timeout=15)
            if response.status_code == 200:
                info = response.json()['info']
                metadata = info.get('metadata') or {}
                if (info.get('key_alias') != self.alias(device) or metadata.get('company_ai_device') != device['id']
                        or metadata.get('company_ai_subject') != device['subject']
                        or info.get('blocked') or set(info.get('models') or []) != set(self.settings.all_models)
                        or set(info.get('allowed_routes') or []) != set(self.settings.inference_routes)):
                    raise unavailable()
                return
            if response.status_code not in (400, 401, 404):
                raise unavailable()
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            raise unavailable() from exc
        await self.call('/key/generate', {
            'key': raw, 'key_alias': self.alias(device),
            # key_type=llm_api overwrites explicit allowed_routes with the
            # broader llm_api_routes preset in LiteLLM. Keep the exact list.
            'models': list(self.settings.all_models),
            'duration': f"{max(1, device['expires_at'] - int(time.time()))}s",
            'rpm_limit': self.settings.rpm, 'tpm_limit': self.settings.tpm,
            'allowed_routes': self.settings.inference_routes,
            'metadata': {'company_ai_device': device['id'], 'company_ai_subject': device['subject'],
                         'company_ai_environment': self.settings.environment},
        })

    def alias(self, device):
        return f"company-ai-{self.settings.environment}-{device['id']}"

    async def renew(self, raw, seconds):
        await self.call('/key/update', {'key': raw, 'duration': f'{max(1, seconds)}s'})

    async def block(self, raw):
        # Local registry denial happens first, even if LiteLLM is unavailable.
        await self.call('/key/update', {'key': raw, 'blocked': True})


class Directory:
    """A separate service account with view-users only; no identity writes."""
    def __init__(self, settings, client, *, clock=time.time):
        self.settings, self.client, self.clock = settings, client, clock
        self.cache, self.roles, self.token, self.token_until = {}, {}, '', 0
        self.lock = asyncio.Lock()

    async def allowed(self, subject, *, fresh=False):
        if not subject or len(subject) > 128:
            return False
        cached = self.cache.get(subject)
        if not fresh and cached and cached[0] > self.clock():
            return cached[1]
        async with self.lock:
            try:
                value = await self._allowed(subject)
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                raise unavailable() from exc
            self.cache[subject] = (self.clock() + self.settings.identity_cache_seconds, value)
            return value

    async def _allowed(self, subject):
        config = self.settings
        if not all((config.issuer, config.directory_client, config.directory_secret, config.role_client_uuid, config.required_roles)):
            raise unavailable()
        if self.token_until <= self.clock():
            response = await self.client.post(config.issuer + '/protocol/openid-connect/token',
                data={'grant_type': 'client_credentials', 'client_id': config.directory_client,
                      'client_secret': config.directory_secret}, timeout=10)
            if response.status_code != 200:
                raise unavailable()
            result = response.json()
            self.token = result['access_token']
            self.token_until = self.clock() + max(0, int(result['expires_in']) - 30)
        base, realm = config.issuer.rsplit('/realms/', 1)
        target = base + '/admin/realms/' + quote(realm, safe='') + '/users/' + quote(subject, safe='')
        headers = {'Authorization': 'Bearer ' + self.token}
        response = await self.client.get(target, headers=headers, timeout=10)
        if response.status_code == 401:
            self.token_until = 0
        if response.status_code == 404:
            self.roles[subject] = set()
            return False
        if response.status_code != 200:
            raise unavailable()
        user = response.json()
        if user.get('id') != subject or user.get('enabled') is not True:
            self.roles[subject] = set()
            return False
        response = await self.client.get(target + '/role-mappings/clients/' + quote(config.role_client_uuid, safe='') + '/composite',
                                         headers=headers, timeout=10)
        if response.status_code != 200:
            raise unavailable()
        roles = {role['name'] for role in response.json()}
        self.roles[subject] = roles
        return bool(set(config.required_roles).intersection(roles))

    async def is_admin(self, subject):
        return await self.allowed(subject, fresh=True) and bool(set(self.settings.admin_roles).intersection(self.roles.get(subject, set())))
