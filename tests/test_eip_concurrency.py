"""Concurrent hourly accounts with actual PostgreSQL claims, no remote writes."""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from official_document_workflow.application import automation
from official_document_workflow.infrastructure.concurrency import OwnerSlots
from official_document_workflow.infrastructure.storage.automation_store import AutomationStore
from official_document_workflow.infrastructure.storage.state import Journal
from official_document_workflow.infrastructure.storage.state import State
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
        with patch.object(automation, 'store', return_value=self.db), patch.object(automation, 'STATE', self.state), patch.object(automation, 'run_account', side_effect=run):
            task = asyncio.create_task(automation.scheduler(self.journal))
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
        with patch.object(automation, 'store', return_value=self.db), patch.object(automation, 'STATE', self.state), patch.object(automation, 'run_account', side_effect=run):
            task = asyncio.create_task(automation.scheduler(self.journal))
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
