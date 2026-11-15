"""Open manual EIP sessions from a saved binding; failed requests propagate."""
from __future__ import annotations

import time
from contextlib import asynccontextmanager
from contextlib import AsyncExitStack

from .netask import Netask
from examples.official_document_workflow.infrastructure.storage.state import Link
from examples.shared.runtime.io import run_sync


class ManualSession:
    def __init__(self, owner, state, store, factory):
        self.owner, self.state, self.store, self.factory = owner, state, store, factory
        self.link = state.links.get(owner)

    async def refresh(self):
        config = await run_sync(self.store.account, self.owner)
        if not config:
            raise ValueError('沒有已保存的 EIP 憑證。請重新綁定；僅短期登入者過期後仍需輸入帳密。')
        adapter = self.factory()
        async with AsyncExitStack() as cleanup:
            cleanup.push_async_callback(adapter.close)
            secret = await run_sync(self.store.account, self.owner, secrets=True)
            if secret is not None:
                cleanup.callback(secret.clear)
            if not secret or secret['generation'] != config['generation']:
                raise ValueError('EIP 綁定已變更，請重新操作。')
            await adapter.login(secret['account'], secret['password'])
            secret.pop('password', None)
            latest = await run_sync(self.store.account, self.owner)
            if not latest or latest['generation'] != config['generation']:
                raise ValueError('EIP 綁定已變更，已取消重新連線。')
            if adapter.user_id != config['identity']:
                raise ValueError('EIP 登入身分與綁定不符，請重新綁定正確帳號。')
            old = self.link
            if old is not None and old.binding_generation == config['generation'] and old.adapter.user_id == adapter.user_id:
                previous = old.adapter
                old.adapter = adapter
                old.expires = time.time()+7200
                await previous.close()
            else:
                if old is not None:
                    await old.adapter.close()
                self.link = Link(adapter, secret['account'], binding_generation=config['generation'])
                self.state.previews = {k: v for k, v in self.state.previews.items() if v['owner'] != self.owner}
            self.state.links[self.owner] = self.link
            secret.clear()
            cleanup.pop_all()

    async def read(self, method, *args):
        if method not in {'documents', 'document_detail', 'attachments', 'download', 'stage', 'calendar', 'calendar_detail', 'calendar_import_matches'}:
            raise ValueError('此操作不允許自動重送。')
        expires = self.link.expires
        self.link.expires = 0
        result = await getattr(self.link.adapter, method)(*args)
        self.link.expires = expires
        return result


@asynccontextmanager
async def manual_session(owner, state, store, *, factory=Netask, force=False):
    async with state.session_lock(owner), AsyncExitStack() as stack:
        session = ManualSession(owner, state, store, factory)
        original = session.link
        if original:
            await stack.enter_async_context(original.lock)
        config = await run_sync(store.account, owner)
        changed = original and original.binding_generation is not None and (
            not config or original.binding_generation != config['generation']
        )
        if force or changed or original is None or original.expires <= time.time():
            await session.refresh()
        if session.link is not original:
            await stack.enter_async_context(session.link.lock)
        yield session
