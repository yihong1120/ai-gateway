"""knowledge role: prompt and allowed tools, without infrastructure state."""
from __future__ import annotations

from company_agent.agents.spec import AgentSpec

SPEC = AgentSpec(
    'knowledge',
    frozenset(
        (
            'read_file',
            'read_ocr',
            'read_spreadsheet',
            'search_files',
            'understand_image',
        ),
    ),
)
