"""Concurrent hourly accounts with actual PostgreSQL claims, no remote writes."""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from functools import partial
from types import SimpleNamespace
from examples.shared.runtime.workers import WorkerHealth
from unittest.mock import patch

from examples.official_document_workflow.application import automation
from examples.shared.runtime.concurrency import OwnerSlots
from examples.official_document_workflow.infrastructure.storage.automation_store import AutomationStore
from examples.official_document_workflow.infrastructure.storage.state import Journal
from examples.official_document_workflow.infrastructure.storage.state import State
from tests.eip_postgres import EIPPostgresTestCase


class ConcurrentAccountTests(EIPPostgresTestCase, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.db = AutomationStore(Path(self.temporary.name) / 'key')
        self.journal = Journal()
        self.state = State()
        self.state.processing = OwnerSlots(2)
        for owner in ['alice', 'bob', 'carol']:
            self.db.bind(owner, owner + '-identity', owner + '-account', 'test-password')
        with self.db.db() as db:
            db.execute('UPDATE accounts SET next_run=0')

    def scheduler(self, run, state=None):
        return automation.AutomaticScheduler(
            self.db, self.journal, state or self.state, WorkerHealth(),
            SimpleNamespace(run=partial(run, self.db, self.journal)),
        )

    async def wait_for(self, predicate):
        async with asyncio.timeout(5):
            while not predicate():
                await asyncio.sleep(.001)

    async def test_claim_due_bounds_batch_and_excludes_still_running_owner_after_hour(self):
        claimed = self.db.claim_due(limit=1)
        self.assertEqual(len(claimed), 1)
        first = claimed[0]['owner']
        with self.db.db() as db:
            db.execute('UPDATE accounts SET next_run=0 WHERE owner=%s', (first,))
        rest = self.db.claim_due(limit=2, excluded_owners=[first])
        self.assertEqual({row['owner'] for row in rest}, {'alice', 'bob', 'carol'} - {first})
        self.assertEqual([row['owner'] for row in self.db.claim_due(limit=2)], [first])
        self.assertEqual(self.db.claim_due(), [])

    async def test_slow_account_does_not_block_other_accounts_and_shutdown_drains(self):
        started, cleaned = [], []
        release = {owner: asyncio.Event() for owner in ['alice', 'bob', 'carol']}

        async def run(db, journal, owner, generation):
            self.assertEqual(db.account(owner)['generation'], generation)
            started.append(owner)
            try:
                await release[owner].wait()
            finally:
                cleaned.append(owner)
        task = asyncio.create_task(self.scheduler(run).run())
        try:
            await self.wait_for(lambda: len(started) == 2)
            self.assertEqual(started, ['alice', 'bob'])
            self.assertEqual(len(self.state.automatic_tasks), 2)
            release['bob'].set()
            await self.wait_for(lambda: 'carol' in started)
            self.assertNotIn('alice', cleaned)
            self.assertEqual(len(self.state.automatic_tasks), 2)
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertCountEqual(cleaned, ['alice', 'bob', 'carol'])
        self.assertFalse(self.state.automatic_tasks)
        self.assertEqual(self.db.claim_due(), [])  # Hourly tick already committed.

    async def test_one_account_failure_is_recorded_and_others_finish_once(self):
        calls = []

        async def run(db, journal, owner, generation):
            calls.append(owner)
            if owner == 'alice':
                raise ValueError('private account error')
            db.finish(owner, generation, 'completed')
        task = asyncio.create_task(self.scheduler(run).run())
        try:
            await self.wait_for(lambda: len(calls) == 3 and not self.state.automatic_tasks)
            self.assertFalse(task.done())
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertCountEqual(calls, ['alice', 'bob', 'carol'])
        alice = self.db.account('alice')
        self.assertEqual(alice['failures'], 1)
        self.assertTrue(alice['enabled'])
        self.assertNotIn('private', alice['message'])
        self.assertEqual(self.db.account('bob')['message'], 'completed')
        self.assertEqual(self.db.account('carol')['message'], 'completed')

    async def test_second_application_cannot_recover_work_while_dispatcher_lease_is_held(self):
        entered = asyncio.Event()
        async def run(*args):
            entered.set()
            await asyncio.Event().wait()
        with patch.object(self.db,'recover',wraps=self.db.recover) as recover:
            first = asyncio.create_task(self.scheduler(run).run())
            try:
                await asyncio.wait_for(entered.wait(),3)
                with self.assertRaises(RuntimeError):
                    await self.scheduler(run, State()).run()
                self.assertEqual(recover.call_count,1)
                self.assertFalse(first.done())
            finally:
                first.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await first


    async def test_capacity_profile_uses_owned_scheduler_without_legacy_patches(self):
        from company_agent.evals.capacity import profile

        result = await profile(limit=1, model_limit=2, ocr_limit=1, users=1, hold=0)
        self.assertEqual(result['jobs'], 3)
        self.assertEqual(result['errors'], 0)
        self.assertEqual(result['users'], 1)
        for lane in ('company', 'document', 'eip'):
            self.assertEqual(result['peaks'][lane], 1)
