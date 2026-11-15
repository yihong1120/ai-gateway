"""Verify a resource-bound token and live Keycloak session, then resolve owner."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import time

import httpx
import jwt
from mcp.server.auth.provider import AccessToken

from .config import SCOPE


class AuthorizationUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class Principal:
    owner: str
    subject: str
    client: str


class Verifier:
    def __init__(self, configuration, client, *, resolve_owner, key_client=None):
        self.configuration, self.client, self.resolve_owner = configuration, client, resolve_owner
        self.keys = key_client or jwt.PyJWKClient(
            configuration.issuer + '/protocol/openid-connect/certs', timeout=5,
        )

    async def verify_token(self, token):
        config = self.configuration
        if not isinstance(token, str) or len(token) > 16384 or any(c.isspace() for c in token):
            return None
        try:
            key = await asyncio.to_thread(self.keys.get_signing_key_from_jwt, token)
            claims = jwt.decode(
                token, key.key, algorithms=['RS256'], audience=config.resource,
                issuer=config.issuer, options={'require': ['iss', 'aud', 'sub', 'iat', 'exp', 'azp']},
            )
            subject, authorized_party = claims['sub'], claims['azp']
            if (not isinstance(subject, str) or not subject or subject != subject.strip() or len(subject) > 255
                    or authorized_party not in config.clients
                    or not isinstance(claims.get('scope'), str) or SCOPE not in claims['scope'].split()
                    or claims.get('typ') not in (None, 'Bearer')
                    or type(claims['exp']) is not int or type(claims['iat']) is not int
                    or not 0 < claims['exp'] - claims['iat'] <= 300):
                return None
        except jwt.PyJWKClientConnectionError:
            raise AuthorizationUnavailable('Company authorization is unavailable.') from None
        except (jwt.PyJWTError, ValueError, TypeError, KeyError):
            return None
        # JWT verification alone cannot detect a user/device session logout.
        # The confidential verifier is backend-only, separate from public CLI IDs.
        try:
            response = await self.client.post(
                config.issuer + '/protocol/openid-connect/token/introspect',
                auth=httpx.BasicAuth(config.introspection_client, config.introspection_secret),
                data={'token': token, 'token_type_hint': 'access_token'}, timeout=5,
            )
            if response.status_code != 200:
                raise AuthorizationUnavailable('Company authorization is unavailable.')
            if len(response.content) > 65536:
                raise AuthorizationUnavailable('Company authorization is unavailable.')
            current = response.json()
            if not isinstance(current, dict):
                raise AuthorizationUnavailable('Company authorization is unavailable.')
            if (current.get('active') is not True or current.get('sub') != subject
                    or current.get('client_id', current.get('azp')) != authorized_party):
                return None
            owner = await self.resolve_owner(subject)
        except AuthorizationUnavailable:
            raise
        except Exception:
            raise AuthorizationUnavailable('Company authorization is unavailable.') from None
        if not owner or claims['exp'] <= time.time():
            return None
        return AccessToken(
            token=token, client_id=authorized_party, subject=subject,
            scopes=[SCOPE], resource=config.resource, expires_at=claims['exp'],
            claims={'owner': owner, 'iss': config.issuer},
        )


def principal(access):
    if not access or not access.subject or not (access.claims or {}).get('owner'):
        raise PermissionError('MCP authentication is required.')
    return Principal(access.claims['owner'], access.subject, access.client_id)
