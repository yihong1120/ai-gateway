"""Bind server-discovered JSON schemas to the generic knowledge-agent boundary."""
from __future__ import annotations

import asyncio

from langchain_core.tools import StructuredTool
from langchain_core.tools import ToolException
from langgraph.prebuilt import ToolRuntime

from company_agent.runtime.models import UserBilledModel
from company_agent.runtime.tools import tool_receipt
from company_agent.services.external_tools.client import ExternalToolUnavailable


async def external_tool(runtime: ToolRuntime, *, specification, **kwargs):
    ctx = runtime.context
    if ctx.role != 'knowledge' or ctx.external_tools is None:
        raise ValueError('外部工具僅供已連接的 knowledge 角色使用。')
    if not isinstance(ctx.model, UserBilledModel) or ctx.model.owner != ctx.owner:
        raise ValueError('外部工具需要已驗證的個人身分。')
    if specification.server not in ctx.settings.mcp_servers:
        raise ValueError('外部工具不在目前工作允許的服務清單內。')
    runtime.stream_writer({'stage': 'tool', 'tool': specification.runtime_name})
    try:
        result = await ctx.external_tools.invoke(ctx.owner, ctx.model.subject, specification, kwargs)
    except ExternalToolUnavailable as failure:
        raise ToolException(str(failure)) from None
    receipt = {**result, 'source': specification.server.url}
    await asyncio.to_thread(ctx.store.audit, ctx.owner, ctx.job, specification.runtime_name, 'done')
    return tool_receipt(runtime.tool_call_id, specification.runtime_name, receipt)


def external_agent_tools(specs):
    return [
        StructuredTool.from_function(
            coroutine=ExternalToolBinding(spec).invoke,
            name=spec.runtime_name,
            description=spec.description,
            args_schema=spec.input_schema,
            handle_tool_error=True,
        )
        for spec in specs
    ]


class ExternalToolBinding:
    """Keep the discovered contract outside all model-controlled arguments."""

    def __init__(self, specification):
        self.specification = specification

    async def invoke(self, runtime: ToolRuntime, **kwargs):
        if 'specification' in kwargs:
            raise ToolException('外部工具參數含保留欄位。')
        return await external_tool(runtime, specification=self.specification, **kwargs)
