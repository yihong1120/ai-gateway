"""document role: prompt and allowed tools, without infrastructure state."""
from __future__ import annotations

from company_agent.agents.spec import AgentSpec

SPEC = AgentSpec(
    'document',
    frozenset(
        (
            'create_slides',
            'create_text',
            'create_word',
            'edit_artifact_text_run',
            'edit_text_run',
            'inspect_artifact',
            'propose_publish',
            'read_file',
            'read_ocr',
            'read_spreadsheet',
            'understand_image',
        ),
    ),
)
