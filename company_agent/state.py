"""Versioned business state extending the official agent state."""
from __future__ import annotations

from operator import or_
from typing import Annotated

from langchain.agents import AgentState

from company_agent.services.research.web import merge_web_sources

GRAPH_VERSION = 31


def merge_sources(existing, updates):
    merged = {(s['root'], s['path']): s for s in existing}
    merged.update({(s['root'], s['path']): s for s in updates})
    return list(merged.values())


def merge_roots(existing, updates):
    return sorted(set(existing) | set(updates))


class State(AgentState):
    sources: Annotated[list[dict], merge_sources]
    web_sources: Annotated[list[dict], merge_web_sources]
    accessed_roots: Annotated[list[str], merge_roots]
    pending: dict | None
    graph_version: int
    reports: Annotated[dict[str, dict], or_]
    completion: dict | None
    completion_protocol: str
    billing_identity: dict | None
    model_policy: dict | None
    nas_binding_revision: str | None
    nas_binding_revisions: list[str] | None
    orchestration_mode: str
    office_execution_mode: str
    require_delivery_plan: bool
    external_tools_policy: list[dict] | None
