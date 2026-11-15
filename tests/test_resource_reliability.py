"""Resource snapshots, retrying workers and independently owned shared pools."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from examples.shared.runtime.database import (
    PoolRegistry,
    PoolScope,
    connection,
    DatabasePoolMiddleware,
)
from examples.identity.config import Settings
from examples.identity.runtime import IdentityResources
from examples.identity import lifespan as identity_lifecycle
from examples.db_management import lifespan as lifecycle


class SnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_resource_options_use_the_original_settings_snapshot(self):
        original = {
            'JWT_SECRET_KEY': 'original',
            'PYTHON_DOTENV_DISABLED': '1',
            'BFF_SESSION_TTL_SECONDS': '1234',
            'MEDIA_SESSION_TTL_SECONDS': '700',
            'REDIS_PASSWORD': 'a@b/c',
            'DB_POOL_SIZE': '3',
            'PLAYBACK_UPSTREAM_TIMEOUT_SECONDS': '17',
            'BFF_CHAT_API_URL': 'http://original.test',
            'BFF_UPSTREAM_TIMEOUT_SECONDS': '19',
        }
        with patch.dict(os.environ, original, clear=True):
            settings = Settings()
        with patch.dict(
            os.environ,
            {'DB_POOL_SIZE': '9', 'BFF_CHAT_API_URL': 'http://changed.test'},
            clear=True,
        ):
            resources = IdentityResources(settings)
            engine = Mock(dispose=AsyncMock())
            with patch(
                'examples.identity.database.create_async_engine', return_value=engine
            ) as create:
                resources.database.start()
        self.addAsyncCleanup(resources.database.close)
        self.addAsyncCleanup(resources.http_clients.close)
        self.assertEqual(create.call_args.kwargs['pool_size'], 3)
        self.assertEqual(resources.session_ttl_seconds, 1234)
        self.assertEqual(resources.media_ttl_seconds, 700)
        self.assertIn('a%40b%2Fc@', resources.redis_url)
        self.assertEqual(resources.bff.upstreams['chat'], 'http://original.test')
        self.assertEqual(
            (await resources.http_clients.get('streaming-playback')).timeout.read, 17
        )
        self.assertEqual(
            (await resources.http_clients.get('bff-upstream')).timeout.read, 19
        )

    async def test_pool_middleware_allows_lifespan_to_create_its_owner(self):
        app = AsyncMock()
        middleware = DatabasePoolMiddleware(
            app, SimpleNamespace(state=SimpleNamespace())
        )
        receive, send = AsyncMock(), AsyncMock()
        await middleware({'type': 'lifespan'}, receive, send)
        app.assert_awaited_once()

    async def test_cleanup_retries_after_first_failure_and_resets_backoff(self):
        stop, waits = asyncio.Event(), []

        @asynccontextmanager
        async def session():
            yield object()

        async def wait(awaitable, *, timeout):
            awaitable.close()
            waits.append(timeout)
            if len(waits) == 2:
                stop.set()
            raise TimeoutError

        drain = AsyncMock(side_effect=[RuntimeError('database temporarily down'), 0])
        with (
            patch.object(lifecycle, 'drain_site_media_cleanup_jobs', drain),
            patch.object(lifecycle.asyncio, 'wait_for', side_effect=wait),
        ):
            await lifecycle._run_site_media_cleanup_worker(
                stop, SimpleNamespace(session=session)
            )
        self.assertEqual(drain.await_count, 2)
        self.assertEqual(waits, [2, 30])

    async def test_lifespan_always_starts_worker_and_joins_it_before_database_close(
        self,
    ):
        entered, cancelled = asyncio.Event(), asyncio.Event()

        async def worker(stop, database):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        @asynccontextmanager
        async def account_session():
            yield object()
        database = Mock(start=Mock(), close=AsyncMock(), session=account_session)
        resources = SimpleNamespace(
            database=database,
            http_clients=Mock(close=AsyncMock()),
            redis_url='redis://unused',
            auto_create_schema=False,
            recovery=Mock(drain_revocations=AsyncMock()),
        )
        redis = Mock(connect=AsyncMock(return_value=object()), close=AsyncMock())
        with (
            patch.object(identity_lifecycle, 'require_schema', AsyncMock()),
            patch.object(identity_lifecycle, 'RedisClient', return_value=redis),
            patch.object(identity_lifecycle, 'start_jwt_scheduler', return_value=Mock()),
            patch.object(identity_lifecycle.rate_limiter_service, 'preload_script', AsyncMock()),
            patch.object(
                lifecycle, '_run_site_media_cleanup_worker', side_effect=worker
            ),
        ):
            async with lifecycle.global_lifespan(
                SimpleNamespace(state=SimpleNamespace(identity=resources))
            ):
                await asyncio.wait_for(entered.wait(), 2)
                database.close.assert_not_awaited()
        self.assertTrue(cancelled.is_set())
        database.close.assert_awaited_once()


class PoolOwnershipTests(unittest.TestCase):
    def test_shared_pool_survives_an_owner_close_and_active_borrow(self):
        registry = PoolRegistry()
        one, two = PoolScope(registry), PoolScope(registry)
        pool = Mock()
        with patch(
            'examples.shared.runtime.database.ConnectionPool', return_value=pool
        ) as create:
            one.retain('postgresql://localhost/test')
            two.retain('postgresql://localhost/test')
            create.assert_called_once()
            one.close()
            pool.close.assert_not_called()
            with registry.borrow('postgresql://localhost/test'):
                two.close()
                pool.close.assert_not_called()
            pool.close.assert_called_once()
            one.close()
            two.close()
        self.assertFalse(registry._entries)

    def test_independent_dsns_release_independently_and_binding_reaches_threads(self):
        registry = PoolRegistry()
        one, two = PoolScope(registry), PoolScope(registry)
        pools = [Mock(), Mock()]
        with patch('examples.shared.runtime.database.ConnectionPool', side_effect=pools):
            one.retain('postgresql://localhost/one')
            two.retain('postgresql://localhost/two')
        one.close()
        pools[0].close.assert_called_once()
        pools[1].close.assert_not_called()
        two.close()
        pools[1].close.assert_called_once()
        with self.assertRaises(RuntimeError):
            one.retain('postgresql://localhost/one')

    def test_unscoped_pool_is_released_on_transaction_failure(self):
        registry = PoolRegistry()
        pool = Mock()
        with patch('examples.shared.runtime.database.ConnectionPool', return_value=pool):
            with self.assertRaises(ValueError):
                with registry.borrow('postgresql://localhost/test'):
                    raise ValueError('transaction failed')
        pool.close.assert_called_once()
        self.assertFalse(registry._entries)

    def test_scoped_connections_restore_row_factory_and_release_once(self):
        registry, pool = PoolRegistry(), Mock()
        database = Mock(row_factory='old')
        pool.connection.return_value.__enter__ = Mock(return_value=database)
        pool.connection.return_value.__exit__ = Mock(return_value=False)
        scope = PoolScope(registry)
        with (
            patch('examples.shared.runtime.database.ConnectionPool', return_value=pool),
            scope.bind(),
        ):
            with self.assertRaises(ValueError):
                with connection(
                    'postgresql://localhost/test', 'tenant', row_factory='new'
                ) as db:
                    self.assertEqual(db.row_factory, 'new')
                    raise ValueError('rollback')
            self.assertEqual(database.row_factory, 'old')
            pool.close.assert_not_called()
        scope.close()
        pool.close.assert_called_once()
