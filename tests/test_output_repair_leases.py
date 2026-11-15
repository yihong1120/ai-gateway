"""Slow reads renew leases; no receipt waits for a processing slot after claim."""
from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from examples.official_document_workflow.application import output_receipts
from examples.official_document_workflow.application.output_receipts import MemoryOutputReceipts
from examples.official_document_workflow.application.output_verification import FileVerification
from examples.official_document_workflow.services.output_repair import OutputRepair


class RepairLeaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_batch_claims_only_free_slots_and_renews_active_reads(self):
        clock = [1000.0]
        loop = asyncio.get_running_loop()
        started = asyncio.Queue()
        release = {str(i): asyncio.Event() for i in range(12)}
        renewed = {key: asyncio.Event() for key in release}

        class ShortLeases(MemoryOutputReceipts):
            def __init__(self):
                super().__init__()
                self.renewals = 0
                self.renewals_by_id = dict.fromkeys(release, 0)
                self.claim_limits = []
                self.initial_until = {}
                self.max_leased = 0

            def claim_reviews(self, limit=32):
                with self._lock:
                    self.claim_limits.append(limit)
                    claims = super().claim_reviews(limit)
                    for claim in claims:
                        self.initial_until[claim.record.id] = clock[0] + 0.1
                        self._reviews[claim.record.id]['until'] = self.initial_until[claim.record.id]
                    self.max_leased = max(
                        self.max_leased,
                        sum(r['token'] is not None for r in self._reviews.values()),
                    )
                return claims

            def renew_review(self, claim):
                with self._lock:
                    valid = super().renew_review(claim)
                    if valid:
                        self.renewals += 1
                        self.renewals_by_id[claim.record.id] += 1
                        if self.renewals_by_id[claim.record.id] == 2:
                            loop.call_soon_threadsafe(renewed[claim.record.id].set)
                return valid

        repo = ShortLeases()

        async def inspect(record):
            started.put_nowait(record.id)
            await release[record.id].wait()
            return FileVerification('verified')

        files = SimpleNamespace(inspect=inspect)
        repair = OutputRepair(repo, files)
        repair.heartbeat_seconds = 0
        # Only the adapter's clock is replaced; asyncio still uses its real
        # monotonic clock. Gates, rather than elapsed sleeps, control each wave.
        with patch.object(output_receipts, 'time', SimpleNamespace(time=lambda: clock[0])):
            for key in release:
                record, _ = repo.begin(key, 'owner', 'file.pdf', 'hash', 'source')
                repo._reviews[record.id]['at'] = 0
            task = asyncio.create_task(repair.run_once())
            inspected = []
            try:
                # A watchdog catches missing renewals without leaving workers
                # running; this timeout does not determine lease validity.
                async with asyncio.timeout(10):
                    for _ in range(3):
                        active = [await started.get() for _ in range(4)]
                        inspected.extend(active)
                        self.assertEqual(len(set(active)), 4)
                        await asyncio.gather(*(renewed[key].wait() for key in active))
                        clock[0] += 0.13
                        with repo._lock:
                            self.assertEqual(sum(r['token'] is not None for r in repo._reviews.values()), 4)
                            for key in active:
                                self.assertLess(repo.initial_until[key], clock[0])
                                self.assertGreater(repo._reviews[key]['until'], clock[0])
                        for key in active:
                            release[key].set()
                    self.assertEqual(await task, 12)
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assertCountEqual(inspected, release)
        self.assertTrue(all(limit == 1 for limit in repo.claim_limits))
        self.assertGreater(repo.renewals, 12)
        self.assertLessEqual(repo.max_leased, 4)
        self.assertTrue(
            all(repo.get(str(i), 'owner').state == 'stored' for i in range(12)),
        )

    async def test_deadline_records_unavailable_and_leaves_a_retry(self):
        repo = MemoryOutputReceipts()
        record, _ = repo.begin('file', 'owner', 'file.pdf', 'hash', 'source')
        repo._reviews[record.id]['at'] = 0

        async def blocked(record):
            await asyncio.Event().wait()

        repair = OutputRepair(repo, SimpleNamespace(inspect=blocked))
        repair.timeout_seconds = 0.01
        self.assertEqual(await repair.run_once(), 1)
        self.assertEqual(repo._reviews['file']['outcome'], 'unavailable')
        self.assertEqual(repo._reviews['file']['reason'], 'verification_timeout')
        self.assertEqual(await repair.run_once(), 0)

    async def test_shutdown_cancels_and_drains_a_running_review(self):
        repo = MemoryOutputReceipts()
        record, _ = repo.begin('file', 'owner', 'file.pdf', 'hash', 'source')
        repo._reviews[record.id]['at'] = 0
        started, stopped = asyncio.Event(), asyncio.Event()

        async def inspect(record):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        repair = OutputRepair(repo, SimpleNamespace(inspect=inspect))
        task = asyncio.create_task(repair.run_once())
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(stopped.is_set())
        self.assertEqual(repo.get('file', 'owner').state, 'writing')
