"""Explicit, fail-closed configuration of the resource server (not model keys)."""
from __future__ import annotations

from dataclasses import dataclass, field
from ipaddress import ip_network
import os
from urllib.parse import urlsplit


SCOPE = 'company.resources'
PATH = '/company-mcp'
METADATA_PATH = '/.well-known/oauth-protected-resource/company-mcp'
BACKEND_PATH = '/internal/company-mcp'


@dataclass(frozen=True)
class Configuration:
    origin: str
    issuer: str
    clients: tuple[str, ...]
    cidrs: tuple[str, ...]
    introspection_client: str = ''
    introspection_secret: str = field(default='', repr=False)
    backend_key: str = field(default='', repr=False)
    workspace_seconds: int = 3600
    eip_transport_acknowledged: bool = False
    networks: tuple = field(init=False, repr=False)

    def __post_init__(self):
        for value in (self.origin, self.issuer):
            url = urlsplit(value)
            if (url.scheme != 'https' or not url.hostname or url.username or url.password
                    or url.query or url.fragment or value.endswith('/')):
                raise ValueError('Company MCP needs fixed HTTPS origins/issuer without credentials or trailing slash.')
        if urlsplit(self.origin).path or not self.clients or len(set(self.clients)) != len(self.clients):
            raise ValueError('Company MCP requires an origin and distinct approved OAuth clients.')
        if any(not value or len(value) > 200 or any(c.isspace() for c in value) for value in self.clients):
            raise ValueError('Invalid MCP OAuth client identifier.')
        if not self.cidrs or bool(self.introspection_client) != bool(self.introspection_secret):
            raise ValueError('Configure MCP network restrictions and complete token introspection settings.')
        if self.backend_key and (len(self.backend_key) < 43 or len(self.backend_key) > 256 or any(c.isspace() for c in self.backend_key)):
            raise ValueError('MCP backend key must be a dedicated high-entropy service credential.')
        if not 60 <= self.workspace_seconds <= 3600:
            raise ValueError('MCP workspaces expire within one hour.')
        object.__setattr__(self, 'networks', tuple(ip_network(value, strict=True) for value in self.cidrs))
        if any(network.prefixlen == 0 for network in self.networks):
            raise ValueError('MCP must be restricted to explicitly approved networks.')

    @property
    def resource(self):
        return self.origin + PATH

    @classmethod
    def from_environment(cls, *, backend=True):
        if os.getenv('COMPANY_MCP_ENABLED', '').lower() not in {'1', 'true'}:
            return None
        issuer = os.environ['COMPANY_MCP_ISSUER'].rstrip('/')
        if backend:
            host_issuer = os.environ['OPENID_PROVIDER_URL'].split('/.well-known/', 1)[0].rstrip('/')
            if issuer != host_issuer:
                raise ValueError('MCP and WebUI must use the same trusted OIDC issuer.')
            if not os.getenv('COMPANY_MCP_INTROSPECTION_CLIENT') or not os.getenv('COMPANY_MCP_INTROSPECTION_SECRET'):
                raise ValueError('Configure backend-only confidential introspection.')
        if not os.getenv('COMPANY_MCP_BACKEND_KEY'):
            raise ValueError('Configure the dedicated private MCP backend credential.')
        return cls(
            origin=os.environ['COMPANY_MCP_ORIGIN'], issuer=issuer,
            clients=tuple(value.strip() for value in os.environ['COMPANY_MCP_CLIENTS'].split(',') if value.strip()),
            cidrs=tuple(value.strip() for value in os.environ['COMPANY_MCP_ALLOWED_CIDRS'].split(',') if value.strip()),
            introspection_client=os.environ['COMPANY_MCP_INTROSPECTION_CLIENT'] if backend else '',
            introspection_secret=os.environ['COMPANY_MCP_INTROSPECTION_SECRET'] if backend else '',
            backend_key=os.environ['COMPANY_MCP_BACKEND_KEY'],
            eip_transport_acknowledged=os.getenv('COMPANY_MCP_EIP_TRANSPORT_ACKNOWLEDGED', '').lower() in {'1', 'true'},
        )
