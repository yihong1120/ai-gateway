"""Private, versioned interface between components of the same MCP resource.

The backend is the resource's authentication/authorization authority. Native
tokens terminate there and are never used to authenticate to NAS/EIP or another
OAuth resource. A workload key alone grants no user/resource access.
"""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

PROTOCOL = 1
ASSERTION_HEADER = 'x-company-mcp-assertion'
MAX_BYTES = 256 * 1024


class BackendRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    protocol: Literal[1]
    action: Literal['health', 'authorize', 'grant', 'execute', 'validate', 'audit']
    tool: str | None = Field(default=None, max_length=100)
    arguments: dict = Field(default_factory=dict)
    output: dict | None = None


class Identity(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    owner: str = Field(min_length=1, max_length=255)
    subject: str = Field(min_length=1, max_length=255)
    client: str = Field(min_length=1, max_length=200)
    expires: int
    issuer: str
    resource: str
