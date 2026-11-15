"""Verified identity and injected execution dependencies; never model arguments."""
from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from typing import Any

from company_agent.config import Settings
from company_agent.runtime.storage import Store


@dataclass(frozen=True)
class Context:
    owner: str
    job: str
    settings: Settings
    store: Store
    model: Any = None  # Runtime only; never serialized into graph/checkpoints.
    delegation_id: str | None = None
    required_evidence: tuple[str, ...] = ()
    source_errors: dict[str, str] = field(default_factory=dict)  # This run only; no credentials.
    role: str = 'coordinator'
    assignment_outputs: tuple[str, ...] = ()
    external_tools: Any = field(default=None, repr=False)  # Runtime MCP/token adapter; never checkpointed.
    authorize_resources: Any = field(default=None, repr=False)  # Runtime-only resource grant guard.
