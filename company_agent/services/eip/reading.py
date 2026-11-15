"""Read bounded EIP attachment text without generating Word/PDF/CSV artifacts."""
from __future__ import annotations

import asyncio
import hashlib
import json
from contextlib import ExitStack
from functools import lru_cache
from importlib.metadata import version
from pathlib import Path

from company_agent.services.files import block_page
from company_agent.services.eip.client import attachment_list
from company_agent.services.eip.client import binding
from company_agent.services.eip.client import source
from examples.official_document_workflow.runtime import current_resources
from examples.shared.runtime.concurrency import run_ocr


async def read_attachment(ctx, args):
    # Binding and source ownership/revision are checked even on cached pages.
    db = await binding(ctx)
    item = await asyncio.to_thread(source, ctx, args.handle, 'attachment')
    attachment = item['attachment']
    if Path(attachment['name']).suffix.lower() not in {'.pdf', '.docx', '.doc'}:
        raise ValueError('附件唯讀支援 PDF／DOCX／DOC。')
    if args.mode == 'ocr' and not ctx.settings.enable_ocr:
        raise ValueError('本地 OCR 未啟用。')
    key = (
        'eip_read:'
        + hashlib.sha256(json.dumps([args.handle, args.mode, args.page]).encode()).hexdigest()
    )
    cached = await asyncio.to_thread(
        ctx.store.begin_tool,
        ctx.owner,
        ctx.job,
        key,
        'eip_read_attachment',
        {'handle': args.handle, 'mode': args.mode, 'page': args.page},
        limit_names={'eip_read_attachment'},
        limit=24,
    )
    if cached is None:
        sha, data = await _snapshot(ctx, args.handle, item, db)
        cached = {
            'handle': args.handle,
            'name': attachment['name'],
            'sha256': sha,
            'mode': args.mode,
            'page': args.page if args.mode == 'ocr' else None,
            **await _content_blocks(data, args.mode, args.page),
        }
        await binding(ctx)
        await asyncio.to_thread(ctx.store.finish_tool, ctx.owner, ctx.job, key, cached)
    await binding(ctx)
    return {
        **cached,
        **block_page(cached['blocks'], args.start, args.limit),
        'scope': '本工作下載時的附件快照；不代表後續新版本。未產製文件或行事曆。',
    }


def _validate_blocks(value):
    if (
        not isinstance(value, dict) or set(value) != {'blocks', 'notice'}
        or not isinstance(value['notice'], str) or not isinstance(value['blocks'], list)
        or any(
            not isinstance(block, dict) or set(block) != {'location', 'text'}
            or not isinstance(block['location'], str) or not isinstance(block['text'], str)
            for block in value['blocks']
        )
        or len(json.dumps(value['blocks'], ensure_ascii=False)) > 1_000_000
    ):
        raise ValueError('附件文字快取格式或大小不符。')


@lru_cache(maxsize=2)
def _reader_identity(mode):
    # These workers live outside the workflow implementation revision. Bind
    # cached blocks to their installed public code, never account/config files.
    files = ('eip/reading.py',)
    if mode == 'ocr':
        files += ('office/ocr.py', 'office/sandbox.py', 'office/worker.py', 'sandbox.py')
    else:
        files += ('files/operations.py', 'office/parser_worker.py', 'office/documents.py')
    root = Path(__file__).resolve().parents[1]
    digest = hashlib.sha256()
    for relative in files:
        digest.update(relative.encode())
        digest.update(b'\0')
        digest.update((root / relative).read_bytes())
        digest.update(b'\0')
    return {'implementation': digest.hexdigest(), 'pymupdf': version('PyMuPDF')}


async def _content_blocks(data, mode, page):
    cache = await asyncio.to_thread(lambda: current_resources().shared_content)

    async def parse():
        return await (run_ocr if mode == 'ocr' else asyncio.to_thread)(_parse, data, mode, page)

    if cache is None:
        return await parse()
    # Hash only the authorized bytes, never a caller-provided digest or URL.
    settings = dict(await asyncio.to_thread(_reader_identity, mode))
    if mode == 'ocr':
        from company_agent.services.office.ocr import endpoint
        from company_agent.services.office.sandbox import IMAGE
        settings.update(endpoint=endpoint(), max_new_tokens=4096, office_image=IMAGE)
    key = cache.key(
        'eip-read-blocks-v1', hashlib.sha256(data).hexdigest(), mode,
        page if mode == 'ocr' else None, settings,
    )
    return await cache.aget_or_compute(key, parse, validate=_validate_blocks)


def _load_snapshot(ctx, handle):
    with ctx.store.db() as conn:
        return conn.execute(
            'SELECT sha,data FROM eip_read_inputs WHERE owner=%s AND job=%s AND handle=%s',
            (ctx.owner, ctx.job, handle),
        ).fetchone()


def _save_snapshot(ctx, handle, data):
    sha = hashlib.sha256(data).hexdigest()
    with ctx.store.db() as conn:
        ctx.store.lock(conn)
        size = conn.execute(
            'SELECT COALESCE(sum(octet_length(data)),0) FROM eip_read_inputs WHERE owner=%s AND job=%s',
            (ctx.owner, ctx.job),
        ).fetchone()[0]
        if size + len(data) > 50_000_000:
            raise ValueError('本工作唯讀附件合計超過 50 MB。')
        conn.execute(
            'INSERT INTO eip_read_inputs VALUES(%s,%s,%s,%s,%s)',
            (ctx.owner, ctx.job, handle, sha, data),
        )
    return sha, data


async def _snapshot(ctx, handle, item, db):
    """Load private PDF bytes only when a new text/OCR receipt is needed."""
    saved = await asyncio.to_thread(_load_snapshot, ctx, handle)
    if saved is not None:
        return saved
    data = await _download_pdf(ctx, item, db)
    await binding(ctx)
    return await asyncio.to_thread(_save_snapshot, ctx, handle, data)


async def _download_pdf(ctx, item, db):
    from examples.official_document_workflow.infrastructure.eip.sessions import manual_session
    resources = current_resources()
    state = resources.state
    from examples.official_document_workflow.infrastructure.office.office_input import to_pdf

    attachment = item['attachment']
    with ExitStack() as cleanup:
        async with manual_session(ctx.owner, state, db) as current:
            fresh = await attachment_list(current, item['doc'])
            if not any(
                r['url'] == attachment['url'] and r['name'] == attachment['name']
                for r in fresh
            ):
                raise ValueError('附件清單已變更；請重新查詢。')
            raw = cleanup.enter_context(await current.read('download', attachment['url'], attachment['name']))
        size = raw.seek(0, 2)
        raw.seek(0)
        if not size or size > ctx.settings.max_file_bytes:
            raise ValueError('附件為空或超過單檔上限。')
        content_cache = await asyncio.to_thread(lambda: resources.shared_content)
        converted, _ = await to_pdf(raw, attachment['name'], content_cache=content_cache)
        if converted is not raw:
            cleanup.enter_context(converted)
        data = await asyncio.to_thread(converted.read, ctx.settings.max_file_bytes + 1)
    if not data or len(data) > ctx.settings.max_file_bytes:
        raise ValueError('轉換後附件超過單檔上限。')
    return data


def _parse(data, mode, page):
    from company_agent.services.files.operations import worker
    from company_agent.services.office.ocr import ocr_page

    if mode == 'ocr':
        parsed = ocr_page(data, '.pdf', page)
        blocks, notice = parsed['blocks'], parsed['notice']
    else:
        blocks = worker('extract', data=data, suffix='.pdf')
        notice = 'PDF 文字層閱讀；空白頁可能是掃描影像，可明確使用 mode=ocr 指定 page。非圖面語意或版面驗證。'
    bounded = []
    for block in blocks:
        text = block['text']
        for offset in range(0, max(1, len(text)), 4000):
            bounded.append(
                {
                    'location': block['location'] + f"/offset:{offset}",
                    'text': text[offset: offset + 4000],
                },
            )
    if len(json.dumps(bounded, ensure_ascii=False)) > 1_000_000:
        raise ValueError('附件文字超過上限，未截斷宣稱已完整分析。')
    return {'blocks': bounded, 'notice': notice}


def read_results(receipts):
    """Combine attachment pages into a bounded excerpt for each source version."""
    found = {}
    for receipt in receipts:
        if not receipt or not receipt.get('sha256'):
            continue
        key = (receipt.get('handle'), receipt['sha256'])
        excerpt = found.get(key, {}).get('excerpt', '')
        text = '\n'.join(block.get('text', '') for block in receipt.get('blocks', []))
        found[key] = {
            **{name: receipt.get(name) for name in ('handle', 'name', 'sha256', 'mode', 'page')},
            'excerpt': (excerpt + text)[:1000],
        }
    return list(found.values())
