"""reviewer role: prompt and allowed tools, without infrastructure state."""
from __future__ import annotations

from company_agent.agents.spec import AgentSpec

SPEC = AgentSpec(
    'reviewer',
    frozenset(
        (
            'analyze_spreadsheet',
            'inspect_artifact',
            'read_cad',
            'read_file',
            'read_ocr',
            'read_spreadsheet',
            'understand_image',
        ),
    ),
)
