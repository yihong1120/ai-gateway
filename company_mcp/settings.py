"""The standalone edge has no database, vault key or IdP client secret."""
from __future__ import annotations

import os
from dataclasses import dataclass
from dataclasses import field
from urllib.parse import urlsplit

from .config import BACKEND_PATH


@dataclass(frozen=True)
class Settings:
    backend_key: str = field(repr=False)
    backend_url: str = 'http://127.0.0.1:8080' + BACKEND_PATH
    host: str = '127.0.0.1'
    port: int = 18090

    def __post_init__(self):
        url = urlsplit(self.backend_url)
        if (
            url.scheme != 'http' or url.hostname != '127.0.0.1' or url.path != BACKEND_PATH
            or not url.port or url.username or url.password or url.query or url.fragment
        ):
            raise ValueError('MCP backend must be the fixed same-host loopback resource authority.')
        if len(self.backend_key) < 43 or len(self.backend_key) > 256 or any(c.isspace() for c in self.backend_key):
            raise ValueError('Configure a dedicated high-entropy MCP backend credential.')
        if self.host != '127.0.0.1' or type(self.port) is not int or not 1024 <= self.port <= 65535:
            raise ValueError('Expose MCP through the approved proxy; listen on a dedicated loopback port.')

    @classmethod
    def from_environment(cls):
        return cls(
            backend_key=os.environ['COMPANY_MCP_BACKEND_KEY'],
            backend_url=os.getenv('COMPANY_MCP_BACKEND_URL', 'http://127.0.0.1:8080' + BACKEND_PATH),
            port=int(os.getenv('COMPANY_MCP_PORT', '18090')),
        )
