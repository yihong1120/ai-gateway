from __future__ import annotations

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from langchain.agents.middleware.model_call_limit import ModelCallLimitExceededError
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from company_agent.api.service import WorkQueue
from company_agent.runtime.storage import Store
from company_agent.tests.postgres_fixture import checkpointer
from company_agent.tests.test_agent import Fixture


class PostgreSQLTests(Fixture):
    def test_no_database_url_fails_without_sqlite(self):
        with patch.dict(os.environ, {'COMPANY_AGENT_DATABASE_URL': ''}):
            with self.assertRaises(ValueError):
                Store(self.private)
        self.assertFalse((self.private/'agent.sqlite3').exists())

    def test_invalid_schema_cannot_change_search_path(self):
        with patch.dict(os.environ, {'COMPANY_AGENT_DB_SCHEMA': 'public,company_agent'}):
            with self.assertRaises(ValueError):
                Store(self.private)

    def test_failed_transaction_rolls_back(self):
        with self.assertRaises(RuntimeError):
            with self.store.db() as db:
                db.execute("UPDATE jobs SET status='bad' WHERE id=%s", (self.job,))
                raise RuntimeError('rollback')
        self.assertEqual(self.store.result('alice', self.job)['status'], 'running')

    def test_database_mutex_excludes_other_connection_and_releases(self):
        with self.store.mutex('worker'):
            with self.assertRaises(ValueError):
                with self.store.mutex('worker'):
                    pass
        with self.store.mutex('worker'):
            pass

    def test_parallel_model_attempts_never_exceed_limit(self):
        def reserve(_):
            try:
                return self.store.reserve_model_attempt('alice', self.job, 3)
            except ModelCallLimitExceededError:
                return None
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(reserve, range(8)))
        self.assertEqual(sorted(v for v in results if v is not None), [1, 2, 3])
        self.assertEqual(self.store.attempts(self.job), 3)

    def test_parallel_queue_submit_and_claim_are_unique(self):
        queue = WorkQueue(self.private, self.base/'uploads')
        def submit(_): return queue.submit('alice', 'sub', 'chat', 'message', 'hi', [])
        with ThreadPoolExecutor(max_workers=5) as pool:
            keys = list(pool.map(submit, range(5)))
        self.assertEqual(len(set(keys)), 1)
        with ThreadPoolExecutor(max_workers=5) as pool:
            claims = list(pool.map(lambda _: queue.claim(), range(5)))
        self.assertEqual(sum(r is not None for r in claims), 1)

    def test_official_postgres_checkpoint_and_restart(self):
        async def run():
            async with checkpointer(self.store) as saver:
                self.assertIsInstance(saver, AsyncPostgresSaver)
                cp = empty_checkpoint()
                cp['channel_values'] = {'canary': 'private-postgres-canary'}
                cp['channel_versions'] = {'canary': '1'}
                config = await saver.aput({'configurable': {'thread_id': self.job, 'checkpoint_ns': ''}}, cp, {'source': 'input', 'step': 0, 'parents': {}}, {'canary': '1'})
            async with checkpointer(Store(self.private)) as saver:
                result = await saver.aget_tuple(config)
                self.assertEqual(result.checkpoint['channel_values']['canary'], 'private-postgres-canary')
        asyncio.run(run())
        self.assertFalse(list(self.private.glob('*.sqlite*')))
