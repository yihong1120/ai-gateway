"""research role: prompt and allowed tools, without infrastructure state."""
from __future__ import annotations

from company_agent.agents.spec import AgentSpec

SPEC = AgentSpec(
    'research',
    frozenset(
        (
            'fetch_url',
            'search_web',
        ),
    ),
)
