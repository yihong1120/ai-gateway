"""Administrator-owned MCP endpoints and explicit remote tool allowlists."""
from __future__ import annotations

from typing import Annotated
from urllib.parse import urlsplit

import httpx
from pydantic import Field
from pydantic import field_validator

from company_agent.schemas import Strict


def validate_endpoint(value: str) -> str:
    """Keep credentials on one fixed HTTPS origin or explicit local loopback."""
    try:
        if not isinstance(value, str) or any(ord(char) <= 32 or ord(char) == 127 for char in value):
            raise ValueError()
        parts = urlsplit(value)
        _ = parts.port
        if (
            parts.scheme not in {'https', 'http'} or not parts.hostname
            or parts.username is not None or parts.password is not None
            or parts.query or parts.fragment or '?' in value or '#' in value
            or '\\' in value or '%' in parts.path
            or any(segment in {'.', '..'} for segment in parts.path.split('/'))
            or '//' in parts.path
        ):
            raise ValueError()
        if parts.scheme == 'http' and parts.hostname not in {'localhost', '127.0.0.1', '::1'}:
            raise ValueError()
        return str(httpx.URL(value)).rstrip('/')
    except (ValueError, TypeError, httpx.InvalidURL):
        raise ValueError('MCP endpoint 必須是固定 HTTPS 位址；HTTP 僅允許本機 loopback。') from None


RemoteToolName = Annotated[str, Field(min_length=1, max_length=128, pattern=r'^[A-Za-z0-9_][A-Za-z0-9_.-]*$')]


class MCPServerConfig(Strict):
    name: str = Field(min_length=1, max_length=16, pattern=r'^[a-z][a-z0-9_]*$')
    url: str = Field(min_length=1, max_length=2000)
    allowed_tools: list[RemoteToolName] = Field(min_length=1, max_length=100)

    @field_validator('url')
    @classmethod
    def fixed_endpoint(cls, value):
        return validate_endpoint(value)

    @field_validator('allowed_tools')
    @classmethod
    def unique_tools(cls, value):
        if len(value) != len(set(value)):
            raise ValueError('MCP 工具白名單不可重複。')
        return value
