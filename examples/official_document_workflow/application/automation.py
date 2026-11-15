"""Bounded parallel hourly accounts; consent is persisted, not inferred."""
from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from functools import partial

from open_webui.models.users import Users

from .automatic_task import AutomaticTask
from examples.official_document_workflow.infrastructure.eip.automatic_session import (
    AutomaticSession,
)
from examples.official_document_workflow.infrastructure.eip.netask import Netask
from examples.shared.runtime.io import run_sync


async def verified_owner(owner):
    user = await Users.get_user_by_id(owner)
    return user is not None and user.role in {'user', 'admin'}


class AutomaticAccount:
    def __init__(self, db, journal, slots, *, factory=Netask, verify=verified_owner):
        self.db, self.journal, self.slots = db, journal, slots
        self.factory, self.verify = factory, verify

    async def run(self, owner, generation):
        db, journal, factory, verify = self.db, self.journal, self.factory, self.verify
        completed = 0
        allowed = partial(db.active, owner, generation)
        async with AsyncExitStack() as cleanup:
            if not await run_sync(allowed):
                return
            if not await verify(owner):
                await run_sync(db.pause, owner)
                return
            config = await run_sync(db.account, owner)
            adapter = AutomaticSession(db, owner, generation, factory, verify)
            cleanup.push_async_callback(adapter.close)
            await adapter.login()
            if not await run_sync(allowed):
                return
            supersession = await adapter.reconcile_calendar(journal, allowed=allowed)
            documents = await adapter.read('automatic_documents', config['scan_start'])
            current = {doc['id']: doc for doc in documents}
            await run_sync(db.discover, owner, adapter.user_id, documents)
            for task in await run_sync(db.tasks, owner, adapter.user_id, pending=True):
                if not await run_sync(allowed):
                    break
                payload = task['payload']
                processor = AutomaticTask(db, journal, owner, task, adapter, config, allowed, verify, self.slots)
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
                    await run_sync(db.save_task, task, 'outside_scope', payload)
                    continue
                task_run = asyncio.create_task(processor.run())
                try:
                    completed_task = await task_run
                except BaseException:
                    await run_sync(processor.finished, task_run)
                    raise
                if completed_task:
                    completed += 1
                if processor.stopped:
                    break
            message = f"本輪完成 {completed} 份。下次每小時檢查其他尚未失敗的文件。"
            if supersession['removed']:
                message += f"已保留人工行程並清理 {supersession['removed']} 筆 AI 複本。"
            if supersession['needs_review']:
                message += f"另有 {supersession['needs_review']} 筆行程清理待核對。"
            await run_sync(
                db.finish,
                owner,
                generation,
                message,
                failed=not adapter.usable,
            )


class AutomaticScheduler:
    def __init__(self, db, journal, state, health, account=None):
        self.db, self.journal, self.state, self.health = db, journal, state, health
        self.account = account if account is not None else AutomaticAccount(db, journal, state.processing)

    async def _run_due(self, account):
        try:
            await self.account.run(account['owner'], account['generation'])
        except Exception as failure:
            await run_sync(
                self.db.finish, account['owner'], account['generation'],
                f'本輪已中止（{type(failure).__name__}）；失敗文件不自動重試。', failed=True,
            )

    async def run(self):
        db, journal, running = self.db, self.journal, self.state.automatic_tasks
        if running:
            raise ValueError('自動排程器已啟動。')
        async with journal.scheduler_lock() as lease:
            await run_sync(db.recover)
            try:
                while True:
                    await lease.execute('SELECT 1')
                    for owner, task in list(running.items()):
                        if task.done():
                            del running[owner]
                            task.result()
                    available = self.state.processing.limit - len(running)
                    if available:
                        for account in await run_sync(db.claim_due, limit=available, excluded_owners=list(running)):
                            owner = account['owner']
                            if owner in running:
                                raise ValueError('重複的自動排程帳號。')
                            running[owner] = asyncio.create_task(self._run_due(account))
                    self.health.beat()
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
