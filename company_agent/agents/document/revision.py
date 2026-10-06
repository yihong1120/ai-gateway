"""Narrow, receipt-backed revision of this job's private Word/PowerPoint draft."""
from __future__ import annotations

from langgraph.types import Command

from company_agent.runtime.tools import injected_tool
from company_agent.runtime.tools import tool_receipt
from company_agent.services.office.revision import revise as revise_artifact
from company_agent.services.office.schemas import EditArtifact


async def revise(runtime, **kwargs):
    result = await revise_artifact(
        runtime.context,
        runtime.state.get('messages', []),
        runtime.tool_call_id,
        EditArtifact.model_validate(kwargs),
    )
    runtime.stream_writer({'stage': 'tool', 'tool': 'edit_artifact_text_run'})
    return Command(
        update={'messages': [tool_receipt(runtime.tool_call_id, 'edit_artifact_text_run', result)]},
    )


def artifact_edit_tool():
    return injected_tool(
        invoke=revise,
        name='edit_artifact_text_run',
        schema=EditArtifact,
        input_name='EditArtifactInput',
        description='修訂本工作已產生的 DOCX/PPTX 草稿；先 inspect_artifact 取得 SHA256 及精確 run。只改一個 run，另存不同檔名，新版須審核。不用來源 root/path，不覆寫或操作其他工作。',
    )
