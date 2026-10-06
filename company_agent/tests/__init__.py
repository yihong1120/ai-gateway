"""Shared tool fixtures; no services or model requests."""
from __future__ import annotations

from unittest.mock import AsyncMock

from company_agent.agents.delegation import delegation_tools
from company_agent.agents.registry import ROLE_TOOLS
from company_agent.graph import specialist_tools


def coordinator_tools():
    return delegation_tools({
        role: AsyncMock(name=role)
        for role in ROLE_TOOLS if role != 'coordinator'
    })


def all_tools():
    return [*coordinator_tools(), *specialist_tools()]
