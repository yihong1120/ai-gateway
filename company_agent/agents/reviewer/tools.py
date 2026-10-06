from __future__ import annotations

import asyncio
import json

from langchain_core.messages import ToolMessage
from langchain_core.tools import ToolException
from langgraph.prebuilt import ToolRuntime
from langgraph.types import Command

from company_agent.runtime.tools import injected_tool
from company_agent.runtime.tools import tool_receipt
from company_agent.services.files.inspection import inspect_artifact
from company_agent.services.files.schemas import InspectArtifact
"""Read-only inspection of an artifact belonging to this exact job."""


def inspected_artifacts(messages):
    """Proof of actual successful reads; a model's review claim is insufficient."""
    found = {}
    for message in messages:
        if isinstance(message, ToolMessage) and message.name == 'inspect_artifact' and message.status != 'error':
            value = json.loads(message.content)
            found[value['artifact_id']] = {'id': value['artifact_id'], 'sha256': value['sha256']}
    return list(found.values())


async def inspect(runtime: ToolRuntime, **kwargs):
    args = InspectArtifact.model_validate(kwargs)
    ctx = runtime.context
    # Invalid references are repairable input errors, not a failed file read.
    # Offer only this owner's current job metadata; never guess a replacement ID.
    files = await asyncio.to_thread(ctx.store.files, ctx.owner, ctx.job)
    if args.artifact_id not in {file['id'] for file in files}:
        raise ToolException(
            '成果 ID 不在本工作清單中，未讀取。請使用以下真實 ID，勿抄寫檔名或猜測：'
            + json.dumps([{'id': file['id'], 'name': file['name']} for file in files], ensure_ascii=False),
        )
    result = await inspect_artifact(ctx, args)
    runtime.stream_writer({'stage': 'tool', 'tool': 'inspect_artifact'})
    return Command(update={'messages': [tool_receipt(runtime.tool_call_id, 'inspect_artifact', result)]})


def inspection_tool():
    return injected_tool(
        'inspect_artifact',
        InspectArtifact,
        '分頁讀取本工作產出草稿及 SHA256；參數只有 artifact_id、start、limit、sheet。XLSX start 從0算，不能使用 read_spreadsheet 的 start_row/row_limit。不可讀其他工作或使用者的產出。用來交付前校核，不是批准存檔。',
        inspect,
        input_name='InspectArtifactInput',
        handle_tool_error=True,
    )
