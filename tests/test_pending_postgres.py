"""Cross-worker continuation safety against a real isolated PostgreSQL schema."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest

from examples.official_document_workflow.application.pending import PendingClarificationScope
from examples.official_document_workflow.domain.documents import PendingCalendarReminder
from examples.official_document_workflow.domain.workflow import JobPlan
from examples.official_document_workflow.infrastructure.storage.automation_store import (
    AutomationStore,
)
from examples.official_document_workflow.infrastructure.storage.pending import (
    PostgresClarifications,
)
from tests.eip_postgres import EIPPostgresTestCase


class DurableClarificationTests(EIPPostgresTestCase, unittest.TestCase):
    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.key = Path(temporary.name) / 'secret'
        self.backend = AutomationStore(self.key)
        self.repo = PostgresClarifications(self.backend)
        self.scope = PendingClarificationScope('owner', 'chat')
        self.payload = PendingCalendarReminder(
            ({'Subject': 'private subject', 'Start Date': '2026/11/09'},),
            JobPlan(),
            (),
            0,
            ('source',),
            'calendar',
            False,
        )

    def put(self, **kwargs):
        return self.repo.put(
            self.scope,
            attachment_fingerprint='source-revision',
            payload=self.payload,
            ttl_seconds=kwargs.get('ttl', 60.0),
        )

    def test_restart_roundtrip_encryption_and_scope(self):
        saved = self.put()
        restarted = PostgresClarifications(AutomationStore(self.key))
        self.assertEqual(restarted.get(self.scope), saved)
        self.assertIsNone(restarted.get(PendingClarificationScope('other', 'chat')))
        with self.backend.db() as db:
            raw = bytes(
                db.execute('SELECT payload FROM pending_clarifications').fetchone()[
                    'payload'
                ]
            )
        self.assertNotIn(b'private subject', raw)

    def test_only_one_worker_claims_and_original_expiry_is_preserved(self):
        saved = self.put()

        def claim(_):
            return PostgresClarifications(self.backend).claim(
                self.scope,
                attachment_fingerprint='source-revision',
                expected_generation=saved.generation,
            )

        with ThreadPoolExecutor(max_workers=8) as pool:
            claims = list(pool.map(claim, range(8)))
        record = next(c for c in claims if c is not None)
        self.assertEqual(sum(c is not None for c in claims), 1)
        self.assertIsNone(PostgresClarifications(self.backend).get(self.scope))
        self.assertTrue(self.repo.restore(record))
        self.assertEqual(self.repo.get(self.scope).expires_at, saved.expires_at)
        self.assertFalse(self.repo.restore(record))

    def test_new_upload_rejects_stale_claim_restore_advance_and_finalize(self):
        old = self.put()
        claimed = self.repo.claim(self.scope, attachment_fingerprint='source-revision')
        new = self.put()
        self.assertNotEqual(old.generation, new.generation)
        self.assertIsNone(
            self.repo.claim(
                self.scope,
                attachment_fingerprint='source-revision',
                expected_generation=old.generation,
            )
        )
        self.assertFalse(
            self.repo.discard(self.scope, expected_generation=old.generation)
        )
        self.assertFalse(self.repo.restore(claimed))
        self.assertFalse(self.repo.advance(claimed, self.payload))
        self.assertFalse(self.repo.finalize(claimed))
        self.assertEqual(self.repo.get(self.scope), new)
        self.repo.discard(self.scope)
        self.assertFalse(self.repo.restore(claimed))

    def test_expiration_wrong_fingerprint_and_atomic_finalization(self):
        self.put()
        self.assertIsNone(
            self.repo.claim(self.scope, attachment_fingerprint='changed-source')
        )
        claimed = self.repo.claim(self.scope, attachment_fingerprint='source-revision')
        self.assertTrue(self.repo.finalize(claimed))
        self.assertFalse(self.repo.restore(claimed))
        self.put()
        with self.backend.db() as db:
            db.execute('UPDATE pending_clarifications SET expires_at=0')
        self.assertIsNone(self.repo.get(self.scope))
        self.assertIsNone(
            self.repo.claim(self.scope, attachment_fingerprint='source-revision')
        )

    def test_old_claim_cannot_restore_after_a_later_claim_or_replace_the_key(self):
        self.put()
        first = self.repo.claim(self.scope, attachment_fingerprint='source-revision')
        self.assertTrue(self.repo.restore(first))
        second = self.repo.claim(self.scope, attachment_fingerprint='source-revision')
        self.assertNotEqual(first.generation, second.generation)
        self.assertFalse(self.repo.restore(first))
        self.assertFalse(self.repo.finalize(first))
        self.assertTrue(self.repo.restore(second))
        self.key.unlink()
        with self.assertRaisesRegex(ValueError, '金鑰遺失'):
            AutomationStore(self.key)
        self.assertFalse(self.key.exists())

    def test_active_conversations_are_not_evicted_by_a_process_capacity_limit(self):
        self.put()
        for index in range(16):
            self.repo.put(
                PendingClarificationScope('owner', f'chat-{index}'),
                attachment_fingerprint='source-revision',
                payload=self.payload,
                ttl_seconds=60.0,
            )
        restarted = PostgresClarifications(AutomationStore(self.key))
        self.assertIsNotNone(restarted.get(self.scope))
        self.assertIsNotNone(
            restarted.get(PendingClarificationScope('owner', 'chat-15'))
        )

    def test_concurrent_first_start_shares_one_complete_key(self):
        from threading import Barrier
        empty_key = self.key.parent / 'concurrent-key'
        start = Barrier(6)
        def open_store(_):
            start.wait(timeout=5)
            return AutomationStore(empty_key)
        with ThreadPoolExecutor(max_workers=6) as pool:
            stores = list(pool.map(open_store, range(6)))
        encrypted = stores[0].encode('owner', {'value': 'shared'})
        for backend in stores:
            self.assertEqual(backend.decode('owner', encrypted), {'value': 'shared'})
