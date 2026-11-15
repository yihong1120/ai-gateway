"""engineering role: prompt and allowed tools, without infrastructure state."""
from __future__ import annotations

from company_agent.agents.spec import AgentSpec

SPEC = AgentSpec(
    'engineering',
    frozenset(
        (
            'create_cad',
            'edit_cad_text',
            'inspect_artifact',
            'read_cad',
            'read_ocr',
            'search_files',
            'understand_image',
            'plan_delivery',
        ),
    ),
)
