"""data role: prompt and allowed tools, without infrastructure state."""
from __future__ import annotations

from company_agent.agents.spec import AgentSpec

SPEC = AgentSpec(
    'data',
    frozenset(
        (
            'analyze_spreadsheet',
            'create_spreadsheet',
            'create_spreadsheet_report',
            'edit_spreadsheet',
            'inspect_artifact',
            'read_spreadsheet',
        ),
    ),
)
