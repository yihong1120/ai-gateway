"""Read-only issuer preflight before publishing a company MCP configuration."""
from __future__ import annotations

import asyncio
import json

import httpx

from .config import Configuration


def validate_metadata(configuration, metadata):
    if (not isinstance(metadata, dict) or metadata.get('issuer') != configuration.issuer
            or metadata.get('authorization_response_iss_parameter_supported') is not True
            or 'S256' not in metadata.get('code_challenge_methods_supported', [])):
        raise ValueError('MCP issuer must support PKCE S256 and issuer-bound authorization responses.')
    for key, suffix in (
        ('authorization_endpoint', '/auth'), ('token_endpoint', '/token'),
        ('jwks_uri', '/certs'), ('introspection_endpoint', '/token/introspect'),
    ):
        if metadata.get(key) != configuration.issuer + '/protocol/openid-connect' + suffix:
            raise ValueError('MCP issuer metadata does not match the configured Keycloak endpoints.')
    return {
        'issuer_metadata_valid': True, 'resource': configuration.resource,
        'public_clients': list(configuration.clients), 'pkce': 'S256',
        'issuer_bound_authorization': True,
        'eip_transport_acknowledged': configuration.eip_transport_acknowledged,
        'remaining': ['native client login/refresh', 'introspection client permission',
                      'resource scope/audience mappings', 'source network and Windows acceptance'],
    }


async def check(configuration):
    async with httpx.AsyncClient(timeout=5, trust_env=False, follow_redirects=False) as client:
        response = await client.get(configuration.issuer + '/.well-known/openid-configuration')
        if response.status_code != 200 or len(response.content) > 65536:
            raise ValueError('MCP issuer discovery is unavailable.')
        return validate_metadata(configuration, response.json())


def main():
    from examples.shared.config import load_service
    load_service('company_mcp')
    configuration = Configuration.from_environment(backend=False)
    if configuration is None:
        raise SystemExit('Company MCP is disabled; configure the dedicated service environment before preflight.')
    try:
        value = asyncio.run(check(configuration))
    except Exception:
        raise SystemExit('Company MCP issuer preflight failed; no changes were made and no credentials are displayed.') from None
    print(json.dumps(value, indent=2))


if __name__ == '__main__':
    main()
