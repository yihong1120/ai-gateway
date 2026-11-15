"""Company Agent deployment settings and file-root configuration."""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic import field_validator

from company_agent.schemas import Strict
from company_agent.services.external_tools.config import MCPServerConfig

MAX_STEPS = 80


class Root(Strict):
    name: str = Field(pattern=r'^[A-Za-z][A-Za-z0-9_-]{0,31}$')
    path: str
    readers: list[str] = Field(default_factory=list)
    writers: list[str] = Field(default_factory=list)
    model_access: bool = False

    @field_validator('path')
    @classmethod
    def absolute_root(cls, value):
        p = Path(value)
        if not p.is_absolute() or p in (Path('/'), Path.home()):
            raise ValueError('Choose an explicit project directory, not / or home')
        return value


Role = Literal['coordinator', 'knowledge', 'document', 'data', 'reviewer', 'research', 'engineering', 'eip']
Purpose = Literal['summary', 'vision', 'attachment', 'attachment_plan']


class ModelRoute(Strict):
    model: str = Field(min_length=1, max_length=100)
    reasoning_effort: Literal['minimal', 'low', 'medium', 'high'] | None = None
    max_tokens: int = Field(default=4096, ge=256, le=16384)
    request_timeout_seconds: int | None = Field(default=None, ge=10, le=300)


class ModelProfile(ModelRoute):
    description: str = Field(min_length=1, max_length=1000)
    supports_images: bool = False


class Settings(Strict):
    mcp_servers: list[MCPServerConfig] = Field(default_factory=list, max_length=8)

    @field_validator('mcp_servers')
    @classmethod
    def distinct_tool_servers(cls, value):
        if len({server.name for server in value}) != len(value):
            raise ValueError('MCP server 名稱不可重複。')
        return value

    nas_binding_revision: str | None = Field(default=None, pattern=r'^[a-f0-9]{32}$')
    nas_binding_revisions: list[str] = Field(default_factory=list, max_length=16)

    @field_validator('nas_binding_revisions')
    @classmethod
    def valid_nas_revisions(cls, values):
        import re
        if len(set(values)) != len(values) or any(not re.fullmatch(r'[a-f0-9]{32}', v) for v in values):
            raise ValueError('NAS 帳號版本不可重複且須為有效識別碼。')
        return values
    roots: list[Root] = Field(default_factory=list)
    state_dir: str
    model_name: str | None = Field(default=None, min_length=1, max_length=100)
    role_models: dict[Role, ModelRoute] = Field(default_factory=dict)
    purpose_models: dict[Purpose, ModelRoute] = Field(default_factory=dict)
    model_profiles: dict[str, ModelProfile] = Field(default_factory=dict)
    orchestration_mode: Literal['delegated', 'hybrid', 'chat'] = 'delegated'
    office_execution_mode: Literal['flexible', 'specialist'] = 'flexible'
    model_timeout_seconds: int = Field(default=60, ge=10, le=300)
    require_delivery_plan: bool = False

    @field_validator('model_profiles')
    @classmethod
    def profile_names(cls, profiles):
        import re

        if len(profiles) > 12 or any(not re.fullmatch(r'[a-z][a-z0-9_-]{0,39}', key) for key in profiles):
            raise ValueError('Use at most 12 named model profiles (lowercase letters, digits, _ and -)')
        return profiles
    max_steps: int = Field(default=40, ge=1, le=MAX_STEPS)
    max_concurrent_jobs: int = Field(default=4, ge=1, le=32)
    max_pending_jobs: int = Field(default=500, ge=1, le=10000)
    max_pending_per_user: int = Field(default=20, ge=1, le=1000)
    execution_timeout_seconds: int = Field(default=1200, gt=0)
    max_file_bytes: int = Field(default=20_000_000, ge=1024, le=50_000_000)
    summary_trigger_tokens: int = Field(default=16_000, ge=2048, le=64000)
    compact_prompts: bool = False
    require_artifact_review: bool = False
    structured_completion: bool = False
    require_user_billing: bool = False
    enable_web_research: bool = False
    enable_cad: bool = False
    enable_ocr: bool = False
    enable_spreadsheet_reports: bool = False
    enable_eip_read: bool = False
    enable_eip_processing: bool = False
    enable_vision: bool = False
    eip_binding_revision: str | None = Field(default=None, max_length=100)
