"""Bounded transient state plus a durable, content-free write journal."""
from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import time
from contextlib import asynccontextmanager, AsyncExitStack
from contextvars import ContextVar

import psycopg
from dataclasses import dataclass
from dataclasses import field
from typing import Literal
from typing import TypedDict

from .database import configuration
from .database import connection
from .migrations import migrate
from examples.shared.runtime.workers import LeaseBusy
from examples.shared.runtime.concurrency import configured_limit
from examples.shared.runtime.concurrency import OwnerSlots
from examples.official_document_workflow.infrastructure.eip.netask import Netask

_calendar_lock = ContextVar('eip_calendar_lock', default=None)


class ImportReceipt(TypedDict):
    event_id: str
    detail_verified: Literal[True]


def link_expiration():
    return time.time() + 7200


@dataclass
class Link:
    adapter: Netask
    account: str
    expires: float = field(default_factory=link_expiration)
    generation: str = field(default_factory=secrets.token_urlsafe)
    documents: dict = field(default_factory=dict)
    attachments: dict = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    binding_generation: str | None = None


class State:
    def __init__(self):
        self.links: dict[str, Link] = {}
        self.previews: dict[str, dict] = {}
        self.jobs: dict[str, dict] = {}
        self.tasks: set[asyncio.Task] = set()
        self.automatic_tasks: dict[str, asyncio.Task] = {}
        self.processing = OwnerSlots(configured_limit('EIP_MAX_CONCURRENT_JOBS', 4))
        self.login_attempts: dict[str, list[float]] = {}
        # Fixed-size striped locks serialize per-user credential/session changes.
        self.session_locks = [asyncio.Lock() for _ in range(64)]

    def session_lock(self, owner):
        return self.session_locks[int.from_bytes(hashlib.sha256(owner.encode()).digest()[:2], 'big') % 64]

    def link(self, owner: str) -> Link:
        link = self.links.get(owner)
        if not link or link.expires <= time.time():
            raise ValueError('請先連結 EIP 帳號；Session 過期或服務重啟後需重新登入。')
        return link

    def owned(self, collection: dict, key: str, owner: str) -> dict:
        item = collection.get(key)
        if not item or item['owner'] != owner or item['expires'] <= time.time():
            raise ValueError('資料不存在、已過期，或不屬於目前帳號。')
        return item

    async def unlink(self, owner: str, *, expired_only=False):
        link = self.links.get(owner)
        if link:
            async with link.lock:
                if self.links.get(owner) is not link:
                    return
                if expired_only and link.expires > time.time():
                    return
                if self.links.get(owner) is link:
                    del self.links[owner]
                    await link.adapter.close()
        self.previews = {k: v for k, v in self.previews.items() if v['owner'] != owner}

    async def sweep(self):
        now = time.time()
        for owner, link in list(self.links.items()):
            if link.expires <= now:
                await self.unlink(owner, expired_only=True)
        for collection in (self.previews, self.jobs):
            for key, item in list(collection.items()):
                if item['expires'] <= now and item.get('status') not in {'queued', 'running'}:
                    collection.pop(key, None)
        cutoff = now - 300
        for owner, attempts in list(self.login_attempts.items()):
            attempts[:] = [attempt for attempt in attempts if attempt > cutoff]
            if not attempts:
                del self.login_attempts[owner]

    def allow_login(self, owner: str):
        attempts = self.login_attempts.setdefault(owner, [])
        now = time.time()
        attempts[:] = [attempt for attempt in attempts if attempt > now - 300]
        if len(attempts) >= 5 or len(self.login_attempts) > 256:
            raise ValueError('登入嘗試太頻繁，請五分鐘後再試。')
        attempts.append(now)


class Journal:
    """Reserve before network I/O. Unknown outcomes NEVER auto-retry.

    No names, credentials, document bodies, or calendar contents on disk.
    EIP identity + canonical event hash deduplicates across WebUI users/restarts.
    """

    def __init__(self, *, dsn=None, schema=None):
        self.dsn, self.schema = (dsn, schema) if dsn is not None and schema is not None else configuration()
        migrate(self.dsn, self.schema)

    def connect(self):
        return connection(self.dsn, self.schema)

    @asynccontextmanager
    async def scheduler_lock(self):
        """One recovery/dispatcher owner per database schema across applications."""
        async with await psycopg.AsyncConnection.connect(self.dsn, autocommit=True, connect_timeout=5,
                options='-c statement_timeout=5000') as db:
            cursor = await db.execute('SELECT pg_try_advisory_lock(hashtextextended(%s,0))',
                                      (self.schema + ':workflow_scheduler',))
            if not (await cursor.fetchone())[0]:
                raise LeaseBusy('此資料庫的 EIP 排程器已由其他程序執行。')
            yield db

    @asynccontextmanager
    async def calendar_lock(self):
        """Serialize discovery/create/share across accounts in this EIP store.

        Reentrant only for the same task, so the scheduler can hold the lock
        until newly created private events have been shared to their project.
        Connection close releases the lock on cancellation/process exit.
        """
        key = (hashlib.sha256(self.dsn.encode()).hexdigest(), self.schema, asyncio.current_task())
        if _calendar_lock.get() == key:
            yield
            return
        async with AsyncExitStack() as cleanup:
            db = await cleanup.enter_async_context(await psycopg.AsyncConnection.connect(
                self.dsn, autocommit=True, connect_timeout=5,
                options='-c statement_timeout=90000',
            ))
            await db.execute('SELECT pg_advisory_lock(hashtextextended(%s,0))', (self.schema+':visible_calendar_import',))
            token = _calendar_lock.set(key)
            cleanup.callback(_calendar_lock.reset, token)
            yield

    @staticmethod
    def key(user_id: str, row: dict) -> str:
        value = json.dumps([user_id, row], sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(value.encode()).hexdigest()

    def status(self, key: str) -> str | None:
        return self.statuses([key]).get(key)

    def statuses(self, keys):
        keys = list(dict.fromkeys(keys))
        if not keys:
            return {}
        with self.connect() as db:
            return dict(db.execute('SELECT key,status FROM writes WHERE key=ANY(%s)', (keys,)).fetchall())

    def reserve(self, key: str) -> bool:
        with self.connect() as db:
            return db.execute("INSERT INTO writes VALUES (%s, 'unknown', %s) ON CONFLICT DO NOTHING", (key, time.time())).rowcount == 1

    def source_manifest(self, key):
        with self.connect() as db:
            row = db.execute('SELECT manifest FROM source_manifests WHERE key=%s', (key,)).fetchone()
        return row[0] if row else None

    def reserve_source_batch(self, key, manifest):
        with self.connect() as db:
            db.execute('INSERT INTO source_manifests VALUES (%s,%s) ON CONFLICT DO NOTHING', (key, manifest))
            actual = db.execute('SELECT manifest FROM source_manifests WHERE key=%s FOR UPDATE', (key,)).fetchone()[0]
            if actual != manifest:
                raise ValueError('來源排程已由另一工作處理且內容不同，須先核對；未重送。')

    def reserve_event(self, key, aliases=()):
        keys = list(dict.fromkeys((key, *aliases)))
        now = time.time()
        with self.connect() as db:
            states = [r[0] for r in db.execute('SELECT status FROM writes WHERE key = ANY(%s) FOR UPDATE', (keys,))]
            if 'unknown' in states:
                return 'unknown'
            if 'done' in states:
                # Bind newly introduced source aliases to the legacy receipt
                # atomically, so removing an old CSV cannot reopen a done event.
                with db.cursor() as cursor:
                    cursor.executemany(
                        "INSERT INTO writes VALUES (%s, 'done', %s) ON CONFLICT DO NOTHING", [
                            (k, now) for k in keys
                        ],
                    )
                return 'done'
            with db.cursor() as cursor:
                cursor.executemany("INSERT INTO writes VALUES (%s, 'unknown', %s)", [(k, now) for k in keys])
            return 'reserved'

    def done(self, key: str, *, aliases=(), receipt: ImportReceipt | None = None):
        keys = list(dict.fromkeys((key, *aliases)))
        now = time.time()
        with self.connect() as db:
            with db.cursor() as cursor:
                cursor.executemany(
                    "UPDATE writes SET status='done', updated=%s WHERE key=%s",
                    ((now, item) for item in keys),
                )
                if receipt and receipt.get('event_id') and receipt.get('detail_verified') is True:
                    cursor.executemany(
                        'INSERT INTO event_receipts VALUES (%s,%s,%s) ON CONFLICT(key) DO UPDATE SET event_id=EXCLUDED.event_id,updated=EXCLUDED.updated',
                        ((item, receipt['event_id'], now) for item in keys),
                    )
