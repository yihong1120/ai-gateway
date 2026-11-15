"""HTTP request, identity and response contracts. Browser responses exclude device secrets."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field


class MCPPolicy(BaseModel):
    version: Literal[1] = 1
    url: str
    issuer: str
    scope: Literal['company.resources'] = 'company.resources'
    resource_mode: Literal['all_owned_bindings'] = 'all_owned_bindings'
    codex_client_id: str
    claude_client_id: str
    codex_callback_port: int = 18431
    claude_callback_port: int = 18432


class Policy(BaseModel):
    environment: str
    portal_origin: str
    inference_base_url: str
    models: list[str]
    default_model: str
    codex_enabled: bool = False
    codex_models: list[str] = Field(default_factory=list)
    codex_release: str | None = None
    lease_seconds: int
    confirmation_seconds: int
    max_devices: int
    inference_network_configured: bool
    windows_setup_url: str
    product_name: str = 'changdar-ai'
    windows_installer_url: str | None = None
    mcp: MCPPolicy | None = None


class Device(BaseModel):
    id: str
    label: str
    status: Literal['provisioning', 'active', 'revoked']
    created_at: int
    expires_at: int
    hard_expires_at: int
    last_seen_at: int | None
    reason: str | None
    acked: bool
    confirmation_required: bool
    subject: str | None = None


class DeviceList(BaseModel):
    devices: list[Device]


class Revoked(BaseModel):
    status: Literal['revoked']
    upstream_block_pending: bool


class EnrollmentPreview(BaseModel):
    id: str
    label: str
    code: str
    status: Literal['pending', 'approved', 'delivered', 'completed']
    expires_at: int
    replace_device_id: str | None


class Approved(BaseModel):
    device_id: str
    status: Literal['approved']


class Started(BaseModel):
    request_id: str
    user_code: str
    expires_at: int
    interval: int
    verification_uri: str
    verification_uri_complete: str
    codex_enabled: bool = False


class Pending(BaseModel):
    status: Literal['pending']


class Ready(BaseModel):
    status: Literal['ready']
    device_id: str
    key: str = Field(
        repr=False,
        description='Native initiating client only. Never expose to Flutter or browser storage.',
    )
    expires_at: int
    confirmation_due_at: int
    base_url: str
    model: str


class Completed(BaseModel):
    status: Literal['completed']


class ErrorDetail(BaseModel):
    code: str
    errors: list[dict] | None = None


class ControlError(BaseModel):
    detail: ErrorDetail


@dataclass(frozen=True)
class Principal:
    subject: str
    admin: bool = False


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Start(StrictModel):
    device_name: str = Field(min_length=1, max_length=80, pattern=r'^[\w .()\-]+$')
    code_challenge: str = Field(pattern=r'^[A-Za-z0-9_-]{43}$')
    replace_device_id: str | None = Field(default=None, pattern=r'^[0-9a-f-]{36}$')


class Proof(StrictModel):
    request_id: str = Field(pattern=r'^[0-9a-f-]{36}$')
    code_verifier: str = Field(min_length=43, max_length=128, pattern=r'^[A-Za-z0-9._~-]+$')


class Approve(StrictModel):
    user_code: str = Field(pattern=r'^[A-F0-9]{4}-[A-F0-9]{4}$')
    confirm: Literal[True]
