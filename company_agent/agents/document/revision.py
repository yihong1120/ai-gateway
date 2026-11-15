"""Narrow, receipt-backed revision of this job's private Word/PowerPoint draft."""
from __future__ import annotations

from langgraph.types import Command

from company_agent.runtime.tools import injected_tool
from company_agent.runtime.tools import tool_receipt
from company_agent.services.office.revision import revise as revise_artifact
from company_agent.services.office.schemas import EditArtifact
from company_agent.services.files.errors import input_error_feedback


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
        handle_tool_error=input_error_feedback,
        description='修訂本工作已產生的 DOCX/PPTX 草稿；先 inspect_artifact 取得 SHA256 及精確 run。只改一個 run。沿用相同檔名時建立新ID版本並原子取代舊草稿，舊版保留稽核；不同核准檔名則保留兩版交付。新版須重新審核，不覆寫來源或其他工作。',
    )
