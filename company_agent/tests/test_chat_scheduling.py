"""Durable delivery fairness, retry revisions and execution isolation."""

from __future__ import annotations

import asyncio
import time
import unittest
from unittest.mock import patch

from company_agent.api.queue import WorkQueue
from company_agent.api.service import Service
from company_agent.tests.test_agent import Fixture


class ChatSchedulingTests(unittest.IsolatedAsyncioTestCase, Fixture):
    def setUp(self):
        super().setUp()
        self.queue = WorkQueue(self.private, self.base / 'uploads')

    def submit(self, owner, number):
        return self.queue.submit(owner, 'subject', 'chat', str(number), '處理文件', [])

    async def test_failures_defer_only_the_failed_revision_and_new_progress_is_immediate(self):
        key = self.submit('alice', 1)
        row = self.queue.chat_pending()[0]
        self.queue.chat_failed(key, row['revision'])
        self.assertEqual(self.queue.chat_pending(), [])
        with patch('company_agent.api.queue.time.time', return_value=time.time() + 3):
            self.assertEqual(self.queue.chat_pending()[0]['id'], key)
        self.queue.update(key, progress='新進度')
        self.queue.chat_failed(key, row['revision'])
        current = self.queue.chat_pending()[0]
        self.assertGreater(current['revision'], row['revision'])
        with self.store.db() as db:
            self.assertEqual(db.execute('SELECT chat_failures FROM web_jobs WHERE id=%s', (key,)).fetchone(), (0,))

    async def test_one_owner_cannot_fill_the_delivery_batch_before_another_owners_first_message(self):
        for number in range(10):
            self.submit('alice', number)
        bob = self.submit('bob', 1)
        self.assertIn(bob, [row['id'] for row in self.queue.chat_pending(limit=2)])

    async def test_deferred_prefix_cannot_starve_later_results_after_restart(self):
        for number in range(32):
            self.submit(f'owner-{number}', number)
        for row in self.queue.chat_pending():
            self.queue.chat_failed(row['id'], row['revision'])
        later = self.submit('bob', 1)
        restored = WorkQueue(self.private, self.base / 'uploads')
        self.assertEqual([row['id'] for row in restored.chat_pending()], [later])

    async def test_blocked_publisher_does_not_block_job_dispatch(self):
        key = self.submit('alice', 1)
        publishing, executing = asyncio.Event(), asyncio.Event()

        async def publish(*args):
            publishing.set()
            await asyncio.Event().wait()

        async def execute(row):
            executing.set()
            await asyncio.Event().wait()

        service = Service(self.queue, None, None, None, chat_publisher=publish)
        service.execute = execute
        task = asyncio.create_task(service.work())
        try:
            await asyncio.wait_for(publishing.wait(), 2)
            await asyncio.wait_for(executing.wait(), 2)
            self.assertEqual(self.queue.get('alice', key)['status'], 'running')
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
