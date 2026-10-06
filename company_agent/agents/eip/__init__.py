"""eip role: prompt and allowed tools, without infrastructure state."""
from __future__ import annotations

from company_agent.agents.spec import AgentSpec

SPEC = AgentSpec(
    'eip',
    frozenset(
        (
            'eip_attachments',
            'eip_calendar_prepare',
            'eip_calendar_query',
            'eip_calendar_detail',
            'eip_process_attachments',
            'eip_query',
            'eip_read_attachment',
        ),
    ),
)
