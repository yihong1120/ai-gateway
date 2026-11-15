"""Real PostgreSQL quota races across single and site batch stream writes."""

import asyncio
import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
import uuid

from fastapi import HTTPException
from sqlalchemy import func, insert, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateSchema, DropSchema

from examples.db_management.routers import streams as routes
from examples.db_management.schemas.stream_config import (
    SiteStreamConfigItem, SiteStreamConfigUpsert, StreamConfigCreate,
)
from examples.db_management.services import stream_config_services as services
from examples.identity.database import Base
from examples.identity.models import Group, Site, StreamConfig


def single(name, *, group=1, site=100):
    return StreamConfigCreate(site_id=site, group_id=group, stream_name=name, video_url='rtsp://synthetic/stream')


def batch(*items):
    return SiteStreamConfigUpsert(streams=[
        SiteStreamConfigItem(stream_name=item, video_url='rtsp://synthetic/stream')
        if isinstance(item, str) else item for item in items
    ])


class StreamWriteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        dsn = os.getenv('COMPANY_AGENT_TEST_DATABASE_URL')
        if not dsn:
            self.skipTest('Set COMPANY_AGENT_TEST_DATABASE_URL for PostgreSQL stream write tests')
        self.schema = 'stream_quota_test_' + uuid.uuid4().hex
        self.engine = create_async_engine(
            make_url(dsn).set(drivername='postgresql+asyncpg'),
            connect_args={'server_settings': {'search_path': self.schema, 'lock_timeout': '3000', 'statement_timeout': '5000'}},
        )
        self.addAsyncCleanup(self.engine.dispose)
        self.addAsyncCleanup(self.drop_schema)
        async with self.engine.begin() as connection:
            await connection.execute(CreateSchema(self.schema))
            await connection.run_sync(lambda db: Base.metadata.create_all(db, tables=[
                Group.__table__, Site.__table__, StreamConfig.__table__,
            ]))
            await connection.execute(insert(Group), [
                {'id': 1, 'name': 'first', 'uniform_number': '00000001', 'max_allowed_streams': 1},
                {'id': 2, 'name': 'second', 'uniform_number': '00000002', 'max_allowed_streams': 1},
            ])
            await connection.execute(insert(Site), [{'id': 100, 'name': 'shared'}, {'id': 200, 'name': 'other'}])
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def drop_schema(self):
        async with self.engine.begin() as connection:
            await connection.execute(DropSchema(self.schema, cascade=True, if_exists=True))

    async def wait_for_database_lock(self, pid):
        async with self.engine.connect() as observer:
            for _ in range(100):
                blocked = await observer.scalar(text('SELECT cardinality(pg_blocking_pids(:pid)) > 0'), {'pid': pid})
                if blocked:
                    return
                await asyncio.sleep(.01)
        self.fail('Competing writer did not wait for a real PostgreSQL row lock')

    async def race(self, first_write, second_write):
        async with self.sessions() as first, self.sessions() as second:
            # Force overlap without mocking COUNT or writes. The old single
            # path reads its stale count then blocks on the FK; the fixed path
            # waits for the Group lock before reading the count.
            await first.execute(select(Group.id).where(Group.id == 1).with_for_update())
            pid = await second.scalar(text('SELECT pg_backend_pid()'))
            contender = asyncio.create_task(second_write(second))
            try:
                await self.wait_for_database_lock(pid)
                await first_write(first)
                with self.assertRaises(HTTPException) as failure:
                    await contender
                self.assertEqual((failure.exception.status_code, failure.exception.detail),
                                 (403, 'Stream limit reached for group.'))
            finally:
                await first.rollback()
                if not contender.done():
                    contender.cancel()
                await asyncio.gather(contender, return_exceptions=True)
        async with self.sessions() as db:
            self.assertEqual(await services.get_group_stream_limit(1, db), (1, 1))

    async def test_concurrent_single_creates_cannot_overbook_one_group(self):
        await self.race(
            lambda db: services.create_stream_config(single('first'), db),
            lambda db: services.create_stream_config(single('second', site=200), db),
        )

    async def test_single_and_batch_share_the_same_capacity_lock(self):
        await self.race(
            lambda db: services.create_stream_config(single('first'), db),
            lambda db: services.upsert_site_stream_configs(200, batch('second'), db, group_id=1, visible_group_id=1),
        )

    async def test_batch_then_single_cannot_use_a_stale_prelock_count(self):
        await self.race(
            lambda db: services.upsert_site_stream_configs(100, batch('first'), db, group_id=1, visible_group_id=1),
            lambda db: services.create_stream_config(single('second', site=200), db),
        )

    async def test_unrelated_group_write_finishes_while_another_group_is_locked(self):
        async with self.sessions() as held, self.sessions() as independent:
            await held.execute(select(Group.id).where(Group.id == 1).with_for_update())
            try:
                result = await asyncio.wait_for(services.create_stream_config(single('independent', group=2), independent), 1)
                self.assertIsNotNone(result.id)
            finally:
                await held.rollback()
        async with self.sessions() as db:
            self.assertEqual(await services.get_group_stream_limit(2, db), (1, 1))

    async def seed_existing(self):
        async with self.engine.begin() as connection:
            await connection.execute(text('UPDATE group_info SET max_allowed_streams=4'))
        async with self.sessions() as db:
            one = await services.create_stream_config(single('existing-one'), db)
            two = await services.create_stream_config(single('existing-two', group=2), db)
            return one.id, two.id

    async def test_cross_group_batches_take_ordered_locks_without_deadlock(self):
        one, two = await self.seed_existing()
        start = asyncio.Barrier(2)

        async def write(group, order):
            async with self.sessions() as db:
                await db.scalar(text('SELECT pg_backend_pid()'))
                await start.wait()
                items = [SiteStreamConfigItem(id=identifier, stream_name=name,
                         video_url=f'rtsp://synthetic/writer-{group}') for identifier, name in order]
                await services.upsert_site_stream_configs(
                    100, batch(*items, f'new-{group}'), db, group_id=group, visible_group_id=None,
                )
        await asyncio.wait_for(asyncio.gather(
            write(1, [(two, 'existing-two'), (one, 'existing-one')]),
            write(2, [(one, 'existing-one'), (two, 'existing-two')]),
        ), 3)
        async with self.sessions() as db:
            self.assertEqual(await services.get_group_stream_limit(1, db), (2, 4))
            self.assertEqual(await services.get_group_stream_limit(2, db), (2, 4))

    async def test_batch_constraint_failure_rolls_back_updates_additions_and_lock(self):
        one, _ = await self.seed_existing()
        async with self.engine.begin() as connection:
            await connection.execute(text("ALTER TABLE stream_configs ADD CONSTRAINT reject_synthetic_stream CHECK (stream_name <> 'rejected-by-database')"))
        async with self.sessions() as db:
            with self.assertRaises(HTTPException) as failure:
                await services.upsert_site_stream_configs(100, batch(
                    SiteStreamConfigItem(id=one, stream_name='existing-one', video_url='rtsp://changed'),
                    'first-new', 'rejected-by-database',
                ), db, group_id=1, visible_group_id=1)
            self.assertEqual(failure.exception.status_code, 409)
            self.assertFalse(db.in_transaction())
        async with self.sessions() as db:
            self.assertEqual(await db.scalar(select(StreamConfig.video_url).where(StreamConfig.id == one)), 'rtsp://synthetic/stream')
            self.assertEqual(await db.scalar(select(func.count()).select_from(StreamConfig)), 2)
            await services.create_stream_config(single('first-new'), db)

    async def test_single_constraint_failure_leaves_no_capacity_or_held_lock(self):
        await self.seed_existing()
        async with self.sessions() as db:
            with self.assertRaises(HTTPException) as failure:
                await services.create_stream_config(single('existing-two'), db)
            self.assertEqual(failure.exception.status_code, 500)
            self.assertFalse(db.in_transaction())
        async with self.sessions() as db:
            self.assertEqual(await services.get_group_stream_limit(1, db), (1, 4))
            await services.create_stream_config(single('after-failure'), db)

    async def test_batch_cannot_edit_another_groups_existing_item(self):
        _, two = await self.seed_existing()
        async with self.sessions() as db:
            with self.assertRaises(HTTPException) as failure:
                await services.upsert_site_stream_configs(100, batch(
                    SiteStreamConfigItem(id=two, stream_name='existing-two', video_url='rtsp://changed'),
                ), db, group_id=1, visible_group_id=1)
            self.assertEqual((failure.exception.status_code, failure.exception.detail), (404, 'Stream configuration not found.'))
        async with self.sessions() as db:
            self.assertEqual(await db.scalar(select(StreamConfig.video_url).where(StreamConfig.id == two)), 'rtsp://synthetic/stream')


class StreamRouteScopeTests(unittest.IsolatedAsyncioTestCase):
    async def test_single_resolves_authorised_group_before_delegating(self):
        site = SimpleNamespace(groups=[SimpleNamespace(id=1), SimpleNamespace(id=2)])
        administrator = SimpleNamespace(username='group-admin', role='admin', group_id=1)
        with patch.object(routes, '_get_site_or_404', AsyncMock(return_value=site)), \
                patch.object(routes, 'create_stream_config', AsyncMock(return_value=SimpleNamespace(id=7))) as create:
            result = await routes.endpoint_create_stream_config(single('camera', group=2), 'db', administrator)
        self.assertEqual(result, {'id': 7, 'message': 'Stream configuration created successfully.'})
        self.assertEqual(create.call_args.args[0].group_id, 1)

    async def test_bulk_passes_only_the_callers_existing_scope(self):
        site = SimpleNamespace(groups=[SimpleNamespace(id=1), SimpleNamespace(id=2)])
        administrator = SimpleNamespace(username='group-admin', role='admin', group_id=1)
        with patch.object(routes, '_get_site_or_404', AsyncMock(return_value=site)), \
                patch.object(routes, 'upsert_site_stream_configs', AsyncMock()) as write, \
                patch.object(routes, '_list_site_stream_config_reads', AsyncMock(return_value=[])):
            self.assertEqual(await routes.endpoint_put_site_stream_config(100, batch('camera'), 'db', administrator), [])
        self.assertEqual(write.call_args.kwargs, {'group_id': 1, 'visible_group_id': 1})

    async def test_out_of_scope_site_never_reaches_a_writer(self):
        site = SimpleNamespace(groups=[SimpleNamespace(id=2)])
        administrator = SimpleNamespace(username='group-admin', role='admin', group_id=1)
        with patch.object(routes, '_get_site_or_404', AsyncMock(return_value=site)), \
                patch.object(routes, 'create_stream_config', AsyncMock()) as create:
            with self.assertRaises(HTTPException) as failure:
                await routes.endpoint_create_stream_config(single('camera'), 'db', administrator)
        self.assertEqual(failure.exception.status_code, 403)
        create.assert_not_awaited()
