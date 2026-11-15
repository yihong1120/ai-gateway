"""Validated, application-owned loopback transport for the WebUI bridge."""
from __future__ import annotations

import os
from dataclasses import dataclass
from dataclasses import field
from urllib.parse import urlsplit

import httpx

from examples.company_ai_access.errors import error


@dataclass(frozen=True)
class BridgeSettings:
    url: str
    key: str = field(repr=False)

    def __post_init__(self):
        try:
            target = urlsplit(self.url)
            canonical = 'http://127.0.0.1' + (f':{target.port}' if target.port is not None else '')
            valid = (
                self.url == canonical and target.scheme == 'http'
                and target.hostname == '127.0.0.1'
                and target.username is None and target.password is None
                and (target.port is None or 1 <= target.port <= 65535)
                and len(self.key) >= 32 and all(33 <= ord(c) <= 126 for c in self.key)
            )
        except ValueError:
            valid = False
        if not valid:
            raise ValueError('AI access bridge requires loopback and a private random credential.')

    @classmethod
    def from_environment(cls):
        if os.getenv('AI_ACCESS_ENABLED') != 'true':
            return None
        return cls(os.getenv('AI_ACCESS_BRIDGE_URL', ''), os.getenv('AI_ACCESS_BRIDGE_KEY', ''))


class BridgeClient:
    def __init__(self, settings: BridgeSettings, client: httpx.AsyncClient):
        self.settings, self.client = settings, client
        self.timeout = httpx.Timeout(600, connect=5)

    async def check_network(self, address):
        try:
            response = await self.client.get(
                self.settings.url + '/company-ai/network-access',
                headers={'X-Company-AI-Bridge': self.settings.key, 'X-Company-AI-Client-IP': address}, timeout=5,
            )
            if response.status_code == 403:
                raise error(403, 'ai_access_network_denied')
            if response.status_code != 200 or response.json() != {'allowed': True}:
                raise error(503, 'ai_access_dependency_unavailable')
        except (httpx.HTTPError, ValueError):
            raise error(503, 'ai_access_dependency_unavailable')

    async def send(self, method, path, headers, body):
        if not path.startswith('/') or path.startswith('//'):
            raise error(400, 'ai_access_path_not_allowed')
        headers = {name: value for name, value in headers.items() if name.lower() != 'x-company-ai-bridge'}
        headers['X-Company-AI-Bridge'] = self.settings.key
        try:
            return await self.client.send(
                self.client.build_request(
                    method, self.settings.url + path, headers=headers, json=body, timeout=self.timeout,
                ), stream=True,
            )
        except httpx.HTTPError:
            raise error(503, 'ai_access_dependency_unavailable')
