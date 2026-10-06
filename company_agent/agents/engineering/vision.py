"""Thin official tool adapters over business services."""
from __future__ import annotations

from langgraph.prebuilt import ToolRuntime
from langgraph.types import Command

from company_agent.runtime.tools import injected_tool
from company_agent.runtime.tools import tool_receipt
from company_agent.services.office import vision
from company_agent.services.office.schemas import UnderstandImage


async def understand_image(runtime: ToolRuntime, **kwargs):
    args = UnderstandImage.model_validate(kwargs)
    ctx = runtime.context
    runtime.stream_writer({'stage': 'tool', 'tool': 'understand_image'})
    result = await vision.understand(ctx, args)
    ctx.store.audit(ctx.owner, ctx.job, 'understand_image', 'done')
    return Command(
        update={
            'sources': [{k: result[k] for k in ('root', 'path', 'sha256')}],
            'accessed_roots': [args.root],
            'messages': [tool_receipt(runtime.tool_call_id, 'understand_image', result)],
        },
    )


def vision_tools():
    return [
        injected_tool(
            'understand_image',
            UnderstandImage,
            '以本人 LiteLLM 額度分析來源圖片或 PDF 指定單頁的語意、物件、版面及可見問題；每次 1 頁、每工作最多 6 種問題。同頁文字擷取優先本地 read_ocr；不聲稱整份讀完或工程安全驗證。',
            understand_image,
            input_name='UnderstandImageInput',
        ),
    ]
