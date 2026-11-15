"""Explicit environment configuration; disabled until an operator configures it."""
from __future__ import annotations

from dataclasses import dataclass, field
from ipaddress import ip_network
import os
import json
import re
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Settings:
    origin: str
    state_dir: Path
    litellm_url: str
    master_key: str = field(repr=False)
    models: tuple[str, ...] = ('claude-sonnet-5',)
    inference_cidrs: tuple[str, ...] = ()
    inference_deny_cidrs: tuple[str, ...] = ()
    environment: str = 'dev'
    lease_seconds: int = 30 * 86400
    confirmation_seconds: int = 90 * 86400
    renew_before_seconds: int = 7 * 86400
    enrollment_seconds: int = 600
    delivery_seconds: int = 300
    max_devices: int = 3
    rpm: int = 30
    tpm: int = 100000
    identity_cache_seconds: int = 30
    maintenance_seconds: int = 300
    issuer: str = ''
    directory_client: str = ''
    directory_secret: str = field(default='', repr=False)
    role_client_uuid: str = ''
    required_roles: tuple[str, ...] = ('dev-user', 'dev-admin')
    admin_roles: tuple[str, ...] = ('dev-admin',)
    oidc_audience: str = ''
    bridge_key: str = field(default='', repr=False)
    network_proxy_key: str = field(default='', repr=False)
    inference_networks: tuple = field(default=(), init=False, repr=False)
    inference_denied_networks: tuple = field(default=(), init=False, repr=False)
    approval_path: str = '/company-ai/setup'
    control_origins: tuple[str, ...] = ()
    # Codex shares the Claude model policy; the optional field is retained for
    # compatibility but may not narrow or reorder that policy.
    codex_models: tuple[str, ...] = ()
    codex_release: str = ''
    codex_catalog_path: Path | None = None
    codex_catalog: dict = field(default_factory=dict, init=False, repr=False, compare=False)
    mcp_url: str = ''
    mcp_codex_client: str = ''
    mcp_claude_client: str = ''

    @property
    def mcp_policy(self):
        if not self.mcp_url:
            return None
        return {
            'version': 1, 'url': self.mcp_url, 'issuer': self.issuer,
            'scope': 'company.resources', 'resource_mode': 'all_owned_bindings',
            'codex_client_id': self.mcp_codex_client, 'claude_client_id': self.mcp_claude_client,
            'codex_callback_port': 18431, 'claude_callback_port': 18432,
        }

    @property
    def all_models(self):
        return tuple(dict.fromkeys((*self.models, *self.codex_models)))

    @property
    def inference_routes(self):
        routes = ['/v1/messages', '/v1/messages/count_tokens', '/v1/models']
        if self.codex_models:
            routes.append('/v1/responses')
        return routes

    def __post_init__(self):
        if self.mcp_url or self.mcp_codex_client or self.mcp_claude_client:
            endpoint = urlsplit(self.mcp_url)
            if (endpoint.scheme != 'https' or not endpoint.hostname or endpoint.path != '/company-mcp'
                    or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment
                    or not self.issuer or not self.mcp_codex_client or not self.mcp_claude_client
                    or self.mcp_codex_client == self.mcp_claude_client
                    or any(not re.fullmatch(r'[A-Za-z0-9._-]{1,100}', item)
                           for item in (self.mcp_codex_client, self.mcp_claude_client))):
                raise ValueError('MCP policy needs a fixed HTTPS resource and two distinct public OAuth client IDs.')
        for origin in (self.origin, *self.control_origins):
            parsed = urlsplit(origin)
            if (parsed.scheme != 'https' or not parsed.hostname or parsed.path or parsed.query or parsed.fragment
                    or '*' in parsed.netloc or parsed.username is not None or parsed.password is not None):
                raise ValueError('AI access origin must be an exact HTTPS origin without a path.')
        upstream = urlsplit(self.litellm_url)
        if (upstream.scheme != 'http' or upstream.hostname not in {'127.0.0.1', '::1', 'localhost'} or upstream.path
                or upstream.query or upstream.fragment or upstream.username is not None or upstream.password is not None):
            raise ValueError('LiteLLM management and inference must remain on loopback.')
        if not self.master_key or not self.models or len(set(self.models)) != len(self.models):
            raise ValueError('An explicit master key and unique model allowlist are required.')
        if any(not isinstance(model, str) or not re.fullmatch(r'[A-Za-z0-9._/-]{1,200}', model)
               for model in self.all_models):
            raise ValueError('Invalid company model identifier.')
        if self.codex_models or self.codex_release or self.codex_catalog_path:
            if self.codex_models and self.codex_models != self.models:
                raise ValueError('Codex must use the same ordered model list as Claude.')
            object.__setattr__(self, 'codex_models', self.models)
            if not re.fullmatch(r'\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?', self.codex_release):
                raise ValueError('Codex requires an explicitly approved CLI release.')
            path = self.codex_catalog_path
            if path is None or not path.is_absolute() or path.is_symlink() or not path.is_file():
                raise ValueError('Codex requires an absolute local model catalog file.')
            if path.stat().st_size > 1024 * 1024:
                raise ValueError('Codex catalog exceeds 1 MiB.')
            catalog = json.loads(path.read_text())
            entries = catalog.get('models', []) if isinstance(catalog, dict) else []
            if (not isinstance(entries, list) or len(entries) != len(self.codex_models)
                    or any(not isinstance(entry, dict) for entry in entries)
                    or tuple(entry.get('slug') for entry in entries) != self.codex_models):
                raise ValueError('Codex catalog must contain the same ordered model aliases as Claude.')
            for entry in entries:
                if (entry.get('upgrade') is not None or not entry.get('base_instructions')
                        or not isinstance(entry.get('context_window'), int) or entry['context_window'] < 4096
                        or entry.get('supported_in_api') is not True):
                    raise ValueError('Codex catalog needs instructions, context limits, API support and no upgrade route.')
            object.__setattr__(self, 'codex_catalog', catalog)
        if not 60 <= self.lease_seconds <= self.confirmation_seconds <= 90 * 86400:
            raise ValueError('Lease must fit within a confirmation window of at most 90 days.')
        if not 0 < self.renew_before_seconds < self.lease_seconds:
            raise ValueError('Renewal threshold must be shorter than the lease.')
        if not 0 <= self.identity_cache_seconds <= 60 or not 1 <= self.max_devices <= 10:
            raise ValueError('Invalid identity cache or device limit.')
        if not 60 <= self.enrollment_seconds <= 900 or not 30 <= self.delivery_seconds <= 600:
            raise ValueError('Enrollment and delivery must be short lived.')
        if not 1 <= self.rpm <= 1000 or not 1 <= self.tpm <= 10000000 or not 10 <= self.maintenance_seconds <= 3600:
            raise ValueError('Invalid rate or maintenance limits.')
        object.__setattr__(self, 'inference_networks', tuple(ip_network(value, strict=True) for value in self.inference_cidrs))
        object.__setattr__(self, 'inference_denied_networks', tuple(ip_network(value, strict=True) for value in self.inference_deny_cidrs))
        if self.environment not in {'dev', 'production', 'test'}:
            raise ValueError('Unknown AI access environment.')
        if not self.state_dir.is_absolute():
            raise ValueError('AI access state directory must be absolute.')
        if self.approval_path not in {'/company-ai/setup', '/company-ai/portal/'}:
            raise ValueError('Approval must use a fixed same-origin company UI.')
        if self.issuer:
            identity = urlsplit(self.issuer)
            if (identity.scheme != 'https' or not identity.hostname or '/realms/' not in identity.path
                    or identity.username is not None or identity.password is not None or identity.query or identity.fragment):
                raise ValueError('Keycloak issuer must use a fixed HTTPS realm URL.')

    @classmethod
    def from_env(cls):
        from examples.shared.config import load_service
        from examples.company_ai_access.storage.registry import database_config
        load_service('company_ai_access')
        database_config()

        def csv(name, default=''):
            return tuple(value.strip() for value in os.getenv(name, default).split(',') if value.strip())
        return cls(
            origin=os.environ['AI_ACCESS_ORIGIN'].rstrip('/'),
            state_dir=Path(os.environ['AI_ACCESS_STATE_DIR']),
            litellm_url=os.environ['AI_ACCESS_LITELLM_URL'].rstrip('/'),
            master_key=os.environ['AI_ACCESS_MASTER_KEY'],
            models=csv('AI_ACCESS_MODELS'),
            codex_models=csv('AI_ACCESS_CODEX_MODELS'),
            codex_release=os.getenv('AI_ACCESS_CODEX_RELEASE', ''),
            codex_catalog_path=Path(os.environ['AI_ACCESS_CODEX_CATALOG']) if os.getenv('AI_ACCESS_CODEX_CATALOG') else None,
            inference_cidrs=csv('AI_ACCESS_INFERENCE_CIDRS'),
            inference_deny_cidrs=csv('AI_ACCESS_INFERENCE_DENY_CIDRS'),
            environment=os.getenv('AI_ACCESS_ENVIRONMENT', 'dev'),
            issuer=os.environ['AI_ACCESS_ISSUER'].rstrip('/'),
            directory_client=os.environ['AI_ACCESS_DIRECTORY_CLIENT'],
            directory_secret=os.environ['AI_ACCESS_DIRECTORY_SECRET'],
            role_client_uuid=os.environ['AI_ACCESS_ROLE_CLIENT_UUID'],
            required_roles=csv('AI_ACCESS_REQUIRED_ROLES'),
            admin_roles=csv('AI_ACCESS_ADMIN_ROLES'),
            oidc_audience=os.getenv('AI_ACCESS_OIDC_AUDIENCE', ''),
            bridge_key=os.getenv('AI_ACCESS_BRIDGE_KEY', ''),
            network_proxy_key=os.getenv('AI_ACCESS_NETWORK_PROXY_KEY', ''),
            approval_path=os.getenv('AI_ACCESS_APPROVAL_PATH', '/company-ai/setup'),
            control_origins=csv('AI_ACCESS_CONTROL_ORIGINS'),
            mcp_url=os.getenv('AI_ACCESS_MCP_URL', ''),
            mcp_codex_client=os.getenv('AI_ACCESS_MCP_CODEX_CLIENT', ''),
            mcp_claude_client=os.getenv('AI_ACCESS_MCP_CLAUDE_CLIENT', ''),
        )
