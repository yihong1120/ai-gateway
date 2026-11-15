"""Bounded same-host calls; no credential storage and no automatic retries."""
from __future__ import annotations

import asyncio
import json
import time

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from pydantic import ValidationError

from .authentication import AuthorizationUnavailable
from .authentication import principal
from .backend import ASSERTION_HEADER
from .backend import Identity
from .backend import MAX_BYTES
from .backend import PROTOCOL
from .config import SCOPE
from .storage import ResourceError
from .tools import contract_digest


class BackendClient:
    def __init__(self, configuration, settings, client):
        self.configuration, self.settings, self.client = configuration, settings, client

    async def request(self, action, *, assertion=None, tool=None, arguments=None, output=None):
        headers = {'Authorization': 'Bearer ' + self.settings.backend_key}
        if assertion is not None:
            headers[ASSERTION_HEADER] = assertion
        payload = {'protocol': PROTOCOL, 'action': action, 'tool': tool, 'arguments': arguments or {}, 'output': output}
        try:
            request = self.client.build_request(
                'POST', self.settings.backend_url, headers=headers,
                json=payload, timeout=125,
            )
            request.headers.pop('cookie', None)
            response = await self.client.send(request, stream=True, follow_redirects=False, auth=None)
            try:
                body = bytearray()
                async for part in response.aiter_bytes():
                    body.extend(part)
                    if len(body) > MAX_BYTES:
                        raise AuthorizationUnavailable('Company resource backend is unavailable.')
                value = json.loads(body)
                if not isinstance(value, dict):
                    raise ValueError()
                if response.status_code == 401:
                    raise ResourceError('authentication_required', '公司 MCP 授權已失效，請重新登入。')
                if response.status_code == 409:
                    # These are fixed application policy codes, never upstream
                    # HTML, exceptions, request arguments or credential values.
                    code = value.get('code')
                    if not isinstance(code, str) or len(code) > 100 or not code.replace('_', '').isalnum():
                        raise ValueError()
                    message = value.get('message')
                    if not isinstance(message, str) or len(message) > 500:
                        raise ValueError()
                    raise ResourceError(code, message)
                if response.status_code != 200:
                    raise AuthorizationUnavailable('Company resource backend is unavailable.')
                return value
            finally:
                await response.aclose()
        except (ResourceError, AuthorizationUnavailable):
            raise
        except Exception:
            raise AuthorizationUnavailable('Company resource backend is unavailable.') from None

    async def ready(self):
        async with asyncio.timeout(5):
            value = await self.request('health')
        config = self.configuration
        if (
            value.get('protocol') != PROTOCOL or value.get('resource') != config.resource
            or value.get('issuer') != config.issuer or value.get('tools') != contract_digest()
            or value.get('clients') != list(config.clients)
        ):
            raise AuthorizationUnavailable('Company resource backend configuration does not match.')


class RemoteVerifier:
    def __init__(self, backend):
        self.backend = backend

    async def verify_token(self, token):
        if not isinstance(token, str) or not token or len(token) > 16384 or any(c.isspace() for c in token):
            return None
        try:
            value = Identity.model_validate(await self.backend.request('authorize', assertion=token))
        except ResourceError as failure:
            if failure.code == 'authentication_required':
                return None
            raise
        except ValidationError:
            raise AuthorizationUnavailable('Company authorization is unavailable.') from None
        config = self.backend.configuration
        if (
            value.issuer != config.issuer or value.resource != config.resource or value.client not in config.clients
            or not 0 < value.expires - time.time() <= 300
        ):
            return None
        return AccessToken(
            token=token, client_id=value.client, subject=value.subject,
            scopes=[SCOPE], resource=config.resource, expires_at=value.expires,
            claims={'owner': value.owner, 'iss': config.issuer},
        )


class RemoteResources:
    def __init__(self, backend, access):
        self.backend, self.person = backend, principal(access)

    async def call(self, person, action, **kwargs):
        access = get_access_token()
        if access is None or person != self.person or principal(access) != self.person:
            raise ResourceError('authentication_required', '本人 MCP 身分已變更。')
        return await self.backend.request(action, assertion=access.token, **kwargs)

    async def authorize(self, person):
        await self.call(person, 'grant')

    async def audit(self, person, name):
        await self.call(person, 'audit', tool=name)

    async def execute(self, person, name, value):
        return await self.call(person, 'execute', tool=name, arguments=value.model_dump(mode='json'))

    async def validate_result(self, person, name, value, output=None):
        await self.call(person, 'validate', tool=name, arguments=value.model_dump(mode='json'), output=output)
