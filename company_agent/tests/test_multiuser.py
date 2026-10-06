"""Real PostgreSQL dispatch under multiuser admission, failure and shutdown."""
from __future__ import annotations

import asyncio
import threading
import unittest
from contextlib import contextmanager
from unittest.mock import AsyncMock
from unittest.mock import patch

import psycopg

from company_agent.api.queue import WorkQueue
from company_agent.api.service import Service
from company_agent.tests.test_agent import Fixture


class MultiuserTests(unittest.IsolatedAsyncioTestCase, Fixture):
    def setUp(self):
        super().setUp()
        self.queue = WorkQueue(self.private, self.base / 'uploads')

    def submit(self, owner, number=0):
        return self.queue.submit(
            owner, 'subject-' + owner, 'chat', str(number),
            f'work {owner} {number}', [],
        )

    async def wait_for(self, predicate):
        async with asyncio.timeout(10):
            while not predicate():
                await asyncio.sleep(.01)

    async def stop(self, service):
        service.task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await service.task
        self.assertFalse(service.executions)
        with self.queue.store.mutex('worker'):
            pass

    async def test_four_owners_parallel_one_per_owner_and_cancel_does_not_block_peers(self):
        owners = ['alice', 'bob', 'carol', 'david', 'emma']
        jobs = {(owner, n): self.submit(owner, n) for owner in owners for n in range(2)}
        started, active, peaks = [], set(), []
        release = {owner: asyncio.Event() for owner in owners}
        cleanup = asyncio.Event()
        finish_cleanup = asyncio.Event()
        service = Service(self.queue, None, None, None, max_concurrent=4)

        async def execute(row):
            owner = row['owner']
            self.assertNotIn(owner, active)
            active.add(owner)
            started.append((owner, row['id']))
            peaks.append(len(active))
            try:
                await release[owner].wait()
                self.queue.update(row['id'], status='done')
            finally:
                if owner == 'alice' and row['id'] == jobs[('alice', 0)]:
                    cleanup.set()
                    await finish_cleanup.wait()
                active.remove(owner)

        with patch.object(service, 'execute', side_effect=execute):
            service.task = asyncio.create_task(service.loop())
            try:
                await self.wait_for(lambda: len(started) == 4)
                self.assertEqual({owner for owner, _ in started}, set(owners[:4]))
                self.assertEqual(self.queue.get('alice', jobs[('alice', 1)])['status'], 'queued')
                cancellation = asyncio.create_task(service.cancel('alice', jobs[('alice', 0)]))
                await asyncio.wait_for(cleanup.wait(), 5)
                release['bob'].set()
                await self.wait_for(lambda: 'emma' in active)
                # New owner is served before Bob's backlog. Alice's cleanup keeps
                # only her slot; other owners continue while it is blocked.
                self.assertEqual(started[4][0], 'emma')
                self.assertFalse(cancellation.done())
                finish_cleanup.set()
                await cancellation
                self.assertEqual(self.queue.get('alice', jobs[('alice', 0)])['status'], 'cancelled')
                for event in release.values():
                    event.set()
                await self.wait_for(
                    lambda: all(
                        self.queue.get(owner, key)['status'] in {'done', 'cancelled'}
                        for (owner, _), key in jobs.items()
                    ) and not service.executions,
                )
                self.assertEqual(max(peaks), 4)
                self.assertEqual(len(started), 10)
                with self.assertRaises(ValueError):
                    self.queue.get('bob', jobs[('alice', 1)])
            finally:
                finish_cleanup.set()
                for event in release.values():
                    event.set()
                await self.stop(service)

    async def test_failure_isolated_without_retry_of_other_running_owner(self):
        bad = self.submit('alice')
        good = self.submit('bob')
        calls = []
        both_started = asyncio.Event()
        service = Service(self.queue, None, None, None, max_concurrent=2)

        async def execute(row):
            calls.append(row['id'])
            if len(calls) == 2:
                both_started.set()
            await both_started.wait()
            if row['id'] == bad:
                raise RuntimeError('native private exception')
            await asyncio.sleep(.03)
            self.queue.update(row['id'], status='done')

        with patch.object(service, 'execute', side_effect=execute):
            service.task = asyncio.create_task(service.loop())
            try:
                await self.wait_for(lambda: self.queue.get('bob', good)['status'] == 'done' and not service.executions)
                self.assertEqual(self.queue.get('alice', bad)['status'], 'error')
                self.assertCountEqual(calls, [bad, good])
                with self.assertRaisesRegex(RuntimeError, 'native private'):
                    service.result('alice', bad)
                self.assertFalse(service.task.done())
            finally:
                await self.stop(service)

    async def test_cancel_between_claim_and_registration_never_calls_identity(self):
        key = self.submit('alice')
        row = self.queue.claim()
        self.queue.request_cancel('alice', key)
        identity = AsyncMock()
        service = Service(self.queue, identity, None, None)
        await service._run_claimed(row)
        identity.assert_not_awaited()
        self.assertEqual(self.queue.get('alice', key)['status'], 'cancelled')

    async def test_cancel_before_coroutine_starts_releases_slot_and_persists_stop(self):
        key = self.submit('alice')
        row = self.queue.claim()
        service = Service(self.queue, None, None, None)
        task = asyncio.create_task(service._run_claimed(row))
        service.executions[('alice', key)] = task
        # Task is cancelled before entering its own exception handler.
        task.cancel()
        await service.cancel('alice', key)
        self.assertEqual(self.queue.get('alice', key)['status'], 'cancelled')

    async def test_stop_on_completed_job_during_client_cleanup_preserves_result(self):
        key = self.submit('alice')
        self.queue.claim()
        self.queue.update(key, status='done')
        task = asyncio.create_task(asyncio.Event().wait())
        service = Service(self.queue, None, None, None)
        service.executions[('alice', key)] = task
        try:
            await service.cancel('alice', key)
            self.assertFalse(task.cancelling())
            self.assertEqual(self.queue.get('alice', key)['status'], 'done')
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_lease_loss_drains_all_workers_then_recovery_interrupts_without_replay(self):
        jobs = {owner: self.submit(owner) for owner in ['alice', 'bob', 'carol']}
        begun, cleaned = set(), set()
        failed = threading.Event()
        original = self.queue.store.mutex

        @contextmanager
        def mutex(key):
            with original(key) as connection:
                class Lease:
                    def execute(self, *args):
                        if failed.is_set():
                            raise psycopg.OperationalError('simulated lost lease')
                        return connection.execute(*args)
                yield Lease()

        service = Service(self.queue, None, None, None, max_concurrent=3)

        async def execute(row):
            begun.add(row['owner'])
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(.01)
                cleaned.add(row['owner'])

        with patch.object(self.queue.store, 'mutex', side_effect=mutex), patch.object(service, 'execute', side_effect=execute):
            task = asyncio.create_task(service.work())
            await self.wait_for(lambda: len(begun) == 3)
            failed.set()
            with self.assertRaises(psycopg.OperationalError):
                await asyncio.wait_for(task, 5)
        self.assertEqual(cleaned, set(jobs))
        self.assertFalse(service.executions)
        self.queue.recover_running()
        for owner, key in jobs.items():
            self.assertEqual(self.queue.get(owner, key)['status'], 'interrupted')
        self.assertIsNone(self.queue.claim())
        with self.queue.store.mutex('worker'):
            pass

    async def test_owner_and_global_queue_caps_preserve_idempotent_request(self):
        self.queue.max_pending = 3
        self.queue.max_per_user = 2
        first = self.submit('alice')
        self.submit('alice', 1)
        with self.assertRaises(ValueError):
            self.submit('alice', 2)
        third = self.submit('bob')
        with self.assertRaises(ValueError):
            self.submit('carol')
        self.assertEqual(self.submit('alice'), first)
        self.queue.request_cancel('bob', third)
        self.submit('carol')
        self.assertEqual(len(self.queue.list('carol')), 1)

    async def test_async_following_turn_waits_for_prior_owner_and_reads_finished_artifact(self):
        prior = self.submit('alice')
        following = self.queue.submit(
            'alice', 'subject-alice', 'chat', 'next', 'read finished output', [], [prior],
            allow_pending_references=True,
        )
        self.assertEqual(self.queue.claim()['id'], prior)
        self.assertIsNone(self.queue.claim())
        engine = self.store.job('alice')
        artifact = self.store.artifact('alice', engine, 'finished.txt', b'finished')
        self.store.finish(
            'alice', engine, 'done', {
                'answer': 'verified prior answer',
                'web_sources': [{'url': 'https://example.com/evidence'}],
            },
        )
        self.queue.update(prior, status='done', engine_job=engine)
        row = self.queue.claim()
        self.assertEqual(row['id'], following)
        service = Service(self.queue, None, None, None)
        sources = service._prepare_sources(row, self.config)
        self.assertEqual(sources['history_1'][0]['display_name'], 'finished.txt')
        self.assertEqual(sources['previous_results'][0]['answer'], 'verified prior answer')
        self.assertEqual(sources['previous_results'][0]['web_sources'], [{'url': 'https://example.com/evidence'}])
        self.assertEqual((self.queue.sources/prior/(artifact['id']+'.txt')).read_bytes(), b'finished')

    async def test_forty_users_eighty_jobs_bounded_parallel_and_no_duplicate_claim(self):
        jobs = [(f'user-{n}', self.submit(f'user-{n}', turn)) for n in range(40) for turn in range(2)]
        active, served, peaks = set(), [], []
        service = Service(self.queue, None, None, None, max_concurrent=4)

        async def execute(row):
            owner = row['owner']
            self.assertNotIn(owner, active)
            active.add(owner)
            served.append((owner, row['id']))
            peaks.append(len(active))
            try:
                await asyncio.sleep(.04)
                self.queue.update(row['id'], status='done')
            finally:
                active.remove(owner)
        with patch.object(service, 'execute', side_effect=execute):
            service.task = asyncio.create_task(service.loop())
            try:
                await self.wait_for(lambda: len(served) == 80 and not service.executions)
                self.assertEqual(max(peaks), 4)
                self.assertEqual(len({key for _, key in served}), 80)
                self.assertEqual(len({owner for owner, _ in served[:40]}), 40)
                for owner, key in jobs:
                    self.assertEqual(self.queue.get(owner, key)['status'], 'done')
            finally:
                await self.stop(service)
