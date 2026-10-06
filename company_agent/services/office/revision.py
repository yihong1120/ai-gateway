"""Receipt-backed private draft revision, independent of graph tool dispatch."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from langchain_core.messages import ToolMessage

from company_agent.services.files.operations import output_name
from company_agent.services.files.operations import worker


def require_inspected(messages, args):
    for message in messages:
        if (
            not isinstance(message, ToolMessage)
            or message.name != 'inspect_artifact'
            or message.status == 'error'
        ):
            continue
        value = json.loads(message.content)
        if value is None:
            continue
        if value.get('artifact_id') != args.artifact_id or value.get('sha256') != args.sha256:
            continue
        for block in value.get('blocks', []):
            if any(
                r.get('location') == args.location and r.get('text') == args.expected
                for r in block.get('runs', [])
            ):
                return
    raise ValueError(
        '須先用 inspect_artifact 實際讀到指定草稿版本與文字 run，不能猜測位置或原文。',
    )


async def revise(ctx, messages, call_id, args):
    name, sha, data = ctx.store.read_artifact(ctx.owner, args.artifact_id, ctx.job)
    if sha != args.sha256:
        raise ValueError('草稿版本不符，未修改。')
    suffix = Path(name).suffix.lower()
    if suffix not in {'.docx', '.pptx'}:
        raise ValueError('草稿文字修訂僅支援 DOCX／PPTX。')
    target = output_name(args.name, {suffix})
    require_inspected(messages, args)
    result = ctx.store.begin_tool(
        ctx.owner, ctx.job, call_id, 'edit_artifact_text_run', args.model_dump(),
    )
    if result is None:
        if any(
            f['name'].casefold() == target.casefold() for f in ctx.store.files(ctx.owner, ctx.job)
        ):
            raise ValueError('修訂草稿須使用不同的新檔名；既有成果不覆蓋。')
        changed = await asyncio.to_thread(
            worker,
            'edit',
            data=data,
            suffix=suffix,
            location=args.location,
            expected=args.expected,
            replacement=args.replacement,
        )
        await asyncio.to_thread(worker, 'extract', data=changed, suffix=suffix)
        result = {
            **ctx.store.artifact(ctx.owner, ctx.job, target, changed),
            'revises': {'artifact_id': args.artifact_id, 'sha256': sha},
            'notice': '指定文字 run 已另存新版；原草稿保留。新版須獨立審核，字長可能影響排版，未寫入 NAS。',
        }
        ctx.store.finish_tool(ctx.owner, ctx.job, call_id, result)
    ctx.store.audit(ctx.owner, ctx.job, 'edit_artifact_text_run', 'done')
    return result
