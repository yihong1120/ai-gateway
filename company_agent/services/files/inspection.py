"""Read and verify a private artifact without graph or prompt dependencies."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from company_agent.services.files import block_page
from company_agent.services.files.operations import worker


async def inspect_artifact(ctx, args):
    name, sha, data = await asyncio.to_thread(
        ctx.store.read_artifact, ctx.owner, args.artifact_id, ctx.job,
    )
    suffix = Path(name).suffix.lower()
    result = {'artifact_id': args.artifact_id, 'name': name, 'sha256': sha}
    if suffix == '.dxf':
        from company_agent.services.cad.sandbox import cad_worker

        if not ctx.settings.enable_cad:
            raise ValueError('此入口未啟用 CAD。')
        page = await asyncio.to_thread(
            cad_worker,
            'read',
            data=data,
            suffix='.dxf',
            view='entities',
            start=args.start,
            limit=min(args.limit, 10),
        )
        result.update({
            'cad': page,
            'notice': 'DXF 結構校核，不代表 CAD 視覺、字型、尺寸或工程安全通過驗證。',
        })
    elif suffix == '.xlsx':
        page = await asyncio.to_thread(
            worker,
            'read_spreadsheet',
            data=data,
            sheet=args.sheet,
            start_row=args.start + 1,
            row_limit=args.limit,
            column_limit=100,
        )
        result.update({
            'spreadsheet': page,
            'notice': 'XLSX start 為從零計數的列偏移；sheet 未指定只讀第一張。須依 next_row 及 sheets 繼續讀取；不計算公式。',
        })
    else:
        blocks = await asyncio.to_thread(
            worker, 'extract', data=data, suffix=suffix,
        )
        result.update({
            **block_page(blocks, args.start, args.limit),
            'notice': '僅讀取產出的文字，不代表視覺排版、公式或工程安全已通過驗證。',
        })
    if len(json.dumps(result, ensure_ascii=False)) > 18000:
        raise ValueError('產出閱讀區塊超過上限，請降低 limit；未截斷內容。')
    ctx.store.audit(ctx.owner, ctx.job, 'inspect_artifact', 'done')
    return result
