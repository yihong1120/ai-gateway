"""One authenticated hourly run; failed operations propagate without replay."""
from __future__ import annotations

from contextlib import AsyncExitStack, asynccontextmanager

from examples.shared.runtime.io import run_sync

from . import calendar_attachments, calendar_projects


class AutomaticSession:
    def __init__(self, store, owner, generation, factory, verify):
        self.store, self.owner, self.generation = store, owner, generation
        self.factory, self.verify = factory, verify
        self.adapter = None
        self.usable = False

    @property
    def user_id(self):
        return self.adapter.user_id if self.adapter else ''

    async def check_active(self):
        if not await run_sync(self.store.active,self.owner, self.generation):
            self.usable = False
            raise ValueError('自動處理已暫停或綁定變更，未繼續連線。')
        if not await self.verify(self.owner):
            self.usable = False
            await run_sync(self.store.pause,self.owner)
            raise ValueError('Open WebUI 帳號已停用，已停止 EIP 自動處理。')

    async def login(self):
        self.usable = False
        await self.check_active()
        async with AsyncExitStack() as cleanup:
            secret = await run_sync(self.store.account,self.owner, secrets=True)
            if secret is not None:
                cleanup.callback(secret.clear)
            if not secret or secret['generation'] != self.generation:
                raise ValueError('EIP 綁定已變更，請重新操作。')
            candidate = self.factory()
            cleanup.push_async_callback(candidate.close)
            await candidate.login(secret['account'], secret['password'])
            secret.pop('password', None)
            await self.check_active()
            if candidate.user_id != secret['identity']:
                raise ValueError('登入後 EIP 身分與綁定不符，已停止自動處理。')
            old = self.adapter
            self.adapter = candidate
            secret.clear()
            cleanup.pop_all()
            if old is not None:
                await old.close()
            self.usable = True

    async def read(self, method, *args):
        if method not in {'automatic_documents', 'document_detail', 'attachments', 'download', 'stage', 'calendar_projects', 'calendar_import_matches'}:
            raise ValueError('此操作不允許自動重送。')
        if not self.usable:
            raise ValueError('本輪 EIP 連線已停止；不再重試。')
        await self.check_active()
        self.usable = False
        result = await getattr(self.adapter, method)(*args)
        self.usable = True
        return result

    @asynccontextmanager
    async def writing(self, action):
        """Check authorization once; a failed write must never refresh or replay."""
        if not self.usable:
            raise ValueError(f'本輪 EIP 連線已停止，未繼續{action}。')
        await self.check_active()
        self.usable = False
        yield
        self.usable = True

    async def commit(self, line):
        async with self.writing('寫入行事曆'):
            return await self.adapter.commit(line)

    async def calendar_import_matches(self, lines):
        return await self.read('calendar_import_matches', lines)

    async def begin_calendar_creation(self, line, key):
        begin = getattr(self.adapter, 'begin_calendar_creation', None)
        if begin is not None:
            await self.check_active()
            await begin(line, key)

    async def record_calendar_creation(self, line, key, receipt, *, aliases=()):
        record = getattr(self.adapter, 'record_calendar_creation', None)
        if record is not None:
            await self.check_active()
            await record(line, key, receipt, aliases=aliases)

    async def reconcile_calendar(self, journal, *, allowed=object, keys=None):
        reconcile = getattr(self.adapter, 'reconcile_calendar', None)
        if reconcile is None:
            return {'checked': 0, 'removed': 0, 'needs_review': 0}
        if not self.usable:
            raise ValueError('本輪 EIP 連線已停止，未清理行程。')
        await self.check_active()
        self.usable = False
        result = await reconcile(journal, allowed=allowed, authorize=self.check_active, keys=keys)
        self.usable = True
        return result

    async def attach_calendar_files(self, event_id, line, files, *, project_id='', sharing=None):
        """A post-import write: no automatic session refresh or replay."""
        async with self.writing('夾帶行事曆附件'):
            return await calendar_attachments.attach_event(self.adapter, event_id, line, files, project_id=project_id, sharing=sharing)

    async def assign_calendar_project(self, event_id, line, project_id, *, sharing=None):
        """A project-only post-import write: never refresh/replay on expiry."""
        async with self.writing('設定行事曆專案'):
            return await calendar_projects.assign_project(self.adapter, event_id, line, project_id, sharing=sharing)

    async def close(self):
        self.usable = False
        if self.adapter is not None:
            adapter, self.adapter = self.adapter, None
            await adapter.close()
