"""Owned HTTP resources and import-time ORM initialization boundaries."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from examples.shared.runtime.http_clients import HttpClientPool
from examples.identity.database import Database


class ResourceTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_clients_are_reused_and_closed_once(self):
        client = AsyncMock()
        pool = HttpClientPool()
        with patch('examples.shared.runtime.http_clients.httpx.AsyncClient', return_value=client) as factory:
            first = await pool.get('oidc', timeout=10)
            self.assertIs(first, await pool.get('oidc', timeout=10))
            factory.assert_called_once()
        await pool.close()
        await pool.close()
        client.aclose.assert_awaited_once()
        with self.assertRaises(RuntimeError):
            await pool.get('oidc')

    async def test_http_cleanup_attempts_every_client_even_if_one_fails(self):
        failed = Mock(aclose=AsyncMock(side_effect=RuntimeError('close failed')))
        healthy = Mock(aclose=AsyncMock())
        pool = HttpClientPool()
        with patch('examples.shared.runtime.http_clients.httpx.AsyncClient', side_effect=[failed, healthy]):
            await pool.get('first')
            await pool.get('second')
        with self.assertRaises(ExceptionGroup):
            await pool.close()
        failed.aclose.assert_awaited_once()
        healthy.aclose.assert_awaited_once()

    async def test_failed_database_start_also_closes_the_pool(self):
        from examples.identity.lifespan import global_lifespan
        database = Mock(start=Mock(side_effect=RuntimeError('database failed')), close=AsyncMock())
        pool = HttpClientPool()
        resources = SimpleNamespace(database=database, http_clients=pool)
        with self.assertRaisesRegex(RuntimeError, 'database failed'):
            async with global_lifespan(SimpleNamespace(state=SimpleNamespace(identity=resources))):
                self.fail('unexpected successful startup')
        database.close.assert_awaited_once()
        with self.assertRaises(RuntimeError):
            await pool.get('closed')

    async def test_database_closes_the_engine_owned_by_that_instance(self):
        database = Database()
        engine = Mock(dispose=AsyncMock())
        database.engine, database.sessions = engine, Mock()
        await database.close()
        await database.close()
        engine.dispose.assert_awaited_once()
        self.assertIsNone(database.engine)
        self.assertIsNone(database.sessions)
        with self.assertRaises(RuntimeError):
            database.session()

    def test_models_register_without_creating_database_or_loading_a_host_package(self):
        code = """
import sys
from examples.identity.database import Database
from examples.identity.models import Base
from sqlalchemy.orm import configure_mappers
configure_mappers()
assert Database().engine is None
assert len(Base.metadata.tables) == 24
assert 'account_action_tokens' in Base.metadata.tables
assert not any(name == 'src' or name.startswith('src.') for name in sys.modules)
"""
        result = subprocess.run(
            [sys.executable, '-c', code],
            capture_output=True,
            text=True,
            env={**os.environ, 'DATABASE_URL': 'must-not-be-used-at-import'},
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    async def test_partial_startup_closes_resources_before_propagating_failure(self):
        redis = Mock(connect=AsyncMock(side_effect=RuntimeError('redis unavailable')), close=AsyncMock())
        scheduler = Mock()
        modules = {}
        for name, attributes in {
            'examples.identity.cache': {'rate_limiter_service': Mock()},
            'examples.identity.sessions.scheduler': {'start_jwt_scheduler': Mock(return_value=scheduler)},
            'examples.identity.redis_pool': {'RedisClient': Mock(return_value=redis)},
            'examples.db_management.services.site_media_cleanup': {
                'drain_site_media_cleanup_jobs': AsyncMock()
            },
        }.items():
            module = ModuleType(name)
            vars(module).update(attributes)
            modules[name] = module
        path = Path(__file__).resolve().parents[1] / 'examples/identity/lifespan.py'
        spec = importlib.util.spec_from_file_location('identity_lifespan_test', path)
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, modules):
            spec.loader.exec_module(module)
        database = Mock(start=Mock(), close=AsyncMock())
        with self.assertRaisesRegex(RuntimeError, 'redis unavailable'):
            async with module.global_lifespan(SimpleNamespace(state=SimpleNamespace(identity=SimpleNamespace(
                    database=database, http_clients=HttpClientPool(), redis_url='redis://localhost', auto_create_schema=False
                )))):
                self.fail('startup should fail before accepting requests')
        database.close.assert_awaited_once()
        redis.close.assert_awaited_once()
        scheduler.shutdown.assert_called_once()
