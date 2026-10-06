"""Bounded parallel hourly accounts; consent is persisted, not inferred."""
from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from functools import partial

from open_webui.models.users import Users

from .automatic_task import AutomaticTask
from official_document_workflow.infrastructure.eip.automatic_session import (
    AutomaticSession,
)
from official_document_workflow.infrastructure.eip.netask import Netask
from official_document_workflow.infrastructure.storage.automation_store import store
from official_document_workflow.infrastructure.storage.state import STATE


async def verified_owner(owner):
    user = await Users.get_user_by_id(owner)
    return user is not None and user.role in {'user', 'admin'}


async def run_account(db, journal, owner, generation, *, factory=Netask, verify=verified_owner):
    completed = 0
    allowed = partial(db.active, owner, generation)
    async with AsyncExitStack() as cleanup:
        if not allowed():
            return
        if not await verify(owner):
            db.pause(owner)
            return
        config = db.account(owner)
        adapter = AutomaticSession(db, owner, generation, factory, verify)
        cleanup.push_async_callback(adapter.close)
        await adapter.login()
        if not allowed():
            return
        documents = await adapter.read('automatic_documents', config['scan_start'])
        current = {doc['id']: doc for doc in documents}
        db.discover(owner, adapter.user_id, documents)
        for task in db.tasks(owner, adapter.user_id, pending=True):
            if not allowed():
                break
            payload = task['payload']
            processor = AutomaticTask(db, journal, owner, task, adapter, config, allowed, verify)
            # Never grant the all-date exception from a stale queued inbox row.
            # Only this round's complete snapshot establishes current inbox membership.
            doc = current.get(task['document_id'])
            eligible = (
                Netask.automatic_eligible(doc, config['scan_start'])
                if doc is not None
                else Netask.received_since(payload['document'], config['scan_start'])
            )
            if not eligible:
                payload['notice'] = '文件匣紀錄時間早於固定抓取起點，未自動處理；需要時請手動選取。'
                db.save_task(task, 'outside_scope', payload)
                continue
            task_run = asyncio.create_task(processor.run())
            task_run.add_done_callback(processor.finished)
            if await task_run:
                completed += 1
            if processor.stopped:
                break
        db.finish(
            owner,
            generation,
            f"本輪完成 {completed} 份。下次每小時檢查其他尚未失敗的文件。",
            failed=not adapter.usable,
        )


async def _run_due(db, journal, account):
    try:
        await run_account(db, journal, account['owner'], account['generation'])
    except Exception as failure:
        db.finish(
            account['owner'], account['generation'],
            f"本輪已中止（{type(failure).__name__}）；失敗文件不自動重試。",
            failed=True,
        )


async def scheduler(journal):
    running = STATE.automatic_tasks
    if running:
        raise ValueError('自動排程器已啟動。')
    db = store()
    db.recover()
    try:
        while True:
            for owner, task in list(running.items()):
                if task.done():
                    del running[owner]
                    task.result()
            available = STATE.processing.limit - len(running)
            if available:
                for account in db.claim_due(limit=available, excluded_owners=list(running)):
                    owner = account['owner']
                    if owner in running:
                        raise ValueError('重複的自動排程帳號。')
                    running[owner] = asyncio.create_task(_run_due(db, journal, account))
            if running:
                await asyncio.wait(
                    set(running.values()), timeout=15,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                await asyncio.sleep(0)
            else:
                await asyncio.sleep(15)
    finally:
        for task in running.values():
            if not task.done() and not task.cancelling():
                task.cancel()
        if running:
            await asyncio.gather(*running.values(), return_exceptions=True)
        running.clear()
