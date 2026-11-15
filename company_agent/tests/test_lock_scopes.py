"""Unrelated jobs progress while per-job mutations and global limits serialize."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event
import time

from company_agent.tests.test_agent import Fixture


class MutationLockTests(Fixture):
    def test_another_job_can_write_while_the_first_job_is_locked(self):
        other = self.store.job('bob')
        holding, release = Event(), Event()

        def hold():
            with self.store.db() as db:
                self.store.lock(db, owner='alice', job=self.job)
                holding.set()
                if not release.wait(5):
                    raise AssertionError('test holder timed out')

        with ThreadPoolExecutor(max_workers=3) as pool:
            first = pool.submit(hold)
            self.assertTrue(holding.wait(2))
            try:
                # Different owners and jobs must not wait for the held transaction.
                result = pool.submit(
                    self.store.artifact, 'bob', other, 'other.txt', b'other'
                ).result(timeout=2)
                self.assertEqual(result['name'], 'other.txt')
                waiting = pool.submit(
                    self.store.artifact, 'alice', self.job, 'same.txt', b'same'
                )
                time.sleep(0.05)
                self.assertFalse(waiting.done())
            finally:
                release.set()
            first.result(timeout=2)
            self.assertEqual(waiting.result(timeout=2)['name'], 'same.txt')
        metrics = self.store.lock_metrics()['job']
        self.assertGreaterEqual(metrics['acquisitions'], 3)
        self.assertGreater(metrics['max_seconds'], 0.04)
        metrics['acquisitions'] = 0
        self.assertGreater(self.store.lock_metrics()['job']['acquisitions'], 0)

    def test_global_queue_lock_still_serializes_global_decisions(self):
        holding, release, attempting, acquired = Event(), Event(), Event(), Event()

        def hold():
            with self.store.db() as db:
                self.store.lock(db)
                holding.set()
                release.wait(5)

        def wait():
            with self.store.db() as db:
                attempting.set()
                self.store.lock(db)
                acquired.set()

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(hold)
            self.assertTrue(holding.wait(2))
            second = pool.submit(wait)
            try:
                self.assertTrue(attempting.wait(2))
                self.assertFalse(acquired.wait(0.05))
            finally:
                release.set()
            first.result(timeout=2)
            second.result(timeout=2)
        self.assertEqual(self.store.lock_metrics()['global']['acquisitions'], 2)
