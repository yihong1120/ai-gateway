"""coordinator role: prompt and allowed tools, without infrastructure state."""
from __future__ import annotations

from company_agent.agents.spec import AgentSpec

SPEC = AgentSpec(
    'coordinator',
    frozenset(
        (
            'delegate_data',
            'delegate_document',
            'delegate_eip',
            'delegate_engineering',
            'delegate_knowledge',
            'delegate_research',
            'delegate_reviewer',
            'plan_delivery',
            'supersede_artifact',
            'validate_delivery',
        ),
    ),
)
