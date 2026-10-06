"""Thin official tool adapters over business services."""
from __future__ import annotations

from langgraph.prebuilt import ToolRuntime
from langgraph.types import Command

from company_agent.runtime.tools import injected_tool
from company_agent.runtime.tools import tool_receipt
from company_agent.services.research import web
from company_agent.services.research.schemas import FetchURL
from company_agent.services.research.schemas import SearchWeb


async def research_tool(runtime: ToolRuntime, *, tool_name, input_schema, **kwargs):
    name, schema = tool_name, input_schema
    ctx = runtime.context
    if not ctx.settings.enable_web_research:
        raise ValueError('此入口尚未啟用網路研究，未送出請求。')
    config = await web.web_config(ctx.owner)  # recheck even for cached calls
    args = schema.model_validate(kwargs).model_dump()
    result = ctx.store.begin_tool(
        ctx.owner,
        ctx.job,
        runtime.tool_call_id,
        name,
        args,
    )
    if result is None:
        runtime.stream_writer({'stage': 'tool', 'tool': name})
        result = await web.perform(name, args, config)
        ctx.store.finish_tool(ctx.owner, ctx.job, runtime.tool_call_id, result)
    ctx.store.audit(ctx.owner, ctx.job, name, 'done')
    sources = [
        {k: v for k, v in row.items() if k != 'content'}
        for row in result['results']
    ]
    return Command(
        update={
            'web_sources': sources,
            'messages': [tool_receipt(runtime.tool_call_id, name, result)],
        },
    )


def research_tools():
    return [
        injected_tool(name, schema, description, research_tool, tool_name=name, input_schema=schema)
        for name, schema, description in (
            (
                'search_web',
                SearchWeb,
                '以 Tavily 基本搜尋查詢公開網路，最多五筆含來源網址的摘要；不代表已讀全文。',
            ),
            (
                'fetch_url',
                FetchURL,
                '用 Tavily 擷取一個公開網頁的文字，最多 12000 字元；不登入、不抓內網。',
            ),
        )
    ]
