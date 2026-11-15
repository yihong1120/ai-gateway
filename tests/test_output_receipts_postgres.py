"""Durable output reservations serialize writers and survive continuation expiry."""

from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest

from examples.official_document_workflow.infrastructure.storage.automation_store import (
    AutomationStore,
)
from examples.official_document_workflow.infrastructure.storage.output_receipts import (
    PostgresOutputReceipts,
)
from tests.eip_postgres import EIPPostgresTestCase


class OutputReceiptTests(EIPPostgresTestCase, unittest.TestCase):
    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.key = Path(temporary.name) / 'secret'
        self.store = AutomationStore(self.key)
        self.repo = PostgresOutputReceipts(lambda: self.store)

    def begin(self):
        return self.repo.begin(
            'output-id', 'owner', 'private-title.csv', 'hash', 'source-id'
        )

    def test_concurrent_workers_reserve_one_writer_and_encrypt_metadata(self):
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda _: self.begin(), range(6)))
        self.assertEqual(sum(started for _, started in results), 1)
        self.assertEqual({receipt.id for receipt, _ in results}, {'output-id'})
        self.assertIsNone(self.repo.get('output-id', 'other'))
        with self.store.db() as db:
            data = bytes(
                db.execute('SELECT payload FROM clarification_outputs').fetchone()[
                    'payload'
                ]
            )
        self.assertNotIn(b'private-title', data)

    def test_only_confirmed_nonwrites_allow_a_new_writer(self):
        record, _ = self.begin()
        record = self.repo.mark(record, 'unknown')
        self.assertFalse(self.begin()[1])
        restarted = PostgresOutputReceipts(lambda: AutomationStore(self.key))
        self.assertEqual(restarted.get(record.id, 'owner').state, 'unknown')
        record = self.repo.mark(record, 'stored')
        self.assertFalse(self.begin()[1])
        self.assertEqual(self.repo.mark(record, 'not_written').state, 'stored')
        retry, _ = self.repo.begin('retry-id', 'owner', 'x.csv', 'hash', 'source')
        self.repo.mark(retry, 'not_written')
        self.assertTrue(
            self.repo.begin('retry-id', 'owner', 'x.csv', 'new-hash', 'source')[1]
        )

    def test_receipts_are_kept_and_a_lost_key_is_not_replaced(self):
        record, _ = self.begin()
        self.repo.mark(record, 'unknown')
        with self.store.db() as db:
            db.execute('DELETE FROM pending_clarifications')
        self.key.unlink()
        with self.assertRaisesRegex(ValueError, '金鑰遺失'):
            AutomationStore(self.key)
        self.assertFalse(self.key.exists())

    def test_manual_reviews_are_encrypted_and_do_not_reopen_unknown_writes(self):
        record, _ = self.begin()
        self.repo.mark(record, 'unknown')
        review_id = self.repo.record_review(record, actor='operator', method='manual',
                                           outcome='reported_missing', note='private investigation note')
        self.assertGreater(review_id, 0)
        self.assertEqual(self.repo.get(record.id, 'owner').state, 'unknown')
        self.assertFalse(self.begin()[1])
        self.assertEqual(self.repo.reviews(record.id, 'owner')[0]['note'], 'private investigation note')
        self.assertEqual(self.repo.reviews(record.id, 'other'), [])
        with self.store.db() as db:
            data = bytes(db.execute('SELECT payload FROM clarification_output_reviews').fetchone()['payload'])
        self.assertNotIn(b'private investigation', data)

    def test_verified_content_can_finish_expired_continuations_but_stale_proof_is_rejected(self):
        record, _ = self.begin()
        self.repo.mark(record, 'unknown')
        with self.store.db() as db:
            db.execute('DELETE FROM pending_clarifications')
        with self.assertRaises(ValueError):
            self.repo.record_review(record, actor='admin', method='webui_sha256', outcome='verified')
        self.repo.record_review(record, actor='admin', method='webui_sha256', outcome='verified',
                                proof={'sha256': record.sha256, 'bytes': 12})
        self.assertEqual(self.repo.get(record.id, 'owner').state, 'stored')
        retry, _ = self.repo.begin('retry-id', 'owner', 'a.csv', 'old-hash', 'source')
        self.repo.mark(retry, 'not_written')
        self.repo.begin('retry-id', 'owner', 'a.csv', 'new-hash', 'source')
        with self.assertRaises(ValueError):
            self.repo.record_review(retry, actor='admin', method='webui_sha256', outcome='verified',
                                    proof={'sha256': 'old-hash', 'bytes': 12})
        self.assertEqual(self.repo.reviews('retry-id', 'owner'), [])
